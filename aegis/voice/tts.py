"""Text to speech, with a pre-rendered acknowledgement cache.

The cache is the point. Perceived latency is dominated by *response onset*, not
completion, so the moment a command is submitted we play a cached
"Right away, sir." while the planner is still thinking. The real reply follows
when execution finishes.

Speech runs on its own worker thread with its own COM apartment, and requests
are serialised through a queue so utterances never overlap.

Backend note: Piper is the default when its voice files are present
(``var/voices/jarvis-high.onnx`` - a JARVIS-styled en_GB fine-tune, MIT
licensed, from https://huggingface.co/jgkawell/jarvis). SAPI5 remains as a
zero-download fallback for a machine with no Piper model on disk.

Each backend has a distinct ``cache_key``, and the acknowledgement cache is
namespaced under it. Without that, switching backends would silently keep
serving acknowledgements pre-rendered in the *old* voice, since
``synth_to_wav`` skips files that already exist.
"""

from __future__ import annotations

import logging
import queue
import random
import threading
from pathlib import Path
from typing import Protocol

from aegis.config import settings

log = logging.getLogger(__name__)

ACK_PHRASES = [
    "Right away, sir.",
    "Of course, sir.",
    "At once, sir.",
    "Certainly, sir.",
]


class TtsBackend(Protocol):
    #: Namespaces the acknowledgement cache. Must change whenever the voice
    #: does, or stale pre-rendered audio in a different voice gets reused.
    cache_key: str

    def speak(self, text: str) -> None: ...
    def synth_to_wav(self, text: str, path: Path) -> bool: ...


class NullBackend:
    """Used when no speech engine is available. Keeps the app fully usable."""

    cache_key = "null"

    def speak(self, text: str) -> None:
        log.info("[tts:disabled] %s", text)

    def synth_to_wav(self, text: str, path: Path) -> bool:
        return False


def _prepend_silence(path: Path, milliseconds: int) -> None:
    """Pad a WAV file with leading silence, in place.

    Piper writes samples straight to the wave.Wave_write object as it
    synthesises, so there is no hook to inject silence before its own first
    write. Padding is done as a cheap second pass instead: read the finished
    file back, prepend zero frames matching its own format, rewrite it.
    """
    import wave  # noqa: PLC0415

    if milliseconds <= 0:
        return

    with wave.open(str(path), "rb") as src:
        params = src.getparams()
        frames = src.readframes(src.getnframes())

    silence_frame_count = int(params.framerate * milliseconds / 1000)
    silence = b"\x00" * (silence_frame_count * params.sampwidth * params.nchannels)

    with wave.open(str(path), "wb") as dst:
        dst.setparams(params)
        dst.writeframes(silence + frames)


class PiperBackend:
    """Piper (ONNX, CPU). Loads a single voice model at construction.

    Piper has no direct-to-speaker output; every utterance is synthesised to a
    WAV file first. That's a fine fit here since utterances are already
    serialised through the TTS queue, so ``speak()`` just reuses one scratch
    file rather than allocating a temp file per call.
    """

    cache_key: str

    def __init__(self, model_path: Path, config_path: Path) -> None:
        from piper import PiperVoice  # noqa: PLC0415

        self._voice = PiperVoice.load(str(model_path), config_path=str(config_path))
        # The voice, the speed and the silence padding all affect the audio
        # content, so all three must be part of the cache key - otherwise a
        # settings change (e.g. tuning AEGIS_TTS_LEAD_SILENCE_MS) would edit
        # what live speech sounds like but silently leave the cached
        # acknowledgements playing the old, stale padding.
        self.cache_key = (
            f"piper-{model_path.stem}-ls{settings.tts_speed}-pad{settings.tts_lead_silence_ms}"
        )
        self._scratch = settings.tts_cache_dir / "_piper_scratch.wav"
        settings.tts_cache_dir.mkdir(parents=True, exist_ok=True)

    def _render(self, text: str, path: Path) -> None:
        import wave  # noqa: PLC0415

        from piper.config import SynthesisConfig  # noqa: PLC0415

        cfg = SynthesisConfig(length_scale=settings.tts_speed)
        with wave.open(str(path), "wb") as wav_file:
            self._voice.synthesize_wav(text, wav_file, syn_config=cfg)
        _prepend_silence(path, settings.tts_lead_silence_ms)

    def speak(self, text: str) -> None:
        import winsound  # noqa: PLC0415

        self._render(text, self._scratch)
        winsound.PlaySound(str(self._scratch), winsound.SND_FILENAME)

    def synth_to_wav(self, text: str, path: Path) -> bool:
        try:
            self._render(text, path)
            return True
        except Exception:
            log.exception("failed to pre-render %r", text)
            return False


class Sapi5Backend:
    """Windows SAPI5. Prefers an en-GB voice, and a male one, when installed."""

    cache_key: str

    def __init__(self) -> None:
        import win32com.client  # noqa: PLC0415 - imported on the worker thread

        self._voice = win32com.client.Dispatch("SAPI.SpVoice")
        self._dispatch = win32com.client.Dispatch
        self._select_voice()
        self.cache_key = f"sapi5-pad{settings.tts_lead_silence_ms}"

    def _select_voice(self) -> None:
        try:
            voices = list(self._voice.GetVoices())
        except Exception:
            log.warning("could not enumerate SAPI voices")
            return

        def score(v) -> int:
            desc = v.GetDescription().lower()
            points = 0
            if settings.tts_voice_hint.lower() in desc or "united kingdom" in desc or "british" in desc:
                points += 2
            if any(name in desc for name in ("george", "james", "ryan", "male")):
                points += 1
            return points

        best = max(voices, key=score, default=None)
        if best is not None:
            self._voice.Voice = best
            log.info("SAPI voice: %s", best.GetDescription())

    def speak(self, text: str) -> None:
        self._voice.Speak(text)  # synchronous, so the queue serialises naturally

    def synth_to_wav(self, text: str, path: Path) -> bool:
        try:
            stream = self._dispatch("SAPI.SpFileStream")
            stream.Open(str(path), 3)  # SSFMCreateForWrite
            try:
                self._voice.AudioOutputStream = stream
                self._voice.Speak(text)
            finally:
                self._voice.AudioOutputStream = None
                stream.Close()
            # This cache is played back through winsound (see _play_cached),
            # so it needs the same Bluetooth wake-up padding as Piper's.
            _prepend_silence(path, settings.tts_lead_silence_ms)
            return True
        except Exception:
            log.exception("failed to pre-render %r", text)
            return False


class TtsEngine:
    """Queue-backed speech with a warm acknowledgement cache."""

    def __init__(self) -> None:
        self._queue: queue.Queue[tuple[str, str] | None] = queue.Queue()
        self._ack_cache: dict[str, Path] = {}
        self._backend: TtsBackend = NullBackend()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, name="aegis-tts", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._queue.put(None)
        self._thread.join(timeout=3.0)

    def say(self, text: str) -> None:
        if settings.tts_enabled and text.strip():
            self._queue.put(("say", text))

    def ack(self) -> None:
        """Play a cached acknowledgement. Cheap enough to fire on every command."""
        if settings.tts_enabled:
            self._queue.put(("ack", random.choice(ACK_PHRASES)))

    # --- worker ---------------------------------------------------------
    def _run(self) -> None:
        import pythoncom  # noqa: PLC0415

        pythoncom.CoInitialize()
        try:
            self._backend = self._select_backend()
            self._warm_ack_cache()
            self._ready.set()

            while True:
                item = self._queue.get()
                if item is None:
                    break
                kind, text = item
                try:
                    if kind == "ack" and self._play_cached(text):
                        continue
                    self._backend.speak(text)
                except Exception:
                    log.exception("speech failed for %r", text)
        finally:
            pythoncom.CoUninitialize()

    def _select_backend(self) -> TtsBackend:
        want = settings.tts_backend
        piper_available = settings.tts_piper_model.exists() and settings.tts_piper_config.exists()

        if want in ("piper", "auto") and piper_available:
            try:
                backend = PiperBackend(settings.tts_piper_model, settings.tts_piper_config)
                log.info("TTS backend: piper (%s)", settings.tts_piper_model.name)
                return backend
            except Exception:
                log.exception("Piper backend failed to load")
                if want == "piper":
                    return NullBackend()

        if want in ("sapi5", "auto"):
            try:
                return Sapi5Backend()
            except Exception:
                log.exception("SAPI5 unavailable")

        log.warning("no TTS backend available; speech disabled")
        return NullBackend()

    def _warm_ack_cache(self) -> None:
        settings.ensure_dirs()
        # Namespaced by backend so switching voices can't silently replay
        # acknowledgements pre-rendered in a different one.
        cache_dir = settings.tts_cache_dir / self._backend.cache_key
        cache_dir.mkdir(parents=True, exist_ok=True)
        for index, phrase in enumerate(ACK_PHRASES):
            path = cache_dir / f"ack_{index}.wav"
            if not path.exists() and not self._backend.synth_to_wav(phrase, path):
                continue
            if path.exists():
                self._ack_cache[phrase] = path
        log.info(
            "acknowledgement cache warm (%s): %d/%d",
            self._backend.cache_key, len(self._ack_cache), len(ACK_PHRASES),
        )

    def _play_cached(self, phrase: str) -> bool:
        path = self._ack_cache.get(phrase)
        if path is None:
            return False
        import winsound  # noqa: PLC0415

        # No SND_ASYNC: blocking here is what keeps utterances from overlapping.
        winsound.PlaySound(str(path), winsound.SND_FILENAME)
        return True

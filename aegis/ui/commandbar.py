"""Spotlight-style command bar, summoned by a global hotkey."""

from __future__ import annotations

import ctypes
import logging

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, Qt, Signal
from PySide6.QtGui import QColor, QCursor, QGuiApplication, QKeyEvent, QPalette
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsDropShadowEffect,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from aegis.config import settings
from aegis.execute import windows as win

log = logging.getLogger(__name__)

user32 = ctypes.WinDLL("user32", use_last_error=True)
ASFW_ANY = -1

# Space reserved around the visible card for the glow to blur into. Qt clips a
# QGraphicsEffect to its widget's own rect, so the glow needs a transparent
# margin to bleed into or it gets cut off flush with the card's edge.
GLOW_MARGIN = 28
CARD_WIDTH = 640
ACCENT = QColor(110, 190, 235)

STYLE = """
#card {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 rgba(21, 27, 37, 214),
        stop:1 rgba(9, 12, 18, 214));
    border: 1px solid rgba(110, 190, 235, 165);
    border-radius: 16px;
}
QLineEdit {
    background: transparent;
    border: none;
    color: #dff1ff;
    font-size: 22px;
    font-family: "Segoe UI", sans-serif;
    padding: 6px 4px;
    selection-background-color: rgba(110, 190, 235, 110);
}
QLabel#status {
    color: rgba(150, 200, 230, 190);
    font-size: 12px;
    font-family: "Segoe UI", sans-serif;
    padding: 0px 4px;
}
"""


class CommandBar(QWidget):
    submitted = Signal(str)
    confirm_answered = Signal(bool)

    def __init__(self) -> None:
        super().__init__()
        self._pending_confirm = False
        # The top-level window itself stays fully transparent and unstyled;
        # everything visible lives on the `card` child so the glow effect has
        # room to render outside the card's own edge instead of getting
        # clipped at the window boundary.
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(GLOW_MARGIN, GLOW_MARGIN, GLOW_MARGIN, GLOW_MARGIN)

        card = QFrame(self)
        card.setObjectName("card")
        card.setFixedWidth(CARD_WIDTH)
        card.setStyleSheet(STYLE)
        outer.addWidget(card)

        glow = QGraphicsDropShadowEffect(card)
        glow.setBlurRadius(48)
        glow.setOffset(0, 0)
        glow.setColor(QColor(ACCENT.red(), ACCENT.green(), ACCENT.blue(), 150))
        card.setGraphicsEffect(glow)

        self.input = QLineEdit()
        self.input.setPlaceholderText(f"Command {settings.persona_name}…")
        self.input.returnPressed.connect(self._submit)
        placeholder_palette = self.input.palette()
        placeholder_palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(160, 200, 225, 140))
        self.input.setPalette(placeholder_palette)

        self.status = QLabel("")
        self.status.setObjectName("status")

        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(20, 16, 20, 14)
        card_layout.setSpacing(6)
        card_layout.addWidget(self.input)
        card_layout.addWidget(self.status)

        self._fade = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade.setDuration(140)
        self._fade.setStartValue(0.0)
        self._fade.setEndValue(1.0)
        self._fade.setEasingCurve(QEasingCurve.Type.OutCubic)

    # --- visibility -----------------------------------------------------
    def toggle(self) -> None:
        # Hiding mid-confirmation would strand the pipeline thread waiting for
        # an answer, so the hotkey is inert until the question is resolved.
        if self._pending_confirm:
            self.raise_()
            self.activateWindow()
            return
        if self.isVisible():
            self.hide()
        else:
            self.summon()

    def summon(self) -> None:
        self._centre_on_active_screen()
        self.input.clear()
        self.status.setText("")
        self.setWindowOpacity(0.0)
        self.show()
        self._activate()
        self.input.setFocus(Qt.FocusReason.OtherFocusReason)
        self._fade.stop()
        self._fade.start()

    def _activate(self) -> None:
        """Take real keyboard focus, even while another app holds foreground.

        Qt's activateWindow() alone loses Windows' foreground race whenever we
        were not the last process to receive input - which is exactly the
        state after a plan ran focus_window on some other app. focus_window()
        does the documented AttachThreadInput handover, the same mechanism the
        executor itself uses; the plain SetForegroundWindow fallback covers
        the hotkey case, where our recent input grants us the right directly.
        """
        self.raise_()
        self.activateWindow()
        if not win.focus_window(int(self.winId())):
            try:
                user32.AllowSetForegroundWindow(ASFW_ANY)
                user32.SetForegroundWindow(int(self.winId()))
            except OSError:
                log.debug("SetForegroundWindow failed", exc_info=True)

    def _centre_on_active_screen(self) -> None:
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        area = screen.availableGeometry()
        self.adjustSize()
        x = area.x() + (area.width() - self.width()) // 2
        y = area.y() + int(area.height() * 0.28)
        self.move(x, y)

    # --- confirmation ---------------------------------------------------
    def ask_confirm(self, question: str) -> None:
        """Enter a modal-ish confirm state without opening a second window.

        The input is disabled so Enter cannot be swallowed by the line edit,
        and focus moves to the widget itself so its keyPressEvent sees the
        answer.

        _activate() is not optional here: after a plan ran focus_window, the
        OS foreground is the target app, and without a real focus grab the
        user's Enter would land *in that app* while the question times out
        unanswered. The pipeline restores the previous foreground window once
        the question is resolved.
        """
        self._pending_confirm = True
        if not self.isVisible():
            self.summon()
        self.input.setEnabled(False)
        self.input.setText("")
        self.status.setText(f"{question}?   [Enter] proceed   [Esc] cancel")
        self._activate()
        self.setFocus(Qt.FocusReason.OtherFocusReason)

    def cancel_confirm(self) -> None:
        """Leave the confirm state without emitting an answer (e.g. on timeout)."""
        self._pending_confirm = False
        self.input.setEnabled(True)
        self.input.setFocus(Qt.FocusReason.OtherFocusReason)

    def _resolve_confirm(self, approved: bool) -> None:
        self._pending_confirm = False
        self.input.setEnabled(True)
        self.status.setText("Proceeding…" if approved else "Cancelled.")
        self.input.setFocus(Qt.FocusReason.OtherFocusReason)
        self.confirm_answered.emit(approved)

    # --- events ---------------------------------------------------------
    def keyPressEvent(self, event: QKeyEvent) -> None:
        if self._pending_confirm:
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self._resolve_confirm(True)
            elif event.key() == Qt.Key.Key_Escape:
                self._resolve_confirm(False)
            # Everything else is swallowed: while a confirmation is pending
            # there is no other meaningful input.
            return

        if event.key() == Qt.Key.Key_Escape:
            self.hide()
            return
        super().keyPressEvent(event)

    def _submit(self) -> None:
        text = self.input.text().strip()
        if text:
            self.input.clear()
            self.submitted.emit(text)

    def set_status(self, text: str) -> None:
        self.status.setText(text)

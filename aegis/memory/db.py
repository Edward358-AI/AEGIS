"""SQLite task store.

Deliberately small: tasks, an optional category, an optional due date, and a
done flag. The agent's job is frictionless capture ("log that I finished
history"), not project management.

Connections are opened per call rather than held. The pipeline runs on a
worker thread and SQLite connections are not safe to share across threads by
default; a local file database makes per-call connection cost irrelevant.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from aegis.config import settings

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    title      TEXT    NOT NULL,
    category   TEXT,
    due        TEXT,
    done       INTEGER NOT NULL DEFAULT 0,
    created_at TEXT    NOT NULL,
    done_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_open ON tasks(done, due);
"""


@dataclass
class Task:
    id: int
    title: str
    category: str | None
    due: str | None
    done: bool

    def describe(self) -> str:
        bits = [self.title]
        if self.category:
            bits.append(f"({self.category})")
        if self.due:
            bits.append(f"due {self.due}")
        return " ".join(bits)


def _connect() -> sqlite3.Connection:
    settings.ensure_dirs()
    conn = sqlite3.connect(settings.data_dir / "aegis.db")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def parse_due(text: str | None) -> str | None:
    """Turn loose date phrasing into ISO, or None if it is not a date.

    The model is told to pass ISO dates, but 3B models drift to "tomorrow"
    regularly, so the common relative words are handled here rather than
    letting them land in the database as literal strings.
    """
    if not text:
        return None
    raw = text.strip().lower()
    today = date.today()
    relative = {
        "today": 0, "tonight": 0, "tomorrow": 1,
        "next week": 7, "next month": 30,
    }
    if raw in relative:
        return (today + timedelta(days=relative[raw])).isoformat()

    weekdays = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    for index, name in enumerate(weekdays):
        if name in raw:
            ahead = (index - today.weekday()) % 7 or 7
            return (today + timedelta(days=ahead)).isoformat()

    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(text.strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return None


def add_task(title: str, category: str | None = None, due: str | None = None) -> Task:
    created = datetime.now().isoformat(timespec="seconds")
    with _connect() as conn:
        cursor = conn.execute(
            "INSERT INTO tasks (title, category, due, created_at) VALUES (?, ?, ?, ?)",
            (title.strip(), (category or "").strip() or None, parse_due(due), created),
        )
        return Task(cursor.lastrowid, title.strip(), category, parse_due(due), False)


def complete_task(query: str) -> Task | None:
    """Mark the best open match for ``query`` as done.

    Matches on a LIKE against the title, preferring the oldest open task so
    "finished the essay" resolves to the essay you have been putting off
    rather than one added moments ago.
    """
    needle = f"%{query.strip()}%"
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM tasks WHERE done = 0 AND title LIKE ? ORDER BY created_at LIMIT 1",
            (needle,),
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE tasks SET done = 1, done_at = ? WHERE id = ?",
            (datetime.now().isoformat(timespec="seconds"), row["id"]),
        )
        return Task(row["id"], row["title"], row["category"], row["due"], True)


def open_tasks(limit: int = 10) -> list[Task]:
    """Open tasks, soonest due first; undated ones last."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE done = 0 "
            "ORDER BY CASE WHEN due IS NULL THEN 1 ELSE 0 END, due, created_at LIMIT ?",
            (limit,),
        ).fetchall()
    return [Task(r["id"], r["title"], r["category"], r["due"], False) for r in rows]


def summarise_open(limit: int = 5) -> str:
    """One-line spoken-friendly summary of what is outstanding."""
    tasks = open_tasks(limit)
    if not tasks:
        return "You have nothing outstanding, sir."
    if len(tasks) == 1:
        return f"One task outstanding: {tasks[0].describe()}."
    listed = "; ".join(t.describe() for t in tasks)
    return f"{len(tasks)} tasks outstanding: {listed}."

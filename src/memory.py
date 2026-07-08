"""
memory.py

Handles everything related to Jarvis's persistent memory: a single SQLite
database file that survives between runs, so the assistant doesn't start
from zero every time you talk to it.

SQLite is a full database engine that lives in one plain file on disk
(no server to install or run) - a good fit for a single-user personal
project like this.
"""

import sqlite3
from datetime import datetime
from pathlib import Path

# Path to the database file. Lives in memory/, which is gitignored -
# this is your personal data, it shouldn't end up on GitHub.
DB_PATH = Path(__file__).parent.parent / "memory" / "jarvis.db"


def init_db():
    """
    Creates the database file and the `memory` table if they don't
    already exist. Safe to call every time the program starts - it
    won't wipe existing data.
    """
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            category TEXT NOT NULL,
            content TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()


def remember(category: str, content: str):
    """
    Saves one fact to memory.

    category: a short label like "preference", "project_note", or "log" -
        lets us later pull back just the kind of memory we need instead
        of everything ever stored.
    content: the actual thing to remember, in plain text.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO memory (created_at, category, content) VALUES (?, ?, ?)",
        (datetime.now().isoformat(), category, content),
    )
    conn.commit()
    conn.close()


def forget(keyword: str) -> int:
    """
    Deletes any memory whose content contains the given keyword/phrase
    (case-insensitive, partial match) - across every category, since a
    stale fact can hide in preferences, notes, or the raw log alike.

    Returns how many rows were actually deleted, so the caller can tell
    whether anything really happened.
    """
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.execute(
        "DELETE FROM memory WHERE content LIKE ? COLLATE NOCASE",
        (f"%{keyword}%",),
    )
    conn.commit()
    deleted = cursor.rowcount
    conn.close()
    return deleted


def recall(category: str = None, limit: int = 20) -> list[str]:
    """
    Pulls back recent memories, most recent first.

    category: if given, only return memories with that label.
        If None, return across all categories.
    limit: max number of entries to return, so we don't hand the
        entire history to Claude every single time.
    """
    conn = sqlite3.connect(DB_PATH)
    if category:
        rows = conn.execute(
            "SELECT content FROM memory WHERE category = ? ORDER BY id DESC LIMIT ?",
            (category, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT content FROM memory ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    conn.close()
    # rows is a list of one-item tuples like [("some content",), ...] -
    # unwrap them into a plain list of strings.
    return [row[0] for row in rows]

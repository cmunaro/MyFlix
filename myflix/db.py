import sqlite3
from contextlib import contextmanager


@contextmanager
def connect(path):
    db = sqlite3.connect(path, timeout=15)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    try:
        with db:
            yield db
    finally:
        db.close()


def initialize(path):
    with connect(path) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY, value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS media (
                id INTEGER PRIMARY KEY,
                root TEXT NOT NULL,
                path TEXT NOT NULL,
                title TEXT NOT NULL,
                series TEXT NOT NULL DEFAULT '',
                season INTEGER,
                episode INTEGER,
                hidden INTEGER NOT NULL DEFAULT 0,
                available INTEGER NOT NULL DEFAULT 1,
                size INTEGER NOT NULL,
                added_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(root, path)
            );
            CREATE TABLE IF NOT EXISTS progress (
                media_id INTEGER PRIMARY KEY REFERENCES media(id),
                position REAL NOT NULL DEFAULT 0,
                duration REAL NOT NULL DEFAULT 0,
                watched INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS artwork (
                key TEXT PRIMARY KEY, retry_at INTEGER NOT NULL
            );
            PRAGMA user_version=1;
        """)


def settings(db):
    return dict(db.execute("SELECT key,value FROM settings").fetchall())

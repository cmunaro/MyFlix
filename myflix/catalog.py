"""Filesystem indexing. The configured root owns all media access."""
import os
import re
import time
from pathlib import Path

from .db import connect, settings

EXTENSIONS = {".mp4", ".m4v", ".mkv", ".webm", ".mov"}
EPISODE = re.compile(r"(?i)(?:s(\d{1,3})[ ._-]*e(\d{1,4})|(\d{1,3})x(\d{1,4}))")


def identify(path):
    match = EPISODE.search(path.stem)
    clean = lambda name: re.sub(r"[._]+", " ", name).strip(" -")
    if not match:
        return clean(path.stem), "", None, None
    season = int(match[1] or match[3])
    episode = int(match[2] or match[4])
    series = clean(path.stem[:match.start()])
    if not series:
        parent = path.parent
        if re.match(r"(?i)^season[ ._-]*\d+$", parent.name):
            parent = parent.parent
        series = clean(parent.name) or "Unknown series"
    title = clean(path.stem[match.end():]) or f"Episode {episode}"
    return title, series, season, episode


def media_path(root, relative):
    root = Path(root).resolve(strict=True)
    path = (root / relative).resolve(strict=True)
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError("Media must be a file inside the configured folder")
    return path


def scan(database):
    # Complete enumeration before any availability updates. An offline mount or
    # unreadable subtree must not turn a whole library into missing media.
    with connect(database) as db:
        root_value = settings(db).get("videos_path", "")
    if not root_value:
        return 0
    root = Path(root_value)
    if not root.is_dir():
        raise ValueError("Video folder is unavailable; catalog preserved")
    found = []
    def fail(error):
        raise error
    for directory, dirs, names in os.walk(root, onerror=fail, followlinks=False):
        dirs[:] = [d for d in dirs if not d.startswith(".") and not (Path(directory) / d).is_symlink()]
        for name in names:
            path = Path(directory) / name
            if name.startswith(".") or path.suffix.lower() not in EXTENSIONS or path.is_symlink():
                continue
            stat = path.stat()
            # Safety buffer only. Downloads must live outside the library until
            # completed; a stalled writer cannot be detected from mtime alone.
            if not stat.st_size or time.time() - stat.st_mtime < 60:
                continue
            relative = path.relative_to(root)
            found.append((str(relative), stat.st_size, identify(relative)))
    with connect(database) as db:
        # Folder changes and indexing serialize through SQLite's write lock.
        db.execute("BEGIN IMMEDIATE")
        if settings(db).get("videos_path") != root_value:
            return 0
        db.execute("UPDATE media SET available=0")
        for relative, size, (title, series, season, episode) in found:
            db.execute("""INSERT INTO media(root,path,title,series,season,episode,size)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(root,path) DO UPDATE SET
                available=1,size=excluded.size""",
                (root_value, relative, title, series, season, episode, size))
        db.execute("INSERT OR REPLACE INTO settings VALUES('last_scan',?)", (str(int(time.time())),))
        db.execute("DELETE FROM settings WHERE key='scan_error'")
    return len(found)


def scan_safely(database):
    try:
        return scan(database)
    except (OSError, ValueError) as error:
        with connect(database) as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES('scan_error',?)", (str(error),))
        raise

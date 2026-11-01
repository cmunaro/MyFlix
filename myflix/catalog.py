"""Filesystem indexing. The configured root owns all media access."""
import os
import json
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


def source_identity(path):
    title, series, season, episode = identify(Path(path))
    return json.dumps([series, season, episode] if series else [title])


def library_files(root, *, minimum_age=60):
    """Enumerate confined media; recovery also checks newly published files."""
    root = Path(root)
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
            if not stat.st_size or time.time() - stat.st_mtime < minimum_age:
                continue
            relative = path.relative_to(root)
            found.append((str(relative), stat.st_size, identify(relative)))
    return found


def scan(database):
    # Complete enumeration before any availability updates. An offline mount or
    # unreadable subtree must not turn a whole library into missing media.
    with connect(database) as db:
        root_value = settings(db).get("videos_path", "")
    if not root_value:
        return 0
    found = library_files(root_value)
    with connect(database) as db:
        # Folder changes and indexing serialize through SQLite's write lock.
        db.execute("BEGIN IMMEDIATE")
        if settings(db).get("videos_path") != root_value:
            return 0
        for row in db.execute("SELECT id,path FROM media WHERE id NOT IN (SELECT media_id FROM media_identity)").fetchall():
            db.execute("INSERT INTO media_identity VALUES(?,?)", (row["id"], source_identity(row["path"])))
        db.execute("UPDATE media SET available=0")
        found_paths = {relative for relative, _, _ in found}
        aliases = {}
        for row in db.execute("SELECT series,payload FROM subscriptions WHERE root=?", (root_value,)):
            aliases.setdefault(json.loads(row["payload"]).get("name"), []).append(row["series"])
        for relative, size, (title, series, season, episode) in found:
            key = source_identity(relative)
            if len(aliases.get(series, [])) == 1:
                series = aliases[series][0]
            existing = db.execute("SELECT id FROM media WHERE root=? AND path=?", (root_value, relative)).fetchone()
            if not existing:
                # Reattach an unambiguous returning title to its durable catalog
                # record, retaining edits, hidden state and playback history.
                candidates = db.execute("""SELECT m.id,m.path FROM media m JOIN media_identity i
                    ON i.media_id=m.id WHERE root=? AND i.identity=?""", (root_value, key)).fetchall()
                candidates = [row for row in candidates if row["path"] not in found_paths]
                if len(candidates) == 1:
                    db.execute("UPDATE media SET path=? WHERE id=?", (relative, candidates[0]["id"]))
            db.execute("""INSERT INTO media(root,path,title,series,season,episode,size)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(root,path) DO UPDATE SET
                available=1,size=excluded.size""",
                (root_value, relative, title, series, season, episode, size))
            db.execute("""INSERT OR IGNORE INTO media_identity(media_id,identity)
                SELECT id,? FROM media WHERE root=? AND path=?""", (key, root_value, relative))
        # A season marked watched also applies to episodes imported later.
        db.execute("""INSERT INTO progress(media_id,watched)
            SELECT m.id,1 FROM media m JOIN season_state s
            ON s.root=m.root AND s.series=m.series AND s.season=m.season
            WHERE s.watched=1 AND m.id NOT IN (SELECT media_id FROM progress)
        """)
        db.execute("""UPDATE discoveries SET state='downloaded',error='' WHERE state IN ('submitted','uncertain','failed')
            AND EXISTS(SELECT 1 FROM subscriptions s JOIN media m ON m.root=s.root AND m.series=s.series
                WHERE s.id=discoveries.subscription_id AND m.season=discoveries.season
                AND m.episode=discoveries.episode AND m.available=1)""")
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

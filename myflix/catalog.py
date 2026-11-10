"""Filesystem indexing. The configured root owns all media access."""
import os
import hashlib
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


def remove_files(db, items):
    """Remove confined videos, recording each success even after a partial failure."""
    failed = 0
    for item in items:
        try:
            path = media_path(item["root"], item["path"])
            if path != Path(item["root"]).resolve(strict=True) / item["path"]:
                raise ValueError("Refusing to remove a symlink target")
            path.unlink()
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            failed += 1
            continue
        db.execute("UPDATE media SET available=0 WHERE id=?", (item["id"],))
    return failed


def watched_cleanup(db, root):
    """Snapshot only watched, available, confined videos for cleanup confirmation."""
    groups = {"episodes": [], "movies": []}
    fingerprints = []
    for row in db.execute("""SELECT m.* FROM media m JOIN progress p ON p.media_id=m.id
            WHERE m.root=? AND m.available=1 AND p.watched=1 ORDER BY m.id""", (root,)):
        try:
            path = media_path(root, row["path"])
            if path != Path(root).resolve(strict=True) / row["path"]:
                continue
            stat = path.stat()
        except (OSError, ValueError):
            continue
        item = dict(row)
        item["size"] = stat.st_size
        groups["episodes" if item["series"] else "movies"].append(item)
        fingerprints.append((item["id"], item["path"], item["series"], stat.st_dev,
                             stat.st_ino, stat.st_size, stat.st_mtime_ns))
    return groups, hashlib.sha256(repr(fingerprints).encode()).hexdigest()


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


def library_revision(db, root):
    """Track library rows, provider discoveries and controls for live updates."""
    snapshots = []
    for query in (
        "SELECT m.id,m.path,m.title,m.series,m.season,m.episode,m.size,m.available,m.hidden,COALESCE(p.watched,0) FROM media m LEFT JOIN progress p ON p.media_id=m.id WHERE m.root=? ORDER BY m.id",
        "SELECT s.id,s.series,s.mode,s.auto_new,d.season,d.episode,d.title,d.state,d.listed,d.watched,d.error FROM subscriptions s LEFT JOIN discoveries d ON d.subscription_id=s.id WHERE s.root=? AND s.mode!='setup' ORDER BY s.id,d.season,d.episode",
        "SELECT series,season,watched,excluded FROM season_state WHERE root=? ORDER BY series,season",
    ):
        snapshots.append([tuple(row) for row in db.execute(query, (root,))])
    return hashlib.sha256(repr(snapshots).encode()).hexdigest()


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
        # A publication may have committed after enumeration. Include its exact
        # verified file so this snapshot cannot hide it or apply the age buffer.
        indexed = {relative: (relative, size, identity) for relative, size, identity in found}
        for row in db.execute("""SELECT p.*,m.path FROM published_media p JOIN media m ON m.id=p.media_id
            WHERE m.root=?""", (root_value,)).fetchall():
            try:
                path = media_path(root_value, row["path"])
                stat = path.stat()
                verified = (path == Path(root_value).resolve() / row["path"]
                            and stat.st_size == row["size"] and stat.st_mtime_ns == row["mtime_ns"])
            except (OSError, ValueError):
                verified = False
            if verified:
                indexed[row["path"]] = (row["path"], stat.st_size, identify(Path(row["path"])))
            else:
                db.execute("DELETE FROM published_media WHERE media_id=?", (row["media_id"],))
        found = list(indexed.values())
        _index_files(db, root_value, found, full_scan=True)
        db.execute("INSERT OR REPLACE INTO settings VALUES('last_scan',?)", (str(int(time.time())),))
        db.execute("DELETE FROM settings WHERE key='scan_error'")
    return len(found)


def _index_files(db, root_value, found, *, full_scan):
    """Apply the same identity, metadata and history rules to scans and publication."""
    for row in db.execute("SELECT id,path FROM media WHERE id NOT IN (SELECT media_id FROM media_identity)").fetchall():
        db.execute("INSERT INTO media_identity VALUES(?,?)", (row["id"], source_identity(row["path"])))
    if full_scan:
        db.execute("UPDATE media SET available=0")
    found_paths = {relative for relative, _, _ in found} if full_scan else None
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
            candidates = [row for row in candidates if (row["path"] not in found_paths if full_scan
                          else not (Path(root_value) / row["path"]).exists())]
            if len(candidates) == 1:
                db.execute("UPDATE media SET path=? WHERE id=?", (relative, candidates[0]["id"]))
        db.execute("""INSERT INTO media(root,path,title,series,season,episode,size)
            VALUES(?,?,?,?,?,?,?) ON CONFLICT(root,path) DO UPDATE SET
            available=1,size=excluded.size""",
            (root_value, relative, title, series, season, episode, size))
        db.execute("""INSERT OR IGNORE INTO media_identity(media_id,identity)
            SELECT id,? FROM media WHERE root=? AND path=?""", (key, root_value, relative))
    # Preserve legacy season rules and episode history recorded before a file arrived.
    db.execute("""INSERT INTO progress(media_id,watched)
        SELECT m.id,1 FROM media m WHERE m.id NOT IN (SELECT media_id FROM progress) AND
        (EXISTS(SELECT 1 FROM season_state s WHERE s.root=m.root AND s.series=m.series AND s.season=m.season AND s.watched=1)
         OR EXISTS(SELECT 1 FROM discoveries d JOIN subscriptions s ON s.id=d.subscription_id
            WHERE s.root=m.root AND s.series=m.series AND d.season=m.season AND d.episode=m.episode AND d.watched=1))
    """)
    db.execute("""UPDATE discoveries SET state='downloaded',error='' WHERE state IN ('submitted','uncertain','failed')
        AND EXISTS(SELECT 1 FROM subscriptions s JOIN media m ON m.root=s.root AND m.series=s.series
            WHERE s.id=discoveries.subscription_id AND m.season=discoveries.season
            AND m.episode=discoveries.episode AND m.available=1)""")


def import_completed(database, path, library):
    """Commit a verified publication to the catalog before reporting completion."""
    with connect(database) as db:
        db.execute("BEGIN IMMEDIATE")
        root = settings(db)["videos_path"]
        if root != str(library):
            raise ValueError("The configured library changed during publication")
        relative = str(Path(path).relative_to(Path(root).resolve(strict=True)))
        target = media_path(root, relative)
        if target != Path(root).resolve() / relative:
            raise ValueError("Published media must not use symbolic links")
        stat = target.stat()
        if not stat.st_size or target.suffix.lower() not in EXTENSIONS:
            raise ValueError("The published download is not a non-empty supported video")
        _index_files(db, root, [(relative, stat.st_size, identify(Path(relative)))], full_scan=False)
        media = db.execute("SELECT id,hidden FROM media WHERE root=? AND path=?", (root, relative)).fetchone()
        if media["hidden"]:
            raise ValueError("The downloaded title is hidden in the library")
        db.execute("INSERT OR REPLACE INTO published_media VALUES(?,?,?)",
                   (media["id"], stat.st_size, stat.st_mtime_ns))
        return media["id"]


def scan_safely(database):
    try:
        return scan(database)
    except (OSError, ValueError) as error:
        with connect(database) as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES('scan_error',?)", (str(error),))
        raise

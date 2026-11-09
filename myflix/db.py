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
            CREATE TABLE IF NOT EXISTS credits (
                fingerprint TEXT PRIMARY KEY,
                position REAL,
                retry_at REAL NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS media_identity (
                media_id INTEGER PRIMARY KEY REFERENCES media(id),
                identity TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS media_identity_key ON media_identity(identity);
            CREATE INDEX IF NOT EXISTS media_episode ON media(root,series,season,episode);
            CREATE TABLE IF NOT EXISTS artwork (
                key TEXT PRIMARY KEY, retry_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS discovery_feeds (
                name TEXT PRIMARY KEY, payload TEXT NOT NULL DEFAULT '[]',
                updated INTEGER NOT NULL DEFAULT 0, next_check INTEGER NOT NULL DEFAULT 0,
                error INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS season_state (
                root TEXT NOT NULL, series TEXT NOT NULL, season INTEGER NOT NULL,
                watched INTEGER NOT NULL DEFAULT 0,
                excluded INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(root,series,season)
            );
            CREATE TABLE IF NOT EXISTS subscriptions (
                id INTEGER PRIMARY KEY, root TEXT NOT NULL, series TEXT NOT NULL,
                site TEXT NOT NULL, identity TEXT NOT NULL, payload TEXT NOT NULL,
                mode TEXT NOT NULL DEFAULT 'auto',
                initialized INTEGER NOT NULL DEFAULT 0,
                checked_at INTEGER NOT NULL DEFAULT 0,
                next_check INTEGER NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT '',
                UNIQUE(root,site,identity), UNIQUE(root,series)
            );
            CREATE TABLE IF NOT EXISTS discoveries (
                subscription_id INTEGER NOT NULL REFERENCES subscriptions(id),
                season INTEGER NOT NULL, episode INTEGER NOT NULL, title TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'new',
                listed INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY(subscription_id,season,episode)
            );
            CREATE TABLE IF NOT EXISTS download_requests (
                root TEXT NOT NULL, download_id TEXT NOT NULL, body TEXT NOT NULL,
                PRIMARY KEY(root,download_id)
            );
            CREATE TABLE IF NOT EXISTS download_request_state (
                root TEXT NOT NULL, download_id TEXT NOT NULL,
                engine_instance TEXT NOT NULL DEFAULT '',
                submitted_at REAL NOT NULL DEFAULT 0,
                state TEXT NOT NULL DEFAULT 'submitted',
                PRIMARY KEY(root,download_id),
                FOREIGN KEY(root,download_id) REFERENCES download_requests(root,download_id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS download_retries (
                id INTEGER PRIMARY KEY, root TEXT NOT NULL, source_key TEXT NOT NULL,
                source TEXT NOT NULL, body TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending', download_id TEXT NOT NULL DEFAULT '',
                submitted_at REAL NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '',
                failure TEXT NOT NULL DEFAULT '', UNIQUE(root,source_key)
            );
            CREATE INDEX IF NOT EXISTS download_retries_pending ON download_retries(root,state,id);
        """)
        db.execute("BEGIN IMMEDIATE")
        if any(row["name"] == "media_id" for row in db.execute("PRAGMA table_info(credits)")):
            # Credits belong to file bytes, including completed files that the
            # catalog has not scanned yet. Retain derived results on upgrade.
            db.execute("ALTER TABLE credits RENAME TO credits_legacy")
            db.execute("CREATE TABLE credits(fingerprint TEXT PRIMARY KEY,position REAL,retry_at REAL NOT NULL DEFAULT 0)")
            db.execute("INSERT OR REPLACE INTO credits SELECT fingerprint,position,retry_at FROM credits_legacy")
            db.execute("DROP TABLE credits_legacy")
        columns = {row["name"] for row in db.execute("PRAGMA table_info(discoveries)")}
        for name, definition in (("error", "TEXT NOT NULL DEFAULT ''"),
                                 ("download_id", "TEXT NOT NULL DEFAULT ''"),
                                 ("submitted_at", "REAL NOT NULL DEFAULT 0")):
            if name not in columns:
                db.execute(f"ALTER TABLE discoveries ADD COLUMN {name} {definition}")
        # Retain the time of legacy acknowledged episode/retry submissions. A
        # pre-boot timestamp can prove interruption even before instance tracking.
        # Keep the original three-column request format usable by older releases
        # so rolling back application code cannot break download submissions.
        db.execute("""INSERT OR IGNORE INTO download_request_state(root,download_id,submitted_at)
            SELECT download_requests.root,download_requests.download_id,COALESCE(
            (SELECT MAX(d.submitted_at) FROM discoveries d JOIN subscriptions s ON s.id=d.subscription_id
             WHERE s.root=download_requests.root AND d.download_id=download_requests.download_id AND d.submitted_at>0),
            (SELECT MAX(r.submitted_at) FROM download_retries r
             WHERE r.root=download_requests.root AND r.download_id=download_requests.download_id AND r.submitted_at>0),0)
            FROM download_requests""")
        columns = {row["name"] for row in db.execute("PRAGMA table_info(subscriptions)")}
        for name, definition in (("lookup_state", "TEXT NOT NULL DEFAULT ''"),
                                 ("lookup_token", "TEXT NOT NULL DEFAULT ''"),
                                 ("lookup_seasons_done", "INTEGER NOT NULL DEFAULT 0"),
                                 ("lookup_seasons_total", "INTEGER NOT NULL DEFAULT 0"),
                                 ("lookup_season", "INTEGER NOT NULL DEFAULT 0"),
                                 ("lookup_episodes", "INTEGER NOT NULL DEFAULT 0")):
            if name not in columns:
                db.execute(f"ALTER TABLE subscriptions ADD COLUMN {name} {definition}")
        db.execute("INSERT OR IGNORE INTO settings VALUES('download_limit','4')")
        db.execute("INSERT OR IGNORE INTO settings VALUES('processing_limit','1')")
        if db.execute("PRAGMA user_version").fetchone()[0] < 3:
            # Follow now includes automatic catch-up. Upgrade existing follows,
            # retaining pauses, dismissals and all uncertain download reservations.
            db.execute("UPDATE subscriptions SET mode='auto',next_check=0 WHERE mode='notify'")
            db.execute("UPDATE discoveries SET state='new' WHERE state='baseline'")
            db.execute("PRAGMA user_version=3")


def settings(db):
    return dict(db.execute("SELECT key,value FROM settings").fetchall())

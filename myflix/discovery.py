"""Bounded, credential-free discovery feeds, refreshed off request paths."""
import hashlib
import json
import logging
import re
import time
from datetime import datetime, timezone
from urllib.request import Request, build_opener, HTTPRedirectHandler

from .db import connect
from .artwork import ArtworkError
from .streamingcommunity import FEEDS as SC_FEEDS


FEEDS = {
    "popular_tv": ("discover/trending/tv/week_100.json", 86400, "tv"),
    "popular_movie": ("discover/trending/movies/week_100.json", 86400, "movie"),
    "upcoming_tv": ("calendar/v2/tv.json", 21600, "tv"),
}
ALL_FEEDS = {**FEEDS, **{name: (name, 86400, "") for name in SC_FEEDS}}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def date(value):
    if not isinstance(value, str):
        return None
    for pattern in ("%m/%d/%Y", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, pattern).replace(tzinfo=timezone.utc).date()
        except ValueError:
            pass
    return None


def image(path, kind):
    if not isinstance(path, str) or not re.fullmatch(r"[A-Za-z0-9]{1,80}(?:/[A-Za-z0-9]{1,80})?", path):
        return None
    # JPEG remains usable on older TV browsers. No provider-controlled host or query.
    suffix = "_medium.jpg" if kind == "fanart" else "_m.jpg"
    return f"https://simkl.in/{kind}/{path}{suffix}"


def normalize(raw, kind):
    if not isinstance(raw, dict):
        return None
    ids = raw.get("ids")
    identity = ids.get("simkl_id") if isinstance(ids, dict) else None
    title = raw.get("title")
    if type(identity) is not int or identity <= 0 or not isinstance(title, str) or not title.strip():
        return None
    trailer = raw.get("trailer")
    return dict(id=identity, kind=kind, title=title.strip()[:200], series="", discovery=True,
                poster=image(raw.get("poster"), "posters"), backdrop=image(raw.get("fanart"), "fanart"),
                trailer=trailer if isinstance(trailer, str) and re.fullmatch(r"[A-Za-z0-9_-]{11}", trailer) else "",
                overview=str(raw.get("overview") or "")[:2000], release="")


def parse_feed(name, data):
    kind = FEEDS[name][2]
    upcoming = name.startswith("upcoming")
    if upcoming:
        if not isinstance(data, dict) or not isinstance(data.get("calendar"), list) or not isinstance(data.get("metadata"), dict):
            raise ValueError("Invalid discovery calendar")
        candidates = []
        for entry in data["calendar"]:
            if not isinstance(entry, dict):
                continue
            raw = data["metadata"].get(str(entry.get("simkl_id")))
            if not isinstance(raw, dict):
                continue
            release = date(entry.get("date"))
            if not release:
                continue
            if release < datetime.now(timezone.utc).date():
                continue
            episode = entry.get("episode")
            if not isinstance(episode, dict) or episode.get("episode") != 1:
                continue
            candidates.append((release.isoformat(), raw))
        candidates.sort(key=lambda candidate: candidate[0])
    else:
        if not isinstance(data, list):
            raise ValueError("Invalid discovery rankings")
        # The feed is ordered by weekly watchers. Its `rank` field is a rating rank.
        candidates = [("", raw) for raw in data]
    items, seen = [], set()
    for release, raw in candidates:
        item = normalize(raw, kind)
        if not item or item["id"] in seen:
            continue
        if not upcoming and kind == "movie":
            home_release = date(raw.get("dvd_date"))
            if not home_release or home_release > datetime.now(timezone.utc).date():
                continue
        item["release"] = release
        items.append(item)
        seen.add(item["id"])
        if len(items) == 100:
            break
    return items


class DiscoveryCache:
    def __init__(self, database, engine, artwork):
        self.database = database
        self.engine = engine
        self.artwork = artwork

    def read(self):
        with connect(self.database) as db:
            rows = db.execute("SELECT * FROM discovery_feeds").fetchall()
        return {row["name"]: dict(items=json.loads(row["payload"]), updated=row["updated"], error=row["error"])
                for row in rows if row["name"] in ALL_FEEDS}

    def provider_items(self, name, stop):
        items = self.engine.call("discovery", {"feed": name})["items"]
        if not isinstance(items, list) or len(items) > SC_FEEDS[name][2]:
            raise ValueError("Invalid provider discovery feed")
        for item in items:
            if stop.is_set():
                return None
            for kind in ("poster", "backdrop"):
                url = item.get(kind)
                item[kind] = None
                if not isinstance(url, str) or not url:
                    continue
                key = hashlib.sha256(url.encode()).hexdigest()
                try:
                    if not self.artwork.cached(key):
                        self.artwork.download(key, url)
                    item[kind] = f"/discovery-artwork/{key}.jpg"
                except ArtworkError:
                    logging.warning("Discovery artwork unavailable")
        return items

    def fetch(self, path):
        request = Request("https://data.simkl.in/" + path, headers={"User-Agent": "MyFlix/0.1", "Accept": "application/json"})
        # Fixed public endpoint; redirects cannot turn it into a LAN request.
        with build_opener(NoRedirect()).open(request, timeout=15) as response:
            data = response.read(8 * 1024 * 1024 + 1)
        if len(data) > 8 * 1024 * 1024:
            raise ValueError("Discovery feed too large")
        return json.loads(data)

    def update(self, stop):
        for name, (path, interval, _) in ALL_FEEDS.items():
            if stop.is_set():
                return
            now = int(time.time())
            with connect(self.database) as db:
                row = db.execute("SELECT next_check FROM discovery_feeds WHERE name=?", (name,)).fetchone()
            if row and row["next_check"] > now:
                continue
            try:
                items = self.provider_items(name, stop) if name in SC_FEEDS else parse_feed(name, self.fetch(path))
                if items is None:
                    return
                with connect(self.database) as db:
                    db.execute("""INSERT OR REPLACE INTO discovery_feeds
                        (name,payload,updated,next_check,error) VALUES(?,?,?,?,0)""",
                               (name, json.dumps(items), now, now + interval))
            except Exception:
                logging.warning("Discovery feed %s unavailable; preserving cached results", name)
                with connect(self.database) as db:
                    db.execute("INSERT OR IGNORE INTO discovery_feeds(name) VALUES(?)", (name,))
                    db.execute("UPDATE discovery_feeds SET error=1,next_check=? WHERE name=?", (now + 3600, name))

    def run(self, stop):
        while not stop.is_set():
            try:
                self.update(stop)
            except Exception:
                logging.exception("Discovery worker failed")
            stop.wait(60)

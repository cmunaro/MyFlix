"""Local-first artwork with bounded, background-only provider artwork from VibraVid."""
import hashlib
import http.client
import io
import ipaddress
import json
import logging
import os
import re
import socket
import ssl
import tempfile
import time
import unicodedata
from pathlib import Path
from urllib.parse import urlsplit

from PIL import Image, UnidentifiedImageError

from .catalog import media_path
from .db import connect, settings
from .vibravid import VibraVidError


def local_artwork(item, kind):
    video = Path(item["path"])
    names = ([video.with_name(video.stem + "-fanart.jpg"), video.parent / "fanart.jpg"]
             if kind == "backdrop" else
             [video.with_name(video.stem + "-poster.jpg"), video.with_name(video.stem + "-thumb.jpg"), video.parent / "poster.jpg"])
    if item["series"]:
        names.append(video.parent.parent / ("fanart.jpg" if kind == "backdrop" else "poster.jpg"))
    for name in names:
        try:
            return media_path(item["root"], name)
        except (OSError, ValueError):
            continue
    return None


def normalize(title):
    return "".join(c for c in unicodedata.normalize("NFKD", title.casefold()) if c.isalnum())


def identity(item):
    title = item["series"] or item["title"]
    title = re.sub(r"[._]+", " ", title)
    title = re.split(r"\b(?:720p|1080p|2160p|BluRay|WEB[ -]?(?:DL|Rip)|HDTV|x26[45]|HEVC)\b", title, flags=re.I)[0]
    year = re.search(r"\s[([]?((?:19|20)\d{2})\b", title)
    title, year = (title[:year.start()], year[1]) if year else (title, "")
    title = title.strip(" -[]()")
    media_type = "tv" if item["series"] else "movie"
    key = hashlib.sha256(json.dumps(["provider", media_type, normalize(title), year]).encode()).hexdigest()
    return key, media_type, title, year


class ArtworkError(Exception):
    pass


class ArtworkCache:
    def __init__(self, database, engine):
        self.database = database
        self.engine = engine
        self.directory = Path(database).resolve().parent / "artwork"

    def cached(self, key):
        try:
            return media_path(self.directory, f"{key}-poster.jpg")
        except (OSError, ValueError):
            return None

    def path(self, item, kind):
        local = local_artwork(item, kind)
        return local or (self.cached(identity(item)[0]) if kind == "poster" else None)

    def read(self, url, limit):
        # Provider URLs may use any public CDN, but must never reach LAN services.
        connection = None
        try:
            parts = urlsplit(url)
            port = 443 if parts.scheme == "https" else 80
            if (parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password
                    or parts.port not in (None, port) or parts.fragment):
                raise ValueError
            addresses = socket.getaddrinfo(parts.hostname, port, type=socket.SOCK_STREAM)
            if not addresses or any(not ipaddress.ip_address(address[4][0]).is_global for address in addresses):
                raise ValueError
            cls = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
            connection = cls(parts.hostname, port, timeout=5)
            # Connect to the validated IP; TLS still verifies the original CDN hostname.
            connection.sock = socket.create_connection((addresses[0][4][0], port), timeout=5)
            if parts.scheme == "https":
                connection.sock = ssl.create_default_context().wrap_socket(connection.sock, server_hostname=parts.hostname)
            connection.request("GET", parts.path + ("?" + parts.query if parts.query else ""),
                               headers={"User-Agent": "MyFlix/0.1", "Accept": "image/*"})
            response = connection.getresponse()
            # No redirect can bypass the public-address check.
            if response.status != 200:
                raise ValueError
            data = response.read(limit + 1)
            if len(data) > limit:
                raise ValueError
            return data
        except (OSError, ValueError, http.client.HTTPException):
            raise ArtworkError("Provider artwork is unavailable. Covers will retry later.") from None
        finally:
            if connection is not None:
                connection.close()

    def match(self, entries, media_type, title, year, history=False):
        matches = {}
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            payload = entry.get("payload") or {}
            if not isinstance(payload, dict):
                continue
            if history:
                kind = str(entry.get("type", "")).casefold()
                if kind not in ("film", "movie", "tv", "serie", "series"):
                    continue
                is_movie = kind in ("film", "movie")
            else:
                is_movie = entry.get("is_movie")
                if not isinstance(is_movie, bool):
                    continue
            name = entry.get("series_name") or entry.get("title") or payload.get("name")
            if not isinstance(name, str):
                continue
            if not is_movie:
                name = re.sub(r"\s+-\s+S\d+.*$", "", name, flags=re.I)
            _, candidate_type, candidate_title, candidate_year = identity({"title": name, "series": "" if is_movie else name})
            candidate_year = str(entry.get("year") or payload.get("year") or candidate_year)
            poster = payload.get("poster") or entry.get("poster")
            if (candidate_type == media_type and normalize(candidate_title) == normalize(title)
                    and (not year or candidate_year == year) and isinstance(poster, str) and poster):
                matches.setdefault(candidate_year, poster)
        return next(iter(matches.values())) if len(matches) == 1 else None

    def lookup(self, media_type, title, year, history):
        poster = self.match(history, media_type, title, year, history=True)
        if poster:
            return poster
        # Older downloads may have fallen out of engine history. Reuse its
        # provider search, including the provider's original localized title.
        results = self.engine.call("search", {"query": title, "site": "__all__"})
        return self.match(results.get("results", []), media_type, title, year)

    def download(self, key, image_url):
        data = self.read(image_url, 5 * 1024 * 1024)
        self.directory.mkdir(mode=0o700, exist_ok=True)
        temporary = None
        try:
            with Image.open(io.BytesIO(data), formats=["JPEG", "PNG", "WEBP"]) as image:
                if image.width * image.height > 8_000_000:
                    raise ArtworkError("Provider cover is too large.")
                image.thumbnail((1000, 1000))
                with tempfile.NamedTemporaryFile(dir=self.directory, prefix=".art-", delete=False) as output:
                    temporary = Path(output.name)
                    image.convert("RGB").save(output, "JPEG", quality=85)
                    output.flush()
                    os.fsync(output.fileno())
            temporary.replace(self.directory / f"{key}-poster.jpg")
        except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError):
            raise ArtworkError("Provider returned an invalid cover. Covers will retry later.") from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def update(self, stop):
        if stop.is_set():
            return
        now = int(time.time())
        with connect(self.database) as db:
            config = settings(db)
            if int(config.get("artwork_retry_at", "0")) > now:
                return
            rows = db.execute("""SELECT * FROM media WHERE hidden=0 AND available=1 AND root=?
                ORDER BY id""", (config.get("videos_path", ""),)).fetchall()
            retries = dict(db.execute("SELECT key,retry_at FROM artwork").fetchall())
        groups = {}
        for row in rows:
            key, media_type, title, year = identity(row)
            if not title or retries.get(key, 0) > now:
                continue
            if not self.path(row, "poster"):
                groups.setdefault(key, (media_type, title, year))
        if not groups:
            return
        try:
            queue = self.engine.call("queue")
            history = [item for group in ("active", "scheduled", "history") for item in queue.get(group, [])]
        except VibraVidError:
            return
        for key, (media_type, title, year) in list(groups.items())[:10]:
            if stop.is_set():
                return
            try:
                poster = self.lookup(media_type, title, year, history)
                if stop.is_set():
                    return
                if poster:
                    self.download(key, poster)
                with connect(self.database) as db:
                    db.execute("INSERT OR REPLACE INTO artwork VALUES(?,?)", (key, now + 7 * 86400))
                    db.execute("DELETE FROM settings WHERE key IN ('artwork_error','artwork_retry_at')")
            except (ArtworkError, OSError, VibraVidError) as error:
                message = str(error) if isinstance(error, ArtworkError) else "Provider covers are unavailable. Check the engine and data-folder permissions."
                with connect(self.database) as db:
                    db.execute("INSERT OR REPLACE INTO settings VALUES('artwork_error',?)", (message,))
                    db.execute("INSERT OR REPLACE INTO settings VALUES('artwork_retry_at',?)", (str(now + 3600),))
                logging.warning("%s", message)
                return

    def run(self, stop):
        while not stop.is_set():
            try:
                self.update(stop)
            except Exception:
                logging.error("Automatic artwork worker failed; will retry in 60 seconds")
            stop.wait(60)

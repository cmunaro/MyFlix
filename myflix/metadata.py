"""Provider-first title details and local runtimes, fetched only in the background."""
import json
import logging
import math
import os
import shutil
import time
from pathlib import Path

from .artwork import identity, normalize
from .catalog import identify, media_path
from .credits import Cancelled, command
from .db import connect, settings
from .vibravid import VibraVidError


def text(value, limit=5000):
    return value.strip()[:limit] if isinstance(value, str) else ""


def description(data):
    # Entries.desc is also used for availability/pricing; never treat it as plot.
    return next((text(data.get(key)) for key in ("overview", "synopsis", "plot", "description")
                 if text(data.get(key))), "")


def names(value):
    if not isinstance(value, list):
        return []
    result = []
    for item in value[:30]:
        name = text(item.get("name") if isinstance(item, dict) else item, 200)
        if name and name not in result:
            result.append(name)
    return result


def title_details(data):
    if not isinstance(data, dict):
        data = {}
    year = str(data.get("year") or "")
    return {"synopsis": description(data), "genres": names(data.get("genres")),
            "cast": names(data.get("cast") or data.get("actors")),
            "year": year if year.isdigit() and len(year) == 4 else ""}


def source_item(item):
    # Use the original file identity for matching. Catalog edits remain authoritative.
    title, series, season, episode = identify(Path(item["path"]))
    if item.get("source_identity"):
        original = json.loads(item["source_identity"])
        if len(original) == 3:
            series, season, episode = original
        else:
            title, series, season, episode = original[0], "", None, None
    return dict(item, title=title, series=series, season=season, episode=episode)


def match_source(body, item):
    payload = body.get("payload")
    if not isinstance(payload, dict) or not isinstance(body.get("site"), str) or not body["site"]:
        return False
    name = payload.get("name")
    kind = str(payload.get("type") or "").lower()
    if (not isinstance(name, str) or kind not in ("", "film", "movie", "ova", "tv", "serie", "series", "anime")
            or (not kind and not isinstance(payload.get("is_movie"), bool))):
        return False
    movie = payload["is_movie"] if isinstance(payload.get("is_movie"), bool) else kind in ("film", "movie", "ova")
    _, media_type, title, year = identity(item)
    _, candidate_type, candidate, candidate_year = identity({"title": name, "series": "" if movie else name})
    candidate_year = str(payload.get("year") or candidate_year)
    return (media_type == candidate_type and normalize(title) == normalize(candidate)
            and (not year or year == candidate_year))


class MetadataCache:
    def __init__(self, database, engine):
        self.database = database
        self.engine = engine

    def decorate(self, items):
        """Read saved details only; HTTP handlers never call providers or native tools."""
        if not items:
            return items
        ids = [item["id"] for item in items]
        with connect(self.database) as db:
            saved = {row["media_id"]: row for row in db.execute(f"""SELECT mm.*,i.identity source_identity
                FROM media_metadata mm LEFT JOIN media_identity i ON i.media_id=mm.media_id
                WHERE mm.media_id IN ({','.join('?' for _ in ids)})""", ids)}
            keys = list({row["title_key"] for row in saved.values()})
            titles = {row["key"]: json.loads(row["details"]) for row in db.execute(
                f"SELECT key,details FROM title_metadata WHERE key IN ({','.join('?' for _ in keys)})", keys)} if keys else {}
        for item in items:
            row = saved.get(item["id"])
            details = titles.get(row["title_key"], {}) if row else {}
            season = details.get("seasons", {}).get(str(item["season"]), {})
            episode = season.get("episodes", {}).get(str(item["episode"]), {})
            item["metadata"] = details
            item["season_description"] = season.get("synopsis", "")
            item["episode_description"] = episode.get("synopsis", "")
            item["runtime_minutes"] = max(1, round(row["runtime"] / 60)) if row and row["runtime"] else None
            item["display_title"] = item["title"]
            # Supplement inferred titles without ever overwriting an admin edit.
            original = source_item(dict(item, source_identity=row["source_identity"] if row else None))
            if (item["series"] and item["title"] == original["title"]
                    and item["season"] == original["season"] and item["episode"] == original["episode"]):
                item["display_title"] = episode.get("title") or item["title"]
        return items

    def source(self, db, item, cached):
        root = item["root"]
        candidates = []
        # A follow explicitly maps a provider title to a household series name.
        for row in db.execute("SELECT site,payload FROM subscriptions WHERE root=? AND series=?", (root, item["series"])):
            candidates.append({"site": row["site"], "payload": json.loads(row["payload"])})
        for row in db.execute("SELECT body FROM download_requests WHERE root=?", (root,)):
            body = json.loads(row["body"])
            if match_source(body, source_item(item)):
                candidates.append({"site": body["site"], "payload": body["payload"]})
        if candidates:
            return candidates[0]
        return json.loads(cached["source"]) if cached else {}

    def update_title(self, items, stop):
        item = items[0]
        key, _, title, _ = identity(source_item(item))
        now = time.time()
        with connect(self.database) as db:
            cached = db.execute("SELECT * FROM title_metadata WHERE key=?", (key,)).fetchone()
            details = json.loads(cached["details"]) if cached else {}
            source = self.source(db, item, cached)
        seasons = sorted({row["season"] for row in items if row["series"] and row["season"] is not None})
        # New seasons bypass the normal refresh interval.
        missing = any(number not in details.get("checked_seasons", []) for number in seasons)
        source_changed = cached and source != json.loads(cached["source"])
        if cached and cached["retry_at"] > now and not missing and not source_changed:
            return key
        details["checked_seasons"] = seasons
        try:
            if not source:
                results = self.engine.call("search", {"query": title, "site": "__all__"}).get("results", [])
                matches = [{"site": row.get("site"), "payload": row.get("payload")}
                           for row in results if isinstance(row, dict) and match_source(row, source_item(item))]
                # Cross-provider or remake ambiguity must not attach arbitrary metadata.
                if len(matches) == 1:
                    source = matches[0]
            if stop.is_set():
                raise Cancelled()
            if source:
                payload = source["payload"]
                raw = payload.get("raw_data")
                for field, value in title_details(dict(raw if isinstance(raw, dict) else {}, year=payload.get("year"))).items():
                    if value:
                        details[field] = value
                result = self.engine.call("metadata", dict(source, seasons=seasons))
                fresh = result.get("details")
                if not isinstance(fresh, dict):
                    raise VibraVidError("Invalid title metadata response")
                # Keep previously fetched nonempty fields if a provider becomes sparse.
                for field in ("synopsis", "genres", "cast", "year"):
                    if fresh.get(field):
                        details[field] = fresh[field]
                for number, season in fresh.get("seasons", {}).items():
                    old = details.setdefault("seasons", {}).setdefault(number, {})
                    for field in ("title", "synopsis"):
                        if season.get(field):
                            old[field] = season[field]
                    for episode, values in season.get("episodes", {}).items():
                        saved = old.setdefault("episodes", {}).setdefault(episode, {})
                        saved.update({field: value for field, value in values.items() if value})
            retry_at = now + 7 * 86400
        except VibraVidError:
            retry_at = now + 3600
        if stop.is_set():
            raise Cancelled()
        with connect(self.database) as db:
            db.execute("INSERT OR REPLACE INTO title_metadata VALUES(?,?,?,?)",
                       (key, json.dumps(details), json.dumps(source), retry_at))
        return key

    def probe(self, path, stop):
        binary = shutil.which("ffprobe")
        bundled = Path(self.database).resolve().parent / "vibravid" / "binary" / "tools" / "ffprobe"
        if not binary and os.access(bundled, os.X_OK):
            binary = str(bundled)
        if not binary:
            raise RuntimeError("ffprobe is unavailable")
        result = json.loads(command([binary, "-v", "error", "-show_entries", "format=duration",
                                     "-of", "json", path], stop, timeout=15))
        duration = float(result["format"]["duration"])
        if not math.isfinite(duration) or duration <= 0 or duration > 7 * 86400:
            raise ValueError("Invalid video runtime")
        return duration

    def update_runtime(self, item, key, stop):
        try:
            path = media_path(item["root"], item["path"])
            stat = path.stat()
            signature = f"{item['path']}:{stat.st_size}:{stat.st_mtime_ns}"
        except (OSError, ValueError):
            return
        with connect(self.database) as db:
            old = db.execute("SELECT * FROM media_metadata WHERE media_id=?", (item["id"],)).fetchone()
        if old and old["signature"] == signature and (old["runtime"] or old["retry_at"] > time.time()):
            return
        try:
            runtime, retry_at = self.probe(path, stop), 0
        except Cancelled:
            raise
        except (OSError, ValueError, TypeError, RuntimeError, KeyError, TimeoutError):
            runtime, retry_at = None, time.time() + 3600
        if stop.is_set():
            raise Cancelled()
        try:
            current = media_path(item["root"], item["path"]).stat()
            if (current.st_size, current.st_mtime_ns) != (stat.st_size, stat.st_mtime_ns):
                return
        except (OSError, ValueError):
            return
        with connect(self.database) as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT root,path,available,hidden FROM media WHERE id=?", (item["id"],)).fetchone()
            if (settings(db).get("videos_path") != item["root"] or not current
                    or tuple(current) != (item["root"], item["path"], 1, 0)):
                return
            db.execute("INSERT OR REPLACE INTO media_metadata VALUES(?,?,?,?,?)",
                       (item["id"], key, signature, runtime, retry_at))

    def update(self, stop):
        with connect(self.database) as db:
            rows = [dict(row) for row in db.execute("""SELECT m.*,i.identity source_identity FROM media m
                LEFT JOIN media_identity i ON i.media_id=m.id
                WHERE root=? AND available=1 AND hidden=0 ORDER BY m.id""", (settings(db).get("videos_path", ""),))]
        groups = {}
        for item in rows:
            groups.setdefault(identity(source_item(item))[0], []).append(item)
        for items in groups.values():
            if stop.is_set():
                raise Cancelled()
            key = self.update_title(items, stop)
            for item in items:
                self.update_runtime(item, key, stop)

    def run(self, stop):
        while not stop.is_set():
            try:
                self.update(stop)
            except Cancelled:
                return
            except Exception:
                logging.exception("Title metadata worker failed; retrying in 60 seconds")
            stop.wait(60)

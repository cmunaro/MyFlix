"""Durable retries of confirmed failures and original provider request identities."""
import hashlib
import json
import re
import time
from pathlib import Path

from .db import connect, settings
from .vibravid import VibraVidError


def failure_key(item):
    fields = ("id", "site", "title", "season", "episode", "start_time", "end_time")
    return hashlib.sha256(json.dumps([item.get(k) for k in fields], sort_keys=True).encode()).hexdigest()


def target_key(item):
    title = item.get("series_name") or re.sub(r" - S\d+(?: E.*)?$", "", item.get("title") or "")
    return item.get("site"), title, str(item.get("season") or 0), str(item.get("episode") or 0)


def _submitted_requests(db, root):
    return db.execute("""SELECT r.*,s.engine_instance,s.submitted_at,s.state
        FROM download_requests r JOIN download_request_state s USING(root,download_id)
        WHERE r.root=? AND s.state='submitted'""", (root,)).fetchall()


def _movie_title_matches(title, payload):
    name = payload["name"]
    if title.casefold() == name.casefold():
        return True
    year = str(payload.get("year", ""))
    if not re.fullmatch(r"\d{4}", year):
        return False
    # The pinned engine's movie format includes year and a quality suffix.
    return bool(re.fullmatch(re.escape(f"{name} ({year})") + r"(?: \[[^\[\]]+\])?", title, re.IGNORECASE))


class DownloadRetries:
    def __init__(self, database):
        self.database = database

    def submit(self, engine, body, *, priority=False, root=None):
        if root is None:
            with connect(self.database) as db:
                root = settings(db)["videos_path"]
        instance = getattr(engine, "instance_id", "")
        submitted_at = time.time()
        result = engine.call("retry_download" if priority else "download", body)
        if result.get("queued") is not True:
            raise VibraVidError("Download was not acknowledged. Check the queue before retrying.")
        if isinstance(result.get("download_id"), str) and result["download_id"]:
            with connect(self.database) as db:
                db.execute("INSERT OR REPLACE INTO download_requests VALUES(?,?,?)",
                           (root, result["download_id"], json.dumps(body)))
                db.execute("""INSERT INTO download_request_state(root,download_id,engine_instance,submitted_at)
                    VALUES(?,?,?,?)""", (root, result["download_id"], instance, submitted_at))
        return result

    def recover_interrupted(self, engine, queue):
        """Queue acknowledged work owned by a departed managed engine, once.

        Return None while the library is unavailable. Unacknowledged mutations
        and requests still owned by this engine always require queue review.
        """
        from .catalog import library_files
        from .monitoring import queued_episode

        if not all(isinstance(queue.get(key), list) and all(isinstance(job, dict) for job in queue[key])
                   for key in ("active", "scheduled", "history")):
            raise VibraVidError("Could not verify the queue before recovering interrupted downloads.")
        instance = getattr(engine, "instance_id", "")
        if not instance:
            return 0
        try:
            boot_time = time.time() - time.clock_gettime(time.CLOCK_BOOTTIME)
        except (AttributeError, OSError):
            boot_time = 0
        with connect(self.database) as db:
            config = settings(db)
            root = config["videos_path"]
            if config.get("scan_error") or not config.get("last_scan") or not Path(root).is_dir():
                return None
        # Publication is atomic. Check even files younger than the catalog's
        # scan buffer, so a reboot immediately after publication cannot resend.
        files = [identity for _, _, identity in library_files(root, minimum_age=0)]
        films = {title.casefold() for title, series, _, _ in files if not series}
        episodes = {(series.casefold(), season, episode) for _, series, season, episode in files if series}
        queued = 0
        with connect(self.database) as db:
            db.execute("BEGIN IMMEDIATE")
            config = settings(db)
            if config["videos_path"] != root or config.get("scan_error"):
                return None
            for request in _submitted_requests(db, root):
                old_instance = request["engine_instance"]
                if old_instance == instance or (not old_instance and not 0 < request["submitted_at"] < boot_time):
                    continue
                if not request["download_id"]:
                    continue
                body = json.loads(request["body"])
                payload, site = body.get("payload"), body.get("site")
                if not isinstance(payload, dict) or not isinstance(payload.get("name"), str) or not payload["name"] or not site:
                    continue
                name = payload["name"]
                movie = bool(payload.get("is_movie"))
                season = episode = None
                if not movie:
                    # An interrupted range may include completed episodes. Only
                    # exact episodes have enough identity for a safe retry.
                    if not str(body.get("season", "")).isdigit() or not str(body.get("episodes", "")).isdigit():
                        continue
                    season, episode = int(body["season"]), int(body["episodes"])
                    if not 0 <= season <= 999 or not 1 <= episode <= 9999:
                        continue
                source = {"id": request["download_id"], "site": site, "title": name,
                          "season": season, "episode": episode, "start_time": request["submitted_at"],
                          "status": "interrupted", "error": "Download interrupted by engine restart."}
                subscription = next((s for s in db.execute("SELECT * FROM subscriptions WHERE root=? AND site=?", (root, site))
                                     if self.matches(source, s)), None) if not movie else None
                discovery = db.execute("SELECT * FROM discoveries WHERE subscription_id=? AND season=? AND episode=?",
                                       (subscription["id"], season, episode)).fetchone() if subscription else None
                if discovery and (discovery["state"] not in ("new", "submitted") or not discovery["listed"]
                                  or (discovery["download_id"] and discovery["download_id"] != request["download_id"])):
                    continue
                if subscription and subscription["mode"] != "auto":
                    continue
                if any(r["state"] != "submitted" for r in db.execute(
                        "SELECT state FROM download_retries WHERE root=? AND download_id=?", (root, request["download_id"]))):
                    continue
                if any(j.get("id") == request["download_id"] and j.get("status") in ("completed", "cancelled", "failed")
                       for j in queue["history"]):
                    continue
                if any(j.get("id") == request["download_id"] or j.get("parent_id") == request["download_id"]
                       or queued_episode(j, site, name, season, episode) for j in queue["active"] + queue["scheduled"]):
                    continue
                status = self.status(db, root, body, subscription)
                series_names = {name.casefold()}
                if subscription:
                    series_names.add(subscription["series"].casefold())
                on_disk = any(_movie_title_matches(title, payload) for title in films) if movie else any(
                    (series, season, episode) in episodes for series in series_names)
                if on_disk or any(status.values()):
                    db.execute("UPDATE download_request_state SET state=? WHERE root=? AND download_id=?",
                               ("completed" if on_disk or status["available"] else "skipped", root, request["download_id"]))
                    continue
                inserted = db.execute("""INSERT OR IGNORE INTO download_retries(root,source_key,source,body)
                    VALUES(?,?,?,?)""", (root, failure_key(source), json.dumps(source), json.dumps(body)))
                queued += inserted.rowcount
                db.execute("UPDATE download_request_state SET state='interrupted' WHERE root=? AND download_id=?", (root, request["download_id"]))
                db.execute("UPDATE download_retries SET state='interrupted' WHERE root=? AND download_id=? AND state='submitted'",
                           (root, request["download_id"]))
                if discovery and inserted.rowcount:
                    db.execute("""UPDATE discoveries SET state='retry_pending',error='' WHERE subscription_id=? AND season=? AND episode=?""",
                               (subscription["id"], season, episode))
        return queued

    @staticmethod
    def _available_failure(db, root, item):
        """Resolve only an exact film or episode backed by a confined library file."""
        from .catalog import media_path

        request = db.execute("SELECT body FROM download_requests WHERE root=? AND download_id=?",
                             (root, item.get("id", ""))).fetchone()
        body = json.loads(request["body"]) if request else {}
        payload = body.get("payload", {})
        if payload.get("is_movie"):
            rows = db.execute("SELECT path,title FROM media WHERE root=? AND series='' AND available=1", (root,)).fetchall()
            rows = [row for row in rows if _movie_title_matches(row["title"], payload)]
        else:
            site, name, _, _ = target_key(item)
            match = re.search(r" - S(\d+) E(\d+)$", item.get("title") or "")
            try:
                season = int(item.get("season") if item.get("season") is not None else match[1] if match else body.get("season", ""))
                episode = int(item.get("episode") if item.get("episode") is not None else match[2] if match else body.get("episodes", ""))
            except (ValueError, TypeError):
                return False  # A range or a series-wide failure is not an exact episode.
            names = {name, payload.get("name", name)}
            for subscription in db.execute("SELECT * FROM subscriptions WHERE root=? AND site=?", (root, site)):
                if DownloadRetries.matches(item, subscription):
                    names.add(subscription["series"])
            rows = db.execute("SELECT path,series FROM media WHERE root=? AND season=? AND episode=? AND available=1",
                              (root, season, episode)).fetchall()
            rows = [row for row in rows if row["series"] in names]
        for row in rows:
            try:
                if media_path(root, row["path"]).stat().st_size:
                    return True
            except (OSError, ValueError):
                continue
        return False

    def present(self, queue):
        """Remove retried attempts from failures, while retaining new retry failures."""
        with connect(self.database) as db:
            root = settings(db)["videos_path"]
            retries = db.execute("SELECT * FROM download_retries WHERE root=? ORDER BY id", (root,)).fetchall()
            hidden = {row["source_key"] for row in retries}
            completed = {row["download_id"] for row in db.execute(
                "SELECT download_id FROM download_request_state WHERE root=? AND state='completed'", (root,))}
            retired = {}
            for row in retries:
                if row["state"] == "cancelled":
                    continue
                source = json.loads(row["source"])
                key = target_key(source)
                retired[key] = max(retired.get(key, 0), source.get("start_time") or 0)
            failures = list(queue.get("history", []))
            failures += [json.loads(row["failure"]) for row in retries if row["failure"]]
            # A followed failure stays visible after the engine's bounded history rolls over.
            for item in db.execute("""SELECT d.*,s.site,s.series,s.payload FROM discoveries d
                JOIN subscriptions s ON s.id=d.subscription_id WHERE s.root=? AND d.state='failed'""", (root,)):
                if any(self.matches(job, item) for job in failures if job.get("status") == "failed"):
                    continue
                failures.append({"id": item["download_id"], "site": item["site"],
                                 "title": json.loads(item["payload"])["name"], "season": item["season"],
                                 "episode": item["episode"], "status": "failed", "error": item["error"],
                                 "start_time": item["submitted_at"]})
            unique = {}
            for item in sorted(failures, key=lambda item: item.get("end_time") or item.get("start_time") or 0):
                if item.get("status") == "failed" and (item.get("id") in completed or self._available_failure(db, root, item)):
                    continue
                key = failure_key(item)
                retried = target_key(item) in retired and (item.get("start_time") or 0) <= retired[target_key(item)]
                if key not in hidden and not retried:
                    unique[target_key(item)] = item
            queue["history"] = list(unique.values())
            for item in queue["history"]:
                if item.get("status") == "failed":
                    item["cancel_id"] = "failed:" + failure_key(item)
            # Selected episodes wait durably in MyFlix until an engine slot opens.
            queue.setdefault("scheduled", []).extend({"id": f"selection:{row['subscription_id']}:{row['season']}:{row['episode']}", "title": row["series"], "site": row["site"],
                "season": row["season"], "episode": row["episode"], "status": "Queued", "priority": False}
                for row in db.execute("""SELECT s.series,s.site,d.subscription_id,d.season,d.episode FROM discoveries d
                    JOIN subscriptions s ON s.id=d.subscription_id
                    WHERE s.root=? AND d.state='manual_pending' ORDER BY s.id,d.season,d.episode""", (root,)))
            queue["checks"] = [{"series": row["series"], "error": row["error"]} for row in db.execute(
                "SELECT series,error FROM subscriptions WHERE root=? AND mode IN ('auto','baseline') AND error!=''", (root,))]

            queue["retrying"] = [{**json.loads(row["source"]), "id": "retry:" + str(row["id"]),
                                  "cancel_id": "retry:" + str(row["id"]) if row["state"] == "pending" else None,
                                  "status": "Priority retry queued" if row["state"] == "pending" else "Request unconfirmed — check queue",
                                  "priority": True, "error": row["error"]}
                                 for row in retries if row["state"] in ("pending", "submitting", "uncertain")]
            ids = {row["download_id"] for row in retries if row["download_id"]}
            for group in ("active", "scheduled"):
                for item in queue.get(group, []):
                    item["priority"] = bool(item.get("priority") or item.get("id") in ids or item.get("parent_id") in ids)
                queue[group] = sorted(queue.get(group, []), key=lambda item: not item["priority"])
        return queue

    @staticmethod
    def matches(job, item):
        name = json.loads(item["payload"])["name"]
        title = job.get("series_name") or job.get("title") or ""
        exact = ("season" not in item.keys() or (str(job.get("season")) == str(item["season"])
                 and str(job.get("episode")) == str(item["episode"])))
        return exact and job.get("site") == item["site"] and (title == name or title.startswith(name + " - S"))

    @staticmethod
    def status(db, root, body, subscription):
        from .monitoring import episode_status
        if body["payload"].get("is_movie"):
            rows = db.execute("""SELECT m.title,m.available,COALESCE(p.watched,0) watched FROM media m
                LEFT JOIN progress p ON p.media_id=m.id WHERE m.root=? AND m.series=''
                """, (root,)).fetchall()
            rows = [row for row in rows if _movie_title_matches(row["title"], body["payload"])]
            return {"available": any(row["available"] for row in rows),
                    "watched": any(row["watched"] for row in rows), "excluded": False}
        series = subscription["series"] if subscription else body["payload"]["name"]
        return episode_status(db, {"root": root, "series": series}, int(body["season"]), int(body["episodes"]))

    def enqueue(self, queue):
        from .monitoring import queued_episode
        if not all(isinstance(queue.get(key), list) and all(isinstance(job, dict) for job in queue[key])
                   for key in ("active", "scheduled", "history")):
            raise VibraVidError("Could not verify failed downloads. Refresh the queue before retrying.")
        queue = self.present(queue)
        queued = skipped = 0
        with connect(self.database) as db:
            db.execute("BEGIN IMMEDIATE")
            root = settings(db)["videos_path"]
            subscriptions = db.execute("SELECT * FROM subscriptions WHERE root=?", (root,)).fetchall()
            for item in queue["history"]:
                if item.get("status") != "failed":
                    continue
                request = db.execute("SELECT body FROM download_requests WHERE root=? AND download_id=?",
                                     (root, item.get("id", ""))).fetchone()
                body = json.loads(request["body"]) if request else None
                if body and (not isinstance(body.get("payload"), dict)
                             or not isinstance(body["payload"].get("name"), str) or not body["payload"]["name"]):
                    body = None
                subscription = next((candidate for candidate in subscriptions if self.matches(item, candidate)), None)
                is_movie = bool(body and body["payload"].get("is_movie"))
                try:
                    season, episode = int(item["season"]), int(item["episode"])
                    exact = 0 <= season <= 999 and 1 <= episode <= 9999
                except (KeyError, ValueError, TypeError):
                    exact = False
                if not body and subscription and exact:
                    body = {"site": subscription["site"], "payload": json.loads(subscription["payload"])}
                if body and not is_movie and exact:
                    body.update(season=str(season), episodes=str(episode))
                if not body or (not is_movie and not exact):
                    skipped += 1
                    continue
                name = body["payload"]["name"]
                if subscription and exact:
                    discovery = db.execute("SELECT * FROM discoveries WHERE subscription_id=? AND season=? AND episode=?",
                                           (subscription["id"], season, episode)).fetchone()
                    if not discovery or discovery["state"] != "failed" or not discovery["listed"]:
                        skipped += 1
                        continue
                if any(self.status(db, root, body, subscription).values()):
                    skipped += 1
                    continue
                if any(queued_episode(job, body["site"], name, season if exact else None, episode if exact else None)
                       for job in queue.get("active", []) + queue.get("scheduled", [])):
                    skipped += 1
                    continue
                # The unique source key makes repeat clicks and lost responses idempotent.
                result = db.execute("""INSERT OR IGNORE INTO download_retries(root,source_key,source,body)
                    VALUES(?,?,?,?)""", (root, failure_key(item), json.dumps(item), json.dumps(body)))
                queued += result.rowcount
                if subscription and exact and result.rowcount:
                    db.execute("UPDATE discoveries SET state='retry_pending' WHERE subscription_id=? AND season=? AND episode=?",
                               (subscription["id"], season, episode))
        return {"queued": queued, "skipped": skipped}

    def dispatch(self, engine):
        """Reserve before sending; an uncertain send never returns to pending."""
        from .monitoring import queued_episode
        queue = engine.call("queue")
        if not all(isinstance(queue.get(key), list) for key in ("active", "scheduled")):
            raise VibraVidError("Could not verify the queue before retrying.")
        with connect(self.database) as db:
            config = settings(db)
            root = config["videos_path"]
            if config.get("scan_error") or not config.get("last_scan") or not Path(root).is_dir():
                return
            pending = db.execute("SELECT * FROM download_retries WHERE root=? AND state='pending' ORDER BY id", (root,)).fetchall()
            ids = {row[0] for row in db.execute("SELECT download_id FROM download_retries WHERE root=? AND state='submitted'", (root,))}
            capacity = int(config["download_limit"])
        waiting = sum(bool(job.get("priority") or job.get("id") in ids or job.get("parent_id") in ids)
                      for job in queue["active"] + queue["scheduled"])
        for retry in pending[:max(0, capacity - waiting)]:
            body = json.loads(retry["body"])
            source = json.loads(retry["source"])
            with connect(self.database) as db:
                db.execute("BEGIN IMMEDIATE")
                if settings(db)["videos_path"] != root:
                    return
                subscription = next((s for s in db.execute("SELECT * FROM subscriptions WHERE root=? AND site=?", (root, body["site"]))
                                     if self.matches(source, s)), None)
                status = self.status(db, root, body, subscription)
                discovery = db.execute("SELECT listed FROM discoveries WHERE subscription_id=? AND season=? AND episode=?",
                                       (subscription["id"], source.get("season"), source.get("episode"))).fetchone() if subscription else None
                if any(status.values()) or (discovery and not discovery["listed"]):
                    db.execute("UPDATE download_retries SET state='skipped' WHERE id=?", (retry["id"],))
                    if subscription:
                        db.execute("""UPDATE discoveries SET state=?,error='' WHERE subscription_id=? AND season=? AND episode=?
                            AND state='retry_pending'""", ("downloaded" if status["available"] else "dismissed",
                                subscription["id"], source["season"], source["episode"]))
                    continue
                if any(queued_episode(job, body["site"], body["payload"]["name"], source.get("season"), source.get("episode"))
                       for job in queue["active"] + queue["scheduled"]):
                    continue
                submitted_at = time.time()
                result = db.execute("UPDATE download_retries SET state='submitting',submitted_at=? WHERE id=? AND state='pending'",
                                    (submitted_at, retry["id"]))
                if not result.rowcount:
                    continue
            state, download_id, error = "uncertain", "", ""
            try:
                result = self.submit(engine, body, priority=True, root=root)
                state, download_id = "submitted", result.get("download_id") or ""
            except VibraVidError as failure:
                error = str(failure)
                raise
            finally:
                with connect(self.database) as db:
                    db.execute("UPDATE download_retries SET state=?,download_id=?,error=? WHERE id=?",
                               (state, download_id, error, retry["id"]))
                    # Keep Following's per-episode reservation in sync with the retry.
                    for subscription in db.execute("SELECT * FROM subscriptions WHERE root=? AND site=?", (root, body["site"])):
                        if self.matches(source, subscription):
                            db.execute("""UPDATE discoveries SET state=?,download_id=?,submitted_at=?,error=''
                                WHERE subscription_id=? AND season=? AND episode=? AND state='retry_pending'""",
                                (state, download_id, submitted_at, subscription["id"], source.get("season"), source.get("episode")))
            queue["scheduled"].append({"id": download_id, "title": body["payload"]["name"], "site": body["site"],
                                        "season": body.get("season"), "episodes": body.get("episodes"), "priority": True})

    def reconcile(self, queue):
        with connect(self.database) as db:
            root = settings(db)["videos_path"]
            for item in queue.get("history", []):
                if item.get("status") in ("failed", "completed", "cancelled"):
                    if item["status"] == "failed" and self._available_failure(db, root, item):
                        db.execute("UPDATE download_request_state SET state='completed' WHERE root=? AND download_id=? AND state IN ('submitted','failed')",
                                   (root, item.get("id", "")))
                    else:
                        db.execute("UPDATE download_request_state SET state=? WHERE root=? AND download_id=? AND state='submitted'",
                                   (item["status"], root, item.get("id", "")))
            for request in _submitted_requests(db, root):
                body = json.loads(request["body"])
                if not body["payload"].get("is_movie") and (not str(body.get("season", "")).isdigit()
                        or not str(body.get("episodes", "")).isdigit()):
                    continue
                if self.status(db, root, body, None)["available"]:
                    db.execute("UPDATE download_request_state SET state='completed' WHERE root=? AND download_id=?",
                               (root, request["download_id"]))
            for row in db.execute("SELECT * FROM download_retries WHERE root=? AND state='submitted'", (root,)).fetchall():
                source = json.loads(row["source"])
                matches = [item for item in queue.get("history", []) if item.get("id") == row["download_id"]
                           and item.get("season") == source.get("season") and item.get("episode") == source.get("episode")
                           and item.get("start_time", 0) >= row["submitted_at"]]
                if not matches:
                    continue
                latest = max(matches, key=lambda item: item.get("end_time", 0))
                if latest.get("status") in ("failed", "completed", "cancelled"):
                    db.execute("UPDATE download_retries SET state=?,failure=? WHERE id=?",
                               (latest["status"], json.dumps(latest) if latest["status"] == "failed" else "", row["id"]))

    def pending(self):
        with connect(self.database) as db:
            return bool(db.execute("SELECT 1 FROM download_retries WHERE root=? AND state='pending' LIMIT 1",
                                   (settings(db)["videos_path"],)).fetchone())

    def cancel_local(self, queue, identity):
        """Retire a failed attempt or a retry that has not reached the engine."""
        queue = self.present(queue)
        item = next((job for group in ("history", "retrying") for job in queue[group]
                     if job.get("cancel_id") == identity), None)
        with connect(self.database) as db:
            db.execute("BEGIN IMMEDIATE")
            root = settings(db)["videos_path"]
            if not item:
                # Repeated cancellation of an already retired attempt is safe.
                row = db.execute("SELECT 1 FROM download_retries WHERE root=? AND state='cancelled' AND "
                                 + ("source_key=?" if identity.startswith("failed:") else "id=?"),
                                 (root, identity.split(":", 1)[1])).fetchone()
                if row:
                    return {"cancelled": True}
                raise ValueError("This download may have started. Refresh the queue before cancelling it")
            if identity.startswith("failed:"):
                db.execute("""INSERT INTO download_retries(root,source_key,source,body,state)
                    VALUES(?,?,?,'{}','cancelled') ON CONFLICT(root,source_key)
                    DO UPDATE SET state='cancelled',failure=''""",
                    (root, failure_key(item), json.dumps(item)))
            else:
                changed = db.execute("UPDATE download_retries SET state='cancelled' WHERE root=? AND id=? AND state='pending'",
                                     (root, identity.split(":", 1)[1]))
                if not changed.rowcount:
                    raise ValueError("This retry may have started. Refresh the queue before cancelling it")
            db.execute("UPDATE download_request_state SET state='cancelled' WHERE root=? AND download_id=?",
                       (root, item.get("id", "")))
            for subscription in db.execute("SELECT * FROM subscriptions WHERE root=?", (root,)):
                if self.matches(item, subscription):
                    db.execute("""UPDATE discoveries SET state='dismissed',error='' WHERE subscription_id=?
                        AND season=? AND episode=? AND state IN ('failed','retry_pending')""",
                        (subscription["id"], item.get("season"), item.get("episode")))
        return {"cancelled": True}

    def cancel_pending(self, queue, download_id):
        job = next((item for group in ("active", "scheduled") for item in queue.get(group, [])
                    if item.get("id") == download_id), None)
        if not job:
            return
        site, name, _, _ = target_key(job)
        with connect(self.database) as db:
            db.execute("BEGIN IMMEDIATE")
            root = settings(db)["videos_path"]
            # Persist cancellation intent before sending it to the engine. Even
            # an uncertain cancellation must never resurrect work after reboot.
            for row in _submitted_requests(db, root):
                body = json.loads(row["body"])
                if body.get("site") == site and body.get("payload", {}).get("name") == name:
                    db.execute("UPDATE download_request_state SET state='cancelled' WHERE root=? AND download_id=?", (root, row["download_id"]))
            for row in db.execute("SELECT * FROM download_retries WHERE root=? AND state='pending'", (root,)).fetchall():
                source = json.loads(row["source"])
                if target_key(source)[:2] != (site, name):
                    continue
                db.execute("UPDATE download_retries SET state='cancelled' WHERE id=?", (row["id"],))
                for subscription in db.execute("SELECT * FROM subscriptions WHERE root=? AND site=?", (root, site)):
                    if self.matches(source, subscription):
                        db.execute("""UPDATE discoveries SET state='dismissed' WHERE subscription_id=?
                            AND season=? AND episode=? AND state='retry_pending'""",
                            (subscription["id"], source.get("season"), source.get("episode")))

"""Durable series subscriptions and periodic provider checks."""
import hashlib
import json
import logging
import re
import threading
import time
import uuid
from pathlib import Path

from .db import connect, settings
from .downloads import DownloadRetries
from .vibravid import VibraVidError

INTERVAL = 4 * 3600


def follow(database, site, payload, series):
    if (not isinstance(site, str) or not site or len(site) > 100
            or not isinstance(payload, dict) or payload.get("is_movie")
            or not isinstance(payload.get("name"), str) or not payload["name"].strip()
            or not isinstance(payload.get("type"), str)
            or payload["type"].lower() in ("film", "movie", "ova", "song", "album", "book")
            or not isinstance(series, str) or not series.strip() or len(series) > 300):
        raise ValueError("Select a series and its provider")
    provider_id = next((payload.get(k) for k in ("id", "url", "path_id", "slug") if payload.get(k) not in (None, "")), None)
    if provider_id is None or not isinstance(provider_id, (str, int)) or isinstance(provider_id, bool):
        raise ValueError("This provider result has no stable series identity")
    identity = hashlib.sha256(json.dumps(provider_id).encode()).hexdigest()
    with connect(database) as db:
        db.execute("BEGIN IMMEDIATE")
        root = settings(db)["videos_path"]
        old = db.execute("SELECT id FROM subscriptions WHERE root=? AND (series=? OR (site=? AND identity=?))",
                         (root, series.strip(), site, identity)).fetchone()
        if old:
            raise ValueError("This series is already followed. Manage it in Following.")
        db.execute("""INSERT INTO subscriptions(root,series,site,identity,payload,mode)
            VALUES(?,?,?,?,?,'auto')""", (root, series.strip(), site, identity, json.dumps(payload)))


def episode_status(db, subscription, season, episode):
    rule = db.execute("SELECT * FROM season_state WHERE root=? AND series=? AND season=?",
                      (subscription["root"], subscription["series"], season)).fetchone()
    rows = db.execute("""SELECT m.available,COALESCE(p.watched,0) watched FROM media m
        LEFT JOIN progress p ON p.media_id=m.id WHERE m.root=? AND m.series=? AND m.season=? AND m.episode=?""",
        (subscription["root"], subscription["series"], season, episode)).fetchall()
    return {"available": any(r["available"] for r in rows),
            "watched": bool((rule and rule["watched"]) or any(r["watched"] for r in rows)),
            "excluded": bool(rule and rule["excluded"])}


def queued_episode(job, site, name, season, episode):
    """Match exact episodes when known; conservatively block ambiguous series jobs."""
    title = job.get("series_name") or job.get("title", "")
    if job.get("site") != site or not (title == name or title.startswith(name + " - S")):
        return False
    match = re.search(r" - S(\d+) E(.+)$", job.get("title", ""))
    number = job.get("season") or (match[1] if match else None)
    try:
        if number is not None and int(number) != season:
            return False
        if number is None:
            return True
        if job.get("episode"):
            return int(job["episode"]) == episode
        if job.get("planned_episodes"):
            return episode in job["planned_episodes"]
        selection = job.get("episodes") or (match[2] if match else "*")
        for part in str(selection).split(","):
            if part.strip() == "*":
                return True
            if "-" in part:
                first, last = part.split("-", 1)
                if int(first) <= episode <= int(last):
                    return True
            elif int(part) == episode:
                return True
        return False
    except (ValueError, TypeError):
        return True


def lookup_status(subscription, config):
    item = dict(subscription)
    state = item["lookup_state"]
    error = item["error"]
    done, total = item["lookup_seasons_done"], item["lookup_seasons_total"]
    episodes = item["lookup_episodes"]
    if state == "fetching":
        message = "Fetching episodes — loading season list…"
        if total:
            message = f"Fetching episodes — {done} of {total} seasons fetched · {episodes} episodes found"
            if item["lookup_season"]:
                message += f" · fetching season {item['lookup_season']}"
    elif item["mode"] == "paused":
        state, message = "paused", "Episode checks paused."
    elif error:
        if state == "error":
            message = f"Episode lookup failed after {done} of {total} seasons · {episodes} episodes found." if total else "Episode lookup failed."
        else:
            message = "Following needs attention."
        state = "error"
    elif item["next_check"] <= time.time():
        state, message = "queued", "Episode lookup queued — waiting for the background checker…"
        if not config.get("last_scan") or config.get("scan_error"):
            message = "Episode lookup queued — waiting for a successful library scan…"
    elif state == "complete":
        message = f"Episode lookup complete — {episodes} episodes found across {total} seasons."
        if not episodes:
            message = "Episode lookup complete — no episodes returned by the provider."
    else:
        state, message = "idle", "Waiting for the next scheduled episode check."
    return {"id": item["id"], "series": item["series"], "state": state,
            "message": message, "error": error,
            "seasons_done": done, "seasons_total": total, "episodes": episodes,
            "checked": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(item["checked_at"])) if item["checked_at"] else "Not checked yet"}


class SeriesMonitor:
    def __init__(self, database, engine):
        self.database = database
        self.engine = engine
        self.lock = threading.Lock()
        self.download_lock = threading.Lock()
        self.retries = DownloadRetries(database)

    def check(self, subscription):
        token = uuid.uuid4().hex
        with connect(self.database) as db:
            current = db.execute("SELECT * FROM subscriptions WHERE id=?", (subscription["id"],)).fetchone()
            if not current or current["mode"] == "paused":
                return
            db.execute("""UPDATE subscriptions SET lookup_state='fetching',lookup_token=?,
                lookup_seasons_done=0,lookup_seasons_total=0,lookup_season=0,lookup_episodes=0,error=''
                WHERE id=?""", (token, subscription["id"]))
        try:
            result = self.engine.call("episodes", {"site": subscription["site"],
                "payload": json.loads(subscription["payload"]), "lookup_token": token})
            self._finish_check(subscription, result, token)
        except Exception as error:
            message = str(error) if isinstance(error, (VibraVidError, ValueError)) else "Episode lookup failed. Check the service logs."
            with connect(self.database) as db:
                db.execute("""UPDATE subscriptions SET lookup_state='error',lookup_token='',error=?,next_check=?
                    WHERE id=? AND lookup_token=?""",
                    (message, int(time.time()) + INTERVAL, subscription["id"], token))
            raise

    def _finish_check(self, subscription, result, token):
        episodes = result.get("episodes")
        if not isinstance(episodes, list) or len(episodes) > 20000:
            raise VibraVidError("Invalid episode metadata; previous discoveries preserved")
        found = {}
        for item in episodes:
            if not isinstance(item, dict):
                raise VibraVidError("Invalid episode metadata")
            season, episode = item.get("season"), item.get("episode")
            if (type(season) is not int or type(episode) is not int
                    or not 0 <= season <= 999 or not 1 <= episode <= 9999):
                raise VibraVidError("Provider episode numbering is unsupported")
            title = item.get("title", "")
            if not isinstance(title, str):
                raise VibraVidError("Invalid episode title")
            found[season, episode] = title[:300]
        with connect(self.database) as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT * FROM subscriptions WHERE id=?", (subscription["id"],)).fetchone()
            if not current or current["lookup_token"] != token:
                return
            if current["mode"] == "paused":
                db.execute("UPDATE subscriptions SET lookup_state='',lookup_token='' WHERE id=?", (current["id"],))
                return
            db.execute("UPDATE discoveries SET listed=0 WHERE subscription_id=?", (current["id"],))
            for (season, episode), title in found.items():
                db.execute("""INSERT INTO discoveries(subscription_id,season,episode,title,state)
                    VALUES(?,?,?,?,?) ON CONFLICT(subscription_id,season,episode)
                    DO UPDATE SET title=excluded.title,listed=1""",
                    (current["id"], season, episode, title, "new"))
            season_count = len({season for season, episode in found})
            db.execute("""UPDATE subscriptions SET initialized=?,checked_at=?,next_check=?,error='',lookup_state='complete',lookup_token='',
                lookup_episodes=?,lookup_seasons_done=?,lookup_seasons_total=? WHERE id=?""",
                       (bool(found) or current["initialized"], int(time.time()), int(time.time()) + INTERVAL, len(found),
                        season_count, season_count, current["id"]))

    def download(self, subscription_id, season, episode, *, automatic=False, include_watched=False):
        with self.download_lock:
            return self._download(subscription_id, season, episode, automatic=automatic, include_watched=include_watched)

    def retry_failed(self):
        queue = self.engine.call("queue")
        self.reconcile_queue(queue)
        with self.download_lock:
            return self.retries.enqueue(queue)

    def reconcile_queue(self, queue):
        """Persist confirmed episode failures without retrying uncertain requests."""
        if not all(isinstance(queue.get(key), list) for key in ("active", "scheduled", "history")):
            return
        if not all(isinstance(job, dict) for key in ("active", "scheduled", "history") for job in queue[key]):
            return
        self.retries.reconcile(queue)
        with self.download_lock, connect(self.database) as db:
            db.execute("BEGIN IMMEDIATE")
            pending = db.execute("""SELECT d.*,s.site,s.payload,s.series,s.root FROM discoveries d
                JOIN subscriptions s ON s.id=d.subscription_id WHERE s.root=?
                AND d.state IN ('submitted','submitting','uncertain')""", (settings(db)["videos_path"],)).fetchall()
            for item in pending:
                name = json.loads(item["payload"]).get("name", item["series"])
                if episode_status(db, item, item["season"], item["episode"])["available"]:
                    continue
                if any(queued_episode(job, item["site"], name, item["season"], item["episode"])
                       for job in queue["active"] + queue["scheduled"]):
                    continue
                matches = []
                for job in queue["history"]:
                    # Never infer a terminal outcome from a series-wide/range job.
                    match = re.search(r" - S(\d+) E(\d+)$", str(job.get("title") or ""))
                    season = job.get("season") if job.get("season") is not None else (match[1] if match else None)
                    episode = job.get("episode") if job.get("episode") is not None else (match[2] if match else None)
                    try:
                        exact = int(season) == item["season"] and int(episode) == item["episode"]
                        recent = float(job.get("start_time", 0)) >= item["submitted_at"]
                        ended = float(job.get("end_time") or job.get("start_time") or 0)
                    except (ValueError, TypeError):
                        continue
                    if (exact and recent and queued_episode(job, item["site"], name, item["season"], item["episode"])
                            and (not item["download_id"] or job.get("id") == item["download_id"])):
                        matches.append((ended, job))
                if not matches:
                    continue
                latest = max(matches, key=lambda match: match[0])[1]
                if latest.get("status") == "failed":
                    error = str(latest.get("error") or "Download failed; the engine supplied no details.")[:2000]
                    db.execute("""UPDATE discoveries SET state='failed',error=?
                        WHERE subscription_id=? AND season=? AND episode=?""",
                        (error, item["subscription_id"], item["season"], item["episode"]))

    def _download(self, subscription_id, season, episode, *, automatic, include_watched):
        # Check individual episodes and season ranges so independent episodes
        # of one followed series can download together without duplicates.
        queue = self.engine.call("queue")
        if not all(isinstance(queue.get(key), list) for key in ("active", "scheduled")):
            raise VibraVidError("Could not verify the queue. Check the engine before downloading.")
        with connect(self.database) as db:
            db.execute("BEGIN IMMEDIATE")
            subscription = db.execute("SELECT * FROM subscriptions WHERE id=? AND root=?",
                (subscription_id, settings(db)["videos_path"])).fetchone()
            if not subscription:
                raise ValueError("Followed series not found in this library")
            discovery = db.execute("SELECT * FROM discoveries WHERE subscription_id=? AND season=? AND episode=?",
                                   (subscription_id, season, episode)).fetchone()
            if not discovery:
                raise ValueError("Episode not found")
            if discovery["state"] in ("submitting", "submitted", "uncertain", "retry_pending"):
                raise ValueError("Check the download queue before allowing another attempt")
            status = episode_status(db, subscription, season, episode)
            if status["available"]:
                if automatic:
                    return False
                raise ValueError("This episode is already in the library")
            if automatic:
                config = settings(db)
                capacity = int(config["download_limit"])
                if len(queue["active"]) + len(queue["scheduled"]) >= capacity:
                    return False
                if (subscription["mode"] != "auto" or discovery["state"] != "new" or not discovery["listed"]
                        or status["excluded"] or status["watched"]
                        or settings(db).get("scan_error") or not settings(db).get("last_scan")
                        or not Path(subscription["root"]).is_dir()):
                    return False
            elif status["watched"] and not include_watched:
                raise ValueError("Select Download watched episode to download it again")
            if not discovery["listed"]:
                raise ValueError("This episode is no longer listed by the provider. Check again later.")
            payload = json.loads(subscription["payload"])
            name = payload.get("name", subscription["series"])
            for job in queue.get("active", []) + queue.get("scheduled", []):
                if queued_episode(job, subscription["site"], name, season, episode):
                    if automatic:
                        return False
                    raise ValueError("This episode already has queued work. Wait for it to finish.")
            db.execute("""UPDATE discoveries SET state='submitting',error='',download_id='',submitted_at=?
                WHERE subscription_id=? AND season=? AND episode=?""",
                       (time.time(), subscription_id, season, episode))
        # Commit the reservation before the mutation. A process crash leaves it
        # blocked, never eligible for an automatic retry after an uncertain send.
        state = "uncertain"
        download_id = ""
        try:
            result = self.retries.submit(self.engine, {"site": subscription["site"], "payload": payload,
                                                       "season": str(season), "episodes": str(episode)},
                                         priority=discovery["state"] == "failed", root=subscription["root"])
            if result.get("queued") is not True:
                raise VibraVidError("Download was not acknowledged. Check the queue before retrying.")
            state = "submitted"
            if isinstance(result.get("download_id"), str):
                download_id = result["download_id"][:200]
        finally:
            with connect(self.database) as db:
                db.execute("UPDATE discoveries SET state=?,download_id=? WHERE subscription_id=? AND season=? AND episode=?",
                           (state, download_id, subscription_id, season, episode))
        return True

    def tick(self):
        if not self.lock.acquire(blocking=False):
            return
        try:
            with connect(self.database) as db:
                pending = db.execute("""SELECT 1 FROM discoveries d JOIN subscriptions s ON s.id=d.subscription_id
                    WHERE s.root=? AND d.state IN ('submitted','submitting','uncertain')
                    UNION ALL SELECT 1 FROM download_retries WHERE root=? AND state='submitted' LIMIT 1""",
                    (settings(db)["videos_path"], settings(db)["videos_path"])).fetchone()
            if pending:
                try:
                    self.reconcile_queue(self.engine.call("queue"))
                except VibraVidError:
                    logging.warning("Could not check episode download outcomes; previous states retained")
            if self.retries.pending():
                try:
                    with self.download_lock:
                        self.retries.dispatch(self.engine)
                except VibraVidError:
                    logging.warning("Retry submission unconfirmed; inspect the queue before another attempt")
                if self.retries.pending():
                    return
            with connect(self.database) as db:
                subscriptions = db.execute("SELECT * FROM subscriptions WHERE root=? AND mode!='paused'",
                                           (settings(db)["videos_path"],)).fetchall()
            for subscription in subscriptions:
                try:
                    if subscription["error"] and subscription["next_check"] > time.time():
                        continue
                    if subscription["next_check"] <= time.time():
                        self.check(subscription)
                    if subscription["mode"] == "auto":
                        with connect(self.database) as db:
                            new = db.execute("SELECT season,episode FROM discoveries WHERE subscription_id=? AND state='new' AND listed=1 ORDER BY season,episode",
                                             (subscription["id"],)).fetchall()
                            # Skip known local/watched/excluded episodes before
                            # querying the engine queue. Download rechecks these
                            # rules under the write lock before claiming a send.
                            new = [item for item in new if not any(episode_status(db, subscription, item["season"], item["episode"]).values())]
                            limit = int(settings(db)["download_limit"])
                        for item in new[:limit]:
                            self.download(subscription["id"], item["season"], item["episode"], automatic=True)
                except (VibraVidError, ValueError) as error:
                    with connect(self.database) as db:
                        db.execute("UPDATE subscriptions SET error=?,next_check=? WHERE id=?",
                                   (str(error), int(time.time()) + INTERVAL, subscription["id"]))
        finally:
            self.lock.release()

    def run(self, stop, scan_ready=None):
        # A previous engine process cannot complete a lookup after a restart.
        with connect(self.database) as db:
            db.execute("""UPDATE subscriptions SET lookup_state='',lookup_token='',next_check=0,
                error='Episode lookup interrupted by restart; waiting to check again.'
                WHERE lookup_state='fetching'""")
        startup_recovery = True
        while not stop.is_set():
            if scan_ready is not None and not scan_ready.is_set():
                stop.wait(.2)
                continue
            try:
                if startup_recovery:
                    result = self.retry_failed()
                    startup_recovery = False
                    logging.info("Startup download recovery: %s priority retries queued; %s skipped",
                                 result["queued"], result["skipped"])
                self.tick()
            except Exception:
                logging.exception("Series monitoring failed")
            stop.wait(30)

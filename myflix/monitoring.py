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


def _series_identity(site, payload, series):
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
    return hashlib.sha256(json.dumps(provider_id).encode()).hexdigest()


def follow(database, site, payload, series):
    identity = _series_identity(site, payload, series)
    with connect(database) as db:
        db.execute("BEGIN IMMEDIATE")
        root = settings(db)["videos_path"]
        old = db.execute("SELECT id FROM subscriptions WHERE root=? AND (series=? OR (site=? AND identity=?))",
                         (root, series.strip(), site, identity)).fetchone()
        if old:
            raise ValueError("This series already has a download source. Manage it in Downloads.")
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
            message = "Automatic episode checks need attention."
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
        self.download_lock = threading.RLock()
        self.retries = DownloadRetries(database)
        self.restored_roots = {}

    def _ensure_series(self, site, payload, *, mode='setup', expected_root=None):
        series = payload.get("name") if isinstance(payload, dict) else None
        identity = _series_identity(site, payload, series)
        with connect(self.database) as db:
            db.execute("BEGIN IMMEDIATE")
            root = settings(db)["videos_path"]
            if expected_root is not None and root != expected_root:
                raise ValueError("The library changed while restoring download sources")
            old = db.execute("""SELECT * FROM subscriptions WHERE root=? AND (series=? OR (site=? AND identity=?))
                ORDER BY (site=? AND identity=?) DESC LIMIT 1""",
                (root, series.strip(), site, identity, site, identity)).fetchone()
            if old:
                if old["site"] != site or old["identity"] != identity:
                    if old["mode"] != 'setup':
                        raise ValueError("This series already uses another download source")
                    # Merely inspecting a result must not claim its download source.
                    db.execute("DELETE FROM discoveries WHERE subscription_id=?", (old["id"],))
                    db.execute("""UPDATE subscriptions SET site=?,identity=?,payload=?,mode=?,initialized=0,
                        lookup_state='',lookup_token='',error='' WHERE id=?""", (site, identity, json.dumps(payload), mode, old["id"]))
                    return dict(db.execute("SELECT * FROM subscriptions WHERE id=?", (old["id"],)).fetchone())
                if old["mode"] == 'setup' and mode == 'baseline':
                    db.execute("UPDATE subscriptions SET mode='baseline',next_check=0 WHERE id=?", (old["id"],))
                    return dict(db.execute("SELECT * FROM subscriptions WHERE id=?", (old["id"],)).fetchone())
                return dict(old)
            identity_id = db.execute("""INSERT INTO subscriptions(root,series,site,identity,payload,mode)
                VALUES(?,?,?,?,?,?)""", (root, series.strip(), site, identity, json.dumps(payload), mode)).lastrowid
            return dict(db.execute("SELECT * FROM subscriptions WHERE id=?", (identity_id,)).fetchone())

    def inspect(self, site, payload):
        subscription = self._ensure_series(site, payload)
        self.check(subscription, baseline=True, full=True)
        with connect(self.database) as db:
            if settings(db)["videos_path"] != subscription["root"]:
                raise ValueError("The library changed. Search for the series again")
            current = db.execute("SELECT * FROM subscriptions WHERE id=?", (subscription["id"],)).fetchone()
            if current["site"] != subscription["site"] or current["identity"] != subscription["identity"]:
                raise ValueError("This search result changed. Load its episodes again")
            episodes = []
            for row in db.execute("SELECT * FROM discoveries WHERE subscription_id=? AND listed=1 ORDER BY season,episode",
                                  (subscription["id"],)):
                item = dict(row)
                available = episode_status(db, subscription, item["season"], item["episode"])["available"]
                locked = available or item["state"] in ('manual_pending','submitting','submitted','uncertain','retry_pending')
                episodes.append({"season": item["season"], "episode": item["episode"], "title": item["title"],
                                 "available": available, "locked": locked})
        return {"series_id": subscription["id"], "source": subscription["site"] + ':' + subscription["identity"], "episodes": episodes}

    def start(self, series_id, selection, only_new, source):
        if type(series_id) is not int or not 1 <= series_id <= 2**63 - 1 or type(only_new) is not bool:
            raise ValueError("Choose a series and a download action")
        if not isinstance(selection, list) or len(selection) > 20000 or (only_new and selection):
            raise ValueError("Choose valid episodes")
        selected = set()
        for item in selection:
            if (not isinstance(item, dict) or type(item.get("season")) is not int or type(item.get("episode")) is not int
                    or not 0 <= item["season"] <= 999 or not 1 <= item["episode"] <= 9999):
                raise ValueError("Choose valid episodes")
            selected.add((item["season"], item["episode"]))
        if not only_new and not selected:
            raise ValueError("Select at least one episode")
        with self.download_lock, connect(self.database) as db:
            db.execute("BEGIN IMMEDIATE")
            subscription = db.execute("SELECT * FROM subscriptions WHERE id=? AND root=?",
                (series_id, settings(db)["videos_path"])).fetchone()
            if not subscription or not subscription["initialized"] or subscription["lookup_state"] != 'complete':
                raise ValueError("Load the episode list before choosing downloads")
            if source != subscription["site"] + ':' + subscription["identity"]:
                raise ValueError("This search result changed. Load its episodes again")
            listed = {(row["season"], row["episode"]): row for row in db.execute(
                "SELECT * FROM discoveries WHERE subscription_id=? AND listed=1", (series_id,))}
            if any(identity not in listed for identity in selected):
                raise ValueError("An episode is no longer available. Reload the episode list")
            # Existing provider listings form the baseline. Unselected episodes
            # must not become automatic catch-up work on the next check.
            db.execute("UPDATE discoveries SET state='dismissed' WHERE subscription_id=? AND state='new'", (series_id,))
            queued = 0
            for season, episode in selected:
                item = listed[season, episode]
                if episode_status(db, subscription, season, episode)["available"] or item["state"] in (
                        'manual_pending','submitting','submitted','uncertain','retry_pending'):
                    continue
                db.execute("UPDATE discoveries SET state='manual_pending',error='' WHERE subscription_id=? AND season=? AND episode=?",
                           (series_id, season, episode))
                queued += 1
            db.execute("UPDATE subscriptions SET mode='auto',next_check=?,error='' WHERE id=?",
                       (int(time.time()) + INTERVAL, series_id))
        return {"queued": True, "selected": queued, "only_new": only_new}

    def cancel_selection(self, identity):
        match = re.fullmatch(r"selection:([0-9]{1,19}):([0-9]{1,3}):([0-9]{1,4})", identity)
        if not match or int(match[1]) > 2**63 - 1:
            raise ValueError("Select a queued episode")
        series_id, season, episode = map(int, match.groups())
        with self.download_lock, connect(self.database) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""SELECT d.state FROM discoveries d JOIN subscriptions s ON s.id=d.subscription_id
                WHERE s.root=? AND s.id=? AND d.season=? AND d.episode=?""",
                (settings(db)["videos_path"], series_id, season, episode)).fetchone()
            if not row or row["state"] not in ('manual_pending','dismissed'):
                raise ValueError("This episode may have started. Refresh the queue before cancelling it")
            db.execute("UPDATE discoveries SET state='dismissed' WHERE subscription_id=? AND season=? AND episode=?",
                       (series_id, season, episode))
        return {"cancelled": True}

    def restore_sources(self):
        """Give saved manual series downloads the same future-episode checks."""
        with connect(self.database) as db:
            root = settings(db)["videos_path"]
            cursor = self.restored_roots.get(root, 0)
            rows = db.execute("SELECT rowid,body FROM download_requests WHERE root=? AND rowid>? ORDER BY rowid",
                              (root, cursor)).fetchall()
        for row in rows:
            body = json.loads(row["body"])
            if body.get("payload", {}).get("is_movie"):
                continue
            try:
                self._ensure_series(body.get("site"), body.get("payload"), mode='baseline', expected_root=root)
            except ValueError:
                with connect(self.database) as db:
                    if settings(db)["videos_path"] != root:
                        return
                continue  # Legacy requests without a stable source remain manual.
        self.restored_roots[root] = rows[-1]["rowid"] if rows else cursor

    def check(self, subscription, *, baseline=False, full=False):
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
                "payload": json.loads(subscription["payload"]), "lookup_token": token,
                "latest_only": bool(current["initialized"]) and not full})
            self._finish_check(subscription, result, token, baseline=baseline)
        except Exception as error:
            message = str(error) if isinstance(error, (VibraVidError, ValueError)) else "Episode lookup failed. Check the service logs."
            with connect(self.database) as db:
                db.execute("""UPDATE subscriptions SET lookup_state='error',lookup_token='',error=?,next_check=?
                    WHERE id=? AND lookup_token=?""",
                    (message, int(time.time()) + INTERVAL, subscription["id"], token))
            raise

    def _finish_check(self, subscription, result, token, *, baseline=False):
        episodes = result.get("episodes")
        if not isinstance(episodes, list) or len(episodes) > 20000:
            raise VibraVidError("Invalid episode metadata; previous discoveries preserved")
        checked_seasons = result.get("checked_seasons")
        if checked_seasons is not None and (not isinstance(checked_seasons, list) or len(checked_seasons) > 1
                or any(type(number) is not int or not 0 <= number <= 999 for number in checked_seasons)):
            raise VibraVidError("Invalid episode lookup scope; previous discoveries preserved")
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
            if checked_seasons is not None and season not in checked_seasons:
                raise VibraVidError("Episode outside the checked season; previous discoveries preserved")
            found[season, episode] = title[:300]
        with connect(self.database) as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT * FROM subscriptions WHERE id=?", (subscription["id"],)).fetchone()
            if not current or current["lookup_token"] != token:
                return
            if current["mode"] == "paused":
                db.execute("UPDATE subscriptions SET lookup_state='',lookup_token='' WHERE id=?", (current["id"],))
                return
            if checked_seasons is None:
                db.execute("UPDATE discoveries SET listed=0 WHERE subscription_id=?", (current["id"],))
            elif checked_seasons:
                db.execute("UPDATE discoveries SET listed=0 WHERE subscription_id=? AND season=?",
                           (current["id"], checked_seasons[0]))
            latest = max((season for season, episode in found), default=None)
            for (season, episode), title in found.items():
                state = 'dismissed' if baseline and (current["mode"] != 'auto' or season != latest) else 'new'
                db.execute("""INSERT INTO discoveries(subscription_id,season,episode,title,state)
                    VALUES(?,?,?,?,?) ON CONFLICT(subscription_id,season,episode)
                    DO UPDATE SET title=excluded.title,listed=1""",
                    (current["id"], season, episode, title, state))
            season_count = len(checked_seasons) if checked_seasons is not None else len({season for season, episode in found})
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

    def recover_startup(self):
        with self.download_lock:
            # Keep manual submissions out of the gap between the queue snapshot
            # and the durable recovery reservation. Reconciliation shares this lock.
            queue = self.engine.call("queue")
            self.reconcile_queue(queue)
            interrupted = self.retries.recover_interrupted(self.engine, queue)
            return {**self.retries.enqueue(queue), "interrupted": interrupted}

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
                raise ValueError("Series not found in this library")
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
                manual = discovery["state"] == 'manual_pending'
                if (subscription["mode"] != "auto" or discovery["state"] not in ("new", "manual_pending") or not discovery["listed"]
                        or (not manual and (status["excluded"] or status["watched"]))
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
            self.restore_sources()
            with connect(self.database) as db:
                pending = db.execute("""SELECT 1 FROM discoveries d JOIN subscriptions s ON s.id=d.subscription_id
                    WHERE s.root=? AND d.state IN ('submitted','submitting','uncertain')
                    UNION ALL SELECT 1 FROM download_retries WHERE root=? AND state='submitted'
                    UNION ALL SELECT 1 FROM download_request_state WHERE root=? AND state='submitted' LIMIT 1""",
                    (settings(db)["videos_path"], settings(db)["videos_path"], settings(db)["videos_path"])).fetchone()
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
                subscriptions = db.execute("SELECT * FROM subscriptions WHERE root=? AND mode IN ('auto','notify','baseline')",
                                           (settings(db)["videos_path"],)).fetchall()
            for subscription in subscriptions:
                try:
                    if subscription["error"] and subscription["next_check"] > time.time():
                        continue
                    if subscription["next_check"] <= time.time():
                        self.check(subscription, baseline=subscription["mode"] == 'baseline', full=subscription["mode"] == 'baseline')
                        if subscription["mode"] == 'baseline':
                            with connect(self.database) as db:
                                db.execute("UPDATE subscriptions SET mode='auto' WHERE id=?", (subscription["id"],))
                            continue
                    if subscription["mode"] == "auto":
                        with connect(self.database) as db:
                            new = db.execute("SELECT season,episode,state FROM discoveries WHERE subscription_id=? AND state IN ('new','manual_pending') AND listed=1 ORDER BY state!='manual_pending',season,episode",
                                             (subscription["id"],)).fetchall()
                            # Skip known local/watched/excluded episodes before
                            # querying the engine queue. Download rechecks these
                            # rules under the write lock before claiming a send.
                            new = [item for item in new if not episode_status(db, subscription, item["season"], item["episode"])["available"]
                                   and (item["state"] == 'manual_pending' or not any(episode_status(db, subscription, item["season"], item["episode"]).values()))]
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
                    result = self.recover_startup()
                    startup_recovery = result["interrupted"] is None
                    logging.info("Startup download recovery: %s failed and %s interrupted jobs queued; %s skipped",
                                 result["queued"], result["interrupted"], result["skipped"])
                self.tick()
            except Exception:
                logging.exception("Series monitoring failed")
            stop.wait(30)

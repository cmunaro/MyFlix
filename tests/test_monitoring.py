import copy
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from myflix.app import create_app
from myflix.catalog import scan
from myflix.db import connect, initialize
from myflix.monitoring import INTERVAL, SeriesMonitor, queued_episode
from myflix.vibravid import VibraVidError


class Provider:
    def __init__(self):
        self.instance_id = "engine-1"
        self.episodes = [{"season": 1, "episode": 1, "title": "First"}]
        self.calls = []
        self.queue = {"active": [], "scheduled": [], "history": []}
        self.fail = None

    def call(self, action, body=None):
        self.calls.append((action, copy.deepcopy(body)))
        if self.fail == action:
            raise VibraVidError("Provider unavailable; check queue")
        if action == "episodes":
            episodes = copy.deepcopy(self.episodes)
            if episodes:
                self.last_season = max(item["season"] for item in episodes)
            if body.get("latest_only"):
                latest = getattr(self, "last_season", None)
                return {"episodes": [item for item in episodes if item["season"] == latest],
                        "checked_seasons": [latest] if latest is not None else []}
            return {"episodes": episodes}
        if action == "queue":
            return copy.deepcopy(self.queue)
        return {"queued": True, "download_id": f"job-{len(self.calls)}"}


class MonitoringTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.database = str(self.base / "myflix.sqlite3")
        self.app = create_app({"TESTING": True, "DATABASE": self.database})
        self.client = self.app.test_client()
        self.client.get("/")
        self.library = self.base / "library"
        self.provider = Provider()
        self.monitor = self.app.extensions["monitor"]
        self.monitor.engine = self.provider
        self.payload = {"id": "show-1", "name": "Show", "type": "tv", "is_movie": False}
        scan(self.database)

    def tearDown(self):
        self.temp.cleanup()

    def post(self, path, data=None, body=None):
        with self.client.session_transaction() as session:
            csrf = session["csrf"]
        if body is not None:
            return self.client.post(path, json=body, headers={"X-CSRF-Token": csrf})
        return self.client.post(path, data={"csrf": csrf, **(data or {})})

    def follow(self, series="Show", automatic=False):
        self.assertEqual(self.post("/admin/follow", body={"site": "example", "payload": self.payload, "series": series}).status_code, 200)
        if not automatic:
            self.post("/admin/following/1", {"mode": "notify"})
        self.monitor.tick()

    def check_now(self):
        with connect(self.database) as db:
            db.execute("UPDATE subscriptions SET next_check=0")
        self.monitor.tick()

    def discoveries(self):
        with connect(self.database) as db:
            return [dict(row) for row in db.execute("SELECT * FROM discoveries ORDER BY season,episode")]

    def video(self, name):
        path = self.library / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"video")
        os.utime(path, (time.time() - 120, time.time() - 120))
        return path

    def lookup(self):
        return self.client.get("/admin/following/status").json["subscriptions"][0]

    def test_live_lookup_progress_completion_and_private_status_boundary(self):
        from myflix.engine_api import report_metadata

        self.post("/admin/follow", body={"site": "example", "payload": self.payload, "series": "Show"})
        self.assertEqual(self.lookup()["state"], "queued")
        call = self.provider.call

        def provider(action, body=None):
            if action == "episodes":
                token = body["lookup_token"]
                self.assertEqual(self.lookup()["state"], "fetching")
                self.assertIn("season list", self.lookup()["message"])
                report_metadata(self.database, "stale-token", 99, 99, 999)
                self.assertEqual(self.lookup()["seasons_done"], 0)
                report_metadata(self.database, token, 1, 2, 8, 3)
                status = self.lookup()
                self.assertEqual((status["seasons_done"], status["seasons_total"], status["episodes"]), (1, 2, 8))
                self.assertIn("fetching season 3", status["message"])
                response = self.client.get("/admin/following/status")
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                self.assertNotIn(token.encode(), response.data)
                self.assertNotIn(b"payload", response.data)
                page = self.client.get("/admin?section=following").data
                self.assertIn(b'data-lookup-progress', page)
                self.assertIn(b'max="2" value="1"', page)
                # Another library's follows must never leak through the endpoint.
                with connect(self.database) as db:
                    db.execute("UPDATE subscriptions SET root='/other-library' WHERE id=1")
                self.assertEqual(self.client.get("/admin/following/status").json["subscriptions"], [])
                with connect(self.database) as db:
                    db.execute("UPDATE subscriptions SET root=? WHERE id=1", (str(self.library),))
            return call(action, body)

        self.provider.call = provider
        self.monitor.tick()
        status = self.lookup()
        self.assertEqual(status["state"], "complete")
        self.assertEqual(status["episodes"], 1)
        self.assertIn("1 episodes found", status["message"])
        self.assertNotEqual(status["checked"], "Not checked yet")
        self.assertEqual(self.discoveries()[0]["state"], "submitted")
        # Polling reads only SQLite; it never causes another provider request.
        calls = len(self.provider.calls)
        self.lookup()
        self.assertEqual(len(self.provider.calls), calls)

    def test_lookup_failure_is_visible_and_late_progress_cannot_revive_it(self):
        from myflix.engine_api import report_metadata

        self.follow()
        previous = self.discoveries()
        call = self.provider.call
        tokens = []

        def fail(action, body=None):
            if action == "episodes":
                tokens.append(body["lookup_token"])
                report_metadata(self.database, tokens[-1], 1, 2, 7, 2)
                raise VibraVidError("Episode lookup timed out")
            return call(action, body)

        self.provider.call = fail
        self.check_now()
        status = self.lookup()
        self.assertEqual(status["state"], "error")
        self.assertIn("timed out", status["error"])
        self.assertEqual(status["seasons_done"], 1)
        self.assertIn("1 of 2 seasons", status["message"])
        self.assertEqual(self.discoveries(), previous)
        report_metadata(self.database, tokens[-1], 2, 2, 20)
        self.assertEqual(self.lookup(), status)
        self.post("/admin/following/1", {"mode": "notify"})
        self.assertEqual(self.lookup()["state"], "queued")
        self.assertEqual(self.lookup()["error"], "")
        self.provider.call = call
        self.monitor.tick()
        self.assertEqual(self.lookup()["state"], "complete")

    def test_interrupted_lookup_requeues_on_restart_and_migration_preserves_progress(self):
        self.follow()
        with connect(self.database) as db:
            db.execute("UPDATE subscriptions SET lookup_state='fetching',lookup_token='old',lookup_seasons_done=2")
        initialize(self.database)
        self.assertEqual(self.lookup()["seasons_done"], 2)
        stop = threading.Event()

        def tick():
            status = self.lookup()
            self.assertEqual(status["state"], "error")
            self.assertIn("interrupted", status["error"])
            with connect(self.database) as db:
                row = db.execute("SELECT lookup_token,next_check FROM subscriptions WHERE id=1").fetchone()
            self.assertEqual(tuple(row), ("", 0))
            stop.set()

        with patch.object(self.monitor, "tick", side_effect=tick):
            self.monitor.run(stop)

    def test_progress_columns_migrate_without_changing_follows_or_discoveries(self):
        self.follow()
        previous = self.discoveries()
        columns = ("lookup_state", "lookup_token", "lookup_seasons_done",
                   "lookup_seasons_total", "lookup_season", "lookup_episodes")
        with connect(self.database) as db:
            for column in columns:
                db.execute("ALTER TABLE subscriptions DROP COLUMN " + column)
        initialize(self.database)
        initialize(self.database)
        self.assertEqual(self.discoveries(), previous)
        self.assertEqual(self.lookup()["series"], "Show")
        self.assertEqual(self.lookup()["state"], "idle")

    def test_queued_status_explains_scan_block_and_pause(self):
        self.follow()
        self.post("/admin/following/1", {"mode": "auto"})
        with connect(self.database) as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES('scan_error','Missing library mount')")
        self.assertIn("successful library scan", self.lookup()["message"])
        self.post("/admin/following/1", {"mode": "paused"})
        self.assertEqual(self.lookup()["state"], "paused")

    def test_sparse_episodes_and_new_season_are_durable(self):
        self.provider.episodes = [{"season": 1, "episode": 1}, {"season": 1, "episode": 3}]
        self.follow()
        self.assertEqual([r["state"] for r in self.discoveries()], ["new", "new"])
        count = len(self.provider.calls)
        self.monitor.tick()
        self.assertEqual(len(self.provider.calls), count, "metadata not polled before due time")
        self.provider.episodes += [{"season": 1, "episode": 2}, {"season": 2, "episode": 1}]
        self.check_now()
        self.assertEqual([(r["season"], r["episode"], r["state"]) for r in self.discoveries()],
                         [(1, 1, "new"), (1, 3, "new"), (2, 1, "new")])
        self.assertTrue(all(row["listed"] for row in self.discoveries()), "older seasons retain their listings")
        initialize(self.database)
        page = self.client.get("/admin?section=following").data
        self.assertIn(b"New episodes", page)
        self.assertIn(b"Automatically download unwatched", page)
        with connect(self.database) as db:
            sub = db.execute("SELECT * FROM subscriptions").fetchone()
            self.assertGreaterEqual(sub["next_check"], sub["checked_at"] + INTERVAL)

    def test_recurring_checks_refresh_only_latest_season_and_preserve_older_states(self):
        self.provider.episodes = [{"season": 1, "episode": 1}, {"season": 2, "episode": 1}]
        self.follow()
        self.assertFalse([body for action, body in self.provider.calls if action == "episodes"][0]["latest_only"])
        self.post("/admin/following/1/1/1", {"action": "dismiss"})
        self.provider.episodes += [{"season": 1, "episode": 2}, {"season": 2, "episode": 2}]
        self.check_now()
        self.assertTrue([body for action, body in self.provider.calls if action == "episodes"][-1]["latest_only"])
        self.assertEqual([(r["season"], r["episode"], r["state"], r["listed"]) for r in self.discoveries()],
                         [(1, 1, "dismissed", 1), (2, 1, "new", 1), (2, 2, "new", 1)])
        self.provider.episodes = [{"season": 2, "episode": 2}]
        self.check_now()
        self.assertEqual([(r["season"], r["episode"], r["listed"]) for r in self.discoveries()],
                         [(1, 1, 1), (2, 1, 0), (2, 2, 1)])
        self.provider.episodes += [{"season": 3, "episode": 1}]
        self.check_now()
        self.assertEqual((self.discoveries()[-1]["season"], self.discoveries()[-1]["episode"]), (3, 1))

    def test_metadata_errors_do_not_erase_or_create_discoveries(self):
        self.follow()
        self.provider.episodes = [{"season": 1, "episode": "2"}]
        self.check_now()
        self.assertEqual(len(self.discoveries()), 1)
        with connect(self.database) as db:
            self.assertIn("unsupported", db.execute("SELECT error FROM subscriptions").fetchone()[0])
        self.provider.episodes = []
        self.check_now()
        self.assertEqual(self.discoveries()[0]["listed"], 0)
        self.assertEqual(self.post("/admin/following/1/1/1", {"action": "download"}).status_code, 400)
        self.provider.episodes = [{"season": 1, "episode": 1}]
        self.check_now()
        self.assertEqual(self.discoveries()[0]["state"], "new")

    def test_follow_automatically_downloads_existing_unwatched_episodes(self):
        watched = self.video("Show.S01E01.mp4")
        self.video("Show.S01E02.mp4")
        scan(self.database)
        with connect(self.database) as db:
            db.execute("INSERT INTO progress(media_id,watched) SELECT id,1 FROM media WHERE episode=1")
            db.execute("INSERT INTO season_state(root,series,season,excluded) VALUES(?,'Show',2,1)", (str(self.library),))
        watched.unlink()
        scan(self.database)
        self.provider.episodes = [{"season": 1, "episode": n} for n in (1, 2, 3, 4)] + [{"season": 2, "episode": 1}]
        self.follow(automatic=True)
        with connect(self.database) as db:
            self.assertEqual(db.execute("SELECT mode FROM subscriptions").fetchone()[0], "auto")
        self.assertEqual([(b["season"], b["episodes"]) for a, b in self.provider.calls if a == "download"], [("1", "3"), ("1", "4")])
        self.monitor.tick()
        self.assertEqual(len([a for a, _ in self.provider.calls if a == "download"]), 2)

    def test_parallel_same_series_downloads_keep_automatic_backlog_bounded(self):
        self.provider.episodes = [{"season": 1, "episode": n} for n in range(1, 11)]
        original = self.provider.call
        def call(action, body=None):
            result = original(action, body)
            if action == "download":
                self.provider.queue["scheduled"].append({"site": "example", "title": "Show",
                    "season": body["season"], "episodes": body["episodes"]})
            return result
        with patch.object(self.provider, "call", side_effect=call):
            self.follow(automatic=True)
            self.assertEqual(len(self.provider.queue["scheduled"]), 4)
            self.provider.queue["active"] = self.provider.queue.pop("scheduled")
            self.provider.queue["scheduled"] = []
            for item, status in zip(self.provider.queue["active"],
                                    ("downloading", "Processing", "Waiting for processing", "Waiting for processing")):
                item["status"] = status
            self.monitor.tick()
            self.assertEqual(len(self.provider.queue["scheduled"]), 0)
            self.provider.queue["active"].pop()
            self.monitor.tick()
            self.assertEqual(len(self.provider.queue["scheduled"]), 1)
        self.assertEqual([body["episodes"] for action, body in self.provider.calls if action == "download"], ["1", "2", "3", "4", "5"])

    def test_queue_matching_covers_episode_numbers_ranges_and_ambiguous_series(self):
        active = {"site": "example", "series_name": "Show", "season": 1, "episode": 2}
        self.assertTrue(queued_episode(active, "example", "Show", 1, 2))
        self.assertFalse(queued_episode(active, "example", "Show", 1, 3))
        season = {"site": "example", "title": "Show - S1 E1-3,5"}
        self.assertTrue(queued_episode(season, "example", "Show", 1, 3))
        self.assertFalse(queued_episode(season, "example", "Show", 1, 4))
        self.assertFalse(queued_episode(season, "example", "Show", 2, 1))
        self.assertTrue(queued_episode({"site": "example", "series_name": "Show"}, "example", "Show", 1, 1))
        self.assertFalse(queued_episode(active, "other", "Show", 1, 2))

    def test_existing_follows_upgrade_once_without_retrying_uncertain_requests(self):
        self.follow()
        with connect(self.database) as db:
            db.execute("PRAGMA user_version=2")
            db.execute("UPDATE subscriptions SET next_check=9999999999")
            db.execute("UPDATE discoveries SET state='baseline'")
            db.execute("""INSERT INTO discoveries(subscription_id,season,episode,title,state) VALUES
                (1,1,2,'Second','uncertain'),(1,1,3,'Third','submitted'),
                (1,1,4,'Fourth','dismissed'),(1,1,5,'Fifth','submitting')""")
            db.execute("""INSERT INTO subscriptions(root,series,site,identity,payload,mode)
                SELECT root,'Other Show',site,'other',payload,'paused' FROM subscriptions WHERE id=1""")
        initialize(self.database)
        with connect(self.database) as db:
            self.assertEqual([r[0] for r in db.execute("SELECT mode FROM subscriptions ORDER BY id")], ["auto", "paused"])
            self.assertEqual(db.execute("SELECT next_check FROM subscriptions WHERE id=1").fetchone()[0], 0)
        self.assertEqual([r["state"] for r in self.discoveries()], ["new", "uncertain", "submitted", "dismissed", "submitting"])
        self.monitor.tick()
        self.assertEqual([b["episodes"] for a, b in self.provider.calls if a == "download"], ["1"])
        self.post("/admin/following/1", {"mode": "notify"})
        initialize(self.database)
        with connect(self.database) as db:
            self.assertEqual(db.execute("SELECT mode FROM subscriptions WHERE id=1").fetchone()[0], "notify")

    def test_watched_season_excludes_current_and_future_imports_and_manual_override(self):
        self.video("Show.S01E01.First.mp4")
        scan(self.database)
        self.follow()
        self.assertEqual(self.post("/admin/season", {"series": "Show", "season": "1", "action": "watched"}).status_code, 302)
        self.provider.episodes.append({"season": 1, "episode": 2})
        self.post("/admin/following/1", {"mode": "auto"})
        self.monitor.tick()
        self.assertFalse(any(action == "download" for action, _ in self.provider.calls))
        self.assertEqual(self.post("/admin/following/1/1/2", {"action": "download"}).status_code, 400)
        self.assertEqual(self.post("/admin/following/1/1/2", {"action": "download", "include_watched": "1"}).status_code, 302)
        self.video("Show.S01E02.Second.mp4")
        scan(self.database)
        with connect(self.database) as db:
            self.assertEqual([r[0] for r in db.execute("SELECT watched FROM progress ORDER BY media_id")], [1, 1])
        self.assertEqual(self.discoveries()[1]["state"], "downloaded")
        self.post("/admin/season", {"series": "Show", "season": "1", "action": "unwatched"})
        with connect(self.database) as db:
            self.assertEqual(tuple(db.execute("SELECT watched,excluded FROM season_state").fetchone()), (0, 1))

    def test_automatic_downloads_skip_local_watched_excluded_and_external_queue(self):
        self.follow()
        self.video("Show.S01E02.mp4")
        self.video("Show.S01E03.mp4")
        scan(self.database)
        with connect(self.database) as db:
            media_id = db.execute("SELECT id FROM media WHERE episode=3").fetchone()[0]
            db.execute("INSERT INTO progress(media_id,watched) VALUES(?,1)", (media_id,))
        (self.library / "Show.S01E03.mp4").unlink()
        scan(self.database)
        self.provider.episodes += [{"season": 1, "episode": n} for n in (2, 3, 4)]
        self.post("/admin/following/1", {"mode": "auto"})
        self.provider.queue["active"] = [{"site": "example", "title": "Show - S1 E*"}]
        self.monitor.tick()
        self.assertFalse(any(a == "download" for a, _ in self.provider.calls))
        self.provider.queue["active"] = []
        self.monitor.tick()
        downloads = [body for action, body in self.provider.calls if action == "download"]
        self.assertEqual([b["episodes"] for b in downloads], ["1", "4"])
        self.monitor.tick()
        self.assertEqual([b["episodes"] for a, b in self.provider.calls if a == "download"], ["1", "4"])

    def test_uncertain_and_crashed_requests_require_explicit_queue_review(self):
        self.follow()
        self.provider.fail = "download"
        self.assertEqual(self.post("/admin/following/1/1/1", {"action": "download"}).status_code, 400)
        self.assertEqual(self.discoveries()[0]["state"], "uncertain")
        self.provider.fail = None
        self.assertEqual(self.post("/admin/following/1/1/1", {"action": "download"}).status_code, 400)
        self.assertEqual(self.post("/admin/following/1/1/1", {"action": "retry"}).status_code, 400)
        self.assertEqual(self.post("/admin/following/1/1/1", {"action": "retry", "queue_checked": "1"}).status_code, 302)
        self.post("/admin/following/1", {"mode": "auto"})
        self.monitor.tick()
        self.assertEqual(self.discoveries()[0]["state"], "manual")
        self.assertEqual(len([a for a, _ in self.provider.calls if a == "download"]), 1)
        self.assertEqual(self.post("/admin/following/1/1/1", {"action": "download"}).status_code, 302)
        with connect(self.database) as db:
            db.execute("UPDATE discoveries SET state='submitting'")
        initialize(self.database)
        self.assertEqual(self.post("/admin/following/1/1/1", {"action": "download"}).status_code, 400)
        self.assertEqual(len([a for a, _ in self.provider.calls if a == "download"]), 2)

    def test_confirmed_failure_is_durable_visible_and_manually_retryable(self):
        self.follow(automatic=True)
        sent = self.discoveries()[0]
        failure = {"id": sent["download_id"], "site": "example", "title": "Show", "season": 1,
                   "episode": 1, "status": "failed", "error": "Merge failed <script>bad</script>",
                   "start_time": sent["submitted_at"] + 1, "end_time": sent["submitted_at"] + 2}
        self.provider.queue["history"] = [failure]
        with patch.object(self.app.extensions["vibravid"], "call", return_value=self.provider.queue):
            self.assertEqual(self.client.get("/admin/vibravid/queue").status_code, 200)
        self.assertEqual(self.discoveries()[0]["state"], "failed")
        self.assertEqual(self.discoveries()[0]["error"], failure["error"])
        page = self.client.get("/admin?section=following").data
        self.assertIn(b"Season 1", page)
        self.assertIn(b"Failed downloads", page)
        self.assertIn("S1 · E1".encode(), page)
        self.assertIn(b"Merge failed &lt;script&gt;bad&lt;/script&gt;", page)
        self.assertIn(b"Retry failed episode", page)
        self.assertNotIn(b"Allow manual retry", page)
        self.provider.queue["history"] = []
        initialize(self.database)
        self.monitor.tick()
        self.assertEqual(self.discoveries()[0]["state"], "failed")
        self.assertEqual(len([a for a, _ in self.provider.calls if a == "download"]), 1)
        self.assertEqual(self.post("/admin/following/1/1/1", {"action": "download"}).status_code, 302)
        retry = self.discoveries()[0]
        self.assertEqual((retry["state"], retry["error"]), ("submitted", ""))
        self.assertNotEqual(retry["download_id"], sent["download_id"])
        self.provider.queue["history"] = [failure]
        self.monitor.tick()
        self.assertEqual(self.discoveries()[0]["state"], "submitted", "previous attempt cannot fail the retry")

    def test_failure_reconciliation_requires_exact_current_terminal_outcome(self):
        self.follow(automatic=True)
        sent = self.discoveries()[0]
        failure = {"id": sent["download_id"], "site": "example", "title": "Show", "season": 1,
                   "episode": 1, "status": "failed", "error": "Merge failed",
                   "start_time": sent["submitted_at"] + 1, "end_time": sent["submitted_at"] + 2}
        for changed in ({"id": "another-job"}, {"site": "other"}, {"season": 2}, {"episode": 2},
                        {"title": "Other Show"}, {"start_time": sent["submitted_at"] - 1},
                        {"episode": None}, {"status": "completed"}, {"status": "cancelled"}):
            with self.subTest(changed=changed):
                self.monitor.reconcile_queue({"active": [], "scheduled": [], "history": [{**failure, **changed}]})
                self.assertEqual(self.discoveries()[0]["state"], "submitted")
        self.monitor.reconcile_queue({"active": [{**failure, "status": "downloading"}], "scheduled": [], "history": [failure]})
        self.assertEqual(self.discoveries()[0]["state"], "submitted")
        self.monitor.reconcile_queue({"active": [], "scheduled": [{"site": "example", "title": "Show - S1 E*"}], "history": [failure]})
        self.assertEqual(self.discoveries()[0]["state"], "submitted")
        self.monitor.reconcile_queue({"active": [], "scheduled": [], "history": [failure, {**failure, "status": "completed", "end_time": failure["end_time"] + 1}]})
        self.assertEqual(self.discoveries()[0]["state"], "submitted")
        self.monitor.reconcile_queue({"active": [], "scheduled": [], "history": [failure]})
        self.assertEqual(self.discoveries()[0]["state"], "failed")
        self.video("Show.S01E01.mp4")
        scan(self.database)
        self.assertEqual((self.discoveries()[0]["state"], self.discoveries()[0]["error"]), ("downloaded", ""))

    def test_legacy_submitted_episode_recovers_failure_and_preserves_root_isolation(self):
        self.post("/admin/settings", {"videos_path": str(self.library)})
        self.follow()
        with connect(self.database) as db:
            db.execute("UPDATE discoveries SET state='submitted'")
        queue = {"active": [], "scheduled": [], "history": [{"site": "example", "title": "Show",
            "season": 1, "episode": 1, "status": "failed", "error": "Merge failed"}]}
        other = self.base / "other"
        other.mkdir()
        self.post("/admin/settings", {"videos_path": str(other)})
        self.monitor.reconcile_queue(queue)
        self.assertEqual(self.discoveries()[0]["state"], "submitted")
        self.post("/admin/settings", {"videos_path": str(self.library)})
        self.post("/admin/following/1", {"mode": "paused"})
        self.provider.queue = queue
        self.monitor.tick()
        self.assertEqual(self.discoveries()[0]["state"], "failed", "paused follows still show terminal errors")

    def test_failure_columns_migrate_without_resetting_reservations(self):
        self.follow(automatic=True)
        with connect(self.database) as db:
            for column in ("error", "download_id", "submitted_at"):
                db.execute("ALTER TABLE discoveries DROP COLUMN " + column)
        initialize(self.database)
        initialize(self.database)
        item = self.discoveries()[0]
        self.assertEqual((item["state"], item["error"], item["download_id"], item["submitted_at"]),
                         ("submitted", "", "", 0))

    def failed_downloads(self, count=2):
        self.provider.episodes = [{"season": 1, "episode": n} for n in range(1, count + 1)]
        self.follow(automatic=True)
        self.provider.queue["history"] = [{"id": row["download_id"], "site": "example", "title": "Show",
            "season": 1, "episode": row["episode"], "status": "failed", "error": "Merge failed",
            "start_time": row["submitted_at"] + .001, "end_time": row["submitted_at"] + .002}
            for row in self.discoveries()]

    def test_server_restart_automatically_retries_confirmed_failures_with_priority(self):
        self.failed_downloads(1)
        self.monitor.reconcile_queue(self.provider.queue)
        self.assertEqual(self.discoveries()[0]["state"], "failed")
        restarted = SeriesMonitor(self.database, self.provider)
        stop = threading.Event()
        tick = restarted.tick
        def first_tick():
            tick()
            stop.set()
        with patch.object(restarted, "tick", side_effect=first_tick):
            restarted.run(stop)
        self.assertEqual([b["episodes"] for a, b in self.provider.calls if a == "retry_download"], ["1"])
        self.assertEqual(self.discoveries()[0]["state"], "submitted")
        self.assertEqual(restarted.retries.present(copy.deepcopy(self.provider.queue))["history"], [])

    def test_startup_retries_once_without_looping_when_the_retry_fails(self):
        self.failed_downloads(1)
        restarted = SeriesMonitor(self.database, self.provider)
        stop = threading.Event()
        tick = restarted.tick
        ticks = []
        def advance():
            tick()
            ticks.append(True)
            if len(ticks) == 1:
                sent = self.discoveries()[0]
                self.provider.queue["history"] = [{"id": sent["download_id"], "site": "example", "title": "Show",
                    "season": 1, "episode": 1, "status": "failed", "error": "Still failing",
                    "start_time": sent["submitted_at"] + .01, "end_time": sent["submitted_at"] + .02}]
            else:
                stop.set()
        with patch.object(restarted, "tick", side_effect=advance), patch.object(stop, "wait", side_effect=lambda _: stop.is_set()):
            restarted.run(stop)
        self.assertEqual(len([a for a, _ in self.provider.calls if a == "retry_download"]), 1)
        self.assertEqual((self.discoveries()[0]["state"], self.discoveries()[0]["error"]), ("failed", "Still failing"))

    def test_startup_preserves_uncertain_requests_and_resumes_existing_pending_retries(self):
        self.failed_downloads()
        self.retry_all()
        self.provider.fail = "retry_download"
        self.monitor.tick()
        self.provider.fail = None
        restarted = SeriesMonitor(self.database, self.provider)
        stop = threading.Event()
        tick = restarted.tick
        def first_tick():
            tick()
            stop.set()
        with patch.object(restarted, "tick", side_effect=first_tick):
            restarted.run(stop)
        self.assertEqual([b["episodes"] for a, b in self.provider.calls if a == "retry_download"], ["1", "2"])
        self.assertEqual([r["state"] for r in self.discoveries()], ["uncertain", "submitted"])

    def test_startup_never_retries_submissions_without_terminal_evidence(self):
        self.failed_downloads(3)
        self.provider.queue["history"] = []
        with connect(self.database) as db:
            for episode, state in enumerate(("uncertain", "submitted", "submitting"), 1):
                db.execute("UPDATE discoveries SET state=? WHERE episode=?", (state, episode))
        restarted = SeriesMonitor(self.database, self.provider)
        stop = threading.Event()
        tick = restarted.tick
        def first_tick():
            tick()
            stop.set()
        with patch.object(restarted, "tick", side_effect=first_tick):
            restarted.run(stop)
        self.assertFalse(any(a == "retry_download" for a, _ in self.provider.calls))
        self.assertEqual([r["state"] for r in self.discoveries()], ["uncertain", "submitted", "submitting"])

    def run_restarted_monitor(self):
        restarted = SeriesMonitor(self.database, self.provider)
        stop = threading.Event()
        tick = restarted.tick
        def first_tick():
            tick()
            stop.set()
        with patch.object(restarted, "tick", side_effect=first_tick):
            restarted.run(stop)
        return restarted

    def test_reboot_recovers_acknowledged_missing_episodes_once_with_priority(self):
        self.provider.episodes = [{"season": 1, "episode": n} for n in (10, 11, 12)]
        self.follow(automatic=True)
        original_ids = [r["download_id"] for r in self.discoveries()]
        self.provider.instance_id = "engine-after-reboot"
        restarted = self.run_restarted_monitor()
        self.assertEqual([b["episodes"] for a, b in self.provider.calls if a == "retry_download"], ["10", "11", "12"])
        self.assertEqual([r["state"] for r in self.discoveries()], ["submitted"] * 3)
        self.assertFalse(set(original_ids) & {r["download_id"] for r in self.discoveries()})
        self.assertEqual(restarted.recover_startup()["interrupted"], 0)
        restarted.tick()
        self.assertEqual(len([a for a, _ in self.provider.calls if a == "retry_download"]), 3)
        with connect(self.database) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM download_retries").fetchone()[0], 3)
            self.assertEqual(db.execute("SELECT count(*) FROM download_request_state WHERE engine_instance='engine-after-reboot'").fetchone()[0], 3)

    def test_reboot_recovery_serializes_queue_read_and_reservation_with_manual_submissions(self):
        self.follow(automatic=True)
        self.provider.instance_id = "engine-after-reboot"
        reading, release, manual_entered = threading.Event(), threading.Event(), threading.Event()
        original = self.provider.call
        results = []
        def call(action, body=None):
            if action == "queue":
                reading.set()
                release.wait(3)
            return original(action, body)
        def recover():
            results.append(self.monitor.recover_startup())
        def manual():
            with self.monitor.download_lock:
                manual_entered.set()
        with patch.object(self.provider, "call", side_effect=call):
            recovery = threading.Thread(target=recover)
            submission = threading.Thread(target=manual)
            recovery.start()
            try:
                self.assertTrue(reading.wait(1))
                submission.start()
                self.assertFalse(manual_entered.wait(.1))
            finally:
                release.set()
                recovery.join(3)
                if submission.ident is not None:
                    submission.join(3)
        self.assertEqual(results[0]["interrupted"], 1)
        self.assertTrue(manual_entered.is_set())

    def test_reboot_does_not_resend_uncertain_or_unacknowledged_mutations(self):
        self.failed_downloads(3)
        self.provider.queue["history"] = []
        self.provider.instance_id = "engine-after-reboot"
        with connect(self.database) as db:
            db.execute("UPDATE discoveries SET state='uncertain' WHERE episode=1")
            db.execute("UPDATE discoveries SET state='submitting' WHERE episode=2")
            db.execute("DELETE FROM download_requests WHERE download_id=?", (self.discoveries()[2]["download_id"],))
        self.run_restarted_monitor()
        self.assertFalse(any(a == "retry_download" for a, _ in self.provider.calls))
        self.assertEqual([r["state"] for r in self.discoveries()], ["uncertain", "submitting", "submitted"])

    def test_reboot_recovery_checks_live_queue_and_library_readiness(self):
        self.follow(automatic=True)
        request = self.discoveries()[0]
        self.assertEqual(self.monitor.recover_startup()["interrupted"], 0, "the same engine must never be retried")
        self.provider.instance_id = "engine-after-reboot"
        for group, job in (("active", {"site": "example", "title": "Show", "season": 1, "episode": 1}),
                           ("scheduled", {"site": "example", "title": "Show - S1 E1-3"})):
            with self.subTest(group=group):
                self.provider.queue[group] = [job]
                self.assertEqual(self.monitor.recover_startup()["interrupted"], 0)
                self.provider.queue[group] = []
        for mode in ("paused", "notify"):
            with connect(self.database) as db:
                db.execute("UPDATE subscriptions SET mode=?", (mode,))
            self.assertEqual(self.monitor.recover_startup()["interrupted"], 0)
        with connect(self.database) as db:
            db.execute("UPDATE subscriptions SET mode='auto'")
            db.execute("INSERT INTO settings VALUES('scan_error','Drive unavailable')")
        self.assertIsNone(self.monitor.recover_startup()["interrupted"])
        with connect(self.database) as db:
            db.execute("DELETE FROM settings WHERE key='scan_error'")
        self.library.rename(self.base / "offline")
        self.assertIsNone(self.monitor.recover_startup()["interrupted"])
        (self.base / "offline").rename(self.library)
        original = self.provider.queue
        self.provider.queue = {"active": [], "history": []}
        with self.assertRaises(VibraVidError):
            self.monitor.recover_startup()
        self.provider.queue = original
        other = self.base / "other-library"
        other.mkdir()
        with connect(self.database) as db:
            db.execute("UPDATE settings SET value=? WHERE key='videos_path'", (str(other),))
        self.assertEqual(self.monitor.recover_startup()["interrupted"], 0)
        with connect(self.database) as db:
            db.execute("UPDATE settings SET value=? WHERE key='videos_path'", (str(self.library),))
        self.assertEqual(self.monitor.recover_startup()["interrupted"], 1)
        self.assertEqual(self.discoveries()[0]["state"], "retry_pending")

    def test_reboot_recovery_preserves_newly_published_file_even_before_scan_buffer(self):
        self.follow(automatic=True)
        self.provider.instance_id = "engine-after-reboot"
        path = self.video("Show.S01E01.mp4")
        os.utime(path, None)
        scan(self.database)  # Too fresh for the normal catalog buffer.
        self.assertEqual(self.discoveries()[0]["state"], "submitted")
        self.assertEqual(self.monitor.recover_startup()["interrupted"], 0)
        path.unlink()  # A later deliberate removal must not resurrect the old request.
        self.assertEqual(self.monitor.recover_startup()["interrupted"], 0)
        self.assertEqual(path.exists(), False)

    def test_reboot_recovery_skips_watched_excluded_and_unlisted_episodes(self):
        self.follow(automatic=True)
        self.provider.instance_id = "engine-after-reboot"
        with connect(self.database) as db:
            db.execute("UPDATE discoveries SET listed=0")
        self.assertEqual(self.monitor.recover_startup()["interrupted"], 0)
        with connect(self.database) as db:
            db.execute("UPDATE discoveries SET listed=1")
        for watched, excluded in ((1, 0), (0, 1)):
            with self.subTest(watched=watched, excluded=excluded):
                with connect(self.database) as db:
                    db.execute("UPDATE download_request_state SET state='submitted'")
                    db.execute("INSERT OR REPLACE INTO season_state VALUES(?,'Show',1,?,?)", (str(self.library), watched, excluded))
                self.assertEqual(self.monitor.recover_startup()["interrupted"], 0)
        self.assertFalse(any(a == "retry_download" for a, _ in self.provider.calls))

    def test_reboot_recovery_retains_terminal_outcomes_and_cancellation_intent(self):
        self.failed_downloads(3)
        sent = self.discoveries()
        for row, status in zip(self.provider.queue["history"], ("completed", "cancelled", "failed")):
            row["status"] = status
        self.monitor.reconcile_queue(self.provider.queue)
        self.provider.queue["history"] = []
        self.provider.instance_id = "engine-after-reboot"
        self.assertEqual(self.monitor.recover_startup()["interrupted"], 0)
        # A confirmed failure uses the existing failure retry path, not recovery
        # inferred from absence. Completed/cancelled jobs are never requeued.
        with connect(self.database) as db:
            self.assertEqual(db.execute("SELECT state FROM download_request_state WHERE download_id=?", (sent[0]["download_id"],)).fetchone()[0], "completed")
            self.assertEqual(db.execute("SELECT state FROM download_request_state WHERE download_id=?", (sent[1]["download_id"],)).fetchone()[0], "cancelled")
        self.provider.episodes.append({"season": 1, "episode": 4})
        self.check_now()
        fourth = self.discoveries()[3]
        queue = {"active": [{"id": fourth["download_id"], "site": "example", "title": "Show", "season": 1, "episode": 4}], "scheduled": []}
        self.monitor.retries.cancel_pending(queue, fourth["download_id"])
        self.provider.instance_id = "engine-next-reboot"
        self.assertEqual(self.monitor.recover_startup()["interrupted"], 0)

    def test_retry_interrupted_by_another_reboot_recovers_but_uncertain_retry_does_not(self):
        self.follow(automatic=True)
        self.provider.instance_id = "engine-after-reboot"
        self.run_restarted_monitor()
        self.provider.instance_id = "engine-next-reboot"
        self.run_restarted_monitor()
        self.assertEqual(len([a for a, _ in self.provider.calls if a == "retry_download"]), 2)
        self.provider.instance_id = "engine-third-reboot"
        self.provider.fail = "retry_download"
        self.run_restarted_monitor()
        self.assertEqual(self.discoveries()[0]["state"], "uncertain")
        self.provider.instance_id = "engine-fourth-reboot"
        self.provider.fail = None
        self.run_restarted_monitor()
        self.assertEqual(len([a for a, _ in self.provider.calls if a == "retry_download"]), 3)

    def test_reboot_recovery_migrates_legacy_acknowledgements_without_guessing_unknown_sends(self):
        self.follow(automatic=True)
        with connect(self.database) as db:
            db.execute("UPDATE discoveries SET submitted_at=1")
            db.execute("DROP TABLE download_request_state")
        initialize(self.database)
        initialize(self.database)
        with connect(self.database) as db:
            self.assertEqual([r["name"] for r in db.execute("PRAGMA table_info(download_requests)")], ["root", "download_id", "body"])
            # The old release's insertion format must remain usable on rollback.
            db.execute("INSERT INTO download_requests VALUES(?,'legacy-unknown',?)", (str(self.library), '{"site":"example","payload":{"name":"Unknown"}}'))
        initialize(self.database)
        self.assertEqual(self.monitor.recover_startup()["interrupted"], 1)
        self.assertEqual(self.monitor.recover_startup()["interrupted"], 0)

    def test_reboot_recovers_saved_film_request_but_requires_review_for_episode_ranges(self):
        film = {"site": "example", "payload": {"name": "Film", "is_movie": True}}
        season = {"site": "example", "payload": self.payload, "season": "1", "episodes": "1-3"}
        self.monitor.retries.submit(self.provider, film)
        self.monitor.retries.submit(self.provider, season)
        self.provider.instance_id = "engine-after-reboot"
        self.run_restarted_monitor()
        self.assertEqual([b for a, b in self.provider.calls if a == "retry_download"], [film])

    def test_reboot_recovery_matches_pinned_movie_year_and_quality_format(self):
        film = {"site": "example", "payload": {"name": "Film", "year": 2024, "is_movie": True}}
        self.monitor.retries.submit(self.provider, film)
        self.video("Film (2024) [1080p].mkv")
        scan(self.database)
        self.provider.instance_id = "engine-after-reboot"
        self.assertEqual(self.monitor.recover_startup()["interrupted"], 0)
        newer = copy.deepcopy(film)
        newer["payload"]["year"] = 2025
        self.monitor.retries.submit(self.provider, newer)
        self.provider.instance_id = "engine-next-reboot"
        self.assertEqual(self.monitor.recover_startup()["interrupted"], 1, "a different year's film is not the same download")

    def test_startup_waits_for_initial_scan_and_failed_mount_blocks_submission(self):
        self.failed_downloads(1)
        with connect(self.database) as db:
            db.execute("INSERT INTO settings VALUES('scan_error','Library unavailable')")
        restarted = SeriesMonitor(self.database, self.provider)
        stop, scan_ready = threading.Event(), threading.Event()
        tick = restarted.tick
        previous_calls = copy.deepcopy(self.provider.calls)
        def first_tick():
            tick()
            stop.set()
        def wait(interval):
            if interval == .2:
                self.assertEqual(self.provider.calls, previous_calls, "no recovery reads or sends before the initial scan")
                scan_ready.set()
            return stop.is_set()
        with patch.object(restarted, "tick", side_effect=first_tick), patch.object(stop, "wait", side_effect=wait):
            restarted.run(stop, scan_ready)
        self.assertEqual(self.discoveries()[0]["state"], "retry_pending")
        self.assertFalse(any(a == "retry_download" for a, _ in self.provider.calls))
        with connect(self.database) as db:
            db.execute("DELETE FROM settings WHERE key='scan_error'")
        restarted.tick()
        self.assertEqual(len([a for a, _ in self.provider.calls if a == "retry_download"]), 1)

    def test_startup_recovers_after_queue_read_failure_without_duplicate_attempts(self):
        self.failed_downloads(1)
        self.provider.fail = "queue"
        restarted = SeriesMonitor(self.database, self.provider)
        stop = threading.Event()
        tick = restarted.tick
        def first_tick():
            tick()
            stop.set()
        def wait(interval):
            self.provider.fail = None
            return stop.is_set()
        with patch.object(restarted, "tick", side_effect=first_tick), patch.object(stop, "wait", side_effect=wait), self.assertLogs(level="ERROR"):
            restarted.run(stop)
        self.assertEqual(len([a for a, _ in self.provider.calls if a == "retry_download"]), 1)

    def retry_all(self):
        with patch.object(self.app.extensions["vibravid"], "call", return_value=copy.deepcopy(self.provider.queue)):
            return self.post("/admin/downloads/retry-failed", body={})

    def test_retry_all_is_durable_idempotent_and_precedes_new_downloads(self):
        self.failed_downloads()
        self.assertEqual(self.retry_all().json, {"queued": 2, "skipped": 0})
        self.assertEqual([r["state"] for r in self.discoveries()], ["retry_pending", "retry_pending"])
        self.assertEqual(self.retry_all().json, {"queued": 0, "skipped": 0})
        initialize(self.database)
        presented = self.monitor.retries.present(copy.deepcopy(self.provider.queue))
        self.assertEqual(presented["history"], [])
        self.assertEqual([r["episode"] for r in presented["retrying"]], [1, 2])
        self.provider.episodes.append({"season": 2, "episode": 1})
        self.check_now()
        actions = [(a, b["season"], b["episodes"]) for a, b in self.provider.calls if a in ("download", "retry_download")]
        self.assertEqual(actions[-3:], [("retry_download", "1", "1"), ("retry_download", "1", "2"), ("download", "2", "1")])
        self.assertEqual([r["state"] for r in self.discoveries()], ["submitted", "submitted", "submitted"])
        self.assertEqual(self.monitor.retries.present(copy.deepcopy(self.provider.queue))["history"], [])
        self.assertEqual(self.retry_all().json["queued"], 0)

    def test_priority_retry_window_is_bounded_and_does_not_wait_behind_normal_queue(self):
        self.failed_downloads(4)
        self.retry_all()
        with connect(self.database) as db:
            db.execute("UPDATE settings SET value='1' WHERE key IN ('download_limit','processing_limit')")
        self.provider.queue["scheduled"] = [{"id": "normal", "site": "other", "title": "Normal film"}]
        original = self.provider.call
        def call(action, body=None):
            result = original(action, body)
            if action == "retry_download":
                self.provider.queue["scheduled"].append({"id": result["download_id"], "site": body["site"],
                    "title": body["payload"]["name"], "season": body["season"], "episodes": body["episodes"], "priority": True})
            return result
        with patch.object(self.provider, "call", side_effect=call):
            self.monitor.tick()
            self.monitor.tick()
        self.assertEqual(len([a for a, _ in self.provider.calls if a == "retry_download"]), 1)
        self.assertEqual([r["state"] for r in self.discoveries()], ["submitted", "retry_pending", "retry_pending", "retry_pending"])
        presented = self.monitor.retries.present(copy.deepcopy(self.provider.queue))
        self.assertEqual([item["priority"] for item in presented["scheduled"]], [True, False])

    def test_bulk_retry_uncertain_send_is_not_repeated_and_new_failure_reappears(self):
        self.failed_downloads()
        self.retry_all()
        self.provider.fail = "retry_download"
        self.monitor.tick()
        self.assertEqual([r["state"] for r in self.discoveries()], ["uncertain", "retry_pending"])
        self.provider.fail = None
        self.monitor.tick()
        sends = [b["episodes"] for a, b in self.provider.calls if a == "retry_download"]
        self.assertEqual(sends, ["1", "2"])
        sent = self.discoveries()[1]
        failure = {"id": sent["download_id"], "site": "example", "title": "Show", "season": 1,
                   "episode": 2, "status": "failed", "error": "Another merge failure",
                   "start_time": sent["submitted_at"] + .01, "end_time": sent["submitted_at"] + .02}
        self.provider.queue["history"].append(failure)
        self.monitor.tick()
        presented = self.monitor.retries.present(copy.deepcopy(self.provider.queue))
        self.assertEqual([r["error"] for r in presented["history"]], ["Another merge failure"])
        self.provider.queue["history"] = []
        self.assertEqual(self.monitor.retries.present(copy.deepcopy(self.provider.queue))["history"][0]["error"], "Another merge failure")
        self.assertEqual([r["state"] for r in self.discoveries()], ["uncertain", "failed"])

    def test_retry_all_skips_local_watched_excluded_unlisted_and_unknown_requests(self):
        self.failed_downloads(4)
        self.video("Show.S01E01.mp4")
        self.video("Show.S01E02.mp4")
        scan(self.database)
        with connect(self.database) as db:
            db.execute("INSERT INTO progress(media_id,watched) SELECT id,1 FROM media WHERE episode=2")
            db.execute("UPDATE discoveries SET listed=0 WHERE episode=3")
        (self.library / "Show.S01E02.mp4").unlink()
        scan(self.database)
        self.provider.queue["history"].append({"id": "unknown", "site": "other", "title": "Unknown film", "status": "failed"})
        response = self.retry_all()
        self.assertEqual(response.json, {"queued": 1, "skipped": 3})
        self.post("/admin/season", {"series": "Show", "season": "1", "action": "exclude"})
        self.monitor.tick()
        self.assertFalse(any(a == "retry_download" for a, _ in self.provider.calls), "changed season rules win before submission")

    def test_available_downloads_resolve_old_failures_without_retrying(self):
        self.failed_downloads(2)
        self.monitor.reconcile_queue(self.provider.queue)
        video = self.video("Show.S01E01.mp4")
        scan(self.database)
        self.monitor.reconcile_queue(self.provider.queue)
        presented = self.monitor.retries.present(copy.deepcopy(self.provider.queue))
        self.assertEqual([job["episode"] for job in presented["history"]], [2])
        with patch.object(self.app.extensions["vibravid"], "call", return_value=copy.deepcopy(self.provider.queue)):
            self.assertEqual([job["episode"] for job in self.client.get("/admin/vibravid/queue").json["history"]], [2])
        with connect(self.database) as db:
            self.assertEqual(db.execute("SELECT state FROM download_request_state WHERE download_id=?",
                (self.provider.queue["history"][0]["id"],)).fetchone()[0], "completed")
        video.unlink()
        scan(self.database)
        self.assertEqual([job["episode"] for job in self.monitor.retries.present(copy.deepcopy(self.provider.queue))["history"]], [2],
                         "removing a resolved video must not resurrect its old failure")

    def test_failure_resolution_requires_exact_identity_and_an_existing_confined_file(self):
        self.video("Show.S01E01.mp4")
        scan(self.database)
        exact = {"id": "legacy", "site": "example", "title": "Show", "season": 1, "episode": 1, "status": "failed"}
        def visible(item):
            return self.monitor.retries.present({"history": [item]})["history"]
        self.assertEqual(visible(exact), [])
        self.assertTrue(visible({**exact, "episode": 2}))
        self.assertTrue(visible({**exact, "title": "Other"}))
        self.assertTrue(visible({**exact, "episode": None, "episodes": "1-3"}))
        (self.library / "Show.S01E01.mp4").unlink()
        self.assertTrue(visible(exact), "stale catalog availability must not hide failures")
        outside = self.base / "outside.mp4"
        outside.write_bytes(b"outside")
        (self.library / "Show.S01E01.mp4").symlink_to(outside)
        self.assertTrue(visible(exact), "files outside the configured root must not resolve failures")

    def test_failure_resolution_matches_film_requests_and_followed_aliases(self):
        self.follow(series="Library Show")
        self.video("Library Show.S01E01.mp4")
        film = self.video("Film (2025) [1080p].mp4")
        scan(self.database)
        body = {"site": "example", "payload": {"name": "Film", "year": 2025, "is_movie": True}}
        with connect(self.database) as db:
            import json
            db.execute("INSERT INTO download_requests VALUES(?,?,?)", (str(self.library), "film", json.dumps(body)))
        queue = {"history": [{"id": "episode", "site": "example", "title": "Show", "season": 1, "episode": 1, "status": "failed"},
                             {"id": "film", "title": "Film", "status": "failed"}]}
        self.assertEqual(self.monitor.retries.present(copy.deepcopy(queue))["history"], [])
        film.unlink()
        self.assertEqual([job["id"] for job in self.monitor.retries.present(copy.deepcopy(queue))["history"]], ["film"])
        other = self.base / "other"
        other.mkdir()
        self.post("/admin/settings", {"videos_path": str(other)})
        self.assertEqual(len(self.monitor.retries.present(copy.deepcopy(queue))["history"]), 2)

    def test_retry_all_respects_root_and_requires_csrf(self):
        self.post("/admin/settings", {"videos_path": str(self.library)})
        scan(self.database)
        self.failed_downloads(1)
        self.assertEqual(self.client.post("/admin/downloads/retry-failed", json={}).status_code, 403)
        self.retry_all()
        other = self.base / "other"
        other.mkdir()
        self.post("/admin/settings", {"videos_path": str(other)})
        self.monitor.tick()
        self.assertFalse(any(a == "retry_download" for a, _ in self.provider.calls))
        self.assertEqual(self.retry_all().json["queued"], 0)
        self.post("/admin/settings", {"videos_path": str(self.library)})
        scan(self.database)
        self.monitor.tick()
        self.assertEqual(len([a for a, _ in self.provider.calls if a == "retry_download"]), 1)

    def test_cancelling_series_also_stops_retries_waiting_in_sqlite(self):
        self.failed_downloads()
        self.retry_all()
        self.provider.queue["active"] = [{"id": "active-series", "site": "example", "title": "Show", "series_name": "Show"}]
        with patch.object(self.app.extensions["vibravid"], "call", side_effect=self.provider.call):
            response = self.post("/admin/vibravid/cancel", body={"download_id": "active-series"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.monitor.retries.pending())
        self.monitor.tick()
        self.assertFalse(any(a == "retry_download" for a, _ in self.provider.calls))
        self.assertEqual([r["state"] for r in self.discoveries()], ["dismissed", "dismissed"])

    def test_manual_film_request_can_be_bulk_retried_without_following(self):
        self.provider.calls.clear()
        request = {"site": "example", "payload": {"id": "film-1", "name": "Film", "is_movie": True, "type": "movie"}}
        with patch.object(self.app.extensions["vibravid"], "call", side_effect=self.provider.call):
            result = self.post("/admin/vibravid/download", body=request).json
        self.provider.queue["history"] = [{"id": result["download_id"], "site": "example", "title": "Film",
            "season": 0, "episode": 0, "status": "failed", "start_time": time.time(), "end_time": time.time(), "error": "Merge failed"}]
        self.assertEqual(self.retry_all().json, {"queued": 1, "skipped": 0})
        self.monitor.tick()
        self.assertEqual([b for a, b in self.provider.calls if a == "retry_download"], [request])
        self.assertEqual(self.monitor.retries.present(copy.deepcopy(self.provider.queue))["history"], [])

    def test_file_removal_archive_and_renamed_return_preserve_edits_and_progress(self):
        self.video("Show.S01E01.First.mp4")
        scan(self.database)
        with connect(self.database) as db:
            media_id = db.execute("SELECT id FROM media").fetchone()[0]
        self.post(f"/admin/media/{media_id}", {"title": "Custom title", "series": "Show", "season": "1", "episode": "1", "hidden": "on"})
        self.post("/admin/season", {"series": "Show", "season": "1", "action": "watched"})
        self.assertEqual(self.post(f"/admin/media/{media_id}/remove").status_code, 400)
        self.assertEqual(self.post(f"/admin/media/{media_id}/remove", {"confirm": "remove"}).status_code, 302)
        self.assertFalse((self.library / "Show.S01E01.First.mp4").exists())
        archive = self.client.get("/admin?section=removed").data
        self.assertIn(b"Custom title", archive)
        self.assertIn(b"Find to download again", archive)
        self.video("Season 1/Show.S01E01.New.Name.mkv")
        scan(self.database)
        with connect(self.database) as db:
            row = db.execute("SELECT * FROM media").fetchone()
            self.assertEqual((row["id"], row["title"], row["hidden"], row["available"]), (media_id, "Custom title", 1, 1))
            self.assertEqual(db.execute("SELECT watched FROM progress").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM media").fetchone()[0], 1)
        self.assertNotIn(b"Custom title", self.client.get("/admin?section=removed").data)

    def test_library_groups_episodes_under_one_series_and_season_controls(self):
        self.video("Show.S01E01.First.mp4")
        self.video("Show.S01E02.Second.mp4")
        self.video("Show.S02E01.Return.mp4")
        self.video("Film.mp4")
        scan(self.database)
        page = self.client.get("/admin?section=library").data.decode()
        self.assertEqual(page.count('<details class="catalog-series"'), 1)
        self.assertEqual(page.count('<section class="catalog-season">'), 2)
        self.assertEqual(page.count('<details class="catalog-item"'), 4)
        self.assertIn('Mark season watched &amp; exclude downloads', page)
        self.assertIn('name="confirm" value="remove" required', page)

    def test_alias_links_provider_output_to_selected_library_series(self):
        self.follow("My Show")
        self.post("/admin/season", {"series": "My Show", "season": "1", "action": "watched"})
        self.video("Show.S01E01.mp4")
        scan(self.database)
        with connect(self.database) as db:
            self.assertEqual(db.execute("SELECT series FROM media").fetchone()[0], "My Show")
            self.assertEqual(db.execute("SELECT watched FROM progress").fetchone()[0], 1)

    def test_csrf_input_validation_root_isolation_and_pause(self):
        self.assertEqual(self.client.post("/admin/follow", json={}).status_code, 403)
        self.assertEqual(self.post("/admin/follow", body={"site": "example", "payload": {}, "series": "Show"}).status_code, 400)
        self.follow()
        self.assertEqual(self.post("/admin/follow", body={"site": "example", "payload": self.payload, "series": "Other"}).status_code, 400)
        self.assertEqual(self.post("/admin/season", {"series": "Missing", "season": "1", "action": "watched"}).status_code, 404)
        self.post("/admin/following/1", {"mode": "paused"})
        count = len(self.provider.calls)
        self.check_now()
        self.assertEqual(len(self.provider.calls), count)
        other = self.base / "other"
        other.mkdir()
        self.post("/admin/settings", {"videos_path": str(other)})
        self.assertEqual(self.post("/admin/following/1", {"mode": "auto"}).status_code, 404)
        self.assertEqual(self.post("/admin/following/1/1/1", {"action": "download"}).status_code, 400)
        self.assertIn(b"No followed series", self.client.get("/admin?section=following").data)

    def test_season_changes_during_provider_read_win_and_failed_mount_blocks_auto(self):
        self.follow()
        self.provider.episodes.append({"season": 1, "episode": 2})
        self.post("/admin/following/1", {"mode": "auto"})
        original = self.provider.call
        def call(action, body=None):
            if action == "episodes":
                self.post("/admin/season", {"series": "Show", "season": "1", "action": "exclude"})
            return original(action, body)
        with patch.object(self.provider, "call", side_effect=call):
            self.monitor.tick()
        self.assertFalse(any(a == "download" for a, _ in self.provider.calls))
        self.post("/admin/season", {"series": "Show", "season": "1", "action": "include"})
        self.library.rename(self.base / "offline")
        self.monitor.tick()
        self.assertFalse(any(a == "download" for a, _ in self.provider.calls))

    def test_concurrent_downloads_submit_once_and_cannot_dismiss_inflight_request(self):
        self.follow()
        entered, release = threading.Event(), threading.Event()
        original = self.provider.call
        results = []
        def call(action, body=None):
            if action == "download":
                entered.set()
                release.wait(2)
            return original(action, body)
        def download():
            try:
                results.append(self.monitor.download(1, 1, 1))
            except ValueError:
                results.append(False)
        with patch.object(self.provider, "call", side_effect=call):
            first = threading.Thread(target=download)
            second = threading.Thread(target=download)
            first.start()
            try:
                self.assertTrue(entered.wait(1))
                second.start()
            finally:
                release.set()
                first.join(2)
                second.join(2)
        self.assertCountEqual(results, [True, False])
        self.assertEqual(len([a for a, _ in self.provider.calls if a == "download"]), 1)
        self.assertEqual(self.post("/admin/following/1/1/1", {"action": "dismiss"}).status_code, 404)

    def test_existing_database_migrates_without_losing_history_and_removal_is_confined(self):
        file = self.video("Film.mp4")
        scan(self.database)
        with connect(self.database) as db:
            media_id = db.execute("SELECT id FROM media").fetchone()[0]
            db.execute("INSERT INTO progress(media_id,position,duration,watched) VALUES(?,96,100,1)", (media_id,))
            for table in ("discoveries", "subscriptions", "season_state", "media_identity"):
                db.execute("DROP TABLE " + table)
            db.execute("PRAGMA user_version=1")
        initialize(self.database)
        scan(self.database)
        with connect(self.database) as db:
            self.assertEqual(tuple(db.execute("SELECT media_id,position,duration,watched FROM progress").fetchone()), (media_id, 96, 100, 1))
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 3)
        outside = self.base / "private.mp4"
        outside.write_bytes(b"private")
        file.unlink()
        file.symlink_to(outside)
        self.assertEqual(self.post(f"/admin/media/{media_id}/remove", {"confirm": "remove"}).status_code, 400)
        self.assertEqual(outside.read_bytes(), b"private")
        file.unlink()
        local = self.video("Other.mp4")
        file.symlink_to(local)
        self.assertEqual(self.post(f"/admin/media/{media_id}/remove", {"confirm": "remove"}).status_code, 400)
        self.assertTrue(local.exists())


if __name__ == "__main__":
    unittest.main()

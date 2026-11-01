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
from myflix.monitoring import INTERVAL, queued_episode
from myflix.vibravid import VibraVidError


class Provider:
    def __init__(self):
        self.episodes = [{"season": 1, "episode": 1, "title": "First"}]
        self.calls = []
        self.queue = {"active": [], "scheduled": []}
        self.fail = None

    def call(self, action, body=None):
        self.calls.append((action, copy.deepcopy(body)))
        if self.fail == action:
            raise VibraVidError("Provider unavailable; check queue")
        if action == "episodes":
            return {"episodes": copy.deepcopy(self.episodes)}
        if action == "queue":
            return copy.deepcopy(self.queue)
        return {"queued": True}


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
                         [(1, 1, "new"), (1, 2, "new"), (1, 3, "new"), (2, 1, "new")])
        initialize(self.database)
        page = self.client.get("/admin?section=following").data
        self.assertIn(b"New episodes", page)
        self.assertIn(b"Automatically download unwatched", page)
        with connect(self.database) as db:
            sub = db.execute("SELECT * FROM subscriptions").fetchone()
            self.assertGreaterEqual(sub["next_check"], sub["checked_at"] + INTERVAL)

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
            self.monitor.tick()
            self.assertEqual(len(self.provider.queue["scheduled"]), 5)
            self.monitor.tick()
            self.assertEqual(len(self.provider.queue["scheduled"]), 5)
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
        self.assertEqual(page.count('<details class="catalog-series">'), 1)
        self.assertEqual(page.count('<section class="catalog-season">'), 2)
        self.assertEqual(page.count('<details class="catalog-item">'), 4)
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

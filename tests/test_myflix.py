import errno
import os
import re
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from deploy.vibravid_publish import main as publish_hook, publish
from myflix.app import create_app
from myflix.catalog import identify, scan
from myflix.db import connect, settings


class MyFlixTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.library = self.base / "library"
        self.library.mkdir()
        self.database = str(self.base / "myflix.sqlite3")
        self.app = create_app({"TESTING": True, "DATABASE": self.database})
        self.client = self.app.test_client()
        self.client.get("/")
        self.csrf = self.token()

    def tearDown(self):
        self.temp.cleanup()

    def token(self):
        with self.client.session_transaction() as session:
            return session["csrf"]

    def post(self, path, data=None, body=None):
        if body is not None:
            return self.client.post(path, json=body, headers={"X-CSRF-Token": self.token()})
        return self.client.post(path, data={"csrf": self.token(), **(data or {})})

    def configure(self):
        self.assertEqual(self.post("/admin/settings", {"videos_path": str(self.library)}).status_code, 302)

    def video(self, name="Example.mp4", content=b"0123456789"):
        file = self.library / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(content)
        os.utime(file, (time.time() - 120, time.time() - 120))
        return file

    def item(self):
        with connect(self.database) as db:
            return dict(db.execute("SELECT * FROM media ORDER BY id LIMIT 1").fetchone())

    def test_media_advertises_ranges_and_serves_later_bytes(self):
        self.configure()
        self.video()
        self.post("/admin/scan")
        url = f'/media/{self.item()["id"]}'
        with self.client.get(url) as response:
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["Accept-Ranges"], "bytes")
        with self.client.get(url, headers={"Range": "bytes=7-"}) as response:
            self.assertEqual(response.status_code, 206)
            self.assertEqual(response.headers["Accept-Ranges"], "bytes")
            self.assertEqual(response.headers["Content-Range"], "bytes 7-9/10")
            self.assertEqual(response.data, b"789")

    def test_import_browse_range_resume_and_watch(self):
        self.configure()
        self.video("The.Show.S01E02.Arrival.mp4")
        self.assertEqual(self.post("/admin/scan").status_code, 302)
        item = self.item()
        self.assertEqual((item["series"], item["season"], item["episode"]), ("The Show", 1, 2))
        self.assertIn(b"The Show", self.client.get("/").data)
        self.assertIn(b"Arrival", self.client.get(f'/watch/{item["id"]}').data)
        response = self.client.get(f'/media/{item["id"]}', headers={"Range": "bytes=2-5"})
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.headers["Content-Range"], "bytes 2-5/10")
        self.assertEqual(response.data, b"2345")
        response.close()
        self.assertEqual(self.client.get(f'/media/{item["id"]}', headers={"Range": "bytes=90-100"}).status_code, 416)
        path = f'/api/progress/{item["id"]}'
        self.assertEqual(self.post(path, body={"position": 40, "duration": 100}).status_code, 200)
        self.assertIn(b'data-position="40.0"', self.client.get(f'/watch/{item["id"]}').data)
        self.assertIn(b"Arrival", self.client.get("/?view=continue").data)
        self.post(path, body={"position": 96, "duration": 100})
        self.assertNotIn(b"Arrival", self.client.get("/?view=continue").data)
        self.post(path, body={"position": 2, "duration": 100})
        self.assertIn(b"Watched", self.client.get("/").data)
        self.post(path, body={"position": 0, "duration": 100, "watched": False})
        self.assertNotIn(b"Watched", self.client.get("/").data)
        # Navigation beacon uses form encoding rather than JSON.
        self.assertEqual(self.post(path, {"position": "30", "duration": "100"}).status_code, 200)

    def test_library_seasons_and_collapsed_episode_watch_action(self):
        self.configure()
        for name in ("Show.S01E01.Start.mp4", "Show.S01E02.Next.mp4", "Show.S02E01.Return.mp4"):
            self.video(name)
        scan(self.database)
        media_id = self.item()["id"]
        url = f"/admin/media/{media_id}/watched"
        page = self.client.get("/admin?section=library").data.decode()
        self.assertEqual(page.count('<details class="catalog-season">'), 2)
        self.assertIn('<span>Season 1</span><span>2 episodes</span>', page)
        self.assertIn('<span>Season 2</span><span>1 episode</span>', page)
        summaries = re.findall(r'<details class="catalog-item">\s*<summary>(.*?)</summary>', page, re.S)
        self.assertEqual(len(summaries), 3)
        for summary in summaries:
            self.assertLess(summary.index('data-watch-url='), summary.index('data-watch-state'))
            self.assertIn('Mark as watched</button>', summary)
        for tv_url in ("/", f"/series/{media_id}", f"/watch/{media_id}"):
            self.assertNotIn('Episode options', self.client.get(tv_url).data.decode())
        self.assertEqual(self.post(url, body={"watched": True}).json,
                         {"position": 0, "duration": 0, "watched": 1})
        self.post(f"/api/progress/{media_id}", body={"position": 30, "duration": 100})
        self.assertEqual(self.post(url, body={"watched": True}).json,
                         {"position": 30, "duration": 100, "watched": 1})
        page = self.client.get(f"/series/{media_id}").data.decode()
        self.assertIn('value="100" max="100"', page)
        self.assertNotIn('class="tile-badge"', page)
        self.assertIn('Mark as unwatched</button>', self.client.get("/admin?section=library").data.decode())
        self.assertEqual(self.post(url, body={"watched": False}).json,
                         {"position": 0, "duration": 100, "watched": 0})
        self.assertNotIn('<progress ', self.client.get(f"/series/{media_id}").data.decode())
        for body in ({}, {"watched": 1}, {"watched": "false"}, [], {"watched": None}):
            self.assertEqual(self.post(url, body=body).status_code, 400)
        self.assertEqual(self.client.post(url, json={"watched": True}).status_code, 403)
        # Library management also covers hidden and missing episodes.
        with connect(self.database) as db:
            db.execute("UPDATE media SET hidden=1,available=0 WHERE id=?", (media_id,))
        self.assertEqual(self.post(url, body={"watched": True}).status_code, 200)
        self.assertIn('Mark as unwatched</button>', self.client.get("/admin?section=removed").data.decode())
        self.assertEqual(self.post("/admin/media/99999/watched", body={"watched": True}).status_code, 404)
        (self.base / "other-library").mkdir()
        self.assertEqual(self.post("/admin/settings", {"videos_path": str(self.base / "other-library")}).status_code, 302)
        self.assertEqual(self.post(url, body={"watched": True}).status_code, 404)

    def test_edit_and_progress_survive_missing_and_rescan(self):
        self.configure()
        file = self.video()
        scan(self.database)
        item = self.item()
        self.post(f'/api/progress/{item["id"]}', body={"position": 30, "duration": 100})
        self.post(f'/admin/media/{item["id"]}', {"title": "Corrected", "series": "", "hidden": "on"})
        scan(self.database)
        self.assertEqual(self.item()["title"], "Corrected")
        self.assertEqual(self.client.get(f'/media/{item["id"]}').status_code, 404)
        file.unlink()
        scan(self.database)
        self.assertEqual(self.item()["available"], 0)
        self.video()
        scan(self.database)
        self.assertEqual(self.item()["id"], item["id"])
        with connect(self.database) as db:
            self.assertEqual(db.execute("SELECT position FROM progress").fetchone()[0], 30)

    def test_admin_password_free_csrf_validation_and_root_switch(self):
        page = self.client.get("/admin")
        self.assertEqual(page.status_code, 200)
        self.assertNotIn(b"Sign out", page.data)
        self.assertNotIn("ADMIN_PASSWORD", self.app.config)
        self.assertEqual(self.client.post("/admin/scan").status_code, 403)
        self.assertEqual(self.client.post("/admin/scan", data={"csrf": "☃"}).status_code, 403)
        other_client = self.app.test_client()
        self.assertEqual(other_client.get("/admin").status_code, 200)
        self.assertEqual(other_client.post("/admin/scan", data={"csrf": self.token()}).status_code, 403)
        self.configure()
        self.assertEqual(self.post("/admin/settings", {"videos_path": "/does-not-exist"}).status_code, 400)
        # Old external-link settings are ignored and removed on save.
        self.assertEqual(self.post("/admin/settings", {"videos_path": str(self.library), "vibravid_url": "javascript:alert(1)"}).status_code, 302)
        self.assertNotIn(b"VibraVid Web URL", self.client.get("/admin").data)
        self.video()
        scan(self.database)
        media_id = self.item()["id"]
        for body in ({"position": -1, "duration": 100}, {"position": 101, "duration": 100},
                     {"position": True, "duration": 100}, {"position": 0, "duration": 100, "watched": "false"},
                     {"position": float("nan"), "duration": 100}):
            self.assertEqual(self.post(f"/api/progress/{media_id}", body=body).status_code, 400)
        other = self.base / "other"
        other.mkdir()
        self.post("/admin/settings", {"videos_path": str(other)})
        self.assertEqual(self.client.get(f"/media/{media_id}").status_code, 404)

    def test_symlink_escape_fresh_files_and_failed_scan(self):
        self.configure()
        self.video()
        self.video("fresh.mp4").touch()
        self.video("hidden.part")
        outside = self.base / "private.mp4"
        outside.write_bytes(b"secret")
        (self.library / "escape.mp4").symlink_to(outside)
        self.assertEqual(scan(self.database), 1)
        item = self.item()
        (self.library / "Example.mp4").unlink()
        (self.library / "Example.mp4").symlink_to(outside)
        self.assertEqual(self.client.get(f'/media/{item["id"]}').status_code, 404)
        self.library.rename(self.base / "offline")
        self.assertEqual(self.post("/admin/scan").status_code, 400)
        self.assertEqual(self.item()["available"], 1)
        with connect(self.database) as db:
            self.assertIn("unavailable", settings(db)["scan_error"])

    def test_filters_and_next_episode(self):
        self.configure()
        for name in ("Show.S01E01.mp4", "Show.S01E02.mp4", "Film.mp4"):
            self.video(name)
        scan(self.database)
        with connect(self.database) as db:
            first = db.execute("SELECT id FROM media WHERE episode=1").fetchone()[0]
        self.assertIn(b"Next episode", self.client.get(f"/watch/{first}").data)
        self.assertNotIn(b"EPISODES", self.client.get("/?view=movies").data)
        self.assertNotIn(b"Ready to play", self.client.get("/?q=no-match").data)
        self.assertEqual(self.client.get("/admin").status_code, 200)
        self.assertEqual(self.client.get("/health").json, {"status": "ok"})

    def test_home_has_continue_not_watched_series_and_film_shelves(self):
        self.configure()
        for name in ("Show.S01E01.mp4", "Show.S01E02.mp4", "Film.mp4"):
            self.video(name)
        scan(self.database)
        with connect(self.database) as db:
            first = db.execute("SELECT id FROM media WHERE episode=1").fetchone()[0]
        self.post(f"/api/progress/{first}", body={"position": 30, "duration": 100})
        home = self.client.get("/").data.decode()
        self.assertEqual(re.findall(r"<h2>(.*?)</h2>", home), ["Continue watching", "Not watched", "Series", "Film"])
        self.assertNotIn('aria-label="Browse"', home)
        self.assertNotIn("Recently added", home)
        self.assertEqual(home.count(f'href="/series/{first}" data-card'), 1)
        search = self.client.get("/?q=Show").data.decode()
        self.assertEqual(re.findall(r"<h2>(.*?)</h2>", search), ["Continue watching", "Series"])

    def test_continuing_series_is_excluded_from_not_watched(self):
        self.configure()
        for name in ("Show.S01E01.mp4", "Show.S01E02.mp4", "Other.S01E01.mp4"):
            self.video(name)
        scan(self.database)
        with connect(self.database) as db:
            ids = {row["path"]: row["id"] for row in db.execute("SELECT id,path FROM media")}
        first, second = ids["Show.S01E01.mp4"], ids["Show.S01E02.mp4"]
        other = ids["Other.S01E01.mp4"]
        # The continuing episode differs from the series card's representative.
        self.post(f"/api/progress/{second}", body={"position": 30, "duration": 100})
        for url in ("/", "/?q=Show", "/?view=series"):
            with self.subTest(url=url):
                home = self.client.get(url).data.decode()
                continuing = re.search(r'<h2>Continue watching</h2>(.*?)</section>', home, re.S)
                self.assertIn(f'data-media-id="{second}"', continuing[1])
                unwatched = re.search(r'<h2>Not watched</h2>(.*?)</section>', home, re.S)
                if url == "/?q=Show":
                    self.assertIsNone(unwatched)
                else:
                    self.assertEqual(re.findall(r'data-media-id="(\d+)"', unwatched[1]), [str(other)])

        self.post(f"/api/progress/{second}", body={"position": 100, "duration": 100})
        home = self.client.get("/?q=Show").data.decode()
        self.assertNotIn("<h2>Continue watching</h2>", home)
        unwatched = re.search(r'<h2>Not watched</h2>(.*?)</section>', home, re.S)
        self.assertEqual(re.findall(r'data-media-id="(\d+)"', unwatched[1]), [str(first)])

    def test_all_watched_thumbnails_show_progress_and_continue_keeps_details(self):
        self.configure()
        for name in ("Show.S01E01.Start.mp4", "Show.S01E02.Next.mp4", "Film.mp4", "New.Film.mp4"):
            self.video(name)
        (self.library / "poster.jpg").write_bytes(b"local-jpeg-fixture")
        scan(self.database)
        with connect(self.database) as db:
            episode = db.execute("SELECT id FROM media WHERE episode=1").fetchone()[0]
            film = db.execute("SELECT id FROM media WHERE title='Film'").fetchone()[0]
        self.post(f"/api/progress/{episode}", body={"position": 30, "duration": 100})
        self.post(f"/api/progress/{film}", body={"position": 100, "duration": 100})

        for url in ("/", f"/series/{episode}"):
            home = self.client.get(url).data.decode()
            shelves = re.findall(r'<h2>(.*?)</h2>(.*?)</section>', home, re.S)
            self.assertTrue(shelves)
            for label, shelf in shelves:
                with self.subTest(url=url, shelf=label):
                    self.assertIn('<img src="/art/', shelf)
                    self.assertIn('<div class="tile-caption"><strong>', shelf)
                    cards = re.findall(r'<a class="tile\b(.*?)</a>', shelf, re.S)
                    self.assertTrue(cards)
                    for card in cards:
                        media_id = int(re.search(r'data-media-id="(\d+)"', card)[1])
                        if media_id in (episode, film):
                            picture = card.split('<div class="tile-caption">')[0]
                            self.assertIn('<progress ', picture)
                            self.assertIn('value="100" max="100"' if media_id == film
                                          else 'value="30.0" max="100.0"', picture)
                        else:
                            self.assertNotIn('<progress ', card)
                    if label == "Continue watching":
                        self.assertIn('class="tile-badge">Resume', shelf)
                        self.assertIn('<progress ', shelf)
                        self.assertIn('<span>Episode</span>', shelf)
                    else:
                        for decoration in ('class="tile-badge"', 'class="tile-logo"',
                                           'class="tile-play"', '</strong><span>'):
                            self.assertNotIn(decoration, shelf)
        detail = self.client.get(f"/series/{episode}").data.decode()
        self.assertIn('S1:E1 · Start</strong>', detail)
        self.assertIn('S1:E2 · Next</strong>', detail)

        # Marking watched without a known duration still renders a full line.
        self.post(f"/api/progress/{film}", body={"position": 0, "duration": 0, "watched": True})
        home = self.client.get("/?view=movies").data.decode()
        self.assertIn('<progress value="100" max="100"', home)

    def test_not_watched_shelf_tracks_completion_and_available_series(self):
        self.configure()
        for name in ("Show.S01E01.mp4", "Show.S01E02.mp4", "Film.mp4",
                     "Hidden.mp4", "Missing.mp4"):
            self.video(name)
        scan(self.database)
        with connect(self.database) as db:
            first = db.execute("SELECT id FROM media WHERE episode=1").fetchone()[0]
            second = db.execute("SELECT id FROM media WHERE episode=2").fetchone()[0]
            film = db.execute("SELECT id FROM media WHERE title='Film'").fetchone()[0]
            db.execute("UPDATE media SET hidden=1 WHERE title='Hidden'")
            db.execute("UPDATE media SET available=0 WHERE title='Missing'")

        def unwatched_ids(url="/"):
            home = self.client.get(url).data.decode()
            shelf = re.search(r'<h2>Not watched</h2>(.*?)</section>', home, re.S)
            return re.findall(r'data-media-id="(\d+)"', shelf[1]) if shelf else []

        self.assertCountEqual(unwatched_ids(), [str(film), str(first)])
        self.post(f"/api/progress/{first}", body={"position": 96, "duration": 100})
        self.post(f"/api/progress/{film}", body={"position": 30, "duration": 100})
        self.assertCountEqual(unwatched_ids(), [str(film), str(second)])
        self.assertEqual(unwatched_ids("/?q=Show"), [str(second)])
        self.assertEqual(unwatched_ids("/?view=movies"), [str(film)])
        self.assertEqual(unwatched_ids("/?view=series"), [str(second)])
        self.assertEqual(unwatched_ids("/?view=continue"), [])
        for media_id in (film, second):
            self.post(f"/api/progress/{media_id}", body={"position": 100, "duration": 100})
        self.assertNotIn(b"<h2>Not watched</h2>", self.client.get("/").data)
        self.post(f"/api/progress/{film}", body={"position": 0, "duration": 100, "watched": False})
        self.assertEqual(unwatched_ids(), [str(film)])
        other = self.base / "other"
        other.mkdir()
        self.post("/admin/settings", {"videos_path": str(other)})
        self.assertEqual(unwatched_ids(), [])

    def test_continue_watching_shows_last_played_episode_per_series(self):
        self.configure()
        for name in ("Show.S01E01.mp4", "Show.S01E02.mp4", "Other.S01E01.mp4", "Film.mp4", "Another.Film.mp4"):
            self.video(name)
        scan(self.database)
        with connect(self.database) as db:
            ids = {row["path"]: row["id"] for row in db.execute("SELECT id,path FROM media")}
        first, second = ids["Show.S01E01.mp4"], ids["Show.S01E02.mp4"]
        other, film, another = ids["Other.S01E01.mp4"], ids["Film.mp4"], ids["Another.Film.mp4"]
        # Watch the lower-numbered episode last within the same second.
        with patch("myflix.app.datetime") as clock:
            for index, media_id in enumerate((second, other, film, another, first)):
                clock.now.return_value = datetime(2026, 11, 1, 12, 0, 0, index + 1, tzinfo=timezone.utc)
                self.post(f"/api/progress/{media_id}", body={"position": 30, "duration": 100})

        def continuing(url):
            home = self.client.get(url).data.decode()
            shelf = re.search(r'<h2>Continue watching</h2>(.*?)</section>', home, re.S)
            return re.findall(r'data-media-id="(\d+)"', shelf[1]) if shelf else []

        self.assertEqual(continuing("/"), list(map(str, (first, another, film, other))))
        self.assertEqual(continuing("/?view=continue"), list(map(str, (first, another, film, other))))
        self.assertEqual(continuing("/?q=Show"), [str(first)])
        self.assertEqual(continuing("/?view=series"), list(map(str, (first, other))))
        self.assertEqual(continuing("/?view=movies"), list(map(str, (another, film))))
        with connect(self.database) as db:
            self.assertEqual(db.execute("SELECT position FROM progress WHERE media_id=?", (second,)).fetchone()[0], 30)
            db.execute("UPDATE media SET hidden=1 WHERE id=?", (first,))
        self.assertEqual(continuing("/?q=Show"), [str(second)], "hidden episodes stay out of Continue watching")
        with connect(self.database) as db:
            db.execute("UPDATE media SET available=0 WHERE id=?", (second,))
        self.assertEqual(continuing("/?q=Show"), [])

    def test_admin_sections_search_and_action_destinations(self):
        self.configure()
        self.video("Show.S01E01.Start.mp4")
        self.video("Film.mp4")
        scan(self.database)
        downloads = self.client.get("/admin").data
        self.assertIn(b'class="tv-admin"', downloads)
        self.assertIn(b"Current downloads", downloads)
        self.assertNotIn(b'Recent history', downloads)
        self.assertNotIn(b'id="videos-path"', downloads)
        self.assertNotIn(b'class="catalog-item"', downloads)
        self.assertEqual(self.client.get("/admin?section=unknown").data, downloads)
        settings_page = self.client.get("/admin?section=settings").data
        self.assertIn(b'id="videos-path"', settings_page)
        self.assertNotIn(b'downloads.js', settings_page)
        library = self.client.get("/admin?section=library&q=Show").data
        self.assertIn(b"Start", library)
        self.assertNotIn(b'value="Film"', library)
        self.assertIn(b'value="Show"', library)
        self.assertNotIn(b'id="download-search"', library)
        self.assertIn("section=settings", self.post("/admin/settings", {"videos_path": str(self.library)}).location)
        self.assertIn("section=library", self.post("/admin/scan").location)
        item = self.item()
        response = self.post(f'/admin/media/{item["id"]}', {"title": "Updated"})
        self.assertIn("section=library", response.location)

    def test_episode_playback_contract_orders_seasons_and_excludes_hidden_items(self):
        self.configure()
        for name in ("Show.S01E01.Start.mp4", "Show.S01E02.Hidden.mp4", "Show.S02E01.Return.mp4", "Film.mp4"):
            self.video(name)
        scan(self.database)
        with connect(self.database) as db:
            first = db.execute("SELECT id FROM media WHERE season=1 AND episode=1").fetchone()[0]
            hidden = db.execute("SELECT id FROM media WHERE episode=2").fetchone()[0]
            last = db.execute("SELECT id FROM media WHERE season=2").fetchone()[0]
            film = db.execute("SELECT id FROM media WHERE series=''").fetchone()[0]
            db.execute("UPDATE media SET hidden=1 WHERE id=?", (hidden,))
        page = self.client.get(f"/watch/{first}").data
        self.assertIn(b'id="track-settings"', page)
        self.assertIn(b'aria-controls="track-panel"', page)
        self.assertIn(b'id="track-panel"', page)
        self.assertIn(b'aria-labelledby="track-heading" hidden', page)
        self.assertNotIn(b'id="watched"', page)
        self.assertNotIn(b'Mark watched', page)
        self.assertIn(f'data-next-id="{last}"'.encode(), page)
        details = self.client.get(f"/api/playback/{first}").json
        self.assertEqual(details["next"]["id"], last)
        self.assertEqual(details["next"]["season"], 2)
        self.assertEqual(details["next"]["watch_url"], f"/watch/{last}")
        self.assertNotIn("root", details)
        self.assertNotIn("path", details)
        self.assertEqual(details["stream_url"], f"/media/{first}")
        self.assertIsNone(self.client.get(f"/api/playback/{last}").json["next"])
        self.assertIsNone(self.client.get(f"/api/playback/{film}").json["next"])
        self.assertEqual(self.client.get(f"/api/playback/{hidden}").status_code, 404)
        self.post(f"/api/progress/{last}", body={"position": 50, "duration": 100})
        self.assertEqual(self.client.get(f"/api/playback/{last}").json["position"], 50)
        self.post(f"/api/progress/{last}", body={"position": 100, "duration": 100})
        self.assertEqual(self.client.get(f"/api/playback/{last}").json["position"], 0)

    def test_episode_inference(self):
        self.assertEqual(identify(Path("Show/Season 2/S02E03.mp4")), ("Episode 3", "Show", 2, 3))
        self.assertEqual(identify(Path("Show.2x03.Title.mp4")), ("Title", "Show", 2, 3))

    def test_tv_series_shelves_artwork_and_hidden_access(self):
        self.configure()
        self.video("Show/Season 1/Show.S01E01.Start.mp4")
        self.video("Show/Season 1/Show.S01E02.Next.mp4")
        poster = self.library / "Show" / "poster.jpg"
        poster.write_bytes(b"local-jpeg-fixture")
        scan(self.database)
        with connect(self.database) as db:
            item = dict(db.execute("SELECT * FROM media WHERE episode=1").fetchone())
        home = self.client.get("/").data
        self.assertIn(b'data-rail', home)
        self.assertNotIn(b'<footer>', home)
        self.assertIn(f'/series/{item["id"]}'.encode(), home)
        detail = self.client.get(f'/series/{item["id"]}').data
        self.assertIn(b"Season 1", detail)
        self.assertIn(b"Start", detail)
        self.assertIn(b"Next", detail)
        response = self.client.get(f'/art/{item["id"]}/poster')
        self.assertEqual(response.data, b"local-jpeg-fixture")
        response.close()
        poster.unlink()
        outside = self.base / "secret.jpg"
        outside.write_bytes(b"private")
        poster.symlink_to(outside)
        self.assertEqual(self.client.get(f'/art/{item["id"]}/poster').status_code, 404)
        self.post(f'/admin/media/{item["id"]}', {"title": "Start", "series": "Show", "season": "1", "episode": "1", "hidden": "on"})
        self.assertEqual(self.client.get(f'/series/{item["id"]}').status_code, 404)
        self.assertEqual(self.client.get(f'/art/{item["id"]}/poster').status_code, 404)

    def test_completion_hook_is_atomic_confined_and_non_destructive(self):
        downloads = self.base / "downloads"
        downloads.mkdir()
        source = downloads / "Show" / "Show.S01E01.mp4"
        source.parent.mkdir()
        source.write_bytes(b"completed")
        target = publish(source, downloads, self.library)
        self.assertEqual(target.read_bytes(), b"completed")

        self.assertTrue(source.exists())
        self.assertFalse(os.path.samefile(source, target))
        source.write_bytes(b"staging changed")
        self.assertEqual(target.read_bytes(), b"completed")
        with self.assertRaises(FileExistsError):
            publish(source, downloads, self.library)
        source2 = downloads / "Movie.mp4"
        source2.write_bytes(b"second")
        self.assertEqual(publish(source2, downloads, self.library).read_bytes(), b"second")
        with self.assertRaises(ValueError):
            publish(target, downloads, self.library)
        with self.assertRaises(ValueError):
            publish(source, downloads, downloads)
        source.unlink()
        source.write_bytes(b"replacement")
        with self.assertRaises(FileExistsError):
            publish(source, downloads, self.library)
        self.assertEqual(target.read_bytes(), b"completed")

    def test_hook_rejects_symlinked_destination_before_creating_directories(self):
        downloads = self.base / "downloads"
        source = downloads / "Show" / "Season 1" / "episode.mp4"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"complete")
        outside = self.base / "outside"
        outside.mkdir()
        (self.library / "Show").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            publish(source, downloads, self.library)
        self.assertFalse((outside / "Season 1").exists())

    @unittest.skipUnless(sys.platform == "linux", "Linux no-replace rename")
    def test_completion_hook_without_hard_links_never_overwrites(self):
        downloads = self.base / "downloads"
        downloads.mkdir()
        source = downloads / "episode.mp4"
        source.write_bytes(b"complete")
        for code in (errno.EPERM, errno.EOPNOTSUPP):
            with self.subTest(errno=code):
                target = self.library / source.name
                with patch("deploy.vibravid_publish.os.link", side_effect=AssertionError("Must not create hard links")) as link:
                    self.assertEqual(publish(source, downloads, self.library), target)
                    self.assertEqual(target.read_bytes(), b"complete")
                    self.assertEqual(source.read_bytes(), b"complete")
                    with self.assertRaises(FileExistsError):
                        publish(source, downloads, self.library)
                    link.assert_not_called()
                self.assertEqual(target.read_bytes(), b"complete")
                self.assertEqual(list(self.library.glob(".myflix-*")), [])
                target.unlink()

    @unittest.skipUnless(sys.platform == "linux", "Linux no-replace rename")
    def test_completion_hook_rename_race_preserves_existing_video(self):
        from deploy.vibravid_publish import _rename_no_replace
        downloads = self.base / "downloads"
        downloads.mkdir()
        source = downloads / "episode.mp4"
        source.write_bytes(b"new")
        target = self.library / source.name

        def competing_publish(temporary, destination):
            self.assertEqual(Path(temporary).read_bytes(), b"new")
            target.write_bytes(b"existing")
            _rename_no_replace(temporary, destination)

        with patch("deploy.vibravid_publish._rename_no_replace", side_effect=competing_publish):
            with self.assertRaises(FileExistsError):
                publish(source, downloads, self.library)
        self.assertEqual(target.read_bytes(), b"existing")
        self.assertEqual(source.read_bytes(), b"new")
        self.assertEqual(list(self.library.glob(".myflix-*")), [])

    def test_hook_publishes_local_artwork_after_success_only(self):
        downloads = self.base / "downloads"
        source = downloads / "Show" / "Season 1" / "Show.S01E01.mp4"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"video")
        (downloads / "Show" / "poster.jpg").write_bytes(b"show-poster")
        (source.parent / "Show.S01E01-thumb.jpg").write_bytes(b"episode-art")
        args = ["hook", "--downloads", str(downloads), "--library", str(self.library)]
        with patch("sys.argv", args), patch.dict(os.environ, {"SC_DOWNLOAD_SUCCESS": "0", "SC_DOWNLOAD_PATH": str(source)}):
            publish_hook()
        self.assertFalse((self.library / "Show").exists())
        with patch("sys.argv", args), patch.dict(os.environ, {"SC_DOWNLOAD_SUCCESS": "1", "SC_DOWNLOAD_PATH": str(source)}), patch("builtins.print"):
            publish_hook()
        self.assertEqual((self.library / "Show" / "poster.jpg").read_bytes(), b"show-poster")
        self.assertEqual((self.library / "Show" / "Season 1" / "Show.S01E01-thumb.jpg").read_bytes(), b"episode-art")


if __name__ == "__main__":
    unittest.main()

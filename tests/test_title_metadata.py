import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from myflix.app import create_app
from myflix.artwork import identity
from myflix.catalog import scan
from myflix.credits import Cancelled
from myflix.db import connect, initialize
from myflix.engine_api import provider_metadata
from myflix.metadata import MetadataCache, match_source, source_item, title_details
from myflix.vibravid import VibraVidError


class ProviderMetadataTest(unittest.TestCase):
    def test_raw_provider_fields_and_availability_are_distinct(self):
        media = SimpleNamespace(raw_data={"overview": "A story", "genres": [{"name": "Drama"}],
                                          "cast": ["Actor"], "desc": "Rent for €4"},
                                year=2024, is_movie=True)
        details = provider_metadata(SimpleNamespace(site_name="example"), media, [])
        self.assertEqual(details, {"synopsis": "A story", "genres": ["Drama"], "cast": ["Actor"], "year": "2024"})
        self.assertEqual(title_details({"desc": "Subscription required"})["synopsis"], "")
        self.assertEqual(title_details({"cast": "bad", "genres": [None, {"url": "private"}]})["cast"], [])

    def test_only_requested_available_seasons_are_loaded(self):
        episode = SimpleNamespace(number=2, name="Arrival", description="An unexpected visitor")
        season = SimpleNamespace(number=1, name="Season one", description="The beginning")
        scraper = SimpleNamespace(title_info={"plot": "A series"},
                                  seasons_manager=SimpleNamespace(seasons=[season]),
                                  getNumberSeason=Mock(), getEpisodeSeasons=Mock(return_value=[episode]))
        api = SimpleNamespace(site_name="example", _build_scraper=Mock(return_value=scraper))
        details = provider_metadata(api, SimpleNamespace(raw_data={}, year=None, is_movie=False), [1, 99])
        scraper.getEpisodeSeasons.assert_called_once_with(1)
        self.assertEqual(details["synopsis"], "A series")
        self.assertEqual(details["seasons"]["1"]["episodes"]["2"]["synopsis"], "An unexpected visitor")
        self.assertNotIn("99", details["seasons"])

    def test_provider_episode_mapping_retains_raw_descriptions(self):
        season = SimpleNamespace(number=1, name="One")
        scraper = SimpleNamespace(seasons_manager=SimpleNamespace(seasons=[season]), getNumberSeason=Mock(),
            getEpisodeSeasons=Mock(return_value=[{"episodeNumber": 4, "title": "Four", "description": "The fourth"}]))
        api = SimpleNamespace(site_name="example", _build_scraper=Mock(return_value=scraper),
            _map_episode=lambda episode, index: SimpleNamespace(number=episode["episodeNumber"], name=episode["title"]))
        result = provider_metadata(api, SimpleNamespace(raw_data={}, year=None, is_movie=False), [1])
        self.assertEqual(result["seasons"]["1"]["episodes"]["4"], {"title": "Four", "synopsis": "The fourth"})


class TitleMetadataTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.database = str(self.base / "myflix.sqlite3")
        self.app = create_app({"TESTING": True, "DATABASE": self.database})
        self.cache = self.app.extensions["metadata"]
        self.engine = Mock()
        self.cache.engine = self.engine
        self.stop = threading.Event()
        self.library = self.base / "library"
        self.write("Film.2024.mp4")
        self.write("Show.S01E01.mp4")
        self.write("Show.S01E02.mp4")
        scan(self.database)
        self.payloads = {"Film": {"name": "Film", "year": 2024, "type": "Film", "is_movie": True},
                         "Show": {"name": "Show", "type": "Serie", "is_movie": False}}
        with connect(self.database) as db:
            for name, payload in self.payloads.items():
                db.execute("INSERT INTO download_requests VALUES(?,?,?)", (str(self.library), name,
                           json.dumps({"site": "example", "payload": payload})))
        self.engine.call.side_effect = self.response
        self.probe = patch.object(self.cache, "probe", return_value=3600).start()
        self.addCleanup(patch.stopall)

    def write(self, name):
        path = self.library / name
        path.write_bytes(b"video")
        os.utime(path, (time.time() - 120,) * 2)
        return path

    def items(self):
        with connect(self.database) as db:
            return [dict(row) for row in db.execute("SELECT * FROM media ORDER BY path")]

    def response(self, action, body):
        self.assertEqual(action, "metadata")  # Durable source identity avoids another search.
        self.assertEqual(body["site"], "example")
        details = {"synopsis": "A <script>story</script>", "year": "2024", "genres": ["Drama"], "cast": ["Actor"]}
        if body["payload"]["name"] == "Show":
            self.assertEqual(body["seasons"], [1])
            details["seasons"] = {"1": {"synopsis": "A season", "episodes": {
                "1": {"title": "Pilot", "synopsis": "An arrival"},
                "2": {"title": "Departure", "synopsis": "A farewell"}}}}
        return {"details": details}

    def test_details_survive_restart_rescan_and_history_cleanup_and_render_offline(self):
        self.cache.update(self.stop)
        self.assertEqual(self.engine.call.call_count, 2)
        self.assertEqual(self.probe.call_count, 3)
        initialize(self.database)
        with connect(self.database) as db:
            db.execute("DELETE FROM download_requests")
        scan(self.database)
        restarted = MetadataCache(self.database, self.engine)
        with patch.object(restarted, "probe", side_effect=AssertionError("Already probed")):
            restarted.update(self.stop)
        self.assertEqual(self.engine.call.call_count, 2)
        items = self.cache.decorate(self.items())
        film, episode = items[0], items[1]
        self.assertEqual(film["runtime_minutes"], 60)
        self.assertEqual(episode["display_title"], "Pilot")
        self.assertEqual(episode["episode_description"], "An arrival")
        with patch.object(self.engine, "call", side_effect=AssertionError("HTTP must stay offline")):
            client = self.app.test_client()
            home = client.get("/")
            self.assertIn(f'/title/{film["id"]}'.encode(), home.data)
            movie = client.get(f'/title/{film["id"]}')
            self.assertEqual(movie.status_code, 200)
            for expected in (b"60 min", b"Drama", b"Actor", b"2024", b"&lt;script&gt;"):
                self.assertIn(expected, movie.data)
            self.assertNotIn(b"<script>story", movie.data)
            series = client.get(f'/series/{episode["id"]}')
            for expected in (b"Pilot", b"An arrival", b"A season", b"Departure"):
                self.assertIn(expected, series.data)
            self.assertNotIn(b"example", movie.data)
            self.assertEqual(client.get(f'/title/{episode["id"]}').status_code, 302)

    def test_admin_edits_progress_and_root_confinement_are_preserved(self):
        episode = self.items()[1]
        client = self.app.test_client()
        client.get("/")
        with client.session_transaction() as session:
            token = session["csrf"]
        self.assertEqual(client.post(f'/admin/media/{episode["id"]}', data={"csrf": token,
            "title": "My edited title", "series": "My renamed show", "season": "1", "episode": "1"}).status_code, 302)
        with connect(self.database) as db:
            db.execute("INSERT INTO progress(media_id,position,duration) VALUES(?,30,3600)", (episode["id"],))
        self.cache.update(self.stop)
        scan(self.database)
        item = self.cache.decorate(self.items())[1]
        self.assertEqual((item["title"], item["series"], item["display_title"]),
                         ("My edited title", "My renamed show", "My edited title"))
        home = client.get("/").data
        self.assertIn(f'href="/watch/{episode["id"]}"'.encode(), home)
        with connect(self.database) as db:
            self.assertEqual(db.execute("SELECT position FROM progress").fetchone()[0], 30)
            db.execute("UPDATE media SET hidden=1 WHERE id=?", (episode["id"],))
        self.assertEqual(client.get(f'/series/{episode["id"]}').status_code, 404)
        film = self.items()[0]
        with connect(self.database) as db:
            db.execute("UPDATE settings SET value=? WHERE key='videos_path'", (str(self.base / "other"),))
        self.assertEqual(client.get(f'/title/{film["id"]}').status_code, 404)

    def test_provider_failure_keeps_cached_details_and_backs_off(self):
        self.cache.update(self.stop)
        with connect(self.database) as db:
            db.execute("UPDATE title_metadata SET retry_at=0")
        self.engine.call.side_effect = VibraVidError("Unavailable")
        self.cache.update(self.stop)
        self.engine.call.reset_mock()
        self.cache.update(self.stop)
        self.engine.call.assert_not_called()
        self.assertEqual(self.cache.decorate(self.items())[0]["metadata"]["genres"], ["Drama"])

    def test_failed_runtime_retries_and_file_replacement_invalidates_runtime(self):
        self.probe.side_effect = RuntimeError("Bad file")
        self.cache.update(self.stop)
        self.assertIsNone(self.cache.decorate(self.items())[0]["runtime_minutes"])
        self.probe.reset_mock()
        self.cache.update(self.stop)
        self.probe.assert_not_called()
        self.probe.side_effect = None
        self.probe.return_value = 5400
        (self.library / "Film.2024.mp4").write_bytes(b"replacement video")
        self.cache.update(self.stop)
        self.probe.assert_called_once()
        self.assertEqual(self.cache.decorate(self.items())[0]["runtime_minutes"], 90)

    def test_unknown_and_ambiguous_titles_are_not_guessed(self):
        with connect(self.database) as db:
            db.execute("DELETE FROM download_requests")
        duplicate = {"site": "one", "payload": self.payloads["Film"]}
        self.engine.call.return_value = {"results": [duplicate, dict(duplicate, site="two")]}
        self.engine.call.side_effect = None
        self.cache.update(self.stop)
        self.assertTrue(all(call.args[0] == "search" for call in self.engine.call.call_args_list))
        self.assertFalse(self.cache.decorate(self.items())[0]["metadata"].get("synopsis"))
        film = source_item(self.items()[0])
        self.assertFalse(match_source({"site": "one", "payload": dict(self.payloads["Film"], year=1984)}, film))

    def test_shutdown_and_missing_files_do_not_run_tools(self):
        self.stop.set()
        with self.assertRaises(Cancelled):
            self.cache.update(self.stop)
        self.engine.call.assert_not_called()
        self.stop.clear()
        self.cache.update(self.stop)
        for path in self.library.iterdir():
            path.unlink()
        self.probe.reset_mock()
        self.cache.update(self.stop)
        self.probe.assert_not_called()
        self.assertEqual(self.cache.decorate(self.items())[0]["runtime_minutes"], 60)

    def test_new_source_and_new_season_bypass_negative_cache(self):
        with connect(self.database) as db:
            db.execute("DELETE FROM download_requests")
        self.engine.call.side_effect = None
        self.engine.call.return_value = {"results": []}
        self.cache.update(self.stop)
        self.engine.call.reset_mock()
        with connect(self.database) as db:
            db.execute("INSERT INTO download_requests VALUES(?,?,?)", (str(self.library), "Show", json.dumps(
                {"site": "example", "payload": self.payloads["Show"]})))
        self.engine.call.side_effect = self.response
        self.cache.update(self.stop)
        self.engine.call.assert_called_once()
        self.engine.call.reset_mock()
        self.write("Show.S02E01.mp4")
        scan(self.database)
        self.engine.call.side_effect = None
        self.engine.call.return_value = {"details": {}}
        self.cache.update(self.stop)
        self.engine.call.assert_called_once()
        self.assertEqual(self.engine.call.call_args.args[1]["seasons"], [1, 2])
        self.engine.call.reset_mock()
        self.cache.update(self.stop)
        self.engine.call.assert_not_called()

    def test_replaced_symlink_and_inflight_root_changes_discard_runtime(self):
        self.cache.update(self.stop)
        path = self.library / "Film.2024.mp4"
        path.unlink()
        outside = self.base / "private.mp4"
        outside.write_bytes(b"private")
        path.symlink_to(outside)
        self.probe.reset_mock()
        self.cache.update(self.stop)
        self.probe.assert_not_called()
        path.unlink()
        path.write_bytes(b"changed video")

        def change_root(*args):
            with connect(self.database) as db:
                db.execute("UPDATE settings SET value=? WHERE key='videos_path'", (str(self.base / "other"),))
            return 5400

        self.probe.side_effect = change_root
        self.cache.update(self.stop)
        self.assertEqual(self.cache.decorate(self.items())[0]["runtime_minutes"], 60)

    def test_native_probe_validates_duration_and_uses_bounded_command(self):
        cache = MetadataCache(self.database, self.engine)
        for value in ("nan", "inf", "-1", "0", "999999999"):
            with patch("myflix.metadata.shutil.which", return_value="/usr/bin/ffprobe"), \
                    patch("myflix.metadata.command", return_value=json.dumps({"format": {"duration": value}})):
                with self.assertRaises(ValueError):
                    cache.probe(self.library / "Film.2024.mp4", self.stop)
        with patch("myflix.metadata.shutil.which", return_value="/usr/bin/ffprobe"), \
                patch("myflix.metadata.command", return_value='{"format":{"duration":"3600"}}') as run:
            self.assertEqual(cache.probe(self.library / "Film.2024.mp4", self.stop), 3600)
            self.assertEqual(run.call_args.kwargs["timeout"], 15)


if __name__ == "__main__":
    unittest.main()

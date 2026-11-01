import json
import tempfile
import threading
import unittest
from html.parser import HTMLParser
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from myflix.app import create_app
from myflix.db import connect
from myflix.discovery import FEEDS, image, normalize, parse_feed


def entry(identity=1, **fields):
    return dict(ids={"simkl_id": identity}, title=f"Title {identity}",
                poster="12/abc123", fanart="34/def456", trailer="abcdefghijk", **fields)


class DiscoveryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = str(Path(self.temp.name) / "test.sqlite3")
        self.app = create_app({"TESTING": True, "DATABASE": self.database})
        self.cache = self.app.extensions["discovery"]
        self.today = datetime.now(timezone.utc).date()

    def tearDown(self):
        self.temp.cleanup()

    def test_rankings_preserve_watcher_order_and_exclude_cinema_only_films(self):
        released = self.today.strftime("%m/%d/%Y")
        future = (self.today + timedelta(days=1)).strftime("%m/%d/%Y")
        data = [entry(9, dvd_date=released), entry(2), entry(3, dvd_date=future), entry(1, dvd_date=released)]
        self.assertEqual([item["id"] for item in parse_feed("popular_movie", data)], [9, 1])
        self.assertEqual([item["id"] for item in parse_feed("popular_tv", data)], [9, 2, 3, 1])

    def test_calendar_only_shows_upcoming_season_premieres(self):
        past = (self.today - timedelta(days=1)).isoformat()
        future = (self.today + timedelta(days=2)).isoformat()
        data = {"calendar": [
            {"simkl_id": 1, "date": past, "episode": {"season": 1, "episode": 1}},
            {"simkl_id": 1, "date": future, "episode": {"season": 2, "episode": 1}},
            {"simkl_id": 2, "date": future, "episode": {"season": 1, "episode": 3}},
            {"simkl_id": 3, "date": future}],
            "metadata": {"1": entry(1), "2": entry(2), "3": entry(3, dvd_date=future)}}
        items = parse_feed("upcoming_tv", data)
        self.assertEqual([(item["id"], item["release"]) for item in items], [(1, future)])

    def test_removed_home_release_feed_is_not_fetched_or_shown_from_old_cache(self):
        self.assertNotIn("upcoming_movie", FEEDS)
        with connect(self.database) as db:
            db.execute("INSERT INTO discovery_feeds(name,payload,error) VALUES('upcoming_movie',?,1)",
                       (json.dumps([dict(normalize(entry(42), "movie"), release="2099-01-01")]),))
        self.assertNotIn("upcoming_movie", self.cache.read())
        home = self.app.test_client().get('/')
        self.assertNotIn(b'Upcoming home releases', home.data)
        self.assertNotIn(b'discover-movie-42', home.data)
        self.assertNotIn(b'Discovery updates unavailable', home.data)

    def test_provider_values_cannot_inject_hosts_trailer_urls_or_markup(self):
        self.assertIsNone(image("https://127.0.0.1/admin", "posters"))
        self.assertIsNone(image("../../secrets", "posters"))
        raw = entry()
        raw["trailer"] = 'bad" onload="alert(1)'
        self.assertEqual(normalize(raw, "tv")["trailer"], "")
        raw["ids"] = []
        self.assertIsNone(normalize(raw, "tv"))

    def test_cache_refresh_has_no_credentials_and_preserves_last_good_data(self):
        def feed(path):
            return [entry()] if "trending" in path else {"calendar": [], "metadata": {}}
        with patch.object(self.cache, "fetch", side_effect=feed) as fetch:
            self.cache.update(threading.Event())
            self.assertEqual(fetch.call_count, len(FEEDS))
            self.assertTrue(all("?" not in call.args[0] for call in fetch.call_args_list))
            self.cache.update(threading.Event())
            self.assertEqual(fetch.call_count, len(FEEDS), "fresh feeds are not fetched again")
        saved = self.cache.read()["popular_tv"]["items"]
        with connect(self.database) as db:
            db.execute("UPDATE discovery_feeds SET next_check=0")
        with patch.object(self.cache, "fetch", side_effect=ValueError("invalid JSON")):
            self.cache.update(threading.Event())
        self.assertEqual(self.cache.read()["popular_tv"]["items"], saved)
        self.assertTrue(self.cache.read()["popular_tv"]["error"])

    def test_home_and_details_use_cached_data_without_network_or_library_entries(self):
        items = parse_feed("popular_tv", [entry(7, overview="A story")])
        items[0]["title"] = '<script>alert("x")</script>'
        with connect(self.database) as db:
            db.execute("INSERT INTO discovery_feeds(name,payload,updated) VALUES(?,?,1)", ("popular_tv", json.dumps(items)))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM media").fetchone()[0], 0)
        with patch.object(self.cache, "fetch", side_effect=AssertionError("request must not fetch")):
            client = self.app.test_client()
            home = client.get("/")
            self.assertEqual(home.status_code, 200)
            self.assertIn(b'data-media-id="discover-tv-7"', home.data)
            self.assertIn(b'&lt;script&gt;', home.data)
            class Frames(HTMLParser):
                def handle_starttag(self, tag, attrs):
                    if tag == "iframe":
                        self.frames.append(dict(attrs))
            parser = Frames()
            parser.frames = []
            parser.feed(home.get_data(as_text=True))
            self.assertEqual(len(parser.frames), 1, "first iframe must exist before JavaScript runs")
            frame = parser.frames[0]
            self.assertEqual(frame["loading"], "eager")
            self.assertEqual(frame["id"], "preview-frame")
            source = urlsplit(frame["src"])
            self.assertEqual(source.netloc, "www.youtube-nocookie.com")
            self.assertEqual(source.path, "/embed/abcdefghijk")
            self.assertEqual(parse_qs(source.query)["autoplay"], ["0"])
            self.assertLess(home.data.index(b'<iframe'), home.data.index(b'id="discovery-preview"'),
                            "warm iframe must not live inside the hidden preview panel")
            self.assertIn("frame-src https://www.youtube-nocookie.com", home.headers["Content-Security-Policy"])
            self.assertNotIn(b'data-discovery', client.get('/?q=search').data)
            self.assertNotIn(b'<iframe', client.get('/?q=search').data)
            self.assertNotIn(b'data-discovery', client.get('/?view=continue').data)
            detail = client.get('/discover/tv/7')
            self.assertEqual(detail.status_code, 200)
            self.assertIn(b'Find in Downloads', detail.data)
            self.assertIn(b'https://simkl.com/tv/7/', detail.data)
            self.assertEqual(client.get('/discover/tv/999').status_code, 404)
            self.assertNotIn('youtube', client.get('/admin').headers['Content-Security-Policy'])


if __name__ == "__main__":
    unittest.main()

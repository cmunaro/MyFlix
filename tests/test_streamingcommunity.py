import copy
import html
import io
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image

from myflix.app import create_app
from myflix.db import connect
from myflix.discovery import FEEDS as SIMKL_FEEDS, normalize as simkl_item
from myflix.streamingcommunity import FEEDS, fetch, normalize


BASE = "https://streamingcommunity.example"


def title(identity=1, kind="movie"):
    return {"id": identity, "name": f"Title {identity}", "slug": f"title-{identity}", "type": kind,
            "images": [{"type": "cover", "filename": "cover.webp"},
                       {"type": "background", "filename": "background.webp"}],
            "translations": [{"locale": "it", "key": "plot", "value": "Una storia"}]}


class ProviderTest(unittest.TestCase):
    def client(self, titles, *, broken_details=False, slider="top10"):
        def get(url, **kwargs):
            self.assertFalse(kwargs["allow_redirects"])
            if "/titles/" in url:
                if broken_details:
                    raise TimeoutError()
                props = {"title": {"trailers": [{"youtube_id": "abcdefghijk"}]}}
            else:
                props = {"sliders": [{"name": slider, "titles": titles}]}
            content = '<div id="app" data-page="' + html.escape(json.dumps({"props": props}), quote=True) + '"></div>'
            return SimpleNamespace(status_code=200, text=content, content=content.encode(), raise_for_status=lambda: None)
        client = Mock()
        client.get.side_effect = get
        client.__enter__ = Mock(return_value=client)
        client.__exit__ = Mock(return_value=False)
        return client

    def test_exact_provider_pages_order_limits_and_trailers(self):
        for name, (page, slider, limit) in FEEDS.items():
            with self.subTest(name=name):
                kind = "tv" if name == "sc_top_tv" else "movie"
                rows = [title(50, kind), title(50, kind)] + [title(i, kind) for i in range(1, 31)]
                client = self.client(rows, slider=slider)
                module = SimpleNamespace(create_client=Mock(return_value=client), get_userAgent=lambda: "test")
                with patch.dict(sys.modules, {"VibraVid.utils.http_client": module}):
                    items = fetch(SimpleNamespace(base_url=BASE + "/"), name)
                self.assertEqual(client.get.call_args_list[0].args[0], BASE + "/it" + ("/" + page if page else ""))
                self.assertEqual(len(items), limit)
                self.assertEqual([item["id"] for item in items[:3]], [50, 1, 2])
                self.assertEqual(items[0]["trailer"], "abcdefghijk")
                self.assertEqual(items[0]["overview"], "Una storia")
                self.assertEqual(items[0]["source_url"], BASE + "/it/titles/50-title-50")

    def test_missing_trailers_keep_cards_and_missing_shelf_fails(self):
        client = self.client([title()], broken_details=True)
        module = SimpleNamespace(create_client=Mock(return_value=client), get_userAgent=lambda: "test")
        with patch.dict(sys.modules, {"VibraVid.utils.http_client": module}):
            self.assertEqual(fetch(SimpleNamespace(base_url=BASE), "sc_top_movie")[0]["trailer"], "")
            with self.assertRaises(ValueError):
                fetch(SimpleNamespace(base_url=BASE), "sc_trending")

    def test_home_canonical_redirect_stays_on_provider_origin(self):
        client = self.client([title()], slider="trending")
        get = client.get.side_effect
        client.get.side_effect = lambda url, **kwargs: (
            SimpleNamespace(status_code=301, headers={"Location": BASE})
            if url == BASE + "/it" else get(url, **kwargs))
        module = SimpleNamespace(create_client=Mock(return_value=client), get_userAgent=lambda: "test")
        with patch.dict(sys.modules, {"VibraVid.utils.http_client": module}):
            self.assertEqual(len(fetch(SimpleNamespace(base_url=BASE), "sc_trending")), 1)
            client.get.side_effect = lambda *args, **kwargs: SimpleNamespace(
                status_code=302, headers={"Location": "http://127.0.0.1/admin"})
            with self.assertRaisesRegex(ValueError, "left provider origin"):
                fetch(SimpleNamespace(base_url=BASE), "sc_trending")

    def test_provider_fields_cannot_inject_image_hosts_paths_or_trailers(self):
        raw = title()
        raw["images"][0]["filename"] = "https://127.0.0.1/secret"
        raw["images"][1]["filename"] = "../secret.jpg"
        raw["trailers"] = [{"youtube_id": 'bad" onload="alert(1)'}]
        item = normalize(raw, BASE)
        self.assertIsNone(item["poster"])
        self.assertIsNone(item["backdrop"])
        self.assertEqual(item["trailer"], "")
        for fields in ({"id": True}, {"slug": "../admin"}, {"type": "unknown"}):
            self.assertIsNone(normalize(dict(title(), **fields), BASE))


class StreamingCommunityTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = str(Path(self.temp.name) / "test.sqlite3")
        self.app = create_app({"TESTING": True, "DATABASE": self.database})
        self.cache = self.app.extensions["discovery"]
        self.engine = self.app.extensions["vibravid"]

    def save(self, name, items):
        with connect(self.database) as db:
            db.execute("INSERT OR REPLACE INTO discovery_feeds(name,payload,updated) VALUES(?,?,1)",
                       (name, json.dumps(items)))

    def test_cached_shelves_details_filters_and_provider_id_collisions(self):
        movie, tv = normalize(title(), BASE), normalize(title(2, "tv"), BASE)
        movie["title"] = "<Movie>"
        movie["trailer"] = "abcdefghijk"
        self.save("sc_top_movie", [movie])
        self.save("sc_top_tv", [tv])
        self.save("sc_trending", [movie, tv])
        self.save("popular_movie", [simkl_item({"ids": {"simkl_id": 1}, "title": "SIMKL film"}, "movie")])
        with patch.object(self.engine, "call", side_effect=AssertionError("No network in handlers")):
            client = self.app.test_client()
            home = client.get("/")
            for label in (b'Top 10 film', b'Top 10 serie TV', b'Titoli del momento'):
                self.assertIn(label, home.data)
            self.assertIn(b'data-media-id="discover-streamingcommunity-movie-1"', home.data)
            self.assertIn(b'/discover/streamingcommunity/movie/1', home.data)
            self.assertIn(b'data-trailer="abcdefghijk"', home.data)
            self.assertIn(b'&lt;Movie&gt;', home.data)
            self.assertNotIn(b'discover-streamingcommunity-tv-', client.get('/?view=movies').data)
            self.assertNotIn(b'discover-streamingcommunity-movie-', client.get('/?view=series').data)
            self.assertNotIn(b'data-discovery', client.get('/?q=Title').data)
            self.assertNotIn(b'data-discovery', client.get('/?view=continue').data)
            detail = client.get('/discover/streamingcommunity/movie/1')
            self.assertIn(b'Find in Downloads', detail.data)
            self.assertIn(b'Watch trailer', detail.data)
            self.assertIn((BASE + '/it/titles/1-title-1').encode(), detail.data)
            self.assertNotIn(b'Data from <a href="https://simkl.com', detail.data)
            self.assertIn(b'SIMKL film', client.get('/discover/movie/1').data)
            self.assertEqual(client.get('/discover/streamingcommunity/movie/999').status_code, 404)
        with connect(self.database) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM media").fetchone()[0], 0)

    def test_background_jpeg_cache_daily_refresh_and_failure_retention(self):
        artwork = self.app.extensions["artwork"]
        output = io.BytesIO()
        Image.new("RGB", (20, 30), "red").save(output, "WEBP")
        def response(action, body):
            self.assertEqual(action, "discovery")
            kind = "tv" if body["feed"] == "sc_top_tv" else "movie"
            return {"items": [normalize(title(1, kind), BASE)]}
        with connect(self.database) as db:
            for name in SIMKL_FEEDS:
                db.execute("INSERT INTO discovery_feeds(name,next_check) VALUES(?,9999999999)", (name,))
        with patch.object(self.engine, "call", side_effect=response) as call, patch.object(artwork, "read", return_value=output.getvalue()) as read:
            self.cache.update(threading.Event())
            self.assertEqual(call.call_count, 3)
            self.assertEqual(read.call_count, 2, "shared images download once")
            self.cache.update(threading.Event())
            self.assertEqual(call.call_count, 3)
        saved = copy.deepcopy(self.cache.read()["sc_top_movie"]["items"])
        image_url = saved[0]["poster"]
        self.assertTrue(image_url.startswith("/discovery-artwork/"))
        with patch.object(artwork, "read", side_effect=AssertionError("No request-time fetch")):
            result = self.app.test_client().get(image_url)
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.mimetype, "image/jpeg")
            self.assertTrue(result.data.startswith(b'\xff\xd8'))
            result.close()
            self.assertEqual(self.app.test_client().get('/discovery-artwork/invalid.jpg').status_code, 404)
        with connect(self.database) as db:
            db.execute("UPDATE discovery_feeds SET next_check=0 WHERE name LIKE 'sc_%'")
        with patch.object(self.engine, "call", side_effect=TimeoutError()):
            self.cache.update(threading.Event())
        feed = self.cache.read()["sc_top_movie"]
        self.assertEqual(feed["items"], saved)
        self.assertTrue(feed["error"])
        self.assertIn(b'Discovery updates unavailable', self.app.test_client().get('/').data)


if __name__ == "__main__":
    unittest.main()

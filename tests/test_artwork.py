import io
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from myflix.app import create_app
from PIL import Image

from myflix.artwork import ArtworkCache, ArtworkError, identity
from myflix.catalog import scan
from myflix.db import connect, initialize, settings


def image_bytes(format="JPEG"):
    output = io.BytesIO()
    Image.new("RGB", (20, 30), "red").save(output, format)
    return output.getvalue()


JPEG = image_bytes()


class ArtworkTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.database = str(self.base / "myflix.sqlite3")
        self.app = create_app({"TESTING": True, "DATABASE": self.database})
        self.cache = self.app.extensions["artwork"]
        self.engine = Mock()
        self.engine.call.side_effect = self.engine_response
        self.cache.engine = self.engine
        self.stop = threading.Event()
        self.library = self.base / "library"
        for name in ("Dune.2021.1080p.WEB-DL.mp4", "Show/Season 1/Show.S01E01.mp4", "Show/Season 1/Show.S01E02.mp4"):
            file = self.library / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(b"video")
            os.utime(file, (time.time() - 120,) * 2)
        scan(self.database)
        with connect(self.database) as db:
            self.items = [dict(row) for row in db.execute("SELECT * FROM media ORDER BY id")]
        self.film = next(row for row in self.items if not row["series"])
        self.episodes = [row for row in self.items if row["series"]]

    def result(self, movie=True, title="Dune", year="2021"):
        return {"title": title, "year": year, "is_movie": movie,
                "payload": {"name": title, "poster": "https://cdn.example.com/poster.webp", "year": year}}

    def engine_response(self, action, body=None):
        if action == "queue":
            return {"history": [{"title": "Show", "type": "TV", "poster": "https://cdn.example.com/show.webp"}]}
        if action == "search":
            return {"results": [self.result()]}
        raise AssertionError("Artwork must never mutate engine downloads")

    def response(self, url, limit):
        return JPEG

    def test_background_cache_shared_by_series_served_offline_and_private(self):
        with patch.object(self.cache, "read", side_effect=self.response) as read:
            self.cache.update(self.stop)
            self.assertEqual(read.call_count, 2)  # Two titles, not three videos.
        self.assertEqual(identity(self.episodes[0]), identity(self.episodes[1]))
        with patch.object(self.cache, "read", side_effect=AssertionError("HTTP handlers must stay offline")):
            client = self.app.test_client()
            home = client.get("/")
            self.assertEqual(home.status_code, 200)
            for item in self.items:
                for kind in ("poster",):
                    response = client.get(f'/art/{item["id"]}/{kind}')
                    self.assertEqual(response.status_code, 200)
                    with Image.open(io.BytesIO(response.data)) as image:
                        self.assertEqual(image.format, "JPEG")
                        self.assertEqual(image.size, (20, 30))
                    response.close()
            page = client.get("/admin")
            self.assertIn(b"No account or API key is needed", page.data)
            self.assertNotIn("TMDB_API_KEY", self.app.config)
            self.assertNotIn(b"Wikipedia", page.data)
            self.assertNotIn(b"upload.wikimedia.org", home.data)
            restarted = ArtworkCache(self.database, self.engine)
            with patch.object(restarted, "read", side_effect=AssertionError("Cache must survive restarts")):
                restarted.update(self.stop)
            with connect(self.database) as db:
                db.execute("UPDATE media SET hidden=1 WHERE id=?", (self.film["id"],))
            self.assertEqual(client.get(f'/art/{self.film["id"]}/poster').status_code, 404)
            with connect(self.database) as db:
                db.execute("UPDATE settings SET value=? WHERE key='videos_path'", (str(self.base / "other"),))
            self.assertEqual(client.get(f'/art/{self.episodes[0]["id"]}/poster').status_code, 404)

    def test_local_artwork_preserved_and_cache_confined(self):
        for kind, suffix in (("poster", "poster"), ("backdrop", "fanart")):
            file = self.library / (Path(self.film["path"]).stem + f"-{suffix}.jpg")
            file.write_bytes(b"local-art")
        with patch.object(self.cache, "read", side_effect=self.response) as read:
            self.cache.update(self.stop)
            self.assertEqual(read.call_count, 1)  # Only the series needs lookup.
        self.assertEqual(self.cache.path(self.film, "poster").read_bytes(), b"local-art")
        key = identity(self.episodes[0])[0]
        cached = self.cache.directory / f"{key}-poster.jpg"
        cached.unlink()
        outside = self.base / "private.jpg"
        outside.write_bytes(b"private")
        cached.symlink_to(outside)
        self.assertIsNone(self.cache.path(self.episodes[0], "poster"))

    def test_matching_uses_type_year_and_rejects_ambiguity(self):
        key, media_type, title, year = identity(self.film)
        self.assertEqual((media_type, title, year), ("movie", "Dune", "2021"))
        self.assertIsNotNone(self.cache.lookup(media_type, title, year, []))
        self.assertEqual(self.engine.call.call_args.args, ("search", {"query": "Dune", "site": "__all__"}))
        for results in ([self.result(year="1984")], [self.result(title="Dune 2")],
                        [self.result(year="1984"), self.result(year="2021")], [self.result(movie=False)]):
            self.assertIsNone(self.cache.match(results, "movie", "Dune", "" if len(results) == 2 else "2021"))
        history = [{"title": "I Simpson - S01 E1-3", "type": "TV", "poster": "https://cdn.example.com/simpson.webp"}]
        self.engine.reset_mock()
        self.assertEqual(self.cache.lookup("tv", "I Simpson", "", history), history[0]["poster"])
        self.engine.call.assert_not_called()
        edited = dict(self.film, title="Arrival (2016)")
        self.assertNotEqual(identity(edited)[0], key)

    def test_stopped_worker_and_negative_cache_make_no_calls(self):
        stopped = threading.Event()
        stopped.set()
        with patch.object(self.cache, "read", side_effect=AssertionError("Unexpected lookup")):
            self.cache.update(stopped)
        with patch.object(self.cache, "lookup", return_value=None) as read:
            self.cache.update(self.stop)
            self.assertEqual(read.call_count, 2)
            initialize(self.database)  # Additive schema creation must retain retry state.
            self.cache.update(self.stop)
            self.assertEqual(read.call_count, 2)
        with connect(self.database) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM artwork").fetchone()[0], 2)

    def test_errors_back_off_and_do_not_block_browsing(self):
        with patch.object(self.cache, "read", side_effect=ArtworkError("Provider artwork is unavailable.")) as read:
            self.cache.update(self.stop)
            self.cache.update(self.stop)
            self.assertEqual(read.call_count, 1)
        with connect(self.database) as db:
            self.assertGreater(int(settings(db)["artwork_retry_at"]), time.time())
        client = self.app.test_client()
        self.assertEqual(client.get("/").status_code, 200)
        self.assertIn(b"Provider artwork is unavailable.", client.get("/admin").data)
        self.assertEqual(client.get(f'/art/{self.film["id"]}/poster').status_code, 404)

    def test_batch_limits_and_stop_between_downloads(self):
        with connect(self.database) as db:
            for number in range(15):
                db.execute("INSERT INTO media(root,path,title,size) VALUES(?,?,?,?)",
                           (str(self.library), f"Extra{number}.mp4", f"Extra{number}", 5))
        with patch.object(self.cache, "lookup", return_value=None) as read:
            self.cache.update(self.stop)
            self.assertEqual(read.call_count, 10)
            self.cache.update(self.stop)
            self.assertEqual(read.call_count, 17)
        with connect(self.database) as db:
            db.execute("DELETE FROM artwork")
        with patch.object(self.cache, "lookup", side_effect=lambda *args: self.stop.set()) as lookup:
            self.cache.update(self.stop)
            self.assertEqual(lookup.call_count, 1)

    def test_download_validation_atomicity_and_network_boundary(self):
        key = identity(self.film)[0]
        image_url = "https://cdn.example.com/poster.webp"
        with patch.object(self.cache, "read", return_value=b"<html>not an image</html>"):
            with self.assertRaises(ArtworkError):
                self.cache.download(key, image_url)
        self.assertIsNone(self.cache.cached(key))
        with patch.object(self.cache, "read", return_value=JPEG):
            self.cache.download(key, image_url)
        self.assertEqual(list(self.cache.directory.glob(".art-*")), [])
        for url in ("file:///etc/passwd", "https://user:pass@example.com/image", "https://example.com:8080/a"):
            with self.assertRaises(ArtworkError), patch("myflix.artwork.socket.getaddrinfo") as resolve:
                self.cache.read(url, 100)
            resolve.assert_not_called()
        for address in ("127.0.0.1", "192.168.1.5", "169.254.169.254", "::1", "::ffff:127.0.0.1"):
            with patch("myflix.artwork.socket.getaddrinfo", return_value=[(2, 1, 6, "", (address, 443))]), patch("myflix.artwork.socket.create_connection") as connect:
                with self.assertRaises(ArtworkError):
                    self.cache.read(image_url, 100)
                connect.assert_not_called()
        with patch("myflix.artwork.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("1.1.1.1", 443))]), patch("myflix.artwork.socket.create_connection") as connect, patch("myflix.artwork.ssl.create_default_context") as tls, patch("myflix.artwork.http.client.HTTPSConnection") as client:
            response = client.return_value.getresponse.return_value
            response.status = 200
            response.read.return_value = JPEG
            self.assertEqual(self.cache.read(image_url, len(JPEG)), JPEG)
            connect.assert_called_once_with(("1.1.1.1", 443), timeout=5)
            self.assertEqual(tls.return_value.wrap_socket.call_args.kwargs["server_hostname"], "cdn.example.com")
            response.read.return_value = b"too large"
            with self.assertRaises(ArtworkError):
                self.cache.read(image_url, 1)
            response.status = 302
            with self.assertRaises(ArtworkError):
                self.cache.read(image_url, 100)

    def test_webp_and_png_covers_become_jpeg_and_local_backdrop_survives(self):
        key = identity(self.film)[0]
        for format in ("PNG", "WEBP"):
            with patch.object(self.cache, "read", return_value=image_bytes(format)):
                self.cache.download(key, "https://cdn.example.com/poster")
            with Image.open(self.cache.cached(key)) as image:
                self.assertEqual(image.format, "JPEG")
                self.assertEqual(image.size, (20, 30))
        client = self.app.test_client()
        response = client.get(f'/art/{self.film["id"]}/poster')
        self.assertEqual(response.mimetype, "image/jpeg")
        response.close()
        self.assertEqual(client.get(f'/art/{self.film["id"]}/backdrop').status_code, 404)
        (self.library / "fanart.jpg").write_bytes(JPEG)
        response = client.get(f'/art/{self.film["id"]}/backdrop')
        self.assertEqual(response.data, JPEG)
        response.close()


if __name__ == "__main__":
    unittest.main()

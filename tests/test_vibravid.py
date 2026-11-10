import json
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import URLError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from myflix.app import create_app
from myflix.vibravid import VibraVid, VibraVidError


class Engine(BaseHTTPRequestHandler):
    calls = []
    failure = None

    def log_message(self, *args):
        pass

    def handle_api(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"null")
        type(self).calls.append((self.command, self.path, self.headers.get("X-VibraVid-Token"), body))
        if self.headers.get("X-VibraVid-Token") != "private-engine-secret":
            self.send_response(403)
            self.end_headers()
            return
        if type(self).failure:
            code, data = type(self).failure
        else:
            code = 200
            data = {
                "/api/bot/sites/": {"groups": [{"label": "Film & TV", "options": [{"value": "example", "label": "Example"}]}]},
                "/api/bot/status/": {"slots": {"active": 1, "max": 1}, "queued": 0, "disk": {"free": 100000}},
                "/api/get-downloads/?history=30": {"active": [{"id": "job-1", "title": "A show", "progress": 40}], "scheduled": [], "history": []},
                "/api/bot/search/": {"results": [{"title": "A show", "site": "example", "is_movie": False, "payload": {"name": "A show", "is_movie": False}}], "failed": []},
                "/api/bot/seasons/": {"seasons": [{"number": 1, "episodes": 4}]},
                "/api/bot/download/": {"queued": True, "download_id": "job-1", "title": "A show"},
                "/api/bot/cancel/": {"cancelled": True},
            }.get(self.path, {})
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    do_GET = do_POST = handle_api


class IntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = ThreadingHTTPServer(("127.0.0.1", 0), Engine)
        cls.thread = threading.Thread(target=cls.engine.serve_forever, daemon=True)
        cls.thread.start()
        cls.address = f"http://127.0.0.1:{cls.engine.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.engine.shutdown()
        cls.engine.server_close()
        cls.thread.join()

    def setUp(self):
        Engine.calls = []
        Engine.failure = None
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app({"TESTING": True, "DATABASE": str(Path(self.temp.name) / "db.sqlite3"),
                               "VIBRAVID_ADDRESS": self.address,
                               "VIBRAVID_SECRET": "private-engine-secret"})
        self.client = self.app.test_client()
        self.client.get("/")

    def tearDown(self):
        self.temp.cleanup()

    def token(self):
        with self.client.session_transaction() as session:
            return session["csrf"]

    def post(self, action, body):
        return self.client.post("/admin/vibravid/" + action, json=body, headers={"X-CSRF-Token": self.token()})

    def test_search_seasons_queue_cancel_without_login(self):
        self.assertEqual(self.client.get("/admin/vibravid/sites").json["groups"][0]["label"], "Film & TV")
        result = self.post("search", {"query": "A show", "site": "example"}).json["results"][0]
        choice = {"site": result["site"], "payload": result["payload"]}
        self.assertEqual(self.post("seasons", choice).json["seasons"][0]["episodes"], 4)
        response = self.post("download", {**choice, "season": "1", "episodes": "1-3"})
        self.assertTrue(response.json["queued"])
        self.assertEqual(self.client.get("/admin/vibravid/queue").json["active"][0]["progress"], 40)
        self.assertEqual(self.client.get("/admin/vibravid/status").json["slots"]["active"], 1)
        self.assertTrue(self.post("cancel", {"download_id": "job-1"}).json["cancelled"])
        self.assertTrue(all(call[2] == "private-engine-secret" for call in Engine.calls))
        self.assertEqual([c[0] for c in Engine.calls], ["GET", "POST", "POST", "POST", "GET", "GET", "GET", "POST"])
        self.assertNotIn(b"private-engine-secret", self.client.get("/admin").data)
        self.assertNotIn(b"VibraVid Web URL", self.client.get("/admin").data)

    def test_csrf_allowlist_and_validation_prevent_engine_calls(self):
        self.assertEqual(self.client.post("/admin/vibravid/download", json={}).status_code, 403)
        self.assertEqual(self.client.get("/admin/vibravid/download").status_code, 405)
        self.assertEqual(self.post("unknown", {}).status_code, 405)
        for action, body in [("search", {"query": ""}), ("cancel", {}),
                             ("download", {"site": "example", "payload": {"name": "Show"}, "season": "1", "episodes": "../../file"}),
                             ("download", {"site": "example", "payload": {"name": "Show"}})]:
            self.assertEqual(self.post(action, body).status_code, 400)
        self.assertEqual(Engine.calls, [])

    def test_engine_errors_are_safe_and_mutations_are_not_retried(self):
        Engine.failure = (500, {"error": "sensitive-provider-detail"})
        response = self.post("download", {"site": "example", "payload": {"name": "Film", "is_movie": True}})
        self.assertEqual(response.status_code, 502)
        self.assertNotIn(b"sensitive-provider-detail", response.data)
        self.assertEqual(len(Engine.calls), 1)
        client = VibraVid(self.address, "wrong-secret")
        with self.assertRaisesRegex(VibraVidError, "authentication"):
            client.call("sites")
        with self.assertRaisesRegex(VibraVidError, "bundled download engine"):
            VibraVid(self.address, "").call("sites")


class ProviderHeadersTest(unittest.TestCase):
    def test_streamingcommunity_requests_keep_navigation_referers(self):
        runtime = tempfile.TemporaryDirectory()
        self.addCleanup(runtime.cleanup)
        with patch.dict("os.environ", {"VIBRAVID_BASE_PATH": runtime.name}):
            from VibraVid.player import vixcloud

        source = vixcloud.VideoSource("https://provider.example/it", is_series=True, media_id=42)
        iframe = MagicMock(url="https://provider.example/it/iframe/42",
                           text='<iframe src="https://video.example/embed/42"></iframe>')
        video = MagicMock(text="video page")
        expected = ["https://provider.example/it/watch/42?e=77", iframe.url]
        responses = iter([iframe, video])

        def client(headers):
            result = MagicMock()
            result.__enter__.return_value = result
            result.headers = dict(headers)
            return result

        def get(client, url, **kwargs):
            # Model a provider rejecting requests without the navigation context.
            if client.headers.get("Referer") != expected.pop(0):
                raise PermissionError("HTTP 403: missing navigation Referer")
            return next(responses)

        with patch.object(vixcloud, "create_client", side_effect=client), \
                patch.object(vixcloud, "get_with_retry", side_effect=get), \
                patch.object(source, "parse_script") as parse:
            source.get_iframe(77)
            source.get_content()
        self.assertEqual(expected, [])
        self.assertEqual(source.headers["Referer"], "https://video.example/embed/42")
        parse.assert_called_once_with(script_text="video page")


class MetadataTimeoutTest(unittest.TestCase):
    def test_slow_all_season_lookup_has_time_to_complete(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"episodes":[{"season":1,"episode":1}]}'

        def slow_metadata(request, *, timeout):
            # Model a provider that needs 45 seconds without sleeping in tests.
            if timeout < 45:
                raise TimeoutError()
            return response

        with patch("myflix.vibravid.build_opener") as factory:
            factory.return_value.open.side_effect = slow_metadata
            result = VibraVid("http://127.0.0.1:8091", "secret").call(
                "episodes", {"site": "example", "payload": {"name": "Long series"}})
        self.assertEqual(result["episodes"], [{"season": 1, "episode": 1}])
        self.assertEqual(factory.return_value.open.call_count, 1)

    def test_other_requests_keep_normal_timeout_and_never_retry(self):
        for action, body in (("queue", None), ("download", {"site": "example"})):
            with self.subTest(action=action), patch("myflix.vibravid.build_opener") as factory:
                factory.return_value.open.side_effect = TimeoutError()
                with self.assertRaisesRegex(VibraVidError, "Check the queue"):
                    VibraVid("http://127.0.0.1:8091", "secret").call(action, body)
                self.assertEqual(factory.return_value.open.call_count, 1)
                self.assertEqual(factory.return_value.open.call_args.kwargs["timeout"], 30)

    def test_episode_timeout_identifies_the_failed_lookup_safely(self):
        for error in (TimeoutError("private detail"), URLError(TimeoutError("private detail"))):
            with self.subTest(error=type(error)), patch("myflix.vibravid.build_opener") as factory:
                factory.return_value.open.side_effect = error
                with self.assertRaisesRegex(VibraVidError, "Episode lookup timed out") as caught:
                    VibraVid("http://127.0.0.1:8091", "secret").call("episodes", {})
                self.assertNotIn("private detail", str(caught.exception))
                self.assertEqual(factory.return_value.open.call_count, 1)


if __name__ == "__main__":
    unittest.main()

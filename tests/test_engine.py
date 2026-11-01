import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tomlkit

from deploy.vibravid_publish import main as publish_hook
from myflix.app import create_app
from myflix.db import connect
from myflix.engine import ManagedEngine, prepare


class ManagedEngineTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data = Path(self.temp.name)
        self.database = str(self.data / "myflix.sqlite3")
        self.app = create_app({"TESTING": True, "DATABASE": self.database})

    def tearDown(self):
        self.temp.cleanup()

    def test_secret_default_library_and_managed_configuration_persist(self):
        secret = self.app.config["VIBRAVID_SECRET"]
        self.assertTrue(len(secret) >= 64)
        second = create_app({"DATABASE": self.database})
        self.assertEqual(second.config["VIBRAVID_SECRET"], secret)
        state, runtime = prepare(self.database)
        self.assertTrue((runtime / "GUI/webgui/wsgi.py").is_file())
        config = tomlkit.parse((state / "Conf/config.toml").read_text())
        config["DOWNLOAD"]["select_audio"] = "eng"
        (state / "Conf/config.toml").write_text(tomlkit.dumps(config))
        prepare(self.database)
        config = tomlkit.parse((state / "Conf/config.toml").read_text())
        self.assertEqual(config["DOWNLOAD"]["select_audio"], "eng")
        self.assertEqual(config["ARR"]["max_concurrent_downloads"], 1)
        hooks = config["HOOKS"]["post_download"]
        self.assertEqual(len([h for h in hooks if h["name"] == "myflix-publish"]), 1)
        self.assertIn("--database", hooks[0]["args"])
        self.assertEqual(config["OUTPUT"]["root_path"], str(state / "downloads"))

    def test_publication_uses_latest_ui_library_selection(self):
        state, runtime = prepare(self.database)
        file = state / "downloads" / "Film.mp4"
        file.write_bytes(b"completed-video")
        selected = self.data / "selected-library"
        selected.mkdir()
        with connect(self.database) as db:
            db.execute("UPDATE settings SET value=? WHERE key='videos_path'", (str(selected),))
        args = ["hook", "--downloads", str(state / "downloads"), "--database", self.database]
        with patch("sys.argv", args), patch.dict(os.environ, {"SC_DOWNLOAD_SUCCESS": "1", "SC_DOWNLOAD_PATH": str(file)}), patch("builtins.print"):
            publish_hook()
        self.assertEqual((selected / "Film.mp4").read_bytes(), b"completed-video")
        self.assertFalse((self.data / "library/Film.mp4").exists())

    def test_real_bundled_engine_starts_without_env_secret_and_stops(self):
        engine = ManagedEngine(self.app)
        try:
            with patch.dict(os.environ, {"MYFLIX_VIBRAVID_SECRET": "", "MYFLIX_VIBRAVID_ADDRESS": ""}):
                engine.start()
            process = engine.process
            client = self.app.extensions["vibravid"]
            self.assertNotIn(":0/", client.address)
            self.assertTrue(client.call("sites")["groups"])
            self.assertEqual(client.call("status")["slots"]["max"], 1)
            self.assertEqual(client.call("queue")["active"], [])
            duplicate = ManagedEngine(self.app)
            with self.assertRaisesRegex(RuntimeError, "Another MyFlix instance"):
                duplicate.start()
        finally:
            engine.close()
        self.assertIsNotNone(process.poll())
        engine.close()  # Repeated teardown is harmless.


if __name__ == "__main__":
    unittest.main()

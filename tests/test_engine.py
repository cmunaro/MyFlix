import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tomlkit

from deploy.vibravid_publish import main as publish_hook
from myflix.app import create_app
from myflix.db import connect
from myflix.engine import ManagedEngine, prepare
from myflix.vibravid import VibraVidError


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
        config["HOOKS"]["post_download"] = [
            {"name": "myflix-publish", "type": "python", "path": "legacy.py"},
            {"name": "household-hook", "type": "python", "path": "custom.py"}]
        (state / "Conf/config.toml").write_text(tomlkit.dumps(config))
        prepare(self.database)
        config = tomlkit.parse((state / "Conf/config.toml").read_text())
        self.assertEqual(config["DOWNLOAD"]["select_audio"], "eng")
        self.assertEqual(config["ARR"]["max_concurrent_downloads"], 4)
        self.assertFalse(config["PROCESS"]["use_live_mux"])
        self.assertFalse(config["DOWNLOAD"]["use_live_decrypt"])
        hooks = config["HOOKS"]["post_download"]
        self.assertFalse(any(h["name"] == "myflix-publish" for h in hooks),
                         "publication is synchronous before completion, never a duplicate post-download hook")
        self.assertTrue(any(h["name"] == "household-hook" for h in hooks))
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
            self.assertEqual(len(client.instance_id), 32)
            self.assertNotIn(":0/", client.address)
            self.assertTrue(client.call("sites")["groups"])
            self.assertEqual(client.call("status")["slots"]["max"], 4)
            self.assertEqual(client.call("queue")["active"], [])
            with self.assertRaisesRegex(VibraVidError, "HTTP 502"):
                client.call("episodes", {})  # Installed adapter, no provider request.
            with self.assertRaisesRegex(VibraVidError, "HTTP 400"):
                client.call("metadata", {"seasons": [True]})
            with self.assertRaisesRegex(VibraVidError, "HTTP 502"):
                client.call("metadata", {})  # Installed metadata adapter, no provider request.
            with self.assertRaisesRegex(VibraVidError, "HTTP 400"):
                client.call("retry_download", {})  # Priority adapter, no provider request.
            duplicate = ManagedEngine(self.app)
            with self.assertRaisesRegex(RuntimeError, "Another MyFlix instance"):
                duplicate.start()
            # Simulate the parent losing its descriptor while the engine lives.
            # Recovery must never run beside an orphaned previous engine.
            engine.lock.close()
            engine.lock = None
            with self.assertRaisesRegex(RuntimeError, "Another MyFlix instance"):
                ManagedEngine(self.app).start()
        finally:
            engine.close()
        self.assertIsNotNone(process.poll())
        engine.close()  # Repeated teardown is harmless.

    def test_engine_process_retains_instance_lock_and_each_start_gets_new_identity(self):
        spawn = subprocess.Popen
        worker = "import os,sys,time; os.write(int(sys.argv[1]), b'http://127.0.0.1:12345'); os.close(int(sys.argv[1])); time.sleep(60)"
        def lightweight_engine(command, **kwargs):
            return spawn([sys.executable, "-c", worker, command[-1]], **kwargs)
        identities = []
        with patch("myflix.engine.prepare", return_value=(self.data, self.data)), \
                patch("myflix.engine.subprocess.Popen", side_effect=lightweight_engine):
            for _ in range(2):
                engine = ManagedEngine(self.app)
                try:
                    engine.start()
                    identities.append(self.app.extensions["vibravid"].instance_id)
                    engine.lock.close()
                    engine.lock = None
                    with self.assertRaisesRegex(RuntimeError, "Another MyFlix instance"):
                        ManagedEngine(self.app).start()
                finally:
                    engine.close()
        self.assertEqual(len(set(identities)), 2)
        self.assertTrue(all(len(identity) == 32 for identity in identities))


if __name__ == "__main__":
    unittest.main()

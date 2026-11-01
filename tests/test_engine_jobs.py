import json
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from myflix.app import create_app
from myflix.db import connect, initialize, settings
from myflix.engine import prepare
from myflix.engine_jobs import DownloadStages, PriorityDownloads, ToolWrappers, read_limits


def wait_for(predicate):
    deadline = time.monotonic() + 4
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("Timed out waiting for test workers")
        time.sleep(0.01)


class JobLimitsTest(unittest.TestCase):
    def test_durable_retry_backlog_keeps_ordinary_jobs_behind_later_retry_windows(self):
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "catalog.sqlite3")
            create_app({"TESTING": True, "DATABASE": database})
            with connect(database) as db:
                root = settings(db)["videos_path"]
                db.execute("INSERT INTO download_retries(root,source_key,source,body) VALUES(?,'failure','{}','{}')", (root,))
            stages = DownloadStages(None, None, 1)
            scheduler = PriorityDownloads(1, stages, lambda count: None, database)
            stages.acquire_network, stages.release_network = scheduler.acquire, scheduler.release
            ordinary_started = threading.Event()
            try:
                ordinary = scheduler.submit(ordinary_started.set)
                with scheduler.prioritize():
                    retry = scheduler.submit(lambda: "priority retry")
                self.assertEqual(retry.result(timeout=2), "priority retry")
                self.assertFalse(ordinary_started.is_set(), "SQLite retries retain priority before the next dispatch window")
                with connect(database) as db:
                    db.execute("UPDATE download_retries SET state='submitted'")
                ordinary.result(timeout=3)
                self.assertTrue(ordinary_started.is_set())
            finally:
                scheduler.shutdown()

    def test_full_worker_pool_can_finish_running_season_tasks_before_retry(self):
        stages = DownloadStages(None, None, 1)
        scheduler = PriorityDownloads(1, stages, lambda count: None)
        stages.acquire_network, stages.release_network = scheduler.acquire, scheduler.release
        all_started = threading.Barrier(11)
        release = threading.Event()
        def season():
            stages.release()
            all_started.wait(3)
            release.wait(3)
            stages.acquire()
        try:
            normal = [scheduler.submit(season) for _ in range(10)]
            all_started.wait(3)
            with scheduler.prioritize():
                retry = scheduler.submit(lambda: "retry")
            release.set()
            self.assertEqual(retry.result(timeout=3), "retry")
            for future in normal:
                future.result(timeout=3)
        finally:
            release.set()
            scheduler.shutdown()

    def test_priority_retries_start_before_ordinary_waiting_downloads(self):
        stages = DownloadStages(None, None, 1)
        scheduler = PriorityDownloads(1, stages, lambda count: None)
        stages.acquire_network, stages.release_network = scheduler.acquire, scheduler.release
        started, release = threading.Event(), threading.Event()
        order = []
        def running():
            order.append("running")
            started.set()
            release.wait(3)
        try:
            first = scheduler.submit(running)
            self.assertTrue(started.wait(1))
            normal = [scheduler.submit(lambda n=n: order.append(n)) for n in ("normal1", "normal2")]
            with scheduler.prioritize():
                retries = [scheduler.submit(lambda n=n: order.append(n)) for n in ("retry1", "retry2")]
            release.set()
            for future in [first] + retries + normal:
                future.result(timeout=3)
            self.assertEqual(order, ["running", "retry1", "retry2", "normal1", "normal2"])
            wait_for(lambda: scheduler.active == 0)
        finally:
            release.set()
            scheduler.shutdown()

    def test_priority_scheduler_releases_network_while_processing_and_after_failure(self):
        stages = DownloadStages(None, None, 1)
        scheduler = PriorityDownloads(1, stages, lambda count: None)
        stages.acquire_network, stages.release_network = scheduler.acquire, scheduler.release
        processing, release, retry_started = threading.Event(), threading.Event(), threading.Event()
        def finish():
            processing.set()
            release.wait(3)
            raise RuntimeError("merge failure")
        try:
            first = scheduler.submit(lambda: stages.finish(finish, lambda: False, lambda: None))
            self.assertTrue(processing.wait(1))
            with scheduler.prioritize():
                retry = scheduler.submit(retry_started.set)
            self.assertTrue(retry_started.wait(1), "muxing must not hold a network permit")
            retry.result(timeout=2)
            release.set()
            with self.assertRaisesRegex(RuntimeError, "merge failure"):
                first.result(timeout=2)
            self.assertEqual(scheduler.submit(lambda: "next").result(timeout=2), "next")
        finally:
            release.set()
            scheduler.shutdown()

    def test_four_downloads_overlap_and_processing_does_not_hold_network_slots(self):
        network = threading.BoundedSemaphore(4)
        stages = DownloadStages(network.acquire, network.release, 1)
        downloaded = threading.Barrier(5)
        finish = threading.Event()
        processing = threading.Event()
        waiting = threading.Event()
        counts = {"active": 0, "peak": 0}
        lock = threading.Lock()
        failures = []
        def work():
            with lock:
                counts["active"] += 1
                counts["peak"] = max(counts["peak"], counts["active"])
            processing.set()
            finish.wait(4)
            with lock:
                counts["active"] -= 1
        def task():
            try:
                stages.acquire()
                stages.acquire()  # Per-episode precheck must not double acquire.
                downloaded.wait(4)
                stages.finish(work, lambda: False, waiting.set)
                stages.acquire()  # The next episode reacquires its network slot.
            except Exception as error:
                failures.append(error)
            finally:
                stages.release()
                stages.release()  # Upstream finally may release an early return.
        threads = [threading.Thread(target=task) for _ in range(4)]
        for thread in threads:
            thread.start()
        try:
            downloaded.wait(4)  # All four reached the download stage together.
            self.assertTrue(processing.wait(2))
            self.assertTrue(waiting.wait(2))
            wait_for(lambda: network._value == 4)
            stages.acquire()  # Another download can start during processing.
            stages.release()
        finally:
            finish.set()
            for thread in threads:
                thread.join(4)
        self.assertEqual(failures, [])
        self.assertEqual(counts["peak"], 1)
        self.assertEqual(network._value, 4)

    def test_cancellation_and_processing_errors_release_permits(self):
        stages = DownloadStages(lambda: None, lambda: None, 1)
        stages.processing.acquire()
        cancelled = threading.Event()
        errors = []
        def task():
            try:
                stages.finish(lambda: self.fail("Cancelled processing started"), cancelled.is_set, lambda: None)
            except InterruptedError:
                errors.append("cancelled")
        thread = threading.Thread(target=task)
        thread.start()
        cancelled.set()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        stages.processing.release()
        self.assertEqual(errors, ["cancelled"])
        with self.assertRaisesRegex(RuntimeError, "processing failure"):
            stages.finish(lambda: (_ for _ in ()).throw(RuntimeError("processing failure")), lambda: False, lambda: None)
        self.assertEqual(stages.finish(lambda: "finished", lambda: False, lambda: None), "finished")

    def test_settings_validate_persist_and_drive_engine_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "catalog.sqlite3")
            app = create_app({"TESTING": True, "DATABASE": database})
            client = app.test_client()
            page = client.get("/admin?section=settings")
            self.assertIn(b'name="download_limit"', page.data)
            self.assertIn(b'name="processing_limit"', page.data)
            with client.session_transaction() as session:
                csrf = session["csrf"]
            url = "/admin/settings/downloads"
            self.assertEqual(client.post(url, data={"download_limit": "2", "processing_limit": "1"}).status_code, 403)
            for downloads, processing in (("0", "1"), ("11", "1"), ("4", "0"), ("4", "11"), ("1.5", "1"), ("4", "")):
                self.assertEqual(client.post(url, data={"csrf": csrf, "download_limit": downloads, "processing_limit": processing}).status_code, 400)
                self.assertEqual(read_limits(database), (4, 1))
            self.assertEqual(client.post(url, data={"csrf": csrf, "download_limit": "3", "processing_limit": "2"}).status_code, 302)
            initialize(database)
            self.assertEqual(read_limits(database), (3, 2))
            with connect(database) as db:
                self.assertTrue(settings(db)["videos_path"])
            state, _ = prepare(database)
            self.assertIn("max_concurrent_downloads = 3", (state / "Conf/config.toml").read_text())


class ProcessLimitTest(unittest.TestCase):
    def test_tool_shims_keep_their_command_names_and_probe_dispatch(self):
        with tempfile.TemporaryDirectory(prefix="myflix shims ") as directory:
            root = Path(directory)
            dispatcher = root / "mise"
            dispatcher.write_text(f"#!{sys.executable}\n" + '''import json, sys
from pathlib import Path
print(json.dumps([Path(sys.argv[0]).name, sys.argv[1:]]))
''')
            dispatcher.chmod(0o700)
            shims = root / "shims"
            shims.mkdir()
            for name in ("ffmpeg", "ffprobe", "mkvmerge"):
                (shims / name).symlink_to(dispatcher)
            wrappers = ToolWrappers(root / "state", 1)
            ffmpeg = Path(wrappers.wrap(shims / "ffmpeg", ffprobe=shims / "ffprobe"))
            for path, name in ((ffmpeg, "ffmpeg"), (ffmpeg.parent / "ffprobe", "ffprobe"),
                               (Path(wrappers.wrap(shims / "mkvmerge")), "mkvmerge")):
                with self.subTest(tool=name):
                    output = subprocess.check_output([str(path), "-version", "name with spaces", "$literal"], text=True)
                    self.assertEqual(json.loads(output), [name, ["-version", "name with spaces", "$literal"]])
            self.assertNotEqual(ffmpeg, Path(wrappers.wrap(dispatcher)), "shim and dispatcher must not share a wrapper")

    def test_media_tools_share_process_limit_and_preserve_arguments(self):
        for limit in (1, 2):
            with self.subTest(limit=limit), tempfile.TemporaryDirectory(prefix="myflix tools ") as directory:
                root = Path(directory)
                script = root / "fake.py"
                script.write_text('''import json, os, sys, time
from pathlib import Path
root = Path(sys.argv[1])
def record(event):
    fd = os.open(root / "events", os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
    os.write(fd, (json.dumps([event, os.getpid(), sys.argv[2:]]) + "\\n").encode())
    os.close(fd)
record("start")
while not (root / "release").exists(): time.sleep(.01)
record("end")
''')
                processes = []
                wrappers = ToolWrappers(root / "state", limit)
                def events():
                    path = root / "events"
                    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
                try:
                    for index in range(4):
                        tool = root / ("ffmpeg" if index % 2 else "mkvmerge")
                        tool.write_text("#!/bin/sh\nexec " + shlex.join([sys.executable, str(script)]) + ' "$@"\n')
                        tool.chmod(0o700)
                        wrapped = wrappers.wrap(tool)
                        processes.append(subprocess.Popen([wrapped, str(root), "name with spaces", "$literal"], stdout=subprocess.PIPE, stderr=subprocess.PIPE))
                    wait_for(lambda: len(events()) >= limit)
                    self.assertEqual(len(events()), limit)
                    (root / "release").touch()
                    for process in processes:
                        _, error = process.communicate(timeout=4)
                        self.assertEqual(process.returncode, 0, error.decode())
                    active = peak = 0
                    for event, _, arguments in events():
                        active += 1 if event == "start" else -1
                        peak = max(peak, active)
                        self.assertEqual(arguments, ["name with spaces", "$literal"])
                    self.assertEqual((active, peak), (0, limit))
                finally:
                    for process in processes:
                        if process.poll() is None:
                            process.kill()
                        process.communicate()

    def test_killed_process_releases_slot_for_next_job(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tool = root / "ffmpeg"
            tool.write_text('#!/bin/sh\nprintf started > "$1"\nexec /bin/sleep 30\n')
            tool.chmod(0o700)
            wrapped = ToolWrappers(root / "state", 1).wrap(tool)
            first = subprocess.Popen([wrapped, str(root / "first")])
            second = None
            try:
                wait_for(lambda: (root / "first").exists())
                second = subprocess.Popen([wrapped, str(root / "second")])
                self.assertFalse((root / "second").exists())
                first.kill()
                first.wait(timeout=2)
                wait_for(lambda: (root / "second").exists())
            finally:
                for process in (first, second):
                    if process is not None:
                        if process.poll() is None:
                            process.kill()
                        process.wait(timeout=2)


if __name__ == "__main__":
    unittest.main()

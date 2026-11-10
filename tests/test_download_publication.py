import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from deploy.vibravid_publish import _rename_no_replace
from myflix.app import create_app
from myflix.catalog import scan
from myflix.credits import CreditsCache, fingerprint
from myflix.db import connect
from myflix.engine_jobs import DownloadStages, install_publication


class Tracker:
    def __init__(self):
        self._lock = threading.Lock()
        self.downloads = {'job': {'status': 'Downloading'}}
        self.history = []
        self.stop_events = {'job': threading.Event()}
        self.cancelled = set()

    def is_stopped(self, identity):
        with self._lock:
            event = self.stop_events.get(identity)
            return identity in self.cancelled or bool(event and event.is_set())

    def update_status(self, identity, status):
        with self._lock:
            if identity in self.downloads:
                self.downloads[identity]['status'] = status

    def complete_download(self, identity, success=True, error=None, path=None):
        with self._lock:
            self.downloads.pop(identity, None)
            self.history.append({'id': identity, 'success': success, 'error': error, 'path': path})


class DownloadPublicationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.database = str(self.base / 'myflix.sqlite3')
        self.app = create_app({'TESTING': True, 'DATABASE': self.database})
        self.downloads = self.base / 'vibravid' / 'downloads'
        self.source = self.downloads / 'Show' / 'Show.S02E20.mp4'
        self.source.parent.mkdir(parents=True)
        self.source.write_bytes(b'completed-video')
        os.utime(self.source, (time.time() - 120,) * 2)
        self.target = self.base / 'library' / self.source.relative_to(self.downloads)
        self.tracker = Tracker()
        self.stages = DownloadStages(1)
        install_publication(self.tracker, self.database, self.stages)

    def tearDown(self):
        self.temp.cleanup()

    def finish(self):
        self.tracker.complete_download('job', path=str(self.source))

    def test_detection_and_cache_precede_visibility_and_success(self):
        def detect(path, *args):
            self.assertEqual(Path(path).read_bytes(), b'completed-video')
            self.assertFalse(self.target.exists())
            self.assertEqual(self.tracker.downloads['job']['status'], 'Processing')
            self.assertEqual(self.tracker.history, [])
            return 1200

        def rename(temporary, target):
            with connect(self.database) as db:
                row = db.execute('SELECT position FROM credits WHERE fingerprint=?',
                                 (fingerprint(temporary, location=target),)).fetchone()
            self.assertEqual(row[0], 1200, 'timestamp must be committed before the file is visible')
            return _rename_no_replace(temporary, target)

        with patch.object(CreditsCache, 'tools', return_value=('f', 'p')), \
                patch('myflix.credits.analyze', side_effect=detect) as detector, \
                patch('deploy.vibravid_publish._rename_no_replace', side_effect=rename):
            # Finalization already holds the processing slot; publication must
            # reuse it rather than deadlocking while acquiring a second permit.
            self.stages.finish(self.finish, lambda: False, lambda: None)
            self.assertTrue(self.tracker.history[0]['success'])
            self.assertEqual(self.target.read_bytes(), b'completed-video')
            self.assertTrue(self.source.exists())
            self.finish()  # Duplicate completion cannot repeat publication.
            self.assertEqual(len(self.tracker.history), 1)
            scan(self.database)
            with connect(self.database) as db:
                media = db.execute('SELECT id FROM media').fetchone()[0]
            self.assertEqual(self.app.test_client().get('/api/playback/' + str(media)).json['credits_start'], 1200)
            self.assertFalse(self.app.extensions['credits'].step(threading.Event()))
            self.assertEqual(detector.call_count, 1, 'background indexing must reuse the pre-publication result')

    def test_inconclusive_and_failed_detection_still_publish_with_fallback(self):
        for error in [None, RuntimeError('Frame extraction failed')]:
            with self.subTest(error=error):
                with patch.object(CreditsCache, 'tools', return_value=('f', 'p')), \
                        patch('myflix.credits.analyze', return_value=None, side_effect=error):
                    if error:
                        with self.assertLogs(level='ERROR'):
                            self.finish()
                    else:
                        self.finish()
                self.assertTrue(self.tracker.history[-1]['success'])
                with connect(self.database) as db:
                    cached = db.execute('SELECT position,retry_at FROM credits WHERE fingerprint=?',
                                        (fingerprint(self.target),)).fetchone()
                self.assertIsNone(cached[0])
                if error:
                    self.assertGreater(cached[1], time.time())
                else:
                    self.assertEqual(cached[1], 0)
                self.target.unlink()
                with connect(self.database) as db:
                    db.execute('DELETE FROM credits')
                self.tracker.downloads['job'] = {'status': 'Downloading'}

    def test_cancellation_keeps_staging_and_removes_hidden_copy(self):
        def detect(*args):
            self.tracker.stop_events['job'].set()
            return 1200
        with patch.object(CreditsCache, 'tools', return_value=('f', 'p')), \
                patch('myflix.credits.analyze', side_effect=detect):
            self.finish()
        self.assertEqual(self.tracker.history[0]['error'], 'cancelled')
        self.assertFalse(self.tracker.history[0]['success'])
        self.assertTrue(self.source.exists())
        self.assertFalse(self.target.exists())
        self.assertEqual(list(self.target.parent.glob('.myflix-*')), [])
        with connect(self.database) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM credits').fetchone()[0], 0)

    def test_failed_and_conflicting_downloads_are_never_analyzed_or_overwritten(self):
        with patch('myflix.credits.analyze') as detector:
            self.tracker.complete_download('job', success=False, error='failed', path=str(self.source))
            self.assertFalse(self.target.exists())
            self.target.parent.mkdir(parents=True)
            self.target.write_bytes(b'existing-video')
            self.tracker.downloads['job'] = {'status': 'Downloading'}
            with self.assertLogs(level='ERROR'):
                self.finish()
            self.assertFalse(self.tracker.history[-1]['success'])
            self.assertEqual(self.target.read_bytes(), b'existing-video')
            detector.assert_not_called()

    def test_cancelled_timed_out_recovery_never_publishes(self):
        self.tracker.downloads.clear()
        self.tracker.history.append({'id': 'job', 'status': 'timed_out'})
        self.tracker.stop_events.clear()
        self.tracker.cancelled.add('job')
        with patch('myflix.credits.analyze') as detector:
            self.finish()
        self.assertEqual(self.tracker.history[-1]['error'], 'cancelled')
        self.assertFalse(self.target.exists())
        detector.assert_not_called()

    def test_background_and_download_analysis_share_one_slot(self):
        started, release = threading.Event(), threading.Event()
        errors = []
        def detect(*args):
            started.set()
            release.wait(3)
            return 1200
        def background():
            try:
                self.app.extensions['credits'].step(threading.Event())
            except BaseException as error:
                errors.append(error)
        with patch.object(CreditsCache, 'tools', return_value=('f', 'p')), \
                patch('myflix.credits.analyze', side_effect=detect) as detector:
            publisher = threading.Thread(target=self.finish)
            worker = threading.Thread(target=background)
            publisher.start()
            try:
                self.assertTrue(started.wait(2))
                worker.start()
                time.sleep(.1)
                self.assertTrue(worker.is_alive(), 'backfill must wait for download analysis')
                self.assertEqual(self.tracker.history, [])
            finally:
                release.set()
                publisher.join(4)
                if worker.ident:
                    worker.join(4)
            self.assertFalse(publisher.is_alive())
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(detector.call_count, 1)

    def test_legacy_standalone_hook_runs_outside_project_and_films_skip_detection(self):
        source = self.downloads / 'Film.mp4'
        source.write_bytes(b'film')
        hook = Path(__file__).resolve().parent.parent / 'deploy' / 'vibravid_publish.py'
        subprocess.run([sys.executable, str(hook), '--downloads', str(self.downloads),
                        '--database', self.database], cwd=self.base,
                       env=dict(os.environ, SC_DOWNLOAD_SUCCESS='1', SC_DOWNLOAD_PATH=str(source)),
                       check=True, capture_output=True, timeout=10)
        self.assertEqual((self.base / 'library' / 'Film.mp4').read_bytes(), b'film')
        with connect(self.database) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM credits').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()

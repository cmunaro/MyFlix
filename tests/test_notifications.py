import tempfile
import unittest
from collections import namedtuple
from pathlib import Path
from unittest.mock import patch

from myflix.app import create_app
from myflix.catalog import import_completed, scan
from myflix.db import connect


class NotificationsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = str(Path(self.temp.name).resolve() / 'test.sqlite3')
        self.app = create_app({'TESTING': True, 'DATABASE': self.database})
        self.client = self.app.test_client()
        self.root = Path(self.temp.name).resolve() / 'library'

    def notices(self):
        with patch('myflix.app.shutil.disk_usage', return_value=namedtuple('Usage', 'total used free')(100, 50, 50)):
            response = self.client.get('/api/notifications')
        self.assertEqual(response.status_code, 200)
        return response.json['notifications']

    def publish(self, name):
        path = self.root / name
        path.write_bytes(b'video')
        return import_completed(self.database, path, str(self.root))

    def test_publication_is_durable_idempotent_and_scans_do_not_notify(self):
        path = self.root / 'Film.mp4'
        path.write_bytes(b'video')
        with patch('myflix.catalog.time.time', return_value=path.stat().st_mtime + 120):
            scan(self.database)
        self.assertEqual(self.notices(), [])
        media_id = import_completed(self.database, path, str(self.root))
        import_completed(self.database, path, str(self.root))
        self.assertEqual([n['message'] for n in self.notices()], ['Film downloaded'])
        self.assertEqual(self.notices()[0]['href'], f'/title/{media_id}')
        restarted = create_app({'TESTING': True, 'DATABASE': self.database})
        self.assertEqual(len(restarted.test_client().get('/api/notifications').json['notifications']), 1)
        with connect(self.database) as db:
            db.execute('UPDATE media SET hidden=1 WHERE id=?', (media_id,))
        self.assertEqual(self.notices(), [])
        with connect(self.database) as db:
            db.execute('UPDATE media SET hidden=0,available=0 WHERE id=?', (media_id,))
        self.assertEqual(self.notices(), [])
        with connect(self.database) as db:
            db.execute("UPDATE settings SET value=? WHERE key='videos_path'", (str(self.root / 'other'),))
        self.assertEqual(self.notices(), [])

    def test_season_requires_all_listed_visible_episodes(self):
        with connect(self.database) as db:
            db.execute("""INSERT INTO subscriptions(root,series,site,identity,payload,lookup_state)
                VALUES(?,'Show','site','show','{}','complete')""", (str(self.root),))
            db.executemany("INSERT INTO discoveries(subscription_id,season,episode,title) VALUES(1,1,?,'')", [(1,), (2,)])
        self.publish('Show.S01E01.mp4')
        self.assertEqual([n['message'] for n in self.notices()], ['Show · S01E01 downloaded'])
        self.publish('Show.S01E02.mp4')
        notices = self.notices()
        self.assertEqual(notices[0]['message'], 'Show · Season 1 downloaded')
        self.assertEqual(notices[1]['message'], 'Show · S01E02 downloaded')
        self.assertTrue(notices[0]['href'].startswith('/series/'))
        self.publish('Unknown.S01E01.mp4')
        self.assertFalse(any('Unknown · Season' in n['message'] for n in self.notices()))

    def test_hidden_episode_does_not_complete_season_and_failed_import_has_no_notice(self):
        with connect(self.database) as db:
            db.execute("""INSERT INTO subscriptions(root,series,site,identity,payload,lookup_state)
                VALUES(?,'Show','site','show','{}','complete')""", (str(self.root),))
            db.executemany("INSERT INTO discoveries(subscription_id,season,episode,title) VALUES(1,1,?,'')", [(1,), (2,)])
        first = self.publish('Show.S01E01.mp4')
        with connect(self.database) as db:
            db.execute('UPDATE media SET hidden=1 WHERE id=?', (first,))
        self.publish('Show.S01E02.mp4')
        self.assertFalse(any('Season' in n['message'] for n in self.notices()))
        with self.assertRaises(ValueError):
            self.publish('Show.S01E01.mp4')
        self.assertEqual(len(self.notices()), 1)

    def test_storage_threshold_recovers_and_missing_mount_does_not_claim_low_space(self):
        usage = namedtuple('Usage', 'total used free')
        for free, expected in ((11, False), (10, True), (0, True), (50, False)):
            with patch('myflix.app.shutil.disk_usage', return_value=usage(100, 100-free, free)):
                notices = self.client.get('/api/notifications').json['notifications']
            self.assertEqual(any(n['id'] == 'storage' for n in notices), expected)
        with patch('myflix.app.shutil.disk_usage', side_effect=FileNotFoundError):
            self.assertEqual(self.client.get('/api/notifications').json['notifications'], [])
        page = self.client.get('/').data.decode()
        self.assertIn('id="notification-toggle"', page)
        self.assertIn('aria-controls="notifications"', page)

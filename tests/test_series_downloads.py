import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from myflix.app import create_app
from myflix.catalog import scan
from myflix.db import connect, initialize
from test_monitoring import Provider


class SeriesDownloadsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.database = str(self.base / 'myflix.sqlite3')
        self.app = create_app({'TESTING': True, 'DATABASE': self.database})
        self.client = self.app.test_client()
        self.client.get('/')
        self.monitor = self.app.extensions['monitor']
        self.provider = Provider()
        self.monitor.engine = self.provider
        self.app.extensions['vibravid'].call = self.provider.call
        self.payload = {'name': 'Show', 'id': 'show', 'type': 'tv', 'is_movie': False}
        self.library = self.base / 'library'
        scan(self.database)

    def post(self, url, body):
        with self.client.session_transaction() as session:
            token = session['csrf']
        return self.client.post(url, json=body, headers={'X-CSRF-Token': token})

    def inspect(self):
        response = self.post('/admin/vibravid/episodes', {'site': 'example', 'payload': self.payload})
        self.assertEqual(response.status_code, 200, response.data)
        self.source = response.json['source']
        return response.json

    def start(self, selection=None, only_new=False, identity=1):
        return self.post('/admin/downloads/series', {'series_id': identity, 'source': self.source, 'selection': selection or [], 'only_new': only_new})

    def check(self):
        with connect(self.database) as db:
            db.execute('UPDATE subscriptions SET next_check=0')
        self.monitor.tick()

    def downloads(self):
        return [body for action, body in self.provider.calls if action in ('download', 'retry_download')]

    def video(self, episode):
        path = self.library / ('Show.S01E%02d.mp4' % episode)
        path.write_bytes(b'video')
        os.utime(path, (time.time() - 120,) * 2)
        scan(self.database)

    def test_only_new_baselines_existing_episodes_and_downloads_future_ones(self):
        self.inspect()
        self.monitor.tick()
        self.assertEqual(self.downloads(), [], 'opening the picker must not enable downloads')
        self.assertTrue(self.start(only_new=True).json['queued'])
        self.monitor.tick()
        self.assertEqual(self.downloads(), [])
        self.provider.episodes.append({'season': 1, 'episode': 2})
        self.check()
        self.monitor.tick()
        self.assertEqual([(b['season'], b['episodes']) for b in self.downloads()], [('1', '2')])
        self.provider.episodes += [{'season': 1, 'episode': 3}, {'season': 2, 'episode': 1}]
        self.check()
        self.assertEqual([(b['season'], b['episodes']) for b in self.downloads()], [('1', '2'), ('2', '1')])

    def test_download_queues_only_selected_episodes_and_keeps_monitoring(self):
        self.provider.episodes += [{'season': 1, 'episode': 2}, {'season': 1, 'episode': 3}]
        self.inspect()
        selected = [{'season': 1, 'episode': 2}]
        self.assertEqual(self.start(selected).json['selected'], 1)
        self.assertEqual(self.start(selected).json['selected'], 0, 'repeat clicks cannot duplicate durable work')
        queued = self.client.get('/admin/vibravid/queue').json['scheduled']
        self.assertEqual([(j['season'], j['episode']) for j in queued], [(1, 2)])
        self.monitor.tick()
        self.assertEqual([b['episodes'] for b in self.downloads()], ['2'])
        self.check()
        self.assertEqual([b['episodes'] for b in self.downloads()], ['2'], 'unselected existing episodes stay skipped')
        self.provider.episodes.append({'season': 1, 'episode': 4})
        self.check()
        self.assertEqual([b['episodes'] for b in self.downloads()], ['2', '4'])

    def test_downloaded_episodes_are_locked_and_cannot_be_forged_into_downloads(self):
        self.provider.episodes.append({'season': 1, 'episode': 2})
        self.video(1)
        result = self.inspect()
        self.assertTrue(result['episodes'][0]['available'])
        self.assertTrue(result['episodes'][0]['locked'])
        self.assertFalse(result['episodes'][1]['locked'])
        self.assertEqual(self.start([{'season': 1, 'episode': 1}]).json['selected'], 0)
        self.monitor.tick()
        self.assertEqual(self.downloads(), [])

    def test_pending_and_uncertain_work_is_locked_without_automatic_resubmission(self):
        self.inspect()
        self.start([{'season': 1, 'episode': 1}])
        self.assertTrue(self.inspect()['episodes'][0]['locked'])
        self.provider.fail = 'download'
        self.monitor.tick()
        self.provider.fail = None
        self.check()
        self.assertEqual(len(self.downloads()), 1)
        self.assertTrue(self.inspect()['episodes'][0]['locked'])
        self.assertEqual(self.start([{'season': 1, 'episode': 1}]).json['selected'], 0)

    def test_inspecting_managed_series_does_not_enable_older_gap_catchup(self):
        self.inspect()
        self.start(only_new=True)
        self.provider.episodes += [{'season': 1, 'episode': 2}, {'season': 2, 'episode': 1}]
        self.inspect()
        self.check()
        self.assertEqual([(b['season'], b['episodes']) for b in self.downloads()], [('2', '1')],
                         'inspection preserves automatic checks but never catches up older gaps')
        self.provider.episodes.append({'season': 2, 'episode': 2})
        self.check()
        self.assertEqual([(b['season'], b['episodes']) for b in self.downloads()], [('2', '1'), ('2', '2')])

    def test_selection_survives_restart_and_obeys_capacity(self):
        self.provider.episodes.append({'season': 1, 'episode': 2})
        self.inspect()
        self.start([{'season': 1, 'episode': n} for n in (1, 2)])
        with connect(self.database) as db:
            db.execute("UPDATE settings SET value='1' WHERE key='download_limit'")
        self.provider.queue['active'] = [{'id': 'busy'}]
        self.monitor.tick()
        self.assertEqual(self.downloads(), [])
        initialize(self.database)
        self.assertEqual(len(self.client.get('/admin/vibravid/queue').json['scheduled']), 2)
        self.provider.queue['active'] = []
        self.monitor.tick()
        self.assertEqual(len(self.downloads()), 1)

    def test_validation_root_confinement_and_csrf(self):
        self.inspect()
        self.assertEqual(self.client.post('/admin/downloads/series', json={}).status_code, 403)
        for data in ({'series_id': True, 'selection': [], 'only_new': True},
                     {'series_id': 1, 'selection': [{'season': 1, 'episode': 999}], 'only_new': False},
                     {'series_id': 1, 'selection': [{'season': True, 'episode': 1}], 'only_new': False},
                     {'series_id': 1, 'selection': [], 'only_new': False}):
            self.assertEqual(self.post('/admin/downloads/series', data).status_code, 400)
        other = self.base / 'other'
        other.mkdir()
        with connect(self.database) as db:
            db.execute("UPDATE settings SET value=? WHERE key='videos_path'", (str(other),))
        self.assertEqual(self.start(only_new=True).status_code, 400)

    def test_saved_manual_sources_are_baselined_then_monitored(self):
        self.inspect()  # An open picker must not prevent restoring an existing source.
        with connect(self.database) as db:
            db.execute('INSERT INTO download_requests VALUES(?,?,?)',
                       (str(self.library), 'old-job', json.dumps({'site': 'example', 'payload': self.payload, 'season': '1', 'episodes': '1'})))
        self.monitor.tick()
        self.assertEqual(self.downloads(), [])
        self.provider.episodes.append({'season': 1, 'episode': 2})
        self.check()
        self.assertEqual([b['episodes'] for b in self.downloads()], ['2'])

    def test_migration_enables_checks_without_replaying_paused_backlogs(self):
        self.inspect()
        with connect(self.database) as db:
            db.execute("UPDATE subscriptions SET mode='paused'")
            db.execute("UPDATE discoveries SET state='new'")
            db.execute('PRAGMA user_version=3')
        initialize(self.database)
        self.monitor.tick()
        self.assertEqual(self.downloads(), [])
        with connect(self.database) as db:
            self.assertEqual(db.execute('SELECT mode FROM subscriptions').fetchone()[0], 'auto')

    def test_waiting_selections_are_cancellable_without_reaching_the_engine(self):
        self.inspect()
        self.start([{'season': 1, 'episode': 1}])
        identity = self.client.get('/admin/vibravid/queue').json['scheduled'][0]['id']
        before = len(self.provider.calls)
        response = self.post('/admin/vibravid/cancel', {'download_id': identity})
        self.assertTrue(response.json['cancelled'])
        self.assertEqual(len(self.provider.calls), before)
        self.monitor.tick()
        self.assertEqual(self.downloads(), [])
        self.assertEqual(self.client.get('/admin/vibravid/queue').json['scheduled'], [])

    def test_whole_long_running_series_selection_fits_its_own_request_budget(self):
        self.provider.episodes = [{'season': 1, 'episode': episode} for episode in range(1, 701)]
        self.inspect()
        selected = [{'season': 1, 'episode': episode} for episode in range(1, 701)]
        self.assertGreater(len(json.dumps(selected)), 16 * 1024)
        result = self.start(selected)
        self.assertEqual(result.status_code, 200, result.data)
        self.assertEqual(result.json['selected'], 700)

    def test_inspection_can_change_source_but_stale_buttons_cannot_start_wrong_downloads(self):
        original = self.inspect()
        self.payload = {**self.payload, 'id': 'other-result'}
        current = self.inspect()
        self.assertEqual(original['series_id'], current['series_id'])
        self.assertNotEqual(original['source'], current['source'])
        stale = self.post('/admin/downloads/series', {'series_id': original['series_id'], 'source': original['source'],
                          'selection': [{'season': 1, 'episode': 1}], 'only_new': False})
        self.assertEqual(stale.status_code, 400)
        self.assertTrue(self.start(only_new=True).json['queued'])
        self.payload = {**self.payload, 'id': 'third-result'}
        response = self.post('/admin/vibravid/episodes', {'site': 'example', 'payload': self.payload})
        self.assertEqual(response.status_code, 400, 'an active source must never be silently switched')

    def test_following_is_removed_from_the_interface(self):
        for url in ('/admin', '/admin?section=library', '/admin?section=following'):
            page = self.client.get(url).data
            self.assertNotIn(b'>Following<', page)
            self.assertNotIn(b'following.js', page)
            self.assertNotIn(b'Notify only', page)

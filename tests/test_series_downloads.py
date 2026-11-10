import json
import html
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

    def season_action(self, action):
        with self.client.session_transaction() as session:
            csrf = session['csrf']
        return self.client.post('/admin/season', data={'csrf': csrf, 'series': 'Show', 'season': '1', 'action': action})

    def prepare_library_episodes(self):
        self.provider.episodes = [{'season': 1, 'episode': n, 'title': 'Provider episode ' + str(n)} for n in (1, 2, 3)]
        self.inspect()
        self.start(only_new=True)

    def test_library_lists_missing_episodes_without_creating_fake_media(self):
        self.prepare_library_episodes()
        self.video(1)
        calls = len(self.provider.calls)
        page = html.unescape(self.client.get('/admin?section=library').data.decode())
        self.assertEqual(len(self.provider.calls), calls, 'library requests must use cached provider metadata')
        self.assertIn('3 episodes · 5 B · 1 downloaded', page)
        self.assertIn('Downloaded', page)
        self.assertIn('Not downloaded', page)
        self.assertIn('Provider episode 2', page)
        self.assertIn('/admin/downloads/series/1/1/2', page)
        self.assertIn('Mark all as watched', page)
        self.assertIn('>Download all</button>', page)
        self.assertIn('Download new episodes', page)
        self.assertNotIn('>Auto download<', page)
        self.assertNotIn('Mark season watched &amp; exclude', page)
        with connect(self.database) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM media').fetchone()[0], 1)
        self.assertIn('Provider episode 3', self.client.get('/admin?section=library&q=Provider+episode+3').data.decode())
        self.assertNotIn('Provider episode 3', self.client.get('/admin?section=removed').data.decode())

    def test_library_shows_series_with_no_downloaded_episodes(self):
        self.prepare_library_episodes()
        page = self.client.get('/admin?section=library').data.decode()
        self.assertIn('Show', page)
        self.assertIn('3 episodes · 0 downloaded', page)
        self.assertNotRegex(page, r'(?<!\d)0 B')
        self.assertNotIn('Your collection starts here', page)

    def test_library_download_button_queues_exact_episode_idempotently(self):
        self.prepare_library_episodes()
        url = '/admin/downloads/series/1/1/2'
        self.assertEqual(self.client.post(url, json={}).status_code, 403)
        self.assertTrue(self.post(url, {}).json['queued'])
        self.assertTrue(self.post(url, {}).json['queued'])
        page = self.client.get('/admin?section=library').data.decode()
        self.assertIn('Queued / downloading', page)
        self.monitor.tick()
        self.assertEqual([body['episodes'] for body in self.downloads()], ['2'])
        self.video(2)
        self.assertEqual(self.post(url, {}).status_code, 400)
        other = self.base / 'other'
        other.mkdir()
        with connect(self.database) as db:
            db.execute("UPDATE settings SET value=? WHERE key='videos_path'", (str(other),))
        self.assertEqual(self.post('/admin/downloads/series/1/1/3', {}).status_code, 400)

    def test_mark_all_watched_keeps_download_policy_and_future_episodes_independent(self):
        self.prepare_library_episodes()
        self.video(1)
        self.assertEqual(self.season_action('watched').status_code, 302)
        with connect(self.database) as db:
            self.assertEqual(tuple(db.execute('SELECT watched,excluded FROM season_state').fetchone()), (0, 0))
            self.assertEqual([row[0] for row in db.execute('SELECT watched FROM discoveries')], [1, 1, 1])
            self.assertEqual(db.execute('SELECT watched FROM progress').fetchone()[0], 1)
        self.provider.episodes.append({'season': 1, 'episode': 4})
        self.check()
        self.assertEqual([body['episodes'] for body in self.downloads()], ['4'], 'marking known episodes watched must not mark future releases watched')
        self.assertIn('Mark all as watched', self.client.get('/admin?section=library').data.decode())
        self.season_action('exclude')
        self.season_action('watched')
        self.season_action('unwatched')
        with connect(self.database) as db:
            self.assertEqual(db.execute('SELECT excluded FROM season_state').fetchone()[0], 1)
            self.assertTrue(all(row[0] == 0 for row in db.execute('SELECT watched FROM discoveries')))
        page = html.unescape(self.client.get('/admin?section=library').data.decode())
        self.assertIn('>Download all</button>', page)
        self.assertNotIn('>Auto download<', page)

    def test_season_download_toggle_and_explicit_episode_override(self):
        self.prepare_library_episodes()
        self.season_action('exclude')
        self.provider.episodes.append({'season': 1, 'episode': 4})
        self.check()
        self.assertEqual(self.downloads(), [])
        self.assertTrue(self.post('/admin/downloads/series/1/1/2', {}).json['queued'])
        self.monitor.tick()
        self.assertEqual([body['episodes'] for body in self.downloads()], ['2'])
        self.season_action('include')
        self.monitor.tick()
        self.assertEqual([body['episodes'] for body in self.downloads()], ['2', '4'])
        page = html.unescape(self.client.get('/admin?section=library').data.decode())
        self.assertIn('>Download all</button>', page)
        self.assertNotIn('>Auto download<', page)

    def test_episode_history_survives_import_and_explicit_unwatched_updates_it(self):
        self.prepare_library_episodes()
        self.season_action('watched')
        self.video(2)
        with connect(self.database) as db:
            media_id = db.execute('SELECT id FROM media').fetchone()[0]
            self.assertEqual(db.execute('SELECT watched FROM progress').fetchone()[0], 1)
        self.post('/admin/media/' + str(media_id) + '/watched', {'watched': False})
        with connect(self.database) as db:
            self.assertEqual(db.execute('SELECT watched FROM discoveries WHERE episode=2').fetchone()[0], 0)

    def test_library_revision_changes_for_missing_episodes_and_queue_state(self):
        self.prepare_library_episodes()
        before = self.client.get('/api/catalog/ids').json
        self.provider.episodes.append({'season': 1, 'episode': 4})
        self.check()
        after = self.client.get('/api/catalog/ids').json
        self.assertEqual(before['ids'], after['ids'])
        self.assertNotEqual(before['revision'], after['revision'])
        self.post('/admin/downloads/series/1/1/2', {})
        self.assertNotEqual(after['revision'], self.client.get('/api/catalog/ids').json['revision'])

    def test_unavailable_and_uncertain_episodes_cannot_be_queued(self):
        self.prepare_library_episodes()
        with connect(self.database) as db:
            db.execute('UPDATE discoveries SET listed=0 WHERE episode=2')
            db.execute("UPDATE discoveries SET state='uncertain' WHERE episode=3")
        for episode in (2, 3):
            self.assertEqual(self.post('/admin/downloads/series/1/1/' + str(episode), {}).status_code, 400)
        page = self.client.get('/admin?section=library').data.decode()
        self.assertIn('Unavailable from provider', page)
        self.assertIn('Request unconfirmed', page)

    def test_manual_episode_download_can_proceed_during_lookup_error_backoff(self):
        self.prepare_library_episodes()
        with connect(self.database) as db:
            db.execute("UPDATE subscriptions SET error='Episode lookup failed',next_check=?", (int(time.time()) + 3600,))
        self.post('/admin/downloads/series/1/1/2', {})
        self.monitor.tick()
        self.assertEqual([body['episodes'] for body in self.downloads()], ['2'])

    def test_missing_episode_can_be_marked_watched_unwatched_and_remembers_on_import(self):
        self.prepare_library_episodes()
        url = '/admin/series/1/1/2/watched'
        page = self.client.get('/admin?section=library').data.decode()
        self.assertIn('data-watch-url="' + url + '"', page)
        self.assertEqual(self.client.post(url, json={'watched': True}).status_code, 403)
        self.assertEqual(self.post(url, {'watched': True}).json, {'watched': True})
        with connect(self.database) as db:
            self.assertEqual(db.execute('SELECT watched FROM discoveries WHERE episode=2').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM media').fetchone()[0], 0)
        self.assertEqual(self.post(url, {'watched': False}).json, {'watched': False})
        with connect(self.database) as db:
            self.assertEqual(db.execute('SELECT watched FROM discoveries WHERE episode=2').fetchone()[0], 0)
        self.post(url, {'watched': True})
        self.video(2)
        with connect(self.database) as db:
            self.assertEqual(db.execute('SELECT watched FROM progress').fetchone()[0], 1)
        self.assertEqual(self.post(url, {'watched': False}).status_code, 200, 'old controls remain safe if the file arrives before clicking')
        with connect(self.database) as db:
            self.assertEqual(db.execute('SELECT watched FROM progress').fetchone()[0], 0)

    def test_missing_episode_watch_validates_input_and_library_root(self):
        self.prepare_library_episodes()
        url = '/admin/series/1/1/2/watched'
        for body in ({}, {'watched': 1}, {'watched': 'true'}, []):
            self.assertEqual(self.post(url, body).status_code, 400)
        self.assertEqual(self.post('/admin/series/1/1/999/watched', {'watched': True}).status_code, 404)
        with connect(self.database) as db:
            db.execute("UPDATE settings SET value='/another-library' WHERE key='videos_path'")
        self.assertEqual(self.post(url, {'watched': True}).status_code, 404)

    def test_missing_episode_can_clear_inherited_legacy_season_history(self):
        self.prepare_library_episodes()
        self.video(1)
        with connect(self.database) as db:
            db.execute('INSERT INTO season_state(root,series,season,watched,excluded) VALUES(?,?,1,1,1)', (str(self.library), 'Show'))
        self.post('/admin/series/1/1/2/watched', {'watched': False})
        with connect(self.database) as db:
            self.assertEqual([row[0] for row in db.execute('SELECT watched FROM discoveries ORDER BY episode')], [1, 0, 1])
            self.assertEqual(tuple(db.execute('SELECT watched,excluded FROM season_state').fetchone()), (0, 1))
            self.assertEqual(db.execute('SELECT watched FROM progress').fetchone()[0], 1)

    def test_new_episode_checkbox_is_checked_only_in_latest_known_season(self):
        self.provider.episodes = [{'season': 1, 'episode': 1, 'title': 'Older episode'}, {'season': 2, 'episode': 1, 'title': 'Latest episode'}]
        self.inspect()
        self.start(only_new=True)
        page = self.client.get('/admin?section=library').data.decode()
        older = page.index('data-catalog-key="season:Show:1"')
        latest = page.index('data-catalog-key="season:Show:2"')
        self.assertNotIn('data-new-episodes-url', page[older:latest])
        self.assertEqual(page.count('data-new-episodes-url='), 1)
        self.assertIn('data-new-episodes-url="/admin/series/1/new-episodes" checked', page[latest:])
        self.assertIn('Download all', page)
        self.assertNotIn('>Auto download<', page)
        self.assertNotIn("Don&#39;t download", page)
        self.assertNotIn('data-new-episodes-url', self.client.get('/admin?section=library&q=Older').data.decode())

    def test_download_all_queues_only_missing_episodes_even_with_future_downloads_off(self):
        self.prepare_library_episodes()
        self.video(1)
        self.season_action('watched')
        self.post('/admin/series/1/new-episodes', {'enabled': False})
        self.assertEqual(self.season_action('download_all').status_code, 302)
        self.assertEqual(self.season_action('download_all').status_code, 302)
        with connect(self.database) as db:
            self.assertEqual([row[0] for row in db.execute("SELECT episode FROM discoveries WHERE state='manual_pending' ORDER BY episode")], [2, 3])
            self.assertEqual(db.execute('SELECT auto_new FROM subscriptions').fetchone()[0], 0)
        self.monitor.tick()
        self.assertEqual([body['episodes'] for body in self.downloads()], ['2', '3'])

    def test_checkbox_stays_off_in_new_seasons_and_enabling_does_not_catch_up(self):
        self.prepare_library_episodes()
        self.assertEqual(self.post('/admin/series/1/new-episodes', {'enabled': False}).json, {'enabled': False})
        self.provider.episodes.append({'season': 1, 'episode': 4})
        self.check()
        self.provider.episodes.append({'season': 2, 'episode': 1})
        self.check()
        self.assertEqual(self.downloads(), [])
        self.assertTrue([body for action, body in self.provider.calls if action == 'episodes'][-1]['latest_only'])
        page = self.client.get('/admin?section=library').data.decode()
        self.assertIn('data-new-episodes-url="/admin/series/1/new-episodes">', page)
        self.assertTrue(self.post('/admin/series/1/new-episodes', {'enabled': True}).json['enabled'])
        self.provider.episodes.append({'season': 2, 'episode': 2})
        self.check()
        self.assertEqual([(body['season'], body['episodes']) for body in self.downloads()], [('2', '2')])
        initialize(self.database)
        with connect(self.database) as db:
            self.assertEqual(db.execute('SELECT auto_new FROM subscriptions').fetchone()[0], 1)

    def test_future_download_setting_is_validated_scoped_and_idempotent(self):
        self.prepare_library_episodes()
        url = '/admin/series/1/new-episodes'
        self.assertEqual(self.client.post(url, json={'enabled': False}).status_code, 403)
        for body in ({}, [], {'enabled': 1}, {'enabled': 'false'}):
            self.assertEqual(self.post(url, body).status_code, 400)
        with connect(self.database) as db:
            db.execute("UPDATE discoveries SET state='new' WHERE episode=2")
        self.post(url, {'enabled': True})
        self.post(url, {'enabled': True})
        with connect(self.database) as db:
            self.assertEqual(db.execute('SELECT state FROM discoveries WHERE episode=2').fetchone()[0], 'new')
            db.execute("UPDATE settings SET value='/other-library' WHERE key='videos_path'")
        self.assertEqual(self.post(url, {'enabled': False}).status_code, 400)

    def test_download_all_does_not_retry_uncertain_or_unavailable_episodes(self):
        self.prepare_library_episodes()
        self.video(1)
        with connect(self.database) as db:
            db.execute("UPDATE discoveries SET state='uncertain' WHERE episode=2")
            db.execute('UPDATE discoveries SET listed=0 WHERE episode=3')
        self.assertEqual(self.season_action('download_all').status_code, 302)
        self.monitor.tick()
        self.assertEqual(self.downloads(), [])
        with connect(self.database) as db:
            self.assertEqual(db.execute('SELECT state FROM discoveries WHERE episode=2').fetchone()[0], 'uncertain')

    def test_manual_picker_download_preserves_disabled_future_preference(self):
        self.prepare_library_episodes()
        self.post('/admin/series/1/new-episodes', {'enabled': False})
        self.inspect()
        self.start([{'season': 1, 'episode': 2}])
        with connect(self.database) as db:
            self.assertEqual(db.execute('SELECT auto_new FROM subscriptions').fetchone()[0], 0)
        self.monitor.tick()
        self.assertEqual([body['episodes'] for body in self.downloads()], ['2'])
        self.provider.episodes.append({'season': 1, 'episode': 4})
        self.check()
        self.assertEqual([body['episodes'] for body in self.downloads()], ['2'])

    def test_missing_episode_rows_expand_only_when_they_have_body_content(self):
        self.prepare_library_episodes()
        self.video(1)
        with connect(self.database) as db:
            db.execute("UPDATE discoveries SET error='Provider failed',state='failed' WHERE episode=3")
        page = self.client.get('/admin?section=library').data.decode()
        plain = '<div class="catalog-item" data-catalog-key="episode:1:1:2">'
        self.assertIn(plain, page)
        row = page.split(plain, 1)[1].split('</div>', 1)[0]
        self.assertIn('catalog-row', row)
        self.assertIn('catalog-download', row)
        self.assertIn('catalog-watch', row)
        self.assertNotIn('catalog-chevron', row)
        self.assertNotIn('<summary', row)
        self.assertIn('<details class="catalog-item" data-catalog-key="episode:1:1:1">', page)
        self.assertIn('<details class="catalog-item" data-catalog-key="episode:1:1:3">', page)
        self.assertIn('Provider failed', page)

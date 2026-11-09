import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw, ImageFont

from myflix.app import create_app
from myflix.catalog import scan
from myflix.credits import Cancelled, analyze, command, text_region
from myflix.db import connect, initialize
from myflix.credits import fingerprint


class CreditsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.database = str(self.base / 'myflix.sqlite3')
        self.app = create_app({'TESTING': True, 'DATABASE': self.database})
        self.library = self.base / 'library'
        self.video = self.library / 'Show S02E20.mkv'
        self.video.write_bytes(b'example')
        os.utime(self.video, (time.time() - 120,) * 2)
        scan(self.database)
        with connect(self.database) as db:
            self.item = dict(db.execute('SELECT * FROM media').fetchone())
        self.cache = self.app.extensions['credits']
        self.stop = threading.Event()

    def tearDown(self):
        self.temp.cleanup()

    def test_cache_playback_contract_and_file_replacement(self):
        with patch.object(self.cache, 'tools', return_value=('ffmpeg', 'ffprobe')), \
                patch('myflix.credits.analyze', return_value=1319.5) as detector:
            self.assertTrue(self.cache.step(self.stop))
            self.assertFalse(self.cache.step(self.stop))
            self.assertEqual(detector.call_count, 1)
            self.assertEqual(self.cache.position(self.item), 1319.5)
            client = self.app.test_client()
            self.assertEqual(client.get(f'/api/playback/{self.item["id"]}').json['credits_start'], 1319.5)
            self.assertIn(b'data-credits-start="1319.5"', client.get(f'/watch/{self.item["id"]}').data)
            self.video.write_bytes(b'changed file')
            self.assertIsNone(self.cache.position(self.item))
            self.assertTrue(self.cache.step(self.stop))
            self.assertEqual(detector.call_count, 2)

    def test_inconclusive_results_cached_and_failures_retry(self):
        with patch.object(self.cache, 'tools', return_value=('f', 'p')), \
                patch('myflix.credits.analyze', return_value=None):
            self.assertTrue(self.cache.step(self.stop))
            self.assertFalse(self.cache.step(self.stop))
            self.assertIsNone(self.cache.position(self.item))
        self.video.write_bytes(b'another file')
        with patch.object(self.cache, 'tools', return_value=('f', 'p')), \
                patch('myflix.credits.analyze', side_effect=TimeoutError), self.assertLogs(level='ERROR'):
            self.assertTrue(self.cache.step(self.stop))
            self.assertFalse(self.cache.step(self.stop))
        with connect(self.database) as db:
            self.assertGreater(db.execute('SELECT retry_at FROM credits WHERE fingerprint=?',
                                          (fingerprint(self.video),)).fetchone()[0], time.time())

    def test_legacy_media_cache_migrates_without_losing_results(self):
        signature = fingerprint(self.video)
        with connect(self.database) as db:
            db.execute('DROP TABLE credits')
            db.execute('CREATE TABLE credits(media_id INTEGER PRIMARY KEY,fingerprint TEXT,position REAL,retry_at REAL)')
            db.execute('INSERT INTO credits VALUES(?,?,?,0)', (self.item['id'], signature, 1200))
        initialize(self.database)
        initialize(self.database)
        self.assertEqual(self.cache.position(self.item), 1200)

    def test_root_change_and_changed_file_discard_inflight_result(self):
        for change in ['root', 'file']:
            with self.subTest(change=change):
                def detect(*args):
                    if change == 'file':
                        self.video.write_bytes(b'new contents')
                    else:
                        with connect(self.database) as db:
                            db.execute("UPDATE settings SET value=? WHERE key='videos_path'", (str(self.base),))
                    return 100
                with patch.object(self.cache, 'tools', return_value=('f', 'p')), \
                        patch('myflix.credits.analyze', side_effect=detect):
                    self.cache.step(self.stop)
                with connect(self.database) as db:
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM credits').fetchone()[0], 0)
                    db.execute("UPDATE settings SET value=? WHERE key='videos_path'", (str(self.library),))

    def test_symlinks_outside_root_and_missing_mount_never_analyzed(self):
        self.video.unlink()
        outside = self.base / 'outside.mkv'
        outside.write_bytes(b'outside')
        self.video.symlink_to(outside)
        with patch('myflix.credits.analyze') as detector:
            self.assertFalse(self.cache.step(self.stop))
            self.assertIsNone(self.cache.position(self.item))
            detector.assert_not_called()
        self.video.unlink()
        self.library.rmdir()
        self.assertFalse(self.cache.step(self.stop))

    def test_shutdown_and_timeout_reap_child(self):
        group = command([sys.executable, '-c', 'import os; print(os.getpgrp())'], self.stop)
        self.assertEqual(int(group), os.getpgrp(), 'service shutdown must also reach native credits tools')
        with self.assertRaises(TimeoutError):
            command([sys.executable, '-c', 'import time; time.sleep(30)'], self.stop, timeout=.1)
        timer = threading.Timer(.1, self.stop.set)
        timer.start()
        try:
            with self.assertRaises(Cancelled):
                command([sys.executable, '-c', 'import time; time.sleep(30)'], self.stop)
        finally:
            timer.join()

    def test_missing_tools_and_shutdown_do_not_cache_failures(self):
        with patch.object(self.cache, 'tools', side_effect=RuntimeError('missing FFmpeg')):
            with self.assertRaises(RuntimeError):
                self.cache.step(self.stop)
        self.stop.set()
        with self.assertRaises(Cancelled):
            self.cache.step(self.stop)
        with connect(self.database) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM credits').fetchone()[0], 0)

    def detect(self, onset=None, *, duration=1380, visible=None):
        probes, tools_used, positions = [], [], {}

        def region(image):
            position = positions[str(image.filename)]
            found = visible(position) if visible else onset is not None and position >= onset
            return Image.new('L', (64, 36), 255) if found else None

        def fake_command(args, stop, timeout=60):
            tools_used.append(args[0])
            if args[0] == 'probe':
                return json.dumps({'format': {'duration': str(duration)}})
            if args[0] == 'ffmpeg':
                self.assertEqual(args[args.index('-frames:v') + 1], '1', 'each seek extracts only one frame')
                self.assertNotIn('-t', args, 'the search must never decode a sequence of frames')
                position = float(args[args.index('-ss') + 1])
                probes.append(position)
                positions[str(args[-1])] = position
                Image.new('RGB', (64, 36)).save(args[-1])
                return ''
            self.fail('Only FFmpeg and FFprobe may be invoked')

        with patch('myflix.credits.command', side_effect=fake_command), \
                patch('myflix.credits.text_region', side_effect=region):
            result = analyze(self.video, 'ffmpeg', 'probe', self.stop)
        return result, probes, tools_used

    def test_bisection_brackets_early_and_late_credits_with_three_second_tolerance(self):
        for onset in [100, 1100, 1200, 1240, 1319.4, 1340, 1375]:
            with self.subTest(onset=onset):
                result, probes, tools = self.detect(onset)
                self.assertEqual(probes[0], 1320)
                self.assertEqual(probes[1], 1290 if onset <= 1320 else 1350)
                self.assertGreaterEqual(result, onset)
                self.assertLessEqual(result - onset, 3)
                self.assertLessEqual(len(probes), 20)
                self.assertEqual(len(probes), len(set(probes)), 'already checked frames are reused')
                self.assertEqual(set(tools), {'ffmpeg', 'probe'})

    def test_no_text_transition_is_reported(self):
        for onset in [None, 0]:
            with self.subTest(onset=onset), self.assertLogs(level='INFO') as messages:
                result, probes, tools = self.detect(onset)
                self.assertIsNone(result)
                self.assertLessEqual(len(probes), 12)
                self.assertTrue(any('transition unavailable' in message for message in messages.output))
                self.assertEqual(set(tools), {'ffmpeg', 'probe'})

    def test_single_sample_of_text_is_not_rejected_by_extra_confirmation(self):
        result, probes, tools = self.detect(visible=lambda position: 1320 <= position < 1320.3)
        self.assertEqual(result, 1320)
        self.assertEqual(len(probes), 6)
        self.assertEqual(set(tools), {'ffmpeg', 'probe'})

    def test_short_or_invalid_media_never_seek_outside_the_video(self):
        result, probes, tools = self.detect(55, duration=60)
        self.assertGreaterEqual(result, 55)
        self.assertLessEqual(result - 55, 3)
        self.assertTrue(all(0 <= position <= 59.5 for position in probes))
        for duration in [0, 59, float('nan'), float('inf'), 8 * 86400]:
            result, probes, tools = self.detect(duration=duration)
            self.assertIsNone(result)
            self.assertEqual(probes, [])
            self.assertEqual(tools, ['probe'])

    def test_gold_and_dim_names_are_text_without_recognizing_words(self):
        for color in [(210, 170, 90), (165, 165, 165), (130, 60, 20), (40, 40, 40)]:
            with self.subTest(color=color):
                image = Image.new('RGB', (640, 360), 'black')
                ImageDraw.Draw(image).text((150, 140), 'SOME RANDOM NAMES',
                                          font=ImageFont.load_default(size=24), fill=color)
                self.assertIsNotNone(text_region(image))
        image = Image.new('RGB', (640, 360), 'white')
        ImageDraw.Draw(image).text((150, 140), 'SOME RANDOM NAMES',
                                  font=ImageFont.load_default(size=24), fill='black')
        self.assertIsNotNone(text_region(image))

    def test_text_gate_preserves_bright_credits_and_discards_solid_shapes(self):
        for color in ['black', 'white', (40, 80, 90)]:
            image = Image.new('RGB', (640, 360), color)
            self.assertIsNone(text_region(image))
        image = Image.new('RGB', (640, 360), (40, 80, 90))
        draw = ImageDraw.Draw(image)
        draw.rectangle((10, 10, 200, 60), fill='white')
        draw.ellipse((400, 10, 550, 90), fill='white')
        self.assertIsNone(text_region(image), 'solid scenery is not text')
        draw.text((150, 140), 'JAMES L BROOKS', font=ImageFont.load_default(size=24), fill='white')
        self.assertIsNotNone(text_region(image), 'a single line of names counts as text')
        draw.multiline_text((150, 140), 'Executive Producers\nJAMES L BROOKS\nMATT GROENING',
                            font=ImageFont.load_default(size=24), fill='white', spacing=8)
        crop = text_region(image)
        self.assertIsNotNone(crop)
        self.assertLess(crop.width * crop.height, image.width * image.height / 2)
        self.assertEqual(set(crop.get_flattened_data()), {0, 255})



if __name__ == '__main__':
    unittest.main()

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from myflix.engine_api import install_download_adapter


class DownloadAdapterTest(unittest.TestCase):
    def test_exact_episode_is_queued_without_scraping_all_seasons(self):
        def enqueue(site, payload, season, episodes, media_type, *, _metadata=None):
            if _metadata is None:
                raise TimeoutError("Fetching all 38 seasons exceeds the request budget")
            return "queued-job"

        bot = SimpleNamespace(_run_download_in_thread=Mock(side_effect=enqueue))
        original = bot._run_download_in_thread
        install_download_adapter(bot)
        payload = {"name": "I Simpson", "_audio_language": "it"}
        self.assertEqual(bot._run_download_in_thread(
            "streamingcommunity", payload, "1", "1", "Serie"), "queued-job")
        original.assert_called_once_with(
            "streamingcommunity", payload, "1", "1", "Serie", _metadata=[])

    def test_ranges_movies_and_explicit_metadata_keep_upstream_behavior(self):
        for season, episodes in (("1", "*"), ("1", "1-3"), ("1", "1,3"), (None, None)):
            with self.subTest(episodes=episodes):
                enqueue = Mock()
                bot = SimpleNamespace(_run_download_in_thread=enqueue)
                install_download_adapter(bot)
                bot._run_download_in_thread("example", {}, season, episodes, "Serie")
                enqueue.assert_called_once_with("example", {}, season, episodes, "Serie")
        enqueue = Mock()
        bot = SimpleNamespace(_run_download_in_thread=enqueue)
        install_download_adapter(bot)
        metadata = [object()]
        bot._run_download_in_thread("example", {}, "1", "2", _metadata=metadata)
        self.assertIs(enqueue.call_args.kwargs["_metadata"], metadata)

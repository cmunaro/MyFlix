"""Progress observation does not alter provider results or share scraper state."""
from types import SimpleNamespace
import unittest

from myflix.engine_api import observe_metadata


class MetadataProgressTest(unittest.TestCase):
    def test_seasons_report_actual_completion_and_preserve_provider_results(self):
        original_cache = {"existing": object()}

        class API:
            _scraper_cache = original_cache

            def set_cached_scraper(self, media, scraper):
                self._scraper_cache[media] = scraper

        episodes = {1: ["one", "two"], 3: ["three"]}
        scraper = SimpleNamespace(
            seasons_manager=SimpleNamespace(seasons=[1, 3]),
            getNumberSeason=lambda: 2,
            getEpisodeSeasons=lambda number: episodes[number],
        )
        updates = []
        api = API()
        observe_metadata(api, lambda *args: updates.append(args))
        api.set_cached_scraper("series", scraper)
        self.assertEqual(scraper.getNumberSeason(), 2)
        self.assertIs(scraper.getEpisodeSeasons(1), episodes[1])
        self.assertEqual(updates[-1], (1, 2, 2, 0))
        self.assertIs(scraper.getEpisodeSeasons(3), episodes[3])
        self.assertEqual(updates, [(0, 2, 0, 0), (0, 2, 0, 1), (1, 2, 2, 0),
                                   (1, 2, 2, 3), (2, 2, 3, 0)])
        self.assertEqual(list(original_cache), ["existing"])
        # A cached season read does not inflate completed season counts.
        scraper.getEpisodeSeasons(1)
        self.assertEqual(updates[-1], (2, 2, 3, 0))

    def test_failed_season_never_counts_as_completed(self):
        def fail(number):
            raise OSError("provider unavailable")

        api = SimpleNamespace(set_cached_scraper=lambda *args: None)
        scraper = SimpleNamespace(seasons_manager=SimpleNamespace(seasons=[1]),
                                  getNumberSeason=lambda: 1, getEpisodeSeasons=fail)
        updates = []
        observe_metadata(api, lambda *args: updates.append(args))
        api.set_cached_scraper("series", scraper)
        scraper.getNumberSeason()
        with self.assertRaises(OSError):
            scraper.getEpisodeSeasons(1)
        self.assertEqual(updates[-1], (0, 1, 0, 1))

    def test_providers_without_season_hooks_keep_working(self):
        api = SimpleNamespace(set_cached_scraper=lambda *args: None)
        scraper = object()
        updates = []
        observe_metadata(api, lambda *args: updates.append(args))
        api.set_cached_scraper("series", scraper)
        self.assertEqual(updates, [])

    def test_latest_season_skips_older_episode_requests_and_discovers_new_seasons(self):
        for numbers in ([0, 1, 3], [0, 1, 3, 4]):
            with self.subTest(numbers=numbers):
                requested = []
                scraper = SimpleNamespace(
                    seasons_manager=SimpleNamespace(seasons=[]),
                    getEpisodeSeasons=lambda number: requested.append(number) or ["episode"],
                )
                def count():
                    scraper.seasons_manager.seasons[:] = [SimpleNamespace(number=n) for n in numbers]
                    return len(numbers)
                scraper.getNumberSeason = count
                api = SimpleNamespace(set_cached_scraper=lambda *args: None)
                updates = []
                observe_metadata(api, lambda *args: updates.append(args), latest_only=True)
                api.set_cached_scraper("show", scraper)
                scraper.getNumberSeason()
                for season in scraper.seasons_manager.seasons:
                    scraper.getEpisodeSeasons(season.number)
                self.assertEqual(requested, [max(numbers)])
                self.assertEqual(updates[-1], (1, 1, 1, 0))

    def test_initial_lookup_keeps_all_seasons_and_latest_scope_is_request_local(self):
        original = {"shared": object()}
        api = SimpleNamespace(_scraper_cache=original, set_cached_scraper=lambda *args: None)
        numbers = [SimpleNamespace(number=1), SimpleNamespace(number=2)]
        scraper = SimpleNamespace(seasons_manager=SimpleNamespace(seasons=numbers[:]),
                                  getNumberSeason=lambda: 2, getEpisodeSeasons=lambda n: [n])
        observe_metadata(api, lambda *args: None)
        api.set_cached_scraper("show", scraper)
        scraper.getNumberSeason()
        self.assertEqual([s.number for s in scraper.seasons_manager.seasons], [1, 2])
        self.assertEqual(list(original), ["shared"])

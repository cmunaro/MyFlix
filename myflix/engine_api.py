"""MyFlix's authenticated adapter to the pinned engine's episode metadata."""

import logging
import os

from .db import connect


def install_download_adapter(bot):
    """Exact episodes need no all-season expansion before queue registration."""
    enqueue = bot._run_download_in_thread

    def download(site, payload, season=None, episodes=None, *args, **kwargs):
        if str(season or "").isdigit() and str(episodes or "").isdigit():
            # The pinned helper otherwise scrapes every season synchronously,
            # exceeding the HTTP budget before registering even one episode.
            # The download worker still resolves and validates the actual media.
            kwargs.setdefault("_metadata", [])
        return enqueue(site, payload, season, episodes, *args, **kwargs)

    bot._run_download_in_thread = download


def report_metadata(database, token, done, total, episodes, season=0):
    """Only the current live lookup may publish progress to its subscription."""
    with connect(database) as db:
        db.execute("""UPDATE subscriptions SET lookup_seasons_done=?,lookup_seasons_total=?,
            lookup_episodes=?,lookup_season=? WHERE lookup_token=? AND lookup_state='fetching'""",
            (done, total, episodes, season, token))


def observe_metadata(api, report):
    """Observe the pinned providers' scraper calls, without changing their results."""
    # get_api returns a fresh instance. Keep instrumented scrapers private to this
    # request so simultaneous downloads never inherit progress callbacks.
    api._scraper_cache = {}
    cache = api.set_cached_scraper

    def attach(media, scraper):
        cache(media, scraper)
        count_seasons = getattr(scraper, "getNumberSeason", None)
        get_episodes = getattr(scraper, "getEpisodeSeasons", None)
        if not callable(count_seasons) or not callable(get_episodes):
            return
        counts = {}

        def total():
            return len(scraper.seasons_manager.seasons)

        def seasons():
            result = count_seasons()
            report(0, total(), 0, 0)
            return result

        def episodes(number):
            season = number if type(number) is int and 0 <= number <= 999 else 0
            report(len(counts), total(), sum(counts.values()), season)
            result = get_episodes(number)
            counts[number] = len(result or [])
            report(len(counts), total(), sum(counts.values()), 0)
            return result

        scraper.getNumberSeason = seasons
        scraper.getEpisodeSeasons = episodes

    api.set_cached_scraper = attach


def install():
    from django.http import JsonResponse
    from django.urls import path
    from django.views.decorators.csrf import csrf_exempt
    from django.views.decorators.http import require_POST
    from searchapp.api import get_api
    from searchapp.api.base import Entries
    from searchapp import api_bot
    from searchapp.api_bot import _json_body, require_bot_secret, bot_download
    from webgui.urls import urlpatterns

    install_download_adapter(api_bot)

    @csrf_exempt
    @require_bot_secret
    @require_POST
    def episodes(request):
        try:
            body = _json_body(request)
            api = get_api(body["site"])
            payload = body["payload"]
            media = Entries(**{k: v for k, v in payload.items() if k in Entries.__dataclass_fields__})
            from VibraVid.core.utils.audio_selection import audio_language_context, filter_series_audio
            language = payload.get("_audio_language")
            token = body.get("lookup_token")
            if token is not None:
                if not isinstance(token, str) or len(token) != 32 or any(c not in "0123456789abcdef" for c in token):
                    raise ValueError("Invalid lookup token")
                observe_metadata(api, lambda *progress: report_metadata(os.environ["MYFLIX_DATABASE"], token, *progress))
            else:
                api._scraper_cache = {}
            with audio_language_context(language):
                seasons = filter_series_audio(api.get_series_metadata(media) or [], language)
            return JsonResponse({"episodes": [
                {"season": s.number, "episode": e.number, "title": e.name or ""}
                for s in seasons for e in s.episodes]})
        except Exception:
            logging.exception("Episode metadata lookup failed")
            return JsonResponse({"error": "Episode metadata unavailable"}, status=502)

    urlpatterns.insert(0, path("api/myflix/episodes/", episodes))

    @csrf_exempt
    @require_bot_secret
    @require_POST
    def retry_download(request):
        from searchapp import _download_infra
        with _download_infra.download_executor.prioritize():
            return bot_download(request)

    urlpatterns.insert(0, path("api/myflix/retry-download/", retry_download))

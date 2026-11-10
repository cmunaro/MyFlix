"""MyFlix's authenticated adapter to the pinned engine's episode metadata."""

import logging
import os

from .db import connect
from .metadata import description, text, title_details


def provider_metadata(api, media, numbers):
    """Use provider data only; never invoke the engine's external metadata providers."""
    raw = media.raw_data if isinstance(media.raw_data, dict) else {}
    details = title_details(dict(raw, year=media.year or raw.get("year")))
    scraper = None
    if api.site_name == "streamingcommunity":
        from VibraVid.services.streamingcommunity.scrapper import GetSerieInfo
        language = media.provider_language or "it"
        scraper = GetSerieInfo(url=f"{api.base_url}{language}", media_id=media.id,
                               series_name=media.slug, year=media.year, provider_language=language,
                               series_display_name=media.name, languages=[language])
        scraper.collect_info_title()
    elif not media.is_movie and callable(getattr(api, "_build_scraper", None)):
        scraper = api._build_scraper(media)
        if scraper is not None:
            scraper.getNumberSeason()
    if scraper is not None:
        info = getattr(scraper, "title_info", None)
        if isinstance(info, dict):
            for key, value in title_details(info).items():
                if value:
                    details[key] = value
        details["seasons"] = {}
        available = {season.number: season for season in scraper.seasons_manager.seasons}
        for number in numbers:
            season = available.get(number)
            if season is None:
                continue
            episodes = scraper.getEpisodeSeasons(number) or []
            descriptions = {str(row.get("number")): description(row) for row in
                            (info.get("seasons", []) if isinstance(info, dict) else []) if isinstance(row, dict)}
            episode_details = {}
            for index, episode in enumerate(episodes, 1):
                raw_episode = episode if isinstance(episode, dict) else vars(episode)
                mapper = getattr(api, "_map_episode", None)
                mapped = mapper(episode, index) if callable(mapper) else episode
                if type(mapped.number) is int and 0 <= mapped.number <= 9999:
                    episode_details[str(mapped.number)] = {
                        "title": text(getattr(mapped, "name", ""), 300),
                        "synopsis": description(raw_episode)}
            details["seasons"][str(number)] = {
                "title": text(getattr(season, "name", ""), 300),
                "synopsis": description(vars(season)) or descriptions.get(str(number), ""),
                "episodes": episode_details}
    return details


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


def observe_metadata(api, report, *, latest_only=False):
    """Observe provider progress and optionally restrict iteration to its latest season."""
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
            if latest_only and scraper.seasons_manager.seasons:
                available = scraper.seasons_manager.seasons
                if any(type(season.number) is not int or not 0 <= season.number <= 999 for season in available):
                    raise ValueError("Provider season numbering is unsupported")
                latest = max(season.number for season in available)
                available[:] = [season for season in available if season.number == latest]
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
    def discovery(request):
        from .streamingcommunity import FEEDS, fetch
        body = _json_body(request)
        name = body.get("feed")
        if not isinstance(name, str) or name not in FEEDS:
            return JsonResponse({"error": "Invalid discovery feed"}, status=400)
        try:
            return JsonResponse({"items": fetch(get_api("streamingcommunity"), name)})
        except Exception:
            logging.exception("StreamingCommunity discovery unavailable")
            return JsonResponse({"error": "Discovery unavailable"}, status=502)

    urlpatterns.insert(0, path("api/myflix/discovery/", discovery))

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
            latest_only = body.get("latest_only", False)
            if type(latest_only) is not bool:
                raise ValueError("Invalid episode lookup scope")
            report = lambda *progress: None
            if token is not None:
                if not isinstance(token, str) or len(token) != 32 or any(c not in "0123456789abcdef" for c in token):
                    raise ValueError("Invalid lookup token")
                report = lambda *progress: report_metadata(os.environ["MYFLIX_DATABASE"], token, *progress)
            observe_metadata(api, report, latest_only=latest_only)
            scope = {}
            with audio_language_context(language):
                seasons = api.get_series_metadata(media) or []
                if latest_only:
                    # Single-season providers expose their episode list directly,
                    # without the common scraper hooks used by multi-season sites.
                    latest = max((season.number for season in seasons), default=None)
                    seasons = [season for season in seasons if season.number == latest]
                    scope["checked_seasons"] = [latest] if latest is not None else []
                seasons = filter_series_audio(seasons, language)
            return JsonResponse({**scope, "episodes": [
                {"season": s.number, "episode": e.number, "title": e.name or ""}
                for s in seasons for e in s.episodes]})
        except Exception:
            logging.exception("Episode metadata lookup failed")
            return JsonResponse({"error": "Episode metadata unavailable"}, status=502)

    urlpatterns.insert(0, path("api/myflix/episodes/", episodes))

    @csrf_exempt
    @require_bot_secret
    @require_POST
    def metadata(request):
        try:
            body = _json_body(request)
            numbers = body.get("seasons", [])
            if (not isinstance(numbers, list) or len(numbers) > 100
                    or any(type(number) is not int or not 0 <= number <= 9999 for number in numbers)):
                return JsonResponse({"error": "Invalid seasons"}, status=400)
            api = get_api(body["site"])
            api._scraper_cache = {}
            payload = body["payload"]
            media = Entries(**{k: v for k, v in payload.items() if k in Entries.__dataclass_fields__})
            return JsonResponse({"details": provider_metadata(api, media, numbers)})
        except Exception:
            logging.exception("Provider title metadata lookup failed")
            return JsonResponse({"error": "Title metadata unavailable"}, status=502)

    urlpatterns.insert(0, path("api/myflix/metadata/", metadata))

    @csrf_exempt
    @require_bot_secret
    @require_POST
    def retry_download(request):
        from searchapp import _download_infra
        with _download_infra.download_executor.prioritize():
            return bot_download(request)

    urlpatterns.insert(0, path("api/myflix/retry-download/", retry_download))

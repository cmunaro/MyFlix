"""MyFlix's authenticated adapter to the pinned engine's episode metadata."""


def install():
    from django.http import JsonResponse
    from django.urls import path
    from django.views.decorators.csrf import csrf_exempt
    from django.views.decorators.http import require_POST
    from searchapp.api import get_api
    from searchapp.api.base import Entries
    from searchapp.api_bot import _json_body, require_bot_secret, bot_download
    from webgui.urls import urlpatterns

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
            api._scraper_cache.pop(api._get_cache_key(media), None)
            with audio_language_context(language):
                seasons = filter_series_audio(api.get_series_metadata(media) or [], language)
            return JsonResponse({"episodes": [
                {"season": s.number, "episode": e.number, "title": e.name or ""}
                for s in seasons for e in s.episodes]})
        except Exception:
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

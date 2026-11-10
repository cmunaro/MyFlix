"""StreamingCommunity discovery through the bundled engine's configured provider."""
import json
import logging
import re
from urllib.parse import urljoin, urlsplit


FEEDS = {
    "sc_top_movie": ("movies", "top10", 10),
    "sc_top_tv": ("tv-shows", "top10", 10),
    "sc_trending": ("", "trending", 20),
}


def normalize(raw, base):
    if not isinstance(raw, dict):
        return None
    identity, title, kind, slug = (raw.get(key) for key in ("id", "name", "type", "slug"))
    if (type(identity) is not int or not 0 < identity < 2**63 or kind not in ("tv", "movie")
            or not isinstance(title, str) or not title.strip() or not isinstance(slug, str)
            or not re.fullmatch(r"[a-z0-9-]{1,200}", slug)):
        return None
    translations = raw.get("translations") if isinstance(raw.get("translations"), list) else []
    translated = {row.get("key"): row.get("value") for row in translations
                  if isinstance(row, dict) and row.get("locale") == "it" and isinstance(row.get("value"), str)}
    images = raw.get("images") if isinstance(raw.get("images"), list) else []

    def image(types):
        for kind in types:
            for row in images:
                filename = row.get("filename") if isinstance(row, dict) else None
                if (isinstance(row, dict) and row.get("type") == kind and isinstance(filename, str)
                        and re.fullmatch(r"[A-Za-z0-9_-]{1,150}\.(?:jpg|jpeg|png|webp)", filename)):
                    return base.replace("https://", "https://cdn.", 1) + "/images/" + filename
        return None

    trailers = raw.get("trailers") if isinstance(raw.get("trailers"), list) else []
    trailer = next((row["youtube_id"] for row in trailers
                    if isinstance(row, dict) and isinstance(row.get("youtube_id"), str)
                    and re.fullmatch(r"[A-Za-z0-9_-]{11}", row["youtube_id"])), "")
    overview = translated.get("plot") or raw.get("plot") or ""
    return dict(id=identity, kind=kind, title=title.strip()[:200], series="", discovery=True,
                source="streamingcommunity", source_url=f"{base}/it/titles/{identity}-{slug}",
                poster=image(("cover", "poster")), backdrop=image(("background", "cover")),
                trailer=trailer, overview=overview[:2000] if isinstance(overview, str) else "", release="")


def fetch(api, name):
    """Runs inside VibraVid. Only fixed discovery pages and validated title paths."""
    from bs4 import BeautifulSoup
    from VibraVid.utils.http_client import create_client, get_userAgent

    page, slider, limit = FEEDS[name]
    base = api.base_url.rstrip("/")
    parts = urlsplit(base)
    if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password
            or parts.port or parts.path or parts.query or parts.fragment):
        raise ValueError("Invalid StreamingCommunity provider origin")

    with create_client(headers={"user-agent": get_userAgent()}) as client:
        def load(path):
            url = base + path
            for _ in range(4):
                response = client.get(url, timeout=10, allow_redirects=False)
                if response.status_code not in (301, 302, 303, 307, 308):
                    break
                redirected = urlsplit(urljoin(url, response.headers.get("Location", "")))
                if (redirected.scheme, redirected.netloc) != (parts.scheme, parts.netloc):
                    raise ValueError("Discovery redirect left provider origin")
                url = redirected.geturl()
            if response.status_code != 200:
                raise ValueError("Discovery page unavailable")
            response.raise_for_status()
            if len(response.content) > 8 * 1024 * 1024:
                raise ValueError("Discovery page too large")
            node = BeautifulSoup(response.text, "html.parser").find("div", id="app")
            if node is None:
                raise ValueError("Discovery page data missing")
            return json.loads(node["data-page"])["props"]

        props = load("/it" + ("/" + page if page else ""))
        titles = next((row["titles"] for row in props["sliders"] if row.get("name") == slider), None)
        if not isinstance(titles, list):
            raise ValueError("StreamingCommunity discovery shelf missing")
        items, seen = [], set()
        for raw in titles:
            item = normalize(raw, base)
            if (not item or item["id"] in seen
                    or (name == "sc_top_movie" and item["kind"] != "movie")
                    or (name == "sc_top_tv" and item["kind"] != "tv")):
                continue
            seen.add(item["id"])
            try:
                detail = load(urlsplit(item["source_url"]).path)["title"]
                enriched = normalize(dict(raw, plot=detail.get("plot"), trailers=detail.get("trailers") or []), base)
                if enriched:
                    item = enriched
            except Exception:
                logging.warning("StreamingCommunity trailer unavailable for title %s", item["id"])
            items.append(item)
            if len(items) == limit:
                break
    if titles and not items:
        raise ValueError("StreamingCommunity shelf contains no valid titles")
    return items

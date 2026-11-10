"""Server-side VibraVid API boundary. Never exposes its token to the browser."""
import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class VibraVidError(Exception):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class VibraVid:
    READ = {"sites": "api/bot/sites/", "status": "api/bot/status/",
            "queue": "api/get-downloads/?history=30"}
    WRITE = {name: f"api/bot/{name}/" for name in ("search", "seasons", "download", "cancel")}
    INTERNAL = {"episodes": "api/myflix/episodes/", "metadata": "api/myflix/metadata/", "discovery": "api/myflix/discovery/",
                "retry_download": "api/myflix/retry-download/"}

    def __init__(self, address, secret):
        parts = urlsplit(address)
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError("Invalid server-side VibraVid address")
        self.address = address.rstrip("/") + "/"
        self.secret = secret
        self.instance_id = ""

    def call(self, action, body=None):
        if not self.secret:
            raise VibraVidError("The bundled download engine is not ready. Start MyFlix with mise run dev.")
        routes = self.READ if body is None else {**self.WRITE, **self.INTERNAL}
        if action not in routes:
            raise ValueError("Unsupported VibraVid operation")
        request = Request(self.address + routes[action],
                          data=None if body is None else json.dumps(body).encode(),
                          headers={"Content-Type": "application/json", "X-VibraVid-Token": self.secret})
        try:
            # Episode metadata loads every season, which can exceed the normal
            # request budget for long-running series on a Raspberry Pi.
            # Discovery also enriches a bounded shelf with provider title pages.
            timeout = 300 if action in ("episodes", "metadata", "discovery") else 30
            with build_opener(NoRedirect()).open(request, timeout=timeout) as response:
                raw = response.read(4 * 1024 * 1024 + 1)
            if len(raw) > 4 * 1024 * 1024:
                raise ValueError("Response too large")
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise ValueError("Invalid response")
            return result
        except HTTPError as error:
            if error.code in (401, 403):
                raise VibraVidError("VibraVid rejected authentication. Check the server's shared secret.") from error
            raise VibraVidError(f"VibraVid returned HTTP {error.code}. Check its service logs before retrying.") from error
        except (URLError, OSError, ValueError) as error:
            if action == "episodes" and (isinstance(error, TimeoutError)
                    or isinstance(getattr(error, "reason", None), TimeoutError)):
                raise VibraVidError(
                    "Episode lookup timed out. Try loading the episode list again."
                ) from error
            # A timed-out download request may already have queued work. Never
            # retry mutations automatically or expose upstream response content.
            raise VibraVidError("VibraVid is unavailable or returned an invalid response. Check the queue before retrying a download.") from error

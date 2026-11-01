import hmac
import math
import os
import re
import secrets
import threading
from pathlib import Path
from datetime import datetime, timezone

from flask import Flask, abort, jsonify, redirect, render_template, request, send_file, session, url_for
from werkzeug.exceptions import HTTPException

from .artwork import ArtworkCache
from .catalog import media_path, scan_safely
from .db import connect, initialize, settings
from .discovery import DiscoveryCache
from .engine_jobs import validate_limits
from .monitoring import SeriesMonitor, episode_status, follow, lookup_status
from .vibravid import VibraVid, VibraVidError


def create_app(config=None):
    app = Flask(__name__)
    data = Path(os.environ.get("MYFLIX_DATA", "data")).resolve()
    app.config.update(DATABASE=str(data / "myflix.sqlite3"),
                      VIBRAVID_ADDRESS="http://127.0.0.1:0",
                      MAX_CONTENT_LENGTH=16 * 1024,
                      SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Strict")
    if config:
        app.config.update(config)
    database = app.config["DATABASE"]
    Path(database).parent.mkdir(parents=True, exist_ok=True)
    initialize(database)
    Path(database).chmod(0o600)
    with connect(database) as db:
        db.execute("INSERT OR IGNORE INTO settings VALUES('session_secret',?)", (secrets.token_hex(32),))
        db.execute("INSERT OR IGNORE INTO settings VALUES('vibravid_secret',?)", (secrets.token_hex(32),))
        stored = settings(db)
        app.secret_key = stored["session_secret"]
        app.config.setdefault("VIBRAVID_SECRET", stored["vibravid_secret"])
        if not stored.get("videos_path"):
            library = Path(database).parent / "library"
            library.mkdir(exist_ok=True)
            db.execute("INSERT OR REPLACE INTO settings VALUES('videos_path',?)", (str(library),))
    scan_lock = threading.Lock()

    def run_scan():
        if not scan_lock.acquire(blocking=False):
            abort(409, "A library scan is already running")
        try:
            return scan_safely(database)
        finally:
            scan_lock.release()

    app.extensions["scan"] = run_scan
    vibravid = VibraVid(app.config["VIBRAVID_ADDRESS"], app.config["VIBRAVID_SECRET"])
    app.extensions["vibravid"] = vibravid
    monitor = SeriesMonitor(database, vibravid)
    app.extensions["monitor"] = monitor
    artwork_cache = ArtworkCache(database, vibravid)
    app.extensions["artwork"] = artwork_cache
    discovery_cache = DiscoveryCache(database)
    app.extensions["discovery"] = discovery_cache

    @app.template_filter("datetime")
    def format_datetime(value):
        return datetime.fromtimestamp(value, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    @app.before_request
    def protect():
        if "csrf" not in session:
            session["csrf"] = secrets.token_hex(24)
        if request.method == "POST":
            token = request.headers.get("X-CSRF-Token", "") or request.form.get("csrf", "")
            if not hmac.compare_digest(token.encode(), session["csrf"].encode()):
                abort(403, "Session expired. Reload the page and try again.")

    @app.after_request
    def headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self'; script-src 'self'; media-src 'self'; img-src 'self'; frame-ancestors 'none'; form-action 'self'"
        if request.endpoint in ("home", "discover"):
            response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self'; script-src 'self' https://www.youtube.com https://s.ytimg.com; media-src 'self'; img-src 'self' https://simkl.in; frame-src https://www.youtube-nocookie.com; frame-ancestors 'none'; form-action 'self'"
            # YouTube embeds require an origin referrer, including on a LAN origin.
            response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        if not request.path.startswith(("/static/", "/media/")):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.errorhandler(HTTPException)
    def http_error(error):
        if request.is_json or request.path.startswith(("/api/", "/admin/vibravid/")):
            return jsonify(error=error.description), error.code
        return render_template("error.html", message=error.description), error.code

    @app.errorhandler(ValueError)
    def invalid(error):
        if request.is_json or request.path.startswith(("/api/", "/admin/vibravid/")):
            return jsonify(error=str(error)), 400
        return render_template("error.html", message=str(error)), 400

    def get_media(media_id):
        with connect(database) as db:
            item = db.execute("""SELECT m.*,COALESCE(p.position,0) position,
                COALESCE(p.duration,0) duration,COALESCE(p.watched,0) watched
                FROM media m LEFT JOIN progress p ON p.media_id=m.id
                WHERE m.id=? AND m.hidden=0 AND m.available=1 AND m.root=?""",
                (media_id, settings(db).get("videos_path", ""))).fetchone()
        if not item:
            abort(404)
        return dict(item)

    def decorate(item):
        item["poster"] = url_for("art", media_id=item["id"], kind="poster") if artwork_cache.path(item, "poster") else None
        item["backdrop"] = url_for("art", media_id=item["id"], kind="backdrop") if artwork_cache.path(item, "backdrop") else None
        item["href"] = url_for("series", media_id=item["id"]) if item["series"] else url_for("watch", media_id=item["id"])
        return item

    @app.get("/art/<int:media_id>/<any(poster,backdrop):kind>")
    def art(media_id, kind):
        path = artwork_cache.path(get_media(media_id), kind)
        if not path:
            abort(404)
        return send_file(path, conditional=True, mimetype="image/png" if path.suffix == ".png" else "image/jpeg")

    @app.get("/")
    def home():
        query = request.args.get("q", "").strip()[:200]
        view = request.args.get("view", "all")
        with connect(database) as db:
            rows = db.execute("""SELECT m.*,COALESCE(p.position,0) position,
                COALESCE(p.duration,0) duration,COALESCE(p.watched,0) watched,
                COALESCE(p.updated_at,'') progress_updated_at
                FROM media m LEFT JOIN progress p ON p.media_id=m.id
                WHERE m.hidden=0 AND m.available=1 AND m.root=?
                ORDER BY m.series COLLATE NOCASE,m.season,m.episode,m.title COLLATE NOCASE""",
                (settings(db).get("videos_path", ""),)).fetchall()
        rows = [dict(row) for row in rows if query.casefold() in (row["title"] + " " + row["series"]).casefold()]
        if view == "movies":
            rows = [r for r in rows if not r["series"]]
        elif view == "series":
            rows = [r for r in rows if r["series"]]
        elif view == "continue":
            rows = [r for r in rows if r["position"] > 0 and not r["watched"]]
        rows = [decorate(row) for row in rows]
        continuing = []
        continuing_series = set()
        for row in sorted(rows, key=lambda row: (row["progress_updated_at"], row["id"]), reverse=True):
            if row["position"] <= 0 or row["watched"] or (row["series"] and row["series"] in continuing_series):
                continue
            if row["series"]:
                continuing_series.add(row["series"])
            continuing.append(row.copy())
        for item in continuing:
            item["href"] = url_for("watch", media_id=item["id"])
        shows = {}
        for row in rows:
            if row["series"]:
                if row["series"] not in shows or (shows[row["series"]]["watched"] and not row["watched"]):
                    shows[row["series"]] = row
        films = [row for row in rows if not row["series"]]
        titles = films + list(shows.values())
        shelves = []
        if continuing:
            shelves.append(("Continue watching", continuing))
        not_watched = [title for title in titles
                       if not title["watched"] and title["series"] not in continuing_series]
        if not_watched and view != "continue":
            shelves.append(("Not watched", not_watched))
        if shows and view != "continue":
            shelves.append(("Series", list(shows.values())))
        if films and view != "continue":
            shelves.append(("Film", films))
        featured = continuing[0] if continuing else (max(titles, key=lambda r: (r["added_at"], r["id"])) if titles else None)
        discovery_shelves = []
        discovery_notice = ""
        if not query and not request.args.get("search") and view != "continue":
            feeds = discovery_cache.read()
            today = datetime.now(timezone.utc).date().isoformat()
            for name, label in (("popular_tv", "Most watched series this week on SIMKL"),
                                ("popular_movie", "Most watched films this week on SIMKL"),
                                ("upcoming_tv", "Upcoming series & seasons · SIMKL")):
                if (view == "movies" and name.endswith("tv")) or (view == "series" and name.endswith("movie")):
                    continue
                items = [dict(item, href=url_for("discover", kind=item["kind"], identity=item["id"]))
                         for item in feeds.get(name, {}).get("items", [])
                         if not name.startswith("upcoming") or item["release"] >= today]
                if items:
                    discovery_shelves.append((label, items[:20]))
            if any(feed["error"] for feed in feeds.values()):
                discovery_notice = "Discovery updates unavailable. Showing saved results where available."
            elif not feeds:
                discovery_notice = "Discovery is loading. Refresh shortly for SIMKL releases and rankings."
        preview_trailer = next((item["trailer"] for _, items in discovery_shelves
                                for item in items if item["trailer"]), "")
        return render_template("home.html", shelves=shelves, discovery_shelves=discovery_shelves, preview_trailer=preview_trailer,
                               discovery_notice=discovery_notice, featured=featured, query=query, view=view)

    @app.get("/discover/<any(tv,movie):kind>/<int:identity>")
    def discover(kind, identity):
        item = next((item for feed in discovery_cache.read().values() for item in feed["items"]
                     if item["kind"] == kind and item["id"] == identity), None)
        if not item:
            abort(404)
        return render_template("discover.html", item=item)

    @app.get("/series/<int:media_id>")
    def series(media_id):
        item = decorate(get_media(media_id))
        if not item["series"]:
            return redirect(url_for("watch", media_id=media_id))
        with connect(database) as db:
            episodes = db.execute("""SELECT m.*,COALESCE(p.position,0) position,
                COALESCE(p.duration,0) duration,COALESCE(p.watched,0) watched
                FROM media m LEFT JOIN progress p ON p.media_id=m.id
                WHERE m.root=? AND m.series=? AND available=1 AND hidden=0
                ORDER BY season,episode,title""", (item["root"], item["series"])).fetchall()
        seasons = {}
        for episode in episodes:
            episode = decorate(dict(episode))
            episode["href"] = url_for("watch", media_id=episode["id"])
            seasons.setdefault(episode["season"], []).append(episode)
        return render_template("series.html", featured=item, seasons=seasons)

    def playback(media_id):
        item = get_media(media_id)
        with connect(database) as db:
            next_item = db.execute("""SELECT id,title,season,episode FROM media WHERE series=? AND series!=''
                AND root=? AND available=1 AND hidden=0 AND (season>? OR (season=? AND episode>?))
                ORDER BY season,episode,id LIMIT 1""", (item["series"], item["root"], item["season"],
                    item["season"], item["episode"])).fetchone()
        return item, dict(next_item) if next_item else None

    @app.get("/watch/<int:media_id>")
    def watch(media_id):
        item, next_item = playback(media_id)
        return render_template("watch.html", item=item, next_item=next_item)

    @app.get("/api/playback/<int:media_id>")
    def playback_details(media_id):
        item, next_item = playback(media_id)
        if next_item:
            next_item["watch_url"] = url_for("watch", media_id=next_item["id"])
        return jsonify(id=item["id"], title=item["title"], series=item["series"],
                       season=item["season"], episode=item["episode"],
                       position=0 if item["watched"] else item["position"],
                       stream_url=url_for("stream", media_id=item["id"]),
                       watch_url=url_for("watch", media_id=item["id"]), next=next_item)

    @app.get("/media/<int:media_id>")
    def stream(media_id):
        item = get_media(media_id)
        try:
            path = media_path(item["root"], item["path"])
        except (OSError, ValueError):
            abort(404, "Video file is unavailable. Ask the administrator to scan the library.")
        return send_file(path, conditional=True)

    @app.post("/api/progress/<int:media_id>")
    def progress(media_id):
        get_media(media_id)
        if request.is_json:
            body = request.get_json()
        else:
            try:
                body = {key: float(request.form[key]) for key in ("position", "duration")}
            except (KeyError, ValueError):
                abort(400)
        if not isinstance(body, dict):
            abort(400)
        position, duration = body.get("position"), body.get("duration")
        if any(isinstance(n, bool) or not isinstance(n, (int, float)) or not math.isfinite(n) or n < 0
               for n in (position, duration)) or duration > 7 * 86400 or position > duration:
            abort(400, "Invalid playback position")
        watched = body.get("watched")
        if watched is not None and not isinstance(watched, bool):
            abort(400)
        with connect(database) as db:
            # Auto-completion is sticky; only an explicit Mark unwatched clears it.
            old = db.execute("SELECT watched FROM progress WHERE media_id=?", (media_id,)).fetchone()
            complete = watched if watched is not None else bool((old and old[0]) or (duration > 0 and position / duration >= .95))
            db.execute("""INSERT INTO progress(media_id,position,duration,watched,updated_at) VALUES(?,?,?,?,?)
                ON CONFLICT(media_id) DO UPDATE SET position=excluded.position,duration=excluded.duration,
                watched=excluded.watched,updated_at=excluded.updated_at""",
                (media_id, position, duration, complete, datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")))
        return jsonify(ok=True)

    @app.get("/admin")
    def admin():
        section = request.args.get("section", "downloads")
        if section not in ("downloads", "library", "settings", "following", "removed"):
            section = "downloads"
        query = request.args.get("q", "").strip()[:200]
        with connect(database) as db:
            config = settings(db)
            items = [dict(row) for row in db.execute("""SELECT m.*,COALESCE(p.watched,0) watched
                FROM media m LEFT JOIN progress p ON p.media_id=m.id
                WHERE root=? ORDER BY series,season,episode,title""", (config["videos_path"],))]
            subscriptions = [dict(row) for row in db.execute("SELECT * FROM subscriptions WHERE root=? ORDER BY series",
                                                            (config["videos_path"],))]
            for subscription in subscriptions:
                subscription["lookup"] = lookup_status(subscription, config)
            for subscription in subscriptions if section == "following" else []:
                subscription["episodes"] = [dict(row) for row in db.execute(
                    "SELECT * FROM discoveries WHERE subscription_id=? ORDER BY season,episode", (subscription["id"],))]
                for episode in subscription["episodes"]:
                    episode.update(episode_status(db, subscription, episode["season"], episode["episode"]))
                subscription["seasons"] = sorted({e["season"] for e in subscription["episodes"]})
            rules = {(r["series"], r["season"]): dict(r) for r in db.execute(
                "SELECT * FROM season_state WHERE root=?", (config["videos_path"],))}
        if section == "removed":
            items = [item for item in items if item["watched"] and not item["available"]]
        if query:
            items = [item for item in items if query.casefold() in (item["title"] + " " + item["series"]).casefold()]
        groups = {}
        for item in items:
            if item["series"]:
                groups.setdefault(item["series"], {}).setdefault(item["season"], []).append(item)
        return render_template("admin.html", config=config, items=items, groups=groups, rules=rules,
                               subscriptions=subscriptions, section=section, query=query,
                               notice=request.args.get("notice", ""))

    @app.get("/admin/following/status")
    def following_status():
        with connect(database) as db:
            config = settings(db)
            subscriptions = db.execute("SELECT * FROM subscriptions WHERE root=? ORDER BY series",
                                       (config["videos_path"],)).fetchall()
            result = [lookup_status(item, config) for item in subscriptions]
        response = jsonify(subscriptions=result)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.post("/admin/follow")
    def follow_series():
        body = request.get_json()
        if not isinstance(body, dict):
            abort(400)
        follow(database, body.get("site"), body.get("payload"), body.get("series"))
        return jsonify(ok=True)

    @app.post("/admin/following/<int:subscription_id>")
    def update_subscription(subscription_id):
        mode = request.form.get("mode")
        if mode not in ("notify", "auto", "paused"):
            raise ValueError("Choose notify, automatic downloads, or paused")
        with connect(database) as db:
            result = db.execute("UPDATE subscriptions SET mode=?,next_check=0,error='' WHERE id=? AND root=?",
                                (mode, subscription_id, settings(db)["videos_path"]))
            if not result.rowcount:
                abort(404)
        return redirect(url_for("admin", section="following", notice="Preferences saved. Checks run in the background."))

    @app.post("/admin/following/<int:subscription_id>/<int:season>/<int:episode>")
    def discovery_action(subscription_id, season, episode):
        action = request.form.get("action")
        if action == "download":
            try:
                monitor.download(subscription_id, season, episode, include_watched=request.form.get("include_watched") == "1")
            except VibraVidError as error:
                raise ValueError(str(error)) from error
        elif action in ("dismiss", "retry"):
            with monitor.download_lock, connect(database) as db:
                db.execute("BEGIN IMMEDIATE")
                subscription = db.execute("SELECT * FROM subscriptions WHERE id=? AND root=?",
                    (subscription_id, settings(db)["videos_path"])).fetchone()
                if not subscription:
                    abort(404)
                if action == "retry" and request.form.get("queue_checked") != "1":
                    raise ValueError("Check the engine queue before allowing another attempt")
                states = ("new", "manual", "dismissed", "downloaded", "failed") if action == "dismiss" else ("submitted", "uncertain", "submitting")
                placeholders = ",".join("?" for _ in states)
                result = db.execute(f"""UPDATE discoveries SET state=? WHERE subscription_id=? AND season=? AND episode=?
                    AND state IN ({placeholders})""",
                    ("manual" if action == "retry" else "dismissed", subscription_id, season, episode, *states))
                if not result.rowcount:
                    abort(404)
        else:
            raise ValueError("Unknown episode action")
        return redirect(url_for("admin", section="following", notice="Episode updated."))

    @app.post("/admin/season")
    def update_season():
        series = request.form.get("series", "").strip()
        season = request.form.get("season", "")
        action = request.form.get("action")
        if not series or len(series) > 300 or not re.fullmatch(r"\d{1,4}", season) or action not in ("watched", "unwatched", "exclude", "include"):
            raise ValueError("Choose a series, season, and valid action")
        season = int(season)
        with connect(database) as db:
            db.execute("BEGIN IMMEDIATE")
            root = settings(db)["videos_path"]
            known = db.execute("""SELECT 1 FROM media WHERE root=? AND series=? AND season=?
                UNION SELECT 1 FROM subscriptions s JOIN discoveries d ON d.subscription_id=s.id
                WHERE s.root=? AND s.series=? AND d.season=?""", (root, series, season, root, series, season)).fetchone()
            if not known:
                abort(404)
            db.execute("INSERT OR IGNORE INTO season_state(root,series,season) VALUES(?,?,?)", (root, series, season))
            if action in ("watched", "unwatched"):
                watched = action == "watched"
                db.execute("UPDATE season_state SET watched=? WHERE root=? AND series=? AND season=?", (watched, root, series, season))
                if watched:
                    db.execute("UPDATE season_state SET excluded=1 WHERE root=? AND series=? AND season=?", (root, series, season))
                db.execute("""INSERT INTO progress(media_id,watched)
                    SELECT id,? FROM media WHERE root=? AND series=? AND season=?
                    ON CONFLICT(media_id) DO UPDATE SET watched=excluded.watched,updated_at=CURRENT_TIMESTAMP""",
                           (watched, root, series, season))
            else:
                db.execute("UPDATE season_state SET excluded=? WHERE root=? AND series=? AND season=?", (action == "exclude", root, series, season))
        return redirect(url_for("admin", section="following" if request.form.get("return") == "following" else "library",
                                notice="Season updated. Marking unwatched does not clear its download exclusion."))

    @app.post("/admin/media/<int:media_id>/remove")
    def remove_media(media_id):
        if request.form.get("confirm") != "remove":
            raise ValueError("Confirm removal of this video file")
        with scan_lock, connect(database) as db:
            db.execute("BEGIN IMMEDIATE")
            item = db.execute("SELECT * FROM media WHERE id=? AND root=?", (media_id, settings(db)["videos_path"])).fetchone()
            if not item:
                abort(404)
            try:
                path = media_path(item["root"], item["path"])
                if path != Path(item["root"]).resolve(strict=True) / item["path"]:
                    raise ValueError("Refusing to remove a symlink target")
                path.unlink()
            except FileNotFoundError:
                pass
            except (OSError, ValueError) as error:
                raise ValueError("Could not remove video safely. Catalog and history preserved.") from error
            db.execute("UPDATE media SET available=0 WHERE id=?", (media_id,))
        return redirect(url_for("admin", section="library", notice="Video removed. Details and watch history retained."))

    @app.post("/admin/settings")
    def save_settings():
        value = request.form.get("videos_path", "").strip()
        if not value or not Path(value).is_absolute():
            raise ValueError("Choose an absolute video folder path")
        try:
            root = Path(value).resolve(strict=True)
        except OSError as error:
            raise ValueError("The video folder does not exist or is inaccessible") from error
        if not root.is_dir() or not os.access(root, os.R_OK | os.W_OK | os.X_OK):
            raise ValueError("The service must be able to read and publish videos in this folder")
        staging = Path(database).parent / "vibravid" / "downloads"
        if root == staging or root.is_relative_to(staging) or staging.is_relative_to(root):
            raise ValueError("Keep the library separate from the engine's active downloads")
        with connect(database) as db:
            db.execute("BEGIN IMMEDIATE")
            if settings(db).get("videos_path") != str(root):
                db.execute("UPDATE media SET available=0")
                db.execute("DELETE FROM settings WHERE key='last_scan'")
            db.execute("INSERT OR REPLACE INTO settings VALUES('videos_path',?)", (str(root),))
            db.execute("DELETE FROM settings WHERE key='vibravid_url'")
        return redirect(url_for("admin", section="settings", notice="Folder saved. Open Library to scan for files."))

    @app.post("/admin/settings/downloads")
    def save_download_limits():
        downloads, processing = validate_limits(request.form.get("download_limit"), request.form.get("processing_limit"))
        with connect(database) as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES('download_limit',?)", (str(downloads),))
            db.execute("INSERT OR REPLACE INTO settings VALUES('processing_limit',?)", (str(processing),))
        return redirect(url_for("admin", section="settings", notice="Limits saved. Restart MyFlix to apply them; current jobs continue with their existing limits."))

    @app.post("/admin/downloads/retry-failed")
    def retry_failed_downloads():
        try:
            return jsonify(monitor.retry_failed())
        except VibraVidError as error:
            return jsonify(error=str(error)), 502

    @app.route("/admin/vibravid/<action>", methods=["GET", "POST"])
    def downloads_api(action):
        if request.method == "GET":
            if action not in VibraVid.READ:
                abort(405)
            body = None
        else:
            if action not in VibraVid.WRITE:
                abort(405)
            body = request.get_json()
            if not isinstance(body, dict):
                abort(400)
            if action == "search":
                query = body.get("query")
                site = body.get("site", "__all__")
                if not isinstance(query, str) or not query.strip() or len(query) > 200 or not isinstance(site, str) or len(site) > 100:
                    abort(400, "Enter a search title and provider")
                body = {"query": query.strip(), "site": site}
            elif action in ("seasons", "download"):
                if not isinstance(body.get("site"), str) or not body["site"] or not isinstance(body.get("payload"), dict) or not body["payload"]:
                    abort(400, "Select a search result first")
                body = {key: body[key] for key in ("site", "payload", "season", "episodes") if key in body}
                if action == "download" and not body["payload"].get("is_movie"):
                    season = body.get("season", "")
                    episodes = body.get("episodes", "*")
                    if not isinstance(season, str) or not re.fullmatch(r"\d{1,3}", season) or not isinstance(episodes, str) or not re.fullmatch(r"\*|\d{1,4}(?:-\d{1,4})?(?:,\d{1,4}(?:-\d{1,4})?)*", episodes) or len(episodes) > 200:
                        abort(400, "Choose a season and episodes (*, 1-4, or 1,3)")
            else:
                download_id = body.get("download_id")
                if not isinstance(download_id, str) or not download_id or len(download_id) > 200:
                    abort(400, "Select a queued download")
                body = {"download_id": download_id}
        try:
            if action == "download":
                with monitor.download_lock:
                    result = monitor.retries.submit(vibravid, body)
            elif action == "cancel":
                with monitor.download_lock:
                    monitor.retries.cancel_pending(vibravid.call("queue"), body["download_id"])
                    result = vibravid.call(action, body)
            else:
                result = vibravid.call(action, body)
            if action == "queue":
                monitor.reconcile_queue(result)
                result = monitor.retries.present(result)
            return jsonify(result)
        except VibraVidError as error:
            return jsonify(error=str(error)), 502

    @app.post("/admin/scan")
    def scan_library():
        try:
            count = run_scan()
        except OSError as error:
            raise ValueError(str(error)) from error
        return redirect(url_for("admin", section="library", notice=f"Scan complete: {count} videos found."))

    @app.post("/admin/media/<int:media_id>")
    def edit_media(media_id):
        title = request.form.get("title", "").strip()[:300]
        series = request.form.get("series", "").strip()[:300]
        if not title:
            raise ValueError("A title is required")
        def number(key):
            raw = request.form.get(key, "").strip()
            if not raw:
                return None
            if not raw.isdecimal() or len(raw) > 4:
                raise ValueError("Season and episode must be whole numbers from 0 to 9999")
            return int(raw)
        season, episode = number("season"), number("episode")
        if series and (season is None or episode is None):
            raise ValueError("Series need a season and episode number")
        with connect(database) as db:
            result = db.execute("UPDATE media SET title=?,series=?,season=?,episode=?,hidden=? WHERE id=? AND root=?",
                (title, series, season if series else None, episode if series else None,
                 "hidden" in request.form, media_id, settings(db).get("videos_path", "")))
            if not result.rowcount:
                abort(404)
        return redirect(url_for("admin", section="library", notice="Catalog updated."))

    @app.get("/health")
    def health():
        with connect(database) as db:
            db.execute("SELECT 1").fetchone()
        return jsonify(status="ok")

    return app

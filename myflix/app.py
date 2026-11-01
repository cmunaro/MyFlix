import hmac
import math
import os
import re
import secrets
import threading
from pathlib import Path

from flask import Flask, abort, jsonify, redirect, render_template, request, send_file, session, url_for
from werkzeug.exceptions import HTTPException

from .artwork import ArtworkCache
from .catalog import media_path, scan_safely
from .db import connect, initialize, settings
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
    artwork_cache = ArtworkCache(database, vibravid)
    app.extensions["artwork"] = artwork_cache

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
        if not request.path.startswith(("/static/", "/media/")):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.errorhandler(HTTPException)
    def http_error(error):
        if request.path.startswith(("/api/", "/admin/vibravid/")):
            return jsonify(error=error.description), error.code
        return render_template("error.html", message=error.description), error.code

    @app.errorhandler(ValueError)
    def invalid(error):
        if request.path.startswith(("/api/", "/admin/vibravid/")):
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
                COALESCE(p.duration,0) duration,COALESCE(p.watched,0) watched
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
        continuing = [row.copy() for row in rows if row["position"] > 0 and not row["watched"]]
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
        if shows and view != "continue":
            shelves.append(("Series", list(shows.values())))
        if films and view != "continue":
            shelves.append(("Film", films))
        featured = continuing[0] if continuing else (max(titles, key=lambda r: (r["added_at"], r["id"])) if titles else None)
        return render_template("home.html", shelves=shelves, featured=featured, query=query, view=view)

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
            db.execute("""INSERT INTO progress(media_id,position,duration,watched) VALUES(?,?,?,?)
                ON CONFLICT(media_id) DO UPDATE SET position=excluded.position,duration=excluded.duration,
                watched=excluded.watched,updated_at=CURRENT_TIMESTAMP""", (media_id, position, duration, complete))
        return jsonify(ok=True)

    @app.get("/admin")
    def admin():
        with connect(database) as db:
            config = settings(db)
            items = db.execute("SELECT * FROM media WHERE root=? ORDER BY series,season,episode,title",
                               (config.get("videos_path", ""),)).fetchall()
        return render_template("admin.html", config=config, items=items, notice=request.args.get("notice", ""))

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
            db.execute("INSERT OR REPLACE INTO settings VALUES('videos_path',?)", (str(root),))
            db.execute("DELETE FROM settings WHERE key='vibravid_url'")
        return redirect(url_for("admin", notice="Settings saved. Scan the library to import files."))

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
            return jsonify(vibravid.call(action, body))
        except VibraVidError as error:
            return jsonify(error=str(error)), 502

    @app.post("/admin/scan")
    def scan_library():
        try:
            count = run_scan()
        except OSError as error:
            raise ValueError(str(error)) from error
        return redirect(url_for("admin", notice=f"Scan complete: {count} videos found."))

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
        return redirect(url_for("admin", notice="Catalog updated."))

    @app.get("/health")
    def health():
        with connect(database) as db:
            db.execute("SELECT 1").fetchone()
        return jsonify(status="ok")

    return app

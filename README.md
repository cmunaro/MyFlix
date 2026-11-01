# MyFlix

A small household video library for a Raspberry Pi and a VIDAA TV browser. One MyFlix system service serves the TV interface, admin interface, and video files, and supervises its bundled VibraVid download engine. SQLite stores catalog metadata, settings, and shared household watch progress. No Node build, containers, Redis, cloud account, or transcoding service.

## Run locally

Install [mise](https://mise.jdx.dev/), then from this directory:

```sh
mise trust
mise install
mise run setup
export MYFLIX_ADMIN_PASSWORD='choose-a-long-unique-password'
mise run dev
```

Open `http://localhost:8090` for watching and `http://localhost:8090/admin` for management. On the TV, open `http://<server-LAN-IP>:8090` and bookmark it. Downloads are ready from the admin page after startup. A default library is created under the data directory; choose another **existing absolute video folder** in admin to use your media drive. MyFlix manages the download engine automatically.

Python and uv are pinned in `mise.toml`; `.python-version` keeps uv aligned with mise. `uv.lock` locks all Python dependencies, including VibraVid and Django. The exact engine Git revision is pinned in `pyproject.toml`; setup fetches that checkout into the ignored `.vibravid/` directory before installing dependencies. Git and network access are required during setup. Mise supplies Python; uv is configured not to download a different interpreter.

## What works

- A Netflix-style TV interface: featured title, horizontal shelves, one tile per series, season/episode screens, search, and continue watching. Local JPEG artwork is served from the library; titles without art use a typography-based fallback. The player fills the screen and hides its overlay during playback.
- Arrow-key / D-pad navigation with visible focus. Left/right moves along shelves, including off-screen cards; up/down moves between shelves. Browsing focus is restored when returning. The player has one transient overlay with an icon row, seek timeline, remaining time, mute, and fullscreen. Controls and cursor hide after three idle seconds during playback, including when a control had focus. Pointer/touch or remote input reveals controls; pause keeps them available. OK/Space toggles playback, left/right on the video seeks 10 seconds, and up/down accesses the controls. Back exits fullscreen, dismisses visible playback controls, then returns to the library. Native controls remain a no-JavaScript fallback.
- HTTP byte ranges for seeking; resume saved every ten seconds, on pause, and on navigation when the browser supports Beacon. Watched status is recorded automatically at 95% playback and on completion; the player has no manual watched button.
- A next-episode prompt appears during the last 30 seconds (or last 10% for short clips), independently of the fading controls. Select it to advance immediately, or let the next available episode start automatically at the end. Transitions reuse the same video element to preserve fullscreen, update the address and title, and save progress against the correct episode. Hidden/missing episodes are excluded; films and final episodes stop normally. Failed transitions offer a manual retry instead of looping.
- Password-protected `/admin`: choose the library folder, scan, correct titles and season/episode numbers, and hide items from TV. Integrated provider search, season/episode selection, film and series downloads, queue/history, cancellation, and engine status stay within MyFlix.
- Background scans every 60 seconds. Imports MP4, M4V, MKV, WebM, and MOV; skips hidden paths, symlinks, empty files, and files modified in the last 60 seconds.
- Episode names such as `The.Show.S01E02.Title.mp4` and `The.Show.1x02.Title.mp4` are inferred. Edit unusual naming in admin. Edits, visibility, and progress survive rescans. Missing files retain records and history and disappear from the TV until restored.

Progress belongs to one household, shared across devices. Concurrent devices use the last received update. Renaming/moving a video creates a new catalog identity; progress is retained against its previous path. Changing the configured root hides the previous root's catalog without deleting it.

## VibraVid integration

`mise run setup` installs the pinned [AstraeLabs/VibraVid](https://github.com/AstraeLabs/VibraVid) source and its locked dependencies. `mise run dev` and the systemd unit start both MyFlix and the engine. **No separate engine installation, URL, port, or shared-secret configuration is required.** Old `MYFLIX_VIBRAVID_ADDRESS` / `MYFLIX_VIBRAVID_SECRET` environment variables are no longer used and can be removed.

MyFlix generates an internal token, stores it privately in SQLite, and passes it to its engine child through the environment. The engine binds a free loopback port; MyFlix waits for migrations and startup before accepting browser traffic. Shutdown terminates the engine and its download subprocesses. An engine failure stops MyFlix so systemd can restart the complete service. Only one instance can use a data directory at a time.

Search, season selection, film/episode downloads, queue/history, and cancellation run inside MyFlix admin. On series results, enter `*` for all episodes, `1-4`, or `1,3`. Queue/history refresh every 15 seconds while the admin tab is visible. Cancellation also stops related episodes of the same series. Requests that time out are never automatically retried: check the queue before retrying a download.

The bundled engine has its own Django SQLite database alongside MyFlix's catalog database. Under the MyFlix data directory:

```text
myflix.sqlite3             # Catalog, settings, watch progress, internal secrets
library/                   # Default completed library; configurable in admin
vibravid/Conf/             # Engine preferences and provider login settings
vibravid/downloads/        # Active/staged downloads, never indexed as the library
vibravid/db.sqlite3        # Engine history and internal state
vibravid/binary/           # Engine-managed helper tools
vibravid/<revision>/       # Runtime GUI source and logs
```

MyFlix configures the completion hook automatically. Successful downloads are published into the **current folder selected in admin**, read from SQLite at completion. The hook makes hard links on the same filesystem or copies through a hidden temporary file across filesystems; partial copies never appear in the library. Staging originals remain in place. Existing library videos are never overwritten; conflicting files require administrator review. The service user needs write access to the selected library. Keep the library outside the engine's staging folder.

Known JPEG sidecars are published too: `<video>-poster.jpg`, `<video>-fanart.jpg`, `<video>-thumb.jpg`, and show-folder `poster.jpg` / `fanart.jpg`. Existing artwork is retained. NFO metadata is not imported. Manual completed videos can also be copied into the library. Files must be at least 60 seconds old before the scanner imports them.

Defaults suit the Pi: one download at a time, two segment workers, 1080p selection, automatic track selection, and sidecars enabled. MyFlix owns these settings and the publication hook. Other audio/provider preferences can be edited in `vibravid/Conf/config.toml` and `login.toml` while the service is stopped. Provider-specific accounts or optional external modules are still required where upstream requires them; automatic future-episode subscriptions are not exposed by this UI. VibraVid retains its upstream GPL-3.0-or-later license; its source and license are included in the pinned checkout.

The [pinned upstream API](https://github.com/AstraeLabs/VibraVid/blob/3a5045587a4867169235098536254961f5e8fb3f/GUI/searchapp/api_bot.py) is exercised by real-engine startup, provider-list, status, queue, and shutdown tests. Live third-party search/download success still depends on the provider and credentials; no live media download was performed here. The engine's own binary resolver obtains required muxing/downloader helpers on demand, so the first download may need network access for those helpers too. Their availability on Raspberry Pi must be verified on the target.

## Raspberry Pi service

Use Raspberry Pi OS **64-bit** with systemd and mise installed. The following layout keeps the mise Python interpreter under `/opt` so systemd's home-directory protection does not block it. Copy this project to `/opt/myflix`, owned by your installation user; exclude `.venv`, local `data`, `.vibravid`, and `.mise-data` from any existing checkout.

From `/opt/myflix`, as your installation user:

```sh
export MISE_DATA_DIR=/opt/myflix/.mise-data
mise trust
mise install
mise run setup
```

Then provision the service account, media permissions, and configuration:

```sh
sudo groupadd --system --force media
sudo useradd --system --gid media --home-dir /var/lib/myflix --no-create-home --shell /usr/sbin/nologin myflix
sudo install -d -o myflix -g media -m 2750 /srv/media/library
sudo install -m 0600 deploy/myflix.env.example /etc/myflix.env
sudoedit /etc/myflix.env
sudo install -m 0644 deploy/myflix.service /etc/systemd/system/myflix.service
sudo systemctl daemon-reload
sudo systemctl enable --now myflix
sudo systemctl status myflix
```

Set a real password in `/etc/myflix.env`. The bundled engine runs as the same `myflix` account; no second service or user is needed. Ensure `/opt/myflix` and the mise interpreter are readable/traversable by the `myflix` user. Keep code owned by the installation user, not the service user.

The service starts the locked `.venv` directly; mise is used during installation, not on every boot. Waitress uses four threads. All state, internal engine configuration, staging downloads, and SQLite databases live under `/var/lib/myflix`. The service can write there and publish completed downloads to `/srv/media/library`. It restarts on failure. Open the admin page to configure `/srv/media/library` after startup.

For a different mount/folder, adjust `RequiresMountsFor` and `ReadWritePaths` in the unit **as well as** the admin setting. Media under `/home` is blocked by `ProtectHome`; use `/srv` or `/mnt`. `RequiresMountsFor` must reference the real external mount so the service waits for it. An unavailable folder or unreadable subtree preserves the last successful catalog and reports a scan error; an accessible empty folder is treated as an empty library.

```sh
sudo journalctl -u myflix -f
curl http://127.0.0.1:8090/health
```

Use on a trusted home LAN. TV browsing/playback and progress updates are available to LAN clients; admin changes and download operations require login and all writes require CSRF tokens. Do not port-forward the service. Remote access needs a VPN or an authenticated TLS reverse proxy. For direct HTTPS behind a proxy, also configure Flask's secure session cookie appropriately. MyFlix does not provide multi-user profiles or permanent file deletion; use filesystem tools for permanent deletion.

## TV compatibility

On VIDAA/Hisense TV user agents, MyFlix enables remote mode: page cursors are hidden and virtual-pointer movement does not reveal the playback overlay. A remote key or click requests fullscreen with `navigationUI: "hide"`; older WebKit fullscreen APIs are supported too. Fullscreen requests need a user gesture and can be rejected by firmware. Back and the fullscreen button still exit normally. Desktop cursor behavior is unchanged.

The built-in VIDAA Internet Browser owns its address/options bar and virtual pointer outside the page. CSS and fullscreen cannot guarantee that these system overlays disappear. For firmware that forces cursor navigation, launching MyFlix as a native TV app tile may be necessary; that requires a model/firmware-appropriate installation flow. The [VIDAA app maintainer's browser-mode report](https://github.com/NoobyGains/stremio-vidaa-tv/issues/36) describes this distinction. MyFlix does not change TV settings or install a launcher tile automatically.

The UI uses plain HTML/CSS, ES5 JavaScript, and native HTML5 video, consistent with the [VIDAA web app platform](https://www.vidaa.com/wp-content/uploads/2020/12/WebApp_Development_Guide_for_VIDAA.pdf). Start with **MP4, H.264 video, AAC audio**, ideally 720p/1080p. Container/codec support varies by TV model and firmware; recognizing an MKV in the library does not guarantee the browser can decode it. Native player errors are shown in the UI. No DRM handling, server transcoding, automatic codec conversion, or external subtitle selection is included. Convert incompatible media elsewhere; real-time transcoding is unsuitable for this target.

Verify remote keys, fullscreen, audio, seek, and resume on the actual TV before relying on it. Physical VIDAA and Raspberry Pi validation have not been performed.

## Tests and backup

```sh
mise run test
mise run test-tv
```

Run setup first. Tests include starting the actual bundled engine with an automatically generated secret, querying providers/status/queue, verifying duplicate-instance protection and shutdown, preserving configuration, and publishing to the latest UI-selected folder. Other tests cover import, inferred episode ordering, HTTP ranges, progress/resume, watched transitions, rescans, missing media, folder changes, series/artwork access, authentication, CSRF, input validation, symlink confinement, atomic hook publication, and authenticated HTTP integration for provider search, seasons, downloads, queue/status, and cancellation.

The optional `test-tv` task pins Node only for the development test; the service requires no Node runtime. It exercises remote shelf navigation, off-screen selection, shorter shelves, restored browsing focus, player auto-hide with focused buttons, hidden-control tab order, remote media keys, pointer/remote seek buttons, cached metadata, fullscreen, resume saves, next-episode prompts, manual/automatic episode transitions, failed transitions, final episodes, and blocked autoplay with small DOM fixtures. It does not replace visual/browser or physical TV testing.

Back up media separately. For a consistent SQLite backup, stop MyFlix and copy the entire data directory (including any `-wal` and `-shm` files), then restart. Restore it only while stopped. The data directory contains provider login files and internal secrets as well as catalog/settings/history; keep backups private. Before upgrading, back it up, stop the service, update code and mise pins, run `mise install` and `mise run setup` with the same `MISE_DATA_DIR`, then start the service. This initial schema is version 1; future incompatible changes require an explicit migration.

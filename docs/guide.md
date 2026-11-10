# MyFlix setup and operating guide

[← Back to the project README](../README.md)

A small household video library for a Raspberry Pi and a VIDAA TV browser. One MyFlix system service serves the TV interface, admin interface, and video files, and supervises its bundled VibraVid download engine. SQLite stores catalog metadata, settings, and shared household watch progress. No Node build, containers, Redis, cloud account, or transcoding service.

## Run locally

Install [mise](https://mise.jdx.dev/), then from this directory:

```sh
mise trust
mise install
mise run setup
mise run dev
```

Open `http://localhost:8090` for watching and `http://localhost:8090/admin` for management. On the TV, open `http://<server-LAN-IP>:8090` and bookmark it. Downloads are ready from the admin page after startup. A default library is created under the data directory; choose another **existing absolute video folder** in admin to use your media drive. MyFlix manages the download engine automatically.

Python and uv are pinned in `mise.toml`; `.python-version` keeps uv aligned with mise. `uv.lock` locks all Python dependencies, including VibraVid and Django. The exact engine Git revision is pinned in `pyproject.toml`; setup fetches that checkout into the ignored `.vibravid/` directory before installing dependencies. Git and network access are required during setup. Mise supplies Python; uv is configured not to download a different interpreter.

## What works

- A Netflix-style TV interface: featured title and horizontal shelves in order: Continue watching, Not watched, Series, and Film. Continue watching shows only the most recently watched unfinished episode per series, with recent activity first; each episode retains its own saved progress. Not watched includes unfinished films and series with unfinished episodes, excluding series already shown in Continue watching. Empty shelves are hidden. Cards outside Continue watching show only artwork and title; resume badges, playback progress, and playback decorations are confined to Continue watching. The header provides search and library management without category tabs. Series have one tile each and season/episode screens. Local JPEG artwork is served from the library; titles without art use a typography-based fallback. The player fills the screen and hides its overlay during playback.
- Arrow-key / D-pad navigation with visible focus. Left/right moves along shelves, including off-screen cards; up/down moves between shelves. Browsing focus is restored when returning. The player has one transient overlay with an icon row, seek timeline, remaining time, mute, and fullscreen. Controls and cursor hide after three idle seconds during playback, including when a control had focus. Pointer/touch or remote input reveals controls; pause keeps them available. OK/Space toggles playback, left/right on the video seeks 10 seconds, and up/down accesses the controls. Back exits fullscreen, dismisses visible playback controls, then returns to the library. Native controls remain a no-JavaScript fallback.
- An **Audio and subtitles** button appears in the player when the browser exposes multiple audio tracks or subtitle/caption tracks. Subtitles default to Off for each video and episode, including tracks discovered after playback starts. Select a language or enable subtitles without reloading, seeking, or leaving fullscreen. Remote arrows move between choices, OK selects, and Back closes the chooser before exiting fullscreen. Pointer controls and keyboard Tab work too. The chooser hides with idle playback controls, and available tracks refresh for each episode. Audio/subtitle selection uses the browser’s native track APIs; embedded tracks that the TV/browser does not expose cannot be selected. External subtitle files are not imported or converted.
- HTTP byte ranges for seeking; resume saved every ten seconds, on pause, and on navigation when the browser supports Beacon. Watched status is recorded automatically at 95% playback and on completion; the player has no manual watched button.
- A next-episode prompt appears during the last 30 seconds (or last 10% for short clips), independently of the fading controls. Select it to advance immediately, or let the next available episode start automatically at the end. Transitions reuse the same video element to preserve fullscreen, update the address and title, and save progress against the correct episode. Hidden/missing episodes are excluded; films and final episodes stop normally. Failed transitions offer a manual retry instead of looping.
- Password-free `/admin` with TV-friendly Downloads, Library, Watched and removed, and Settings views, large controls, visible remote focus, and an explicit return to watching. Search providers, choose episodes, and manage current downloads. Open a series to manage its seasons and episodes, edit details, hide items, remove video files while retaining history, scan, or change the library folder.
- Library and series lists check catalog membership every 15 seconds while visible. New imports update in place without navigation, preserving focus, scroll position, and expanded seasons. Updates defer while a field is focused, a form has unsaved edits, or an episode action is pending.
- Managed downloads publish and enter the catalog before reporting success. Verified publications bypass the scan age buffer; rescans retain that readiness only while the file size and modification timestamp still match. Import failures stay visible as failed downloads with the staging file retained.
- Background scans every 60 seconds. Imports MP4, M4V, MKV, WebM, and MOV; skips hidden paths, symlinks, empty files, and files modified in the last 60 seconds.
- Episode names such as `The.Show.S01E02.Title.mp4` and `The.Show.1x02.Title.mp4` are inferred. Edit unusual naming in admin. Edits, visibility, and progress survive rescans. Missing files retain records and history and disappear from the TV until restored.

Progress belongs to one household, shared across devices. Concurrent devices use the last received update. A returning or moved video reuses its missing catalog record when the inferred original series/season/episode (or film title) has exactly one missing match. This preserves edits, visibility, and watch progress even when the new filename has a different episode title. Ambiguous matches retain separate records; changing the inferred film title or episode identity cannot be matched automatically. Changing the configured root hides the previous root's catalog without deleting it.

## Series downloads and watch history

Series search entries offer **Choose episodes**. The picker loads the provider's actual season and episode numbers, selects all missing episodes by default, and supports **All seasons**, season checkboxes, and expandable individual episode lists. Downloaded episodes are checked and disabled; fully downloaded seasons are disabled too. Parent checkboxes never toggle locked episodes. Pending or unconfirmed requests are locked as well, preventing duplicate requests.

**Download** atomically saves the selected missing episodes as durable queued work and enables automatic checks. **Only new episodes** establishes the current provider listing as a baseline and enables checks without queuing that listing. Unselected existing episodes stay skipped. Both actions clear search results only after acknowledgement. A failed or uncertain response keeps the result visible and prevents automatic resubmission. Film entries retain their existing action.

Selected episodes appear immediately in Current downloads and wait for a free engine slot. They remain durable across restarts and can be cancelled while waiting. The worker submits exact episode requests, using the normal concurrency, publication, and recovery paths. An entry leaves the active queue only after the published file is committed to the catalog and available for playback. Failed downloads and provider checks remain visible in Downloads.

Every managed series checks the provider's season list every four hours and fetches episodes only for the highest-numbered season. New seasons become the checked season automatically. Older seasons retain their saved listings and history; their later additions are not fetched. The manual picker still loads all seasons. Opening an existing managed series does not queue older gaps, while newly discovered episodes in its latest season remain eligible. Provider errors preserve previous discoveries and back off for four hours. Provider listings describe availability, not broadcast schedules or guaranteed playable streams.

The Following page and its pause/notification controls are removed. Existing saved source identities and library-name mappings are retained internally. Former paused/notification series resume automatic checks after upgrade, with their old unsent backlog skipped and acknowledged or uncertain requests preserved. Earlier manual MyFlix downloads with a stable saved provider identity receive a baseline check and then automatic future-episode checks. Manually copied files without a known source are not guessed: find the series in Downloads and choose either action to establish its source.

In Library, **mark a season watched & exclude downloads** records household history for existing and later-imported episodes. Marking a season unwatched does not clear its download exclusion. Automatic downloads skip watched/excluded episodes and existing library files; explicit missing-episode selections may override watched/excluded status. File removal retains catalog details and progress. Unavailable mounts preserve the catalog and block automatic download submission.

**Retry all failed downloads** uses a durable priority queue ahead of ordinary waiting jobs. Running jobs are not interrupted. Repeated requests cannot duplicate the same retry, and a failed retry reappears with its new error. Eligible acknowledged interrupted requests and confirmed failures recover once at startup, after an initial scan and a verified engine queue. Completed/cancelled requests remain terminal, and unconfirmed engine mutations are never retried automatically. A saved exact film or episode identity is required; ambiguous legacy ranges need queue review. Originals remain in staging until separately cleaned up.

## Automatic covers

Covers come directly from the same providers VibraVid uses to download films and series. No account, API key, or separate metadata service is needed. MyFlix first reuses artwork URLs from active downloads, the queue, and saved download history. Missing older titles use VibraVid provider search, matching catalog title, film/series type, and release year when present. Episodes share their series cover. Ambiguous or unavailable titles keep their title-card fallback.

A single background worker checks every 60 seconds and handles up to ten missing titles per pass. Existing library artwork always takes priority. Provider JPEG, PNG, and WebP images are resized and cached as JPEG under the data directory's `artwork/` folder for TV compatibility and offline use. Pillow is already bundled with VibraVid and is declared directly for this conversion. Local fanart still supplies backgrounds. Rendering pages and serving covers never trigger a network request. Refresh the TV library to see new covers.

The admin page reports failures. Missing covers are checked again after seven days; provider or disk errors pause lookups for an hour. No media files or watch history are changed. The worker uses the restricted server-side engine API and downloads images only from public HTTP(S) addresses, with size limits and no redirects.

## VibraVid integration

`mise run setup` installs the pinned [AstraeLabs/VibraVid](https://github.com/AstraeLabs/VibraVid) source and its locked dependencies. `mise run dev` and the systemd unit start both MyFlix and the engine. **No separate engine installation, URL, port, or shared-secret configuration is required.** Old `MYFLIX_VIBRAVID_ADDRESS` / `MYFLIX_VIBRAVID_SECRET` environment variables are no longer used and can be removed.

MyFlix generates an internal token, stores it privately in SQLite, and passes it to its engine child through the environment. The engine binds a free loopback port; MyFlix waits for migrations and startup before accepting browser traffic. Shutdown terminates the engine and its download subprocesses. An engine failure stops MyFlix so systemd can restart the complete service. Only one instance can use a data directory at a time.

Search, season selection, film/episode downloads, and cancellation run in admin's Downloads view. On series results, enter `*` for all episodes, `1-4`, or `1,3`. Active and queued downloads show progress and cancellation. Recent failed jobs appear with season, episode, and error details; completed and cancelled history stays hidden. Active series downloads also show season and episode numbers when supplied by the engine; completed history stays internal for artwork recovery. The queue refreshes every 15 seconds while Downloads is visible, preserving the focused action. If its job finishes, focus moves to Refresh. Cancellation also stops related episodes of the same series, including priority retries still waiting in SQLite. Requests that time out are never automatically retried: check the queue before retrying a download.

On a TV remote, arrows move between management controls, including provider and season selectors. OK selects an action or starts editing a field; Back finishes editing, closes an expanded catalog item, returns to the current navigation item, then leaves management. Native TV keyboards and dropdowns handle input while editing. Desktop inputs retain normal typing and arrow behavior. Folder changes stay in Settings; scans and catalog edits return to Library. Physical TV keyboard behavior still needs device validation.

The bundled engine has its own Django SQLite database alongside MyFlix's catalog database. Under the MyFlix data directory:

```text
myflix.sqlite3             # Catalog, settings, watch progress, internal secrets
library/                   # Default completed library; configurable in admin
artwork/                   # Cached automatic covers, shared by title
vibravid/Conf/             # Engine preferences and provider login settings
vibravid/downloads/        # Active/staged downloads, never indexed as the library
vibravid/db.sqlite3        # Engine history and internal state
vibravid/binary/           # Engine-managed helper tools
vibravid/<revision>/       # Runtime GUI source and logs
```

MyFlix installs a completion adapter automatically, before the engine records success. Successful downloads are published into the **current folder selected in admin**, read from SQLite at completion. The completion stage copies independent files through a hidden temporary name, runs credits detection for episodes, saves the result in SQLite for the final file path, and publishes with an atomic no-replace rename on Linux. Partial copies never appear in the library, and opening a new episode does not need to wait for analysis. Failed or inconclusive detection uses the normal playback fallback without failing a valid download. Cancellation removes the hidden copy. Staging originals remain in place. Existing library videos are never overwritten; conflicting files require administrator review. The service user needs write access to the selected library. Keep the library outside the engine's staging folder.

Known JPEG sidecars are published too: `<video>-poster.jpg`, `<video>-fanart.jpg`, `<video>-thumb.jpg`, and show-folder `poster.jpg` / `fanart.jpg`. Existing artwork is retained. NFO metadata is not imported. Manual completed videos can also be copied into the library. Files must be at least 60 seconds old before the scanner imports them.

Defaults are four simultaneous jobs, with at most one processing at a time, two segment workers per download, 1080p selection, automatic track selection, and sidecars enabled. In **Settings → Download and processing limits**, set either concurrency limit from 1 to 10. Values persist in MyFlix SQLite and apply on the next restart; saving does not interrupt active jobs. The engine has a bounded worker pool, so these limits are upper bounds rather than guaranteed simultaneous activity. More concurrency uses more RAM, network bandwidth, and temporary disk space; Raspberry Pi throughput still requires device testing. MyFlix owns these settings and the completion adapter. Other audio/provider preferences can be edited in `vibravid/Conf/config.toml` and `login.toml` while the service is stopped. Provider-specific accounts or optional external modules are still required where upstream requires them; series subscriptions run inside MyFlix, using the same providers and queue. VibraVid retains its upstream GPL-3.0-or-later license; its source and license are included in the pinned checkout.

The simultaneous job limit covers downloading, waiting for processing, processing, and publication together. Each scheduler worker keeps one job slot until its task finishes, including a season task with multiple episodes. Processing has a separate limit within that total; it never adds extra job slots. Finished downloads show **Waiting for processing** or **Processing**, and credits detection and publication finish before the job is reported completed. They reuse the processing slot during finalization, and a shared lock prevents simultaneous credits analysis during background backfill. The legacy MyFlix post-download hook is removed automatically to avoid publishing the same file twice. In-flight muxing is disabled, including provider overrides, so FFmpeg does not occupy a processing slot for the duration of a network download. FFmpeg, mkvmerge/mkvpropedit, and Dolby Vision helper executables also share a crash-safe process limit, covering conversions outside the main merge stage and FFmpeg invoked by yt-dlp. yt-dlp receives the guarded FFmpeg path; its combined external download/processing invocation retains its job slot until it returns. Conversion here means the provider engine's existing muxing and compatibility work; MyFlix adds no playback transcoding. Runtime adapters and small executable launchers live outside the pinned source checkout. Launchers preserve executable shim paths and command names, including mise-managed FFmpeg, rather than invoking the shim dispatcher directly. Normal cancellation keeps the same process PID, and process exit or termination releases the tool permit automatically.

The [pinned upstream API](https://github.com/AstraeLabs/VibraVid/blob/10f075261258e41369780cc7822e8642b033037a/GUI/searchapp/api_bot.py) is exercised by real-engine startup, provider-list, status, queue, and shutdown tests. Live third-party search/download success still depends on the provider and credentials; no live media download was performed here. The engine's own binary resolver obtains required muxing/downloader helpers on demand, so the first download may need network access for those helpers too. Their availability on Raspberry Pi must be verified on the target.

## Raspberry Pi service

For an always-on installation, use the scripts below instead of the development setup at the start of this guide.

Use Raspberry Pi OS **64-bit** with systemd. Install Git if necessary (`sudo apt-get install git`), clone this repository anywhere as your normal user, and run:

```sh
./install.sh
```

The script elevates with sudo, installs Debian OS prerequisites, and bootstraps a private [mise installation](https://mise.jdx.dev/installing-mise.html) under `/opt/myflix/.mise`. Pinned Python and uv live in `/opt/myflix/.mise-data`, outside the service's home-directory sandbox. It deploys committed files into a new `/opt/myflix/releases/` folder, runs the existing setup task to fetch the pinned engine and sync the frozen lock, and switches `/opt/myflix/current` only after setup succeeds. The checkout must be clean. The service never runs from your clone, so it can be under your home directory. No global tool versions or Git settings are changed.

The installer creates the `myflix` service account and `media` group. It creates `/srv/media/library` if missing, without changing an existing folder's ownership or permissions. `/etc/myflix.env` is created only once; edit it to adjust `MYFLIX_HOST` and `MYFLIX_PORT`, then restart MyFlix. The systemd unit is refreshed during deployment; keep customizations in `sudo systemctl edit myflix` drop-ins so updates preserve them.

The service starts `/opt/myflix/current/.venv/bin/python` directly and is enabled at boot. All state, internal engine configuration, staging downloads, and SQLite databases live under `/var/lib/myflix`. The service can write there and publish completed downloads to `/srv/media/library`. Open admin Settings and choose `/srv/media/library` after installation. Until then, the default library is `/var/lib/myflix/library`.

For a different mount/folder, override `RequiresMountsFor` and `ReadWritePaths` in the unit **as well as** the admin setting. Give `myflix` read/write access to that folder and restart the service. Media under `/home` is blocked by `ProtectHome`; use `/srv` or `/mnt`. `RequiresMountsFor` must reference the real external mount so the service waits for it. An unavailable folder or unreadable subtree preserves the last successful catalog and reports a scan error; an accessible empty folder is treated as an empty library.

To update, from your original clone:

```sh
./update.sh
```

The script refuses a dirty checkout and uses `git pull --ff-only` with the configured upstream. It then runs the installer from the updated checkout. Preparation happens while the old release runs; the service stops before the active release changes, then starts and gets an HTTP health check using your configured host/port. Concurrent deployments are locked out. Playback and running downloads are interrupted by the restart. A failed preparation leaves the active release running. A failed startup restores the previous release symlink and attempts to start it; check the journal if recovery fails. This restores code, not database schemas: back up data before an upgrade.

Old and failed releases are kept for diagnosis and can consume substantial space. After a successful update, remove unused release directories manually, retaining the target of `/opt/myflix/current` and the previous working release. Do not move releases or their virtual environments. `./install.sh` can be rerun to deploy the checkout's current commit without pulling.

These scripts refuse the older layout with application code directly in `/opt/myflix`. To migrate, stop the old service, preserve `/etc/myflix.env` and `/var/lib/myflix`, move the old application directory aside, and run `install.sh` from a separate clean clone. Keep the old directory until the new deployment works. Custom media mount overrides should be moved to a systemd drop-in. Physical Raspberry Pi installation and engine helper availability still need target validation.

```sh
sudo journalctl -u myflix -f
curl http://127.0.0.1:8090/health
```

Use on a trusted home LAN. TV browsing/playback and progress updates are available to LAN clients; admin management and download operations are password-free for anyone who can reach the service. All writes require CSRF tokens. No admin password environment variable is needed; existing `MYFLIX_ADMIN_PASSWORD` values are ignored and can be removed. Do not port-forward the service. Remote access needs a VPN or an authenticated TLS reverse proxy. For direct HTTPS behind a proxy, also configure Flask's secure session cookie appropriately. MyFlix does not provide multi-user profiles. Removing a video in admin deletes the selected library file while retaining catalog details and watch history; staging originals and sidecars remain.

## TV compatibility

On VIDAA/Hisense TV user agents, MyFlix enables remote mode: page cursors are hidden and virtual-pointer movement does not reveal the playback overlay. A remote key or click requests fullscreen with `navigationUI: "hide"`; older WebKit fullscreen APIs are supported too. Fullscreen requests need a user gesture and can be rejected by firmware. Back and the fullscreen button still exit normally. Desktop cursor behavior is unchanged.

The built-in VIDAA Internet Browser owns its address/options bar and virtual pointer outside the page. CSS and fullscreen cannot guarantee that these system overlays disappear. For firmware that forces cursor navigation, launching MyFlix as a native TV app tile may be necessary; that requires a model/firmware-appropriate installation flow. The [VIDAA app maintainer's browser-mode report](https://github.com/NoobyGains/stremio-vidaa-tv/issues/36) describes this distinction. MyFlix does not change TV settings or install a launcher tile automatically.

The UI uses plain HTML/CSS, ES5 JavaScript, and native HTML5 video, consistent with the [VIDAA web app platform](https://www.vidaa.com/wp-content/uploads/2020/12/WebApp_Development_Guide_for_VIDAA.pdf). Start with **MP4, H.264 video, AAC audio**, ideally 720p/1080p. Container/codec support varies by TV model and firmware; recognizing an MKV in the library does not guarantee the browser can decode it. Native player errors are shown in the UI. No DRM handling, server transcoding, automatic codec conversion, or external subtitle file selection is included. Native audio/subtitle switching depends on the tracks exposed by each TV/browser; physical VIDAA track switching still needs device validation. Convert incompatible media elsewhere; real-time transcoding is unsuitable for this target.

Verify remote keys, fullscreen, audio, seek, and resume on the actual TV before relying on it. Physical VIDAA and Raspberry Pi validation have not been performed.

## Tests and backup

```sh
mise run test
mise run test-tv
```

Deployment orchestration can be checked without root or a systemd host using `mise exec -- python -m unittest discover -s tests -p test_deployment.py -v`. These tests mock OS commands and cover release switching, preparation failures, startup rollback, preserved configuration, and update guards.

Run setup first for the full suite. Tests include starting the actual bundled engine with an automatically generated secret, querying providers/status/queue, verifying duplicate-instance protection and shutdown, preserving configuration, and publishing to the latest UI-selected folder. Other tests cover import, inferred episode ordering, HTTP ranges, progress/resume, watched transitions, rescans, missing media, folder changes, series/artwork access, password-free admin access, CSRF, input validation, symlink confinement, atomic hook publication, and HTTP integration for provider search, seasons, downloads, queue/status, and cancellation.

The optional `test-tv` task pins Node only for the development test; the service requires no Node runtime. It exercises admin input navigation, queue focus restoration, download mutation guards, remote shelf navigation, off-screen selection, shorter shelves, restored browsing focus, player auto-hide with focused buttons, hidden-control tab order, remote media keys, pointer/remote seek buttons, cached metadata, fullscreen, resume saves, next-episode prompts, manual/automatic episode transitions, failed transitions, final episodes, blocked autoplay, native audio/subtitle selection, subtitle Off, dynamic track changes, failed switches, track-menu focus and idle hiding with small DOM fixtures. It does not replace visual/browser or physical TV testing.

Back up media separately. For a consistent SQLite backup, stop MyFlix and copy the entire data directory (including any `-wal` and `-shm` files), then restart. Restore it only while stopped. The data directory contains provider login files and internal secrets as well as catalog/settings/history; keep backups private. Before upgrading, back it up, restart the service after the backup, then run `./update.sh` from your clone; deployment handles tool pins, setup, and the service restart. This initial schema is version 1; future incompatible changes require an explicit migration.

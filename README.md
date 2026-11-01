# MyFlix

**Your home video library, with a streaming-app feel.**

MyFlix turns a folder of films and series into a TV-friendly library with cover art, continue watching, and automatic next episodes. Built for a Raspberry Pi and the browser on a VIDAA TV, it brings watching, catalog management, and downloads together in one small service.

![MyFlix TV home screen with a featured series, Resume and Episodes actions, and a Continue watching shelf](res/home.png)

[Get started](#get-started) · [Add your media](#add-your-media) · [Run on Raspberry Pi](#run-on-raspberry-pi) · [Detailed guide](docs/guide.md)

## Why MyFlix?

- **Made for the sofa.** Browse horizontal shelves with your remote, open seasons and episodes, and watch in an edge-to-edge player whose controls fade away.
- **Pick up where you left off.** Resume across household devices, track watched episodes automatically, and move to the next episode without leaving fullscreen.
- **Manage everything in one place.** Search providers, queue downloads, edit titles, organize series, and choose your library folder from the built-in admin interface.
- **Keep up with series.** Follow a show to download missing unwatched episodes from existing and future seasons, or choose notification-only mode. Watched episodes and excluded seasons are skipped automatically.
- **Covers without API keys.** MyFlix uses local artwork first and caches provider covers in the background when available.
- **A small stack to maintain.** One systemd service, SQLite, plain HTML/CSS/JavaScript, and native browser video. No containers, Redis, Node build, or playback transcoding service.

[VibraVid](https://github.com/AstraeLabs/VibraVid) is bundled as the download engine. MyFlix installs a pinned revision, starts and stops it, and publishes completed downloads into your selected library. You do not need to configure a separate engine website, port, or shared secret.

## Manage your collection

![MyFlix management screen showing provider search, library and following navigation, and download progress](res/manage_download.png)

The admin interface works with a desktop pointer or TV remote. Use **Downloads** to find titles and manage the queue, **Library** to edit your collection, **Following** to monitor series, and **Settings** to select storage and download limits. **Watched and removed** keeps history accessible after video files are removed.

## Get started

You need [mise](https://mise.jdx.dev/), Git, and network access for setup. Mise installs the pinned Python and uv versions; no global Python changes are needed. For a Raspberry Pi deployment, use **64-bit Raspberry Pi OS**.

```sh
git clone https://github.com/cmunaro/MyFlix.git
cd MyFlix
mise trust
mise install
mise run setup
mise run dev
```

Setup fetches the pinned VibraVid source and installs the frozen Python dependencies. Start commands from the project root.

| Open | Address |
| --- | --- |
| Watch on this computer | `http://localhost:8090` |
| Manage the library and downloads | `http://localhost:8090/admin` |
| Watch on your TV | `http://<server-LAN-IP>:8090` |

Connect the TV to the same home network, open the TV address in its browser, and bookmark it. Replace `<server-LAN-IP>` with your server's local IP address. Keep MyFlix running while watching or downloading.

**Trusted home LAN only:** admin is password-free. Anyone who can reach MyFlix can manage the library and downloads. Do not port-forward it; use a VPN or an authenticated TLS reverse proxy for remote access.

## Add your media

1. Open **Admin → Settings** and choose an **existing absolute path** to your video folder. A default `data/library/` folder is created for you.
2. Copy completed videos into that folder, or use **Downloads** to search a provider and queue a film or episodes. Keep the library separate from the engine's download staging folder.
3. Scan from **Library**, or wait for the background scan, which runs every 60 seconds. Files must be non-empty and at least 60 seconds old before import.
4. Return to the TV home screen to browse and play. Refresh the page to see newly imported titles or covers.

MyFlix recognizes MP4, M4V, MKV, WebM, and MOV files. Episode names such as these are detected automatically:

```text
The.Show.S01E02.Episode.Title.mp4
The.Show.1x03.Episode.Title.mp4
```

Correct unusual names and metadata in **Library**. Local JPEG artwork is supported, including `poster.jpg`, `fanart.jpg`, and `<video>-poster.jpg` sidecars. Provider artwork fills gaps when a confident match is available; otherwise titles get a text-based cover.

Catalog edits and watch progress survive rescans and missing files. An unavailable mount preserves the previous catalog and reports a scan error. Removing a video through admin deletes its library file but keeps its catalog details and watch history; sidecars and staging originals remain.

### Follow a series

In **Downloads**, find a series, set **Library series name** to its existing catalog name if applicable, and select **Follow series**. MyFlix checks all seasons every four hours while running. **Save & check now** schedules an earlier check, normally within about 30 seconds.

Following can catch up on existing episodes as well as discover new ones. In **Following**, pause monitoring or choose notification-only mode. Use **mark a season watched & exclude downloads** to skip seasons you have already seen. Notifications stay inside MyFlix.

Failed downloads appear with season, episode, and error details in Downloads. Following retains confirmed episode failures and provides a **Retry failed episode** action. **Retry all failed downloads** moves eligible failures out of Recent failures into a persistent priority queue. Retries start ahead of ordinary queued downloads, while running jobs finish normally. A retry that fails again returns with its new error. Failures require an explicit retry; they do not silently restart. If a download request times out, check the queue before retrying. MyFlix does not automatically retry uncertain download submissions. Provider availability, accounts, and any required optional modules depend on the upstream provider.

## Use your TV remote

| Input | Action |
| --- | --- |
| Arrow keys / D-pad | Move through shelves, episodes, and controls |
| OK / Enter | Select; toggle playback when the video has focus |
| Left / Right with video focused | Seek backward / forward 10 seconds |
| Up / Down during playback | Access player controls |
| Back during playback | Close the track chooser, exit fullscreen, dismiss controls, then return to the library as applicable |
| OK / Back in an admin field | Start / finish editing |

Playback controls hide after three idle seconds while playing, even if a button had focus. Pause keeps them visible. Near an episode's end, a separate next-episode prompt appears; playback advances automatically at completion when another available episode exists. Progress is shared by the household, with no separate user profiles. Series in **Continue watching** stay out of **Not watched**. Cards outside Continue watching show only artwork and title; resume badges and playback progress stay in Continue watching.

Audio and subtitle selection appears when the browser exposes suitable native tracks. Subtitles start off for each video. External subtitle files are not imported.

### Playback compatibility

MyFlix plays video using the TV's native codecs. **MP4 with H.264 video and AAC audio at 720p or 1080p is the recommended starting point.** A file appearing in the library does not guarantee that your TV can decode it. There is no playback transcoding, automatic codec conversion, or DRM handling.

VIDAA firmware controls its own browser bars, pointer, and fullscreen behavior. MyFlix requests fullscreen and supports remote navigation, but cannot guarantee that native browser overlays disappear. Test playback, seek, resume, audio, and remote keys on your actual TV. **Physical VIDAA and Raspberry Pi validation has not been performed.**

## Run on Raspberry Pi

For an always-on installation, use the supplied [systemd unit](deploy/myflix.service) and [environment example](deploy/myflix.env.example). The expected layout is:

| Location | Purpose |
| --- | --- |
| `/opt/myflix` | Application, virtual environment, and pinned tools |
| `/var/lib/myflix` | Databases, settings, artwork cache, and engine staging |
| `/srv/media/library` | Completed videos |

Follow the [Raspberry Pi service installation](docs/guide.md#raspberry-pi-service) for commands to install the pinned tools under `/opt`, create the service account, and grant media access. This keeps the interpreter accessible with the unit's home-directory protection enabled.

After starting the service, select `/srv/media/library` in admin. For another mount, update `RequiresMountsFor` and `ReadWritePaths` in the unit as well as the library setting. The service user needs read/write access; media under `/home` is blocked by `ProtectHome`.

```sh
sudo systemctl status myflix
sudo journalctl -u myflix -f
curl http://127.0.0.1:8090/health
```

Download and processing limits are configurable in **Settings** and apply after restart. Defaults are four downloads and one processing job. Higher limits use more RAM, bandwidth, and temporary disk space; tune them for your hardware. Engine helper binaries are obtained on demand, and their availability on Raspberry Pi needs validation on the target.

## Data, backups, and updates

Local runs store state under `./data` by default; set `MYFLIX_DATA` to use another location. `MYFLIX_HOST` and `MYFLIX_PORT` control the listening address and port, defaulting to `0.0.0.0:8090`.

SQLite stores catalog edits, settings, follows, exclusions, and household watch history. Completed media lives in the library folder chosen in admin. Successful downloads are published atomically; existing videos are never overwritten. Staging originals remain and can continue to occupy disk space.

To back up, stop MyFlix and copy the entire data directory, including any SQLite `-wal` and `-shm` files, then restart. Back up media separately. Keep backups private: the data directory also contains internal secrets and provider login files.

Before updating, back up state and stop the service. Update the checkout, run `mise install` and `mise run setup` using the same `MISE_DATA_DIR` as installation, then restart. See the [operating guide](docs/guide.md#tests-and-backup) for more detail.

## Contribute

Start with the [contributor instructions](AGENTS.md) and keep changes suited to a Raspberry Pi and TV browser: native playback, visible remote focus, ES5 JavaScript, and a single service.

```sh
mise run setup
mise run test
mise run test-tv
```

The Python suite covers catalog behavior, security boundaries, the engine lifecycle, and download publication. TV tests cover navigation and player behavior with DOM fixtures; the test task pins its own Node version, while production requires no Node runtime. These checks do not replace testing on physical hardware.

For bug reports, include the server OS, TV model and firmware, steps to reproduce, and relevant logs. For playback problems, include the container and audio/video codecs. Remove credentials and internal tokens from logs before sharing.

## Further reading

- [Setup and operating guide](docs/guide.md) — detailed behavior and configuration.
- [Following series and watch history](docs/guide.md#following-series-and-keeping-history).
- [Artwork lookup and caching](docs/guide.md#automatic-covers).
- [VibraVid integration, storage, and processing](docs/guide.md#vibravid-integration).
- [VIDAA browser and codec limitations](docs/guide.md#tv-compatibility).

VibraVid retains its upstream **GPL-3.0-or-later** license; its source and license are included in the pinned checkout.

# MyFlix contributor instructions

## Local environment

@/Users/cmunaro/.codex/RTK.md

Use mise for pinned tools. Run commands from the project root. Do not change
global tool versions or Git configuration to work around project problems.

```sh
mise install
mise run setup
mise run dev
mise run test
mise run test-tv
```

`MYFLIX_ADMIN_PASSWORD` must be set before starting the application. Setup fetches
the pinned VibraVid source before syncing the frozen Python dependency lock.
The optional TV test task pins its own Node version; production needs no Node.

## Product and deployment

- MyFlix is a household media library for a VIDAA TV, deployed on a Raspberry Pi
  with limited RAM as one systemd service.
- The TV interface must behave like a streaming TV app: remote focus navigation,
  horizontal shelves, an edge-to-edge player, and transient playback controls.
  Do not replace it with a conventional website layout or persistent button bar.
- Admin manages the catalog and downloads within MyFlix. VibraVid is a bundled,
  pinned engine managed by MyFlix, not a separately configured website link.
- SQLite owns catalog settings and household watch progress. The library folder
  is selected in admin; the completion hook reads that current setting.
- Playback uses the TV's native codecs. Avoid server transcoding, heavy frontend
  frameworks, extra services, and dependencies without a concrete requirement.

## Ownership and invariants

- `myflix/app.py` owns HTTP routes, authentication, CSRF, and playback metadata.
- `myflix/db.py` owns SQLite connections and schema initialization.
- `myflix/catalog.py` owns scans, filename inference, and media path confinement.
- `myflix/engine.py` owns engine configuration, readiness, lifecycle, and shutdown.
  `scripts/setup.py` installs the source revision pinned in `pyproject.toml`;
  `uv.lock` locks its dependencies. Do not edit `.vibravid/` as application code.
- `myflix/vibravid.py` owns the restricted server-side engine API boundary.
- `deploy/vibravid_publish.py` publishes successful downloads atomically. Keep
  active downloads separate from the library; never overwrite existing videos.
- `myflix/static/tv.js` owns browsing navigation and shared fullscreen behavior.
  `player.js` owns playback controls, remote media keys, progress, and episode
  transitions. Each input must have one owner to avoid duplicate actions.
- Preserve TV compatibility: use ES5 syntax in shipped JavaScript, feature-detect
  platform APIs, and retain visible remote focus. Desktop pointer behavior must
  remain usable. Web pages cannot guarantee control of VIDAA's native browser UI.
- Controls must hide during idle playback even when a button previously had
  focus. Seek must work when metadata was cached before script initialization.
  Watched tracking is automatic; do not add a manual watched button to the player.
- Near the end of a series episode, show an independent next-episode prompt.
  Automatically advance at completion using the same video element to preserve
  fullscreen. Exclude hidden/unavailable episodes and stop at the final episode.
- Preserve metadata edits and watch history during rescans and missing mounts.
  Enforce configured-root confinement, authentication, CSRF, and input validation.
  Keep internal engine secrets server-side and never retry download mutations
  automatically after uncertain failures.

## Verification and commits

Trace the affected behavior before editing. Extend its responsible layer and
prefer the smallest complete implementation. Run focused checks for the change:
Python tests cover backend, engine lifecycle, and publication; `test-tv` covers
remote navigation and player behavior. A DOM fixture does not verify actual
VIDAA firmware or Raspberry Pi hardware; state any unverified device behavior.

Keep `README.md` and deployment files aligned with changed setup or behavior.
Never commit runtime data, media, credentials, `.env`, virtual environments,
downloaded engine source, or tool caches. Inspect staged changes before committing.

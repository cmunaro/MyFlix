"""Concurrency adapters for the pinned engine, without editing its source."""
import hashlib
import shlex
import sys
import tempfile
import threading
from pathlib import Path

from .db import connect, settings


def validate_limits(downloads, processing):
    try:
        values = int(downloads), int(processing)
    except (TypeError, ValueError):
        raise ValueError("Download and processing limits must be whole numbers from 1 to 10") from None
    if any(not 1 <= value <= 10 for value in values):
        raise ValueError("Download and processing limits must be whole numbers from 1 to 10")
    return values


def read_limits(database):
    with connect(database) as db:
        config = settings(db)
    return validate_limits(config.get("download_limit", "4"), config.get("processing_limit", "1"))


class ToolWrappers:
    """Guard FFmpeg and muxing helpers, including subprocesses launched by tools."""
    def __init__(self, state, processing):
        self.state = Path(state)
        self.processing = processing
        self.lock = threading.Lock()

    def wrap(self, binary, *, ffprobe=None):
        binary = str(Path(binary).resolve(strict=True))
        key = hashlib.sha256(f"{binary}:{self.processing}".encode()).hexdigest()[:16]
        directory = self.state / "tools" / key
        target = directory / Path(binary).name
        with self.lock:
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            if not target.exists():
                runner = Path(__file__).resolve().parent.parent / "deploy" / "limited_process.py"
                command = shlex.join([sys.executable, str(runner), "--directory", str(self.state / "processing"),
                                      "--slots", str(self.processing), "--binary", binary, "--"])
                with tempfile.NamedTemporaryFile(mode="w", dir=directory, delete=False) as output:
                    temporary = Path(output.name)
                    output.write('#!/bin/sh\nexec ' + command + ' "$@"\n')
                temporary.chmod(0o700)
                temporary.replace(target)
            # yt-dlp discovers ffprobe beside the FFmpeg executable.
            if ffprobe and not (directory / "ffprobe").exists():
                (directory / "ffprobe").symlink_to(Path(ffprobe).resolve(strict=True))
        return str(target)


def install_tools(state, processing):
    import VibraVid.setup as setup

    wrappers = ToolWrappers(state, processing)
    for name in ("get_ffmpeg_path", "get_mkvmerge_path", "get_mkvpropedit_path", "get_dovi_tool_path"):
        original = getattr(setup, name)

        def resolve(original=original, name=name):
            binary = original()
            if not binary:
                return binary
            probe = setup.get_ffprobe_path() if name == "get_ffmpeg_path" else None
            return wrappers.wrap(binary, ffprobe=probe)

        setattr(setup, name, resolve)


class DownloadStages:
    """Return a network permit before waiting for the bounded processing stage."""
    def __init__(self, acquire, release, processing):
        self.acquire_network = acquire
        self.release_network = release
        self.local = threading.local()
        self.processing = threading.BoundedSemaphore(processing)

    def acquire(self):
        if not getattr(self.local, "network", False):
            self.acquire_network()
            self.local.network = True

    def release(self):
        if getattr(self.local, "network", False):
            self.local.network = False
            self.release_network()

    def finish(self, work, cancelled, waiting):
        self.release()
        waiting()
        while not self.processing.acquire(timeout=0.2):
            if cancelled():
                raise InterruptedError("cancelled")
        try:
            if cancelled():
                raise InterruptedError("cancelled")
            return work()
        finally:
            self.processing.release()


def install_stages(downloads, processing):
    from searchapp import _download_infra as infrastructure
    from searchapp.views import _shared
    from VibraVid.core.downloader.base import BaseDownloader
    from VibraVid.core.downloader._generic import Generic_Downloader
    from VibraVid.core.downloader.yt_dlp_downloader import YTDLPDownloader
    from VibraVid.core.ui.tracker import context_tracker

    infrastructure.set_max_download_slots(downloads)
    stages = DownloadStages(infrastructure._acquire_download_slot, infrastructure._release_download_slot, processing)
    # _shared imports these functions by value. Replace both owners together.
    def acquire():
        stages.acquire()
        context_tracker.no_livemux = True
        context_tracker.no_live_decrypt = True

    infrastructure._acquire_download_slot = _shared._acquire_download_slot = acquire
    infrastructure._release_download_slot = _shared._release_download_slot = stages.release
    precheck = BaseDownloader._precheck
    merge = BaseDownloader._merge_files
    finish = BaseDownloader._finalize
    generic_start = Generic_Downloader.start
    ytdlp_start = YTDLPDownloader.start

    def prepare(downloader, *args, **kwargs):
        # A season task reuses its worker for subsequent episodes; every episode
        # must reacquire a network permit after the previous processing stage.
        acquire()
        return precheck(downloader, *args, **kwargs)

    def process(method, downloader, *args, **kwargs):
        def work():
            downloader._update_status("Processing")
            return method(downloader, *args, **kwargs)
        return stages.finish(work, downloader._gui_aborted,
                             lambda: downloader._update_status("Waiting for processing"))

    def start_generic(downloader, *args, **kwargs):
        acquire()
        return generic_start(downloader, *args, **kwargs)

    def start_ytdlp(downloader, *args, **kwargs):
        from VibraVid.setup import get_ffmpeg_path
        acquire()
        downloader.extra_args["ffmpeg_location"] = get_ffmpeg_path()
        return ytdlp_start(downloader, *args, **kwargs)

    BaseDownloader._precheck = prepare
    BaseDownloader._merge_files = lambda self, *a, **kw: process(merge, self, *a, **kw)
    BaseDownloader._finalize = lambda self, *a, **kw: process(finish, self, *a, **kw)
    Generic_Downloader.start = start_generic
    YTDLPDownloader.start = start_ytdlp
    # Prevent per-provider preferences from re-enabling in-flight FFmpeg muxing.
    BaseDownloader._maybe_enable_streaming_mux = lambda self: None

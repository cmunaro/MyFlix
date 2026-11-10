"""Concurrency adapters for the pinned engine, without editing its source."""
import hashlib
import heapq
import logging
import shlex
import sys
import tempfile
import threading
import time
from pathlib import Path
from concurrent.futures import Future
from contextlib import contextmanager

from .db import connect, settings


def validate_limits(downloads, processing):
    try:
        values = int(downloads), int(processing)
    except (TypeError, ValueError):
        raise ValueError("Job and processing limits must be whole numbers from 1 to 10") from None
    if any(not 1 <= value <= 10 for value in values):
        raise ValueError("Job and processing limits must be whole numbers from 1 to 10")
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
        # Mise and other multicall tools dispatch by the invoked shim's name.
        # Dereferencing ffmpeg -> mise would run mise with FFmpeg arguments.
        binary = Path(binary).absolute()
        binary.stat()
        binary = str(binary)
        if ffprobe:
            ffprobe = Path(ffprobe).absolute()
            ffprobe.stat()
        key = hashlib.sha256(f"{binary}:{self.processing}:{ffprobe}".encode()).hexdigest()[:16]
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
                (directory / "ffprobe").symlink_to(ffprobe)
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
    """Bound processing within jobs that retain their scheduler slot throughout."""
    def __init__(self, processing):
        self.processing = threading.BoundedSemaphore(processing)
        self.local = threading.local()

    def finish(self, work, cancelled, waiting):
        if cancelled():
            raise InterruptedError("cancelled")
        if getattr(self.local, "processing", False):
            return work()
        waiting()
        while not self.processing.acquire(timeout=0.2):
            if cancelled():
                raise InterruptedError("cancelled")
        try:
            self.local.processing = True
            if cancelled():
                raise InterruptedError("cancelled")
            return work()
        finally:
            self.local.processing = False
            self.processing.release()


def install_publication(tracker, database, stages):
    """Publish verified outputs before the engine records successful completion."""
    from deploy.vibravid_publish import publish_completed
    from .credits import Cancelled

    original = tracker.complete_download
    finishing = set()
    downloads = Path(database).resolve().parent / "vibravid" / "downloads"

    def complete(download_id, success=True, error=None, path=None):
        if not success or not path:
            return original(download_id, success=success, error=error, path=path)
        with tracker._lock:
            if download_id in finishing:
                return
            known = download_id in tracker.downloads or any(
                item.get("id") == download_id and item.get("status") == "timed_out" for item in tracker.history)
            if not known:
                return  # Duplicate terminal notification; never publish again.
            finishing.add(download_id)
            stop = tracker.stop_events.setdefault(download_id, threading.Event())
        try:
            if tracker.is_stopped(download_id):
                stop.set()
            def work():
                tracker.update_status(download_id, "Processing")
                publish_completed(path, downloads, database=database, stop=stop)
            try:
                stages.finish(work, stop.is_set, lambda: tracker.update_status(download_id, "Waiting for processing"))
            except (Cancelled, InterruptedError):
                success, error = False, "cancelled"
            except FileExistsError:
                logging.exception("Library file already exists; staging file retained")
                success, error = False, "A file already exists in the library for this download. Nothing was overwritten; the staging file was retained. Check the library before downloading again."
            except Exception:
                logging.exception("Completed download could not be published; staging file retained")
                success, error = False, "Library publication failed. Check the service logs; the staging file was retained."
            return original(download_id, success=success, error=error, path=path)
        finally:
            with tracker._lock:
                finishing.discard(download_id)

    tracker.complete_download = complete


class PriorityDownloads:
    """Admit queued retries before ordinary jobs, without preempting running work."""
    def __init__(self, limit, active_changed, database=None):
        self.limit = limit
        self.active_changed = active_changed
        self.condition = threading.Condition()
        self.local = threading.local()
        self.pending = []
        self.sequence = 0
        self.active = 0
        self.stopped = False
        self.workers = []
        self.database = database
        self.retry_check_at = 0
        self.retry_pending = False

    def ordinary_blocked(self):
        if self.stopped:
            return False
        if self.database and time.monotonic() >= self.retry_check_at:
            with connect(self.database) as db:
                self.retry_pending = bool(db.execute("""SELECT 1 FROM download_retries
                    WHERE root=? AND state='pending' LIMIT 1""", (settings(db)["videos_path"],)).fetchone())
            self.retry_check_at = time.monotonic() + 1
        return self.retry_pending

    @contextmanager
    def prioritize(self):
        previous = getattr(self.local, "priority", False)
        self.local.priority = True
        try:
            yield
        finally:
            self.local.priority = previous

    def submit(self, work):
        future = Future()
        with self.condition:
            if self.stopped:
                raise RuntimeError("Cannot submit a download after shutdown")
            heapq.heappush(self.pending, (0 if getattr(self.local, "priority", False) else 1,
                                         self.sequence, future, work))
            self.sequence += 1
            self.retry_check_at = 0
            if not self.workers:
                for index in range(self.limit):
                    worker = threading.Thread(target=self.run, name=f"DownloadWorker-{index}", daemon=True)
                    self.workers.append(worker)
                    worker.start()
            self.condition.notify_all()
        return future

    def run(self):
        while True:
            with self.condition:
                while (not self.pending or (self.pending[0][0] == 1 and self.ordinary_blocked())):
                    if self.stopped and not self.pending:
                        return
                    self.condition.wait(timeout=1)
                _, _, future, work = heapq.heappop(self.pending)
                if not future.set_running_or_notify_cancel():
                    continue
                self.active += 1
                self.active_changed(self.active)
            try:
                future.set_result(work())
            except BaseException as error:
                future.set_exception(error)
            finally:
                with self.condition:
                    self.active -= 1
                    self.active_changed(self.active)
                    self.retry_check_at = 0
                    self.condition.notify_all()

    def shutdown(self, wait=True):
        with self.condition:
            self.stopped = True
            self.condition.notify_all()
        if wait:
            for worker in self.workers:
                worker.join()


def install_stages(downloads, processing, *, database=None):
    from searchapp import _download_infra as infrastructure
    from searchapp.views import _shared
    from VibraVid.core.downloader.base import BaseDownloader
    from VibraVid.core.downloader._generic import Generic_Downloader
    from VibraVid.core.downloader.yt_dlp_downloader import YTDLPDownloader
    from VibraVid.core.ui.tracker import context_tracker, download_tracker

    infrastructure.set_max_download_slots(downloads)
    stages = DownloadStages(processing)
    if database:
        install_publication(download_tracker, database, stages)
    scheduler = PriorityDownloads(downloads, lambda count: setattr(infrastructure, "_active_downloads", count), database)
    infrastructure.download_executor.shutdown(wait=True)
    infrastructure.download_executor = scheduler
    infrastructure._submit_download_task = _shared._submit_download_task = scheduler.submit
    add_scheduled = _shared._add_scheduled_download

    def schedule(download_id, *args, **kwargs):
        add_scheduled(download_id, *args, **kwargs)
        with infrastructure.scheduled_downloads_lock:
            infrastructure.scheduled_downloads[download_id]["priority"] = getattr(scheduler.local, "priority", False)

    _shared._add_scheduled_download = schedule
    # _shared imports these functions by value. Replace both owners together.
    def acquire():
        context_tracker.no_livemux = True
        context_tracker.no_live_decrypt = True

    infrastructure._acquire_download_slot = _shared._acquire_download_slot = acquire
    # The scheduler owns each job's slot until its callable returns, including
    # all episodes, processing, hooks, cancellation, and error cleanup.
    infrastructure._release_download_slot = _shared._release_download_slot = lambda: None
    precheck = BaseDownloader._precheck
    merge = BaseDownloader._merge_files
    finish = BaseDownloader._finalize
    generic_start = Generic_Downloader.start
    ytdlp_start = YTDLPDownloader.start

    def prepare(downloader, *args, **kwargs):
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

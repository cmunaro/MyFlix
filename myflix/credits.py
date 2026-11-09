"""Cached credits detection using text presence and bounded frame bisection."""
import hashlib
import fcntl
import json
import logging
import math
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from contextlib import contextmanager

from PIL import Image, ImageOps

from .catalog import identify, media_path
from .db import connect, settings

VERSION = 4


class Cancelled(Exception):
    pass


def command(args, stop, timeout=60):
    """Bound and reap each native tool; inherit the service's shutdown group."""
    if stop.is_set():
        raise Cancelled()
    env = dict(os.environ, OMP_THREAD_LIMIT="1")
    # nice affects the child only. No shell, shared engine state or global settings.
    with subprocess.Popen(["nice", "-n", "10", *map(str, args)], stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, env=env) as child:
        deadline = time.monotonic() + timeout
        try:
            while True:
                if stop.is_set():
                    raise Cancelled()
                if time.monotonic() >= deadline:
                    raise TimeoutError("Credits analysis command timed out")
                try:
                    output, _ = child.communicate(timeout=.2)
                    break
                except subprocess.TimeoutExpired:
                    continue
            if child.returncode:
                raise RuntimeError("Credits analysis tool failed")
            return output.decode("utf-8", "replace")
        finally:
            if child.poll() is None:
                try:
                    child.kill()
                except ProcessLookupError:
                    pass
                child.communicate()


def text_region(image):
    """Find rows of letter-sized components without reading their words.

    Work at 320 pixels wide, discarding large solid shapes such as clouds.
    A single text line is enough, including a card containing only names.
    """
    gray = ImageOps.grayscale(image)
    histogram = gray.histogram()
    total = sum(histogram)
    weighted = sum(value * count for value, count in enumerate(histogram))
    weight = subtotal = 0
    best, threshold = -1, 0
    # Otsu's threshold separates foreground from background without assuming
    # white lettering: fixed brightness cutoffs discard gold and dim credits.
    for value, count in enumerate(histogram):
        weight += count
        subtotal += value * count
        if not weight or weight == total:
            continue
        separation = weight * (total - weight) * (subtotal / weight - (weighted - subtotal) / (total - weight)) ** 2
        if separation > best:
            best, threshold = separation, value
    bright = total - sum(histogram[:threshold + 1])
    foreground_is_bright = bright <= total / 2
    processed = gray.point(lambda value: 0 if (value > threshold) == foreground_is_bright else 255)
    scale = max(1, processed.width / 320)
    small = processed.resize((round(processed.width / scale), max(1, round(processed.height / scale))),
                             Image.Resampling.NEAREST)
    width, height = small.size
    mask = bytearray(small.tobytes())
    cursor, letters = 0, []
    while True:
        seed = mask.find(b'\0', cursor)
        if seed < 0:
            break
        cursor = seed + 1
        mask[seed] = 255
        pending = [seed]
        left = right = seed % width
        top = bottom = seed // width
        area = 0
        while pending:
            pixel = pending.pop()
            x, y = pixel % width, pixel // width
            area += 1
            left, right = min(left, x), max(right, x)
            top, bottom = min(top, y), max(bottom, y)
            for yy in range(max(0, y - 1), min(height, y + 2)):
                for xx in range(max(0, x - 1), min(width, x + 2)):
                    neighbor = yy * width + xx
                    if mask[neighbor] == 0:
                        mask[neighbor] = 255
                        pending.append(neighbor)
        w, h = right - left + 1, bottom - top + 1
        if 3 <= h <= height * .12 and 1 <= w <= h * 2 and area >= 2:
            letters.append((left, top, right + 1, bottom + 1))

    rows = []
    for box in sorted(letters, key=lambda b: (b[1] + b[3]) / 2):
        center = (box[1] + box[3]) / 2
        if rows and abs(center - rows[-1][0]) <= max(3, (box[3] - box[1]) * .4):
            rows[-1][1].append(box)
        else:
            rows.append((center, [box]))

    def bounds(boxes):
        return (min(b[0] for b in boxes), min(b[1] for b in boxes),
                max(b[2] for b in boxes), max(b[3] for b in boxes))

    lines = []
    for _, boxes in rows:
        segment = []
        for box in sorted(boxes):
            if segment and box[0] - segment[-1][2] > max(box[3] - box[1], segment[-1][3] - segment[-1][1]) * 3:
                if len(segment) >= 6:
                    lines.append(bounds(segment))
                segment = []
            segment.append(box)
        if len(segment) >= 6:
            lines.append(bounds(segment))
    if not lines:
        return None
    left, top, right, bottom = bounds(lines)
    crop = (max(0, left - 10), max(0, top - 10), min(width, right + 10), min(height, bottom + 10))
    return processed.crop(tuple(round(value * scale) for value in crop))


def analyze(path, ffmpeg, ffprobe, stop):
    info = json.loads(command([ffprobe, "-v", "error", "-show_entries", "format=duration",
                               "-of", "json", path], stop))
    duration = float(info["format"]["duration"])
    if not math.isfinite(duration) or not 60 <= duration <= 7 * 86400:
        return None
    start = 0
    end = duration - .5
    with tempfile.TemporaryDirectory(prefix="myflix-credits-") as directory:
        directory = Path(directory)
        samples = {}

        def probe(position):
            if stop.is_set():
                raise Cancelled()
            if position not in samples:
                frame = directory / ("frame-%02d.png" % len(samples))
                command([ffmpeg, "-hide_banner", "-loglevel", "error", "-threads", "1",
                         "-ss", str(position), "-i", path, "-frames:v", "1", "-an", "-sn",
                         "-vf", "scale=640:-2", "-filter_threads", "1", "-threads", "1",
                         frame], stop, timeout=10)
                with Image.open(frame) as image:
                    samples[position] = text_region(image)
            return samples[position]

        # Bracket the text transition starting one minute before the end. Text
        # means search earlier; no text means search later, away from the story.
        pivot = max(start, duration - 60)
        if probe(pivot) is not None:
            upper = pivot
            distance = 30
            while True:
                lower = max(start, upper - distance)
                if lower == upper:
                    logging.info("Credits transition unavailable: text also present at video start")
                    return None
                if probe(lower) is None:
                    break
                upper = lower
                distance *= 2
        else:
            lower = pivot
            upper = min(end, lower + 30)
            while probe(upper) is None:
                if upper >= end:
                    logging.info("Credits transition unavailable: no text found at sampled ending positions")
                    return None
                lower = upper
                upper = min(end, (upper + duration) / 2)

        # The sampled transition is heuristic: credits can have gaps and story
        # scenes can contain text. Never claim a global first-text guarantee.
        while upper - lower > 3:
            middle = (lower + upper) / 2
            if probe(middle) is None:
                lower = middle
            else:
                upper = middle

        return round(upper, 3)


def fingerprint(path, *, location=None):
    stat = path.stat()
    return hashlib.sha256(json.dumps([VERSION, str(location or path), stat.st_size, stat.st_mtime_ns]).encode()).hexdigest()


class CreditsCache:
    def __init__(self, database):
        self.database = database

    @contextmanager
    def processing(self, stop):
        # One analysis across the engine's completion stage and library backfill.
        with (Path(self.database).resolve().parent / "credits.lock").open("a") as lock:
            while True:
                if stop.is_set():
                    raise Cancelled()
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    stop.wait(.2)
            yield

    def _cached(self, signature):
        with connect(self.database) as db:
            return db.execute("SELECT position,retry_at FROM credits WHERE fingerprint=?", (signature,)).fetchone()

    def _record(self, signature, position, retry_at):
        with connect(self.database) as db:
            db.execute("INSERT OR REPLACE INTO credits VALUES(?,?,?)", (signature, position, retry_at))

    def prepare(self, path, target, stop):
        """Analyze the hidden completed copy and cache its final-path result."""
        if not identify(target)[1]:
            return
        with self.processing(stop):
            signature = fingerprint(path, location=target)
            cached = self._cached(signature)
            if cached and (cached["retry_at"] == 0 or cached["retry_at"] > time.time()):
                return
            retry_at = 0
            try:
                position = analyze(path, *self.tools(), stop)
            except Cancelled:
                raise
            except Exception:
                logging.exception("Credits detection failed during download processing; using playback fallback")
                position, retry_at = None, time.time() + 3600
            if stop.is_set():
                raise Cancelled()
            if fingerprint(path, location=target) != signature:
                raise ValueError("Completed video changed during credits detection")
            self._record(signature, position, retry_at)

    def tools(self):
        bundled = Path(self.database).resolve().parent / "vibravid" / "binary" / "tools"
        result = []
        for name in ("ffmpeg", "ffprobe"):
            binary = shutil.which(name)
            if not binary and os.access(bundled / name, os.X_OK):
                binary = str(bundled / name)
            if not binary:
                raise RuntimeError(f"Credits detection needs {name}; see README setup instructions")
            result.append(binary)
        return result

    def position(self, item):
        if not item["series"]:
            return None
        try:
            signature = fingerprint(media_path(item["root"], item["path"]))
        except (OSError, ValueError):
            return None
        row = self._cached(signature)
        return row[0] if row else None

    def step(self, stop):
        with self.processing(stop):
            return self._step(stop)

    def _step(self, stop):
        with connect(self.database) as db:
            root = settings(db).get("videos_path", "")
            rows = db.execute("""SELECT m.* FROM media m
                LEFT JOIN progress p ON p.media_id=m.id
                WHERE m.root=? AND m.available=1 AND m.hidden=0 AND m.series!=''
                ORDER BY COALESCE(p.updated_at,'') DESC,m.id""", (root,)).fetchall()
        for row in rows:
            if stop.is_set():
                raise Cancelled()
            item = dict(row)
            try:
                path = media_path(root, item["path"])
                signature = fingerprint(path)
            except (OSError, ValueError):
                continue
            cached = self._cached(signature)
            if cached and (cached["retry_at"] == 0 or cached["retry_at"] > time.time()):
                continue
            # Resolve prerequisites before reserving a result; missing tools never
            # turn a whole library into permanent "no credits found" entries.
            tools = self.tools()
            retry_at = 0
            try:
                position = analyze(path, *tools, stop)
            except Cancelled:
                raise
            except Exception:
                logging.exception("Credits analysis failed for media %s; retrying in an hour", item["id"])
                position = None
                retry_at = time.time() + 3600
            # Files, mounts and the configured root can change during analysis.
            try:
                if fingerprint(media_path(root, item["path"])) != signature:
                    return True
            except (OSError, ValueError):
                return True
            with connect(self.database) as db:
                db.execute("BEGIN IMMEDIATE")
                if settings(db).get("videos_path") != root:
                    return True
                current = db.execute("SELECT root,path,available,hidden FROM media WHERE id=?", (item["id"],)).fetchone()
                if not current or tuple(current) != (root, item["path"], 1, 0):
                    return True
                db.execute("INSERT OR REPLACE INTO credits VALUES(?,?,?)", (signature, position, retry_at))
            logging.info("Credits analysis media %s: %s", item["id"], position if position is not None else "fallback")
            return True
        return False

    def run(self, stop):
        while not stop.is_set():
            try:
                if self.step(stop):
                    continue
            except Cancelled:
                return
            except Exception:
                logging.exception("Credits worker unavailable; retrying in an hour")
                stop.wait(3600)
                continue
            stop.wait(60)

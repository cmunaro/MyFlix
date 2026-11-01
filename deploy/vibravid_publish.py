#!/usr/bin/env python3
"""VibraVid post_download hook: publish completed files without moving originals."""
import argparse
import ctypes
import errno
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path


def _rename_no_replace(source, target):
    """Publish on Linux filesystems such as exFAT without replacing any entry."""
    libc = ctypes.CDLL(None, use_errno=True)
    rename = getattr(libc, "renameat2", None)
    if rename is None:
        raise OSError(errno.ENOSYS, "Atomic no-replace rename is unavailable", str(target))
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    # AT_FDCWD, RENAME_NOREPLACE. Never fall back to an overwriting rename.
    if rename(-100, os.fsencode(source), -100, os.fsencode(target), 1):
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), str(target))


def publish(source, downloads, library):
    downloads = Path(downloads).resolve(strict=True)
    library = Path(library).resolve(strict=True)
    source = Path(source).resolve(strict=True)
    if library == downloads or library.is_relative_to(downloads) or downloads.is_relative_to(library):
        raise ValueError("Download and library folders must be separate, non-nested directories")
    if not source.is_file() or not source.is_relative_to(downloads):
        raise ValueError("Download must be a file within the downloads folder")
    target = library / source.relative_to(downloads)
    if not target.parent.resolve().is_relative_to(library):
        raise ValueError("Destination escapes the library")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() or target.is_symlink():
        raise FileExistsError(f"Library file already exists: {target}")
    # Store independent bytes on the configured library filesystem. The hidden
    # copy becomes visible only when complete; never replace an existing entry.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".myflix-", dir=target.parent, delete=False) as output:
            temporary = Path(output.name)
            with source.open("rb") as incoming:
                shutil.copyfileobj(incoming, output, 1024 * 1024)
            output.flush()
            os.fsync(output.fileno())
        shutil.copystat(source, temporary)
        _rename_no_replace(temporary, target)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--downloads", required=True)
    destination = parser.add_mutually_exclusive_group(required=True)
    destination.add_argument("--library")
    destination.add_argument("--database", help="Read the current library folder from MyFlix")
    args = parser.parse_args()
    if os.environ.get("SC_DOWNLOAD_SUCCESS") != "1":
        return
    library = args.library
    if args.database:
        database = Path(args.database).resolve(strict=True)
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
            row = db.execute("SELECT value FROM settings WHERE key='videos_path'").fetchone()
        if not row or not row[0]:
            raise RuntimeError("Choose a library folder in MyFlix admin before downloading")
        library = row[0]
    source = Path(os.environ["SC_DOWNLOAD_PATH"]).resolve(strict=True)
    print(publish(source, args.downloads, library))
    # VibraVid sidecars become local TV artwork. Only known JPEG names are
    # copied, never arbitrary files from provider/download directories.
    downloads = Path(args.downloads).resolve(strict=True)
    companions = [source.with_name(source.stem + suffix) for suffix in
                  ("-poster.jpg", "-fanart.jpg", "-thumb.jpg")]
    parent = source.parent
    while parent != downloads and parent.is_relative_to(downloads):
        companions.extend(parent / name for name in ("poster.jpg", "fanart.jpg"))
        parent = parent.parent
    for companion in companions:
        if companion.is_file() and not companion.is_symlink():
            try:
                publish(companion, downloads, library)
            except FileExistsError:
                # Series artwork may already have been published with a previous
                # episode. Keep the existing library image.
                pass


if __name__ == "__main__":
    main()

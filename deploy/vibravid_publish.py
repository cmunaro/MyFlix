#!/usr/bin/env python3
"""VibraVid post_download hook: publish completed files without moving originals."""
import argparse
import errno
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path


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
    # Hard links are atomic and cost no extra space. Never overwrite a library
    # file: replacing a title is an explicit administrator operation.
    try:
        os.link(source, target)
        return target
    except FileExistsError:
        if os.path.samefile(source, target):
            return target
        raise FileExistsError(f"Library file already exists: {target}")
    except OSError as error:
        if error.errno != errno.EXDEV:
            raise
    # Across filesystems, copy under a hidden temporary name then atomically link
    # into view. Even concurrent hooks cannot expose a partial file or overwrite.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".myflix-", dir=target.parent, delete=False) as output:
            temporary = Path(output.name)
            with source.open("rb") as incoming:
                shutil.copyfileobj(incoming, output, 1024 * 1024)
            output.flush()
            os.fsync(output.fileno())
        shutil.copystat(source, temporary)
        os.link(temporary, target)
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

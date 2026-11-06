import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from zlib import crc32

from myflix.app import create_app
from myflix.catalog import scan
from myflix.db import connect
from myflix.matroska import CUES, SEEK_HEAD, HeaderOverlay, seekable_header


def element(kind, body):
    identifier = kind.to_bytes((kind.bit_length() + 7) // 8, "big")
    width = next(n for n in range(1, 9) if len(body) < (1 << (7 * n)) - 1)
    return identifier + ((1 << (7 * width)) | len(body)).to_bytes(width, "big") + body


def checked(kind, body, checksum):
    if checksum:
        body = element(0xBF, crc32(body).to_bytes(4, "little")) + body
    return element(kind, body)


def seek(target, position, checksum=False):
    return checked(0x4DBB, element(0x53AB, target.to_bytes(4, "big")) +
                   element(0x53AC, position.to_bytes(8, "big")), checksum)


def fixture(checksum=False):
    """Muxer layout: small initial SeekHead -> tail SeekHead -> Cues."""
    ebml = element(0x1A45DFA3, element(0x4282, b"matroska"))
    head = checked(SEEK_HEAD, seek(SEEK_HEAD, 0, checksum), checksum)
    padding = b"\xec" + ((1 << 56) | 64).to_bytes(8, "big") + bytes(64)
    cluster = element(0x1F43B675, b"media bytes" * 10000)
    cues = element(CUES, b"cue index")
    cue_position = len(head) + len(padding) + len(cluster)
    tail_position = cue_position + len(cues)
    head = checked(SEEK_HEAD, seek(SEEK_HEAD, tail_position, checksum), checksum)
    tail = checked(SEEK_HEAD, seek(CUES, cue_position, checksum), checksum)
    original = ebml + element(0x18538067, head + padding + cluster + cues + tail)
    fixed_head = checked(SEEK_HEAD, seek(SEEK_HEAD, tail_position, checksum) + seek(CUES, cue_position), checksum)
    padding_size = len(head) + len(padding) - len(fixed_head)
    padding = b"\xec" + ((1 << 56) | (padding_size - 9)).to_bytes(8, "big") + bytes(padding_size - 9)
    expected = ebml + element(0x18538067, fixed_head + padding + cluster + cues + tail)
    return original, expected


class MatroskaTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "episode.mkv"

    def tearDown(self):
        self.temp.cleanup()

    def test_chained_cues_are_exposed_without_changing_file_or_offsets(self):
        for checksum in (False, True):
            with self.subTest(checksum=checksum):
                original, expected = fixture(checksum)
                self.path.write_bytes(original)
                before = self.path.stat()
                header = seekable_header(self.path)
                self.assertIsNotNone(header)
                self.assertEqual(header + original[len(header):], expected)
                self.assertEqual(len(original), len(expected))
                self.assertEqual(self.path.read_bytes(), original)
                self.assertEqual(self.path.stat().st_mtime_ns, before.st_mtime_ns)
                self.path.write_bytes(expected)
                self.assertIsNone(seekable_header(self.path), "direct Cues pointers need no overlay")

    def test_unsupported_or_malformed_headers_fall_back(self):
        for data in (b"", b"not a matroska file", element(0x1A45DFA3, b"") + b"\x18\x53"):
            self.path.write_bytes(data)
            self.assertIsNone(seekable_header(self.path))
        original, _ = fixture()
        # A first-header link back to itself must not cause unbounded reads.
        cycle = element(0x1A45DFA3, b"") + element(0x18538067, element(SEEK_HEAD, seek(SEEK_HEAD, 0)))
        self.path.write_bytes(cycle)
        self.assertIsNone(seekable_header(self.path))
        self.path.write_bytes(original.replace(CUES.to_bytes(4, "big"), b"NOPE"))
        self.assertIsNone(seekable_header(self.path))
        self.path.write_bytes(original)
        with patch.object(Path, "open", side_effect=OSError("offline")):
            self.assertIsNone(seekable_header(self.path))

    def test_header_overlay_slices_and_closes_the_underlying_body(self):
        class Body:
            closed = False
            def __iter__(self):
                return iter([b"abcd", b"efgh", b"ijkl"])
            def close(self):
                self.closed = True
        body = Body()
        overlay = HeaderOverlay(body, b"012345", 2)
        self.assertEqual(b"".join(overlay), b"2345efghijkl")
        overlay.close()
        self.assertTrue(body.closed)
        body = Body()
        HeaderOverlay(body, b"012345").close()
        self.assertTrue(body.closed, "HEAD responses close even without iteration")

    def test_http_full_range_conditional_and_head_responses(self):
        original, expected = fixture()
        self.path.write_bytes(original)
        os.utime(self.path, (time.time() - 120, time.time() - 120))
        database = str(Path(self.temp.name) / "catalog.sqlite3")
        app = create_app({"TESTING": True, "DATABASE": database})
        with connect(database) as db:
            db.execute("UPDATE settings SET value=? WHERE key='videos_path'", (self.temp.name,))
        scan(database)
        client = app.test_client()
        with client.get("/media/1") as response:
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data, expected)
            self.assertEqual(response.content_length, len(original))
            etag = response.headers["ETag"]
        for start, stop in ((0, 100), (60, 90), (65530, 65550), (100000, 100031)):
            with client.get("/media/1", headers={"Range": f"bytes={start}-{stop}", "If-Range": etag}) as response:
                self.assertEqual(response.status_code, 206)
                self.assertEqual(response.data, expected[start:stop + 1])
                self.assertEqual(response.headers["Content-Range"], f"bytes {start}-{stop}/{len(original)}")
        with client.get("/media/1", headers={"Range": "bytes=-20"}) as response:
            self.assertEqual(response.status_code, 206)
            self.assertEqual(response.data, expected[-20:])
        with client.get("/media/1", headers={"If-None-Match": etag}) as response:
            self.assertEqual(response.status_code, 304)
        with client.get("/media/1", headers={"Range": "bytes=999999-"}) as response:
            self.assertEqual(response.status_code, 416)
        with client.head("/media/1") as response:
            self.assertEqual(response.data, b"")
            self.assertEqual(response.content_length, len(original))
        self.assertEqual(self.path.read_bytes(), original)

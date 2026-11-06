"""Expose chained Matroska cue indexes without rewriting library files."""
from zlib import crc32

EBML = 0x1A45DFA3
SEGMENT = 0x18538067
SEEK_HEAD = 0x114D9B74
CUES = 0x1C53BB6B
LIMIT = 64 * 1024


def _vint(data, offset, identifier=False):
    first = data[offset]
    width = 1
    while width <= 8 and not first & (0x80 >> (width - 1)):
        width += 1
    if width > (4 if identifier else 8) or offset + width > len(data):
        raise ValueError("Invalid EBML integer")
    value = int.from_bytes(data[offset:offset + width], "big")
    if not identifier:
        value &= (1 << (7 * width)) - 1
    return value, offset + width


def _element(data, offset):
    kind, offset = _vint(data, offset, True)
    size, start = _vint(data, offset)
    return kind, start, start + size


def _encode(kind, body):
    width = next(n for n in range(1, 9) if len(body) < (1 << (7 * n)) - 1)
    identifier = kind.to_bytes((kind.bit_length() + 7) // 8, "big")
    return identifier + ((1 << (7 * width)) | len(body)).to_bytes(width, "big") + body


def _children(data, start, end):
    if end > len(data):
        raise ValueError("Truncated EBML element")
    while start < end:
        kind, body, stop = _element(data, start)
        if stop > end:
            raise ValueError("Truncated EBML child")
        yield kind, body, stop
        start = stop


def _seeks(data, start, end):
    for kind, body, stop in _children(data, start, end):
        if kind != 0x4DBB:
            continue
        fields = {key: (a, b) for key, a, b in _children(data, body, stop)}
        if 0x53AB not in fields or 0x53AC not in fields:
            continue
        a, b = fields[0x53AB]
        c, d = fields[0x53AC]
        if b - a != 4 or not 1 <= d - c <= 8:
            continue
        yield int.from_bytes(data[a:b], "big"), int.from_bytes(data[c:d], "big")


def _checksum(data, start, end):
    offset = start
    for kind, body, stop in _children(data, start, end):
        if kind == 0xBF:
            if stop - body != 4:
                raise ValueError("Invalid EBML checksum")
            data[body:stop] = crc32(data[start:offset] + data[stop:end]).to_bytes(4, "little")
            return
        offset = stop


def seekable_header(path):
    """Return a same-length prefix with a direct Cues pointer, or None.

    Some muxers put only a link to a second SeekHead in the initial one.
    Firefox's nestegg does not follow that link to discover Cues. Replace
    adjacent reserved padding with a direct Cues pointer, retaining the link
    and all other entries. All media offsets and bytes stay intact.
    Reads and linked-header traversal are bounded, including malformed input.
    """
    if path.suffix.lower() not in (".mkv", ".webm"):
        return None
    try:
        with path.open("rb") as source:
            size = path.stat().st_size
            prefix = bytearray(source.read(LIMIT))
            kind, _, offset = _element(prefix, 0)
            if kind != EBML:
                return None
            kind, segment, _ = _element(prefix, offset)
            if kind != SEGMENT:
                return None
            offset = segment
            while offset < len(prefix):
                kind, start, end = _element(prefix, offset)
                if kind == SEEK_HEAD:
                    break
                if kind == 0xBF:
                    # A Segment-wide CRC cannot be updated without a full scan.
                    return None
                offset = end
            else:
                return None
            entries = list(_seeks(prefix, start, end))
            if any(kind == CUES for kind, _ in entries):
                return None
            link = next((entry for entry in entries if entry[0] == SEEK_HEAD), None)
            if not link:
                return None
            kind, _, padding_end = _element(prefix, end)
            if kind != 0xEC or padding_end > len(prefix):
                return None
            position = link[1]
            visited = set()
            for _ in range(4):
                if position in visited or not 0 <= position < size - segment:
                    return None
                visited.add(position)
                source.seek(segment + position)
                linked = source.read(LIMIT)
                kind, a, b = _element(linked, 0)
                if kind != SEEK_HEAD:
                    return None
                entries = list(_seeks(linked, a, b))
                cue = next((entry for entry in entries if entry[0] == CUES), None)
                if cue:
                    if not 0 <= cue[1] < size - segment:
                        return None
                    source.seek(segment + cue[1])
                    if source.read(4) != CUES.to_bytes(4, "big"):
                        return None
                    entry = _encode(0x4DBB, _encode(0x53AB, CUES.to_bytes(4, "big")) +
                                    _encode(0x53AC, cue[1].to_bytes(8, "big")))
                    head = bytearray(_encode(SEEK_HEAD, prefix[start:end] + entry))
                    _, body, stop = _element(head, 0)
                    _checksum(head, body, stop)
                    padding = padding_end - offset - len(head)
                    if padding < 9:
                        return None
                    void = b"\xec" + ((1 << 56) | (padding - 9)).to_bytes(8, "big") + bytes(padding - 9)
                    prefix[offset:padding_end] = head + void
                    return bytes(prefix)
                following = next((entry for entry in entries if entry[0] == SEEK_HEAD), None)
                if not following:
                    return None
                position = following[1]
    except (OSError, ValueError, IndexError, OverflowError):
        # Invalid/unsupported layouts retain ordinary native file delivery.
        return None
    return None


class HeaderOverlay:
    """Overlay a prefix on Werkzeug's already ranged file response."""
    def __init__(self, body, header, offset=0):
        self.body = body
        self.iterator = iter(body)
        self.header = header
        self.offset = offset

    def __iter__(self):
        return self

    def __next__(self):
        chunk = next(self.iterator)
        count = min(len(chunk), max(0, len(self.header) - self.offset))
        if count:
            chunk = self.header[self.offset:self.offset + count] + chunk[count:]
        self.offset += len(chunk)
        return chunk

    def close(self):
        self.body.close()

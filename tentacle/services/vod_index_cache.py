"""Each provider film's index (an MP4's `moov` box), kept on disk so a
player gets it from Tentacle instead of from the provider.

Why: a player cannot start an MP4 until it has read the whole `moov`, and
the provider's edge sends a film at 1.5-6 MB/s after 0.2-1 s to its first
byte (measured Oct 2026). Its films carry a 2.5-8 MB moov, either right
after the `ftyp` (read with the first request) or at the very end (ExoPlayer
reads the head, jumps to the end for the moov, then back to the media): 2-3
of the 2.6-6 s from OK to picture on the TV were that download.

What is kept: the bytes from 0 to the end of a moov at the head, or the moov
box at the end, as the relay passed them on to a player (or to Jellyfin's
probe, the first time the title is played). The file's size is kept with
them: a provider answer that names another size means the file changed, and
the entry is dropped. Entries are a few MB; the oldest go once the folder
passes MAX_TOTAL_BYTES, and none is used after KEEP_SECONDS.
"""
import hashlib
import json
import logging
import os
import struct
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# A moov larger than this is not kept (a 3-hour film's is ~10 MB).
MAX_INDEX_BYTES = 32 << 20
MAX_TOTAL_BYTES = 1 << 30
KEEP_SECONDS = 7 * 86400
# A head whose moov has not started within this many bytes has the media
# first (moov at the end): nothing to keep from it.
HEAD_SCAN_BYTES = 1 << 20
READ_CHUNK = 256 << 10
_MP4_EXTENSIONS = ("mp4", "m4v", "mov")


def _dir() -> Path:
    return Path(os.getenv("DATA_DIR", "/data")) / "cache" / "vod-index"


def _key(title: str) -> str:
    return hashlib.sha1(title.encode()).hexdigest()


def applies(title: str) -> bool:
    """Only MP4-family files have a moov to keep."""
    return title.rsplit(".", 1)[-1].lower() in _MP4_EXTENSIONS


class Entry:
    """Bytes [start, end) of a file of `size` bytes."""
    __slots__ = ("start", "end", "size", "content_type", "path")

    def __init__(self, start: int, end: int, size: int, content_type: str, path: Path):
        self.start, self.end, self.size, self.content_type, self.path = start, end, size, content_type, path

    def read(self, frm: int, to: int):
        """Bytes [frm, to) as chunks (blocking: run in a worker thread)."""
        out = []
        with open(self.path, "rb") as f:
            f.seek(frm - self.start)
            left = to - frm
            while left > 0:
                piece = f.read(min(READ_CHUNK, left))
                if not piece:
                    raise OSError("index file shorter than its entry")
                out.append(piece)
                left -= len(piece)
        return out


def lookup(title: str) -> Optional[Entry]:
    if not applies(title):
        return None
    base = _dir() / _key(title)
    try:
        meta = json.loads(base.with_suffix(".json").read_text())
        data = base.with_suffix(".bin")
        if time.time() - float(meta["at"]) > KEEP_SECONDS \
                or data.stat().st_size != int(meta["end"]) - int(meta["start"]):
            forget(title)
            return None
        return Entry(int(meta["start"]), int(meta["end"]), int(meta["size"]),
                     str(meta.get("content_type") or "video/mp4"), data)
    except (OSError, ValueError, KeyError, TypeError):
        return None


def forget(title: str) -> None:
    base = _dir() / _key(title)
    for suffix in (".json", ".bin"):
        try:
            base.with_suffix(suffix).unlink()
        except OSError:
            pass


def store(title: str, start: int, data: bytes, size: int, content_type: str) -> None:
    """Keep bytes [start, start+len(data)) of the title (blocking)."""
    folder = _dir()
    try:
        folder.mkdir(parents=True, exist_ok=True)
        base = folder / _key(title)
        tmp = base.with_suffix(".bin.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, base.with_suffix(".bin"))
        meta = {"start": start, "end": start + len(data), "size": size, "content_type": content_type,
                "at": time.time()}
        tmp = base.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(meta))
        os.replace(tmp, base.with_suffix(".json"))
        _prune(folder)
    except OSError as e:
        logger.warning(f"[VOD] could not keep a film's index: {type(e).__name__}")


def _prune(folder: Path) -> None:
    files = []
    for p in folder.glob("*.bin"):
        try:
            st = p.stat()
        except OSError:
            continue
        files.append((st.st_mtime, st.st_size, p))
    total = sum(s for _, s, _ in files)
    for _, s, p in sorted(files):
        if total <= MAX_TOTAL_BYTES:
            break
        for q in (p, p.with_suffix(".json")):
            try:
                q.unlink()
            except OSError:
                pass
        total -= s


def _box(buf: bytes, at: int):
    """(type, size) of the box whose header starts at `at`; None if the
    header is not all there yet, or (None, 0) if it is no box."""
    if len(buf) < at + 8:
        return None
    size, kind = struct.unpack(">I4s", buf[at:at + 8])
    if size == 1:
        if len(buf) < at + 16:
            return None
        size = struct.unpack(">Q", buf[at + 8:at + 16])[0]
    if size < 8 or not kind.isalnum() and kind not in (b"free", b"skip"):
        return None, 0
    return kind, size


class Tap:
    """Watches one response body go by, keeps the moov it carries.

    A body from byte 0 keeps everything up to the end of a moov that comes
    before the media; a body that starts exactly at a moov box (the player's
    jump to a moov at the end) keeps that box. Bytes must arrive in order
    (a resumed body continues at the byte the drop stopped at); any gap,
    and anything that is not such a body, ends the watching."""

    def __init__(self, title: str, start: int, size: Optional[int], content_type: str):
        self.title, self.start, self.size, self.content_type = title, start, size, content_type
        self.buf = bytearray()
        self.want = None            # end (exclusive, file offset) of the bytes to keep, once known
        self.done = not applies(title) or not size

    def feed(self, offset: int, chunk: bytes) -> Optional[bytes]:
        """The chunk at file `offset`. Returns the bytes to keep (from
        `self.start`) once they are complete, then never again."""
        if self.done:
            return None
        if offset != self.start + len(self.buf):
            self.done = True
            return None
        self.buf += chunk
        if self.want is None:
            self.want = self._find_end()
            if self.done or self.want is None:
                return None
        if self.start + len(self.buf) < self.want:
            return None
        self.done = True
        data = bytes(self.buf[:self.want - self.start])
        self.buf = bytearray()
        return data

    def _find_end(self) -> Optional[int]:
        buf = self.buf
        if self.start > 0:
            box = _box(buf, 0)
            if box is None:
                return None
            kind, size = box
            if kind != b"moov" or self.start + size > self.size or size > MAX_INDEX_BYTES:
                self.done = True
                return None
            return self.start + size
        at = 0
        while True:
            box = _box(buf, at)
            if box is None:
                if len(buf) > HEAD_SCAN_BYTES:
                    self.done = True
                return None
            kind, size = box
            if kind == b"moov":
                if at + size > MAX_INDEX_BYTES or at + size > self.size:
                    self.done = True
                    return None
                return at + size
            if kind in (None, b"mdat") or at + size > HEAD_SCAN_BYTES:
                self.done = True        # media first: the moov is at the end
                return None
            at += size

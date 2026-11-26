"""Lining a subtitle up with a film's sound.

A subtitle fetched from a site was made for some copy of the film: it can
start seconds early or late, or drift (a 25 fps copy against a 23.976 one is
4% off: five minutes by the end of a film). People talk where its lines are,
so the right timing is the one where its lines sit on the speech.

The speech pattern comes from the pass that reads the film's subtitles
anyway (services.subtitles: ffmpeg's sound, voice band only, as power 100
times a second), kept per title in /data/cache/speech, so a later "Get
another" needs no second read of a provider title. A frame is speech when
it is clearly louder than the quiet parts of the film.

The fit: every shift within MAX_SHIFT_SECONDS, at each of the usual speed
ratios, scored by how many of the subtitle's on-screen frames land on
speech (bitwise on Python ints: a two-hour film is 144,000 bits at 20 a
second, a few seconds for every shift and speed). It is applied only when
it clearly beats the timing as it came (`MIN_GAIN`).
"""
import math
import os
import re
import struct
from array import array
from pathlib import Path
from typing import Optional

RATE = 100                 # pattern frames per second
COARSE = 5                 # the first search works on every 5th frame (20 a second)
MAX_SHIFT_SECONDS = 90
# Frame rates a copy can be made at: the subtitle may be timed for another.
SPEEDS = (1.0, 25 / 23.976, 23.976 / 25, 24 / 23.976, 23.976 / 24, 25 / 24, 24 / 25)
# The fit must put this much more of the subtitle on speech than the timing it came with.
MIN_GAIN = 0.08
# …and stand out from every other shift tried by this many standard
# deviations: a subtitle that belongs to the film peaks sharply (12 measured
# on the test film), one that doesn't has only noise to offer (1.6 measured:
# without this it was "moved 87 s").
MIN_PEAK = 6.0


def speech_path(item_id: str) -> Path:
    return Path(os.getenv("DATA_DIR", "/data")) / "cache" / "speech" / f"{item_id}.bin"


def speech_from_power(power) -> bytes:
    """One byte per frame, 1 where someone (probably) speaks: louder than
    the quiet parts of the film by a good margin."""
    db = [10 * math.log10(max(p, 0.0) + 1e-10) for p in power]   # resampling rings below 0
    if not db:
        return b""
    ranked = sorted(db)
    quiet = ranked[int(len(ranked) * 0.2)]
    loud = ranked[int(len(ranked) * 0.95)]
    if loud - quiet < 6:
        return bytes(len(db))           # no contrast: nothing to go by
    threshold = quiet + 0.4 * (loud - quiet)
    return bytes(1 if d > threshold else 0 for d in db)


def save_speech(raw: Path, dest: Path) -> None:
    """The f32 power frames ffmpeg wrote (`raw`) as the speech pattern."""
    power = array("f")
    data = raw.read_bytes()
    power.frombytes(data[:len(data) - len(data) % 4])
    if struct.pack("=f", 1.0) != struct.pack("<f", 1.0):
        power.byteswap()
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp")
    tmp.write_bytes(speech_from_power(power))
    os.replace(tmp, dest)


_TIME = re.compile(r"(\d+):(\d{2}):(\d{2})[,.](\d{1,3})")


def _seconds(m) -> float:
    h, mi, s, ms = m.groups()
    return int(h) * 3600 + int(mi) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000


def parse_srt(text: str) -> list:
    """[(start, end, lines)] of an SRT."""
    out = []
    for block in re.split(r"\r?\n\s*\r?\n", text.replace("﻿", "").strip()):
        lines = block.strip().splitlines()
        for i, line in enumerate(lines):
            if "-->" in line:
                times = list(_TIME.finditer(line))
                if len(times) == 2:
                    out.append((_seconds(times[0]), _seconds(times[1]), "\n".join(lines[i + 1:])))
                break
    return out


def _stamp(t: float) -> str:
    ms = max(0, int(round(t * 1000)))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def write_srt(cues: list) -> str:
    return "".join(f"{i}\n{_stamp(a)} --> {_stamp(b)}\n{t}\n\n" for i, (a, b, t) in enumerate(cues, 1))


def _pack(seq: bytes) -> int:
    n = 0
    for i, b in enumerate(seq):
        if b:
            n |= 1 << i
    return n


def _cue_frames(cues: list, speed: float, rate: float, length: int) -> int:
    n = 0
    for a, b, _ in cues:
        s, e = int(a * speed * rate), int(b * speed * rate)
        s, e = max(0, s), min(length, e)
        if e > s:
            n |= ((1 << (e - s)) - 1) << s
    return n


def _score(speech: int, cues: int, shift: int) -> int:
    moved = cues << shift if shift >= 0 else cues >> -shift
    return (speech & moved).bit_count()


def fit(speech: bytes, cues: list) -> Optional[tuple]:
    """(speed, shift seconds, gain) that puts the cues on the speech, or
    None when nothing clearly beats the timing as it came."""
    if not cues or not speech or not any(speech):
        return None
    coarse = bytes(max(speech[i:i + COARSE]) for i in range(0, len(speech), COARSE))
    rate_c = RATE / COARSE
    speech_c = _pack(coarse)
    limit = int(MAX_SHIFT_SECONDS * rate_c)
    on_c = _cue_frames(cues, 1.0, rate_c, len(coarse) + limit).bit_count() or 1
    base = _score(speech_c, _cue_frames(cues, 1.0, rate_c, len(coarse) + limit), 0) / on_c
    best = (base, 1.0, 0)
    n, total_sc, total_sq = 0, 0.0, 0.0
    for speed in SPEEDS:
        frames = _cue_frames(cues, speed, rate_c, len(coarse) + limit)
        total = frames.bit_count() or 1
        for shift in range(-limit, limit + 1):
            sc = _score(speech_c, frames, shift) / total
            n, total_sc, total_sq = n + 1, total_sc + sc, total_sq + sc * sc
            if sc > best[0]:
                best = (sc, speed, shift)
    mean = total_sc / n
    spread = math.sqrt(max(total_sq / n - mean * mean, 1e-12))
    if (best[0] - mean) / spread < MIN_PEAK:
        return None
    # Refine to the full rate around the coarse answer.
    speech_f = _pack(speech)
    _, speed, shift_c = best
    frames = _cue_frames(cues, speed, RATE, len(speech) + MAX_SHIFT_SECONDS * RATE)
    total = frames.bit_count() or 1
    fine = max(((_score(speech_f, frames, s) / total, s) for s in range(shift_c * COARSE - COARSE, shift_c * COARSE + COARSE + 1)),
               default=(0, 0))
    full_base = _score(speech_f, _cue_frames(cues, 1.0, RATE, len(speech) + MAX_SHIFT_SECONDS * RATE), 0) / \
        (_cue_frames(cues, 1.0, RATE, len(speech) + MAX_SHIFT_SECONDS * RATE).bit_count() or 1)
    gain = fine[0] - full_base
    if gain < MIN_GAIN or (speed == 1.0 and fine[1] == 0):
        return None
    return speed, fine[1] / RATE, gain


def belongs(cues: list, speech: bytes) -> bool:
    """Whether a subtitle can be for this film at all: its lines end within
    the film (give or take a frame-rate change and a few minutes)."""
    if not cues or not speech:
        return True
    film = len(speech) / RATE
    return cues[-1][1] <= film * 1.06 + 300


def line_up(srt_text: str, speech: bytes) -> tuple:
    """(the SRT lined up with the speech, the shift in ms at the start,
    None when it was left as it came)."""
    cues = parse_srt(srt_text)
    found = fit(speech, cues)
    if found is None:
        return srt_text, None
    speed, shift, _ = found
    moved = [(a * speed + shift, b * speed + shift, t) for a, b, t in cues]
    moved = [(max(0.0, a), b, t) for a, b, t in moved if b > 0]
    return write_srt(moved), int(round(shift * 1000))

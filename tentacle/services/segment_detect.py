"""Where a show's intro and end credits are, from its episodes themselves.

Three ways, best first:
- **Chapter marks** in the file named like an intro or the credits.
- **The same sound in two episodes**: an intro (or end-credits music) is
  the same audio every week. Each episode's first and last minutes are made
  into an audio fingerprint (ffmpeg's chromaprint: one 32-bit point every
  ~0.124 s); two episodes' fingerprints are lined up by the shift most of
  their identical points agree on, then the longest stretch where the points
  stay alike (at most MAX_BIT_DIFF of 32 bits differ, gaps under MAX_GAP_S)
  is the shared part.
- **Credits on black**: from where the end of the episode is mostly black
  pictures (keyframes only, cheap), when the sound gave nothing.

This module only works on numbers (fingerprints, frame stats, chapter
names); services/segments.py reads the files and keeps the answers.
"""
import re
from collections import Counter
from typing import Optional

# chromaprint (ffmpeg's muxer, its default algorithm): 11025 Hz, a point
# every 4096/3 samples.
POINT_S = 4096 / 3 / 11025
MAX_BIT_DIFF = 6            # of 32: points this alike are "the same sound"
MAX_GAP_S = 3.5             # a stretch carries on across differences this short
MEAN_BIT_DIFF = 3.0         # …and averaged over SMOOTH points (~1 s) no more than this
SMOOTH = 9
EDGE_BIT_DIFF = 1           # where a shared stretch starts and ends: points this close
# A point hears ~1-2 s around it, so a shared stretch shows up early: its
# start moves this much later (measured 0.7-1.8 s early on the test season).
START_PAD_S = 1.5
INTRO_MIN_S, INTRO_MAX_S = 15.0, 120.0
CREDITS_MIN_S, CREDITS_MAX_S = 15.0, 450.0
# Shifts tried per pair: the best few by votes.
SHIFT_CANDIDATES = 6
# The share of a keyframe's pixels that must be dark for it to count as black (credits on black).
BLACK_SHARE = 85

_INTRO_CHAPTER = re.compile(r"^\s*(?:[\d.\s-]*)(?:intro(?:duction)?|opening(?:\s+(?:titles?|credits|theme))?|"
                            r"(?:main\s+)?titles?|op\d*|theme(?:\s+song)?)\s*$", re.I)
_CREDITS_CHAPTER = re.compile(r"^\s*(?:[\d.\s-]*)(?:(?:end(?:ing)?|closing|final)\s+(?:credits?|titles?)|credits?|"
                              r"ending|outro|ed\d*)\s*$", re.I)


def from_chapters(chapters: list, duration: float) -> dict:
    """{"intro": (start, end), "credits": (start, end)} from chapters
    [(start s, end s, title)] whose names say so."""
    out = {}
    for i, (start, end, title) in enumerate(chapters):
        if not title:
            continue
        end = end if end and end > start else (chapters[i + 1][0] if i + 1 < len(chapters) else duration)
        if "intro" not in out and _INTRO_CHAPTER.match(title) and 3 <= end - start <= INTRO_MAX_S * 1.5:
            out["intro"] = (start, end)
        elif "credits" not in out and _CREDITS_CHAPTER.match(title) and end - start >= 5:
            out["credits"] = (start, end)
    return out


def _shifts(a: list, b: list) -> list:
    """The shifts (b index - a index) most identical points agree on."""
    # Identical points vote, and so do points whose top half is identical
    # (the same sound encoded twice differs in a bit or two, often low ones).
    where, near = {}, {}
    for j, v in enumerate(b):
        where.setdefault(v, []).append(j)
        near.setdefault(v >> 16, []).append(j)
    votes = Counter()
    for i, v in enumerate(a):
        for j in where.get(v, ())[:8]:          # a value repeated everywhere (silence) says little
            votes[j - i] += 2
        for j in near.get(v >> 16, ())[:8]:
            votes[j - i] += 1
    # Neighbouring shifts are one alignment (a frame of drift).
    merged = Counter()
    for s, n in votes.items():
        merged[s] += n
        merged[s - 1] += n / 2
        merged[s + 1] += n / 2
    out = []
    for s, _ in merged.most_common():
        if all(abs(s - t) > 2 for t in out):
            out.append(s)
        if len(out) >= SHIFT_CANDIDATES:
            break
    return out


def _longest_alike(a: list, b: list, shift: int) -> Optional[tuple]:
    """(a start index, a end index) of the longest stretch where a[i] and
    b[i + shift] stay alike: their differing bits, averaged over about a
    second, at most MEAN_BIT_DIFF (the same sound re-encoded differs by 0-3;
    unrelated noise by ~6, often under MAX_BIT_DIFF point by point). Gaps
    under MAX_GAP_S are bridged."""
    gap_max = int(MAX_GAP_S / POINT_S)
    lo, hi = max(0, -shift), min(len(a), len(b) - shift)
    if hi - lo < SMOOTH:
        return None
    diffs = [bin(a[i] ^ b[i + shift]).count("1") for i in range(lo, hi)]
    half = SMOOTH // 2
    best, start, last = None, None, None
    running = sum(diffs[:SMOOTH])
    for k in range(len(diffs)):
        w0, w1 = max(0, k - half), min(len(diffs), k + half + 1)
        mean = sum(diffs[w0:w1]) / (w1 - w0) if (k < half or k + half >= len(diffs)) else running / SMOOTH
        if half <= k < len(diffs) - half - 1:
            running += diffs[k + half + 1] - diffs[k - half]
        if mean <= MEAN_BIT_DIFF and diffs[k] <= MAX_BIT_DIFF:
            i = lo + k
            if start is None or i - last > gap_max:
                if start is not None and (best is None or last - start > best[1] - best[0]):
                    best = (start, last)
                start = i
            last = i
    if start is not None and (best is None or last - start > best[1] - best[0]):
        best = (start, last)
    if best is None:
        return None
    # The averaging blurs the edges by up to half a window, and a point
    # hears a little either side: start where the points really agree, end
    # at the last one that does (a skip may start late, never early).
    s0, s1 = best
    tight = [i for i in range(s0, s1 + 1) if diffs[i - lo] <= EDGE_BIT_DIFF]
    if tight:
        s0, s1 = tight[0], tight[-1]
    return s0, s1


def shared(a: list, b: list, min_s: float, max_s: float) -> Optional[tuple]:
    """The longest sound two fingerprints share, between min_s and max_s
    long: ((a start s, a end s), (b start s, b end s)), or None."""
    if not a or not b:
        return None
    found = None
    for shift in _shifts(a, b):
        run = _longest_alike(a, b, shift)
        if run is None:
            continue
        length = (run[1] - run[0]) * POINT_S
        if length < min_s:
            continue
        if found is None or length > found[0]:
            found = (length, run, shift)
    if found is None:
        return None
    length, (i0, i1), shift = found
    if length > max_s:
        return None
    a0, b0 = i0 * POINT_S + START_PAD_S, (i0 + shift) * POINT_S + START_PAD_S
    return ((a0, (i1 + 1) * POINT_S), (b0, (i1 + 1 + shift) * POINT_S))


def intro_between(head_a: list, head_b: list) -> Optional[tuple]:
    """Episode A's intro (start, end) seconds, from what its opening shares with B's."""
    got = shared(head_a, head_b, INTRO_MIN_S, INTRO_MAX_S)
    if not got:
        return None
    start, end = got[0]
    return (0.0 if start < 3 else start, end)      # an intro right at the start starts at 0


def credits_between(tail_a: list, tail_a_starts: float, tail_b: list) -> Optional[tuple]:
    """Episode A's end credits (start, end) in its own time, from what its
    last minutes (fingerprinted from `tail_a_starts` s) share with B's."""
    got = shared(tail_a, tail_b, CREDITS_MIN_S, CREDITS_MAX_S)
    if not got:
        return None
    (s, e), _ = got
    return (tail_a_starts + s, tail_a_starts + e)


def credits_from_black(frames: list, duration: float) -> Optional[tuple]:
    """End credits on black: frames [(time s, dark share %)] of the episode's
    last minutes (keyframes). From the earliest keyframe after which nearly
    every keyframe is dark, to the end; None when the end isn't dark."""
    if len(frames) < 4:
        return None
    frames = sorted(frames)
    start = None
    dark = total = 0
    # Walk back from the end while the stretch stays mostly dark.
    for t, share in reversed(frames):
        total += 1
        dark += share >= BLACK_SHARE
        if dark / total >= 0.85:
            start = t if share >= BLACK_SHARE else start
        elif total > 6:
            break
    if start is None or duration - start < CREDITS_MIN_S:
        return None
    return (start, duration)


def credits_end(credits: tuple, duration: float) -> tuple:
    """Credits that stop within a few seconds of the end run to the end."""
    s, e = credits
    return (s, duration if duration - e < 5 else e)

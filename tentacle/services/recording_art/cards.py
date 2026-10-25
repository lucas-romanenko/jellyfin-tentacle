"""The pictures Tentacle draws for recordings (Pillow, offline, like the
playlist art in services/artwork.py), each in two shapes: 1280x720 for the
Thumb (wide cards) and 800x1200 for the Primary (poster cards), JPEG:

- a matchup card: the league, both teams (code and name), the date and the
  channel;
- a league card: a league's event (a race, "NHL Hockey" with no teams
  named), or a title folder of several games;
- a title card: the title, the channel and the date. Always works.

No team crests and no outside service: the teams' codes stand in for them.
`to_16x9` and `to_2x3` make any other picture (a TMDB backdrop or poster, a
guide picture, a frame) the same shapes; `poster_from_wide` puts a wide
picture that has no poster (a frame, the guide's) on a poster with the title.

Safe area: clients draw a progress bar along the bottom of a card (the TV
app's Continue Watching), so nothing Tentacle draws goes into the bottom
SAFE_BOTTOM of a poster or WIDE_SAFE_BOTTOM of a 16:9 picture: the footer
(date, channel) ends above it and the rest is laid out above the footer.
"""
import colorsys
import hashlib
import io
import logging
from datetime import datetime, timezone
from typing import Optional

from services.artwork import _get_font, _wrap_text

logger = logging.getLogger(__name__)

W, H = 1280, 720
PW, PH = 800, 1200      # the poster (2:3)
QUALITY = 88
SAFE_BOTTOM = 0.12      # share of a poster's height kept clear at the bottom (progress bars)
WIDE_SAFE_BOTTOM = 0.08  # the same for the 16:9 picture
SAFE_Y = round(PH * (1 - SAFE_BOTTOM))          # 1056: nothing drawn below it
WIDE_SAFE_Y = round(H * (1 - WIDE_SAFE_BOTTOM))  # 662

# Background per sport (top, bottom): dark, so white text always reads
_SPORT = {
    "hockey": ((18, 52, 92), (6, 16, 30)),
    "basketball": ((110, 50, 14), (24, 10, 4)),
    "soccer": ((16, 82, 46), (4, 22, 12)),
    "american football": ((92, 40, 18), (22, 10, 4)),
    "canadian football": ((96, 26, 30), (24, 6, 8)),
    "baseball": ((24, 40, 96), (6, 10, 28)),
    "motor racing": ((110, 18, 18), (26, 4, 4)),
}
_DEFAULT_BG = ((44, 44, 56), (14, 14, 18))
_TEXT = (255, 255, 255)
_TEXT2 = (205, 210, 220)


def _hue_color(seed: str, s: float = 0.55, v: float = 0.62) -> tuple:
    """A steady colour for a name (a team's badge, a title card)."""
    h = int(hashlib.md5((seed or "").encode("utf-8")).hexdigest()[:6], 16) / 0xFFFFFF
    r, g, b = colorsys.hsv_to_rgb(h, s, v)
    return int(r * 255), int(g * 255), int(b * 255)


def _canvas(top: tuple, bottom: tuple, size: tuple = (W, H)):
    from PIL import Image, ImageDraw
    w, h = size
    img = Image.new("RGB", (w, h), bottom)
    draw = ImageDraw.Draw(img)
    for y in range(h):
        t = y / (h - 1)
        draw.line([(0, y), (w, y)], fill=tuple(int(top[i] * (1 - t) + bottom[i] * t) for i in range(3)))
    return img, draw


def _fit(draw, text: str, max_width: int, max_lines: int, size: int, min_size: int, bold: bool = True):
    """(font, lines): the biggest size at which `text` fits in `max_lines`
    lines of `max_width`; at the smallest, the last line is cut with "…"."""
    words = (text or "").split() or [""]
    while True:
        font = _get_font(size, bold=bold)
        lines = _wrap_text(draw, words, font, max_width)
        wide = any(_width(draw, ln, font) > max_width for ln in lines)
        if (len(lines) <= max_lines and not wide) or size <= min_size:
            break
        size = max(min_size, size - 4)
    if len(lines) > max_lines or any(_width(draw, ln, font) > max_width for ln in lines):
        lines = lines[:max_lines]
        last = lines[-1]
        while last and _width(draw, last + "…", font) > max_width:
            last = last[:-1].rstrip()
        lines[-1] = last + "…"
    return font, lines


def _width(draw, text: str, font) -> int:
    box = draw.textbbox((0, 0), text, font=font)
    return box[2] - box[0]


def _line_height(font) -> int:
    try:
        ascent, descent = font.getmetrics()
        return ascent + descent
    except Exception:
        return getattr(font, "size", 12)


def _center(draw, lines: list, font, cx: int, top: int, color=_TEXT, gap: int = 8) -> int:
    """Draws lines centred on cx from `top`, one line height apart; returns the bottom."""
    y = top
    for ln in lines:
        draw.text((cx - _width(draw, ln, font) // 2, y), ln, font=font, fill=color)
        y += _line_height(font) + gap
    return y - gap


def _block_height(draw, lines: list, font, gap: int = 8) -> int:
    return _line_height(font) * len(lines) + gap * max(0, len(lines) - 1)


def when_text(start: Optional[datetime]) -> str:
    """ "Fri, Oct 23, 2026 · 8:00 PM" in the server's time zone (start is UTC)."""
    if start is None:
        return ""
    local = start.replace(tzinfo=timezone.utc).astimezone()
    hour = local.hour % 12 or 12
    return f"{local:%a}, {local:%b} {local.day}, {local.year} · {hour}:{local:%M} {'AM' if local.hour < 12 else 'PM'}"


def footer_box() -> tuple:
    """(rule, top, bottom) of the 16:9 footer: its line, and where its text
    starts and ends, always above WIDE_SAFE_Y."""
    bottom = WIDE_SAFE_Y - 12
    top = bottom - max(_line_height(_get_font(32)), _line_height(_get_font(32, bold=True)))
    return top - 22, top, bottom


def _footer(draw, start: Optional[datetime], channel: Optional[str]) -> None:
    """The date on the left and the channel on the right, along the bottom."""
    font = _get_font(32)
    _rule, y, _bottom = footer_box()
    draw.line([(64, y - 22), (W - 64, y - 22)], fill=(255, 255, 255, 60), width=1)
    left = when_text(start)
    if left:
        draw.text((64, y), left, font=font, fill=_TEXT2)
    if channel:
        room = W - 128 - (_width(draw, left, font) + 48 if left else 0)
        _f, lines = _fit(draw, channel, max(200, room), 1, 32, 22, bold=True)
        draw.text((W - 64 - _width(draw, lines[0], _f), y), lines[0], font=_f, fill=_TEXT)


def _jpeg(img) -> bytes:
    out = io.BytesIO()
    img.convert("RGB").save(out, "JPEG", quality=QUALITY, optimize=True)
    return out.getvalue()


def _sport_bg(league: Optional[dict]) -> tuple:
    return _SPORT.get(((league or {}).get("sport") or "").lower(), _DEFAULT_BG)


def _code(team: dict) -> str:
    code = (team.get("abbr") or "").strip()
    if code:
        return code.upper()[:4]
    words = [w for w in (team.get("short") or team.get("name") or "?").split() if w[:1].isalnum()]
    return "".join(w[0] for w in words[:3]).upper() or "?"


def _badge(img, draw, team: dict, cx: int, cy: int, size: int = 210, code_font=None) -> None:
    color = _hue_color(team.get("id", "") + "|" + (team.get("name") or ""))
    x0, y0 = cx - size // 2, cy - size // 2
    shade = tuple(max(0, c - 40) for c in color)
    draw.rounded_rectangle([x0 + 6, y0 + 8, x0 + size + 6, y0 + size + 8], radius=size // 5, fill=(0, 0, 0))
    draw.rounded_rectangle([x0, y0, x0 + size, y0 + size], radius=size // 5, fill=color, outline=shade, width=4)
    code = _code(team)
    font = code_font or _fit(draw, code, size - 36, 1, 96, 40)[0]
    box = draw.textbbox((0, 0), code, font=font)
    draw.text((cx - (box[2] - box[0]) // 2 - box[0], cy - (box[3] - box[1]) // 2 - box[1]), code,
              font=font, fill=_TEXT)


def _league_head(draw, league: dict) -> None:
    name = (league.get("name") or "").upper()
    font, lines = _fit(draw, name, W - 128, 1, 44, 26)
    draw.text((64, 52), lines[0], font=font, fill=_TEXT)
    sport = league.get("sport") or ""
    if sport and sport.lower() not in name.lower():
        draw.text((64, 52 + _block_height(draw, lines, font) + 14), sport, font=_get_font(26), fill=_TEXT2)


def matchup_card(league: dict, teams: list, at: bool, start: Optional[datetime],
                 channel: Optional[str]) -> bytes:
    """Both teams' codes and names (or the one team a listing named), the
    league, the date and the channel."""
    img, draw = _canvas(*_sport_bg(league))
    _league_head(draw, league)
    cy = 300
    if len(teams) >= 2:
        # one size for both codes and both names: the longer decides
        codes = [_code(t) for t in teams[:2]]
        code_font = min((_fit(draw, c, 174, 1, 96, 40)[0] for c in codes), key=lambda f: f.size)
        name_font = min((_fit(draw, t.get("name") or "", 470, 2, 46, 28)[0] for t in teams[:2]),
                        key=lambda f: f.size)
        for team, cx in ((teams[0], 330), (teams[1], W - 330)):
            _badge(img, draw, team, cx, cy, code_font=code_font)
            _f, lines = _fit(draw, team.get("name") or "", 470, 2, name_font.size, name_font.size)
            _center(draw, lines, name_font, cx, cy + 136)
        mid = "@" if at else "VS"
        font = _get_font(64, bold=True)
        box = draw.textbbox((0, 0), mid, font=font)
        draw.text((W // 2 - (box[2] - box[0]) // 2 - box[0], cy - (box[3] - box[1]) // 2 - box[1]), mid,
                  font=font, fill=_TEXT2)
    elif teams:
        _badge(img, draw, teams[0], W // 2, cy - 10, size=230)
        font, lines = _fit(draw, teams[0].get("name") or "", W - 300, 2, 56, 30)
        _center(draw, lines, font, W // 2, cy + 140)
    _footer(draw, start, channel)
    return _jpeg(img)


def league_card(league: dict, title: str, start: Optional[datetime], channel: Optional[str]) -> bytes:
    """The league's name big, what it is under it (a race, "NHL Hockey")."""
    img, draw = _canvas(*_sport_bg(league))
    big, blines = _fit(draw, league.get("name") or "", W - 200, 2, 120, 56)
    sub_font, slines = _fit(draw, title or "", W - 200, 2, 54, 30, bold=False) if title and \
        title.strip().lower() != (league.get("name") or "").strip().lower() else (None, [])
    height = _block_height(draw, blines, big) + (36 + _block_height(draw, slines, sub_font) if slines else 0)
    top = (footer_box()[0] - height) // 2
    y = _center(draw, blines, big, W // 2, top)
    if slines:
        _center(draw, slines, sub_font, W // 2, y + 28, color=_TEXT2)
    _footer(draw, start, channel)
    return _jpeg(img)


def title_card(title: str, start: Optional[datetime], channel: Optional[str]) -> bytes:
    """The title, the channel and the date: for anything nothing else knows."""
    base = _hue_color(title or "", s=0.45, v=0.42)
    img, draw = _canvas(base, tuple(int(c * 0.25) for c in base))
    font, lines = _fit(draw, title or "Recording", W - 200, 3, 104, 44)
    top = (footer_box()[0] - _block_height(draw, lines, font, gap=14)) // 2
    _center(draw, lines, font, W // 2, top, gap=14)
    _footer(draw, start, channel)
    return _jpeg(img)


def to_16x9(data: bytes, only_if_wide: bool = False) -> Optional[bytes]:
    """Any picture as 1280x720 JPEG: cut to 16:9 around its centre. With
    `only_if_wide`, None unless it is about 16:9 already (a guide's poster
    stays out)."""
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as e:
        logger.debug(f"[Recording art] not a picture: {e}")
        return None
    w, h = img.size
    if w < 320 or h < 180:
        return None
    ratio = w / h
    if only_if_wide and abs(ratio - 16 / 9) > 0.12:
        return None
    if ratio > 16 / 9:
        nw = int(h * 16 / 9)
        img = img.crop(((w - nw) // 2, 0, (w - nw) // 2 + nw, h))
    elif ratio < 16 / 9:
        nh = int(w * 9 / 16)
        img = img.crop((0, (h - nh) // 2, w, (h - nh) // 2 + nh))
    img = img.convert("RGB").resize((W, H), Image.LANCZOS)
    return _jpeg(img)


# ── Posters (2:3) ───────────────────────────────────────────────────────────

def short_when(start: Optional[datetime]) -> str:
    """ "Fri, Oct 23 · 8:00 PM" in the server's time zone (start is UTC): a poster is narrow."""
    if start is None:
        return ""
    local = start.replace(tzinfo=timezone.utc).astimezone()
    hour = local.hour % 12 or 12
    return f"{local:%a}, {local:%b} {local.day} · {hour}:{local:%M} {'AM' if local.hour < 12 else 'PM'}"


_WHEN_SIZE, _CHANNEL_SIZE, _FOOT_GAP = 34, 38, 16


def poster_footer_box() -> dict:
    """Where the poster's footer goes: its line (`rule`), the date's and the
    channel's lines (top, bottom), the channel ending 16 px above SAFE_Y.
    The same for every poster, with or without a channel; everything else
    is laid out above `rule`."""
    bottom = SAFE_Y - 16
    channel = (bottom - _line_height(_get_font(_CHANNEL_SIZE, bold=True)), bottom)
    when_top = channel[0] - _FOOT_GAP - _line_height(_get_font(_WHEN_SIZE))
    when = (when_top, channel[0] - _FOOT_GAP)
    return {"rule": when_top - 24, "when": when, "channel": channel}


def _poster_footer(draw, start: Optional[datetime], channel: Optional[str]) -> None:
    """The date over the channel, centred along the bottom, above the safe area."""
    box = poster_footer_box()
    draw.line([(56, box["rule"]), (PW - 56, box["rule"])], fill=(255, 255, 255, 60), width=1)
    when = short_when(start)
    if when:
        _f, lines = _fit(draw, when, PW - 112, 1, _WHEN_SIZE, 24, bold=False)
        _center(draw, lines, _f, PW // 2, box["when"][0], color=_TEXT2)
    if channel:
        _f, lines = _fit(draw, channel, PW - 112, 1, _CHANNEL_SIZE, 24)
        _center(draw, lines, _f, PW // 2, box["channel"][0])


def _poster_league_head(draw, league: dict) -> int:
    """The league's name at the top, the sport under it; returns the bottom."""
    name = (league.get("name") or "").upper()
    font, lines = _fit(draw, name, PW - 112, 2, 56, 30)
    y = _center(draw, lines, font, PW // 2, 64)
    sport = league.get("sport") or ""
    if sport and sport.lower() not in name.lower():
        _f = _get_font(30)
        y = _center(draw, [sport], _f, PW // 2, y + 16, color=_TEXT2)
    return y


def matchup_poster_layout(head: int, foot: int, name_heights: list, mid_height: int,
                          badge: int = 200, min_badge: int = 140, name_gap: int = 20, min_gap: int = 20) -> dict:
    """Two teams one over the other between the league's head (its bottom)
    and the footer's line, the "@" between them, with equal gaps: the
    badges' centres, the names' tops, the "@"'s centre and the badge size.
    The badges only get smaller (to `min_badge`) when two-line names leave
    no room."""
    content = 2 * name_gap + sum(name_heights) + mid_height
    room = foot - head
    badge = max(min_badge, min(badge, (room - content - 4 * min_gap) // 2))
    gap = max(0, (room - content - 2 * badge) // 4)
    y = head + gap
    badges, names = [], []
    for i, nh in enumerate(name_heights):
        badges.append(y + badge // 2)
        names.append(y + badge + name_gap)
        y += badge + name_gap + nh + gap
        if i == 0:
            mid = y + mid_height // 2
            y += mid_height + gap
    return {"badge": badge, "badges": badges, "names": names, "mid": mid,
            "bottom": names[-1] + name_heights[-1]}


def matchup_poster(league: dict, teams: list, at: bool, start: Optional[datetime],
                   channel: Optional[str]) -> bytes:
    """The matchup card as a poster: the league on top, the away team over
    the home team (or the one team named), the date and the channel."""
    img, draw = _canvas(*_sport_bg(league), size=(PW, PH))
    head = _poster_league_head(draw, league)
    foot = poster_footer_box()["rule"]
    if len(teams) >= 2:
        codes = [_code(t) for t in teams[:2]]
        code_font = min((_fit(draw, c, 170, 1, 92, 40)[0] for c in codes), key=lambda f: f.size)
        name_font = min((_fit(draw, t.get("name") or "", PW - 140, 2, 50, 30)[0] for t in teams[:2]),
                        key=lambda f: f.size)
        names = [_fit(draw, t.get("name") or "", PW - 140, 2, name_font.size, name_font.size)[1] for t in teams[:2]]
        mid = "@" if at else "VS"
        font = _get_font(60, bold=True)
        box = draw.textbbox((0, 0), mid, font=font)
        lay = matchup_poster_layout(head, foot, [_block_height(draw, n, name_font) for n in names],
                                    box[3] - box[1])
        for team, lines, cy, top in zip(teams, names, lay["badges"], lay["names"]):
            _badge(img, draw, team, PW // 2, cy, size=lay["badge"], code_font=code_font)
            _center(draw, lines, name_font, PW // 2, top)
        draw.text((PW // 2 - (box[2] - box[0]) // 2 - box[0], lay["mid"] - (box[3] - box[1]) // 2 - box[1]), mid,
                  font=font, fill=_TEXT2)
    elif teams:
        font, lines = _fit(draw, teams[0].get("name") or "", PW - 140, 3, 64, 32)
        size = 260
        block = size + 40 + _block_height(draw, lines, font)
        top = head + max(24, (foot - head - block) // 2)
        _badge(img, draw, teams[0], PW // 2, top + size // 2, size=size)
        _center(draw, lines, font, PW // 2, top + size + 40)
    _poster_footer(draw, start, channel)
    return _jpeg(img)


def league_poster(league: dict, title: str, start: Optional[datetime], channel: Optional[str]) -> bytes:
    """The league card as a poster."""
    img, draw = _canvas(*_sport_bg(league), size=(PW, PH))
    big, blines = _fit(draw, league.get("name") or "", PW - 120, 3, 130, 56)
    sub_font, slines = _fit(draw, title or "", PW - 120, 3, 56, 30, bold=False) if title and \
        title.strip().lower() != (league.get("name") or "").strip().lower() else (None, [])
    height = _block_height(draw, blines, big) + (40 + _block_height(draw, slines, sub_font) if slines else 0)
    top = (poster_footer_box()["rule"] - height) // 2
    y = _center(draw, blines, big, PW // 2, top)
    if slines:
        _center(draw, slines, sub_font, PW // 2, y + 32, color=_TEXT2)
    _poster_footer(draw, start, channel)
    return _jpeg(img)


def title_poster(title: str, start: Optional[datetime], channel: Optional[str]) -> bytes:
    """The title card as a poster."""
    base = _hue_color(title or "", s=0.45, v=0.42)
    img, draw = _canvas(base, tuple(int(c * 0.25) for c in base), size=(PW, PH))
    font, lines = _fit(draw, title or "Recording", PW - 120, 5, 110, 44)
    top = (poster_footer_box()["rule"] - _block_height(draw, lines, font, gap=14)) // 2
    _center(draw, lines, font, PW // 2, top, gap=14)
    _poster_footer(draw, start, channel)
    return _jpeg(img)


def _open(data: bytes, min_size: tuple):
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as e:
        logger.debug(f"[Recording art] not a picture: {e}")
        return None
    if img.size[0] < min_size[0] or img.size[1] < min_size[1]:
        return None
    return img.convert("RGB")


def _cover(img, w: int, h: int):
    """`img` cut around its centre to w:h and scaled to w x h."""
    from PIL import Image
    iw, ih = img.size
    if iw / ih > w / h:
        nw = int(ih * w / h)
        img = img.crop(((iw - nw) // 2, 0, (iw - nw) // 2 + nw, ih))
    elif iw / ih < w / h:
        nh = int(iw * h / w)
        img = img.crop((0, (ih - nh) // 2, iw, (ih - nh) // 2 + nh))
    return img.resize((w, h), Image.LANCZOS)


def to_2x3(data: bytes) -> Optional[bytes]:
    """A poster (TMDB's) as 800x1200 JPEG, cut to 2:3 around its centre.
    None unless it is about portrait already: a wide picture goes through
    `poster_from_wide` instead."""
    img = _open(data, (200, 300))
    if img is None or img.size[0] / img.size[1] > 0.8:
        return None
    return _jpeg(_cover(img, PW, PH))


def poster_from_wide(data: bytes, title: str, start: Optional[datetime], channel: Optional[str]) -> Optional[bytes]:
    """A wide picture (a frame, the guide's, a TMDB backdrop) on a poster:
    the picture whole across the middle, over a dark blurred copy of
    itself, the title above it and the date and channel below."""
    from PIL import Image, ImageFilter
    img = _open(data, (320, 180))
    if img is None:
        return None
    bg = _cover(img, PW // 8, PH // 8).filter(ImageFilter.GaussianBlur(3)).resize((PW, PH), Image.BILINEAR)
    bg = Image.blend(bg, Image.new("RGB", (PW, PH), (0, 0, 0)), 0.55)
    from PIL import ImageDraw
    draw = ImageDraw.Draw(bg)
    pw, ph = PW, int(PW * 9 / 16)
    top = min(470, poster_footer_box()["rule"] - 24 - ph)    # the picture ends above the footer
    bg.paste(_cover(img, pw, ph), (0, top))
    font, lines = _fit(draw, title or "Recording", PW - 112, 4, 76, 36)
    height = _block_height(draw, lines, font, gap=10)
    _center(draw, lines, font, PW // 2, max(48, (top - height) // 2), gap=10)
    _poster_footer(draw, start, channel)
    return _jpeg(bg)

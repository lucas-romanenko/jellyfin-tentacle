"""
Tentacle - Playlist Artwork Generator
Generates poster images for Jellyfin playlists.

Default: bold white text on dark gradient background.
If Logo.dev API key is configured and playlist is a streaming source playlist,
fetches the streaming service logo instead of text.
"""

import hashlib
import logging
import os
import io
from typing import Optional

logger = logging.getLogger(__name__)

ARTWORK_DIR = "/tmp/tentacle_artwork"

# Source tag (lowercase) → brand styling + Logo.dev domain
SOURCE_BRANDS = {
    "netflix":        {"bg1": "#E50914", "bg2": "#8B0000", "text": "#FFFFFF", "domain": "netflix.com"},
    "amazon prime":   {"bg1": "#00A8E1", "bg2": "#005F8A", "text": "#FFFFFF", "domain": "primevideo.com"},
    "apple tv+":      {"bg1": "#1C1C1E", "bg2": "#000000", "text": "#FFFFFF", "domain": "tv.apple.com?theme=dark"},
    "disney+":        {"bg1": "#113CCF", "bg2": "#0A2080", "text": "#FFFFFF", "domain": "disneyplus.com"},
    "hbo":            {"bg1": "#6B2D8B", "bg2": "#2A0A40", "text": "#FFFFFF", "domain": "hbo.com"},
    "paramount+":     {"bg1": "#0064FF", "bg2": "#0030AA", "text": "#FFFFFF", "domain": "paramountplus.com"},
    "peacock":        {"bg1": "#2D2D2D", "bg2": "#000000", "text": "#FFFFFF", "domain": "peacocktv.com"},
    "hulu":           {"bg1": "#1CE783", "bg2": "#0A8A4A", "text": "#000000", "domain": "hulu.com"},
    "showtime":       {"bg1": "#CC0000", "bg2": "#6B0000", "text": "#FFFFFF", "domain": "showtime.com"},
    "discovery+":     {"bg1": "#2175D9", "bg2": "#0A3A80", "text": "#FFFFFF", "domain": "discoveryplus.com"},
    "marvel":         {"bg1": "#ED1D24", "bg2": "#7B0F13", "text": "#FFFFFF", "domain": "marvel.com"},
    "pixar":          {"bg1": "#0A5C36", "bg2": "#032B1A", "text": "#FFFFFF", "domain": "pixar.com"},
    "dreamworks":     {"bg1": "#1A3A6B", "bg2": "#0A1A3A", "text": "#FFFFFF", "domain": "dreamworks.com"},
    "imdb":           {"bg1": "#F5C518", "bg2": "#CC9E00", "text": "#000000", "domain": "imdb.com"},
    "imdb top 250":   {"bg1": "#F5C518", "bg2": "#CC9E00", "text": "#000000", "domain": "imdb.com"},
    "trakt":          {"bg1": "#ED1C24", "bg2": "#8B0000", "text": "#FFFFFF", "domain": "trakt.tv"},
    "letterboxd":     {"bg1": "#00E054", "bg2": "#006628", "text": "#FFFFFF", "domain": "letterboxd.com"},
}

DEFAULT_STYLE = {"bg1": "#2D2D2D", "bg2": "#1A1A1A", "text": "#FFFFFF"}


def _hex_to_rgb(hex_color: str) -> tuple:
    hex_color = hex_color.lstrip("#")
    return tuple(int(hex_color[i:i+2], 16) for i in (0, 2, 4))


def _get_font(size: int, bold: bool = False):
    from PIL import ImageFont
    bold_paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/ubuntu/Ubuntu-B.ttf",
    ]
    regular_paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ]
    for path in (bold_paths if bold else regular_paths):
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _fetch_logo(domain: str, token: str):
    """Fetch square logo from logo.dev. Returns PIL Image or None."""
    import requests
    try:
        from PIL import Image
        if "?" in domain:
            base, extra = domain.split("?", 1)
            url = f"https://img.logo.dev/{base}?token={token}&size=200&format=png&{extra}"
        else:
            url = f"https://img.logo.dev/{domain}?token={token}&size=200&format=png"
        r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        img = Image.open(io.BytesIO(r.content)).convert("RGBA")
        return img
    except Exception as e:
        logger.debug(f"Could not fetch logo for {domain}: {e}")
        return None


def _get_source_tag_from_rule(rule) -> Optional[str]:
    """Extract source_tag value from a TagRule's conditions if it has a source condition."""
    for cond in (rule.conditions or []):
        if cond.get("field") == "source" and cond.get("operator") == "equals":
            return cond.get("value", "")
    return None


def _detect_source_from_name(name: str) -> Optional[str]:
    """Try to match a playlist name against known source brands.
    E.g. 'Amazon Prime Movies' → 'amazon prime', 'IMDB TOP 250' → 'imdb top 250'."""
    name_lower = name.lower()
    # Sort by length descending so longer keys match first
    for brand_key in sorted(SOURCE_BRANDS.keys(), key=len, reverse=True):
        if name_lower.startswith(brand_key) or name_lower == brand_key:
            return brand_key
    return None


def generate_playlist_poster(
    name: str,
    source_tag: Optional[str] = None,
    logodev_token: Optional[str] = None,
) -> Optional[str]:
    """Generate a 200x300 poster for a playlist.

    - If source_tag matches a known brand AND logodev_token is set, uses brand
      colors and fetches the streaming service logo.
    - Otherwise, renders bold white text on a dark gradient.

    Returns path to PNG file, or None on error.
    """
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        logger.error("Pillow not installed.")
        return None

    try:
        os.makedirs(ARTWORK_DIR, exist_ok=True)

        # Cache key: hash of inputs to skip regeneration if nothing changed
        cache_key = hashlib.md5(f"{name}|{source_tag or ''}|{bool(logodev_token)}".encode()).hexdigest()[:10]
        safe_name = name.replace("/", "-").replace("\\", "-").replace(":", "-")
        output_path = os.path.join(ARTWORK_DIR, f"{safe_name}_{cache_key}.png")

        if os.path.exists(output_path):
            return output_path

        brand = None
        if source_tag:
            brand = SOURCE_BRANDS.get(source_tag.lower())

        if brand:
            bg1 = _hex_to_rgb(brand["bg1"])
            bg2 = _hex_to_rgb(brand["bg2"])
            text_color = _hex_to_rgb(brand["text"])
        else:
            bg1 = _hex_to_rgb(DEFAULT_STYLE["bg1"])
            bg2 = _hex_to_rgb(DEFAULT_STYLE["bg2"])
            text_color = _hex_to_rgb(DEFAULT_STYLE["text"])

        W, H = 200, 300

        # Gradient background
        img = Image.new("RGB", (W, H), bg1)
        draw = ImageDraw.Draw(img)
        for y in range(H):
            t = y / H
            r = int(bg1[0] * (1 - t) + bg2[0] * t)
            g = int(bg1[1] * (1 - t) + bg2[1] * t)
            b = int(bg1[2] * (1 - t) + bg2[2] * t)
            draw.line([(0, y), (W, y)], fill=(r, g, b))

        LOGO_SIZE = 60
        LOGO_Y = 40

        has_logo = False
        if brand and brand.get("domain") and logodev_token:
            logo_img = _fetch_logo(brand["domain"], logodev_token)
            if logo_img:
                logo_img = logo_img.resize((LOGO_SIZE, LOGO_SIZE), Image.LANCZOS)
                lx = (W - LOGO_SIZE) // 2
                img_rgba = img.convert("RGBA")
                img_rgba.paste(logo_img, (lx, LOGO_Y), logo_img)
                img = img_rgba.convert("RGB")
                draw = ImageDraw.Draw(img)
                has_logo = True

        # Text — playlist name, auto-fit to card width
        display_text = name.upper()
        max_width = W - 40  # 20px padding each side

        # Word-wrap the text, shrinking font until it fits both width and height
        words = display_text.split()
        font_size = 36
        line_spacing = 6
        while font_size > 8:
            font = _get_font(font_size, bold=True)
            lines = _wrap_text(draw, words, font, max_width)
            total_height = len(lines) * font_size + (len(lines) - 1) * line_spacing

            # Check all lines fit within max_width
            all_fit_width = all(
                (draw.textbbox((0, 0), line, font=font)[2] - draw.textbbox((0, 0), line, font=font)[0]) <= max_width
                for line in lines
            )

            if all_fit_width:
                if has_logo:
                    if 120 + total_height < H - 15:
                        break
                else:
                    if total_height < H - 40:
                        break
            font_size -= 2
        else:
            font = _get_font(font_size, bold=True)
            lines = _wrap_text(draw, words, font, max_width)
            total_height = len(lines) * font_size + (len(lines) - 1) * line_spacing

        # Vertically center text in available area
        if has_logo:
            available_top = 120
            available_bottom = H - 15
        else:
            available_top = 0
            available_bottom = H
        text_y = available_top + (available_bottom - available_top - total_height) // 2

        for line in lines:
            bbox = draw.textbbox((0, 0), line, font=font)
            lw = bbox[2] - bbox[0]
            draw.text(((W - lw) // 2, text_y), line, font=font, fill=text_color)
            text_y += font_size + line_spacing

        img.save(output_path, "PNG")
        return output_path

    except Exception as e:
        logger.error(f"Failed to generate artwork for '{name}': {e}")
        return None


def _wrap_text(draw, words: list, font, max_width: int) -> list:
    """Word-wrap text to fit within max_width."""
    lines = []
    current = ""
    for word in words:
        test = f"{current} {word}".strip()
        bbox = draw.textbbox((0, 0), test, font=font)
        if bbox[2] - bbox[0] <= max_width:
            current = test
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines or [""]

"""
16x16 Chapter Icons & Cover Upload Engine for StoryDeck Core.
Renders 16x16 pixel art PNG icons (shrunk book cover + crisp corner chapter numbers,
big numbers, progress bar, random pixel creatures, or custom chapter art from CUSTOM_ICONS_DIR),
and uploads covers and offline 16x16 display icons to Yoto Cloud CDN.
"""
from __future__ import annotations

import hashlib
import io
import logging
import random
import struct
import zlib
from typing import Any, Dict, List, Optional, Tuple

import requests
import clients
from core.config import CUSTOM_ICONS_DIR

logger = logging.getLogger("storydeck.bridge.icons")
SIZE = 16

DIGITS = {
    "0": ["###", "#.#", "#.#", "#.#", "###"],
    "1": [".#.", "##.", ".#.", ".#.", "###"],
    "2": ["###", "..#", "###", "#..", "###"],
    "3": ["###", "..#", ".##", "..#", "###"],
    "4": ["#.#", "#.#", "###", "..#", "..#"],
    "5": ["###", "#..", "###", "..#", "###"],
    "6": ["###", "#..", "###", "#.#", "###"],
    "7": ["###", "..#", ".#.", ".#.", ".#."],
    "8": ["###", "#.#", "###", "#.#", "###"],
    "9": ["###", "#.#", "###", "..#", "###"],
}

TINY = {
    "0": ["###", "#.#", "#.#", "###"],
    "1": [".#.", "##.", ".#.", ".#."],
    "2": ["##.", "..#", ".#.", "###"],
    "3": ["###", ".##", "..#", "###"],
    "4": ["#.#", "#.#", "###", "..#"],
    "5": ["###", "##.", "..#", "##."],
    "6": ["#..", "###", "#.#", "###"],
    "7": ["###", "..#", ".#.", ".#."],
    "8": ["###", "###", "#.#", "###"],
    "9": ["###", "#.#", "###", "..#"],
}

FALLBACK_BG = (60, 70, 90)
INK = 70

STYLES = {
    "cover-tr": "Cover, number top right",
    "cover-tl": "Cover, number top left",
    "cover-bl": "Cover, number bottom left",
    "cover-br": "Cover, number bottom right",
    "cover-big": "Big number on a dimmed cover",
    "cover": "Cover only",
    "number": "Number only",
    "progress": "Cover with a progress bar",
    "random": "Random pixel creatures",
    "custom": "Custom picture",
    "none": "No icon (Yoto's own)",
}
DEFAULT_STYLE = "cover-tr"
CORNERS = {"cover-tr": (1, 0), "cover-tl": (0, 0), "cover-bl": (0, 1), "cover-br": (1, 1)}

_COVER16_CACHE: Dict[str, Optional[bytes]] = {}
_UPLOADED_ICON_CACHE: Dict[str, str] = {}


def png(rgba: bytes, w: int = SIZE, h: int = SIZE) -> bytes:
    """Encodes raw 16x16 RGBA bytes into a valid PNG image."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + rgba[y * w * 4:(y + 1) * w * 4] for y in range(h))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def shrink_cover_bytes(image_bytes: bytes) -> Optional[bytes]:
    """Converts cover image bytes into 16x16 raw RGBA bytes center-cropped."""
    if not image_bytes:
        return None
    try:
        from PIL import Image
        with Image.open(io.BytesIO(image_bytes)) as im:
            im = im.convert("RGBA")
            w, h = im.size
            side = min(w, h)
            left = (w - side) // 2
            top = (h - side) // 2
            cropped = im.crop((left, top, left + side, top + side))
            resample = getattr(getattr(Image, "Resampling", Image), "BOX", Image.NEAREST)
            resized = cropped.resize((SIZE, SIZE), resample=resample)
            raw = resized.tobytes("raw", "RGBA")
            if len(raw) == SIZE * SIZE * 4:
                return raw
    except Exception as e:
        logger.debug("PIL shrink_cover_bytes notice: %s", e)
    return None


def to_jpeg_bytes(image_bytes: bytes) -> Optional[bytes]:
    """Ensures cover image bytes are in JPEG or PNG format accepted by Yoto Cloud."""
    if not image_bytes:
        return None
    if image_bytes[:2] == b"\xff\xd8" or image_bytes[:8] == b"\x89PNG\r\n\x1a\n":
        return image_bytes
    try:
        from PIL import Image
        with Image.open(io.BytesIO(image_bytes)) as im:
            rgb = im.convert("RGB")
            buf = io.BytesIO()
            rgb.save(buf, format="JPEG", quality=88)
            return buf.getvalue()
    except Exception as e:
        logger.debug("to_jpeg_bytes conversion notice: %s", e)
    return None


def fetch_abs_cover_bytes(abs_item_id: str, token: Optional[str] = None) -> Optional[bytes]:
    """Fetches raw cover image bytes for an Audiobookshelf item."""
    if not abs_item_id or not clients.abs_client or not clients.abs_client.base_url:
        return None
    try:
        url = f"{clients.abs_client.base_url.rstrip('/')}/api/items/{abs_item_id}/cover"
        res = requests.get(url, headers=clients.abs_client.get_headers(token), timeout=10)
        if res.status_code == 200 and res.content:
            return res.content
    except Exception as e:
        logger.debug("fetch_abs_cover_bytes notice for %s: %s", abs_item_id, e)
    return None


def get_cover16(abs_item_id: str, token: Optional[str] = None) -> Optional[bytes]:
    """Returns cached 16x16 raw RGBA bytes for an ABS book cover."""
    if not abs_item_id:
        return None
    if abs_item_id in _COVER16_CACHE:
        return _COVER16_CACHE[abs_item_id]
    raw_cover = fetch_abs_cover_bytes(abs_item_id, token=token)
    rgba = shrink_cover_bytes(raw_cover) if raw_cover else None
    if rgba:
        _COVER16_CACHE[abs_item_id] = rgba
    return rgba


def upload_abs_cover_to_yoto(abs_item_id: str, fallback_url: str = "", token: Optional[str] = None) -> str:
    """
    Fetches an ABS item's cover image, converts to JPEG if needed, and uploads it to
    Yoto Cloud CDN (/media/coverImage/user/me/upload) so Yoto cards always display cover art
    even when StoryDeck Core runs on a private LAN IP. Falls back to fallback_url.
    """
    if not abs_item_id or not clients.yoto_client or not hasattr(clients.yoto_client, "upload_cover"):
        return fallback_url
    try:
        raw = fetch_abs_cover_bytes(abs_item_id, token=token)
        if raw:
            jpg = to_jpeg_bytes(raw)
            if jpg:
                cdn_url = clients.yoto_client.upload_cover(jpg)
                if cdn_url:
                    return cdn_url
    except Exception as e:
        logger.debug("upload_abs_cover_to_yoto fallback for %s: %s", abs_item_id, e)
    return fallback_url


def _base(cover16: Optional[bytes]) -> bytearray:
    return bytearray(cover16) if (cover16 and len(cover16) == SIZE * SIZE * 4) else bytearray(bytes([*FALLBACK_BG, 255]) * SIZE * SIZE)


def _luma(rgb) -> float:
    r, g, b = rgb[:3]
    return 0.299 * r + 0.587 * g + 0.114 * b


def average(cover16: Optional[bytes]) -> Tuple[int, int, int]:
    if not cover16 or len(cover16) != SIZE * SIZE * 4:
        return FALLBACK_BG
    n = len(cover16) // 4
    return tuple(sum(cover16[i + k] for i in range(0, len(cover16), 4)) // n for k in range(3))  # type: ignore


def render_corner(cover16: Optional[bytes], number: Optional[int], corner: str = "cover-tr") -> bytes:
    """Renders 16x16 cover with a tiny 3x4 chapter number tinted into the corner."""
    px = _base(cover16)
    if number is None:
        return png(bytes(px))
    text = str(max(0, int(number)))[-3:]
    width = len(text) * 4 - 1
    right, bottom = CORNERS.get(corner, (1, 0))
    x0 = (SIZE - width) if right else 0
    y0 = (SIZE - 4) if bottom else 0
    area = [px[(y * SIZE + x) * 4:(y * SIZE + x) * 4 + 3] for y in range(y0, y0 + 4) for x in range(x0, x0 + width)]
    light = (sum(_luma(p) for p in area) / max(1, len(area))) > 140
    ink = (40, 40, 44) if light else (235, 235, 235)
    for n, ch in enumerate(text):
        for cy, row in enumerate(TINY.get(ch, TINY["0"])):
            for cx, c in enumerate(row):
                if c == "#":
                    i = ((y0 + cy) * SIZE + x0 + n * 4 + cx) * 4
                    px[i:i + 3] = bytes(p + (k - p) * INK // 100 for p, k in zip(px[i:i + 3], ink))
                    px[i + 3] = 255
    return png(bytes(px))


def _big_digits(px: bytearray, number: int, ink: Tuple[int, int, int]) -> None:
    text = str(max(0, int(number)))[-3:]
    scale = 2 if len(text) <= 2 else 1
    width = (len(text) * 4 - 1) * scale
    x0 = (SIZE - width) // 2
    y0 = (SIZE - 5 * scale) // 2
    for n, ch in enumerate(text):
        for cy, row in enumerate(DIGITS.get(ch, DIGITS["0"])):
            for cx, c in enumerate(row):
                if c != "#":
                    continue
                for dy in range(scale):
                    for dx in range(scale):
                        i = ((y0 + cy * scale + dy) * SIZE + x0 + (n * 4 + cx) * scale + dx) * 4
                        px[i:i + 4] = bytes([*ink, 255])


def render_big(cover16: Optional[bytes], number: Optional[int]) -> bytes:
    px = _base(cover16)
    for i in range(0, len(px), 4):
        px[i:i + 3] = bytes(v * 40 // 100 for v in px[i:i + 3])
    if number is not None:
        _big_digits(px, number, (240, 240, 240))
    return png(bytes(px))


def render_number(cover16: Optional[bytes], number: Optional[int]) -> bytes:
    r, g, b = average(cover16)
    top = max(r, g, b, 1)
    bg = tuple(min(255, v * 150 // top) for v in (r, g, b))
    px = bytearray(bytes([*bg, 255]) * SIZE * SIZE)
    if number is not None:
        _big_digits(px, number, (40, 40, 44) if _luma(bg) > 140 else (245, 245, 245))
    return png(bytes(px))


def render_progress(cover16: Optional[bytes], fraction: Optional[float]) -> bytes:
    px = _base(cover16)
    if fraction is None:
        return png(bytes(px))
    filled = max(1, min(SIZE, round(float(fraction) * SIZE)))
    for x in range(SIZE):
        i = ((SIZE - 1) * SIZE + x) * 4
        px[i:i + 4] = bytes([250, 250, 250, 255] if x < filled else [30, 30, 34, 255])
    return png(bytes(px))


def _vivid(cover16: Optional[bytes], rnd: random.Random) -> Tuple[int, int, int]:
    best = None
    score = 60
    if cover16 and len(cover16) == SIZE * SIZE * 4:
        for i in range(0, len(cover16), 4):
            r, g, b = cover16[i:i + 3]
            s = max(r, g, b) - min(r, g, b) + max(r, g, b) // 4
            if s > score:
                best, score = (r, g, b), s
    if best is None:
        best = rnd.choice([(250, 90, 90), (90, 200, 120), (90, 150, 250), (250, 200, 60), (200, 110, 240)])
    top = max(best) or 1
    return tuple(min(255, v * 240 // top) for v in best)  # type: ignore


def render_random(cover16: Optional[bytes], seed: str) -> bytes:
    rnd = random.Random(seed)
    body = _vivid(cover16, rnd)
    shade = tuple(v * 60 // 100 for v in body)
    bg = tuple(v * 25 // 100 for v in average(cover16))
    grid = [[0] * 8 for _ in range(8)]
    for y in range(1, 7):
        for x in range(1, 4):
            grid[y][x] = 1 if rnd.random() < (0.65 if x == 3 else 0.5) else 0
        grid[y][3] = int(bool(grid[y][3] or (2 <= y <= 5)))
    for y in range(1, 7):
        for x in range(1, 4):
            if grid[y][x] and rnd.random() < 0.2:
                grid[y][x] = 2
    ey = rnd.choice([2, 3])
    grid[ey][2] = 3
    for y in range(8):
        for x in range(4):
            grid[y][7 - x] = grid[y][x]
    colours = {0: bg, 1: body, 2: shade, 3: (20, 20, 24)}
    px = bytearray(SIZE * SIZE * 4)
    for y in range(SIZE):
        for x in range(SIZE):
            i = (y * SIZE + x) * 4
            px[i:i + 4] = bytes([*colours[grid[y // 2][x // 2]], 255])
    return png(bytes(px))


def render_style(
    style: str,
    cover16: Optional[bytes],
    number: Optional[int],
    fraction: Optional[float] = None,
    seed: str = "",
) -> bytes:
    if style == "cover":
        return render_corner(cover16, None)
    if style == "cover-big":
        return render_big(cover16, number)
    if style == "number":
        return render_number(cover16, number)
    if style == "progress":
        return render_progress(cover16, fraction)
    if style == "random":
        return render_random(cover16, seed)
    return render_corner(cover16, number, style if style in CORNERS else DEFAULT_STYLE)


def resolve_source_chapter_idx(
    chapters: Optional[List[Dict[str, Any]]],
    start_time: float,
    end_time: Optional[float] = None,
) -> int:
    """
    Maps a playlist track's [start_time, end_time) interval back to the 0-based
    index in the Audiobookshelf item's raw `chapters` list so split chapters,
    unchaptered split books, and multi-book series tracks resolve custom artwork
    saved at `<abs_item_id>_<chapter_idx>.png`.
    """
    if not chapters or len(chapters) <= 1:
        return 0

    st = float(start_time or 0.0)
    if end_time is not None and float(end_time) > st:
        probe = st + min(1.0, max(0.0, (float(end_time) - st) * 0.1))
    else:
        probe = st + 0.5

    for idx, ch in enumerate(chapters):
        ch_start = float(ch.get("start") or 0.0)
        ch_end = float(ch.get("end") or 0.0)
        if ch_end <= ch_start and idx + 1 < len(chapters):
            ch_end = float(chapters[idx + 1].get("start") or ch_start)
        if ch_end > ch_start and ch_start <= probe < ch_end:
            return idx

    best_idx = 0
    for idx, ch in enumerate(chapters):
        if float(ch.get("start") or 0.0) <= st + 0.5:
            best_idx = idx
    return best_idx


def render_track_icon_png(
    abs_item_id: str,
    track_idx: int = 1,
    style: str = DEFAULT_STYLE,
    fraction: Optional[float] = None,
    token: Optional[str] = None,
    chapter_idx: Optional[int] = None,
) -> bytes:
    """
    Resolves a 16x16 PNG icon for (abs_item_id, track_idx):
    1. Checks CUSTOM_ICONS_DIR for a curated custom chapter icon (`<abs_item_id>_<0-indexed>.png`),
       using `chapter_idx` when provided or `track_idx - 1` otherwise.
    2. Otherwise renders the book's 16x16 cover with the chapter/track number overlay in `style`.
    """
    if abs_item_id:
        icon_idx = max(0, int(chapter_idx)) if chapter_idx is not None else max(0, int(track_idx) - 1)
        local_icon = CUSTOM_ICONS_DIR / f"{abs_item_id}_{icon_idx}.png"
        if local_icon.is_file():
            try:
                return local_icon.read_bytes()
            except Exception as e:
                logger.debug("Custom icon read fallback for %s: %s", local_icon, e)

    cover16 = get_cover16(abs_item_id, token=token) if abs_item_id else None
    return render_style(style or DEFAULT_STYLE, cover16, int(track_idx), fraction=fraction, seed=f"{abs_item_id}:{track_idx}")


def get_or_upload_yoto_icon(
    abs_item_id: str,
    track_idx: int = 1,
    style: str = DEFAULT_STYLE,
    token: Optional[str] = None,
    chapter_idx: Optional[int] = None,
) -> Optional[str]:
    """
    Renders the 16x16 PNG for (abs_item_id, track_idx / chapter_idx), uploads it once to Yoto Cloud
    (`POST /media/displayIcons/user/me/upload`), and returns `'yoto:#<mediaId>'` for offline tracks.
    """
    if not abs_item_id or not clients.yoto_client or not hasattr(clients.yoto_client, "upload_display_icon"):
        return None
    png_bytes = render_track_icon_png(
        abs_item_id,
        track_idx=track_idx,
        style=style,
        token=token,
        chapter_idx=chapter_idx,
    )
    digest = hashlib.sha256(png_bytes).hexdigest()[:16]
    cache_key = f"{abs_item_id}:{digest}"
    if cache_key in _UPLOADED_ICON_CACHE:
        return _UPLOADED_ICON_CACHE[cache_key]
    try:
        ref = clients.yoto_client.upload_display_icon(png_bytes, filename=f"abs-{abs_item_id[:8]}-{track_idx}.png")
        if ref:
            _UPLOADED_ICON_CACHE[cache_key] = ref
            return ref
    except Exception as e:
        logger.debug("get_or_upload_yoto_icon notice for %s/%d: %s", abs_item_id, track_idx, e)
    return None

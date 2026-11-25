"""
Chapters as Yoto tracks & Offline Segment Planners.
Segments Audiobookshelf books/series into Yoto playlist tracks (max 99/100 tracks),
with 99-track sliding window (step 90), 5-hour rolling window ('next') planning,
and binary-search whole-book/series ('book' / 'series') packing.
"""
from __future__ import annotations
import math
from dataclasses import dataclass
from typing import List, Tuple, Dict, Any, Optional

from .quiet import Snap, cut

MAX_TRACKS = 99
MAX_OFFLINE_NEXT_TRACKS = 100
WHOLE_TRACKS = 99
DEFAULT_PART_SECONDS = 1800  # 30 minutes
DEFAULT_OFFLINE_TRACK_SECONDS = 3300  # 55 minutes for 'next' mode
YOTO_MAX_TRACK = 7000  # seconds (~116 min max per Yoto transcode limit)
WINDOW_STEP = MAX_TRACKS - 9  # 90-track step with 9-track overlap


@dataclass
class Segment:
    item_id: str
    book: str              # book title
    start: float           # start time within book in seconds
    end: float             # end time within book in seconds
    number: int            # chapter (or part) number within the book, 1-based
    title: str
    book_duration: float
    chaptered: bool = True # False: a fixed-length part of a book without chapters


def chapter_spans(chapters: List[Dict[str, Any]], duration: float, part_seconds: int = DEFAULT_PART_SECONDS,
                  snap: Optional[Snap] = None) -> List[Tuple[float, float, str]]:
    """
    (start, end, title) per track: each chapter, with chapters longer than two parts split evenly
    into parts of at most part_seconds. Books with fewer than 2 chapters get fixed-length parts.
    """
    out = []
    for start, end, title in _raw_spans(chapters, duration, part_seconds, snap):
        n = 1 if end - start <= 2 * part_seconds else -(-(end - start) // part_seconds)
        n = int(n)
        step = (end - start) / n
        s = start
        for i in range(n):
            e = end if i == n - 1 else cut(snap, start + (i + 1) * step, s, end)
            part_title = f"{title} ({i + 1}/{n})" if n > 1 else title
            out.append((s, e, part_title))
            s = e
    return out


def _raw_spans(chapters: List[Dict[str, Any]], duration: float, part_seconds: int,
               snap: Optional[Snap] = None) -> List[Tuple[float, float, str]]:
    if len(chapters) >= 2:
        spans = []
        for i, c in enumerate(chapters):
            start = float(c.get("start", 0))
            nxt = float(chapters[i + 1].get("start", duration)) if i + 1 < len(chapters) else duration
            end = float(c["end"]) if c.get("end") else nxt
            if end - start > 0.5:
                spans.append((start, end, c.get("title") or f"Chapter {i + 1}"))
        if spans:
            return _fill_gaps(spans, duration, part_seconds, snap)

    out = []
    t = 0.0
    n = 1
    while t < duration - 0.5:
        e = min(duration, n * part_seconds)
        e = e if e >= duration else cut(snap, e, t, duration)
        out.append((t, e, f"Part {n}"))
        t = e
        n += 1
    return out or [(0.0, max(0.1, duration), "Part 1")]


def _fill_gaps(spans: List[Tuple[float, float, str]], duration: float,
               part_seconds: int, snap: Optional[Snap] = None) -> List[Tuple[float, float, str]]:
    """Cover audio that no chapter covers with parts of part_seconds."""
    out = []
    t = 0.0
    n = 1
    for start, end, title in spans + [(duration, duration, "")]:
        gap = t
        k = 1
        while start - t > 0.5:
            e = min(start, gap + k * part_seconds)
            e = e if e >= start else cut(snap, e, t, start)
            out.append((t, e, f"Part {n}"))
            t = e
            n += 1
            k += 1
        if title:
            out.append((start, end, title))
        t = max(t, end)
    return out


def index_at(spans: List[Tuple[float, float, str]], pos: float) -> int:
    """Returns 0-based index of the span containing pos."""
    for i, (s, e, _) in enumerate(spans):
        if s <= pos < e:
            return i
    return len(spans) - 1 if spans and pos >= spans[-1][0] else 0


def window_start(index: int, total: int) -> int:
    """
    First track of the 99-track sliding window holding chapter `index` (0-based)
    of a book with `total` tracks. Moves in steps of 90 with a 9-track overlap.
    """
    if total <= MAX_TRACKS:
        return 0
    return max(0, min((index // WINDOW_STEP) * WINDOW_STEP, total - MAX_TRACKS))


def make_layout(
    books: List[Tuple[str, str, float, List[Dict[str, Any]], float]],
    part_seconds: int = DEFAULT_PART_SECONDS,
    snaps: Optional[Dict[str, Optional[Snap]]] = None,
) -> List[Segment]:
    """
    Builds up to MAX_TRACKS (99) Segment items across one or more chained books:
    books = [(item_id, title, duration, chapters, current_pos), ...].
    """
    segs: List[Segment] = []
    for n, (item_id, title, duration, chapters, pos) in enumerate(books):
        spans = chapter_spans(chapters, duration, part_seconds, (snaps or {}).get(item_id))
        first = 0 if (n > 0 or len(spans) <= MAX_TRACKS) else window_start(index_at(spans, pos), len(spans))
        chaptered = len(chapters) >= 2
        for i in range(first, len(spans)):
            s, e, t = spans[i]
            number = sum(1 for c in chapters if float(c.get("start", 0)) <= s + 0.5) if chaptered else (i + 1)
            segs.append(Segment(item_id, title, s, e, max(1, number), t, duration, chaptered))
            if len(segs) >= MAX_TRACKS:
                return segs
    return segs


def plan_segments(
    start: float,
    end: float,
    chapters: List[Dict[str, Any]],
    max_len: float = DEFAULT_OFFLINE_TRACK_SECONDS,
    snap: Optional[Snap] = None,
) -> List[Tuple[float, float, str]]:
    """
    Plans a 5-hour rolling window ('next' mode) over book time [start, end):
    cuts at chapter boundaries, splits long chapters into pieces <= max_len (snapped to pauses),
    and merges runs of very short chapters (< max_len / 4) so tiny chapters fit inside 100 tracks.
    """
    pieces: List[Tuple[float, float, str]] = []
    chs = [
        c for c in (chapters or [])
        if float(c.get("end", 0)) > start and float(c.get("start", 0)) < end
    ] or [{"start": start, "end": end, "title": ""}]

    for c in chs:
        s = max(start, float(c.get("start", 0)))
        e = min(end, float(c.get("end", end)))
        if e - s < 0.5:
            continue
        n = max(1, math.ceil((e - s) / max(60.0, max_len)))
        step = (e - s) / n
        title = c.get("title") or ""
        a = s
        for i in range(n):
            part = f"{title} ({i + 1}/{n})" if (n > 1 and title) else title
            b = e if i == n - 1 else cut(snap, s + (i + 1) * step, a, e)
            pieces.append((a, b, part))
            a = b

    if pieces and pieces[0][0] > start + 0.5:
        pieces.insert(0, (start, pieces[0][0], ""))

    short = max_len / 4.0
    out: List[Tuple[float, float, str]] = []
    for s, e, title in pieces:
        if out and (e - s) < short and (out[-1][1] - out[-1][0]) < short and (e - out[-1][0]) <= max_len:
            ps, _, pt = out[-1]
            out[-1] = (ps, e, pt or title)
        else:
            out.append((s, e, title))
    return out[:MAX_OFFLINE_NEXT_TRACKS]


def chapter_units(
    chapters: List[Dict[str, Any]],
    duration: float,
    max_len: float = YOTO_MAX_TRACK,
    part_seconds: float = DEFAULT_PART_SECONDS,
    snap: Optional[Snap] = None,
) -> List[Tuple[float, float, str, int]]:
    """Breaks a whole book into contiguous (start, end, title, chapter_num) pieces <= max_len."""
    clean_chs = sorted(
        (c for c in (chapters or []) if 0 <= float(c.get("start", 0)) < duration),
        key=lambda c: float(c.get("start", 0)),
    )
    spans = chapter_spans(clean_chs, duration, int(part_seconds), snap)
    out: List[Tuple[float, float, str, int]] = []
    for i, (s, e, title) in enumerate(spans):
        s = 0.0 if i == 0 else s
        e = duration if i == len(spans) - 1 else spans[i + 1][0]
        if e - s < 0.5:
            continue
        n = max(1, math.ceil((e - s) / max(60.0, max_len) - 1e-9))
        step = (e - s) / n
        a = s
        for k in range(n):
            part = f"{title} ({k + 1}/{n})" if (n > 1 and title) else title
            b = e if k == n - 1 else cut(snap, s + (k + 1) * step, a, e)
            out.append((a, b, part, i + 1))
            a = b
    return out


def _pack(units: List[Tuple[float, float, str, int]], target: float, max_len: float) -> List[Tuple[float, float, str]]:
    """Packs neighboring chapter units into tracks of at most min(target, max_len)."""
    out: List[List[Any]] = []
    for s, e, title, _n in units:
        if out and (e - out[-1][0]) <= min(target, max_len):
            out[-1][1] = e
            out[-1][3] = title
        else:
            out.append([s, e, title, title])
    return [
        (s, e, first if (first == last or not (first and last)) else f"{first} – {last}")
        for s, e, first, last in out
    ]


def min_tracks(
    chapters: List[Dict[str, Any]],
    duration: float,
    max_len: float = YOTO_MAX_TRACK,
    part_seconds: float = DEFAULT_PART_SECONDS,
    snap: Optional[Snap] = None,
) -> int:
    """Minimum number of tracks <= max_len required to hold a whole book."""
    return len(_pack(chapter_units(chapters, duration, max_len, part_seconds, snap), max_len, max_len))


def plan_whole(
    books: List[Tuple[List[Dict[str, Any]], float]],
    max_len: float = YOTO_MAX_TRACK,
    limit: int = WHOLE_TRACKS,
    part_seconds: float = DEFAULT_PART_SECONDS,
    snaps: Optional[List[Optional[Snap]]] = None,
) -> List[List[Tuple[float, float, str]]]:
    """
    Plans whole books into at most `limit` (99) total tracks via binary search on target track length.
    Tracks never span two books.
    """
    snaps_list = snaps or [None] * len(books)
    units = [
        chapter_units(ch, dur, max_len, part_seconds, sn)
        for (ch, dur), sn in zip(books, snaps_list)
    ]

    def _plan(target: float):
        return [_pack(u, target, max_len) for u in units]

    def _count(target: float) -> int:
        return sum(len(p) for p in _plan(target))

    if _count(0.0) <= limit:
        return _plan(0.0)
    lo = 0.0
    hi = min(max_len, max((dur for _, dur in books), default=0.0))
    if _count(hi) > limit:
        return _plan(hi)
    for _ in range(40):
        mid = (lo + hi) / 2.0
        if _count(mid) <= limit:
            hi = mid
        else:
            lo = mid
    return _plan(hi)

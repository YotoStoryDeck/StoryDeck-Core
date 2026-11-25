"""
Old MYO Card -> Audiobookshelf Viterbi Position Transfer Engine for StoryDeck Core.
Matches an existing hand-uploaded Yoto MYO playlist's track durations against
Audiobookshelf book file and chapter boundaries using Viterbi dynamic programming,
correlates the Yoto Player's live MQTT track/position on that card,
and transfers the listener's exact place into Audiobookshelf.
"""
from __future__ import annotations

import bisect
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

import clients
import database
from .mqtt_client import get_mqtt_bridge

logger = logging.getLogger("storydeck.bridge.transfer")

STOP_WORDS = {
    "the", "and", "with", "from", "chapter", "track", "part", "disc", "book", "books",
    "audiobook", "unabridged", "abridged", "series", "volume", "intro", "outro", "credits",
    "opening", "closing", "mp3", "m4a", "m4b", "edition", "card", "yoto", "myo", "read", "audio",
}
KEEP_CANDIDATES = 80


def tol(d: float) -> float:
    """Allowed duration tolerance between a Yoto track and an ABS span."""
    return 2.5 + 0.002 * float(d or 0.0)


def extract_card_tracks(card: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Extracts a Yoto playlist's tracks in play order: n (1-based), chapter/track keys, title, duration."""
    out: List[Dict[str, Any]] = []
    inner = card.get("card") if isinstance(card.get("card"), dict) else card
    for ch in ((inner.get("content") or {}).get("chapters") or []):
        tracks = ch.get("tracks") or [{}]
        for t in tracks:
            d = t.get("duration") if t.get("duration") is not None else (ch.get("duration") if len(tracks) == 1 else None)
            out.append({
                "n": len(out) + 1,
                "chapter_key": str(ch.get("key") or ""),
                "track_key": str(t.get("key") or ""),
                "title": str(t.get("title") or ch.get("title") or f"Track {len(out) + 1}"),
                "duration": float(d) if d else 0.0,
                "type": str(t.get("type") or ""),
            })
    return out


def words(text: str) -> List[str]:
    out = []
    for w in re.findall(r"[a-z][a-z']+", (text or "").lower().replace("_", " ")):
        w = w.strip("'")
        if len(w) < 4 or w in STOP_WORDS:
            continue
        if w.endswith("s") and len(w) > 5:
            w = w[:-1]
        out.append(w)
    return out


@dataclass
class TransferBook:
    item_id: str
    title: str
    duration: float
    bounds: List[float]
    chapters: List[Dict[str, Any]]
    series_name: str = ""
    word_set: Set[str] = field(default_factory=set)

    @classmethod
    def from_abs_item(cls, item: Dict[str, Any]) -> "TransferBook":
        media = item.get("media") or {}
        md = media.get("metadata") or {}
        dur = float(media.get("duration") or 0.0)
        b = {0.0, dur}
        c_off = 0.0
        for t in (media.get("tracks") or media.get("audioFiles") or []):
            t_start = float(t["startOffset"]) if t.get("startOffset") is not None else c_off
            t_dur = float(t.get("duration") or 0.0)
            b.add(t_start)
            b.add(t_start + t_dur)
            c_off = t_start + t_dur
        chapters = media.get("chapters") or []
        for c in chapters:
            b.add(float(c.get("start") or 0.0))
            b.add(float(c.get("end") or 0.0))
        title = str(md.get("title") or item.get("name") or item.get("id") or "")
        ser = str(md.get("seriesName") or "")
        return cls(
            item_id=str(item.get("id") or ""),
            title=title,
            duration=dur,
            bounds=sorted(x for x in b if 0.0 <= x <= dur),
            chapters=chapters,
            series_name=ser,
            word_set=set(words(f"{title} {ser}")),
        )

    def has_bound(self, x: float, t: float) -> bool:
        i = bisect.bisect_left(self.bounds, x - t)
        return i < len(self.bounds) and self.bounds[i] <= x + t

    def chapter_at(self, pos: float) -> str:
        for c in self.chapters:
            if float(c.get("start") or 0.0) <= pos < float(c.get("end") or 0.0):
                return str(c.get("title") or "")
        return ""


@dataclass(frozen=True)
class Place:
    book: int
    start: float
    exact: bool


UNKNOWN = Place(-1, 0.0, False)


def match_tracks_to_books(tracks: List[Dict[str, Any]], books: List[TransferBook]) -> List[Place]:
    """Viterbi pass finding the best (book_idx, start_sec) alignment for every track on a MYO card."""
    n = len(tracks)
    if not n:
        return []
    layers: List[Dict[Place, Tuple[float, Optional[Place]]]] = []
    for i, t in enumerate(tracks):
        d = float(t.get("duration") or 0.0)
        tw = set(words(t.get("title") or ""))
        cands: Set[Place] = {UNKNOWN}
        if d > 0:
            for bi, b in enumerate(books):
                for s in b.bounds:
                    if s < b.duration - 1.0 and b.has_bound(s + d, tol(d)):
                        cands.add(Place(bi, s, True))
        if i > 0:
            pd = float(tracks[i - 1].get("duration") or 0.0)
            for p in layers[-1]:
                if p.book < 0 or not pd:
                    continue
                b = books[p.book]
                exp = p.start + pd
                if exp < b.duration - tol(d) and not any(
                    c.book == p.book and c.exact and abs(c.start - exp) <= tol(exp - p.start) + 2.0 for c in cands
                ):
                    cands.add(Place(p.book, exp, False))
                if abs(exp - b.duration) <= tol(pd) + 2.0:
                    for bi in range(len(books)):
                        if bi != p.book and not any(c.book == bi and c.exact and c.start < tol(d) for c in cands):
                            cands.add(Place(bi, 0.0, False))

        def emit(c: Place) -> float:
            if c.book < 0:
                return 0.0
            title_hit = 0.5 if (tw & books[c.book].word_set) else 0.0
            return (2.0 if c.exact else 0.1) + title_hit

        layer: Dict[Place, Tuple[float, Optional[Place]]] = {}
        for c in cands:
            if i == 0:
                layer[c] = (emit(c) - (1.0 if (c.book >= 0 and c.start > tol(d)) else 0.0), None)
                continue
            best = None
            arg = None
            pd = float(tracks[i - 1].get("duration") or 0.0)
            for p, (ps, _) in layers[-1].items():
                if c.book < 0:
                    tr = -0.5
                elif p.book < 0:
                    tr = -1.0 if c.start > tol(d) else 0.0
                else:
                    exp = p.start + pd
                    slack = tol(pd) + 2.0
                    if c.book == p.book and abs(c.start - exp) <= slack:
                        tr = 1.0 if c.exact else 0.0
                    elif c.start <= tol(d) and abs(exp - books[p.book].duration) <= slack:
                        tr = 1.0 if c.exact else 0.0
                    else:
                        tr = -2.0
                v = ps + tr
                if best is None or v > best:
                    best, arg = v, p
            layer[c] = ((best or 0.0) + emit(c), arg)
        if len(layer) > KEEP_CANDIDATES:
            layer = dict(sorted(layer.items(), key=lambda kv: -kv[1][0])[:KEEP_CANDIDATES])
            layer.setdefault(UNKNOWN, (0.0, None))
        layers.append(layer)

    cur = max(layers[-1].items(), key=lambda kv: kv[1][0])[0]
    path = [cur]
    for i in range(n - 1, 0, -1):
        cur = layers[i][cur][1] or UNKNOWN
        path.append(cur)
    return path[::-1]


def analyse_myo_card_for_abs_transfer(card_id: str, extra_item_ids: Optional[List[str]] = None) -> Dict[str, Any]:
    """
    Analyses a Yoto MYO playlist `card_id`, matches its tracks against candidate Audiobookshelf books,
    and correlates any live MQTT playback position on that card.
    """
    if not clients.yoto_client or not clients.abs_client:
        return {"ok": False, "error": "Both Yoto and Audiobookshelf clients must be configured."}

    card = clients.yoto_client.get_card_detail(card_id) or {}
    if not card:
        return {"ok": False, "error": f"Yoto card '{card_id}' not found."}

    tracks = extract_card_tracks(card)
    card_title = str((card.get("card") or card).get("title") or card_id)
    search_words = set(words(card_title))
    for t in tracks[:20]:
        search_words.update(words(t.get("title") or ""))

    matrix = database.get_catalog_matrix()
    candidate_ids: List[str] = list(extra_item_ids or [])
    for it in matrix.get("items", []):
        if it.get("type") != "abs":
            continue
        it_words = set(words(f"{it.get('title') or ''} {it.get('series') or ''}"))
        if search_words & it_words:
            cid = str(it.get("id") or "")
            if cid and cid not in candidate_ids:
                candidate_ids.append(cid)
        if len(candidate_ids) >= 25:
            break

    books: List[TransferBook] = []
    for bid in candidate_ids:
        raw_item = clients.abs_client.get_item(bid)
        if raw_item:
            books.append(TransferBook.from_abs_item(raw_item))

    path = match_tracks_to_books(tracks, books)
    out_tracks = []
    for t, p in zip(tracks, path):
        row = {"n": t["n"], "title": t["title"], "duration": t["duration"]}
        if p.book >= 0:
            b = books[p.book]
            row.update({"item_id": b.item_id, "book": b.title, "start": round(p.start, 1), "exact": p.exact})
        out_tracks.append(row)

    # Check live MQTT state for any player currently on this card_id
    active_place = None
    for did, ev in get_mqtt_bridge().get_device_states().items():
        if ev.get("cardId") == card_id and ev.get("position") is not None:
            ck = str(ev.get("chapterKey") or ev.get("trackKey") or "")
            t_hit = next((t for t in tracks if t["chapter_key"] == ck or (ck.isdigit() and t["n"] == int(ck))), None)
            if t_hit:
                idx_0 = t_hit["n"] - 1
                p = path[idx_0] if 0 <= idx_0 < len(path) else UNKNOWN
                if p.book >= 0:
                    b = books[p.book]
                    pos_sec = min(max(0.0, p.start + float(ev["position"])), max(0.0, b.duration - 5.0))
                    earlier_ids = []
                    for prev_p in path[:idx_0]:
                        if prev_p.book >= 0 and prev_p.book != p.book:
                            eid = books[prev_p.book].item_id
                            if eid not in earlier_ids:
                                earlier_ids.append(eid)
                    active_place = {
                        "device_id": did,
                        "track": t_hit["n"],
                        "track_position": float(ev["position"]),
                        "item_id": b.item_id,
                        "title": b.title,
                        "position": round(pos_sec, 1),
                        "duration": b.duration,
                        "chapter": b.chapter_at(pos_sec),
                        "exact": p.exact,
                        "finish_item_ids": earlier_ids,
                    }
            break

    return {
        "ok": True,
        "card_id": card_id,
        "title": card_title,
        "tracks": out_tracks,
        "place": active_place,
    }

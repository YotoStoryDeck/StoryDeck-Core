"""
Series / Multi-Book Chaining & Local Own-Place Progress Engine for StoryDeck Core.
Allows a card slot to follow an entire Audiobookshelf series in sequence,
resolving the active unfinished book at tap time and chaining subsequent books,
while providing isolated local progress storage for `own_place` cards.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import clients
import database

logger = logging.getLogger("storydeck.bridge.series")


@dataclass
class SeriesBook:
    item_id: str
    title: str
    duration: float
    sequence: str = ""


def _seq_key(seq: str) -> Tuple[int, float, str]:
    """Numeric-aware sequence ordering ('2' < '10', '1.5' between 1 and 2)."""
    m = re.match(r"\s*(\d+(?:\.\d+)?)", str(seq or ""))
    return (0, float(m.group(1)), str(seq or "")) if m else (1, 0.0, str(seq or ""))


def get_series_books(series_name: str, series_id: str = "") -> List[SeriesBook]:
    """
    Finds all Audiobookshelf books belonging to `series_name` (or `series_id`) from
    StoryDeck's cached catalog matrix / media_items, sorted in numeric sequence order.
    """
    clean_name = (series_name or "").strip().lower()
    clean_id = (series_id or "").strip().lower()
    if not clean_name and not clean_id:
        return []

    matrix = database.get_catalog_matrix()
    books: List[SeriesBook] = []
    seen = set()

    for it in matrix.get("items", []):
        if it.get("type") != "abs":
            continue
        item_id = str(it.get("id") or "").strip()
        if not item_id or item_id in seen:
            continue

        it_series = str(it.get("series") or "").strip()
        it_order = str(it.get("series_order") or "").strip()
        matched_seq: Optional[str] = None

        if clean_name and it_series.lower() == clean_name:
            matched_seq = it_order
        elif clean_id and it_series.lower() == clean_id:
            matched_seq = it_order
        else:
            for s_entry in (it.get("series_list") or []):
                if not isinstance(s_entry, dict):
                    continue
                sn = str(s_entry.get("name") or "").strip().lower()
                sid = str(s_entry.get("id") or "").strip().lower()
                if (clean_name and sn == clean_name) or (clean_id and (sid == clean_id or sn == clean_id)):
                    matched_seq = str(s_entry.get("order") or s_entry.get("sequence") or it_order or "")
                    break

        if matched_seq is None:
            continue

        dur = 0.0
        raw_meta = it.get("raw_metadata")
        if isinstance(raw_meta, str) and raw_meta:
            try:
                raw_meta = json.loads(raw_meta)
            except Exception:
                raw_meta = {}
        if isinstance(raw_meta, dict):
            dur = float(raw_meta.get("duration") or (raw_meta.get("media") or {}).get("duration") or 0.0)

        seen.add(item_id)
        books.append(
            SeriesBook(
                item_id=item_id,
                title=str(it.get("title") or item_id),
                duration=dur,
                sequence=matched_seq,
            )
        )

    books.sort(key=lambda b: (_seq_key(b.sequence), b.title.lower()))
    return books


def current_series_book_index(books: List[SeriesBook], progress_by_item: Dict[str, Dict[str, Any]]) -> int:
    """
    Determines which book in a series should play right now:
    - The book with the most recent listening activity (`lastUpdate`), if not `isFinished`;
    - If that book is finished, the next book in the series;
    - With no activity at all, Book #1 (index 0).
    """
    if not books:
        return 0
    touched: List[Tuple[float, int]] = []
    for idx, b in enumerate(books):
        p = progress_by_item.get(b.item_id)
        if p:
            touched.append((float(p.get("lastUpdate") or 0.0), idx))
    if not touched:
        return 0
    _, latest_idx = max(touched)
    if progress_by_item[books[latest_idx].item_id].get("isFinished"):
        nxt = latest_idx + 1
        while nxt < len(books) and (progress_by_item.get(books[nxt].item_id) or {}).get("isFinished"):
            nxt += 1
        return nxt if nxt < len(books) else 0
    return latest_idx


# ==============================================================================
# Local Own-Place Progress Storage (`bridge_slots.local_progress`)
# ==============================================================================

def get_local_progress_map(slot_id: str) -> Dict[str, Dict[str, Any]]:
    """Returns `{item_id: {currentTime, duration, progress, isFinished, lastUpdate}}` for an own_place slot."""
    if not slot_id:
        return {}
    conn = database.get_db_connection()
    try:
        row = conn.execute("SELECT local_progress FROM bridge_slots WHERE slot_id = ?;", (slot_id,)).fetchone()
        if not row or not row[0]:
            return {}
        data = json.loads(row[0])
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}
    finally:
        conn.close()


def get_local_item_progress(slot_id: str, item_id: str) -> Optional[Dict[str, Any]]:
    """Returns saved local own_place progress for a specific book on a slot."""
    mp = get_local_progress_map(slot_id)
    entry = mp.get(item_id)
    return dict(entry) if isinstance(entry, dict) else None


def set_local_item_progress(
    slot_id: str,
    item_id: str,
    current_time: float,
    duration: float,
    is_finished: bool = False,
) -> bool:
    """Persists local own_place listening progress on `bridge_slots.local_progress` without touching ABS."""
    if not slot_id or not item_id:
        return False
    mp = get_local_progress_map(slot_id)
    dur = float(duration or 0.0)
    cur = max(0.0, float(current_time or 0.0))
    mp[item_id] = {
        "libraryItemId": item_id,
        "currentTime": round(cur, 1),
        "duration": dur,
        "progress": 1.0 if is_finished else ((cur / dur) if dur > 0 else 0.0),
        "isFinished": bool(is_finished),
        "lastUpdate": int(time.time() * 1000),
    }
    conn = database.get_db_connection()
    try:
        with conn:
            conn.execute(
                "UPDATE bridge_slots SET local_progress = ?, updated_at = CURRENT_TIMESTAMP WHERE slot_id = ?;",
                (json.dumps(mp), slot_id),
            )
        return True
    except Exception as e:
        logger.debug("set_local_item_progress notice for %s/%s: %s", slot_id, item_id, e)
        return False
    finally:
        conn.close()


def get_effective_item_progress(
    slot: Dict[str, Any],
    item_id: str,
    abs_token: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Reads progress for `(slot, item_id)`:
    - If `slot['own_place']` is truthy, reads from local SQLite `local_progress`.
    - Otherwise queries Audiobookshelf `GET /api/me/progress/<item_id>`.
    """
    if not item_id:
        return {}
    if slot and slot.get("own_place"):
        return get_local_item_progress(slot.get("slot_id") or "", item_id) or {}
    if clients.abs_client:
        abs_uid = ""
        abs_uname = ""
        if slot:
            owner_uid = str(slot.get("owner_user_id") or "").strip()
            owner_prof = str(slot.get("owner_profile_id") or "").strip()
            if owner_uid:
                u_creds = database.get_user_abs_credentials(owner_uid)
                abs_uid = u_creds.get("abs_user_id") or ""
                abs_uname = u_creds.get("abs_username") or ""
                if not abs_token:
                    abs_token = u_creds.get("abs_api_key") or None
            elif owner_prof:
                prof = database.get_profile_by_id(owner_prof) or {}
                abs_uname = str(prof.get("abs_username") or "").strip()
                if not abs_token:
                    abs_token = (prof.get("abs_api_key") or prof.get("abs_token") or "") or None
        return clients.abs_client.get_media_progress(
            item_id,
            token=abs_token,
            abs_user_id=abs_uid,
            abs_username=abs_uname,
        ) or {}
    return {}


def patch_effective_item_progress(
    slot: Dict[str, Any],
    item_id: str,
    current_time: float,
    duration: float,
    is_finished: bool = False,
    abs_token: Optional[str] = None,
) -> bool:
    """
    Writes progress for `(slot, item_id)`:
    - If `slot['own_place']` is truthy, writes to local SQLite `local_progress`.
    - Otherwise writes to Audiobookshelf `PATCH /api/me/progress/<item_id>`.
    """
    if not item_id:
        return False
    if slot and slot.get("own_place"):
        return set_local_item_progress(
            slot.get("slot_id") or "",
            item_id,
            current_time=current_time,
            duration=duration,
            is_finished=is_finished,
        )
    if clients.abs_client:
        return clients.abs_client.patch_media_progress(
            item_id,
            current_time=current_time,
            duration=duration,
            is_finished=is_finished,
            token=abs_token,
        )
    return False


def resolve_slot_series_chain(
    slot: Dict[str, Any],
    abs_token: Optional[str] = None,
) -> Tuple[List[SeriesBook], int, List[SeriesBook]]:
    """
    For a slot with `series_name` / `series_id`, returns `(all_series_books, active_index, following_books)`:
    - If `group_kind == 'series'` (or the slot has a series), finds all books in the series.
    - When `group_kind == 'series'`, automatically advances `slot['abs_item_id']` to the current unfinished
      book in the series based on ABS (or `own_place` local) progress.
    - Even on a regular single-book slot that belongs to a series, returns the `following_books` after
      `slot['abs_item_id']` so 5-hour rolling offline windows (`mode='next'`) and series cards can roll
      into the next book(s).
    """
    series_name = str(slot.get("series_name") or "").strip()
    series_id = str(slot.get("series_id") or "").strip()
    cur_item_id = str(slot.get("abs_item_id") or "").strip()

    books = get_series_books(series_name, series_id)
    if not books:
        return [], 0, []

    ids = [b.item_id for b in books]
    if str(slot.get("group_kind") or "") == "series" or str(slot.get("offline_mode") or "") == "series":
        prog_map: Dict[str, Dict[str, Any]] = {}
        if slot.get("own_place"):
            prog_map = get_local_progress_map(slot.get("slot_id") or "")
        elif clients.abs_client:
            for b in books:
                p = clients.abs_client.get_media_progress(b.item_id, token=abs_token)
                if p:
                    prog_map[b.item_id] = p
        idx = current_series_book_index(books, prog_map)
        active = books[idx]
        if active.item_id != cur_item_id and slot.get("slot_id"):
            conn = database.get_db_connection()
            try:
                with conn:
                    conn.execute(
                        "UPDATE bridge_slots SET abs_item_id = ?, title = ?, updated_at = CURRENT_TIMESTAMP WHERE slot_id = ?;",
                        (active.item_id, active.title, slot["slot_id"]),
                    )
                slot["abs_item_id"] = active.item_id
                slot["title"] = active.title
            finally:
                conn.close()
        return books, idx, books[idx + 1:]

    if cur_item_id in ids:
        idx = ids.index(cur_item_id)
        return books, idx, books[idx + 1:]
    return books, 0, []

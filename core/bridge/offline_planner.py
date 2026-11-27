"""
Offline Track Planner & Submode Resolvers.
Contains 5-hour rolling window ('next'), whole-book/series ('book' / 'series') track layout planning,
submode/packing resolution, and ABS audio source resolution helpers.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

import clients
import database
from .chapters import (
    DEFAULT_OFFLINE_TRACK_SECONDS,
    DEFAULT_PART_SECONDS,
    MAX_OFFLINE_NEXT_TRACKS,
    MAX_OFFLINE_SERIES_TRACKS,
    WHOLE_TRACKS,
    YOTO_MAX_TRACK,
    chapter_spans,
    chapter_units,
    min_tracks,
    plan_segments,
    plan_whole,
)
from .manager import get_bridge_manager
from .mqtt_client import get_mqtt_bridge
from .series import get_effective_item_progress, resolve_slot_series_chain
from .streamer import AudioTrackSource

logger = logging.getLogger("storydeck.bridge.offline.planner")

OFFLINE_MODES = ("next", "book", "series")
DEFAULT_REWIND_SECONDS = 15.0
DEFAULT_LOOKBACK_HOURS = 1.5


def _hms(sec: float) -> str:
    s = max(0, int(sec))
    return f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def normalize_offline_submode(raw_mode: Optional[str], slot: Optional[Dict[str, Any]] = None) -> str:
    """
    Maps UI/API delivery mode strings to one of ('next', 'book', 'series'):
    - 'offline_next' | 'next' | '5h' | 'hours' -> 'next' (5-hour rolling window from ABS resume point)
    - 'offline_series' | 'series' -> 'series' (rolling whole books <= 100 tracks, or condensed series)
    - 'offline_book' | 'book' | 'offline' -> 'book' (or slot's stored offline_mode if in OFFLINE_MODES)
    """
    m = str(raw_mode or "").strip().lower()
    if m in ("next", "offline_next", "offline_5h", "5h", "hours", "rolling"):
        return "next"
    if m in ("series", "offline_series"):
        return "series"
    if m in ("book", "offline_book"):
        return "book"
    if slot:
        stored = str(slot.get("offline_mode") or "").strip().lower()
        if stored in OFFLINE_MODES:
            return stored
        if str(slot.get("group_kind") or "") == "series":
            return "next"
    return "book"


def resolve_series_packing(slot: Optional[Dict[str, Any]] = None) -> str:
    """
    Resolves effective offline series packing mode ('chapters' [default] or 'condense'):
    - 'chapters': maintains each book's individual chapters and rolls whole books (<= 100 tracks).
    - 'condense': condenses chapters into merged parts to fit maximum books per card.
    Checks per-slot `series_packing` override first, then global `offline_series_packing` setting.
    """
    slot_val = str((slot or {}).get("series_packing") or "").strip().lower()
    if slot_val in ("chapters", "condense"):
        return slot_val
    try:
        sys_val = str((database.get_system_settings() or {}).get("offline_series_packing") or "chapters").strip().lower()
    except Exception:
        sys_val = "chapters"
    return "condense" if sys_val == "condense" else "chapters"


def _extract_slot_base_url(slot: Dict[str, Any]) -> str:
    """Extracts external HTTP(S) base URL prefix from an existing slot track's stream_url."""
    for t in (slot.get("tracks") or []):
        s_url = str(t.get("stream_url") or "")
        if "/s/audio/" in s_url and s_url.startswith("http"):
            return s_url.split("/s/audio/", 1)[0]
    return ""



class OfflinePlannerMixin:
    """Planning and ABS source/playback-state helpers mixed into OfflineTranscodeManager."""

    def _resolve_slot_abs_token(self, slot: Dict[str, Any]) -> Optional[str]:
        owner_user_id = str(slot.get("owner_user_id") or "").strip()
        if owner_user_id:
            tok = database.get_user_abs_credentials(owner_user_id).get("abs_api_key", "")
            if tok:
                return tok
        owner_prof = str(slot.get("owner_profile_id") or "").strip()
        if clients.abs_client:
            return clients.abs_client.resolve_token_for_profile(owner_prof or None)
        return None

    def _is_slot_playing(self, slot: Dict[str, Any]) -> bool:
        """Returns True if the card is currently streaming or playing on a Yoto player (do not overwrite playlist mid-play)."""
        try:
            from .playback import get_playback_manager
            pm = get_playback_manager()
            for s in pm.get_active_sessions_summary():
                if s.get("slot_id") == slot.get("slot_id") and int(s.get("open_streams") or 0) > 0:
                    return True
        except Exception:
            pass
        yoto_cid = str(slot.get("yoto_card_id") or "").strip()
        if yoto_cid:
            try:
                for ev in get_mqtt_bridge().get_device_states().values():
                    if (
                        ev.get("cardId") == yoto_cid
                        and str(ev.get("playbackStatus") or "").lower() == "playing"
                        and (time.time() - float(ev.get("at") or 0)) < 600
                    ):
                        return True
            except Exception:
                pass
        return False

    def _plan_offline_tracks(
        self,
        slot: Dict[str, Any],
        submode: str,
        hours: float,
        speed: float,
        abs_token: Optional[str],
        base_url: str,
        anchor_current_book: bool = False,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """
        Plans the tracks for an offline copy in 'next' (5-hour rolling window), 'book' (whole book),
        or 'series' (whole series) mode, returning `(planned_tracks, offline_meta_dict)`.
        """
        mgr = get_bridge_manager()
        abs_item_id = str(slot.get("abs_item_id") or "").strip()
        if not abs_item_id or not clients.abs_client:
            return slot.get("tracks") or [], {}

        all_series_books, active_idx, following_books = resolve_slot_series_chain(
            slot,
            abs_token=abs_token,
            anchor_current_book=anchor_current_book,
        )
        abs_item_id = str(slot.get("abs_item_id") or abs_item_id).strip()

        item_data = clients.abs_client.get_item(abs_item_id, token=abs_token) or {}
        media = item_data.get("media") or {}
        meta = media.get("metadata") or {}
        book_title = str(slot.get("title") or meta.get("title") or "Audiobook")
        duration = float(media.get("duration") or slot.get("total_duration") or 0.0)
        chapters = media.get("chapters") or []

        prog = get_effective_item_progress(slot, abs_item_id, abs_token=abs_token)
        pos = 0.0 if prog.get("isFinished") else float(prog.get("currentTime") or 0.0)
        if duration > 0 and pos >= duration - 10.0:
            pos = 0.0
        resume_start = max(0.0, pos - DEFAULT_REWIND_SECONDS) if pos > DEFAULT_REWIND_SECONDS else 0.0

        planned_segments: List[Dict[str, Any]] = []
        books_left_off = 0

        if submode == "next":
            # 5-Hour Rotational Window:
            # 1. If the total remaining media content (current book + any following series books) fits within the 5-hour window,
            #    start at 0.0 so the entire book is loaded normally onto the card.
            # 2. Otherwise (> 5h), look back 1.5 hours prior to `resume_start`, snap back to the beginning of the chapter/track
            #    containing that timestamp (so the child/parent can rewind after a bedtime sleep timer), and fill the 5-hour
            #    window forward from that chapter boundary (leaving ~3+ hours of forward audio before the window needs refreshing).
            window_budget_sec = max(0.5, min(float(hours or 5.0), 5.0)) * 3600.0 * speed
            max_len = DEFAULT_OFFLINE_TRACK_SECONDS * speed
            total_chain_dur = duration + sum(float(fb.duration or 0.0) for fb in following_books)

            if duration > 0 and total_chain_dur <= window_budget_sec:
                window_start_sec = 0.0
            else:
                lookback_target = max(0.0, resume_start - (DEFAULT_LOOKBACK_HOURS * 3600.0 * speed))
                window_start_sec = 0.0
                if lookback_target > 0.0:
                    snap_fn = mgr.quiet_spots.snapper(abs_item_id)
                    spans = chapter_spans(chapters, duration, DEFAULT_PART_SECONDS, snap=snap_fn) if duration > 0 else []
                    for sp_start, sp_end, _ in spans:
                        if sp_start <= lookback_target < sp_end:
                            window_start_sec = sp_start
                            break
                    else:
                        window_start_sec = lookback_target

            left_sec = window_budget_sec
            chain = [(abs_item_id, book_title, duration, chapters, window_start_sec)]
            for fb in following_books:
                chain.append((fb.item_id, fb.title, fb.duration, None, 0.0))

            for b_id, b_title, b_dur, b_ch, b_start in chain:
                if left_sec < 30.0 or len(planned_segments) >= MAX_OFFLINE_NEXT_TRACKS:
                    break
                if b_ch is None or b_dur <= 0:
                    b_item = clients.abs_client.get_item(b_id, token=abs_token) or {}
                    b_media = b_item.get("media") or {}
                    b_ch = b_media.get("chapters") or []
                    b_dur = float(b_media.get("duration") or b_dur or 0.0)
                if b_dur <= 0 or b_start >= b_dur:
                    continue
                b_end = min(b_dur, b_start + left_sec)
                snap = mgr.quiet_spots.snapper(b_id)
                for s, e, ch_title in plan_segments(b_start, b_end, b_ch, max_len=max_len, snap=snap):
                    label = ch_title or f"{b_title} · {_hms(s)}"
                    if following_books:
                        label = f"{b_title}: {ch_title}" if ch_title else f"{b_title} · {_hms(s)}"
                    planned_segments.append({
                        "abs_item_id": b_id,
                        "book_title": b_title,
                        "book_duration": b_dur,
                        "start_time": s,
                        "end_time": e,
                        "duration": max(0.1, e - s),
                        "title": label,
                    })
                left_sec -= (b_end - b_start)
            planned_segments = planned_segments[:MAX_OFFLINE_NEXT_TRACKS]

        else:
            # 'book' or 'series': whole book(s) packed into <= 99 ('book') or <= 100 ('series') tracks
            max_len = YOTO_MAX_TRACK * speed
            packing = resolve_series_packing(slot) if submode == "series" else "condense"
            track_limit = MAX_OFFLINE_SERIES_TRACKS if submode == "series" else WHOLE_TRACKS
            earlier = (
                all_series_books[:active_idx]
                if (submode == "series" and packing == "condense" and all_series_books)
                else []
            )
            later = following_books if submode == "series" else []

            candidate_group: List[Tuple[str, str, float, Optional[List[Dict[str, Any]]]]] = (
                [(b.item_id, b.title, b.duration, None) for b in earlier]
                + [(abs_item_id, book_title, duration, chapters)]
                + [(b.item_id, b.title, b.duration, None) for b in later]
            )
            ch_cache: Dict[str, List[Dict[str, Any]]] = {abs_item_id: chapters}
            dur_cache: Dict[str, float] = {abs_item_id: duration}

            def _load_book_meta(b_id: str, fallback_dur: float) -> Tuple[List[Dict[str, Any]], float]:
                if b_id not in ch_cache or dur_cache.get(b_id, 0.0) <= 0:
                    b_item = clients.abs_client.get_item(b_id, token=abs_token) or {}
                    b_media = b_item.get("media") or {}
                    ch_cache[b_id] = b_media.get("chapters") or []
                    dur_cache[b_id] = float(b_media.get("duration") or fallback_dur or 0.0)
                return ch_cache[b_id], dur_cache[b_id]

            fit_books: List[Tuple[str, str, float]] = []
            plans: List[List[Tuple[float, float, str]]] = []

            if submode == "series" and packing == "chapters":
                # Branch A (Default): preserve individual chapters across whole books up to 100 tracks;
                # finished earlier books are dropped so subsequent books roll onto the card.
                used_tracks = 0
                for b_id, b_title, b_dur, _ in candidate_group:
                    c_list, d_val = _load_book_meta(b_id, b_dur)
                    if d_val <= 0:
                        continue
                    snap = mgr.quiet_spots.snapper(b_id)
                    units = chapter_units(c_list, d_val, max_len, DEFAULT_PART_SECONDS, snap)
                    if not units:
                        continue
                    if used_tracks + len(units) <= track_limit:
                        fit_books.append((b_id, b_title, d_val))
                        plans.append([(s, e, t) for s, e, t, _n in units])
                        used_tracks += len(units)
                    elif not fit_books:
                        # Single active book alone exceeds 100 chapters: condense only this book.
                        single_plan = plan_whole(
                            [(c_list, d_val)],
                            max_len=max_len,
                            limit=track_limit,
                            part_seconds=DEFAULT_PART_SECONDS,
                            snaps=[snap],
                        )
                        fit_books.append((b_id, b_title, d_val))
                        plans.append(single_plan[0] if single_plan else [])
                        break
                    else:
                        break
                books_left_off = max(0, len(candidate_group) - len(fit_books))
            else:
                def _take_fitting(group):
                    fit = []
                    fit_chs = []
                    used_tracks = 0
                    for b_id, b_title, b_dur, _ in group:
                        c_list, d_val = _load_book_meta(b_id, b_dur)
                        if d_val <= 0:
                            continue
                        need = min_tracks(c_list, d_val, max_len, DEFAULT_PART_SECONDS, mgr.quiet_spots.snapper(b_id))
                        if used_tracks + need > track_limit and fit:
                            break
                        fit.append((b_id, b_title, d_val))
                        fit_chs.append(c_list)
                        used_tracks += need
                    return fit, fit_chs

                fit_books, fit_chapters = _take_fitting(candidate_group)
                if earlier and abs_item_id not in [b[0] for b in fit_books]:
                    fit_books, fit_chapters = _take_fitting(candidate_group[len(earlier):])
                books_left_off = max(0, len(candidate_group) - len(fit_books))

                if fit_books:
                    snaps = [mgr.quiet_spots.snapper(b[0]) for b in fit_books]
                    plans = plan_whole(
                        [(ch, b[2]) for ch, b in zip(fit_chapters, fit_books)],
                        max_len=max_len,
                        limit=track_limit,
                        part_seconds=DEFAULT_PART_SECONDS,
                        snaps=snaps,
                    )

            if fit_books and plans:
                multi = len(fit_books) > 1
                for (b_id, b_title, b_dur), plan in zip(fit_books, plans):
                    for s, e, ch_title in plan:
                        label = ch_title or f"{b_title} · {_hms(s)}"
                        if multi:
                            label = f"{b_title}: {ch_title}" if ch_title else f"{b_title} · {_hms(s)}"
                        planned_segments.append({
                            "abs_item_id": b_id,
                            "book_title": b_title,
                            "book_duration": b_dur,
                            "start_time": s,
                            "end_time": e,
                            "duration": max(0.1, e - s),
                            "title": label,
                        })
                planned_segments = planned_segments[:track_limit]

        if not planned_segments:
            return slot.get("tracks") or [], {}

        # Persist the planned track layout to bridge_tracks if it differs from current tracks
        slot_id = slot["slot_id"]
        server_url = base_url.rstrip("/") if base_url else ""
        existing_shas = {
            (str(t.get("abs_item_id") or abs_item_id), round(float(t.get("start_time") or 0), 1), round(float(t.get("end_time") or 0), 1)): t.get("yoto_sha256") or ""
            for t in (slot.get("tracks") or [])
            if t.get("yoto_sha256")
        }

        order_books = list(dict.fromkeys(seg["abs_item_id"] for seg in planned_segments))
        first_seg = planned_segments[0]
        last_seg = planned_segments[-1]
        offline_meta: Dict[str, Any] = {
            "status": "building",
            "mode": submode,
            "hours": float(hours or 5.0),
            "speed": speed,
            "books": order_books,
            "cur_item": first_seg["abs_item_id"],
            "start": first_seg["start_time"],
            "end": last_seg["end_time"],
            "books_left_off": books_left_off,
            "last_pos": first_seg["start_time"],
            "last_write_wall": time.time(),
            "tracks": [],
        }
        if submode == "series":
            offline_meta["series_packing"] = resolve_series_packing(slot)
        if first_seg["abs_item_id"] != abs_item_id or resume_start > first_seg["start_time"] + 1.0:
            offline_meta["resume"] = {"item_id": abs_item_id, "pos": resume_start}

        db_tracks: List[Dict[str, Any]] = []
        conn = database.get_db_connection()
        try:
            with conn:
                cur = conn.cursor()
                cur.execute("DELETE FROM bridge_tracks WHERE slot_id = ?;", (slot_id,))
                for idx, seg in enumerate(planned_segments, 1):
                    stream_url = f"{server_url}/s/audio/{slot_id}/{idx}" if server_url else f"/s/audio/{slot_id}/{idx}"
                    sha_key = (seg["abs_item_id"], round(seg["start_time"], 1), round(seg["end_time"], 1))
                    warm_sha = existing_shas.get(sha_key, "")
                    cur.execute(
                        """
                        INSERT INTO bridge_tracks (
                            slot_id, track_idx, abs_item_id, book_title, book_duration,
                            title, start_time, end_time, duration, yoto_sha256, stream_url
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                        """,
                        (
                            slot_id,
                            idx,
                            seg["abs_item_id"],
                            seg["book_title"],
                            seg["book_duration"],
                            seg["title"],
                            seg["start_time"],
                            seg["end_time"],
                            seg["duration"],
                            warm_sha,
                            stream_url,
                        ),
                    )
                    db_tracks.append({
                        "track_idx": idx,
                        "abs_item_id": seg["abs_item_id"],
                        "book_title": seg["book_title"],
                        "book_duration": seg["book_duration"],
                        "title": seg["title"],
                        "start_time": seg["start_time"],
                        "end_time": seg["end_time"],
                        "duration": seg["duration"],
                        "yoto_sha256": warm_sha,
                        "stream_url": stream_url,
                    })
                    offline_meta["tracks"].append({
                        "key": f"{idx:02d}",
                        "track_idx": idx,
                        "item_id": seg["abs_item_id"],
                        "book": seg["book_title"],
                        "book_duration": seg["book_duration"],
                        "start": seg["start_time"],
                        "end": seg["end_time"],
                        "on_card": bool(warm_sha),
                    })
                cur.execute(
                    """
                    UPDATE bridge_slots
                    SET offline_mode = ?, offline_hours = ?, offline_meta = ?, offline_status = 'building', updated_at = CURRENT_TIMESTAMP
                    WHERE slot_id = ?;
                    """,
                    (submode, float(hours or 5.0), json.dumps(offline_meta), slot_id),
                )
        finally:
            conn.close()

        return db_tracks, offline_meta

    def _build_sources_for_book(
        self,
        item_id: str,
        abs_token: Optional[str],
        cache: Dict[str, Tuple[List[AudioTrackSource], str]],
    ) -> Tuple[List[AudioTrackSource], str]:
        if item_id in cache:
            return cache[item_id]
        if not clients.abs_client:
            return [], ""
        item_data = clients.abs_client.get_item(item_id, token=abs_token) or {}
        sources, auth_hdr = get_bridge_manager()._build_abs_audio_sources(
            item_id, item_data, abs_token_override=abs_token
        )
        cache[item_id] = (sources, auth_hdr)
        return sources, auth_hdr


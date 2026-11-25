"""
Offline Transcoding Engine, 5-Hour Rolling Window ('next'), Whole-Book/Series Planner,
Live Mixed Playlist Batching, Offline MQTT->ABS Progress Sync, and Ephemeral Cache Pruner.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import clients
import database
from .chapters import (
    DEFAULT_OFFLINE_TRACK_SECONDS,
    DEFAULT_PART_SECONDS,
    MAX_OFFLINE_NEXT_TRACKS,
    WHOLE_TRACKS,
    YOTO_MAX_TRACK,
    chapter_spans,
    chapter_units,
    min_tracks,
    plan_segments,
    plan_whole,
)
from .icons import get_or_upload_yoto_icon, upload_abs_cover_to_yoto
from .manager import get_bridge_manager
from .mqtt_client import get_mqtt_bridge
from .series import (
    get_effective_item_progress,
    patch_effective_item_progress,
    resolve_slot_series_chain,
)
from .streamer import AudioStreamer, AudioTrackSource

logger = logging.getLogger("storydeck.bridge.offline")

OFFLINE_MODES = ("next", "book", "series")
LIVE_BATCH = 5
LIVE_SECONDS = 120.0
DEFAULT_REWIND_SECONDS = 15.0
DEFAULT_LOOKBACK_HOURS = 1.5
OFFLINE_SYNC_INTERVAL = 15.0


def _hms(sec: float) -> str:
    s = max(0, int(sec))
    return f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def normalize_offline_submode(raw_mode: Optional[str], slot: Optional[Dict[str, Any]] = None) -> str:
    """
    Maps UI/API delivery mode strings to one of ('next', 'book', 'series'):
    - 'offline_next' | 'next' | '5h' | 'hours' -> 'next' (5-hour rolling window from ABS resume point)
    - 'offline_series' | 'series' -> 'series' (whole series packed into <= 99 tracks)
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


class OfflineTranscodeManager:
    """
    Manages background transcoding and upload jobs for offline Yoto cards:
    - Supports 'next' (5h rolling window), 'book' (whole book <=99 tracks), and 'series' (whole series <=99 tracks).
    - Publishes hybrid mixed playlists (`save_mixed_playlist`) every 5 tracks / 120s while building.
    - Tracks MQTT playback events on offline cards (`on_mqtt_event`) to sync listening progress back to ABS
      and auto-jump whole-book copies to the listener's saved ABS position.
    """

    def __init__(self):
        self._jobs: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._queue_sem = threading.Semaphore(1)

    def get_job_status(self, slot_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            job = self._jobs.get(slot_id)
            if not job:
                return None
            return dict(job)

    def is_busy(self, slot_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(slot_id)
            return bool(job and job.get("status") in ("queued", "encoding", "uploading", "processing"))

    def _verify_last_uploaded_track(self, slot: Dict[str, Any], tracks: List[Dict[str, Any]]) -> bool:
        """
        Verifies that the last track marked with `yoto_sha256` before an interruption is complete and uncorrupted:
        1. Validates the stored SHA string format (valid non-empty Yoto base64url/hex digest >= 32 chars).
        2. If the slot's Yoto Cloud playlist (`yoto_card_id`) already contains that track as `yoto:#<sha>`,
           checks that Yoto Cloud's transcoded duration matches the expected track duration (`(end_time - start_time) / speed`)
           within tolerance.
        3. If the last recorded track was NOT yet committed to the live Yoto Cloud playlist (or fails validation),
           clears its `yoto_sha256` in SQLite so the resumed job cleanly re-encodes and re-uploads that boundary track.
        """
        uploaded = [t for t in (tracks or []) if str(t.get("yoto_sha256") or "").strip()]
        if not uploaded:
            return True

        last_trk = uploaded[-1]
        last_idx = int(last_trk.get("track_idx") or 0)
        last_sha = str(last_trk.get("yoto_sha256") or "").strip()
        speed = max(0.5, min(2.5, float(slot.get("speed") or 1.0)))
        expected_dur = max(0.1, (float(last_trk.get("end_time") or 0.0) - float(last_trk.get("start_time") or 0.0)) / speed)

        is_valid = len(last_sha) >= 32 and all(c.isalnum() or c in "-_=" for c in last_sha)

        # Cross-check against the live Yoto Cloud playlist if available
        yoto_cid = str(slot.get("yoto_card_id") or "").strip()
        if is_valid and yoto_cid and clients.yoto_client:
            try:
                detail = clients.yoto_client.get_card_detail(yoto_cid) or {}
                inner = detail.get("card") if isinstance(detail.get("card"), dict) else detail
                chapters = (inner.get("content") or {}).get("chapters") or []
                key_str = f"{last_idx:02d}"
                cloud_trk = None
                for ch in chapters:
                    for ct in (ch.get("tracks") or []):
                        if str(ct.get("key") or "") == key_str or str(ch.get("key") or "") == key_str:
                            cloud_trk = ct
                            break
                    if cloud_trk:
                        break
                if cloud_trk and str(cloud_trk.get("trackUrl") or "") == f"yoto:#{last_sha}":
                    cloud_dur = float(cloud_trk.get("duration") or 0.0)
                    cloud_size = int(cloud_trk.get("fileSize") or 0)
                    if cloud_dur > 0 and abs(cloud_dur - expected_dur) > max(15.0, expected_dur * 0.10):
                        logger.warning(
                            "Last uploaded track %d on slot %s failed Yoto duration integrity check (cloud=%.1fs vs expected=%.1fs); re-encoding.",
                            last_idx,
                            slot["slot_id"],
                            cloud_dur,
                            expected_dur,
                        )
                        is_valid = False
                    elif cloud_size > 0 and cloud_size < int(expected_dur * 2000):
                        logger.warning(
                            "Last uploaded track %d on slot %s failed Yoto fileSize integrity check (%d bytes for %.1fs); re-encoding.",
                            last_idx,
                            slot["slot_id"],
                            cloud_size,
                            expected_dur,
                        )
                        is_valid = False
                else:
                    # The boundary track was saved to SQLite right before restart/interruption and not yet verified on the Yoto card;
                    # re-encode that single boundary track to guarantee zero partial/corrupted state.
                    logger.info(
                        "Re-verifying boundary track %d ('%s') on slot %s from clean encode after restart.",
                        last_idx,
                        last_trk.get("title") or last_idx,
                        slot["slot_id"],
                    )
                    is_valid = False
            except Exception as e:
                logger.debug("Cloud track integrity check notice for %s/%d: %s", slot["slot_id"], last_idx, e)

        if not is_valid:
            last_trk["yoto_sha256"] = ""
            with database.get_db_connection() as conn:
                conn.execute(
                    "UPDATE bridge_tracks SET yoto_sha256 = '' WHERE slot_id = ? AND track_idx = ?;",
                    (slot["slot_id"], last_idx),
                )
                conn.commit()
            return False
        return True

    def resume_interrupted_jobs(self) -> int:
        """
        Scans `bridge_slots` on server startup for any offline slot that was interrupted
        mid-upload (`offline_status == 'building'` or missing `yoto_sha256` tracks),
        verifies the last uploaded track is complete and uncorrupted, and automatically
        re-queues it so it resumes cleanly.
        """
        resumed = 0
        try:
            mgr = get_bridge_manager()
            for slot in (mgr.list_slots() or []):
                if slot.get("mode") != "offline" or not slot.get("abs_item_id"):
                    continue
                if slot.get("kind") in ("profile_cache", "ephemeral_offline"):
                    continue
                tracks = slot.get("tracks") or []
                is_building = str(slot.get("offline_status") or "").lower() == "building"
                has_unencoded = bool(tracks) and any(not str(t.get("yoto_sha256") or "").strip() for t in tracks)
                if not is_building and not has_unencoded:
                    continue
                self._verify_last_uploaded_track(slot, tracks)
                # Extract base_url from existing track stream_url if available
                base_url = ""
                for t in tracks:
                    s_url = str(t.get("stream_url") or "")
                    if "/s/audio/" in s_url and s_url.startswith("http"):
                        base_url = s_url.split("/s/audio/", 1)[0]
                        break
                submode = str(slot.get("offline_mode") or "book")
                hours = float(slot.get("offline_hours") or 5.0)
                done_cnt = sum(1 for t in tracks if str(t.get("yoto_sha256") or "").strip())
                logger.info(
                    "Resuming interrupted offline transcode for slot %s (%s): %d/%d verified tracks already uploaded",
                    slot["slot_id"],
                    slot.get("title") or slot["abs_item_id"],
                    done_cnt,
                    len(tracks),
                )
                self.queue_transcode(slot["slot_id"], base_url=base_url, offline_mode=submode, hours=hours)
                resumed += 1
        except Exception as e:
            logger.warning("Failed scanning for interrupted offline jobs on startup: %s", e)
        return resumed

    def queue_transcode(
        self,
        slot_id: str,
        base_url: str = "",
        offline_mode: Optional[str] = None,
        hours: float = 5.0,
    ) -> Dict[str, Any]:
        """Queues a card slot for offline transcoding and Yoto upload."""
        with self._lock:
            existing = self._jobs.get(slot_id)
            if existing and existing.get("status") in ("queued", "encoding", "uploading", "processing"):
                return dict(existing)

            job = {
                "slot_id": slot_id,
                "status": "queued",
                "offline_mode": offline_mode or "book",
                "hours": float(hours or 5.0),
                "progress": 0.0,
                "done": 0,
                "total": 0,
                "error": None,
                "started_at": time.time(),
                "updated_at": time.time(),
            }
            self._jobs[slot_id] = job

        thread = threading.Thread(
            target=self._run_job_guarded,
            args=(slot_id, base_url, offline_mode, float(hours or 5.0)),
            daemon=True,
        )
        thread.start()
        return job

    def _run_job_guarded(
        self,
        slot_id: str,
        base_url: str,
        offline_mode: Optional[str],
        hours: float,
    ):
        with self._queue_sem:
            self._run_job(slot_id, base_url, offline_mode=offline_mode, hours=hours)

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
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """
        Plans the tracks for an offline copy in 'next' (5-hour rolling window), 'book' (whole book),
        or 'series' (whole series) mode, returning `(planned_tracks, offline_meta_dict)`.
        """
        mgr = get_bridge_manager()
        abs_item_id = str(slot.get("abs_item_id") or "").strip()
        if not abs_item_id or not clients.abs_client:
            return slot.get("tracks") or [], {}

        all_series_books, active_idx, following_books = resolve_slot_series_chain(slot, abs_token=abs_token)
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
            # 'book' or 'series': whole book(s) packed into <= 99 tracks
            max_len = YOTO_MAX_TRACK * speed
            earlier = all_series_books[:active_idx] if (submode == "series" and all_series_books) else []
            later = following_books if submode == "series" else []

            candidate_group: List[Tuple[str, str, float, Optional[List[Dict[str, Any]]]]] = (
                [(b.item_id, b.title, b.duration, None) for b in earlier]
                + [(abs_item_id, book_title, duration, chapters)]
                + [(b.item_id, b.title, b.duration, None) for b in later]
            )
            ch_cache: Dict[str, List[Dict[str, Any]]] = {abs_item_id: chapters}
            dur_cache: Dict[str, float] = {abs_item_id: duration}

            def _take_fitting(group):
                fit = []
                fit_chs = []
                used_tracks = 0
                for b_id, b_title, b_dur, b_ch in group:
                    if b_id not in ch_cache or dur_cache.get(b_id, 0.0) <= 0:
                        b_item = clients.abs_client.get_item(b_id, token=abs_token) or {}
                        b_media = b_item.get("media") or {}
                        ch_cache[b_id] = b_media.get("chapters") or []
                        dur_cache[b_id] = float(b_media.get("duration") or b_dur or 0.0)
                    c_list = ch_cache[b_id]
                    d_val = dur_cache[b_id]
                    if d_val <= 0:
                        continue
                    need = min_tracks(c_list, d_val, max_len, DEFAULT_PART_SECONDS, mgr.quiet_spots.snapper(b_id))
                    if used_tracks + need > WHOLE_TRACKS and fit:
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
                    limit=WHOLE_TRACKS,
                    part_seconds=DEFAULT_PART_SECONDS,
                    snaps=snaps,
                )
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
                planned_segments = planned_segments[:WHOLE_TRACKS]

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

    def _run_job(
        self,
        slot_id: str,
        base_url: str,
        offline_mode: Optional[str] = None,
        hours: float = 5.0,
    ):
        mgr = get_bridge_manager()
        slot = mgr.get_slot(slot_id)
        if not slot:
            self._update_job(slot_id, status="failed", error="Slot not found")
            return

        abs_item_id = slot.get("abs_item_id")
        if not abs_item_id or not clients.abs_client:
            self._update_job(slot_id, status="failed", error="No ABS client or item configured")
            return

        abs_token = self._resolve_slot_abs_token(slot)
        speed = max(0.5, min(2.5, float(slot.get("speed") or 1.0)))
        submode = normalize_offline_submode(offline_mode or slot.get("offline_mode"), slot)
        eff_hours = float(hours or slot.get("offline_hours") or 5.0)

        try:
            tracks, offline_meta = self._plan_offline_tracks(
                slot,
                submode=submode,
                hours=eff_hours,
                speed=speed,
                abs_token=abs_token,
                base_url=base_url,
            )
        except Exception as e:
            logger.warning("Offline track planning fallback for %s: %s", slot_id, e)
            tracks = slot.get("tracks") or []
            offline_meta = {}

        if not tracks:
            self._update_job(slot_id, status="failed", error="Slot has no tracks")
            return

        total_tracks = len(tracks)
        self._update_job(
            slot_id,
            status="encoding",
            offline_mode=submode,
            hours=eff_hours,
            total=total_tracks,
            done=0,
            progress=0.0,
        )

        sources_cache: Dict[str, Tuple[List[AudioTrackSource], str]] = {}
        first_book_id = str(tracks[0].get("abs_item_id") or abs_item_id)
        first_sources, _ = self._build_sources_for_book(first_book_id, abs_token, sources_cache)
        if not first_sources:
            self._update_job(slot_id, status="failed", error="ABS item has no audio files")
            return

        server_url = base_url.rstrip("/") if base_url else ""
        fallback_cover = f"{server_url}/api/cover/{abs_item_id}" if server_url else ""
        cover_url = upload_abs_cover_to_yoto(abs_item_id, fallback_url=fallback_cover, token=abs_token)

        streamer = AudioStreamer(bitrate_kbps=64, channels=1, sample_rate=44100)
        uploaded_infos: List[Optional[Dict[str, Any]]] = [None] * total_tracks
        live_state = {"saved_count": -1, "saved_at": 0.0, "ok": True}
        yoto_card_id = slot.get("yoto_card_id") or None

        def _save_live_batch(force: bool = False):
            nonlocal yoto_card_id
            if not clients.yoto_client or not hasattr(clients.yoto_client, "save_mixed_playlist"):
                return
            done_count = sum(1 for x in uploaded_infos if x is not None)
            if not live_state["ok"] or done_count == live_state["saved_count"] or self._is_slot_playing(slot):
                return
            if (
                not force
                and live_state["saved_count"] >= 0
                and (done_count - live_state["saved_count"]) < LIVE_BATCH
                and (time.time() - live_state["saved_at"]) < LIVE_SECONDS
            ):
                return
            entries: List[Dict[str, Any]] = []
            for idx_0, trk in enumerate(tracks):
                t_idx = int(trk["track_idx"])
                info = uploaded_infos[idx_0]
                icon_stream_url = f"{server_url}/s/icon/{slot_id}/{t_idx}.png" if server_url else ""
                if info:
                    entries.append({
                        **info,
                        "track_idx": t_idx,
                        "key": f"{t_idx:02d}",
                        "title": trk.get("title") or f"Track {t_idx}",
                        "icon_url": icon_stream_url,
                    })
                else:
                    entries.append({
                        "track_idx": t_idx,
                        "key": f"{t_idx:02d}",
                        "title": trk.get("title") or f"Track {t_idx}",
                        "stream_url": trk.get("stream_url") or (f"{server_url}/s/audio/{slot_id}/{t_idx}" if server_url else f"/s/audio/{slot_id}/{t_idx}"),
                        "icon_url": icon_stream_url,
                    })
            try:
                playlist_title = (
                    (slot.get("series_name") if slot.get("group_kind") == "series" and slot.get("series_name") else slot.get("title"))
                    or "Audiobook"
                )
                cid = clients.yoto_client.save_mixed_playlist(
                    title=playlist_title,
                    tracks=entries,
                    card_id=yoto_card_id,
                    cover_url=cover_url,
                    description=f"Offline copy in progress ({done_count}/{total_tracks} tracks on card; rest streaming)",
                )
                if cid:
                    yoto_card_id = cid
                    slot["yoto_card_id"] = cid
                if offline_meta.get("tracks"):
                    for m_trk, inf in zip(offline_meta["tracks"], uploaded_infos):
                        m_trk["on_card"] = bool(inf)
                live_state["saved_count"] = done_count
                live_state["saved_at"] = time.time()
            except Exception as e:
                live_state["ok"] = False
                logger.debug("save_mixed_playlist batch notice for %s: %s", slot_id, e)

        conn = database.get_db_connection()
        try:
            for idx, trk in enumerate(tracks):
                track_idx = int(trk["track_idx"])
                t_book_id = str(trk.get("abs_item_id") or abs_item_id)
                start_sec = float(trk["start_time"])
                end_sec = float(trk["end_time"])
                trk_title = trk.get("title") or f"Track {track_idx}"
                sha = trk.get("yoto_sha256")
                icon_ref = get_or_upload_yoto_icon(t_book_id, track_idx=track_idx, token=abs_token)
                icon_stream_url = f"{server_url}/s/icon/{slot_id}/{track_idx}.png" if server_url else ""

                if sha:
                    uploaded_infos[idx] = {
                        "track_idx": track_idx,
                        "title": trk_title,
                        "sha": sha,
                        "duration": (end_sec - start_sec) / speed,
                        "icon": icon_ref,
                        "icon_url": icon_stream_url,
                    }
                    self._update_job(slot_id, done=idx + 1, progress=(idx + 1) / total_tracks)
                    continue

                b_sources, b_auth = self._build_sources_for_book(t_book_id, abs_token, sources_cache)
                if not b_sources:
                    raise RuntimeError(f"ABS item {t_book_id} has no readable audio sources")

                self._update_job(
                    slot_id,
                    status="encoding",
                    done=idx,
                    progress=idx / total_tracks,
                    current_track=track_idx,
                    current_title=trk_title,
                )
                logger.info(
                    "Offline [%s] (%s): encoding track %d/%d '%s' (%.0fs)",
                    slot_id,
                    submode,
                    idx + 1,
                    total_tracks,
                    trk_title,
                    end_sec - start_sec,
                )
                audio_buffer = bytearray()
                for chunk in streamer.stream_segment(b_sources, start_sec, end_sec, auth_header=b_auth, speed=speed):
                    audio_buffer.extend(chunk)

                expected_track_sec = max(0.1, (end_sec - start_sec) / speed)
                expected_bytes = streamer.seconds_to_bytes(expected_track_sec, 1.0)
                min_valid_bytes = max(512, int(expected_bytes * 0.85))
                if len(audio_buffer) < min_valid_bytes:
                    raise RuntimeError(
                        f"Encoded audio for track {track_idx} ('{trk_title}') is incomplete/corrupted "
                        f"({len(audio_buffer)} bytes vs expected ~{expected_bytes} bytes for {expected_track_sec:.1f}s)"
                    )

                if not clients.yoto_client:
                    raise RuntimeError("Yoto client is not configured")

                self._update_job(
                    slot_id,
                    status="uploading",
                    current_track=track_idx,
                    current_title=trk_title,
                )
                up_info = clients.yoto_client.upload_audio(
                    bytes(audio_buffer),
                    title=f"{slot.get('title', 'Book')} - {trk_title}",
                    on_phase=lambda ph, ti=track_idx, tt=trk_title: self._update_job(
                        slot_id, status=ph, current_track=ti, current_title=tt
                    ),
                )

                sha = up_info.get("sha")
                if not sha:
                    raise RuntimeError(f"Yoto upload failed to return sha for track {track_idx}")

                yoto_dur = float(up_info.get("duration") or 0.0)
                if yoto_dur > 0 and abs(yoto_dur - expected_track_sec) > max(15.0, expected_track_sec * 0.10):
                    raise RuntimeError(
                        f"Yoto transcoded duration mismatch for track {track_idx} ('{trk_title}'): "
                        f"Yoto={yoto_dur:.1f}s vs expected={expected_track_sec:.1f}s"
                    )

                with conn:
                    cur = conn.cursor()
                    cur.execute(
                        "UPDATE bridge_tracks SET yoto_sha256 = ? WHERE slot_id = ? AND track_idx = ?;",
                        (sha, slot_id, track_idx),
                    )

                uploaded_infos[idx] = {
                    "track_idx": track_idx,
                    "title": trk_title,
                    "sha": sha,
                    "duration": up_info.get("duration") or ((end_sec - start_sec) / speed),
                    "fileSize": up_info.get("fileSize") or len(audio_buffer),
                    "channels": up_info.get("channels") or "mono",
                    "format": up_info.get("format") or "mp3",
                    "icon": icon_ref,
                    "icon_url": icon_stream_url,
                }

                self._update_job(slot_id, done=idx + 1, progress=(idx + 1) / total_tracks)
                if (idx + 1) < total_tracks:
                    _save_live_batch(force=False)

            uploaded_tracks = [x for x in uploaded_infos if x is not None]
            mode_desc = (
                f"next {eff_hours:g}h window"
                if submode == "next"
                else ("whole series" if submode == "series" else "whole book")
            )
            playlist_title = (
                (slot.get("series_name") if (slot.get("group_kind") == "series" or submode == "series") and slot.get("series_name") else slot.get("title"))
                or "Audiobook"
            )
            saved_cid = clients.yoto_client.save_track_playlist(
                title=playlist_title,
                tracks=uploaded_tracks,
                card_id=yoto_card_id,
                cover_url=cover_url,
                description=f"Offline audiobook ({mode_desc}): {slot.get('title', '')} by {slot.get('author', '')}",
            )

            if offline_meta:
                offline_meta["status"] = "ready"
                offline_meta["made_at"] = time.time()
                for m_trk in offline_meta.get("tracks", []):
                    m_trk["on_card"] = True

            with conn:
                cur = conn.cursor()
                cur.execute(
                    """
                    UPDATE bridge_slots
                    SET mode = 'offline', offline_mode = ?, offline_hours = ?, offline_status = 'ready',
                        offline_progress = 1.0, offline_meta = ?, yoto_card_id = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE slot_id = ?;
                    """,
                    (
                        submode,
                        eff_hours,
                        json.dumps(offline_meta) if offline_meta else "{}",
                        saved_cid or yoto_card_id or "",
                        slot_id,
                    ),
                )

            self._update_job(slot_id, status="ready", progress=1.0, done=total_tracks)
            logger.info("Successfully finished offline transcode (%s) and upload for slot %s (cardId: %s)", submode, slot_id, saved_cid)

        except Exception as e:
            logger.error("Error during offline transcode/upload for slot %s: %s", slot_id, e)
            self._update_job(slot_id, status="failed", error=str(e))
        finally:
            conn.close()

    def _update_job(self, slot_id: str, **kwargs):
        with self._lock:
            if slot_id in self._jobs:
                self._jobs[slot_id].update(kwargs)
                self._jobs[slot_id]["updated_at"] = time.time()

    # ==========================================================================
    # Offline Copy MQTT Position Sync -> Audiobookshelf & Whole-Copy Auto-Jump
    # ==========================================================================

    def on_mqtt_event(self, device_id: str, ev: Dict[str, Any]):
        """
        Syncs playback progress from a Yoto Player playing an offline ('yoto:#sha256') card
        back to Audiobookshelf (or local own_place progress), and auto-jumps whole-book copies
        to the listener's saved ABS position on first play.
        """
        card_id = str(ev.get("cardId") or "").strip()
        if not card_id or card_id.lower() in ("none", "null") or ev.get("position") is None:
            return

        mgr = get_bridge_manager()
        slot = None
        for s in (mgr.list_slots() or []):
            if str(s.get("yoto_card_id") or "").strip() == card_id and s.get("mode") == "offline":
                slot = s
                break
        if not slot:
            return
        if not slot.get("abs_item_id"):
            first_trk_item = next((str(t.get("abs_item_id") or "").strip() for t in (slot.get("tracks") or []) if str(t.get("abs_item_id") or "").strip()), "")
            if not first_trk_item:
                return
            slot["abs_item_id"] = first_trk_item

        raw_meta = slot.get("offline_meta")
        meta: Dict[str, Any] = {}
        if isinstance(raw_meta, str) and raw_meta:
            try:
                meta = json.loads(raw_meta)
            except Exception:
                meta = {}
        elif isinstance(raw_meta, dict):
            meta = dict(raw_meta)

        raw_key = str(ev.get("chapterKey") or ev.get("trackKey") or "").strip()
        track_idx = 1
        if raw_key.isdigit():
            track_idx = int(raw_key)

        tracks = slot.get("tracks") or []
        trk = next((t for t in tracks if int(t.get("track_idx") or 0) == track_idx), None)
        if not trk:
            return
        # If the track is still streaming (not yet uploaded to the card), live PlaybackManager handles it
        if not trk.get("yoto_sha256"):
            return

        speed = max(0.5, float(meta.get("speed") or slot.get("speed") or 1.0))
        t_start = float(trk.get("start_time") or 0.0)
        t_end = float(trk.get("end_time") or t_start)
        t_item_id = str(trk.get("abs_item_id") or slot.get("abs_item_id") or "")
        t_book_dur = float(trk.get("book_duration") or slot.get("total_duration") or t_end)
        pos = t_start + min(max(0.0, float(ev["position"])) * speed, max(0.0, t_end - t_start))
        status = str(ev.get("playbackStatus") or "").lower()

        # Check initial resume jump on first play:
        # When a whole-book copy or a 5-hour rolling window with 1.5h lookback starts playing at Track 1
        # (earlier than the saved ABS resume position), auto-jump the player once while online to the saved
        # resume track/offset. Once the initial jump has been sent (or if the user twists the left knob back
        # into the 1.5h lookback tracks to rewind after bedtime), mark `passed_resume = True` so rewinding works freely!
        resume_info = meta.get("resume")
        if resume_info and not meta.get("passed_resume"):
            r_item = str(resume_info.get("item_id") or slot["abs_item_id"])
            r_pos = float(resume_info.get("pos") or 0.0)
            order = meta.get("books") or [r_item]
            cur_ord = order.index(t_item_id) if t_item_id in order else 0
            res_ord = order.index(r_item) if r_item in order else 0
            if (cur_ord, pos) < (res_ord, r_pos - 1.0):
                if track_idx == 1 and float(ev.get("position") or 0.0) < 15.0 and not meta.get("jump_sent"):
                    if status == "playing":
                        target_trk = next(
                            (
                                t for t in tracks
                                if str(t.get("abs_item_id") or slot["abs_item_id"]) == r_item
                                and float(t.get("start_time") or 0) <= r_pos < float(t.get("end_time") or 0)
                            ),
                            None,
                        )
                        mqtt = get_mqtt_bridge()
                        if target_trk and mqtt.is_connected:
                            meta["jump_sent"] = True
                            meta["passed_resume"] = True
                            self._save_slot_offline_meta(slot["slot_id"], meta)
                            target_sec = int(max(0.0, (r_pos - float(target_trk["start_time"])) / speed))
                            mqtt.play(device_id, card_id, seconds_in=target_sec, track=int(target_trk["track_idx"]))
                    return
                # User intentionally chose or rewound to an earlier lookback track -> allow playback and sync!
                meta["passed_resume"] = True
            else:
                meta["passed_resume"] = True

        last_attempt = float(meta.get("last_write_attempt") or 0.0)
        due = (time.time() - last_attempt) >= OFFLINE_SYNC_INTERVAL
        if status not in ("paused", "stopped") and not due:
            return

        meta["last_write_attempt"] = time.time()
        meta["last_pos"] = pos
        meta["device_id"] = device_id
        abs_token = self._resolve_slot_abs_token(slot)

        # If listener crossed into a later book on a multi-book offline card, mark earlier book(s) finished
        order = meta.get("books") or [t_item_id]
        cur_item = str(meta.get("cur_item") or order[0])
        if t_item_id != cur_item and t_item_id in order and cur_item in order and order.index(t_item_id) > order.index(cur_item):
            for done_id in order[order.index(cur_item):order.index(t_item_id)]:
                done_dur = next(
                    (float(t.get("book_duration") or 0) for t in tracks if str(t.get("abs_item_id") or "") == done_id),
                    0.0,
                )
                patch_effective_item_progress(slot, done_id, done_dur, done_dur, is_finished=True, abs_token=abs_token)
            meta["cur_item"] = t_item_id

        # Anti-backwards guard: never overwrite if phone moved further since our last write
        prog = get_effective_item_progress(slot, t_item_id, abs_token=abs_token)
        abs_pos = float(prog.get("currentTime") or 0.0)
        newer = (float(prog.get("lastUpdate") or 0.0) / 1000.0) > (float(meta.get("last_write_wall") or 0.0) + 2.0)
        if newer and abs_pos > pos + 2.0:
            self._save_slot_offline_meta(slot["slot_id"], meta)
            return

        if abs(abs_pos - pos) >= 1.0:
            is_fin = bool(t_book_dur > 0 and pos >= t_book_dur - min(30.0, 0.02 * t_book_dur))
            patch_effective_item_progress(slot, t_item_id, pos, t_book_dur, is_finished=is_fin, abs_token=abs_token)
            meta["last_written"] = pos
            meta["last_write_wall"] = time.time()

        self._save_slot_offline_meta(slot["slot_id"], meta)

    def _save_slot_offline_meta(self, slot_id: str, meta: Dict[str, Any]):
        conn = database.get_db_connection()
        try:
            with conn:
                conn.execute(
                    "UPDATE bridge_slots SET offline_meta = ?, updated_at = CURRENT_TIMESTAMP WHERE slot_id = ?;",
                    (json.dumps(meta), slot_id),
                )
        except Exception as e:
            logger.debug("_save_slot_offline_meta notice for %s: %s", slot_id, e)
        finally:
            conn.close()


_offline_manager: Optional[OfflineTranscodeManager] = None


def get_offline_manager() -> OfflineTranscodeManager:
    global _offline_manager
    if _offline_manager is None:
        _offline_manager = OfflineTranscodeManager()
    return _offline_manager


def prune_evicted_profile_playlists() -> Dict[str, Any]:
    """
    Automated LRU Cache Pruner for ephemeral profile playlists.
    Follows profile offline_retention_limit and atomic whole-series recent_tier curation rules.
    Strictly preserves:
    - Pinned playlists (is_pinned = 1 or is_protected = 1)
    - Permanent MYO / fleet cards (kind = 'permanent_myo')
    - Active primary profile cards (kind = 'profile_primary')
    - Custom user-uploaded playlists
    - Sister books belonging to any series present in the profile's retained recent tier
    """
    pruned_count = 0
    pruned_items = []

    profiles = database.get_all_profiles()

    book_to_series: Dict[str, set[str]] = {}
    series_to_books: Dict[str, set[str]] = {}
    try:
        for it in database.get_catalog_matrix().get("items", []):
            mid = it.get("id")
            if not mid:
                continue
            s_names = set()
            if (it.get("series") or "").strip():
                s_names.add(it["series"].strip().lower())
            for sl in (it.get("series_list") or []):
                sn = (sl.get("name") or "").strip().lower()
                if sn:
                    s_names.add(sn)
            if s_names:
                book_to_series[mid] = s_names
                for sn in s_names:
                    series_to_books.setdefault(sn, set()).add(mid)
    except Exception as e:
        logger.debug("Catalog series lookup notice in pruner: %s", e)

    protected_yoto_card_ids: set[str] = {
        str(p_cfg.get("primary_card_id") or "").strip()
        for p_cfg in profiles.values()
        if str(p_cfg.get("primary_card_id") or "").strip()
    }

    conn = database.get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT slot_id, abs_item_id, series_name, kind, is_pinned, is_protected, yoto_card_id FROM bridge_slots;")
        for r in cur.fetchall():
            r_cid = str(r["yoto_card_id"] or "").strip()
            if r_cid and (r["kind"] not in ("profile_cache", "ephemeral_offline") or r["is_pinned"] or r["is_protected"]):
                protected_yoto_card_ids.add(r_cid)
            r_mid = r["abs_item_id"]
            r_ser = (r["series_name"] or "").strip().lower()
            if r_mid and r_ser:
                book_to_series.setdefault(r_mid, set()).add(r_ser)
                series_to_books.setdefault(r_ser, set()).add(r_mid)

        for p_id, p_cfg in profiles.items():
            retention_limit = int(p_cfg.get("offline_retention_limit", 8) or 8)
            playback_history = database.get_playback_history(p_id)

            sorted_recent = sorted(playback_history.items(), key=lambda kv: kv[1], reverse=True)
            base_recent_ids = {book_id for book_id, _ in sorted_recent[:retention_limit]}

            cur.execute("""
                SELECT abs_item_id FROM bridge_slots
                WHERE owner_profile_id = ? AND kind = 'profile_primary';
            """, (p_id,))
            for prow in cur.fetchall():
                if prow["abs_item_id"]:
                    base_recent_ids.add(prow["abs_item_id"])

            retained_series: set[str] = set()
            for bid in base_recent_ids:
                retained_series.update(book_to_series.get(bid, set()))
            retained_ids = set(base_recent_ids)
            for sn in retained_series:
                retained_ids.update(series_to_books.get(sn, set()))

            cur.execute("""
                SELECT slot_id, abs_item_id, title, series_name, is_pinned, is_protected, kind, yoto_card_id
                FROM bridge_slots
                WHERE owner_profile_id = ? AND kind IN ('profile_cache', 'ephemeral_offline');
            """, (p_id,))
            candidate_slots = cur.fetchall()

            for s in candidate_slots:
                slot_id = s["slot_id"]
                book_id = s["abs_item_id"]
                yoto_cid = s["yoto_card_id"] or slot_id
                s_series = (s["series_name"] or "").strip().lower()

                if s["is_pinned"] or s["is_protected"]:
                    continue

                if book_id in retained_ids or (s_series and s_series in retained_series):
                    continue

                logger.info("Pruning evicted offline playlist: %s (slot: %s, profile: %s)", s["title"], slot_id, p_id)
                if clients.yoto_client and yoto_cid not in protected_yoto_card_ids:
                    try:
                        clients.yoto_client.delete_playlist(yoto_cid)
                    except Exception as e:
                        logger.warning("Failed to delete Yoto Cloud playlist %s: %s", yoto_cid, e)

                with conn:
                    sub_cur = conn.cursor()
                    sub_cur.execute("DELETE FROM bridge_slots WHERE slot_id = ?;", (slot_id,))
                    sub_cur.execute("DELETE FROM bridge_tracks WHERE slot_id = ?;", (slot_id,))

                pruned_count += 1
                pruned_items.append({"slot_id": slot_id, "title": s["title"], "profile_id": p_id})

    finally:
        conn.close()

    return {
        "pruned_count": pruned_count,
        "pruned_items": pruned_items,
        "timestamp": time.time()
    }

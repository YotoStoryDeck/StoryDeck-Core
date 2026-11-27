"""
StoryDeck Core Bridge Manager.
Orchestrates bridge slots, tracks, Audiobookshelf metadata parsing,
and synchronized Yoto Cloud playlist updates.
"""
from __future__ import annotations
import json
import logging
from typing import Dict, Any, List, Optional, Tuple
import database
import clients
from .chapters import chapter_spans, make_layout, DEFAULT_PART_SECONDS
from .quiet import get_quiet_spots
from .series import get_effective_item_progress, get_series_books
from .streamer import AudioTrackSource
from .manager_cards import BridgeCardAssignmentMixin

logger = logging.getLogger("storydeck.bridge")


class BridgeManager(BridgeCardAssignmentMixin):
    def __init__(self):
        self.quiet_spots = get_quiet_spots()

    def get_slot(self, slot_id: str) -> Optional[Dict[str, Any]]:
        """Retrieves a bridge card slot by slot_id."""
        conn = database.get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT * FROM bridge_slots WHERE slot_id = ?;", (slot_id,))
            row = cur.fetchone()
            if not row:
                return None
            slot = dict(row)
            slot["tracks"] = self.get_slot_tracks(slot_id)
            return slot
        finally:
            conn.close()

    def list_slots(self) -> List[Dict[str, Any]]:
        """Lists all registered bridge slots (profile cards, fleet cards, and virtual slots)."""
        conn = database.get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT * FROM bridge_slots ORDER BY updated_at DESC;")
            rows = cur.fetchall()
            results = []
            for r in rows:
                s = dict(r)
                s["tracks"] = self.get_slot_tracks(s["slot_id"])
                results.append(s)
            return results
        finally:
            conn.close()

    def get_slot_tracks(self, slot_id: str) -> List[Dict[str, Any]]:
        """Retrieves track layout for a slot ordered by track_idx."""
        conn = database.get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT id, slot_id, track_idx, abs_item_id, book_title, book_duration,
                       title, start_time, end_time, duration, icon_hash, yoto_sha256, stream_url
                FROM bridge_tracks
                WHERE slot_id = ?
                ORDER BY track_idx ASC;
            """, (slot_id,))
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()

    def get_track(self, slot_id: str, track_idx: int) -> Optional[Dict[str, Any]]:
        """Retrieves a specific track for a slot."""
        conn = database.get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT id, slot_id, track_idx, abs_item_id, book_title, book_duration,
                       title, start_time, end_time, duration, icon_hash, yoto_sha256, stream_url
                FROM bridge_tracks
                WHERE slot_id = ? AND track_idx = ?;
            """, (slot_id, track_idx))
            row = cur.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def set_slot_speed(self, slot_id: str, speed: float) -> bool:
        """Sets playback speed for a card slot (1.0 = normal)."""
        clamped_speed = max(0.5, min(2.0, float(speed or 1.0)))
        conn = database.get_db_connection()
        try:
            with conn:
                cur = conn.cursor()
                cur.execute("UPDATE bridge_slots SET speed = ?, updated_at = CURRENT_TIMESTAMP WHERE slot_id = ?;", (clamped_speed, slot_id))
                return cur.rowcount > 0
        finally:
            conn.close()

    def set_slot_own_place(self, slot_id: str, own_place: bool) -> bool:
        """Toggles independent listening place for a card slot."""
        val = 1 if own_place else 0
        conn = database.get_db_connection()
        try:
            with conn:
                cur = conn.cursor()
                cur.execute("UPDATE bridge_slots SET own_place = ?, updated_at = CURRENT_TIMESTAMP WHERE slot_id = ?;", (val, slot_id))
                return cur.rowcount > 0
        finally:
            conn.close()

    def set_slot_pin(self, slot_id: str, is_pinned: bool) -> bool:
        """Pins or unpins a slot to protect it from automated LRU cleaner."""
        val = 1 if is_pinned else 0
        conn = database.get_db_connection()
        try:
            with conn:
                cur = conn.cursor()
                cur.execute("UPDATE bridge_slots SET is_pinned = ?, is_protected = ?, updated_at = CURRENT_TIMESTAMP WHERE slot_id = ?;", (val, val, slot_id))
                return cur.rowcount > 0
        finally:
            conn.close()

    def delete_slot(self, slot_id: str) -> bool:
        """Removes a card slot and cascades its tracks."""
        conn = database.get_db_connection()
        try:
            with conn:
                cur = conn.cursor()
                cur.execute("DELETE FROM bridge_slots WHERE slot_id = ?;", (slot_id,))
                return cur.rowcount > 0
        finally:
            conn.close()

    def _build_abs_audio_sources(self, abs_item_id: str, item_data: Optional[Dict[str, Any]],
                                 abs_token_override: Optional[str] = None) -> Tuple[List[AudioTrackSource], str]:
        """Builds AudioTrackSource spans and auth header for an Audiobookshelf item (supports both media.tracks and media.audioFiles)."""
        media = (item_data or {}).get("media") or {}
        raw_tracks = media.get("tracks") or []
        audio_files = media.get("audioFiles") or []
        abs_token = (abs_token_override or "").strip()
        abs_base = ""
        if clients.abs_client:
            if not abs_token:
                abs_token = getattr(clients.abs_client, "token", "") or getattr(clients.abs_client, "api_key", "") or ""
            abs_base = (getattr(clients.abs_client, "base_url", "") or "").rstrip("/")

        sources: List[AudioTrackSource] = []
        c_offset = 0.0
        for entry in (raw_tracks if raw_tracks else audio_files):
            f_dur = float(entry.get("duration") or 0.0)
            t_start = float(entry["startOffset"]) if entry.get("startOffset") is not None else c_offset
            local_path = entry.get("local_path") or ""
            ino = entry.get("ino") or entry.get("id") or (entry.get("metadata") or {}).get("ino") or ""
            content_url = str(entry.get("contentUrl") or "").strip()
            if local_path:
                url = local_path
            elif abs_base and ino:
                # Prefer /api/items/<id>/file/<ino> because ABS serves it directly over HTTP 206 with Bearer auth,
                # whereas /s/item/... contentUrl requires an active HLS/session token on newer ABS versions.
                url = f"{abs_base}/api/items/{abs_item_id}/file/{ino}"
            elif content_url:
                url = content_url if content_url.startswith("http") else f"{abs_base}{content_url if content_url.startswith('/') else '/' + content_url}"
            else:
                url = ""
            if url and f_dur > 0:
                sources.append(AudioTrackSource(url=url, start=t_start, duration=f_dur))
            c_offset = t_start + f_dur

        # Always authenticate ffmpeg audio file fetches with the master API key if available (or valid user key)
        # so an expired/revoked user key never causes ffmpeg to get 401 Unauthorized on audio streams.
        master_tok = (getattr(clients.abs_client, "api_key", "") or getattr(clients.abs_client, "token", "") or "").strip() if clients.abs_client else ""
        eff_stream_tok = master_tok or abs_token
        auth_hdr = f"Bearer {eff_stream_tok}" if eff_stream_tok else ""
        return sources, auth_hdr

    def assign_abs_book_to_slot(self, slot_id: str, abs_item_id: str, title: str = "",
                                author: str = "", series_id: str = "", series_name: str = "",
                                group_kind: str = "book", owner_profile_id: str = "",
                                kind: str = "permanent_myo", mode: str = "streaming",
                                base_url: str = "", scan_quiet_sync: bool = False,
                                owner_user_id: str = "", abs_token: Optional[str] = None) -> Dict[str, Any]:
        """
        Assigns an Audiobookshelf book to a card slot, generates chapter tracks,
        and saves layout in SQLite.
        """
        # 1. Fetch item from ABS client if available
        item_data = None
        if clients.abs_client:
            try:
                item_data = clients.abs_client.get_item(abs_item_id, token=abs_token)
            except Exception as e:
                logger.warning("Failed to fetch ABS item %s metadata: %s", abs_item_id, e)

        item_media = (item_data or {}).get("media", {})
        item_meta = item_media.get("metadata", {})
        book_title = title or item_meta.get("title") or (item_data or {}).get("name") or "Audiobook"
        book_author = author or item_meta.get("authorName") or item_meta.get("author") or ""
        total_duration = float(item_media.get("duration") or 0.0)
        raw_chapters = item_media.get("chapters") or []

        resolved_series_name = (series_name or "").strip()
        if not resolved_series_name:
            raw_ser = item_meta.get("seriesName") or item_meta.get("series") or ""
            if isinstance(raw_ser, list) and raw_ser:
                first = raw_ser[0]
                raw_ser = first.get("name", "") if isinstance(first, dict) else str(first)
            elif isinstance(raw_ser, dict):
                raw_ser = raw_ser.get("name", "")
            resolved_series_name = str(raw_ser or "").split(",")[0].split("#")[0].strip()
        if not resolved_series_name:
            db_item = database.get_media_item_by_id(abs_item_id)
            if db_item and db_item.get("raw_metadata"):
                try:
                    raw_meta = json.loads(db_item["raw_metadata"])
                    resolved_series_name = (raw_meta.get("series") or "").strip()
                except Exception:
                    pass

        # 2. Build chapter spans (with pause-snapped internal cuts)
        planned_cuts: List[float] = []
        if total_duration > 0:
            chapter_spans(
                raw_chapters,
                total_duration,
                part_seconds=DEFAULT_PART_SECONDS,
                snap=lambda t: (planned_cuts.append(t), t)[1]
            )

        missing_cuts = self.quiet_spots.missing(abs_item_id, planned_cuts) if planned_cuts else []
        sources, auth_hdr = self._build_abs_audio_sources(abs_item_id, item_data, abs_token_override=abs_token) if missing_cuts else ([], "")
        if scan_quiet_sync and missing_cuts and sources:
            self.quiet_spots.scan_windows(abs_item_id, planned_cuts, sources, auth_header=auth_hdr, budget=20.0)

        snap = self.quiet_spots.snapper(abs_item_id)
        prog = get_effective_item_progress({"slot_id": slot_id, "own_place": False}, abs_item_id, abs_token=abs_token)
        cur_pos = 0.0 if prog.get("isFinished") else float(prog.get("currentTime") or 0.0)
        if total_duration > 0 and cur_pos >= total_duration - 10.0:
            cur_pos = 0.0

        books_for_layout = [(abs_item_id, book_title, total_duration, raw_chapters, cur_pos)]
        snaps_map = {abs_item_id: snap}

        # If this is a series card (group_kind == 'series'), chain following books in the series up to 99 tracks
        if group_kind == "series" and resolved_series_name:
            series_list = get_series_books(resolved_series_name, series_id)
            s_ids = [b.item_id for b in series_list]
            if abs_item_id in s_ids:
                for fb in series_list[s_ids.index(abs_item_id) + 1:][:10]:
                    fb_ch = []
                    fb_dur = fb.duration
                    if clients.abs_client:
                        try:
                            fb_item = clients.abs_client.get_item(fb.item_id, token=abs_token) or {}
                            fb_media = fb_item.get("media") or {}
                            fb_ch = fb_media.get("chapters") or []
                            fb_dur = float(fb_media.get("duration") or fb_dur or 0.0)
                        except Exception:
                            pass
                    if fb_dur > 0:
                        books_for_layout.append((fb.item_id, fb.title, fb_dur, fb_ch, 0.0))
                        snaps_map[fb.item_id] = self.quiet_spots.snapper(fb.item_id)

        segments = make_layout(books_for_layout, part_seconds=DEFAULT_PART_SECONDS, snaps=snaps_map)
        multi_book = len({seg.item_id for seg in segments}) > 1

        clean_owner_user = (owner_user_id or "").strip()
        owner_val = None if clean_owner_user else (owner_profile_id.strip() if owner_profile_id and owner_profile_id.strip() else None)
        is_protected_val = 0 if kind in ("profile_cache", "ephemeral_offline", "user_session") else 1

        # 3. Upsert slot record
        conn = database.get_db_connection()
        try:
            with conn:
                cur = conn.cursor()
                cur.execute("""
                    INSERT INTO bridge_slots (
                        slot_id, abs_item_id, owner_profile_id, owner_user_id, title, author,
                        series_id, series_name, group_kind, kind, source, is_protected,
                        total_duration, mode, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'storydeck', ?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(slot_id) DO UPDATE SET
                        abs_item_id = excluded.abs_item_id,
                        owner_profile_id = CASE WHEN excluded.owner_user_id != '' THEN NULL WHEN excluded.owner_profile_id IS NOT NULL THEN excluded.owner_profile_id ELSE bridge_slots.owner_profile_id END,
                        owner_user_id = excluded.owner_user_id,
                        title = excluded.title,
                        author = excluded.author,
                        series_id = excluded.series_id,
                        series_name = excluded.series_name,
                        group_kind = excluded.group_kind,
                        kind = excluded.kind,
                        is_protected = CASE WHEN excluded.kind != bridge_slots.kind THEN excluded.is_protected ELSE bridge_slots.is_protected END,
                        total_duration = excluded.total_duration,
                        mode = excluded.mode,
                        updated_at = CURRENT_TIMESTAMP;
                """, (
                    slot_id, abs_item_id, owner_val, clean_owner_user, book_title, book_author,
                    series_id, resolved_series_name, group_kind, kind, is_protected_val, total_duration, mode
                ))

                # Rebuild tracks (using 99-track sliding window & series chaining from make_layout)
                cur.execute("DELETE FROM bridge_tracks WHERE slot_id = ?;", (slot_id,))
                server_url = base_url.rstrip("/") if base_url else ""
                for idx, seg in enumerate(segments, 1):
                    stream_url = f"{server_url}/s/audio/{slot_id}/{idx}" if server_url else f"/s/audio/{slot_id}/{idx}"
                    t_label = f"{seg.book}: {seg.title}" if multi_book else seg.title
                    cur.execute("""
                        INSERT INTO bridge_tracks (
                            slot_id, track_idx, abs_item_id, book_title, book_duration,
                            title, start_time, end_time, duration, stream_url
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """, (
                        slot_id, idx, seg.item_id, seg.book, seg.book_duration,
                        t_label, seg.start, seg.end, max(0.1, seg.end - seg.start), stream_url
                    ))

            if not scan_quiet_sync and missing_cuts and sources:
                def _refresh_snapped_tracks():
                    cur_slot = self.get_slot(slot_id)
                    if not cur_slot or cur_slot.get("abs_item_id") != abs_item_id:
                        return
                    if any(t.get("yoto_sha256") for t in cur_slot.get("tracks", [])):
                        return
                    new_snap = self.quiet_spots.snapper(abs_item_id)
                    if not new_snap:
                        return
                    new_snaps_map = dict(snaps_map, **{abs_item_id: new_snap})
                    new_segs = make_layout(books_for_layout, part_seconds=DEFAULT_PART_SECONDS, snaps=new_snaps_map)
                    rconn = database.get_db_connection()
                    try:
                        with rconn:
                            rcur = rconn.cursor()
                            rcur.execute("DELETE FROM bridge_tracks WHERE slot_id = ?;", (slot_id,))
                            for ridx, rseg in enumerate(new_segs, 1):
                                rstream = f"{server_url}/s/audio/{slot_id}/{ridx}" if server_url else f"/s/audio/{slot_id}/{ridx}"
                                rlabel = f"{rseg.book}: {rseg.title}" if multi_book else rseg.title
                                rcur.execute("""
                                    INSERT INTO bridge_tracks (
                                        slot_id, track_idx, abs_item_id, book_title, book_duration,
                                        title, start_time, end_time, duration, stream_url
                                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                                """, (
                                    slot_id, ridx, rseg.item_id, rseg.book, rseg.book_duration,
                                    rlabel, rseg.start, rseg.end, max(0.1, rseg.end - rseg.start), rstream
                                ))
                    finally:
                        rconn.close()

                self.quiet_spots.schedule_background_scan(
                    abs_item_id, planned_cuts, sources, auth_header=auth_hdr, on_complete=_refresh_snapped_tracks
                )

            slot_out = self.get_slot(slot_id) or {}
            if server_url and slot_out.get("tracks"):
                for t in slot_out["tracks"]:
                    t["icon_url"] = f"{server_url}/s/icon/{slot_id}/{t['track_idx']}.png"
            return slot_out
        finally:
            conn.close()



_bridge_manager: Optional[BridgeManager] = None

def get_bridge_manager() -> BridgeManager:
    global _bridge_manager
    if _bridge_manager is None:
        _bridge_manager = BridgeManager()
    return _bridge_manager

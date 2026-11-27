"""
Profile, Household User & Fleet Card Assignment Mixin for BridgeManager.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import clients
import database
from .series import get_series_books

logger = logging.getLogger("storydeck.bridge.manager.cards")


class BridgeCardAssignmentMixin:
    """Profile primary card, personal user card, and fleet card assignment orchestration."""

    def update_profile_primary_card(self, profile_id: str, abs_item_id: str,
                                   mode_override: Optional[str] = None,
                                   base_url: str = "",
                                   group_kind: str = "book",
                                   series_name: str = "") -> Dict[str, Any]:
        """
        Dynamically repoints a profile's primary ABS card to the selected book (or series).
        Follows profile default playback mode unless mode_override is specified.
        When switching away from an offline book, archives it into an ephemeral
        'profile_cache' slot managed by the automated LRU cache pruner.
        """
        profile = database.get_profile_by_id(profile_id)
        if not profile:
            raise ValueError(f"Profile '{profile_id}' not found.")

        raw_mode = (mode_override or "").strip().lower()
        if not raw_mode or raw_mode == "default":
            raw_mode = (profile.get("default_playback_mode") or "streaming").strip().lower()
        mode = "offline" if raw_mode.startswith("offline") or raw_mode in ("next", "book", "series") else "streaming"

        slot_id = f"profile_{profile_id}"
        existing = self.get_slot(slot_id)
        explicit_primary_cid = str(profile.get("primary_card_id") or "").strip()
        primary_card_id = explicit_primary_cid

        if existing and existing.get("abs_item_id") and existing.get("abs_item_id") != abs_item_id and existing.get("mode") == "offline":
            old_item_id = existing["abs_item_id"]
            cache_slot_id = f"cache_{profile_id}_{old_item_id}"
            old_pinned = int(existing.get("is_pinned") or 0)
            old_protected = 1 if old_pinned else 0
            old_yoto_cid = (existing.get("yoto_card_id") or "").strip()

            # Preserve the old book's dedicated Yoto Cloud playlist for player flash retention.
            # If the profile uses an explicit physical card (primary_card_id), keep any distinct
            # cached yoto_card_id for the old book so it remains retained on Yoto Cloud and player flash.
            archived_yoto_cid = old_yoto_cid
            if explicit_primary_cid and old_yoto_cid == explicit_primary_cid:
                cur_cache = self.get_slot(cache_slot_id)
                archived_yoto_cid = (cur_cache.get("yoto_card_id") if cur_cache else "") or ""

            conn = database.get_db_connection()
            try:
                with conn:
                    cur = conn.cursor()
                    cur.execute("""
                        INSERT INTO bridge_slots (
                            slot_id, abs_item_id, yoto_card_id, owner_profile_id, title, author,
                            series_id, series_name, group_kind, kind, source, is_protected, is_pinned,
                            total_duration, speed, own_place, mode, offline_status, offline_progress,
                            icon_hash, last_played_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'profile_cache', ?, ?, ?, ?, ?, ?, 'offline', ?, ?, ?, ?, CURRENT_TIMESTAMP)
                        ON CONFLICT(slot_id) DO UPDATE SET
                            abs_item_id = excluded.abs_item_id,
                            yoto_card_id = CASE WHEN excluded.yoto_card_id != '' THEN excluded.yoto_card_id ELSE bridge_slots.yoto_card_id END,
                            owner_profile_id = excluded.owner_profile_id,
                            title = excluded.title,
                            author = excluded.author,
                            series_id = excluded.series_id,
                            series_name = excluded.series_name,
                            group_kind = excluded.group_kind,
                            kind = 'profile_cache',
                            is_pinned = MAX(bridge_slots.is_pinned, excluded.is_pinned),
                            is_protected = MAX(bridge_slots.is_protected, excluded.is_protected),
                            total_duration = excluded.total_duration,
                            speed = excluded.speed,
                            own_place = excluded.own_place,
                            mode = 'offline',
                            offline_status = excluded.offline_status,
                            offline_progress = excluded.offline_progress,
                            icon_hash = excluded.icon_hash,
                            last_played_at = excluded.last_played_at,
                            updated_at = CURRENT_TIMESTAMP;
                    """, (
                        cache_slot_id,
                        old_item_id,
                        archived_yoto_cid,
                        profile_id,
                        existing.get("title") or "Audiobook",
                        existing.get("author") or "",
                        existing.get("series_id") or "",
                        existing.get("series_name") or "",
                        existing.get("group_kind") or "book",
                        existing.get("source") or "storydeck",
                        old_protected,
                        old_pinned,
                        float(existing.get("total_duration") or 0.0),
                        float(existing.get("speed") or 1.0),
                        int(existing.get("own_place") or 0),
                        existing.get("offline_status") or "ready",
                        float(existing.get("offline_progress") or 1.0),
                        existing.get("icon_hash") or "",
                        existing.get("last_played_at") or "",
                    ))

                    cur.execute("DELETE FROM bridge_tracks WHERE slot_id = ?;", (cache_slot_id,))
                    for trk in existing.get("tracks", []):
                        cur.execute("""
                            INSERT INTO bridge_tracks (
                                slot_id, track_idx, abs_item_id, book_title, book_duration,
                                title, start_time, end_time, duration, icon_hash, yoto_sha256, stream_url
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                        """, (
                            cache_slot_id,
                            trk["track_idx"],
                            trk.get("abs_item_id") or old_item_id,
                            trk.get("book_title") or existing.get("title") or "Audiobook",
                            float(trk.get("book_duration") or existing.get("total_duration") or 0.0),
                            trk["title"],
                            trk["start_time"],
                            trk["end_time"],
                            trk["duration"],
                            trk.get("icon_hash") or "",
                            trk.get("yoto_sha256") or "",
                            trk.get("stream_url") or "",
                        ))

                    # If an explicit physical card is bound, keep it on the profile slot so it is updated in-place.
                    # Otherwise, clear slot_id's yoto_card_id so the new book gets its own dedicated cardId.
                    if explicit_primary_cid:
                        cur.execute("UPDATE bridge_slots SET yoto_card_id = ? WHERE slot_id = ?;", (explicit_primary_cid, slot_id))
                    else:
                        cur.execute("UPDATE bridge_slots SET yoto_card_id = '' WHERE slot_id = ?;", (slot_id,))
            finally:
                conn.close()

        warm_slot = self.get_slot(f"cache_{profile_id}_{abs_item_id}")

        slot = self.assign_abs_book_to_slot(
            slot_id=slot_id,
            abs_item_id=abs_item_id,
            series_name=series_name,
            group_kind=group_kind or "book",
            owner_profile_id=profile_id,
            kind="profile_primary",
            mode=mode,
            base_url=base_url
        )

        # Restore dedicated cardId if previously cached and no physical primary card is forced
        if warm_slot and warm_slot.get("yoto_card_id") and not explicit_primary_cid:
            wconn = database.get_db_connection()
            try:
                with wconn:
                    wconn.execute("UPDATE bridge_slots SET yoto_card_id = ? WHERE slot_id = ?;", (warm_slot["yoto_card_id"], slot_id))
            finally:
                wconn.close()
            slot["yoto_card_id"] = warm_slot["yoto_card_id"]

        if mode == "offline" and warm_slot and warm_slot.get("mode") == "offline":
            warm_sha_map = {
                (str(trk.get("abs_item_id") or abs_item_id), round(float(trk.get("start_time") or 0), 1), round(float(trk.get("end_time") or 0), 1)): trk.get("yoto_sha256")
                for trk in warm_slot.get("tracks", [])
                if trk.get("yoto_sha256")
            }
            if not warm_sha_map:
                warm_sha_map = {
                    trk.get("track_idx"): trk.get("yoto_sha256")
                    for trk in warm_slot.get("tracks", [])
                    if trk.get("yoto_sha256")
                }
            if warm_sha_map:
                wconn = database.get_db_connection()
                try:
                    with wconn:
                        wcur = wconn.cursor()
                        for trk in slot.get("tracks", []):
                            key = (str(trk.get("abs_item_id") or abs_item_id), round(float(trk.get("start_time") or 0), 1), round(float(trk.get("end_time") or 0), 1))
                            sha = warm_sha_map.get(key) or warm_sha_map.get(trk.get("track_idx"))
                            if sha:
                                wcur.execute(
                                    "UPDATE bridge_tracks SET yoto_sha256 = ? WHERE slot_id = ? AND track_idx = ?;",
                                    (sha, slot_id, trk["track_idx"])
                                )
                finally:
                    wconn.close()
                slot = self.get_slot(slot_id) or slot

        # Record in media_playback_history for recent tier curation
        database.record_playback(abs_item_id, profile_id)
        return slot

    def update_user_primary_card(self, user_id: str, username: str, abs_item_id: str,
                                 title: str = "", author: str = "",
                                 mode_override: Optional[str] = None,
                                 base_url: str = "",
                                 group_kind: str = "book",
                                 series_name: str = "",
                                 abs_token: Optional[str] = None) -> Dict[str, Any]:
        """
        Creates or updates a signed-in user's dedicated personal card slot (`user_<user_id>`, kind='user_primary'):
        - Reuses the slot's persistent `yoto_card_id` in-place so a physical MYO card linked to it never needs relinking.
        - When switching away from an offline book, archives the old book's `yoto_sha256` track hashes into `cache_user_<user_id>_<old_item_id>`
          for instant warm SHA reuse when returning to that book.
        """
        raw_mode = (mode_override or "").strip().lower()
        mode = "offline" if (raw_mode.startswith("offline") or raw_mode in ("next", "book", "series", "5h", "hours")) else "streaming"
        slot_id = f"user_{user_id}"
        existing = self.get_slot(slot_id)
        persistent_yoto_cid = (existing.get("yoto_card_id") if existing else "") or ""

        if existing and existing.get("abs_item_id") and existing.get("abs_item_id") != abs_item_id and existing.get("mode") == "offline":
            old_item_id = existing["abs_item_id"]
            cache_slot_id = f"cache_user_{user_id}_{old_item_id}"
            old_pinned = int(existing.get("is_pinned") or 0)
            old_protected = 1 if old_pinned else 0
            conn = database.get_db_connection()
            try:
                with conn:
                    cur = conn.cursor()
                    cur.execute("""
                        INSERT INTO bridge_slots (
                            slot_id, abs_item_id, yoto_card_id, owner_user_id, title, author,
                            series_id, series_name, group_kind, kind, source, is_protected, is_pinned,
                            total_duration, speed, own_place, mode, offline_status, offline_progress,
                            icon_hash, last_played_at, updated_at
                        ) VALUES (?, ?, '', ?, ?, ?, ?, ?, ?, 'profile_cache', 'storydeck', ?, ?, ?, ?, ?, 'offline', ?, ?, ?, ?, CURRENT_TIMESTAMP)
                        ON CONFLICT(slot_id) DO UPDATE SET
                            abs_item_id = excluded.abs_item_id,
                            owner_user_id = excluded.owner_user_id,
                            title = excluded.title,
                            author = excluded.author,
                            series_id = excluded.series_id,
                            series_name = excluded.series_name,
                            group_kind = excluded.group_kind,
                            kind = 'profile_cache',
                            is_pinned = MAX(bridge_slots.is_pinned, excluded.is_pinned),
                            is_protected = MAX(bridge_slots.is_protected, excluded.is_protected),
                            total_duration = excluded.total_duration,
                            speed = excluded.speed,
                            own_place = excluded.own_place,
                            mode = 'offline',
                            offline_status = excluded.offline_status,
                            offline_progress = excluded.offline_progress,
                            icon_hash = excluded.icon_hash,
                            last_played_at = excluded.last_played_at,
                            updated_at = CURRENT_TIMESTAMP;
                    """, (
                        cache_slot_id,
                        old_item_id,
                        user_id,
                        existing.get("title") or "Audiobook",
                        existing.get("author") or "",
                        existing.get("series_id") or "",
                        existing.get("series_name") or "",
                        existing.get("group_kind") or "book",
                        old_protected,
                        old_pinned,
                        float(existing.get("total_duration") or 0.0),
                        float(existing.get("speed") or 1.0),
                        int(existing.get("own_place") or 0),
                        existing.get("offline_status") or "ready",
                        float(existing.get("offline_progress") or 1.0),
                        existing.get("icon_hash") or "",
                        existing.get("last_played_at") or "",
                    ))
                    cur.execute("DELETE FROM bridge_tracks WHERE slot_id = ?;", (cache_slot_id,))
                    for trk in existing.get("tracks", []):
                        cur.execute("""
                            INSERT INTO bridge_tracks (
                                slot_id, track_idx, abs_item_id, book_title, book_duration,
                                title, start_time, end_time, duration, icon_hash, yoto_sha256, stream_url
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                        """, (
                            cache_slot_id,
                            trk["track_idx"],
                            trk.get("abs_item_id") or old_item_id,
                            trk.get("book_title") or existing.get("title") or "Audiobook",
                            float(trk.get("book_duration") or existing.get("total_duration") or 0.0),
                            trk["title"],
                            trk["start_time"],
                            trk["end_time"],
                            trk["duration"],
                            trk.get("icon_hash") or "",
                            trk.get("yoto_sha256") or "",
                            trk.get("stream_url") or "",
                        ))
            finally:
                conn.close()

        warm_slot = self.get_slot(f"cache_user_{user_id}_{abs_item_id}")

        slot = self.assign_abs_book_to_slot(
            slot_id=slot_id,
            abs_item_id=abs_item_id,
            title=title,
            author=author,
            series_name=series_name,
            group_kind=group_kind or "book",
            owner_profile_id="",
            owner_user_id=user_id,
            kind="user_primary",
            mode=mode,
            base_url=base_url,
            abs_token=abs_token,
        )

        # Ensure default nickname and preserve persistent yoto_card_id
        default_nick = f"{username or user_id}'s Card"
        uconn = database.get_db_connection()
        try:
            with uconn:
                if persistent_yoto_cid and not slot.get("yoto_card_id"):
                    uconn.execute("UPDATE bridge_slots SET yoto_card_id = ? WHERE slot_id = ?;", (persistent_yoto_cid, slot_id))
                    slot["yoto_card_id"] = persistent_yoto_cid
                if not (slot.get("nickname") or "").strip():
                    uconn.execute("UPDATE bridge_slots SET nickname = ? WHERE slot_id = ?;", (default_nick, slot_id))
                    slot["nickname"] = default_nick
        finally:
            uconn.close()

        if mode == "offline" and warm_slot and warm_slot.get("mode") == "offline":
            warm_sha_map = {
                (str(trk.get("abs_item_id") or abs_item_id), round(float(trk.get("start_time") or 0), 1), round(float(trk.get("end_time") or 0), 1)): trk.get("yoto_sha256")
                for trk in warm_slot.get("tracks", [])
                if trk.get("yoto_sha256")
            }
            if warm_sha_map:
                wconn = database.get_db_connection()
                try:
                    with wconn:
                        wcur = wconn.cursor()
                        for trk in slot.get("tracks", []):
                            key = (str(trk.get("abs_item_id") or abs_item_id), round(float(trk.get("start_time") or 0), 1), round(float(trk.get("end_time") or 0), 1))
                            sha = warm_sha_map.get(key)
                            if sha:
                                wcur.execute(
                                    "UPDATE bridge_tracks SET yoto_sha256 = ? WHERE slot_id = ? AND track_idx = ?;",
                                    (sha, slot_id, trk["track_idx"]),
                                )
                finally:
                    wconn.close()
                slot = self.get_slot(slot_id) or slot

        database.record_playback(abs_item_id, f"user:{user_id}")
        return slot

    def assign_to_fleet_card(self, slot_id: str, abs_item_id: str = "",
                             mode_override: Optional[str] = None,
                             base_url: str = "", yoto_card_id: str = "",
                             title: str = "", author: str = "",
                             group_kind: str = "book", series_name: str = "") -> Dict[str, Any]:
        """Assigns an ABS book/series or an Official Yoto card to a named fleet card slot."""
        if yoto_card_id and not abs_item_id:
            db_item = database.get_media_item_by_id(yoto_card_id) or {}
            card_title = title or db_item.get("title") or f"Yoto Card {yoto_card_id}"
            card_author = author or db_item.get("author") or "Yoto"
            conn = database.get_db_connection()
            try:
                with conn:
                    cur = conn.cursor()
                    cur.execute("""
                        INSERT INTO bridge_slots (
                            slot_id, abs_item_id, yoto_card_id, title, author,
                            group_kind, kind, source, is_protected, mode, updated_at
                        ) VALUES (?, ?, ?, ?, ?, 'yoto_official', 'permanent_myo', 'storydeck', 1, 'retail', CURRENT_TIMESTAMP)
                        ON CONFLICT(slot_id) DO UPDATE SET
                            abs_item_id = excluded.abs_item_id,
                            yoto_card_id = excluded.yoto_card_id,
                            title = excluded.title,
                            author = excluded.author,
                            group_kind = 'yoto_official',
                            mode = 'retail',
                            updated_at = CURRENT_TIMESTAMP;
                    """, (slot_id, yoto_card_id, yoto_card_id, card_title, card_author))
                    cur.execute("DELETE FROM bridge_tracks WHERE slot_id = ?;", (slot_id,))
                return self.get_slot(slot_id) or {}
            finally:
                conn.close()

        raw_mode = (mode_override or "").strip().lower()
        mode = "offline" if (raw_mode.startswith("offline") or raw_mode in ("next", "book", "series")) else "streaming"
        return self.assign_abs_book_to_slot(
            slot_id=slot_id,
            abs_item_id=abs_item_id,
            title=title,
            author=author,
            series_name=series_name,
            group_kind=group_kind or "book",
            kind="permanent_myo",
            mode=mode,
            base_url=base_url
        )


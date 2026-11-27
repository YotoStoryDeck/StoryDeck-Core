"""
Automated LRU Cache Pruner for Ephemeral Offline Playlists.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict

import clients
import database

logger = logging.getLogger("storydeck.bridge.offline.pruner")


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

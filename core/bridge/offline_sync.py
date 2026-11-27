"""
Offline MQTT Progress Sync & Rolling Series Refresh Engine.
Syncs offline card playback timestamps back to Audiobookshelf and automatically
rolls finished books off offline series cards onto the next books in sequence.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, Optional

import database
from .manager import get_bridge_manager
from .mqtt_client import get_mqtt_bridge
from .offline_planner import _extract_slot_base_url
from .series import (
    get_effective_item_progress,
    patch_effective_item_progress,
    resolve_slot_series_chain,
)

logger = logging.getLogger("storydeck.bridge.offline.sync")
OFFLINE_SYNC_INTERVAL = 15.0


class OfflineSyncMixin:
    """MQTT progress synchronization and series advancement mixed into OfflineTranscodeManager."""

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
            if slot.get("offline_mode") == "series":
                meta["series_refresh_pending"] = True

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
            if is_fin and slot.get("offline_mode") == "series":
                meta["series_refresh_pending"] = True

        self._save_slot_offline_meta(slot["slot_id"], meta)
        if status in ("paused", "stopped") and meta.get("series_refresh_pending"):
            self._maybe_refresh_series_slot(slot, meta)

    def _maybe_refresh_series_slot(self, slot: Dict[str, Any], meta: Optional[Dict[str, Any]] = None) -> bool:
        """
        Checks if an offline series card's leading book has finished so finished books can be dropped
        and the next book(s) in the series rolled onto the card automatically.
        """
        slot_id = str(slot.get("slot_id") or "")
        if not slot_id or slot.get("mode") != "offline" or slot.get("offline_mode") != "series":
            return False
        if slot.get("kind") in ("profile_cache", "ephemeral_offline"):
            return False
        if self.is_busy(slot_id) or self._is_slot_playing(slot):
            return False

        if meta is None:
            raw_meta = slot.get("offline_meta")
            meta = {}
            if isinstance(raw_meta, str) and raw_meta:
                try:
                    meta = json.loads(raw_meta)
                except Exception:
                    meta = {}
            elif isinstance(raw_meta, dict):
                meta = dict(raw_meta)

        abs_token = self._resolve_slot_abs_token(slot)
        all_books, active_idx, _ = resolve_slot_series_chain(slot, abs_token=abs_token)
        if not all_books:
            return False

        cur_books = [str(b) for b in (meta.get("books") or []) if b]
        new_active_id = all_books[active_idx].item_id
        if not cur_books or new_active_id == cur_books[0]:
            if meta.get("series_refresh_pending"):
                meta["series_refresh_pending"] = False
                self._save_slot_offline_meta(slot_id, meta)
            return False

        meta["series_refresh_pending"] = False
        self._save_slot_offline_meta(slot_id, meta)
        logger.info(
            "Rolling offline series card %s forward from finished book %s -> %s",
            slot_id,
            cur_books[0],
            new_active_id,
        )
        self.queue_transcode(slot_id, base_url=_extract_slot_base_url(slot), offline_mode="series")
        return True

    def refresh_offline_series_slots(self) -> int:
        """Scans all offline series slots and queues rolling updates when a leading book has finished."""
        queued = 0
        try:
            mgr = get_bridge_manager()
            for slot in (mgr.list_slots() or []):
                if slot.get("mode") == "offline" and slot.get("offline_mode") == "series":
                    if self._maybe_refresh_series_slot(slot):
                        queued += 1
        except Exception as e:
            logger.debug("refresh_offline_series_slots notice: %s", e)
        return queued

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


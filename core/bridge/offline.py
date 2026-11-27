"""
Offline Transcoding Engine & Job Lifecycle Manager.
Delegates track planning to OfflinePlannerMixin, MQTT progress/series rolling to OfflineSyncMixin,
and LRU eviction to offline_pruner.py.
"""
from __future__ import annotations

import json
import logging
import shutil
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import clients
import database
from .icons import get_or_upload_yoto_icon, resolve_source_chapter_idx, upload_abs_cover_to_yoto
from .manager import get_bridge_manager
from .offline_planner import (
    DEFAULT_LOOKBACK_HOURS,
    DEFAULT_REWIND_SECONDS,
    OFFLINE_MODES,
    OfflinePlannerMixin,
    _extract_slot_base_url,
    _hms,
    normalize_offline_submode,
    resolve_series_packing,
)
from .offline_pruner import prune_evicted_profile_playlists
from .offline_sync import OFFLINE_SYNC_INTERVAL, OfflineSyncMixin
from .streamer import AudioStreamer, AudioTrackSource

logger = logging.getLogger("storydeck.bridge.offline")
LIVE_BATCH = 5
LIVE_SECONDS = 120.0


class OfflineTranscodeManager(OfflinePlannerMixin, OfflineSyncMixin):
    """
    Manages background transcoding and upload jobs for offline Yoto cards:
    - Supports 'next' (5h rolling window), 'book' (whole book <=99 tracks), and 'series' (whole series <=100 tracks).
    - Publishes hybrid mixed playlists (`save_mixed_playlist`) every 5 tracks / 120s while building.
    - Tracks MQTT playback events on offline cards (`on_mqtt_event`) via OfflineSyncMixin.
    """

    def __init__(self):
        self._jobs: Dict[str, Dict[str, Any]] = {}
        self._pause_flags: Dict[str, threading.Event] = {}
        self._cancel_flags: Dict[str, threading.Event] = {}
        self._job_gen: Dict[str, int] = {}
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
            return bool(job and job.get("status") in ("queued", "encoding", "uploading", "processing", "pausing", "stopping"))

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
                off_status = str(slot.get("offline_status") or "").lower()
                if off_status in ("failed", "paused", "stopped"):
                    continue
                is_building = off_status == "building"
                has_unencoded = bool(tracks) and any(not str(t.get("yoto_sha256") or "").strip() for t in tracks)
                if not is_building and not has_unencoded:
                    continue
                self._verify_last_uploaded_track(slot, tracks)
                base_url = _extract_slot_base_url(slot)
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
        anchor_current_book: bool = False,
        replace_existing: bool = False,
    ) -> Dict[str, Any]:
        """Queues a card slot for offline transcoding and Yoto upload, optionally superseding any active job."""
        with self._lock:
            existing = self._jobs.get(slot_id)
            if not replace_existing and existing and existing.get("status") in ("queued", "encoding", "uploading", "processing"):
                return dict(existing)

            if slot_id in self._cancel_flags:
                self._cancel_flags[slot_id].set()
            if slot_id in self._pause_flags:
                self._pause_flags[slot_id].clear()

            gen = self._job_gen.get(slot_id, 0) + 1
            self._job_gen[slot_id] = gen
            pause_ev = threading.Event()
            cancel_ev = threading.Event()
            self._pause_flags[slot_id] = pause_ev
            self._cancel_flags[slot_id] = cancel_ev

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
            args=(slot_id, base_url, offline_mode, float(hours or 5.0), anchor_current_book, gen, pause_ev, cancel_ev),
            daemon=True,
        )
        thread.start()
        return job

    def cancel_job(self, slot_id: str) -> Dict[str, Any]:
        """Cancels and clears any active, paused, stopped, or failed offline transfer for a slot."""
        with self._lock:
            self._job_gen[slot_id] = self._job_gen.get(slot_id, 0) + 1
            if slot_id in self._pause_flags:
                self._pause_flags[slot_id].clear()
            self._cancel_flags.setdefault(slot_id, threading.Event()).set()
            self._jobs.pop(slot_id, None)
        self._persist_job_status(slot_id, "none")
        return {"slot_id": slot_id, "status": "cancelled"}

    def _run_job_guarded(
        self,
        slot_id: str,
        base_url: str,
        offline_mode: Optional[str],
        hours: float,
        anchor_current_book: bool = False,
        job_gen: Optional[int] = None,
        pause_ev: Optional[threading.Event] = None,
        cancel_ev: Optional[threading.Event] = None,
    ):
        with self._queue_sem:
            self._run_job(
                slot_id,
                base_url,
                offline_mode=offline_mode,
                hours=hours,
                anchor_current_book=anchor_current_book,
                job_gen=job_gen,
                pause_ev=pause_ev,
                cancel_ev=cancel_ev,
            )

    def pause_job(self, slot_id: str) -> Dict[str, Any]:
        """Requests an active or queued offline transfer to pause cleanly without blocking the queue."""
        persist_now = False
        with self._lock:
            ev = self._pause_flags.setdefault(slot_id, threading.Event())
            ev.set()
            cur = self._jobs.get(slot_id)
            if cur and cur.get("status") in ("encoding", "uploading", "processing", "pausing"):
                cur["status"] = "pausing"
                cur["updated_at"] = time.time()
                return dict(cur)
            if cur and cur.get("status") == "queued":
                cur["status"] = "paused"
                cur["updated_at"] = time.time()
                persist_now = True
                res = dict(cur)
            else:
                slot = get_bridge_manager().get_slot(slot_id) or {}
                tracks = slot.get("tracks") or []
                done_cnt = sum(1 for t in tracks if str(t.get("yoto_sha256") or "").strip())
                total_cnt = len(tracks)
                res = {
                    "slot_id": slot_id,
                    "status": "paused",
                    "offline_mode": slot.get("offline_mode") or "book",
                    "done": done_cnt,
                    "total": total_cnt,
                    "progress": (done_cnt / total_cnt) if total_cnt > 0 else 0.0,
                    "error": None,
                    "updated_at": time.time(),
                }
                self._jobs[slot_id] = dict(res)
                persist_now = True
        if persist_now:
            self._persist_job_status(slot_id, "paused")
        return res

    def stop_job(self, slot_id: str) -> Dict[str, Any]:
        """Requests an active or paused offline transfer to stop cleanly while keeping uploaded tracks."""
        persist_now = False
        with self._lock:
            self._pause_flags.setdefault(slot_id, threading.Event()).clear()
            self._cancel_flags.setdefault(slot_id, threading.Event()).set()
            cur = self._jobs.get(slot_id)
            if cur and cur.get("status") in ("encoding", "uploading", "processing", "pausing", "stopping"):
                cur["status"] = "stopping"
                cur["updated_at"] = time.time()
                return dict(cur)
            if cur and cur.get("status") in ("queued", "paused"):
                cur["status"] = "stopped"
                cur["updated_at"] = time.time()
                persist_now = True
                res = dict(cur)
            else:
                slot = get_bridge_manager().get_slot(slot_id) or {}
                tracks = slot.get("tracks") or []
                done_cnt = sum(1 for t in tracks if str(t.get("yoto_sha256") or "").strip())
                total_cnt = len(tracks)
                res = {
                    "slot_id": slot_id,
                    "status": "stopped",
                    "offline_mode": slot.get("offline_mode") or "book",
                    "done": done_cnt,
                    "total": total_cnt,
                    "progress": (done_cnt / total_cnt) if total_cnt > 0 else 0.0,
                    "error": None,
                    "updated_at": time.time(),
                }
                self._jobs[slot_id] = dict(res)
                persist_now = True
        if persist_now:
            self._persist_job_status(slot_id, "stopped")
        return res

    def resume_job(self, slot_id: str, base_url: str = "") -> Dict[str, Any]:
        """Resumes a paused, stopped, or failed offline transfer from the last verified track."""
        mgr = get_bridge_manager()
        slot = mgr.get_slot(slot_id)
        if not slot:
            raise ValueError("Slot not found")
        self._verify_last_uploaded_track(slot, slot.get("tracks") or [])
        with database.get_db_connection() as conn:
            conn.execute(
                "UPDATE bridge_slots SET offline_status = 'building', updated_at = CURRENT_TIMESTAMP WHERE slot_id = ?;",
                (slot_id,),
            )
            conn.commit()
        submode = str(slot.get("offline_mode") or "book")
        hours = float(slot.get("offline_hours") or 5.0)
        eff_base = base_url or _extract_slot_base_url(slot)
        return self.queue_transcode(
            slot_id,
            base_url=eff_base,
            offline_mode=submode,
            hours=hours,
            anchor_current_book=(submode == "series"),
            replace_existing=True,
        )

    def _run_job(
        self,
        slot_id: str,
        base_url: str,
        offline_mode: Optional[str] = None,
        hours: float = 5.0,
        anchor_current_book: bool = False,
        job_gen: Optional[int] = None,
        pause_ev: Optional[threading.Event] = None,
        cancel_ev: Optional[threading.Event] = None,
    ):
        if pause_ev is None:
            pause_ev = self._pause_flags.setdefault(slot_id, threading.Event())
        if cancel_ev is None:
            cancel_ev = self._cancel_flags.setdefault(slot_id, threading.Event())

        offline_meta: Dict[str, Any] = {}

        def _is_current_gen() -> bool:
            return job_gen is None or self._job_gen.get(slot_id) == job_gen

        def _check_interrupted(save_live_cb=None) -> Optional[str]:
            is_cur = _is_current_gen()
            if cancel_ev.is_set() or not is_cur:
                if save_live_cb and is_cur:
                    save_live_cb(force=True)
                if is_cur:
                    self._persist_job_status(slot_id, "stopped", offline_meta=offline_meta)
                    self._update_job(slot_id, status="stopped")
                logger.info("Offline [%s]: transfer stopped cleanly", slot_id)
                return "stopped"
            if pause_ev.is_set():
                if save_live_cb:
                    save_live_cb(force=True)
                if is_cur:
                    self._persist_job_status(slot_id, "paused", offline_meta=offline_meta)
                    self._update_job(slot_id, status="paused")
                logger.info("Offline [%s]: transfer paused cleanly", slot_id)
                return "paused"
            return None

        if _check_interrupted():
            return

        mgr = get_bridge_manager()
        slot = mgr.get_slot(slot_id)
        if not slot:
            self._update_job(slot_id, status="failed", error="Slot not found")
            return

        abs_item_id = slot.get("abs_item_id")
        if not abs_item_id or not clients.abs_client:
            err = "No ABS client or item configured"
            self._persist_job_failure(slot_id, err)
            self._update_job(slot_id, status="failed", error=err)
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
                anchor_current_book=anchor_current_book,
            )
        except Exception as e:
            logger.warning("Offline track planning fallback for %s: %s", slot_id, e)
            tracks = slot.get("tracks") or []
            offline_meta = {}

        if _check_interrupted():
            return

        if not tracks:
            err = "Slot has no tracks"
            self._persist_job_failure(slot_id, err, offline_meta)
            self._update_job(slot_id, status="failed", error=err)
            return

        if not shutil.which("ffmpeg"):
            err = "ffmpeg binary not found in PATH — install ffmpeg to encode offline audio."
            logger.error("Error during offline transcode/upload for slot %s: %s", slot_id, err)
            self._persist_job_failure(slot_id, err, offline_meta)
            self._update_job(slot_id, status="failed", error=err, total=len(tracks))
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
            err = "ABS item has no audio files"
            self._persist_job_failure(slot_id, err, offline_meta)
            self._update_job(slot_id, status="failed", error=err, total=total_tracks)
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
            if not _is_current_gen() or cancel_ev.is_set():
                return
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

        book_chapters_cache: Dict[str, List[Dict[str, Any]]] = {}

        def _get_book_chapters(book_id: str) -> List[Dict[str, Any]]:
            if book_id not in book_chapters_cache:
                chapters_list: List[Dict[str, Any]] = []
                if clients.abs_client and book_id:
                    try:
                        b_item = clients.abs_client.get_item(book_id, token=abs_token)
                        if isinstance(b_item, dict):
                            media_obj = b_item.get("media")
                            if isinstance(media_obj, dict):
                                chapters_list = media_obj.get("chapters") or []
                    except Exception as e:
                        logger.debug("Offline [%s]: chapters lookup failed for %s: %s", slot_id, book_id, e)
                book_chapters_cache[book_id] = chapters_list
            return book_chapters_cache[book_id]

        conn = database.get_db_connection()
        try:
            for idx, trk in enumerate(tracks):
                if _check_interrupted(_save_live_batch):
                    return
                track_idx = int(trk["track_idx"])
                t_book_id = str(trk.get("abs_item_id") or abs_item_id)
                start_sec = float(trk["start_time"])
                end_sec = float(trk["end_time"])
                trk_title = trk.get("title") or f"Track {track_idx}"
                sha = trk.get("yoto_sha256")
                src_ch_idx = resolve_source_chapter_idx(_get_book_chapters(t_book_id), start_sec, end_sec)
                icon_ref = get_or_upload_yoto_icon(
                    t_book_id,
                    track_idx=track_idx,
                    token=abs_token,
                    chapter_idx=src_ch_idx,
                )
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
                    if cancel_ev.is_set() or pause_ev.is_set():
                        if _check_interrupted(_save_live_batch):
                            return

                if _check_interrupted(_save_live_batch):
                    return

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

            if _check_interrupted(_save_live_batch) or not _is_current_gen():
                return

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
            if not saved_cid:
                raise RuntimeError("Failed to save final offline playlist to Yoto Cloud")

            if not _is_current_gen():
                return

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
            if _is_current_gen():
                self._persist_job_failure(slot_id, str(e), offline_meta)
                self._update_job(slot_id, status="failed", error=str(e))
        finally:
            conn.close()

    def _persist_job_status(self, slot_id: str, status: str, error_msg: str = "", offline_meta: Optional[Dict[str, Any]] = None):
        """Persists offline build state ('failed', 'paused', 'stopped', etc.) in SQLite so status survives restarts."""
        conn = database.get_db_connection()
        try:
            meta: Dict[str, Any] = dict(offline_meta) if isinstance(offline_meta, dict) else {}
            if not meta:
                row = conn.execute("SELECT offline_meta FROM bridge_slots WHERE slot_id = ?;", (slot_id,)).fetchone()
                if row and row[0]:
                    try:
                        parsed = json.loads(row[0])
                        if isinstance(parsed, dict):
                            meta = parsed
                    except Exception:
                        meta = {}
            meta["status"] = status
            if status == "failed":
                meta["error"] = str(error_msg or "Offline build failed")
                meta["failed_at"] = time.time()
            else:
                meta.pop("error", None)
                meta["updated_at"] = time.time()
            with conn:
                conn.execute(
                    "UPDATE bridge_slots SET offline_status = ?, offline_meta = ?, updated_at = CURRENT_TIMESTAMP WHERE slot_id = ?;",
                    (status, json.dumps(meta), slot_id),
                )
        except Exception as db_err:
            logger.debug("_persist_job_status notice for %s: %s", slot_id, db_err)
        finally:
            conn.close()

    def _persist_job_failure(self, slot_id: str, error_msg: str, offline_meta: Optional[Dict[str, Any]] = None):
        """Persists offline build failure state & message in SQLite so errors survive restarts and display in the Cards UI."""
        self._persist_job_status(slot_id, "failed", error_msg=error_msg, offline_meta=offline_meta)

    def _update_job(self, slot_id: str, **kwargs):
        with self._lock:
            if slot_id in self._jobs:
                self._jobs[slot_id].update(kwargs)
                self._jobs[slot_id]["updated_at"] = time.time()

    # ==========================================================================

_offline_manager: Optional[OfflineTranscodeManager] = None


def get_offline_manager() -> OfflineTranscodeManager:
    global _offline_manager
    if _offline_manager is None:
        _offline_manager = OfflineTranscodeManager()
    return _offline_manager


def refresh_offline_series_slots() -> int:
    """Module-level entrypoint for rolling finished books off offline series cards."""
    return get_offline_manager().refresh_offline_series_slots()


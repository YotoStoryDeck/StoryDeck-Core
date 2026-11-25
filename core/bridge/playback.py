"""
Bi-Directional Audiobookshelf Stream Progress Manager for StoryDeck Core.
Tracks active /s/audio/<slot_id>/<track_idx> streaming sessions per player,
estimates real-time book positions from delivered byte ranges, wall-clock play time,
and embedded MQTT telemetry, and syncs progress back to Audiobookshelf.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import clients
import database
from .mqtt_client import get_mqtt_bridge
from .series import (
    get_effective_item_progress,
    patch_effective_item_progress,
    resolve_slot_series_chain,
)
from .streamer import AudioStreamer, AudioTrackSource

logger = logging.getLogger("storydeck.bridge.playback")

DEFAULT_SYNC_INTERVAL = 15.0
DEFAULT_END_GRACE_SECONDS = 15.0
STARTUP_LAG_SECONDS = 3.0
DEFAULT_PLAYER_BUFFER_SECONDS = 8.0
DEFAULT_REWIND_SECONDS = 15.0
SHORT_PAUSE_THRESHOLD = 30.0
SHORT_PAUSE_REWIND = 2.0
DEFAULT_WARM_START_SECONDS = 5.0


@dataclass
class WarmBuffer:
    """Pre-encoded first seconds of a slot's resume audio for instant tap response."""
    abs_item_id: str
    start_sec: float
    speed: float
    audio: bytes


@dataclass
class PlaybackSession:
    """Active HTTP audio streaming session correlated with a Yoto player and ABS book."""
    slot_id: str
    abs_item_id: str
    title: str
    owner_profile_id: str
    device_id: str
    abs_token: Optional[str]
    abs_session_id: str
    own_place: bool
    track_idx: int
    track_start: float
    track_end: float
    book_duration: float
    base: float
    speed: float = 1.0
    byterate: float = 8000.0
    bytes_sent: int = 0
    open_streams: int = 0
    started_at: float = field(default_factory=time.monotonic)
    started_wall: float = field(default_factory=time.time)
    ended_at: Optional[float] = None
    last_pos: float = 0.0
    last_sync_at: float = field(default_factory=time.monotonic)
    last_write_wall: float = field(default_factory=time.time)
    paused_at: Optional[float] = None
    paused_total: float = 0.0
    last_pause_duration: float = 0.0
    pause_pos: Optional[float] = None
    mqtt_event: Dict[str, Any] = field(default_factory=dict)
    mqtt_event_at: Optional[float] = None
    yoto_card_id: str = ""
    warm_bytes: bytes = b""


class PlaybackManager:
    """Tracks live stream sessions and pushes bi-directional progress updates to ABS."""

    def __init__(self, sync_interval: float = DEFAULT_SYNC_INTERVAL,
                 end_grace_seconds: float = DEFAULT_END_GRACE_SECONDS,
                 player_buffer_seconds: float = DEFAULT_PLAYER_BUFFER_SECONDS):
        self.sync_interval = sync_interval
        self.end_grace_seconds = end_grace_seconds
        self.player_buffer_seconds = player_buffer_seconds
        self._lock = threading.Lock()
        self._sessions: Dict[str, PlaybackSession] = {}
        self._expected: Dict[str, Tuple[str, float, Optional[str], str]] = {}
        self._jumping: Dict[str, Tuple[int, float]] = {}
        self._exact_next: set[str] = set()
        self._warm: Dict[str, WarmBuffer] = {}
        self._stop_event = threading.Event()
        self._sync_thread: Optional[threading.Thread] = None

    def mark_exact_next(self, slot_id: str):
        """Marks a slot so the next stream start skips the 15s rewind (e.g. explicit chapter selection)."""
        if slot_id:
            with self._lock:
                self._exact_next.add(slot_id)

    def _card_out_since_start(self, session: PlaybackSession) -> bool:
        """Returns True if the player reported card removal / stop after this session started."""
        if not session.device_id:
            return False
        try:
            st = get_mqtt_bridge().get_device_state(session.device_id) or {}
            return (
                float(st.get("at") or 0.0) > session.started_wall
                and not st.get("cardId")
                and str(st.get("playbackStatus") or "").lower() == "stopped"
            )
        except Exception:
            return False

    def pause_rewind_seconds(self, session: PlaybackSession) -> float:
        """Returns 2s rewind after a short pause (<30s) or 15s rewind after a longer pause."""
        now_t = time.monotonic()
        pause_dur = (now_t - session.paused_at) if session.paused_at is not None else session.last_pause_duration
        return SHORT_PAUSE_REWIND if pause_dur < SHORT_PAUSE_THRESHOLD else DEFAULT_REWIND_SECONDS

    def resolve_track_request(
        self,
        slot: Dict[str, Any],
        track: Dict[str, Any],
        probe: bool = False,
        device_id: str = "",
    ) -> Dict[str, Any]:
        """
        Determines what chapter track `track_idx` plays on `/s/audio/<slot_id>/<track_idx>`:
        - Within an active session:
          * If resuming the same track after a pause (where Yoto sends no Range header), resumes from `pause_pos - pause_rewind`.
          * If moving to next/prev track, plays from `track.start_time`.
        - On a fresh tap (no active session or card was taken out):
          * Queries live ABS (or own_place local) `currentTime` (minus 15s rewind unless `exact_next` or explicit chapter).
          * If the listener's position falls inside `track_idx`, returns `{"start_time": resume_sec, "end_time": track.end_time}`.
          * If the listener is in a different chapter `right_track != track_idx` and MQTT is connected, returns `{"jump": right_track}`
            so the stream endpoint serves a short silent MP3 and commands the player over MQTT to switch to `right_track`!
        """
        slot_id = slot["slot_id"]
        track_idx = int(track.get("track_idx") or 1)
        t_start = float(track.get("start_time") or 0.0)
        t_end = float(track.get("end_time") or 0.0)
        t_item_id = str(track.get("abs_item_id") or slot.get("abs_item_id") or "")

        with self._lock:
            pending = self._jumping.get(slot_id)
            if pending and (time.monotonic() - pending[1]) >= 15.0:
                self._jumping.pop(slot_id, None)
                pending = None
            existing = self._sessions.get(slot_id)

        if pending:
            if pending[0] != track_idx:
                return {"jump": pending[0], "pending": True}

        if existing and not self._card_out_since_start(existing):
            if existing.track_idx == track_idx and existing.abs_item_id == t_item_id:
                pos = existing.pause_pos if existing.pause_pos is not None else self.compute_position(existing)
                if t_start < pos < (t_end - 1.0):
                    back = self.pause_rewind_seconds(existing)
                    resumed_start = max(t_start, pos - back)
                    return {"start_time": resumed_start, "end_time": t_end, "item_id": t_item_id}
            return {"start_time": t_start, "end_time": t_end, "item_id": t_item_id}

        # Fresh tap: check if an explicit chapter was just commanded via Portal / Kiosk
        with self._lock:
            is_exact = slot_id in self._exact_next
            if is_exact and not probe:
                self._exact_next.discard(slot_id)
                self._jumping.pop(slot_id, None)
        if is_exact:
            return {"start_time": t_start, "end_time": t_end, "item_id": t_item_id}

        # If an offline copy is currently building and this track is streaming, honor the track directly
        if slot.get("mode") == "offline":
            return {"start_time": t_start, "end_time": t_end, "item_id": t_item_id}

        resolved_dev, abs_token_override, _ = self._resolve_stream_context(slot, device_id)
        owner_user_id = str(slot.get("owner_user_id") or "").strip()
        owner_prof = str(slot.get("owner_profile_id") or "").strip()
        abs_token = abs_token_override
        if not abs_token and clients.abs_client and not slot.get("own_place"):
            scope_key = f"user:{owner_user_id}" if owner_user_id else (owner_prof or None)
            abs_token = clients.abs_client.resolve_token_for_profile(scope_key, resolved_dev or None)

        if str(slot.get("group_kind") or "") == "series":
            resolve_slot_series_chain(slot, abs_token=abs_token)

        active_item = str(slot.get("abs_item_id") or t_item_id)
        prog = get_effective_item_progress(slot, active_item, abs_token=abs_token)
        if not prog:
            return {"start_time": t_start, "end_time": t_end, "item_id": t_item_id}

        dur = float(prog.get("duration") or track.get("book_duration") or slot.get("total_duration") or 0.0)
        pos = 0.0 if prog.get("isFinished") else float(prog.get("currentTime") or 0.0)
        if dur > 0 and pos >= dur - 10.0:
            pos = 0.0
        resume_sec = max(0.0, pos - DEFAULT_REWIND_SECONDS) if pos > DEFAULT_REWIND_SECONDS else 0.0

        if active_item == t_item_id and t_start <= resume_sec < (t_end - 1.0):
            if not probe:
                with self._lock:
                    self._jumping.pop(slot_id, None)
            return {"start_time": max(t_start, resume_sec), "end_time": t_end, "item_id": t_item_id}

        # Find which track on this slot holds (active_item, resume_sec)
        all_tracks = slot.get("tracks") or []
        right_track_idx: Optional[int] = None
        for cand in all_tracks:
            c_item = str(cand.get("abs_item_id") or slot.get("abs_item_id") or "")
            c_start = float(cand.get("start_time") or 0.0)
            c_end = float(cand.get("end_time") or 0.0)
            if c_item == active_item and c_start <= resume_sec < c_end:
                right_track_idx = int(cand.get("track_idx") or 1)
                break

        mqtt = get_mqtt_bridge()
        yoto_cid = str(slot.get("yoto_card_id") or "").strip()
        if (
            right_track_idx
            and right_track_idx != track_idx
            and yoto_cid
            and mqtt.is_connected
            and not pending
        ):
            logger.info(
                "%s: player asked for track %d but listener is in track %d (%.0fs); jumping via MQTT",
                slot.get("title") or slot_id,
                track_idx,
                right_track_idx,
                resume_sec,
            )
            return {"jump": right_track_idx, "device_id": resolved_dev}

        return {"start_time": t_start, "end_time": t_end, "item_id": t_item_id}

    def trigger_mqtt_track_jump(self, slot: Dict[str, Any], target_track: int, device_id: str = ""):
        """Dispatches an asynchronous MQTT command telling the player that tapped `slot` to jump to `target_track`."""
        slot_id = slot["slot_id"]
        yoto_cid = str(slot.get("yoto_card_id") or "").strip()
        if not yoto_cid:
            return
        with self._lock:
            self._jumping[slot_id] = (target_track, time.monotonic())

        def _worker():
            try:
                dev = device_id
                mqtt = get_mqtt_bridge()
                for _ in range(30):
                    if dev:
                        break
                    states = mqtt.get_device_states()
                    for did, ev in states.items():
                        if ev.get("cardId") == yoto_cid and (time.time() - float(ev.get("at") or 0)) < 15.0:
                            dev = did
                            break
                    if not dev:
                        time.sleep(0.1)
                if not dev:
                    dev = self._resolve_device_id(slot, "")
                if dev and mqtt.is_connected:
                    self.expect(dev, yoto_cid)
                    mqtt.play(dev, yoto_cid, track=target_track)
            except Exception as e:
                logger.debug("trigger_mqtt_track_jump notice for %s: %s", slot_id, e)
                with self._lock:
                    self._jumping.pop(slot_id, None)

        threading.Thread(target=_worker, name=f"YotoTrackJump-{slot_id}", daemon=True).start()

    def expect(
        self,
        device_id: str,
        card_id: str,
        abs_token_override: Optional[str] = None,
        owner_override: str = "",
    ):
        """Records an impending player playback command so incoming streams bind to that player and optional ABS account override."""
        if device_id and card_id:
            with self._lock:
                self._expected[card_id] = (device_id, time.monotonic(), abs_token_override, owner_override)

    def _resolve_stream_context(
        self, slot: Dict[str, Any], explicit_device_id: str = ""
    ) -> Tuple[str, Optional[str], str]:
        """Resolves (device_id, abs_token_override, owner_override) for an opening stream."""
        token_override: Optional[str] = None
        owner_override = ""
        resolved_dev = explicit_device_id.strip() if explicit_device_id and explicit_device_id.strip() else ""

        yoto_cid = (slot.get("yoto_card_id") or "").strip()
        if yoto_cid:
            with self._lock:
                exp = self._expected.get(yoto_cid)
                if exp and (time.monotonic() - exp[1]) < 60.0:
                    self._expected.pop(yoto_cid, None)
                    if not resolved_dev:
                        resolved_dev = exp[0]
                    token_override = exp[2]
                    owner_override = exp[3] or ""

        if not resolved_dev and yoto_cid:
            try:
                mqtt_states = get_mqtt_bridge().get_device_states()
                for did, ev in mqtt_states.items():
                    if ev.get("cardId") == yoto_cid and (time.time() - float(ev.get("at") or 0)) < 60.0:
                        resolved_dev = did
                        break
            except Exception:
                pass

        if not resolved_dev:
            owner_prof = (slot.get("owner_profile_id") or "").strip()
            if owner_prof:
                try:
                    prof = database.get_profile_by_id(owner_prof)
                    if prof and prof.get("default_player_id"):
                        resolved_dev = str(prof["default_player_id"])
                except Exception:
                    pass

        return resolved_dev, token_override, owner_override

    def _resolve_device_id(self, slot: Dict[str, Any], explicit_device_id: str = "") -> str:
        dev_id, _, _ = self._resolve_stream_context(slot, explicit_device_id)
        return dev_id

    def open_stream(self, slot: Dict[str, Any], track: Dict[str, Any],
                    byte_start: int = 0, speed: float = 1.0,
                    byterate: float = 8000.0, device_id: str = "",
                    override_start_sec: Optional[float] = None) -> PlaybackSession:
        """
        Opens or re-anchors a PlaybackSession when a Yoto player fetches /s/audio/<slot_id>/<track_idx>.
        Supports multi-book series auto-advance when `track['abs_item_id']` moves to a subsequent book.
        """
        slot_id = slot["slot_id"]
        abs_item_id = str(track.get("abs_item_id") or slot["abs_item_id"])
        track_idx = int(track.get("track_idx") or 1)
        track_start = float(override_start_sec if override_start_sec is not None else (track.get("start_time") or 0.0))
        track_end = float(track.get("end_time") or 0.0)
        book_duration = max(float(track.get("book_duration") or slot.get("total_duration") or 0.0), track_end)
        eff_speed = max(0.1, float(speed or 1.0))
        eff_byterate = float(byterate or 8000.0)
        rate = eff_byterate / eff_speed
        seek_sec = max(0.0, float(byte_start or 0) / rate)
        req_start = track_start + seek_sec

        with self._lock:
            existing = self._sessions.get(slot_id)
            if existing and existing.abs_item_id == abs_item_id and existing.track_idx == track_idx and byte_start > 0:
                existing.open_streams += 1
                existing.ended_at = None
                if device_id and not existing.device_id:
                    existing.device_id = device_id
                return existing

        if existing and existing.abs_item_id == abs_item_id:
            try:
                self.sync_session(existing, force=True)
            except Exception as e:
                logger.debug("Pre-reanchor sync notice on %s: %s", slot_id, e)
            with self._lock:
                cur = self._sessions.get(slot_id)
                if cur is existing:
                    cur.track_idx = track_idx
                    cur.track_start = track_start
                    cur.track_end = track_end
                    cur.book_duration = book_duration
                    cur.base = req_start
                    cur.speed = eff_speed
                    cur.byterate = eff_byterate
                    cur.bytes_sent = 0
                    cur.started_at = time.monotonic()
                    cur.started_wall = time.time()
                    cur.paused_at = None
                    cur.paused_total = 0.0
                    cur.pause_pos = None
                    cur.open_streams += 1
                    cur.ended_at = None
                    cur.last_pos = req_start
                    cur.last_sync_at = time.monotonic()
                    cur.mqtt_event = {}
                    cur.mqtt_event_at = None
                    cur.warm_bytes = b""
                    if device_id:
                        cur.device_id = device_id
                    return cur
        elif existing:
            # If advancing to the next book in a chained series, mark the previous book finished
            if existing.abs_item_id != abs_item_id and str(slot.get("group_kind") or "") == "series":
                try:
                    patch_effective_item_progress(
                        slot,
                        existing.abs_item_id,
                        existing.book_duration,
                        existing.book_duration,
                        is_finished=True,
                        abs_token=existing.abs_token,
                    )
                    conn = database.get_db_connection()
                    try:
                        with conn:
                            conn.execute(
                                "UPDATE bridge_slots SET abs_item_id = ?, title = ?, updated_at = CURRENT_TIMESTAMP WHERE slot_id = ?;",
                                (abs_item_id, track.get("book_title") or slot.get("title") or "Audiobook", slot_id),
                            )
                    finally:
                        conn.close()
                except Exception as e:
                    logger.debug("Series book advance mark_finished notice: %s", e)
            self.finish_session(slot_id, "switched book")

        resolved_dev, abs_token_override, owner_override = self._resolve_stream_context(slot, device_id)
        owner_profile_id = (slot.get("owner_profile_id") or "").strip()
        owner_user_id = (slot.get("owner_user_id") or "").strip()
        effective_owner = owner_override or (f"user:{owner_user_id}" if owner_user_id else owner_profile_id)
        own_place = bool(slot.get("own_place"))

        abs_token: Optional[str] = None
        abs_session_id = ""
        if clients.abs_client and not own_place:
            if abs_token_override:
                abs_token = abs_token_override
            else:
                scope_key = f"user:{owner_user_id}" if owner_user_id else (owner_profile_id or None)
                abs_token = clients.abs_client.resolve_token_for_profile(
                    scope_key,
                    resolved_dev or None
                )
            sess_data = clients.abs_client.start_playback_session(
                abs_item_id,
                resolved_dev or slot_id,
                device_name="Yoto",
                token=abs_token
            )
            if sess_data and sess_data.get("id"):
                abs_session_id = str(sess_data["id"])

        try:
            database.record_playback(abs_item_id, effective_owner or "default")
        except Exception as e:
            logger.debug("Playback history record notice for %s: %s", abs_item_id, e)

        warm_bytes = b""
        if byte_start == 0:
            with self._lock:
                wb = self._warm.get(slot_id)
                if wb and wb.abs_item_id == abs_item_id and abs(wb.speed - eff_speed) < 0.01 and abs(wb.start_sec - req_start) < 0.6:
                    warm_bytes = wb.audio

        now_m = time.monotonic()
        session = PlaybackSession(
            slot_id=slot_id,
            abs_item_id=abs_item_id,
            title=track.get("book_title") or slot.get("title") or "Audiobook",
            owner_profile_id=effective_owner,
            device_id=resolved_dev,
            abs_token=abs_token,
            abs_session_id=abs_session_id,
            own_place=own_place,
            track_idx=track_idx,
            track_start=track_start,
            track_end=track_end,
            book_duration=book_duration,
            base=req_start,
            speed=eff_speed,
            byterate=eff_byterate,
            bytes_sent=0,
            open_streams=1,
            started_at=now_m,
            started_wall=time.time(),
            ended_at=None,
            last_pos=req_start,
            last_sync_at=now_m,
            last_write_wall=time.time(),
            yoto_card_id=(slot.get("yoto_card_id") or "").strip(),
            warm_bytes=warm_bytes,
        )
        with self._lock:
            self._sessions[slot_id] = session
        return session

    def record_bytes(self, session: PlaybackSession, num_bytes: int):
        """Tracks streamed MP3 byte delivery and periodically triggers ABS sync."""
        due = False
        with self._lock:
            session.bytes_sent += max(0, int(num_bytes))
            if (time.monotonic() - session.last_sync_at) >= self.sync_interval:
                due = True
        if due:
            try:
                self.sync_session(session)
            except Exception as e:
                logger.debug("Periodic stream byte sync notice on %s: %s", session.slot_id, e)

    def close_stream(self, session: PlaybackSession, force_sync: bool = True):
        """Marks an HTTP audio stream response closed and flushes progress to ABS."""
        with self._lock:
            session.open_streams = max(0, session.open_streams - 1)
            if session.open_streams == 0:
                session.ended_at = time.monotonic()
        if force_sync:
            try:
                self.sync_session(session, force=True)
            except Exception as e:
                logger.debug("Close stream sync notice on %s: %s", session.slot_id, e)

    def compute_position(self, session: PlaybackSession, now_mono: Optional[float] = None) -> float:
        """
        Computes current book position in seconds, bounded by delivered audio bytes
        and elapsed active playback time (excluding paused intervals).
        """
        now_t = now_mono if now_mono is not None else time.monotonic()
        rate = session.byterate / max(0.1, session.speed)
        delivered_book_sec = session.bytes_sent / rate
        played = now_t - session.started_at - session.paused_total
        if session.paused_at is not None:
            played -= (now_t - max(session.paused_at, session.started_at))

        cap = session.base + min(delivered_book_sec, max(0.0, played) * session.speed)
        if session.track_end > 0:
            cap = min(cap, session.track_end)
        if session.book_duration > 0:
            cap = min(cap, session.book_duration)

        ev = session.mqtt_event
        status = ev.get("playbackStatus")
        fresh = (
            session.mqtt_event_at is not None
            and ((now_t - session.mqtt_event_at) < 3 * self.sync_interval or status in ("paused", "stopped"))
        )
        if fresh and ev.get("position") is not None:
            p = float(ev["position"])
            if status == "playing" and session.mqtt_event_at is not None:
                p += max(0.0, now_t - session.mqtt_event_at)
            est = session.base + p * session.speed
            return round(max(session.base, min(est, cap)), 2)

        lag = STARTUP_LAG_SECONDS if status is not None else self.player_buffer_seconds
        pos = max(session.base, cap - lag * session.speed, min(session.last_pos, cap))
        return round(pos, 2)

    def _moved_elsewhere(self, session: PlaybackSession) -> bool:
        """Detects whether ABS progress was updated externally (e.g. phone app) since our last write."""
        if session.open_streams > 0 or session.own_place or not clients.abs_client:
            return False
        prog = clients.abs_client.get_media_progress(session.abs_item_id, token=session.abs_token) or {}
        if not prog:
            return False
        newer = (float(prog.get("lastUpdate") or 0.0) / 1000.0) > (session.last_write_wall + 2.0)
        moved = abs(float(prog.get("currentTime") or 0.0) - session.last_pos) > 2.0
        return bool(newer and moved)

    def sync_session(self, session: PlaybackSession, force: bool = False) -> bool:
        """Pushes current estimated book position and listened delta to Audiobookshelf (or local own_place store)."""
        pos = self.compute_position(session)
        if not force and abs(pos - session.last_pos) < 1.0:
            return False

        if session.own_place:
            ok_local = patch_effective_item_progress(
                {"slot_id": session.slot_id, "own_place": True},
                session.abs_item_id,
                pos,
                session.book_duration,
                is_finished=False,
            )
            if ok_local:
                with self._lock:
                    session.last_pos = pos
                    session.last_sync_at = time.monotonic()
                    session.last_write_wall = time.time()
            return ok_local

        if not clients.abs_client:
            return False

        if self._moved_elsewhere(session):
            logger.info("%s moved on another client; dropping Yoto sync session without overwriting", session.title)
            with self._lock:
                self._sessions.pop(session.slot_id, None)
            return False

        now_t = time.monotonic()
        listened = max(0.0, min((pos - session.last_pos) / max(0.1, session.speed), max(0.0, now_t - session.last_sync_at)))

        ok = False
        status = 0
        if session.abs_session_id:
            ok, status = clients.abs_client.sync_playback_session(
                session.abs_session_id,
                pos,
                listened,
                session.book_duration,
                token=session.abs_token
            )
        if not session.abs_session_id or status == 404:
            ok = clients.abs_client.patch_media_progress(
                session.abs_item_id,
                pos,
                session.book_duration,
                token=session.abs_token
            )

        if ok:
            with self._lock:
                session.last_pos = pos
                session.last_sync_at = now_t
                session.last_write_wall = time.time()
            try:
                database.record_playback(session.abs_item_id, session.owner_profile_id or "default")
            except Exception:
                pass
        return bool(ok)

    def finish_session(self, slot_id: str, reason: str = "stream closed") -> bool:
        """Closes a finished/idle stream session in Audiobookshelf (or own_place local store) and prepares warm start."""
        with self._lock:
            session = self._sessions.pop(slot_id, None)
        if not session:
            return False

        pos = self.compute_position(session)
        is_fin = bool(session.book_duration > 0 and pos >= session.book_duration - min(30.0, 0.02 * session.book_duration))

        if session.own_place:
            patch_effective_item_progress(
                {"slot_id": session.slot_id, "own_place": True},
                session.abs_item_id,
                session.book_duration if is_fin else pos,
                session.book_duration,
                is_finished=is_fin,
            )
            return True

        if not clients.abs_client:
            return True
        if self._moved_elsewhere(session):
            return False

        now_t = time.monotonic()
        listened = max(0.0, min((pos - session.last_pos) / max(0.1, session.speed), max(0.0, now_t - session.last_sync_at)))

        closed = False
        if session.abs_session_id:
            closed = clients.abs_client.close_playback_session(
                session.abs_session_id,
                pos,
                listened,
                session.book_duration,
                token=session.abs_token
            )
        if not closed:
            clients.abs_client.patch_media_progress(
                session.abs_item_id,
                pos,
                session.book_duration,
                token=session.abs_token
            )

        if is_fin:
            clients.abs_client.patch_media_progress(
                session.abs_item_id,
                session.book_duration,
                session.book_duration,
                is_finished=True,
                token=session.abs_token
            )
        logger.debug("Finished ABS playback session for %s at %.1fs (%s)", session.title, pos, reason)
        self.schedule_warm_start(slot_id, session.abs_item_id, pos, session.book_duration, session.speed, session.abs_token)
        return True

    def schedule_warm_start(
        self,
        slot_id: str,
        abs_item_id: str,
        pos: float,
        duration: float,
        speed: float = 1.0,
        abs_token: Optional[str] = None,
    ):
        """Pre-encodes the first 5 seconds of audio at the new resume position in the background."""
        if not slot_id or not abs_item_id or not clients.abs_client:
            return
        resume_start = 0.0 if (duration > 0 and pos >= duration - 10.0) else (max(0.0, pos - DEFAULT_REWIND_SECONDS) if pos > DEFAULT_REWIND_SECONDS else 0.0)

        def _warm_worker():
            try:
                from .manager import get_bridge_manager
                item_data = clients.abs_client.get_item(abs_item_id, token=abs_token)
                if not item_data:
                    return
                sources, auth_hdr = get_bridge_manager()._build_abs_audio_sources(abs_item_id, item_data, abs_token_override=abs_token)
                if not sources:
                    return
                streamer = AudioStreamer(bitrate_kbps=64, channels=1, sample_rate=44100)
                audio = streamer.preencode_warm_start(
                    sources,
                    start_sec=resume_start,
                    warm_seconds=DEFAULT_WARM_START_SECONDS,
                    auth_header=auth_hdr,
                    speed=speed,
                )
                if audio:
                    with self._lock:
                        self._warm[slot_id] = WarmBuffer(
                            abs_item_id=abs_item_id,
                            start_sec=resume_start,
                            speed=speed,
                            audio=audio,
                        )
            except Exception as e:
                logger.debug("Warm start pre-encode notice for %s: %s", slot_id, e)

        threading.Thread(target=_warm_worker, name=f"WarmStart-{slot_id}", daemon=True).start()

    def on_mqtt_event(self, device_id: str, ev: Dict[str, Any]):
        """Correlates embedded MQTT EventPatch/StatusPatch updates with active streaming & offline sessions."""
        try:
            from .offline import get_offline_manager
            get_offline_manager().on_mqtt_event(device_id, ev)
        except Exception as e:
            logger.debug("Offline MQTT event hook notice: %s", e)

        card_id = ev.get("cardId")
        matched: List[PlaybackSession] = []
        now_t = time.monotonic()
        status = str(ev.get("playbackStatus") or "").lower()

        with self._lock:
            for sess in self._sessions.values():
                if (card_id and sess.yoto_card_id and card_id == sess.yoto_card_id) or sess.device_id == device_id:
                    if not sess.device_id:
                        sess.device_id = device_id
                    if status in ("paused", "stopped") and sess.paused_at is None:
                        sess.pause_pos = self.compute_position(sess, now_mono=now_t)
                        sess.paused_at = now_t
                    elif status == "playing" and sess.paused_at is not None:
                        sess.paused_total += max(0.0, now_t - max(sess.paused_at, sess.started_at))
                        sess.last_pause_duration = max(0.0, now_t - sess.paused_at)
                        sess.paused_at = None
                        if sess.open_streams > 0:
                            sess.pause_pos = None
                    sess.mqtt_event = dict(ev)
                    sess.mqtt_event_at = now_t
                    matched.append(sess)

        for sess in matched:
            if status in ("paused", "stopped"):
                try:
                    self.sync_session(sess, force=True)
                except Exception as e:
                    logger.debug("MQTT pause/stop sync notice on %s: %s", sess.slot_id, e)
                if status == "stopped" and sess.open_streams == 0 and not card_id:
                    self.finish_session(sess.slot_id, "player stopped")

    def tick(self):
        """Periodic maintenance: finalizes expired buffered streams and syncs active sessions."""
        with self._lock:
            active_list = list(self._sessions.values())

        now_t = time.monotonic()
        for sess in active_list:
            try:
                if sess.open_streams == 0 and sess.ended_at is not None:
                    idle = now_t - sess.ended_at
                    status = str(sess.mqtt_event.get("playbackStatus") or "").lower()
                    rate = sess.byterate / max(0.1, sess.speed)
                    played = now_t - sess.started_at - sess.paused_total
                    if sess.paused_at is not None:
                        played -= (now_t - max(sess.paused_at, sess.started_at))
                    buffered = (sess.bytes_sent / rate) - played
                    if idle > self.end_grace_seconds and (status == "stopped" or (buffered < -self.end_grace_seconds and status != "playing")):
                        self.finish_session(sess.slot_id, "player stopped" if status == "stopped" else "stream closed")
                        continue
                self.sync_session(sess)
            except Exception as e:
                logger.debug("PlaybackManager tick notice for %s: %s", sess.slot_id, e)

    def start_sync_loop(self) -> bool:
        """Starts the periodic background ABS progress sync thread."""
        with self._lock:
            if self._sync_thread and self._sync_thread.is_alive():
                return False
            self._stop_event.clear()
            self._sync_thread = threading.Thread(target=self._loop, name="PlaybackProgressSync", daemon=True)
            self._sync_thread.start()
            return True

    def stop_sync_loop(self):
        """Signals the background ABS progress sync thread to stop."""
        self._stop_event.set()

    def _loop(self):
        while not self._stop_event.wait(self.sync_interval):
            try:
                self.tick()
            except Exception as e:
                logger.debug("PlaybackManager sync loop error: %s", e)

    def get_active_for_device(self, device_id: str) -> Optional[PlaybackSession]:
        """Returns the most recently active PlaybackSession for device_id, if any."""
        with self._lock:
            mine = [s for s in self._sessions.values() if s.device_id == device_id]
        if not mine:
            return None
        return max(mine, key=lambda s: s.started_at)

    def get_active_sessions_summary(self) -> List[Dict[str, Any]]:
        """Returns JSON-serializable summary of all active streaming sessions."""
        with self._lock:
            sessions = list(self._sessions.values())
        return [
            {
                "slot_id": s.slot_id,
                "abs_item_id": s.abs_item_id,
                "title": s.title,
                "device_id": s.device_id,
                "owner_profile_id": s.owner_profile_id,
                "track_idx": s.track_idx,
                "position": self.compute_position(s),
                "duration": s.book_duration,
                "speed": s.speed,
                "open_streams": s.open_streams,
                "own_place": s.own_place,
            }
            for s in sessions
        ]


_playback_manager: Optional[PlaybackManager] = None


def get_playback_manager() -> PlaybackManager:
    """Returns the global singleton PlaybackManager instance."""
    global _playback_manager
    if _playback_manager is None:
        _playback_manager = PlaybackManager()
    return _playback_manager

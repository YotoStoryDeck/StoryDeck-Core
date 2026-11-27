"""
Streaming Playback Sync, Warm-Start Scheduling & MQTT Telemetry Mixin.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import clients
import database
from .mqtt_client import get_mqtt_bridge
from .series import (
    get_effective_item_progress,
    patch_effective_item_progress,
    resolve_slot_series_chain,
)
from .streamer import AudioStreamer, AudioTrackSource

logger = logging.getLogger("storydeck.bridge.playback.sync")

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


class PlaybackSyncMixin:
    """Position estimation, ABS progress syncing, warm-start pre-encoding, and MQTT event handling."""

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


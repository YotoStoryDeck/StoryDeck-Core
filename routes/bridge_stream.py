"""
StoryDeck Core Audio Streaming & Chapter Icon Blueprint (/s/*).
Serves constant-bitrate MP3 streams with HTTP 206 Range requests and 16x16 PNG icons for Yoto Players.
"""
from __future__ import annotations
import io
import re
import logging
from typing import List
from flask import Blueprint, request, Response, jsonify
import database
import clients
from core.config import CUSTOM_ICONS_DIR
from core.version_service import get_installed_version
from core.bridge.icons import render_track_icon_png
from core.bridge.manager import get_bridge_manager
from core.bridge.streamer import AudioStreamer, AudioTrackSource
from core.bridge.playback import get_playback_manager

logger = logging.getLogger("storydeck.routes.bridge_stream")
bridge_stream_bp = Blueprint("bridge_stream", __name__)


@bridge_stream_bp.route("/s/health", methods=["GET"])
def health_check():
    """Health check endpoint for reverse proxy, deploy scripts, and Uptime Kuma."""
    return jsonify({
        "status": "healthy",
        "service": "storydeck-core",
        "version": get_installed_version()
    }), 200


@bridge_stream_bp.route("/s/audio/<slot_id>/<int:track_idx>", methods=["GET", "HEAD"])
def stream_track_audio(slot_id: str, track_idx: int):
    """
    Streams a constant-bitrate MP3 chapter track with HTTP 206 Range request support,
    tap-time ABS / own_place resume, pause-without-Range resume, and silence + MQTT chapter auto-jump.
    """
    mgr = get_bridge_manager()
    slot = mgr.get_slot(slot_id)
    if not slot:
        return Response("Slot not found", status=404)

    track = mgr.get_track(slot_id, track_idx)
    if not track:
        return Response("Track not found", status=404)

    speed = float(slot.get("speed") or 1.0)
    streamer = AudioStreamer(bitrate_kbps=64, channels=1, sample_rate=44100)
    pb_mgr = get_playback_manager()
    range_header = request.headers.get("Range")
    is_head = (request.method == "HEAD")

    # Resolve tap-time resume position, pause-without-Range resume, or MQTT track jump (when starting from byte 0)
    has_nonzero_range = False
    if range_header:
        m_pre = re.match(r"bytes=(\d+)-", range_header)
        if m_pre and int(m_pre.group(1)) > 0:
            has_nonzero_range = True
        elif range_header.startswith("bytes=-"):
            has_nonzero_range = True

    resolved = (
        pb_mgr.resolve_track_request(
            slot,
            track,
            probe=is_head,
            device_id=request.args.get("device_id", ""),
        )
        if not has_nonzero_range
        else {
            "start_time": float(track["start_time"]),
            "end_time": float(track["end_time"]),
            "item_id": str(track.get("abs_item_id") or slot.get("abs_item_id") or ""),
        }
    )

    if "jump" in resolved:
        silence = streamer.generate_silence(seconds=2.5)
        headers = {
            "Content-Type": "audio/mpeg",
            "Content-Length": str(len(silence)),
            "Accept-Ranges": "bytes",
            "Cache-Control": "no-store",
        }
        if is_head:
            return Response(status=200, headers=headers)
        if not resolved.get("pending"):
            pb_mgr.trigger_mqtt_track_jump(slot, int(resolved["jump"]), device_id=str(resolved.get("device_id") or ""))
        return Response(silence, status=200, headers=headers)

    start_sec = float(resolved.get("start_time", track["start_time"]))
    end_sec = float(resolved.get("end_time", track["end_time"]))
    duration = max(0.1, end_sec - start_sec)
    total_bytes = streamer.seconds_to_bytes(duration, speed)

    if is_head:
        return Response(
            status=200,
            headers={
                "Content-Type": "audio/mpeg",
                "Accept-Ranges": "bytes",
                "Content-Length": str(total_bytes),
                "Cache-Control": "no-store",
            },
        )

    abs_item_id = str(resolved.get("item_id") or track.get("abs_item_id") or slot.get("abs_item_id") or "")
    if not abs_item_id or not clients.abs_client:
        return Response("Audiobookshelf item not available", status=503)

    owner_user_id = str(slot.get("owner_user_id") or "").strip()
    user_token = database.get_user_abs_credentials(owner_user_id).get("abs_api_key", "") if owner_user_id else ""
    owner_prof = str(slot.get("owner_profile_id") or "").strip()
    if not user_token and owner_prof and clients.abs_client:
        user_token = clients.abs_client.resolve_token_for_profile(owner_prof) or ""

    try:
        item_data = clients.abs_client.get_item(abs_item_id, token=user_token or None) if user_token else clients.abs_client.get_item(abs_item_id)
    except Exception as e:
        logger.error("Failed to fetch ABS item for stream %s/%d: %s", slot_id, track_idx, e)
        return Response("Audio source unavailable", status=502)

    sources, auth_hdr = mgr._build_abs_audio_sources(abs_item_id, item_data, abs_token_override=user_token or None)
    if not sources and user_token:
        # Fallback to master ABS API key if the user's scoped key lacks raw audioFiles/tracks permission
        try:
            master_item = clients.abs_client.get_item(abs_item_id)
            sources, auth_hdr = mgr._build_abs_audio_sources(abs_item_id, master_item, abs_token_override=None)
        except Exception as e:
            logger.debug("Master key fallback get_item notice for %s: %s", abs_item_id, e)

    if not sources:
        logger.warning("No playable audio sources resolved in ABS item %s for slot %s/%d", abs_item_id, slot_id, track_idx)
        return Response("No audio files in book", status=404)

    byte_start = 0
    byte_end = total_bytes - 1

    if range_header:
        m = re.match(r"bytes=(\d+)-(\d*)", range_header)
        if m:
            byte_start = int(m.group(1))
            if m.group(2):
                byte_end = min(total_bytes - 1, int(m.group(2)))

    if byte_start >= total_bytes:
        return Response(status=416, headers={"Content-Range": f"bytes */{total_bytes}"})

    content_length = byte_end - byte_start + 1
    seek_sec = streamer.bytes_to_seconds(byte_start, speed)
    req_start_sec = start_sec + seek_sec
    req_end_sec = min(end_sec, start_sec + streamer.bytes_to_seconds(byte_end + 1, speed))

    pb_session = pb_mgr.open_stream(
        slot,
        track,
        byte_start=byte_start,
        speed=speed,
        byterate=streamer.byterate,
        device_id=request.args.get("device_id", ""),
        override_start_sec=start_sec,
    )

    def generate_chunks():
        bytes_sent = 0
        stream_from_sec = req_start_sec
        try:
            # Deliver pre-encoded warm start bytes immediately if available at byte 0
            if pb_session.warm_bytes and byte_start == 0:
                warm_take = min(len(pb_session.warm_bytes), content_length)
                if warm_take > 0:
                    warm_chunk = pb_session.warm_bytes[:warm_take]
                    yield warm_chunk
                    bytes_sent += len(warm_chunk)
                    pb_mgr.record_bytes(pb_session, len(warm_chunk))
                    stream_from_sec += streamer.bytes_to_seconds(len(warm_chunk), speed)

            if bytes_sent < content_length and stream_from_sec < req_end_sec:
                for chunk in streamer.stream_segment(sources, stream_from_sec, req_end_sec, auth_header=auth_hdr, speed=speed):
                    remaining = content_length - bytes_sent
                    if remaining <= 0:
                        break
                    if len(chunk) > remaining:
                        out_chunk = chunk[:remaining]
                        yield out_chunk
                        bytes_sent += remaining
                        pb_mgr.record_bytes(pb_session, len(out_chunk))
                        break
                    yield chunk
                    bytes_sent += len(chunk)
                    pb_mgr.record_bytes(pb_session, len(chunk))
        finally:
            pb_mgr.close_stream(pb_session, force_sync=True)

    status_code = 206 if range_header else 200
    headers = {
        "Content-Type": "audio/mpeg",
        "Accept-Ranges": "bytes",
        "Content-Length": str(content_length),
        "Cache-Control": "no-cache",
    }
    if range_header:
        headers["Content-Range"] = f"bytes {byte_start}-{byte_end}/{total_bytes}"

    return Response(generate_chunks(), status=status_code, headers=headers)


@bridge_stream_bp.route("/s/stream/<slot_id>", methods=["GET"])
def stream_slot_legacy(slot_id: str):
    """Legacy whole-book stream endpoint, forwarding to track 1."""
    return stream_track_audio(slot_id, 1)


@bridge_stream_bp.route("/s/icon/<slot_id>.png", methods=["GET"])
@bridge_stream_bp.route("/s/icon/<slot_id>/<int:track_idx>.png", methods=["GET"])
def serve_track_icon(slot_id: str, track_idx: int = 1):
    """Serves a 16x16 pixel art PNG icon (custom chapter art or procedural cover + chapter number) for Yoto Player display."""
    mgr = get_bridge_manager()
    slot = mgr.get_slot(slot_id)
    track = mgr.get_track(slot_id, track_idx) if slot else None
    abs_item_id = (track.get("abs_item_id") if track else None) or (slot.get("abs_item_id") if slot else "") or ""

    fraction = None
    if track and track.get("book_duration"):
        b_dur = float(track["book_duration"] or 0.0)
        if b_dur > 0:
            fraction = min(1.0, max(0.0, float(track.get("start_time") or 0.0) / b_dur))

    png_bytes = render_track_icon_png(abs_item_id, track_idx=track_idx, fraction=fraction)
    return Response(png_bytes, mimetype="image/png", headers={"Cache-Control": "max-age=3600"})

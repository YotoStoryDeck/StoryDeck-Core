"""
StoryDeck Core Offline Transfer, Mode Switching & Card Content Assignment Blueprint.
Handles /api/cards/<slot_id>/mode, /transfer, /retry, and /api/cards/assign.
"""
from __future__ import annotations

import logging
from flask import Blueprint, request, jsonify
import database
import clients
from core.auth import require_auth, require_member, get_current_user
from core.bridge.icons import upload_abs_cover_to_yoto
from core.bridge.manager import get_bridge_manager
from core.bridge.offline import get_offline_manager, normalize_offline_submode
from core.bridge.series import patch_effective_item_progress, resolve_series_start_book
from routes.bridge_cards import _attach_transcode_status, _with_icon_urls

logger = logging.getLogger("storydeck.routes.bridge_offline")
bridge_offline_bp = Blueprint("bridge_offline", __name__)


@bridge_offline_bp.route("/api/cards/<slot_id>/mode", methods=["POST"])
@require_member
def set_card_delivery_mode(slot_id: str):
    """Switches an existing card slot between 'streaming' and 'offline' ('next' 5h window, 'book', or 'series') delivery modes."""
    data = request.get_json() or {}
    raw_mode = (data.get("mode") or "streaming").strip().lower()
    valid_modes = ("streaming", "offline", "offline_next", "offline_book", "offline_series", "next", "book", "series", "5h", "hours")
    if raw_mode not in valid_modes:
        return jsonify({"error": "mode must be 'streaming', 'offline', 'offline_next', 'offline_book', or 'offline_series'"}), 400

    base_url = request.host_url.rstrip("/")
    mgr = get_bridge_manager()
    slot = mgr.get_slot(slot_id)
    if not slot:
        return jsonify({"error": "Slot not found"}), 404

    target_mode = "streaming" if raw_mode == "streaming" else "offline"
    submode = normalize_offline_submode(raw_mode, slot) if target_mode == "offline" else (slot.get("offline_mode") or "book")
    hours = float(data.get("hours") or slot.get("offline_hours") or 5.0)
    new_group_kind = "series" if submode == "series" else (slot.get("group_kind") or "book")

    conn = database.get_db_connection()
    try:
        with conn:
            if "series_packing" in data and data["series_packing"] is not None:
                sp = str(data["series_packing"]).strip().lower()
                sp_val = sp if sp in ("chapters", "condense") else ""
                conn.execute(
                    "UPDATE bridge_slots SET mode = ?, offline_mode = ?, offline_hours = ?, group_kind = ?, series_packing = ?, updated_at = CURRENT_TIMESTAMP WHERE slot_id = ?;",
                    (target_mode, submode, hours, new_group_kind, sp_val, slot_id),
                )
            else:
                conn.execute(
                    "UPDATE bridge_slots SET mode = ?, offline_mode = ?, offline_hours = ?, group_kind = ?, updated_at = CURRENT_TIMESTAMP WHERE slot_id = ?;",
                    (target_mode, submode, hours, new_group_kind, slot_id),
                )
    finally:
        conn.close()

    slot = mgr.get_slot(slot_id) or slot
    off_mgr = get_offline_manager()
    if target_mode == "offline":
        job = off_mgr.queue_transcode(slot_id, base_url=base_url, offline_mode=submode, hours=hours, replace_existing=True)
        slot["transcode"] = job
    elif target_mode == "streaming":
        off_mgr.cancel_job(slot_id)
        if clients.yoto_client and slot.get("abs_item_id"):
            try:
                yoto_cid = slot.get("yoto_card_id") or None
                fallback_cover = f"{base_url}/api/cover/{slot['abs_item_id']}"
                cover_url = upload_abs_cover_to_yoto(slot["abs_item_id"], fallback_url=fallback_cover)
                saved_id = clients.yoto_client.save_stream_playlist(
                    title=slot.get("series_name") if slot.get("group_kind") == "series" and slot.get("series_name") else slot.get("title", "Audiobook"),
                    tracks=_with_icon_urls(slot.get("tracks", []), slot_id, base_url),
                    card_id=yoto_cid,
                    cover_url=cover_url,
                    description=f"Streams {slot.get('title', '')} from Audiobookshelf"
                )
                if saved_id and saved_id != slot.get("yoto_card_id"):
                    slot["yoto_card_id"] = saved_id
                    with database.get_db_connection() as conn:
                        conn.execute("UPDATE bridge_slots SET yoto_card_id = ? WHERE slot_id = ?;", (saved_id, slot_id))
                        conn.execute("DELETE FROM media_items WHERE id = ? AND type IN ('yoto', 'yoto_myo');", (saved_id,))
                        conn.commit()
            except Exception as e:
                logger.warning("Failed to switch slot %s to streaming playlist: %s", slot_id, e)

    return jsonify({"success": True, "slot_id": slot_id, "mode": target_mode, "offline_mode": submode, "slot": slot})


@bridge_offline_bp.route("/api/cards/<slot_id>/transfer", methods=["POST"])
@require_member
def control_card_transfer(slot_id: str):
    """Pauses, stops, cancels, or starts/resumes an offline card transfer."""
    data = request.get_json(silent=True) or {}
    action = str(data.get("action") or "").strip().lower()
    if action not in ("pause", "stop", "cancel", "start", "resume"):
        return jsonify({"error": "action must be 'pause', 'stop', 'cancel', 'start', or 'resume'"}), 400

    mgr = get_bridge_manager()
    slot = mgr.get_slot(slot_id)
    if not slot:
        return jsonify({"error": "Slot not found"}), 404
    if not (slot.get("abs_item_id") or "").strip():
        return jsonify({"error": "Slot has no Audiobookshelf book assigned."}), 400

    off_mgr = get_offline_manager()
    if action == "pause":
        job = off_mgr.pause_job(slot_id)
    elif action == "stop":
        job = off_mgr.stop_job(slot_id)
    elif action == "cancel":
        job = off_mgr.cancel_job(slot_id)
    else:
        job = off_mgr.resume_job(slot_id, base_url=request.host_url.rstrip("/"))
    return jsonify({"success": True, "slot_id": slot_id, "action": action, "transcode": job})


@bridge_offline_bp.route("/api/cards/<slot_id>/retry", methods=["POST"])
@require_member
def retry_card_sync(slot_id: str):
    """Retries a failed or incomplete offline transcode/upload or streaming playlist push for a card slot."""
    mgr = get_bridge_manager()
    slot = mgr.get_slot(slot_id)
    if not slot:
        return jsonify({"error": "Slot not found"}), 404
    if not (slot.get("abs_item_id") or "").strip():
        return jsonify({"error": "Slot has no Audiobookshelf book assigned to retry."}), 400

    base_url = request.host_url.rstrip("/")
    if slot.get("mode") == "offline":
        job = get_offline_manager().resume_job(slot_id, base_url=base_url)
        return jsonify({"success": True, "slot_id": slot_id, "mode": "offline", "transcode": job})

    if not clients.yoto_client:
        return jsonify({"error": "Yoto client is not configured"}), 503

    try:
        yoto_cid = slot.get("yoto_card_id") or None
        fallback_cover = f"{base_url}/api/cover/{slot['abs_item_id']}"
        cover_url = upload_abs_cover_to_yoto(slot["abs_item_id"], fallback_url=fallback_cover)
        saved_id = clients.yoto_client.save_stream_playlist(
            title=slot.get("series_name") if slot.get("group_kind") == "series" and slot.get("series_name") else slot.get("title", "Audiobook"),
            tracks=_with_icon_urls(slot.get("tracks", []), slot_id, base_url),
            card_id=yoto_cid,
            cover_url=cover_url,
            description=f"Streams {slot.get('title', '')} from Audiobookshelf",
        )
        if saved_id and saved_id != slot.get("yoto_card_id"):
            slot["yoto_card_id"] = saved_id
            with database.get_db_connection() as conn:
                conn.execute("UPDATE bridge_slots SET yoto_card_id = ? WHERE slot_id = ?;", (saved_id, slot_id))
                conn.execute("DELETE FROM media_items WHERE id = ? AND type IN ('yoto', 'yoto_myo');", (saved_id,))
                conn.commit()
        return jsonify({"success": True, "slot_id": slot_id, "mode": "streaming", "slot": mgr.get_slot(slot_id) or slot})
    except Exception as e:
        return jsonify({"error": f"Failed to push streaming playlist to Yoto: {e}"}), 500



@bridge_offline_bp.route("/api/cards/assign", methods=["POST"])
@require_auth
def assign_card_content():
    """Assigns an ABS audiobook or Official Yoto Card to a profile primary or fleet card slot."""
    data = request.get_json() or {}
    abs_item_id = (data.get("abs_item_id") or "").strip()
    yoto_card_id = (data.get("yoto_card_id") or "").strip()
    if not abs_item_id and not yoto_card_id:
        return jsonify({"error": "abs_item_id or yoto_card_id is required"}), 400

    profile_id = data.get("profile_id")
    slot_id = data.get("slot_id")
    use_user_abs = bool(data.get("use_user_abs", False)) or (profile_id == "__all__")
    mode_override = str(data.get("mode") or "default").strip().lower()
    group_kind = "series" if str(data.get("group_kind") or "").strip().lower() == "series" or mode_override in ("offline_series", "series") else "book"
    series_name = str(data.get("series_name") or "").strip()
    series_start = str(data.get("series_start") or "selected").strip().lower()
    if series_start not in ("selected", "active", "beginning"):
        series_start = "selected"
    req_title = data.get("title", "")
    hours = float(data.get("hours") or 5.0)
    base_url = request.host_url.rstrip("/")

    mgr = get_bridge_manager()
    user_token = None

    # Validate that the selected ABS item actually contains playable audio files (not an ebook-only EPUB/PDF)
    abs_check = None
    if abs_item_id and clients.abs_client:
        abs_check = clients.abs_client.get_item(abs_item_id)
        if abs_check:
            chk_media = abs_check.get("media") or {}
            chk_dur = float(chk_media.get("duration") or 0.0)
            chk_files = len(chk_media.get("audioFiles") or []) + len(chk_media.get("tracks") or [])
            if chk_dur <= 0 and chk_files <= 0:
                eb_fmt = chk_media.get("ebookFormat") or "ebook"
                # Remove ebook-only item from local media_items cache so it doesn't clutter the audio library
                with database.get_db_connection() as conn:
                    conn.execute("DELETE FROM media_items WHERE id = ?;", (abs_item_id,))
                    conn.commit()
                return jsonify({
                    "error": f"'{chk_media.get('metadata', {}).get('title') or 'This item'}' is an {eb_fmt.upper()} eBook in Audiobookshelf with no audio files to play."
                }), 400

    # Resolve starting book when assigning a series ('selected' clicked book, 'active' unfinished, or 'beginning' Book 1)
    if group_kind == "series" and abs_item_id:
        if not series_name:
            db_item = database.get_media_item_by_id(abs_item_id) or {}
            series_name = str(db_item.get("series") or "").strip()
            if not series_name and abs_check:
                raw_ser = ((abs_check.get("media") or {}).get("metadata") or {}).get("seriesName") or ""
                series_name = str(raw_ser).split(",")[0].split("#")[0].strip()
        eff_token = None
        if use_user_abs:
            u = get_current_user() or {}
            if not u.get("is_lan_guest") and u.get("id"):
                eff_token = database.get_user_abs_credentials(u["id"]).get("abs_api_key") or None
        elif profile_id and clients.abs_client:
            eff_token = clients.abs_client.resolve_token_for_profile(profile_id)
        if series_name:
            resolved_id, resolved_title = resolve_series_start_book(
                series_name,
                abs_item_id,
                series_start=series_start,
                abs_token=eff_token,
            )
            if resolved_id and resolved_id != abs_item_id:
                abs_item_id = resolved_id
                req_title = resolved_title

    if use_user_abs:
        if not abs_item_id:
            return jsonify({"error": "abs_item_id is required"}), 400
        user = get_current_user() or {}
        if user.get("is_lan_guest") or not user.get("id"):
            # Unauthenticated / LAN Guest without an account: do not create a user account or persistent card;
            # create/reuse a single ephemeral streaming playlist slot ('guest_stream')
            slot = mgr.assign_abs_book_to_slot(
                slot_id="guest_stream",
                abs_item_id=abs_item_id,
                title=req_title,
                author=data.get("author", ""),
                series_name=series_name,
                group_kind=group_kind,
                owner_profile_id="",
                owner_user_id="",
                kind="guest_stream",
                mode="streaming",
                base_url=base_url,
                abs_token=None,
            )
        else:
            creds = database.get_user_abs_credentials(user["id"])
            user_token = creds.get("abs_api_key") or None
            slot = mgr.update_user_primary_card(
                user_id=user["id"],
                username=user.get("display_name") or user.get("username") or user["id"],
                abs_item_id=abs_item_id,
                title=req_title,
                author=data.get("author", ""),
                mode_override=mode_override,
                base_url=base_url,
                group_kind=group_kind,
                series_name=series_name,
                abs_token=user_token,
            )
    elif profile_id:
        if not abs_item_id:
            return jsonify({"error": "abs_item_id is required for profile primary card assignment"}), 400
        user = get_current_user() or {}
        if user.get("is_lan_guest") and not database.get_profile_by_id(profile_id):
            slot = mgr.assign_abs_book_to_slot(
                slot_id="guest_stream",
                abs_item_id=abs_item_id,
                title=req_title,
                author=data.get("author", ""),
                series_name=series_name,
                group_kind=group_kind,
                owner_profile_id="",
                owner_user_id="",
                kind="guest_stream",
                mode="streaming",
                base_url=base_url,
            )
        else:
            try:
                slot = mgr.update_profile_primary_card(
                    profile_id=profile_id,
                    abs_item_id=abs_item_id,
                    mode_override=mode_override,
                    base_url=base_url,
                    group_kind=group_kind,
                    series_name=series_name,
                )
            except Exception as e:
                return jsonify({"error": str(e)}), 400
    elif slot_id:
        slot = mgr.assign_to_fleet_card(
            slot_id=slot_id,
            abs_item_id=abs_item_id,
            yoto_card_id=yoto_card_id,
            title=req_title,
            author=data.get("author", ""),
            mode_override=mode_override,
            base_url=base_url,
            group_kind=group_kind,
            series_name=series_name,
        )
    else:
        return jsonify({"error": "Either profile_id, slot_id, or use_user_abs must be provided"}), 400

    slot_mode = slot.get("mode", "streaming")
    slot_key = slot.get("slot_id")
    submode = normalize_offline_submode(mode_override, slot)
    if "series_packing" in data and data["series_packing"] is not None and slot_key:
        sp = str(data["series_packing"]).strip().lower()
        sp_val = sp if sp in ("chapters", "condense") else ""
        with database.get_db_connection() as conn:
            conn.execute("UPDATE bridge_slots SET series_packing = ? WHERE slot_id = ?;", (sp_val, slot_key))
            conn.commit()
        slot["series_packing"] = sp_val
    playlist_title = (slot.get("series_name") if slot.get("group_kind") == "series" and slot.get("series_name") else slot.get("title")) or "Audiobook"

    if slot_mode == "offline":
        # If the slot does not have a Yoto Cloud cardId yet, create an initial streaming playlist
        # so the player can start playing immediately while background offline transcoding runs.
        if clients.yoto_client and not slot.get("yoto_card_id") and abs_item_id:
            try:
                fallback_cover = f"{base_url}/api/cover/{abs_item_id}"
                cover_url = upload_abs_cover_to_yoto(abs_item_id, fallback_url=fallback_cover, token=user_token)
                initial_cid = clients.yoto_client.save_stream_playlist(
                    title=playlist_title,
                    tracks=_with_icon_urls(slot.get("tracks", []), slot_key, base_url),
                    card_id=None,
                    cover_url=cover_url,
                    description=f"Streams {slot.get('title', '')} from Audiobookshelf (offline transcode in progress)"
                )
                if initial_cid:
                    slot["yoto_card_id"] = initial_cid
                    with database.get_db_connection() as conn:
                        conn.execute("UPDATE bridge_slots SET yoto_card_id = ? WHERE slot_id = ?;", (initial_cid, slot_key))
                        conn.execute("DELETE FROM media_items WHERE id = ? AND type IN ('yoto', 'yoto_myo');", (initial_cid,))
                        conn.commit()
            except Exception as e:
                logger.warning("Failed to create initial Yoto playlist for offline slot %s: %s", slot_key, e)
        anchor_cur = bool(group_kind == "series" and series_start in ("selected", "beginning"))
        job = get_offline_manager().queue_transcode(
            slot_key,
            base_url=base_url,
            offline_mode=submode,
            hours=hours,
            anchor_current_book=anchor_cur,
            replace_existing=True,
        )
        slot["transcode"] = job
    elif slot_mode == "streaming":
        if slot_key:
            get_offline_manager().cancel_job(slot_key)
        if clients.yoto_client and abs_item_id:
            try:
                yoto_cid = slot.get("yoto_card_id") or None
                fallback_cover = f"{base_url}/api/cover/{abs_item_id}"
                cover_url = upload_abs_cover_to_yoto(abs_item_id, fallback_url=fallback_cover, token=user_token)
                saved_id = clients.yoto_client.save_stream_playlist(
                    title=playlist_title,
                    tracks=_with_icon_urls(slot.get("tracks", []), slot_key, base_url),
                    card_id=yoto_cid,
                    cover_url=cover_url,
                    description=f"Streams {slot.get('title', '')} from Audiobookshelf"
                )
                if saved_id:
                    slot["yoto_card_id"] = saved_id
                    with database.get_db_connection() as conn:
                        conn.execute("UPDATE bridge_slots SET yoto_card_id = ? WHERE slot_id = ?;", (saved_id, slot_key))
                        conn.execute("DELETE FROM media_items WHERE id = ? AND type IN ('yoto', 'yoto_myo');", (saved_id,))
                        conn.commit()
            except Exception as e:
                logger.warning("Failed to push stream playlist to Yoto Cloud: %s", e)

    return jsonify(slot)


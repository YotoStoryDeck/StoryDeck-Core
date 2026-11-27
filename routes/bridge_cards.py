"""
StoryDeck Core Card Slot Inventory & Yoto Cloud Playlist Manager Blueprint (/api/cards*, /api/yoto/playlists*).
"""
from __future__ import annotations
import logging
from flask import Blueprint, request, jsonify
import database
import clients
from core.auth import require_auth, require_member, get_current_user
from core.bridge.chapter_finder import apply_proposed_chapters_to_item, lookup_audible_chapters_for_item
from core.bridge.icons import upload_abs_cover_to_yoto
from core.bridge.manager import get_bridge_manager
from core.bridge.offline import get_offline_manager, normalize_offline_submode, prune_evicted_profile_playlists
from core.bridge.series import patch_effective_item_progress, resolve_series_start_book
from core.bridge.transfer import analyse_myo_card_for_abs_transfer

logger = logging.getLogger("storydeck.routes.bridge_cards")
bridge_cards_bp = Blueprint("bridge_cards", __name__)


def _with_icon_urls(tracks: list, slot_id: str, base_url: str) -> list:
    if not base_url:
        return tracks
    root = base_url.rstrip("/")
    out = []
    for t in (tracks or []):
        td = dict(t)
        td.setdefault("icon_url", f"{root}/s/icon/{slot_id}/{int(td.get('track_idx') or 1)}.png")
        out.append(td)
    return out


@bridge_cards_bp.route("/api/cards", methods=["GET"])
@require_member
def list_cards():
    """Lists all registered card slots with active transcode and offline status."""
    mgr = get_bridge_manager()
    slots = mgr.list_slots()
    off_mgr = get_offline_manager()

    for s in slots:
        job = off_mgr.get_job_status(s["slot_id"])
        if job:
            s["transcode"] = job

    return jsonify(slots)


@bridge_cards_bp.route("/api/cards", methods=["POST"])
@require_member
def create_card():
    """Registers a new card slot."""
    data = request.get_json() or {}
    slot_id = data.get("slot_id") or data.get("id")
    if not slot_id:
        return jsonify({"error": "slot_id is required"}), 400

    mgr = get_bridge_manager()
    slot = mgr.assign_abs_book_to_slot(
        slot_id=slot_id,
        abs_item_id=data.get("abs_item_id", ""),
        title=data.get("title", ""),
        author=data.get("author", ""),
        owner_profile_id=data.get("owner_profile_id", ""),
        kind=data.get("kind", "permanent_myo"),
        mode=data.get("mode", "streaming")
    )
    return jsonify(slot), 201


@bridge_cards_bp.route("/api/cards/<slot_id>", methods=["GET"])
@require_member
def get_card(slot_id: str):
    """Retrieves slot details and track layout."""
    mgr = get_bridge_manager()
    slot = mgr.get_slot(slot_id)
    if not slot:
        return jsonify({"error": "Slot not found"}), 404

    job = get_offline_manager().get_job_status(slot_id)
    if job:
        slot["transcode"] = job
    return jsonify(slot)


@bridge_cards_bp.route("/api/cards/<slot_id>", methods=["PUT"])
@require_member
def update_card(slot_id: str):
    """Updates slot settings."""
    data = request.get_json() or {}
    mgr = get_bridge_manager()
    slot = mgr.get_slot(slot_id)
    if not slot:
        return jsonify({"error": "Slot not found"}), 404

    if "speed" in data:
        mgr.set_slot_speed(slot_id, float(data["speed"]))
    if "own_place" in data:
        mgr.set_slot_own_place(slot_id, bool(data["own_place"]))
    if "is_pinned" in data:
        mgr.set_slot_pin(slot_id, bool(data["is_pinned"]))

    # Rename, re-pair (yoto_card_id), custom StoryDeck ID (new_slot_id), and/or move Personal <-> Fleet
    new_slot_id = str(data.get("new_slot_id") or "").strip()
    if new_slot_id and new_slot_id != slot_id:
        if mgr.get_slot(new_slot_id):
            return jsonify({"error": f"Card ID '{new_slot_id}' is already in use."}), 409
        with database.get_db_connection() as conn:
            conn.execute("UPDATE bridge_slots SET slot_id = ? WHERE slot_id = ?;", (new_slot_id, slot_id))
            conn.execute("UPDATE bridge_tracks SET slot_id = ? WHERE slot_id = ?;", (new_slot_id, slot_id))
            conn.commit()
        slot_id = new_slot_id

    updates = {}
    if "nickname" in data and data["nickname"] is not None:
        updates["nickname"] = str(data["nickname"]).strip()
    if "title" in data and data["title"] is not None:
        updates["title"] = str(data["title"]).strip()
    if "yoto_card_id" in data and data["yoto_card_id"] is not None:
        updates["yoto_card_id"] = str(data["yoto_card_id"]).strip()
    if "series_packing" in data and data["series_packing"] is not None:
        sp = str(data["series_packing"]).strip().lower()
        updates["series_packing"] = sp if sp in ("chapters", "condense") else ""
    if "kind" in data and data["kind"] in ("profile_primary", "permanent_myo"):
        updates["kind"] = data["kind"]
        if data["kind"] == "permanent_myo":
            updates["owner_profile_id"] = None
        elif "owner_profile_id" in data:
            updates["owner_profile_id"] = str(data.get("owner_profile_id") or "").strip() or None
    elif "owner_profile_id" in data:
        updates["owner_profile_id"] = str(data.get("owner_profile_id") or "").strip() or None

    if updates:
        old_owner = str(slot.get("owner_profile_id") or "").strip()
        old_yoto_cid = str(slot.get("yoto_card_id") or "").strip()
        yoto_cid = updates.get("yoto_card_id", old_yoto_cid)
        set_clause = ", ".join(f"{k} = ?" for k in updates.keys())
        params = list(updates.values()) + [slot_id]
        with database.get_db_connection() as conn:
            conn.execute(f"UPDATE bridge_slots SET {set_clause} WHERE slot_id = ?;", params)
            conn.commit()
        new_owner = str((updates["owner_profile_id"] if "owner_profile_id" in updates else old_owner) or "").strip()
        if old_owner and old_owner != new_owner:
            old_prof = database.get_profile_by_id(old_owner)
            if old_prof and str(old_prof.get("primary_card_id") or "") == old_yoto_cid:
                p_upd = dict(old_prof)
                p_upd["primary_card_id"] = ""
                database.upsert_profile(old_owner, p_upd)
        if new_owner and updates.get("kind", slot.get("kind")) == "profile_primary" and yoto_cid:
            new_prof = database.get_profile_by_id(new_owner)
            if new_prof:
                p_upd = dict(new_prof)
                p_upd["primary_card_id"] = yoto_cid
                database.upsert_profile(new_owner, p_upd)

    return jsonify(mgr.get_slot(slot_id))


@bridge_cards_bp.route("/api/cards/<slot_id>", methods=["DELETE"])
@require_member
def delete_card(slot_id: str):
    """Deletes a card slot."""
    if get_bridge_manager().delete_slot(slot_id):
        return jsonify({"success": True})
    return jsonify({"error": "Slot not found"}), 404


@bridge_cards_bp.route("/api/cards/<slot_id>/speed", methods=["POST"])
@require_member
def set_card_speed(slot_id: str):
    """Updates playback speed multiplier for a card slot."""
    data = request.get_json() or {}
    speed = float(data.get("speed", 1.0))
    if get_bridge_manager().set_slot_speed(slot_id, speed):
        return jsonify({"success": True, "speed": speed})
    return jsonify({"error": "Slot not found"}), 404


@bridge_cards_bp.route("/api/cards/<slot_id>/own-place", methods=["POST"])
@require_member
def set_card_own_place(slot_id: str):
    """Toggles independent listening place for a card slot."""
    data = request.get_json() or {}
    own_place = bool(data.get("own_place", False))
    if get_bridge_manager().set_slot_own_place(slot_id, own_place):
        return jsonify({"success": True, "own_place": own_place})
    return jsonify({"error": "Slot not found"}), 404


@bridge_cards_bp.route("/api/cards/<slot_id>/mode", methods=["POST"])
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
    if target_mode == "offline":
        job = get_offline_manager().queue_transcode(slot_id, base_url=base_url, offline_mode=submode, hours=hours)
        slot["transcode"] = job
    elif target_mode == "streaming" and clients.yoto_client and slot.get("abs_item_id"):
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


@bridge_cards_bp.route("/api/cards/<slot_id>/pin", methods=["POST"])
@require_member
def toggle_card_pin(slot_id: str):
    """Toggles pin / cache eviction protection on a card slot."""
    data = request.get_json() or {}
    is_pinned = bool(data.get("is_pinned", True))
    if get_bridge_manager().set_slot_pin(slot_id, is_pinned):
        return jsonify({"success": True, "is_pinned": is_pinned})
    return jsonify({"error": "Slot not found"}), 404


@bridge_cards_bp.route("/api/cards/assign", methods=["POST"])
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
        )
        slot["transcode"] = job
    elif slot_mode == "streaming" and clients.yoto_client and abs_item_id:
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


@bridge_cards_bp.route("/api/yoto/playlists", methods=["GET"])
@require_member
def list_yoto_playlists():
    """Returns all Yoto Cloud user playlists classified by StoryDeck vs custom source and pin state."""
    if not clients.yoto_client:
        return jsonify([])

    raw_playlists = clients.yoto_client.get_all_cloud_playlists()
    mgr = get_bridge_manager()
    registered_slots = {s["slot_id"]: s for s in mgr.list_slots()}

    yoto_map = {}
    for s in registered_slots.values():
        y_cid = s.get("yoto_card_id")
        if y_cid:
            yoto_map[y_cid] = s

    enriched = []
    for p in raw_playlists:
        cid = p.get("cardId") or p.get("id") or ""
        title = p.get("title") or "Yoto Playlist"
        meta = p.get("metadata") or {}
        content = p.get("content") or {}
        desc = (meta.get("description") or "").lower()
        gen = (meta.get("generator") or "").lower()

        slot = registered_slots.get(cid) or yoto_map.get(cid)
        has_abs = bool(slot and str(slot.get("abs_item_id") or "").strip())
        is_broker = has_abs or "storydeck" in desc or "streams from audiobookshelf" in desc or gen == "storydeck-core"

        is_pinned = bool(slot.get("is_pinned", 0)) if slot else False
        is_protected = bool(slot.get("is_protected", 0)) if slot else False
        mode = slot.get("mode", "streaming") if (slot and is_broker) else "custom"
        owner_profile_id = slot.get("owner_profile_id") if slot else ""
        is_physical = bool(p.get("shareType") == "myo" or str(p.get("reason", "")).startswith("myo"))
        cover_url = meta.get("cover", {}).get("imageL") or content.get("cover", {}).get("imageL") or ""

        chapters_list = content.get("chapters") or []
        if len(chapters_list) > 1:
            chapters_count = len(chapters_list)
        elif len(chapters_list) == 1:
            tracks_in_ch = chapters_list[0].get("tracks") or []
            chapters_count = len(tracks_in_ch) if len(tracks_in_ch) > 1 else 1
        else:
            media_meta = meta.get("media") if isinstance(meta.get("media"), dict) else {}
            chapters_count = int(media_meta.get("trackCount") or p.get("trackCount") or 0)
            if not chapters_count and slot and isinstance(slot.get("tracks"), list):
                chapters_count = len(slot["tracks"])

        enriched.append({
            "card_id": cid,
            "title": title,
            "author": meta.get("author") or "Yoto",
            "source": "broker" if is_broker else "custom",
            "is_broker": is_broker,
            "is_pinned": is_pinned or is_protected,
            "is_physical": is_physical,
            "mode": mode,
            "owner_profile_id": owner_profile_id,
            "slot_id": slot.get("slot_id") if slot else None,
            "cover_url": cover_url,
            "chapters_count": chapters_count
        })

    enriched.sort(key=lambda x: (not x["is_broker"], x["title"].lower()))
    return jsonify(enriched)


@bridge_cards_bp.route("/api/yoto/playlists/<card_id>", methods=["DELETE"])
@require_member
def delete_yoto_playlist(card_id: str):
    """Manually deletes a playlist from Yoto Cloud and cascades removal of any linked local slot."""
    if not clients.yoto_client:
        return jsonify({"error": "Yoto client not configured"}), 503

    if not clients.yoto_client.delete_playlist(card_id):
        return jsonify({"error": "Failed to delete playlist from Yoto Cloud"}), 500

    mgr = get_bridge_manager()
    if mgr.get_slot(card_id):
        mgr.delete_slot(card_id)
    else:
        conn = database.get_db_connection()
        try:
            with conn:
                conn.execute("DELETE FROM bridge_slots WHERE yoto_card_id = ?;", (card_id,))
        finally:
            conn.close()
    conn = database.get_db_connection()
    try:
        with conn:
            conn.execute("DELETE FROM media_items WHERE id = ? AND type IN ('yoto', 'yoto_myo');", (card_id,))
    finally:
        conn.close()

    return jsonify({"success": True, "card_id": card_id})


@bridge_cards_bp.route("/api/yoto/playlists/<card_id>/pin", methods=["POST"])
@require_member
def toggle_playlist_pin(card_id: str):
    """Toggles pin / protection status for a playlist."""
    data = request.get_json() or {}
    is_pinned = bool(data.get("is_pinned", True))

    mgr = get_bridge_manager()
    matched = mgr.get_slot(card_id)
    if not matched:
        for s in (mgr.list_slots() or []):
            if str(s.get("yoto_card_id") or "") == card_id:
                matched = s
                break
    if matched:
        mgr.set_slot_pin(matched["slot_id"], is_pinned)
        return jsonify({"success": True, "card_id": card_id, "is_pinned": is_pinned})

    conn = database.get_db_connection()
    try:
        with conn:
            val = 1 if is_pinned else 0
            conn.execute("""
                INSERT INTO bridge_slots (slot_id, abs_item_id, yoto_card_id, kind, is_pinned, is_protected, updated_at)
                VALUES (?, '', ?, 'custom_playlist', ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(slot_id) DO UPDATE SET is_pinned = excluded.is_pinned, is_protected = excluded.is_protected;
            """, (card_id, card_id, val, val))
        return jsonify({"success": True, "card_id": card_id, "is_pinned": is_pinned})
    finally:
        conn.close()


@bridge_cards_bp.route("/api/yoto/playlists/prune", methods=["POST"])
@require_member
def trigger_lru_prune():
    """Manually triggers LRU pruning of evicted ephemeral profile playlists."""
    return jsonify(prune_evicted_profile_playlists())


@bridge_cards_bp.route("/api/cards/detect", methods=["GET"])
@require_member
def detect_inserted_card():
    """Polls live MQTT/player status for a Yoto Player and classifies any inserted card as Editable (MYO) vs Locked (Retail)."""
    device_id = (request.args.get("device_id") or "").strip()
    if not device_id:
        return jsonify({"ok": False, "error": "device_id is required"}), 400

    try:
        from core.player_service import get_player_status
        status = get_player_status(device_id) or {}
    except Exception as e:
        return jsonify({"ok": False, "error": f"Failed to read player status: {e}"}), 500

    card_id = str(status.get("card_id") or "").strip()
    if not card_id or card_id.lower() in ("none", "null"):
        return jsonify({
            "ok": True,
            "detected": False,
            "device_id": device_id,
            "online": bool(status.get("online")),
        }), 200

    yoto = clients.yoto_client
    if not yoto:
        return jsonify({"ok": False, "error": "Yoto client is not configured"}), 503

    # 1. Authoritative check: is card_id in the user's personal MYO playlists (/content/mine)?
    mine_ids = set()
    mine_title = None
    try:
        mine = yoto.get_all_cloud_playlists() or []
        for p in mine:
            if not isinstance(p, dict):
                continue
            pid = str(p.get("cardId") or p.get("id") or "").strip()
            if pid:
                mine_ids.add(pid)
                if pid == card_id:
                    mine_title = p.get("title")
    except Exception:
        pass

    card_meta = None
    try:
        card_meta = yoto.get_card_detail(card_id)
    except Exception:
        card_meta = None

    title = (
        mine_title
        or (card_meta.get("title") if isinstance(card_meta, dict) else None)
        or status.get("title")
        or status.get("card_title")
        or card_id
    )
    user_id = str((card_meta or {}).get("userId") or "").strip().lower()
    is_myo_flag = (card_meta or {}).get("isMyo")

    is_blank_myo = (card_id == "hMkni") or ("make your own guide" in str(title or "").strip().lower())

    if card_id in mine_ids:
        is_editable = True
    elif is_blank_myo:
        is_editable = True
    elif user_id and user_id != "yoto":
        is_editable = True
    elif is_myo_flag is True:
        is_editable = True
    else:
        is_editable = False

    mgr = get_bridge_manager()
    existing_slot = None
    for s in (mgr.list_slots() or []):
        if str(s.get("yoto_card_id") or "") == card_id:
            existing_slot = {"slot_id": s.get("slot_id"), "title": s.get("title"), "kind": s.get("kind")}
            break

    if is_blank_myo:
        reason = (
            "Blank Make-Your-Own (MYO) card detected (factory Make Your Own Guide). "
            "Pair it now to create its StoryDeck slot, then link that playlist once in the Yoto mobile app."
        )
    elif is_editable:
        reason = "Editable Make-Your-Own (MYO) playlist owned by your Yoto account."
    else:
        reason = (
            "Locked commercial / Yoto Store card (cannot be overwritten). "
            "If this is a blank MYO plastic card currently linked to a retail title, link it to any MYO playlist in the Yoto app first."
        )

    return jsonify({
        "ok": True,
        "detected": True,
        "device_id": device_id,
        "card_id": card_id,
        "title": title,
        "is_editable": is_editable,
        "is_locked": not is_editable,
        "is_blank_myo": is_blank_myo,
        "reason": reason,
        "already_linked": existing_slot,
    }), 200


@bridge_cards_bp.route("/api/cards/pair", methods=["POST"])
@require_member
def pair_physical_card():
    """Links a detected editable MYO card_id to a Profile Primary Card or a Fleet MYO Card slot."""
    import uuid
    data = request.get_json(silent=True) or {}
    card_id = str(data.get("card_id") or "").strip()
    target = str(data.get("target") or "fleet").strip().lower()
    profile_id = str(data.get("profile_id") or "").strip()
    name = str(data.get("name") or "").strip()

    if not card_id:
        return jsonify({"ok": False, "error": "card_id is required"}), 400

    # Yoto custom MYO playlists are downloaded to the player (offline) unless podcast/radio
    default_pair_mode = "offline"
    try:
        if clients.yoto_client:
            meta = clients.yoto_client.get_card_detail(card_id) or {}
            content_type = str((meta.get("metadata") or {}).get("category") or (meta.get("content") or {}).get("playbackType") or "").lower()
            if any(k in content_type for k in ("podcast", "radio", "stream", "live")):
                default_pair_mode = "streaming"
    except Exception:
        pass

    mgr = get_bridge_manager()
    if target == "profile":
        if not profile_id:
            return jsonify({"ok": False, "error": "profile_id is required when pairing to a profile"}), 400
        prof = database.get_profile_by_id(profile_id)
        if not prof:
            return jsonify({"ok": False, "error": "Profile not found"}), 404
        prof_update = dict(prof)
        prof_update["primary_card_id"] = card_id
        database.upsert_profile(profile_id, prof_update)
        slot_id = f"profile_{profile_id}"
        existing = mgr.get_slot(slot_id)
        if not existing:
            mgr.assign_abs_book_to_slot(
                slot_id=slot_id,
                abs_item_id="",
                title=name or f"{prof.get('display_name', profile_id)}'s Card",
                author="",
                owner_profile_id=profile_id,
                kind="profile_primary",
                mode=default_pair_mode,
            )
        with database.get_db_connection() as conn:
            conn.execute("UPDATE bridge_slots SET yoto_card_id = ?, mode = ? WHERE slot_id = ?;", (card_id, default_pair_mode, slot_id))
            conn.commit()
        return jsonify({"ok": True, "slot_id": slot_id, "card_id": card_id, "target": "profile"}), 200

    slot_id = f"fleet_{uuid.uuid4().hex[:8]}"
    mgr.assign_abs_book_to_slot(
        slot_id=slot_id,
        abs_item_id="",
        title=name or f"MYO Card ({card_id})",
        author="",
        owner_profile_id="",
        kind="permanent_myo",
        mode=default_pair_mode,
    )
    with database.get_db_connection() as conn:
        conn.execute("UPDATE bridge_slots SET yoto_card_id = ?, mode = ? WHERE slot_id = ?;", (card_id, default_pair_mode, slot_id))
        conn.commit()
    return jsonify({"ok": True, "slot_id": slot_id, "card_id": card_id, "target": "fleet"}), 201


@bridge_cards_bp.route("/api/bridge/chapters/lookup/<item_id>", methods=["GET"])
@require_member
def lookup_audible_chapters_endpoint(item_id: str):
    """Queries Audible/Audnexus for clean chapter markers for a poorly-chaptered ABS audiobook."""
    asin = (request.args.get("asin") or "").strip()
    region = (request.args.get("region") or "us").strip()
    result = lookup_audible_chapters_for_item(item_id, asin=asin, region=region)
    status = 200 if result.get("ok") else 400
    return jsonify(result), status


@bridge_cards_bp.route("/api/bridge/chapters/apply/<item_id>", methods=["POST"])
@require_member
def apply_audible_chapters_endpoint(item_id: str):
    """Applies proposed Audible chapter markers to an Audiobookshelf item."""
    data = request.get_json(silent=True) or {}
    chapters = data.get("chapters")
    if not isinstance(chapters, list) or not chapters:
        asin = (data.get("asin") or "").strip()
        region = (data.get("region") or "us").strip()
        looked = lookup_audible_chapters_for_item(item_id, asin=asin, region=region)
        if not looked.get("ok") or not looked.get("proposal"):
            return jsonify({"ok": False, "error": looked.get("error") or "No proposed chapters found"}), 400
        chapters = looked["proposal"].get("proposed") or []
    result = apply_proposed_chapters_to_item(item_id, chapters)
    return jsonify(result), (200 if result.get("ok") else 500)


@bridge_cards_bp.route("/api/bridge/transfer/analyse/<card_id>", methods=["GET"])
@require_member
def analyse_myo_transfer_endpoint(card_id: str):
    """Matches an old manually-uploaded Yoto MYO playlist's tracks to ABS books via Viterbi duration matching."""
    result = analyse_myo_card_for_abs_transfer(card_id)
    return jsonify(result), (200 if result.get("ok") else 400)


@bridge_cards_bp.route("/api/bridge/transfer/apply/<card_id>", methods=["POST"])
@require_member
def apply_myo_transfer_endpoint(card_id: str):
    """Applies the matched listening position from an old MYO card directly to Audiobookshelf."""
    data = request.get_json(silent=True) or {}
    item_id = (data.get("item_id") or "").strip()
    position = data.get("position")
    duration = data.get("duration")

    if not item_id or position is None:
        analysis = analyse_myo_card_for_abs_transfer(card_id)
        if not analysis.get("ok") or not analysis.get("resume"):
            return jsonify({"ok": False, "error": analysis.get("error") or "Could not resolve listening position on card"}), 400
        resume = analysis["resume"]
        item_id = resume["book_id"]
        position = float(resume["book_position"])
        duration = float(resume["book_duration"])

    if not clients.abs_client:
        return jsonify({"ok": False, "error": "Audiobookshelf client is not configured"}), 503

    ok = clients.abs_client.patch_media_progress(
        item_id,
        current_time=float(position),
        duration=float(duration or 0.0),
        is_finished=False,
    )
    return jsonify({
        "ok": ok,
        "card_id": card_id,
        "item_id": item_id,
        "position": float(position),
        "duration": float(duration or 0.0),
    }), (200 if ok else 500)

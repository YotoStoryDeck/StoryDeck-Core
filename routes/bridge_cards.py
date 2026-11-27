"""
StoryDeck Core Card Slot Inventory & Physical Card Pairing Blueprint (/api/cards*).
"""
from __future__ import annotations

import json
import logging
from flask import Blueprint, request, jsonify
import database
import clients
from core.auth import require_member
from core.bridge.manager import get_bridge_manager
from core.bridge.offline import get_offline_manager

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


def _attach_transcode_status(slot: dict, off_mgr) -> dict:
    """Attaches live in-memory job status or persisted SQLite offline state ('failed', 'paused', 'stopped') onto a slot dict."""
    slot_id = slot.get("slot_id") or ""
    job = off_mgr.get_job_status(slot_id)
    if job:
        slot["transcode"] = job
        return slot
    off_status = str(slot.get("offline_status") or "").lower()
    if slot.get("mode") == "offline" and off_status in ("failed", "paused", "stopped"):
        raw_meta = slot.get("offline_meta")
        meta = {}
        if isinstance(raw_meta, str) and raw_meta:
            try:
                parsed = json.loads(raw_meta)
                if isinstance(parsed, dict):
                    meta = parsed
            except Exception:
                meta = {}
        elif isinstance(raw_meta, dict):
            meta = dict(raw_meta)
        tracks = slot.get("tracks") or []
        done_cnt = sum(1 for t in tracks if str(t.get("yoto_sha256") or "").strip())
        total_cnt = len(tracks)
        slot["transcode"] = {
            "slot_id": slot_id,
            "status": off_status,
            "offline_mode": slot.get("offline_mode") or "book",
            "error": str(meta.get("error") or "Offline build failed") if off_status == "failed" else None,
            "done": done_cnt,
            "total": total_cnt,
            "progress": (done_cnt / total_cnt) if total_cnt > 0 else 0.0,
        }
    return slot


@bridge_cards_bp.route("/api/cards", methods=["GET"])
@require_member
def list_cards():
    """Lists all registered card slots with active transcode and offline status."""
    mgr = get_bridge_manager()
    slots = mgr.list_slots()
    off_mgr = get_offline_manager()

    for s in slots:
        _attach_transcode_status(s, off_mgr)

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

    _attach_transcode_status(slot, get_offline_manager())
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
    """Deletes a card slot after cancelling any active offline transfer."""
    get_offline_manager().cancel_job(slot_id)
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



@bridge_cards_bp.route("/api/cards/<slot_id>/pin", methods=["POST"])
@require_member
def toggle_card_pin(slot_id: str):
    """Toggles pin / cache eviction protection on a card slot."""
    data = request.get_json() or {}
    is_pinned = bool(data.get("is_pinned", True))
    if get_bridge_manager().set_slot_pin(slot_id, is_pinned):
        return jsonify({"success": True, "is_pinned": is_pinned})
    return jsonify({"error": "Slot not found"}), 404



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


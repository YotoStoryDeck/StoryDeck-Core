"""
StoryDeck Core Yoto Cloud Playlists, Audible Chapter Lookup & MYO Transfer Blueprint.
Handles /api/yoto/playlists*, /api/bridge/chapters/*, and /api/bridge/transfer/*.
"""
from __future__ import annotations

import logging
from flask import Blueprint, request, jsonify
import database
import clients
from core.auth import require_member
from core.bridge.chapter_finder import apply_proposed_chapters_to_item, lookup_audible_chapters_for_item
from core.bridge.manager import get_bridge_manager
from core.bridge.offline import prune_evicted_profile_playlists
from core.bridge.transfer import analyse_myo_card_for_abs_transfer

logger = logging.getLogger("storydeck.routes.bridge_playlists")
bridge_playlists_bp = Blueprint("bridge_playlists", __name__)


@bridge_playlists_bp.route("/api/yoto/playlists", methods=["GET"])
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


@bridge_playlists_bp.route("/api/yoto/playlists/<card_id>", methods=["DELETE"])
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


@bridge_playlists_bp.route("/api/yoto/playlists/<card_id>/pin", methods=["POST"])
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


@bridge_playlists_bp.route("/api/yoto/playlists/prune", methods=["POST"])
@require_member
def trigger_lru_prune():
    """Manually triggers LRU pruning of evicted ephemeral profile playlists."""
    return jsonify(prune_evicted_profile_playlists())



@bridge_playlists_bp.route("/api/bridge/chapters/lookup/<item_id>", methods=["GET"])
@require_member
def lookup_audible_chapters_endpoint(item_id: str):
    """Queries Audible/Audnexus for clean chapter markers for a poorly-chaptered ABS audiobook."""
    asin = (request.args.get("asin") or "").strip()
    region = (request.args.get("region") or "us").strip()
    result = lookup_audible_chapters_for_item(item_id, asin=asin, region=region)
    status = 200 if result.get("ok") else 400
    return jsonify(result), status


@bridge_playlists_bp.route("/api/bridge/chapters/apply/<item_id>", methods=["POST"])
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


@bridge_playlists_bp.route("/api/bridge/transfer/analyse/<card_id>", methods=["GET"])
@require_member
def analyse_myo_transfer_endpoint(card_id: str):
    """Matches an old manually-uploaded Yoto MYO playlist's tracks to ABS books via Viterbi duration matching."""
    result = analyse_myo_card_for_abs_transfer(card_id)
    return jsonify(result), (200 if result.get("ok") else 400)


@bridge_playlists_bp.route("/api/bridge/transfer/apply/<card_id>", methods=["POST"])
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

"""
Mobile Parent Portal Player Controls, MQTT Status, and Profile Library Blueprint.
"""
from __future__ import annotations
import time
import logging
from typing import Dict, Any, List
from flask import Blueprint, request, jsonify
import database
import clients
from core.auth import require_auth, get_current_user
from core.bridge.mqtt_client import get_mqtt_bridge
from core.bridge.playback import get_playback_manager
from core.media_utils import get_media_tracks_data

logger = logging.getLogger("storydeck.routes.portal_api")
portal_api_bp = Blueprint("portal_api", __name__)


@portal_api_bp.route("/api/yoto/auth-status", methods=["GET"])
@require_auth
def get_yoto_auth_status():
    """Reports live Yoto API OAuth token health for Parent Portal status indicator & feature lockout."""
    force = request.args.get("force", "").lower() in ("1", "true", "yes")
    if not clients.yoto_client:
        return jsonify({
            "ok": False,
            "connected": False,
            "token_valid": False,
            "configured": False,
            "mode": "direct",
            "reason": "not_configured",
            "error": "Yoto API client is not initialized. Open Admin Settings to connect Yoto.",
            "checked_at": time.time()
        })
    return jsonify(clients.yoto_client.get_auth_status(verify=True, force=force))


@portal_api_bp.route("/api/player/devices", methods=["GET"])
@require_auth
def get_player_devices():
    """Returns available Yoto player hardware devices with live status."""
    from core.player_service import get_live_players_map
    try:
        return jsonify(list(get_live_players_map().values()))
    except Exception as e:
        logger.error("Failed to query player devices: %s", e)
        return jsonify([])


@portal_api_bp.route("/api/player/status/<device_id>", methods=["GET"])
@require_auth
def get_device_status_endpoint(device_id: str):
    """Returns single device playback status."""
    from core.player_service import get_player_status
    try:
        return jsonify(get_player_status(device_id))
    except Exception as e:
        logger.debug("Device status query notice for %s: %s", device_id, e)
        return jsonify({})


@portal_api_bp.route("/api/player/transport", methods=["POST"])
@require_auth
def send_player_transport():
    """Sends transport, volume, speed, seek, and sleep timer commands from the Parent Portal."""
    from core.player_service import dispatch_player_control
    data = request.get_json() or {}
    device_id = data.get("device_id")
    action = data.get("action")
    if not device_id or not action:
        return jsonify({"ok": False, "error": "device_id and action are required"}), 400

    act = "resume" if action == "play" else action
    _, resp_data, status_code = dispatch_player_control(
        device_id,
        act,
        volume=data.get("volume"),
        minutes=data.get("minutes"),
        position=data.get("position"),
        speed=data.get("speed")
    )
    return jsonify(resp_data), status_code


@portal_api_bp.route("/api/player/play", methods=["POST"])
@require_auth
def start_player_card():
    """Commands a Yoto player to begin playback of a specific card/playlist."""
    data = request.get_json() or {}
    device_id = data.get("device_id")
    card_id = data.get("card_id")
    chapter = int(data.get("chapter", 1))
    use_user_abs = bool(data.get("use_user_abs", False))

    if not device_id or not card_id:
        return jsonify({"error": "device_id and card_id are required"}), 400

    abs_token_override = None
    owner_override = ""
    if use_user_abs:
        user = get_current_user() or {}
        if user.get("id") and not user.get("is_lan_guest"):
            creds = database.get_user_abs_credentials(user["id"])
            abs_token_override = creds.get("abs_api_key") or None
            owner_override = f"user:{user['id']}"

    pb_mgr = get_playback_manager()
    pb_mgr.expect(
        device_id, card_id,
        abs_token_override=abs_token_override,
        owner_override=owner_override,
    )
    is_storydeck_slot = False
    from core.bridge.manager import get_bridge_manager
    for s in (get_bridge_manager().list_slots() or []):
        if s.get("slot_id") == card_id or s.get("yoto_card_id") == card_id:
            if s.get("abs_item_id"):
                is_storydeck_slot = True
            if chapter > 1:
                pb_mgr.mark_exact_next(s["slot_id"])
            break

    ck_val = None
    tk_val = None
    if not is_storydeck_slot and clients.yoto_client:
        try:
            cdetail = clients.yoto_client.get_card_detail(card_id)
            if cdetail:
                c_obj = cdetail.get("content") or (cdetail.get("card") or {}).get("content") or {}
                chaps = c_obj.get("chapters") or []
                if chaps:
                    t_idx = max(0, min(len(chaps) - 1, chapter - 1))
                    chapter = t_idx + 1
                    tgt = chaps[t_idx]
                    ck_val = tgt.get("key")
                    trks = tgt.get("tracks") or []
                    tk_val = (trks[0].get("key") if trks else None) or ck_val
        except Exception as ke:
            logger.debug("Notice extracting chapterKey/trackKey for %s: %s", card_id, ke)

    mqtt = get_mqtt_bridge()
    if mqtt.is_connected and mqtt.play(device_id, card_id, track=chapter, chapter_key=ck_val, track_key=tk_val):
        target_prof = str(data.get("profile_id") or "").strip()
        if target_prof and target_prof != "__all__":
            try:
                database.record_playback(card_id, target_prof)
            except Exception:
                pass
        return jsonify({"ok": True, "device_id": device_id, "card_id": card_id, "chapter": chapter, "chapter_key": ck_val, "track_key": tk_val, "via": "mqtt"})

    if not clients.yoto_client:
        return jsonify({"error": "Yoto client not configured"}), 503

    ok = clients.yoto_client.start_playback(device_id, card_id, chapter=chapter, chapter_key=ck_val, track_key=tk_val)
    if ok:
        target_prof = str(data.get("profile_id") or "").strip()
        if target_prof and target_prof != "__all__":
            try:
                database.record_playback(card_id, target_prof)
            except Exception:
                pass
    return jsonify({"ok": ok, "device_id": device_id, "card_id": card_id, "chapter": chapter})


@portal_api_bp.route("/api/bridge/mqtt/status", methods=["GET"])
@require_auth
def get_bridge_mqtt_status():
    """Returns embedded Yoto MQTT connection telemetry and active ABS stream playback sessions."""
    return jsonify({
        "ok": True,
        "mqtt": get_mqtt_bridge().get_status_summary(),
        "active_sessions": get_playback_manager().get_active_sessions_summary()
    })


def _query_profile_library_catalog(profile_id: str) -> List[Dict[str, Any]]:
    """Builds the combined curated ABS + Yoto catalog list (matching Kiosk) for a profile or '__all__' household view."""
    import hashlib
    import urllib.parse
    from database.curation import get_catalog_items_for_profile, sanitize_display_text
    from database.media import get_all_playback_history

    if profile_id == "__all__":
        matrix = database.get_catalog_matrix()
        all_history = get_all_playback_history()
        results = []
        for it in matrix.get("items", []):
            raw_type = str(it.get("type") or "")
            is_abs = (raw_type == "abs")
            is_yoto = raw_type.startswith("yoto")
            if not is_abs and not is_yoto:
                continue

            item_id = it.get("id") or ""
            cover_url = it.get("cover_url") or ""
            cover_v = hashlib.sha256(cover_url.encode("utf-8")).hexdigest()[:8] if cover_url else "1"
            if is_yoto:
                if cover_url and not cover_url.startswith("/api/thumb"):
                    thumb = f"/api/thumb?type=yoto&id={item_id}&url={urllib.parse.quote(cover_url)}&v={cover_v}"
                elif not cover_url:
                    thumb = f"/api/thumb?type=yoto&id={item_id}&v={cover_v}"
                else:
                    thumb = cover_url if "v=" in cover_url else f"{cover_url}{'&' if '?' in cover_url else '?'}v={cover_v}"
            else:
                base_t = cover_url or f"/api/thumb?type=abs&id={item_id}"
                thumb = base_t if "v=" in base_t else f"{base_t}{'&' if '?' in base_t else '?'}v={cover_v}"

            results.append({
                "id": item_id,
                "type": "abs" if is_abs else "yoto",
                "is_myo": (raw_type == "yoto_myo"),
                "title": sanitize_display_text(it.get("title") or ""),
                "author": sanitize_display_text(it.get("author") or ("Unknown" if is_abs else "Yoto")),
                "thumb": thumb,
                "is_highlight": bool(it.get("is_highlight")),
                "has_physical": bool(it.get("has_physical")),
                "series": sanitize_display_text(it.get("series") or ""),
                "sequence_display": it.get("series_order") or "",
                "series_list": it.get("series_list") or [],
                "last_played_at": all_history.get(item_id) or it.get("last_played_at") or "",
            })
        return results

    abs_items = get_catalog_items_for_profile(profile_id, media_type="abs", client_type="kiosk") or []
    yoto_items = get_catalog_items_for_profile(profile_id, media_type="yoto", client_type="kiosk") or []
    combined = list(abs_items) + list(yoto_items)

    # Interleave recent items first (matching Kiosk combined view), then highlights, then discovery order
    recent = [it for it in combined if str(it.get("last_played_at") or "").strip()]
    recent.sort(key=lambda x: str(x.get("last_played_at") or ""), reverse=True)
    recent_ids = {it.get("id") for it in recent[:8]}
    head = [it for it in recent[:8]]
    tail = [it for it in combined if it.get("id") not in recent_ids]
    return head + tail


@portal_api_bp.route("/api/library/profile/<profile_id>", methods=["GET"])
@require_auth
def get_profile_library_catalog(profile_id: str):
    if profile_id == "__all__":
        user = get_current_user() or {}
        if user.get("role") == "guest":
            return jsonify({"ok": False, "error": "Guest accounts are scoped to assigned profiles."}), 403
    """Returns curated ABS audiobooks and series permitted for the specified profile."""
    try:
        return jsonify(_query_profile_library_catalog(profile_id))
    except Exception as e:
        logger.error("Failed to query catalog for profile %s: %s", profile_id, e)
        return jsonify([])


@portal_api_bp.route("/api/library/recent/<profile_id>", methods=["GET"])
@require_auth
def get_profile_recent_books(profile_id: str):
    """Returns recently played / in-progress books for the 'Continue Listening' shelf."""
    try:
        items = _query_profile_library_catalog(profile_id)
        recent = [it for it in (items or []) if it.get("last_played_at")]
        recent.sort(key=lambda x: str(x.get("last_played_at") or ""), reverse=True)
        return jsonify(recent[:8])
    except Exception as e:
        logger.debug("Recent books lookup notice for %s: %s", profile_id, e)
        return jsonify([])


@portal_api_bp.route("/api/library/yoto", methods=["GET"])
@require_auth
def get_household_yoto_cards():
    """Returns household Yoto retail and MYO cards for assigning official Yoto content to Fleet cards."""
    try:
        matrix = database.get_catalog_matrix()
        cards = [
            {
                "id": it.get("id"),
                "title": it.get("title") or "Yoto Card",
                "author": it.get("author") or "Yoto",
                "type": it.get("type"),
                "cover_url": it.get("cover_url") or ""
            }
            for it in matrix.get("items", [])
            if str(it.get("type") or "").startswith("yoto")
        ]
        cards.sort(key=lambda c: c["title"].lower())
        return jsonify(cards)
    except Exception as e:
        logger.debug("Household Yoto cards lookup notice: %s", e)
        return jsonify([])


@portal_api_bp.route("/api/library/book/<item_id>/details", methods=["GET"])
@require_auth
def get_portal_book_details(item_id: str):
    """Returns book synopsis/description and full chapter breakdown for the Parent Portal Book Modal."""
    description = ""
    total_duration = 0.0
    tracks = []
    title = ""
    author = ""
    try:
        tdata = get_media_tracks_data(item_id)
        if isinstance(tdata, dict):
            title = tdata.get("title") or ""
            author = tdata.get("author") or ""
            tracks = tdata.get("tracks") or []
            total_duration = float(tdata.get("total_duration") or 0.0)
    except Exception as e:
        logger.debug("Portal tracks lookup notice for %s: %s", item_id, e)

    if clients.abs_client:
        try:
            abs_item = clients.abs_client.get_item(item_id)
            if abs_item:
                media = abs_item.get("media", {})
                meta = media.get("metadata", {})
                description = meta.get("description") or meta.get("subtitle") or ""
                if not total_duration:
                    total_duration = float(media.get("duration") or 0.0)
                if not tracks:
                    for idx, ch in enumerate(media.get("chapters", []), 1):
                        st = float(ch.get("start", 0.0))
                        et = float(ch.get("end", st))
                        tracks.append({
                            "number": idx,
                            "title": ch.get("title") or f"Chapter {idx}",
                            "start": st,
                            "end": et,
                            "duration": max(0.0, et - st)
                        })
        except Exception as e:
            logger.debug("Portal ABS item synopsis lookup notice for %s: %s", item_id, e)

    # Resolve user or profile ABS listening progress & suggest starting chapter/track
    suggested_chapter = 1
    suggested_chapter_title = ""
    current_time = 0.0
    is_finished = False
    has_progress = False
    progress_owner = ""

    if clients.abs_client:
        try:
            profile_id = (request.args.get("profile_id") or "").strip()
            use_user_abs_arg = (request.args.get("use_user_abs") or "").strip().lower()
            user = get_current_user() or {}
            u_id = str(user.get("id") or "") if not user.get("is_lan_guest") else ""
            u_creds = database.get_user_abs_credentials(u_id) if u_id else {}
            u_has_abs = bool(u_creds.get("abs_api_key") or u_creds.get("abs_user_id") or u_creds.get("abs_username"))

            if use_user_abs_arg in ("1", "true", "yes"):
                check_user = True
            elif use_user_abs_arg in ("0", "false", "no"):
                check_user = False
            else:
                check_user = bool(u_has_abs and (profile_id in ("", "__all__") or u_has_abs))

            prog = None
            if check_user and u_has_abs:
                prog = clients.abs_client.get_media_progress(
                    item_id,
                    token=u_creds.get("abs_api_key") or None,
                    abs_user_id=u_creds.get("abs_user_id") or "",
                    abs_username=u_creds.get("abs_username") or user.get("username") or "",
                )
                progress_owner = u_creds.get("abs_username") or user.get("username") or "your ABS account"
            elif profile_id and profile_id != "__all__":
                prof = database.get_profile_by_id(profile_id) or {}
                p_tok = (prof.get("abs_api_key") or prof.get("abs_token") or "").strip() or None
                p_uname = (prof.get("abs_username") or "").strip()
                if p_tok or p_uname:
                    prog = clients.abs_client.get_media_progress(
                        item_id,
                        token=p_tok,
                        abs_username=p_uname,
                    )
                    progress_owner = prof.get("display_name") or p_uname or profile_id

            if isinstance(prog, dict):
                current_time = float(prog.get("currentTime") or 0.0)
                is_finished = bool(prog.get("isFinished"))
                if total_duration > 0 and current_time >= total_duration - 10.0:
                    is_finished = True
                has_progress = bool(current_time > 5.0 and not is_finished)
                if has_progress and tracks:
                    running_start = 0.0
                    for idx, tr in enumerate(tracks, 1):
                        t_num = int(tr.get("index") or tr.get("number") or idx)
                        t_start = float(tr["start"]) if tr.get("start") is not None else running_start
                        t_dur = float(tr.get("duration") or 0.0)
                        t_end = float(tr["end"]) if tr.get("end") is not None else (t_start + t_dur)
                        running_start = t_end
                        if t_start <= current_time < max(t_end, t_start + 0.1):
                            suggested_chapter = t_num
                            suggested_chapter_title = str(tr.get("title") or f"Chapter {t_num}")
                            break
                    else:
                        last_tr = tracks[-1]
                        suggested_chapter = int(last_tr.get("index") or last_tr.get("number") or len(tracks))
                        suggested_chapter_title = str(last_tr.get("title") or f"Chapter {suggested_chapter}")
        except Exception as pe:
            logger.debug("Portal book progress suggestion lookup notice for %s: %s", item_id, pe)

    return jsonify({
        "id": item_id,
        "title": title,
        "author": author,
        "description": description,
        "total_duration": total_duration,
        "tracks": tracks,
        "suggested_chapter": suggested_chapter,
        "suggested_chapter_title": suggested_chapter_title,
        "current_time": round(current_time, 1),
        "is_finished": is_finished,
        "has_progress": has_progress,
        "progress_owner": progress_owner,
    })

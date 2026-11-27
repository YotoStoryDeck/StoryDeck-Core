"""
User self-service profile and Audiobookshelf account-linking blueprint (/api/auth/me, /api/auth/abs/*).
"""

from flask import Blueprint, request, jsonify, session
import clients
import database
from core.auth import get_current_user, require_auth

user_profile_bp = Blueprint("user_profile", __name__)


def _auto_match_and_mint_user_abs(
    user_id,
    email: str = "",
    username: str = "",
    persist: bool = True,
):
    """
    Matches a StoryDeck user against ABS /api/users by email (priority 1) or username (priority 2),
    mints a scoped 'Act on behalf of user' API key via POST /api/api-keys (with legacy token fallback),
    and optionally persists the link to admin_users.
    """
    if not clients.abs_client or not clients.abs_client.base_url or not clients.abs_client.api_key:
        return None, "Audiobookshelf server or master Admin API key is not configured in Core Settings."

    matched = clients.abs_client.match_abs_user(email=email, username=username)
    if not matched or not matched.get("id") or not matched.get("username"):
        return None, "No matching Audiobookshelf user found for that email or username."

    result = {
        "abs_user_id": matched["id"],
        "abs_username": matched["username"],
        "abs_email": matched.get("email") or "",
        "abs_type": matched.get("type") or "user",
        "libraries_count": len(matched.get("libraries_accessible") or []),
        "matched_by": matched.get("matched_by") or "username",
        "key_source": "preview",
        "persisted": False,
    }

    if not persist or not user_id:
        return result, None

    old_creds = database.get_user_abs_credentials(user_id)
    old_raw_key = old_creds.get("abs_api_key") or ""
    old_abs_uid = old_creds.get("abs_user_id") or ""
    old_abs_uname = old_creds.get("abs_username") or ""

    minted = clients.abs_client.provision_user_api_key(
        matched["id"],
        matched["username"],
        fallback_token=matched.get("token") or "",
        old_api_key=old_raw_key,
    )
    if not minted or not minted.get("api_key"):
        target_type = str(matched.get("type") or "user").strip().lower()
        if target_type == "root":
            perm = clients.abs_client.check_master_key_permissions()
            master_type = str(perm.get("type") or "admin").strip().lower()
            master_uname = str(perm.get("username") or "admin").strip()
            if master_type != "root":
                return (
                    None,
                    f"Matched ABS root user '{matched['username']}', but your Master ABS key belongs to a standard '{master_type}' account ('{master_uname}'). "
                    "In Audiobookshelf, only a 'root' API key can generate API keys for a 'root' user — either use a root Master ABS key, paste a manual key under Advanced, or match a non-root ABS account.",
                )
        return (
            None,
            f"Matched ABS user '{matched['username']}', but could not mint a scoped API key (verify master ABS key is an Admin key).",
        )

    # If the user switched to a different ABS account, also clean up any key from the previous ABS user ID
    if (old_abs_uid and old_abs_uid != matched["id"]) or (
        old_abs_uname and old_abs_uname.lower() != matched["username"].lower()
    ):
        new_kid = str(minted.get("api_key_id") or "").strip()
        clients.abs_client.revoke_storydeck_api_keys_for_user(
            old_abs_uid,
            old_abs_uname,
            raw_api_key=old_raw_key,
            exclude_key_ids=[new_kid] if new_kid else None,
        )

    database.update_user_abs_account(
        user_id,
        abs_api_key=minted["api_key"],
        abs_username=matched["username"],
        abs_user_id=matched["id"],
    )
    result["key_source"] = minted.get("source") or "api_key"
    result["persisted"] = True
    return result, None


def _apply_user_abs_update(user_id: str, data: dict):
    """Shared helper to auto-match/mint, verify manual key, or clear/revoke a user's linked Audiobookshelf account."""
    if data.get("clear_abs_account"):
        old_creds = database.get_user_abs_credentials(user_id)
        if (old_creds.get("abs_user_id") or old_creds.get("abs_api_key") or old_creds.get("abs_username")) and clients.abs_client and clients.abs_client.base_url:
            clients.abs_client.revoke_storydeck_api_keys_for_user(
                old_creds.get("abs_user_id") or "",
                old_creds.get("abs_username") or "",
                raw_api_key=old_creds.get("abs_api_key") or "",
            )
        database.update_user_abs_account(user_id, clear=True)
        return None

    raw_key = str(data.get("abs_api_key") or "").strip()
    if raw_key:
        abs_username = str(data.get("abs_username") or "").strip()
        abs_user_id = str(data.get("abs_user_id") or "").strip()
        if clients.abs_client and clients.abs_client.base_url:
            info = clients.abs_client.verify_user_token(raw_key)
            if not info or not info.get("username"):
                return "Could not verify Audiobookshelf API key against your ABS server."
            abs_username = info["username"]
            abs_user_id = info.get("id") or ""
        database.update_user_abs_account(
            user_id,
            abs_api_key=raw_key,
            abs_username=abs_username,
            abs_user_id=abs_user_id,
        )
        return None

    if data.get("auto_match_abs"):
        u = database.get_user_by_id(user_id) or {}
        em = str(data.get("email") if data.get("email") is not None else (u.get("email") or "")).strip()
        un = str(data.get("abs_username") or data.get("username") or u.get("username") or "").strip()
        _, err = _auto_match_and_mint_user_abs(user_id, email=em, username=un, persist=True)
        if err:
            return err
    return None


def _is_user_abs_self_service_allowed(user) -> bool:
    """Returns True if the actor is an Admin or if self-service user ABS key generation/linking is enabled."""
    if user and user.get("role") == "admin":
        return True
    try:
        return bool(database.get_system_settings().get("abs_allow_user_self_service_key_gen", True))
    except Exception:
        return True


@user_profile_bp.route("/api/auth/abs/auto-match", methods=["POST"])
@require_auth
def auto_match_abs_user():
    """
    Matches the current user (or an Admin-specified target user) against Audiobookshelf
    by email -> username and automatically mints a scoped 'Act on behalf of user' API key.
    """
    user = get_current_user()
    if not user or user.get("is_lan_guest") or not user.get("id"):
        return jsonify({"ok": False, "error": "Sign in with a user account first."}), 403
    if not _is_user_abs_self_service_allowed(user):
        return jsonify({
            "ok": False,
            "error": "User self-service ABS key generation is disabled. Please ask an Administrator to link your Audiobookshelf account.",
        }), 403

    data = request.get_json(silent=True) or {}
    is_admin = (user.get("role") == "admin")
    target_uid = str(data.get("user_id") or "").strip() if is_admin else str(user["id"])
    if not is_admin or ("user_id" not in data and not data.get("preview_only")):
        target_uid = str(user["id"])

    persist = not bool(data.get("preview_only", False)) and bool(target_uid)
    target_row = database.get_user_by_id(target_uid) if target_uid else None

    email = str(
        data.get("email")
        if data.get("email") is not None
        else ((target_row or user).get("email") or "")
    ).strip()
    username = str(
        data.get("abs_username")
        or data.get("username")
        or ((target_row or user).get("username") or "")
    ).strip()

    res, err = _auto_match_and_mint_user_abs(
        target_uid if persist else None,
        email=email,
        username=username,
        persist=persist,
    )
    if err or not res:
        status = 404 if (err and "No matching" in err) else 400
        return jsonify({"ok": False, "error": err or "Could not auto-match Audiobookshelf user."}), status

    return jsonify({"ok": True, **res}), 200


@user_profile_bp.route("/api/auth/abs/unlink", methods=["POST"])
@require_auth
def unlink_abs_user_account():
    """Immediately deletes the user's auto-minted ABS API key on Audiobookshelf and clears their stored link."""
    user = get_current_user()
    if not user or user.get("is_lan_guest") or not user.get("id"):
        return jsonify({"ok": False, "error": "Sign in with a user account first."}), 403
    if not _is_user_abs_self_service_allowed(user):
        return jsonify({
            "ok": False,
            "error": "User self-service ABS key management is disabled. Please ask an Administrator to unlink your Audiobookshelf account.",
        }), 403

    data = request.get_json(silent=True) or {}
    is_admin = (user.get("role") == "admin")
    target_uid = str(data.get("user_id") or "").strip() if (is_admin and data.get("user_id")) else str(user["id"])

    old_creds = database.get_user_abs_credentials(target_uid)
    revoked = 0
    if (old_creds.get("abs_user_id") or old_creds.get("abs_api_key") or old_creds.get("abs_username")) and clients.abs_client and clients.abs_client.base_url:
        revoked = clients.abs_client.revoke_storydeck_api_keys_for_user(
            old_creds.get("abs_user_id") or "",
            old_creds.get("abs_username") or "",
            raw_api_key=old_creds.get("abs_api_key") or "",
        )
    database.update_user_abs_account(target_uid, clear=True)
    return jsonify({"ok": True, "unlinked": True, "revoked_keys": revoked}), 200


@user_profile_bp.route("/api/auth/abs/verify", methods=["POST"])
@require_auth
def verify_abs_user_key():
    """Queries ABS /api/me using a supplied personal API key to detect username & profile info."""
    user = get_current_user()
    if not user or user.get("is_lan_guest") or not user.get("id"):
        return jsonify({"ok": False, "error": "Sign in with a user account first."}), 403
    if not _is_user_abs_self_service_allowed(user):
        return jsonify({
            "ok": False,
            "error": "User self-service ABS key linking is disabled. Please ask an Administrator to link your Audiobookshelf account.",
        }), 403
    data = request.get_json(silent=True) or {}
    raw_key = str(data.get("abs_api_key") or "").strip()
    if not raw_key:
        return jsonify({"ok": False, "error": "Audiobookshelf API key is required."}), 400
    if not clients.abs_client or not clients.abs_client.base_url:
        return jsonify({"ok": False, "error": "Audiobookshelf server is not configured in Core Settings."}), 503

    info = clients.abs_client.verify_user_token(raw_key)
    if not info or not info.get("username"):
        return jsonify({"ok": False, "error": "Invalid Audiobookshelf API key or ABS server unreachable."}), 401

    return jsonify({
        "ok": True,
        "abs_user_id": info["id"],
        "abs_username": info["username"],
        "abs_type": info["type"],
        "libraries_count": len(info.get("libraries_accessible") or []),
    }), 200


@user_profile_bp.route("/api/auth/me", methods=["GET", "PUT"])
def auth_me():
    """Returns or updates the profile of the currently connected user (admin, member, or LAN guest)."""
    user = get_current_user()
    if request.method == "GET":
        if not user:
            return jsonify({"authenticated": False, "user": None}), 200
        user_out = dict(user)
        user_out["abs_self_service_enabled"] = _is_user_abs_self_service_allowed(user)
        return jsonify({
            "authenticated": True,
            "user": user_out
        }), 200

    # PUT: Self-service profile update (display_name, email, ABS account link, optional password)
    if not user or user.get("is_lan_guest") or not user.get("id"):
        return jsonify({"ok": False, "error": "Sign in with a user account to edit your profile."}), 403

    uid = user["id"]
    target = database.get_user_by_id(uid)
    if not target:
        return jsonify({"ok": False, "error": "User account not found."}), 404

    data = request.get_json(silent=True) or {}
    wants_abs_change = bool(
        str(data.get("abs_api_key") or "").strip()
        or data.get("auto_match_abs")
        or data.get("clear_abs_account")
    )
    if wants_abs_change and not _is_user_abs_self_service_allowed(user):
        return jsonify({
            "ok": False,
            "error": "User self-service ABS key linking is disabled. Please ask an Administrator to update your Audiobookshelf account.",
        }), 403
    try:
        if "display_name" in data and data.get("display_name") is not None:
            dn = str(data.get("display_name")).strip() or target["username"]
            database.update_user_display_name(uid, dn)
            session["display_name"] = dn
        if "email" in data and data.get("email") is not None:
            database.update_user_email(uid, str(data.get("email")).strip())
        if data.get("password"):
            pwd = str(data.get("password"))
            if len(pwd) < 8:
                return jsonify({"ok": False, "error": "New password must be at least 8 characters."}), 400
            database.update_user_password(uid, pwd)
        abs_err = _apply_user_abs_update(uid, data)
        if abs_err:
            return jsonify({"ok": False, "error": abs_err}), 400
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400

    updated = get_current_user()
    return jsonify({"ok": True, "message": "Profile updated.", "user": updated}), 200


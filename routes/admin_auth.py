"""
Administrator authentication and session management blueprint.
"""

import ipaddress
import hashlib
import secrets
import json
import re
import hmac
from urllib.parse import urlencode, quote
import requests
from flask import Blueprint, request, jsonify, render_template, session, redirect
import clients
import database
from core.config import TRUSTED_PROXIES, logger
from core.auth import (
    get_current_admin_user,
    get_current_user,
    require_admin,
    require_auth,
    resolve_or_provision_sso_user,
    _resolve_sso_identity,
    _b64url_encode,
    _b64url_decode,
)

admin_auth_bp = Blueprint("admin_auth", __name__)


def _discover_oidc(raw_url: str):
    """Fetches and parses an OpenID Connect discovery document."""
    clean_url = str(raw_url or "").strip()
    if not clean_url:
        return None, "Discovery or Issuer URL is required."
    if not (clean_url.startswith("https://") or clean_url.startswith("http://")):
        return None, "URL must start with https:// or http://"

    if clean_url.rstrip("/").endswith("/.well-known/openid-configuration"):
        discovery_url = clean_url.rstrip("/")
    else:
        discovery_url = clean_url.rstrip("/") + "/.well-known/openid-configuration"

    try:
        resp = requests.get(discovery_url, timeout=8)
        if resp.status_code != 200:
            return None, f"Provider returned HTTP {resp.status_code} for {discovery_url}"
        doc = resp.json()
        if not isinstance(doc, dict) or not doc.get("issuer"):
            return None, "Response is not a valid OpenID Connect discovery document."
        doc["_discovery_url"] = discovery_url
        return doc, None
    except Exception as exc:
        return None, f"Failed to reach discovery URL: {exc}"


def _get_site_origin(req) -> str:
    """Resolves canonical external origin honoring reverse proxy headers."""
    proto = (req.headers.get("X-Forwarded-Proto") or req.scheme or "http").split(",")[0].strip()
    host = (req.headers.get("X-Forwarded-Host") or req.host or "").split(",")[0].strip()
    return f"{proto}://{host}"


def _get_oidc_callback_url(req) -> str:
    """Builds canonical OIDC callback redirect URI honoring reverse proxy headers."""
    return f"{_get_site_origin(req)}/api/auth/oidc/callback"


def _build_sso_logout_url(raw_logout_url: str, req, id_token_hint: str = "") -> str:
    """
    Returns effective SSO logout redirect URL.
    If an external OIDC end-session URL is configured without a return parameter,
    appends post_logout_redirect_uri=<origin>/login so the provider sends users back to StoryDeck.
    """
    clean_url = str(raw_logout_url or "").strip()
    if not clean_url:
        return ""
    if clean_url.startswith(("http://", "https://")):
        lower_url = clean_url.lower()
        if not any(k in lower_url for k in ("post_logout_redirect_uri=", "redirect_uri=", "rd=")):
            site_origin = _get_site_origin(req)
            if not clean_url.startswith(site_origin):
                params = {"post_logout_redirect_uri": f"{site_origin}/login"}
                if id_token_hint:
                    params["id_token_hint"] = id_token_hint
                sep = "&" if "?" in clean_url else "?"
                clean_url = f"{clean_url}{sep}{urlencode(params)}"
    return clean_url


def _sanitize_next_url(next_url, default: str = "/") -> str:
    """Constrains redirect targets to internal relative paths."""
    if not next_url:
        return default
    cand = str(next_url).strip()
    if cand in ("/login", "/admin/login") or cand.startswith("/login?") or cand.startswith("/admin/login?"):
        return default
    if cand.startswith("/") and not cand.startswith("//") and "\\" not in cand:
        return cand
    return default


def _sso_public_meta() -> dict:
    """Returns non-secret SSO configuration metadata safe for auth status responses."""
    sso_cfg = database.get_sso_config()
    raw_provider = str(sso_cfg.get("provider_name") or "Authentik").strip()
    provider_name = "SSO" if (not raw_provider or raw_provider.lower() in ("reverse proxy", "generic forwardauth")) else raw_provider
    forward_auth_enabled = bool(sso_cfg.get("forward_auth_enabled", True))
    oidc_enabled = bool(sso_cfg.get("oidc_enabled", False))
    oidc_ready = bool(oidc_enabled and sso_cfg.get("oidc_issuer_url") and sso_cfg.get("oidc_client_id"))

    if oidc_enabled:
        login_mode = "oidc"
    elif forward_auth_enabled:
        login_mode = "forward_auth"
    else:
        login_mode = "none"

    return {
        "enabled": bool(oidc_enabled or forward_auth_enabled),
        "login_mode": login_mode,
        "provider_name": provider_name,
        "forward_auth_enabled": forward_auth_enabled,
        "oidc_enabled": oidc_enabled,
        "oidc_ready": oidc_ready,
        "login_url": "/api/auth/sso/login",
        "logout_url": sso_cfg.get("logout_url") or "",
    }


def _mask_sso_config(sso_cfg: dict) -> dict:
    """Returns a copy of sso_config with secrets masked for admin API display."""
    masked = dict(sso_cfg)
    secret = str(masked.get("oidc_client_secret") or "")
    if secret:
        masked["oidc_client_secret"] = (secret[:4] + "••••••••" + secret[-4:]) if len(secret) > 8 else "••••••••"
    return masked


@admin_auth_bp.route("/login", methods=["GET"])
@admin_auth_bp.route("/admin", methods=["GET"])
@admin_auth_bp.route("/admin/setup", methods=["GET"])
@admin_auth_bp.route("/admin/login", methods=["GET"])
def admin_portal_page():
    """Serves the single-page application for authentication and the admin dashboard."""
    return render_template("admin.html")


@admin_auth_bp.route("/api/admin/auth/status", methods=["GET"])
def admin_auth_status():
    """Checks whether initial admin onboarding is needed and reports active session identity."""
    user_count = database.get_admin_user_count()
    sso_meta = _sso_public_meta()
    if user_count == 0:
        return jsonify({
            "setup_required": True,
            "authenticated": False,
            "portal_authenticated": False,
            "user": None,
            "portal_user": None,
            "sso": sso_meta
        })

    user = get_current_admin_user()
    session_user = get_current_user()
    portal_user = session_user if (session_user and not session_user.get("is_lan_guest")) else None
    return jsonify({
        "setup_required": False,
        "authenticated": bool(user),
        "portal_authenticated": bool(portal_user),
        "user": user,
        "portal_user": portal_user,
        "sso": sso_meta
    })


@admin_auth_bp.route("/api/admin/auth/setup", methods=["POST"])
def admin_auth_setup():
    """Initial administrator account creation (only allowed when 0 accounts exist)."""
    if database.get_admin_user_count() > 0:
        return jsonify({"ok": False, "error": "Setup is permanently locked."}), 403

    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    display_name = (data.get("display_name") or "Parent").strip()

    if not username or len(password) < 8:
        return jsonify({"ok": False, "error": "Username required, and password must be at least 8 characters."}), 400

    new_user = database.create_admin_user(username, password, display_name=display_name, role="admin", is_super_admin=True)
    return jsonify({"ok": True, "message": "Initial administrator account created.", "user": new_user}), 201


@admin_auth_bp.route("/api/admin/auth/login", methods=["POST"])
def admin_auth_login():
    """Authenticates admin credentials and issues an HTTP-only session cookie."""
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""

    user = database.authenticate_user(username, password)
    if not user:
        return jsonify({"ok": False, "error": "Invalid username or password"}), 401

    session["user_id"] = user["id"]
    session["username"] = user["username"]
    session["display_name"] = user["display_name"]
    session["role"] = user["role"]
    session["is_super_admin"] = bool(user.get("is_super_admin", False))
    session["auth_type"] = "session"

    return jsonify({"ok": True, "message": "Signed in successfully.", "user": user})


@admin_auth_bp.route("/api/admin/auth/logout", methods=["POST"])
def admin_auth_logout():
    """Terminates session and returns optional SSO post-logout redirect URL."""
    sso_cfg = database.get_sso_config()
    id_token_hint = str(session.get("oidc_id_token") or "").strip()
    logout_redirect = _build_sso_logout_url(
        sso_cfg.get("logout_url") or "",
        request,
        id_token_hint=id_token_hint,
    )
    session.clear()
    return jsonify({
        "ok": True,
        "message": "Signed out.",
        "logout_url": logout_redirect,
    })


# ============================================================================
# User Identity & RBAC User Management Endpoints
# ============================================================================

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


@admin_auth_bp.route("/api/auth/abs/auto-match", methods=["POST"])
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


@admin_auth_bp.route("/api/auth/abs/unlink", methods=["POST"])
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


@admin_auth_bp.route("/api/auth/abs/verify", methods=["POST"])
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


@admin_auth_bp.route("/api/auth/me", methods=["GET", "PUT"])
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


@admin_auth_bp.route("/api/admin/users", methods=["GET"])
@require_admin
def list_users():
    """Lists all registered system user accounts with actor-scoped management permissions."""
    actor = get_current_admin_user() or {}
    actor_id = actor.get("id")
    actor_is_super = bool(actor.get("is_super_admin", False))

    users = database.list_admin_users()
    enriched = []
    for u in users:
        is_super = bool(u.get("is_super_admin", False))
        is_self = bool(actor_id and actor_id == u["id"])
        item = dict(u)
        item["is_super_admin"] = is_super
        item["is_self"] = is_self
        item["can_edit"] = (not is_super) or actor_is_super or is_self
        item["can_change_role"] = (not is_super) and (not is_self)
        item["can_delete"] = (not is_super) and (not is_self)
        enriched.append(item)

    return jsonify({
        "ok": True,
        "users": enriched,
        "current_user_id": actor_id,
        "current_is_super_admin": actor_is_super,
    }), 200


@admin_auth_bp.route("/api/admin/users", methods=["POST"])
@require_admin
def create_user():
    """Creates a new user account with specified role, email, and optional SSO provider/subject link."""
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    display_name = (data.get("display_name") or username or "User").strip()
    email = (data.get("email") or "").strip().lower()
    role = str(data.get("role") or "member").strip().lower()
    is_super_admin = bool(data.get("is_super_admin", False))
    if role == "super_admin":
        role = "admin"
        is_super_admin = True
    if is_super_admin:
        actor = get_current_admin_user() or {}
        if not actor.get("is_super_admin"):
            return jsonify({"ok": False, "error": "Only a Super Admin can create another Super Admin."}), 403
    sso_provider = (data.get("sso_provider") or "").strip()
    sso_subject = (data.get("sso_subject") or "").strip()

    if not username:
        return jsonify({"ok": False, "error": "Username is required."}), 400
    if (sso_provider or sso_subject) and not password:
        # Pre-linked SSO users authenticate via reverse proxy / IdP and receive a strong unusable local placeholder secret
        password = secrets.token_urlsafe(32)
    elif len(password) < 8:
        return jsonify({"ok": False, "error": "Password must be at least 8 characters."}), 400
    if role not in ("admin", "member", "guest"):
        return jsonify({"ok": False, "error": "Role must be 'admin', 'member', or 'guest'."}), 400

    existing = database.get_user_by_username(username)
    if existing:
        return jsonify({"ok": False, "error": f"Username '{username}' already exists."}), 409
    if email and database.get_user_by_email(email):
        return jsonify({"ok": False, "error": f"Email '{email}' is already associated with another account."}), 409
    if sso_subject and database.get_user_by_sso_subject(sso_subject, sso_provider):
        return jsonify({"ok": False, "error": f"SSO Subject UID '{sso_subject}' is already linked to another account."}), 409

    try:
        user = database.create_admin_user(
            username=username,
            password=password,
            display_name=display_name,
            role=role,
            is_super_admin=is_super_admin,
            sso_provider=sso_provider,
            email=email,
            sso_subject=sso_subject
        )
        abs_err = _apply_user_abs_update(user["id"], data)
        if abs_err:
            database.delete_user(user["id"])
            return jsonify({"ok": False, "error": abs_err}), 400
        user = database.get_user_by_id(user["id"]) or user
        return jsonify({"ok": True, "message": f"User '{username}' created successfully.", "user": user}), 201
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@admin_auth_bp.route("/api/admin/users/<user_id>", methods=["PUT"])
@require_admin
def update_user(user_id):
    """Updates user username, email, display name, role, SSO provider/subject link, or password."""
    data = request.get_json(silent=True) or {}
    actor = get_current_admin_user() or {}
    user = database.get_user_by_id(user_id)
    if not user:
        return jsonify({"ok": False, "error": "User not found."}), 404

    is_super_target = bool(user.get("is_super_admin", False))
    is_self = bool(actor.get("id") and actor.get("id") == user["id"])
    actor_is_super = bool(actor.get("is_super_admin", False))

    if is_super_target and not actor_is_super and not is_self:
        return jsonify({"ok": False, "error": "The primary Super Admin account cannot be modified by other administrators."}), 403

    # Validate role transitions before applying any partial mutations
    if "role" in data:
        role = str(data.get("role") or "").strip().lower()
        if role == "super_admin" and is_super_target:
            role = "admin"
        if role not in ("admin", "member", "guest"):
            return jsonify({"ok": False, "error": "Role must be 'admin', 'member', or 'guest'."}), 400
        if is_super_target and role != "admin":
            return jsonify({"ok": False, "error": "The primary Super Admin account cannot be demoted from Admin."}), 400
        if is_self and role != "admin":
            return jsonify({"ok": False, "error": "You cannot demote your own active administrator account."}), 400

    # Validate username change before applying partial mutations
    if "username" in data:
        new_username = (data.get("username") or "").strip()
        if not new_username:
            return jsonify({"ok": False, "error": "Username cannot be empty."}), 400
        if new_username != user["username"]:
            existing = database.get_user_by_username(new_username)
            if existing and existing["id"] != user_id:
                return jsonify({"ok": False, "error": f"Username '{new_username}' already exists."}), 409

    # Validate email uniqueness before partial mutations
    if "email" in data:
        new_email = (data.get("email") or "").strip().lower()
        if new_email:
            existing_em = database.get_user_by_email(new_email)
            if existing_em and existing_em["id"] != user_id:
                return jsonify({"ok": False, "error": f"Email '{new_email}' is already associated with another account."}), 409

    # Validate SSO subject uniqueness before partial mutations
    new_provider = str(data.get("sso_provider") if "sso_provider" in data else (user.get("sso_provider") or "")).strip()
    new_subject = str(data.get("sso_subject") if "sso_subject" in data else (user.get("sso_subject") or "")).strip()
    if new_subject and ("sso_provider" in data or "sso_subject" in data):
        existing_sub = database.get_user_by_sso_subject(new_subject, new_provider)
        if existing_sub and existing_sub["id"] != user_id:
            return jsonify({"ok": False, "error": f"SSO Subject UID '{new_subject}' is already linked to another account."}), 409

    # Validate password length before applying partial mutations
    if data.get("password"):
        new_pw = data.get("password")
        if len(new_pw) < 8:
            return jsonify({"ok": False, "error": "Password must be at least 8 characters."}), 400

    if "username" in data:
        try:
            database.update_user_username(user_id, (data.get("username") or "").strip())
        except ValueError as ve:
            return jsonify({"ok": False, "error": str(ve)}), 409

    if "email" in data:
        try:
            database.update_user_email(user_id, data.get("email") or "")
        except ValueError as ve:
            return jsonify({"ok": False, "error": str(ve)}), 409

    if "display_name" in data:
        database.update_user_display_name(user_id, (data.get("display_name") or "").strip())

    if "sso_provider" in data or "sso_subject" in data:
        try:
            database.update_user_sso_identity(user_id, new_provider, new_subject)
        except ValueError as ve:
            return jsonify({"ok": False, "error": str(ve)}), 409

    if "role" in data:
        role = str(data.get("role") or "").strip().lower()
        if role == "super_admin" and is_super_target:
            role = "admin"
        try:
            database.update_user_role(user_id, role)
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 400

    if data.get("password"):
        database.update_user_password(user_id, data.get("password"))

    abs_err = _apply_user_abs_update(user_id, data)
    if abs_err:
        return jsonify({"ok": False, "error": abs_err}), 400

    updated_user = database.get_user_by_id(user_id)
    if is_self and actor.get("auth_type") in ("session", "oidc", "sso") and updated_user:
        session["username"] = updated_user["username"]
        session["display_name"] = updated_user["display_name"]

    return jsonify({"ok": True, "message": "User updated successfully.", "user": updated_user}), 200


@admin_auth_bp.route("/api/admin/users/<user_id>", methods=["DELETE"])
@require_admin
def delete_user_route(user_id):
    """Deletes a user account, protecting the primary Super Admin, self-deletion, and the last admin."""
    actor = get_current_admin_user() or {}
    user = database.get_user_by_id(user_id)
    if not user:
        return jsonify({"ok": False, "error": "User not found."}), 404

    is_super_target = bool(user.get("is_super_admin", False))
    is_self = bool(actor.get("id") and actor.get("id") == user["id"])
    actor_is_super = bool(actor.get("is_super_admin", False))

    if is_super_target:
        if not actor_is_super and not is_self:
            return jsonify({"ok": False, "error": "The primary Super Admin account cannot be deleted by other administrators."}), 403
        return jsonify({"ok": False, "error": "The primary Super Admin account cannot be deleted."}), 400

    if is_self:
        return jsonify({"ok": False, "error": "You cannot delete your own active account."}), 400

    try:
        success = database.delete_user(user_id)
        if not success:
            return jsonify({"ok": False, "error": "User not found."}), 404
        return jsonify({"ok": True, "message": "User deleted successfully."}), 200
    except ValueError as ve:
        return jsonify({"ok": False, "error": str(ve)}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ============================================================================
# Access Policy & SSO / Authentik Configuration Endpoints
# ============================================================================

@admin_auth_bp.route("/api/admin/auth/sso-config", methods=["GET", "PUT"])
@require_admin
def admin_sso_config():
    """Gets or updates reverse-proxy SSO, Authentik header mapping, OIDC readiness, and LAN guest access."""
    admin_user = get_current_admin_user() or {}

    if request.method == "GET":
        settings = database.get_system_settings()
        sso_cfg = database.get_sso_config()
        return jsonify({
            "ok": True,
            "sso_config": _mask_sso_config(sso_cfg),
            "env_trusted_proxies": [str(n) for n in TRUSTED_PROXIES],
            "lan_sitter_access_enabled": bool(settings.get("lan_sitter_access_enabled", True)),
            "current_auth_type": admin_user.get("auth_type", "session"),
            "oidc_callback_path": "/api/auth/oidc/callback",
        }), 200

    data = request.get_json(silent=True) or {}
    if "lan_sitter_access_enabled" in data:
        database.set_system_setting("lan_sitter_access_enabled", bool(data.get("lan_sitter_access_enabled", True)))

    incoming_sso = data.get("sso_config") if isinstance(data.get("sso_config"), dict) else None
    if incoming_sso is None:
        sso_keys = set(database.DEFAULT_SSO_CONFIG.keys())
        flat_sso = {k: v for k, v in data.items() if k in sso_keys}
        incoming_sso = flat_sso if flat_sso else None

    if incoming_sso is not None:
        current_sso = database.get_sso_config()
        merged = dict(current_sso)

        for bool_key in ("forward_auth_enabled", "auto_link_existing_users", "auto_provision_users", "oidc_enabled"):
            if bool_key in incoming_sso:
                merged[bool_key] = bool(incoming_sso.get(bool_key))

        for str_key in (
            "provider_name", "header_subject", "header_username", "header_email",
            "header_display_name", "header_groups", "admin_group", "member_group", "logout_url",
            "oidc_issuer_url", "oidc_client_id"
        ):
            if str_key in incoming_sso and incoming_sso.get(str_key) is not None:
                merged[str_key] = str(incoming_sso.get(str_key)).strip()

        if "default_role" in incoming_sso:
            role_val = str(incoming_sso.get("default_role") or "member").strip().lower()
            if role_val not in ("admin", "member", "guest"):
                return jsonify({"ok": False, "error": "SSO default_role must be 'admin', 'member', or 'guest'."}), 400
            merged["default_role"] = role_val

        if "extra_trusted_proxies" in incoming_sso:
            raw_proxies = incoming_sso.get("extra_trusted_proxies")
            if isinstance(raw_proxies, str):
                candidates = [p.strip() for p in raw_proxies.replace("\n", ",").split(",") if p.strip()]
            elif isinstance(raw_proxies, list):
                candidates = [str(p).strip() for p in raw_proxies if str(p).strip()]
            else:
                return jsonify({"ok": False, "error": "extra_trusted_proxies must be a list or comma-separated string."}), 400

            validated_proxies = []
            for entry in candidates:
                try:
                    ipaddress.ip_network(entry, strict=False)
                except ValueError:
                    try:
                        ipaddress.ip_network(f"{entry}/32", strict=False)
                    except ValueError:
                        return jsonify({"ok": False, "error": f"Invalid trusted proxy IP/CIDR: '{entry}'"}), 400
                validated_proxies.append(entry)
            merged["extra_trusted_proxies"] = validated_proxies

        if "oidc_client_secret" in incoming_sso:
            new_secret = str(incoming_sso.get("oidc_client_secret") or "").strip()
            if new_secret and "••••" not in new_secret:
                merged["oidc_client_secret"] = new_secret
            elif new_secret == "":
                merged["oidc_client_secret"] = ""

        database.set_system_setting("sso_config", merged)

    settings = database.get_system_settings()
    sso_cfg = database.get_sso_config()
    return jsonify({
        "ok": True,
        "message": "Access & SSO settings saved.",
        "sso_config": _mask_sso_config(sso_cfg),
        "env_trusted_proxies": [str(n) for n in TRUSTED_PROXIES],
        "lan_sitter_access_enabled": bool(settings.get("lan_sitter_access_enabled", True)),
        "current_auth_type": admin_user.get("auth_type", "session"),
        "oidc_callback_path": "/api/auth/oidc/callback",
    }), 200


@admin_auth_bp.route("/api/admin/auth/oidc-discover", methods=["POST"])
@require_admin
def admin_oidc_discover():
    """Fetch an OpenID Connect .well-known/openid-configuration document (Authentik, Authelia, etc.)."""
    data = request.get_json(silent=True) or {}
    raw_url = str(data.get("url") or "").strip()
    doc, err = _discover_oidc(raw_url)
    if err or not doc:
        status_code = 400 if ("required" in str(err).lower() or "must start" in str(err).lower()) else 502
        return jsonify({"ok": False, "error": err or "Failed to reach discovery URL"}), status_code

    return jsonify({
        "ok": True,
        "discovery_url": doc.get("_discovery_url", raw_url),
        "issuer": doc.get("issuer", ""),
        "authorization_endpoint": doc.get("authorization_endpoint", ""),
        "token_endpoint": doc.get("token_endpoint", ""),
        "userinfo_endpoint": doc.get("userinfo_endpoint", ""),
        "end_session_endpoint": doc.get("end_session_endpoint", ""),
        "jwks_uri": doc.get("jwks_uri", ""),
    }), 200


@admin_auth_bp.route("/api/admin/auth/proxy-check", methods=["GET"])
@require_admin
def admin_proxy_check():
    """Inspects the current request's proxy peer IP, trust status, and configured ForwardAuth SSO headers."""
    from core.auth import is_request_from_trusted_proxy, get_effective_client_ip
    sso_cfg = database.get_sso_config()
    peer_ip = request.remote_addr or ""
    trusted = is_request_from_trusted_proxy()
    effective_ip = get_effective_client_ip()
    fwd_proto = request.headers.get("X-Forwarded-Proto", "")
    fwd_for = request.headers.get("X-Forwarded-For", "")

    header_map = {
        "Subject / UID": sso_cfg.get("header_subject") or "X-authentik-uid",
        "Username": sso_cfg.get("header_username") or "X-authentik-username",
        "Email": sso_cfg.get("header_email") or "X-authentik-email",
        "Display Name": sso_cfg.get("header_display_name") or "X-authentik-name",
        "Groups": sso_cfg.get("header_groups") or "X-authentik-groups",
    }
    detected = {}
    for label, hdr_name in header_map.items():
        val = (request.headers.get(hdr_name) or "").strip()
        detected[label] = {"header": hdr_name, "present": bool(val), "value": val}

    return jsonify({
        "ok": True,
        "peer_ip": peer_ip,
        "peer_trusted": trusted,
        "effective_client_ip": effective_ip,
        "x_forwarded_for": fwd_for,
        "x_forwarded_proto": fwd_proto or request.scheme,
        "forward_auth_enabled": bool(sso_cfg.get("forward_auth_enabled", True)),
        "headers": detected,
    }), 200


@admin_auth_bp.route("/api/auth/sso/config", methods=["GET"])
def sso_public_config_endpoint():
    """Public endpoint providing non-secret SSO status and login endpoints."""
    return jsonify({
        "ok": True,
        "sso": _sso_public_meta()
    }), 200


@admin_auth_bp.route("/api/auth/sso/login", methods=["GET", "POST"])
def sso_login_dispatcher():
    """
    Unified SSO Login entrypoint.
    - If Native OIDC is enabled, redirects to /api/auth/oidc/login.
    - If ForwardAuth is enabled:
        - If reverse-proxy headers are present, authenticates session and redirects to target.
        - If headers are absent, redirects to login page with diagnostic guidance.
    """
    sso_cfg = database.get_sso_config()
    next_url = _sanitize_next_url(request.args.get("next"))

    if sso_cfg.get("oidc_enabled"):
        return redirect(f"/api/auth/oidc/login?next={quote(next_url)}")

    if sso_cfg.get("forward_auth_enabled", True):
        identity = _resolve_sso_identity(require_admin_role=False)
        if identity:
            session["user_id"] = identity["id"]
            session["username"] = identity["username"]
            session["display_name"] = identity["display_name"]
            session["role"] = identity["role"]
            session["is_super_admin"] = bool(identity.get("is_super_admin", False))
            session["auth_type"] = "sso"
            session["sso_provider"] = identity.get("sso_provider", "")
            session["sso_subject"] = identity.get("sso_subject", "")
            if identity.get("role") != "admin" and next_url.startswith("/admin"):
                next_url = "/"
            return redirect(next_url)

        prov = sso_cfg.get("provider_name") or "Authentik"
        err_msg = (
            f"Reverse-proxy authentication headers for {prov} were not detected on this request. "
            "Please access StoryDeck through your SSO gateway or enable Native OIDC in Settings."
        )
        return redirect(f"/login?error={quote(err_msg)}&next={quote(next_url)}")

    return redirect(f"/login?error={quote('SSO is not enabled in settings.')}&next={quote(next_url)}")


@admin_auth_bp.route("/api/auth/oidc/login", methods=["GET"])
def oidc_login():
    """
    Initiates OpenID Connect Authorization Code Flow with PKCE (S256).
    Generates state, nonce, and code_verifier, discovers authorization endpoint,
    and redirects user to IdP.
    """
    next_url = _sanitize_next_url(request.args.get("next"))
    sso_cfg = database.get_sso_config()
    if not sso_cfg.get("oidc_enabled"):
        return redirect(f"/login?error={quote('Native OIDC is disabled in Settings.')}&next={quote(next_url)}")

    issuer_url = str(sso_cfg.get("oidc_issuer_url") or "").strip()
    client_id = str(sso_cfg.get("oidc_client_id") or "").strip()
    if not issuer_url or not client_id:
        return redirect(f"/login?error={quote('OIDC Issuer URL or Client ID is not configured.')}&next={quote(next_url)}")

    doc, err = _discover_oidc(issuer_url)
    if err or not doc or not doc.get("authorization_endpoint"):
        msg = f"OIDC Discovery failed: {err or 'Missing authorization_endpoint'}"
        return redirect(f"/login?error={quote(msg)}&next={quote(next_url)}")

    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    code_verifier = secrets.token_urlsafe(64)
    code_challenge = _b64url_encode(hashlib.sha256(code_verifier.encode("ascii")).digest())
    callback_uri = _get_oidc_callback_url(request)

    session["oidc_state"] = state
    session["oidc_nonce"] = nonce
    session["oidc_next"] = next_url
    session["oidc_code_verifier"] = code_verifier
    session["oidc_redirect_uri"] = callback_uri

    scopes = ["openid", "profile", "email"]
    supported_scopes = doc.get("scopes_supported")
    if supported_scopes is None or "groups" in supported_scopes:
        scopes.append("groups")

    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": callback_uri,
        "scope": " ".join(scopes),
        "state": state,
        "nonce": nonce,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    auth_url = doc["authorization_endpoint"]
    sep = "&" if "?" in auth_url else "?"
    return redirect(f"{auth_url}{sep}{urlencode(params)}")


@admin_auth_bp.route("/api/auth/oidc/callback", methods=["GET", "POST"])
def oidc_callback():
    """
    Handles OpenID Connect Authorization Code callback from Identity Provider.
    Validates state, exchanges authorization code for tokens using a single RFC 6749
    client authentication method + PKCE code_verifier, resolves claims,
    and binds or provisions local user session.
    """
    next_url = _sanitize_next_url(session.get("oidc_next") or request.args.get("next"))
    error = request.args.get("error") or request.form.get("error")
    if error:
        err_desc = request.args.get("error_description") or request.form.get("error_description") or error
        return redirect(f"/login?error={quote(str(err_desc))}&next={quote(next_url)}")

    expected_state = session.get("oidc_state")
    received_state = request.args.get("state") or request.form.get("state")
    if not received_state or not expected_state or not hmac.compare_digest(str(received_state), str(expected_state)):
        err_msg = "Invalid or expired OIDC state parameter. Please try signing in again."
        return redirect(f"/login?error={quote(err_msg)}&next={quote(next_url)}")

    code = request.args.get("code") or request.form.get("code")
    if not code:
        return redirect(f"/login?error={quote('Missing authorization code from Identity Provider.')}&next={quote(next_url)}")

    sso_cfg = database.get_sso_config()
    issuer_url = str(sso_cfg.get("oidc_issuer_url") or "").strip()
    client_id = str(sso_cfg.get("oidc_client_id") or "").strip()
    client_secret = str(sso_cfg.get("oidc_client_secret") or "").strip()

    doc, err = _discover_oidc(issuer_url)
    if err or not doc or not doc.get("token_endpoint"):
        msg = f"OIDC token discovery failed: {err or 'Missing token_endpoint'}"
        return redirect(f"/login?error={quote(msg)}&next={quote(next_url)}")

    callback_uri = session.pop("oidc_redirect_uri", None) or _get_oidc_callback_url(request)
    code_verifier = session.pop("oidc_code_verifier", None)

    token_payload = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": callback_uri,
        "client_id": client_id,
    }
    if code_verifier:
        token_payload["code_verifier"] = code_verifier

    supported_methods = doc.get("token_endpoint_auth_methods_supported") or ["client_secret_post", "client_secret_basic"]
    auth_header = None
    used_post_secret = False
    if client_secret:
        if "client_secret_post" in supported_methods:
            token_payload["client_secret"] = client_secret
            used_post_secret = True
        else:
            auth_header = (client_id, client_secret)

    try:
        token_resp = requests.post(
            doc["token_endpoint"],
            data=token_payload,
            auth=auth_header,
            headers={"Accept": "application/json"},
            timeout=10,
        )
        if token_resp.status_code in (400, 401) and client_secret and used_post_secret and "client_secret_basic" in supported_methods:
            retry_payload = {k: v for k, v in token_payload.items() if k != "client_secret"}
            token_resp = requests.post(
                doc["token_endpoint"],
                data=retry_payload,
                auth=(client_id, client_secret),
                headers={"Accept": "application/json"},
                timeout=10,
            )

        if token_resp.status_code != 200:
            logger.warning("OIDC token exchange failed (HTTP %s): %s", token_resp.status_code, token_resp.text)
            try:
                err_json = token_resp.json()
                if not isinstance(err_json, dict):
                    err_json = {}
            except Exception:
                err_json = {}
            err_code = str(err_json.get("error") or "").strip()
            err_desc = str(err_json.get("error_description") or "").strip()
            if err_code == "invalid_client" and not client_secret:
                msg = (
                    "Token exchange failed (invalid_client): Identity Provider requires an OIDC Client Secret "
                    "(or set Client Type to Public in Authentik). Enter your OIDC Client Secret in Admin Settings → Users & Access."
                )
            else:
                msg = f"Token exchange failed (HTTP {token_resp.status_code}): {err_desc or err_code or 'Rejected by Identity Provider'}"
            return redirect(f"/login?error={quote(msg)}&next={quote(next_url)}")
        token_data = token_resp.json()
    except Exception as exc:
        logger.error("OIDC token exchange error: %s", exc)
        return redirect(f"/login?error={quote(f'Token exchange request failed: {exc}')}&next={quote(next_url)}")

    claims = {}
    id_token = token_data.get("id_token")
    if id_token and "." in id_token:
        try:
            parts = id_token.split(".")
            if len(parts) >= 2:
                claims = json.loads(_b64url_decode(parts[1]).decode("utf-8"))
        except Exception as exc:
            logger.debug("Failed parsing id_token payload: %s", exc)

    access_token = token_data.get("access_token")
    userinfo_endpoint = doc.get("userinfo_endpoint")
    if userinfo_endpoint and access_token:
        try:
            ui_resp = requests.get(
                userinfo_endpoint,
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=8,
            )
            if ui_resp.status_code == 200:
                ui_data = ui_resp.json()
                if isinstance(ui_data, dict):
                    claims.update(ui_data)
        except Exception as exc:
            logger.debug("OIDC userinfo query notice: %s", exc)

    sso_subject = str(claims.get("sub") or "").strip()
    if not sso_subject:
        return redirect(f"/login?error={quote('Identity provider did not return a valid subject (sub) claim.')}&next={quote(next_url)}")

    sso_username = str(
        claims.get("preferred_username")
        or claims.get("nickname")
        or claims.get("username")
        or (claims.get("email", "").split("@")[0] if "@" in str(claims.get("email", "")) else "")
        or sso_subject
    ).strip()
    sso_email = str(claims.get("email") or "").strip().lower()
    sso_display_name = str(claims.get("name") or sso_username or "User").strip()

    raw_groups = claims.get("groups") or []
    if isinstance(raw_groups, str):
        groups = {g.strip().lower() for g in re.split(r"[|,;]", raw_groups) if g.strip()}
    elif isinstance(raw_groups, list):
        groups = {str(g).strip().lower() for g in raw_groups if str(g).strip()}
    else:
        groups = set()

    user = resolve_or_provision_sso_user(
        sso_subject=sso_subject,
        sso_username=sso_username,
        sso_email=sso_email,
        sso_display_name=sso_display_name,
        groups=groups,
        sso_cfg=sso_cfg,
        require_admin_role=False,
        auth_type="oidc",
    )

    if not user:
        return redirect(f"/login?error={quote('User provisioning denied or failed for this account.')}&next={quote(next_url)}")

    session["user_id"] = user["id"]
    session["username"] = user["username"]
    session["display_name"] = user["display_name"]
    session["role"] = user["role"]
    session["is_super_admin"] = bool(user.get("is_super_admin", False))
    session["auth_type"] = "oidc"
    session["sso_provider"] = user.get("sso_provider", "")
    session["sso_subject"] = user.get("sso_subject", "")
    if id_token:
        session["oidc_id_token"] = id_token

    # Clean up OIDC flow state from session
    session.pop("oidc_state", None)
    session.pop("oidc_nonce", None)
    next_url = _sanitize_next_url(session.pop("oidc_next", "/admin"))
    if user.get("role") != "admin" and next_url.startswith("/admin"):
        next_url = "/"

    return redirect(next_url)

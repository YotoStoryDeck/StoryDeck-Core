"""
Administrator user account management (RBAC) blueprint (/api/admin/users*).
"""

import secrets
from flask import Blueprint, request, jsonify, session
import database
from core.auth import get_current_admin_user, require_admin
from routes.user_profile import _apply_user_abs_update

admin_users_bp = Blueprint("admin_users", __name__)


@admin_users_bp.route("/api/admin/users", methods=["GET"])
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


@admin_users_bp.route("/api/admin/users", methods=["POST"])
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


@admin_users_bp.route("/api/admin/users/<user_id>", methods=["PUT"])
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


@admin_users_bp.route("/api/admin/users/<user_id>", methods=["DELETE"])
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


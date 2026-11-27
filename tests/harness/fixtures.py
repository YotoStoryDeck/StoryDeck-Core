"""Reusable synthetic fixtures and dirty-DB builders for StoryDeck tests."""
import json
from pathlib import Path
import sqlite3
from typing import Any, Dict, List, Optional

import database


def seed_admin_user(
    username: str = "test_admin",
    password: str = "StrongPass123!",
    display_name: str = "Test Admin",
    role: str = "admin",
    **kwargs,
) -> Dict[str, Any]:
    """Creates or fetches an admin/member user inside the isolated test DB."""
    existing = database.get_user_by_username(username)
    if existing:
        return existing
    return database.create_admin_user(
        username,
        password,
        display_name=display_name,
        role=role,
        **kwargs,
    )


def login_session(client, user: Optional[Dict[str, Any]] = None, *, role: str = "admin",
                  user_id: str = "test_admin_id", username: str = "test_admin",
                  is_super_admin: bool = False) -> None:
    """Authenticates a Flask test client session directly."""
    u = user or {}
    with client.session_transaction() as sess:
        sess["user_id"] = u.get("id", user_id)
        sess["username"] = u.get("username", username)
        sess["display_name"] = u.get("display_name", u.get("username", username))
        sess["role"] = u.get("role", role)
        sess["is_super_admin"] = bool(u.get("is_super_admin", is_super_admin))


def seed_profile(profile_id: str = "test_child", **overrides) -> Dict[str, Any]:
    """Creates or updates a synthetic child profile."""
    payload: Dict[str, Any] = {
        "display_name": overrides.pop("display_name", profile_id.replace("_", " ").title()),
        "default_playback_mode": "streaming",
        "catalog_rotation_limit": 60,
        "yoto": {"inherit_shared": True},
    }
    payload.update(overrides)
    database.upsert_profile(profile_id, payload)
    return database.get_profile_by_id(profile_id)


def seed_media(items: List[Dict[str, Any]]) -> None:
    """Seeds one or more synthetic media items into ``media_items``."""
    database.upsert_media_items(items)


def seed_device(mac: str = "AA:BB:CC:DD:EE:01", friendly_name: str = "Test Touch",
                assigned_profile_id: Optional[str] = None, is_approved: bool = True,
                ip_address: str = "198.51.100.10", firmware_version: str = "v1.0.0") -> None:
    """Registers and optionally approves a synthetic hardware controller."""
    database.register_esp32_device(
        mac_address=mac,
        friendly_name=friendly_name,
        ip_address=ip_address,
        firmware_version=firmware_version,
    )
    if is_approved or assigned_profile_id is not None:
        database.update_esp32_device(
            mac,
            {
                "is_approved": 1 if is_approved else 0,
                "assigned_profile_id": assigned_profile_id,
            },
        )


def make_dirty_test_db(db_path: Path) -> Path:
    """Creates a synthetic schema-v17 DB populated with fake secrets & PII for sanitizer tests."""
    prev_db = database.DB_PATH
    from tests.harness.isolation import rebind_test_db

    rebind_test_db(db_path)
    try:
        database.init_db()
        database.create_admin_user(
            "real_parent_name",
            "SuperRealPass!99",
            display_name="Parent Real Name",
            role="admin",
            email="parent@family-domain.org",
            sso_subject="authentik-sub-12345",
        )
        uid = database.get_user_by_username("real_parent_name")["id"]
        database.update_user_abs_account(
            uid,
            abs_api_key="eyJ_real_user_abs_jwt_secret",
            abs_username="parent_abs",
            abs_user_id="abs_u_real",
        )
        database.set_system_setting("abs_base_url", "http://192.168.1.99:13378")
        database.set_system_setting("abs_api_key", "REAL_ABS_MASTER_SECRET")
        database.set_system_setting("yoto_refresh_token", "REAL_YOTO_REFRESH_SECRET")
        database.set_system_setting("yoto_client_id", "REAL_YOTO_CLIENT_ID")
        database.set_system_setting("openrouter_api_key", "sk-or-v1-REAL_OPENROUTER")
        database.set_system_setting("sso_config", {
            "forward_auth_enabled": True,
            "provider_name": "Authentik",
            "oidc_enabled": True,
            "oidc_issuer_url": "https://auth.my-private-homelab.org/o/storydeck/",
            "oidc_client_id": "real-oidc-client-id",
            "oidc_client_secret": "REAL_OIDC_SECRET_VALUE",
            "extra_trusted_proxies": ["192.168.1.50"],
        })
        database.upsert_profile("secret_kid", {
            "display_name": "Real Child Name",
            "abs_username": "real_kid_abs",
            "abs_api_key": "REAL_CHILD_ABS_KEY",
            "yoto": {"inherit_shared": True},
        })
        database.register_esp32_device("11:22:33:44:55:66", "Kid Bedroom Frame", "192.168.1.77", "v1.0.0")
        database.update_esp32_device("11:22:33:44:55:66", {"is_approved": 1, "parent_pin": "9876"})
        database.upsert_media_items([{
            "id": "dirty_book_1",
            "type": "abs",
            "title": "Clean Title For Snapshot Test",
            "author": "Clean Author",
            "cover_url": "https://cdn.example.invalid/cover.jpg?token=SECRET_TOK_123",
            "raw_metadata": {"series": "Clean Series", "stream": "http://192.168.1.99:13378/a?apikey=ABS_KEY_9"},
        }])

        conn = sqlite3.connect(str(db_path))
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO bridge_slots (slot_id, abs_item_id, yoto_card_id, owner_profile_id, title, kind, mode)
                    VALUES ('dirty_slot_1', 'dirty_book_1', 'yoto1', 'secret_kid', 'Clean Title For Snapshot Test', 'permanent_myo', 'streaming');
                    """
                )
                conn.execute(
                    """
                    INSERT INTO bridge_tracks (slot_id, track_idx, title, start_time, end_time, duration, stream_url)
                    VALUES ('dirty_slot_1', 1, 'Ch 1', 0.0, 60.0, 60.0, 'https://real-home.example.com/s/audio/dirty_slot_1/1');
                    """
                )
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        finally:
            conn.close()
    finally:
        rebind_test_db(prev_db)
    return db_path

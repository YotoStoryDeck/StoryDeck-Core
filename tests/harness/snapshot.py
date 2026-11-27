"""Read-only local SQLite snapshot copy and mandatory credential sanitizer."""
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Dict, Optional, Tuple

from tests.harness.isolation import get_isolation_root

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SENSITIVE_KEY_RE = re.compile(r"(key|secret|token|password|webhook|cookie)", re.IGNORECASE)
_QUERY_SECRET_RE = re.compile(r"([?&](?:token|apikey|api_key)=)[^&\"'\s]+", re.IGNORECASE)
_URL_HOST_RE = re.compile(r"^https?://[^/]+/")

_EXPLICIT_SECRET_SETTINGS = {
    "abs_api_key",
    "yoto_client_id",
    "yoto_client_secret",
    "yoto_refresh_token",
    "yoto_access_token",
    "tinker_api_key",
    "gemini_api_key",
    "anthropic_api_key",
    "openrouter_api_key",
    "sso_oidc_client_id",
    "sso_oidc_client_secret",
    "sso_oidc_issuer",
    "yoto_bridge_api_key",
    "session_secret",
}

TEST_MASTER_API_KEY = "test_master_api_key_0000000000000000"
TEST_SNAPSHOT_PASSWORD = "SnapshotTestPass!123"


def resolve_snapshot_source() -> Optional[Path]:
    """Resolves the optional local SQLite DB path when explicitly opted in.

    Returns ``None`` unless ``STORYDECK_SNAPSHOT_DB`` is enabled (``1``/``true``/
    explicit path) AND points to a non-empty SQLite file on disk.
    """
    raw = os.getenv("STORYDECK_SNAPSHOT_DB", "").strip()
    if not raw or raw.lower() in ("0", "false", "no", "off"):
        return None
    candidate = (_REPO_ROOT / "data" / "broker.db") if raw.lower() in ("1", "true", "yes") else Path(raw)
    if candidate.is_file() and candidate.stat().st_size > 0:
        return candidate
    return None


def has_local_snapshot_db() -> bool:
    """Returns True when an optional local DB snapshot is enabled and present."""
    return resolve_snapshot_source() is not None


def copy_sqlite_readonly(src: Path, dst: Path) -> None:
    """Copies ``src`` to ``dst`` via SQLite read-only URI + online ``backup()``.

    Opens the source strictly read-only (``?mode=ro``), guaranteeing zero writes
    to ``src``, ``-wal``, or ``-shm``.
    """
    src_path = Path(src).resolve()
    dst_path = Path(dst)
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    if dst_path.exists():
        dst_path.unlink()

    src_conn = sqlite3.connect(f"file:{src_path.as_posix()}?mode=ro", uri=True)
    dst_conn = sqlite3.connect(str(dst_path))
    try:
        src_conn.backup(dst_conn)
    finally:
        dst_conn.close()
        src_conn.close()


def _scrub_nested_json(obj):
    if isinstance(obj, dict):
        cleaned = {}
        for k, v in obj.items():
            kl = str(k).lower()
            if kl == "extra_trusted_proxies":
                cleaned[k] = []
            elif kl in ("oidc_issuer_url", "logout_url") and isinstance(v, str) and v:
                cleaned[k] = "https://auth.example.test/sso"
            elif kl == "oidc_client_id" and isinstance(v, str) and v:
                cleaned[k] = "test-oidc-client"
            elif _SENSITIVE_KEY_RE.search(kl):
                cleaned[k] = ""
            else:
                cleaned[k] = _scrub_nested_json(v)
        return cleaned
    if isinstance(obj, list):
        return [_scrub_nested_json(x) for x in obj]
    if isinstance(obj, str):
        return _QUERY_SECRET_RE.sub(r"\1REDACTED", obj)
    return obj


def sanitize_snapshot_db(db_path: Path) -> Dict[str, int]:
    """Scrubs all credentials, tokens, password hashes, emails, and LAN IPs.

    Operates strictly on the ephemeral snapshot copy ``db_path``. Returns a count
    dictionary of scrubbed rows per category for verification and audit tests.
    """
    from database.auth import hash_password

    stats = {
        "system_settings_scrubbed": 0,
        "admin_users_scrubbed": 0,
        "profiles_scrubbed": 0,
        "devices_scrubbed": 0,
        "media_items_scrubbed": 0,
        "bridge_tracks_scrubbed": 0,
    }
    pw_hash, pw_salt = hash_password(TEST_SNAPSHOT_PASSWORD)

    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()
        tables = {r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table';").fetchall()}

        with conn:
            # 1. system_settings: credentials, base URLs, and nested SSO secrets
            if "system_settings" in tables:
                rows = cur.execute("SELECT key, value FROM system_settings;").fetchall()
                for row in rows:
                    key = row["key"]
                    raw_val = row["value"] or ""
                    kl = key.lower()
                    if kl == "master_api_key":
                        conn.execute(
                            "UPDATE system_settings SET value = ? WHERE key = ?;",
                            (json.dumps(TEST_MASTER_API_KEY), key),
                        )
                        stats["system_settings_scrubbed"] += 1
                    elif kl == "abs_base_url":
                        conn.execute(
                            "UPDATE system_settings SET value = ? WHERE key = ?;",
                            (json.dumps("http://abs.test.invalid"), key),
                        )
                        stats["system_settings_scrubbed"] += 1
                    elif kl == "yoto_bridge_url":
                        conn.execute(
                            "UPDATE system_settings SET value = ? WHERE key = ?;",
                            (json.dumps("http://bridge.test.invalid"), key),
                        )
                        stats["system_settings_scrubbed"] += 1
                    elif kl in _EXPLICIT_SECRET_SETTINGS or _SENSITIVE_KEY_RE.search(kl):
                        conn.execute(
                            "UPDATE system_settings SET value = ? WHERE key = ?;",
                            (json.dumps(""), key),
                        )
                        stats["system_settings_scrubbed"] += 1
                    else:
                        try:
                            parsed = json.loads(raw_val)
                        except Exception:
                            parsed = None
                        if isinstance(parsed, (dict, list)):
                            scrubbed = _scrub_nested_json(parsed)
                            if scrubbed != parsed:
                                conn.execute(
                                    "UPDATE system_settings SET value = ? WHERE key = ?;",
                                    (json.dumps(scrubbed), key),
                                )
                                stats["system_settings_scrubbed"] += 1

            # 2. admin_users: credentials, emails, usernames, display names, ABS links, SSO subjects
            if "admin_users" in tables:
                cols = {c[1] for c in cur.execute("PRAGMA table_info(admin_users);").fetchall()}
                u_rows = cur.execute("SELECT rowid, id FROM admin_users ORDER BY rowid ASC;").fetchall()
                for idx, urow in enumerate(u_rows, 1):
                    updates = {
                        "username": f"test_user_{idx}",
                        "password_hash": pw_hash,
                        "salt": pw_salt,
                        "display_name": f"Test User {idx}",
                    }
                    if "email" in cols:
                        updates["email"] = f"user_{idx}@example.test"
                    if "sso_subject" in cols:
                        updates["sso_subject"] = ""
                    if "abs_api_key" in cols:
                        updates["abs_api_key"] = ""
                    if "abs_username" in cols:
                        updates["abs_username"] = ""
                    if "abs_user_id" in cols:
                        updates["abs_user_id"] = ""
                    set_sql = ", ".join(f"{col} = ?" for col in updates)
                    conn.execute(
                        f"UPDATE admin_users SET {set_sql} WHERE id = ?;",
                        list(updates.values()) + [urow["id"]],
                    )
                    stats["admin_users_scrubbed"] += 1

            # 3. profiles: personal ABS API keys/usernames & display names (preserve IDs for FKs)
            if "profiles" in tables:
                cols = {c[1] for c in cur.execute("PRAGMA table_info(profiles);").fetchall()}
                p_rows = cur.execute("SELECT rowid, id FROM profiles ORDER BY rowid ASC;").fetchall()
                for idx, prow in enumerate(p_rows, 1):
                    updates = {"display_name": f"Profile {idx}"}
                    if "abs_api_key" in cols:
                        updates["abs_api_key"] = ""
                    if "abs_username" in cols:
                        updates["abs_username"] = ""
                    set_sql = ", ".join(f"{col} = ?" for col in updates)
                    conn.execute(
                        f"UPDATE profiles SET {set_sql} WHERE id = ?;",
                        list(updates.values()) + [prow["id"]],
                    )
                    stats["profiles_scrubbed"] += 1

            # 4. esp32_devices: RFC5737 IPs, JWT secrets, parent PINs, friendly names
            if "esp32_devices" in tables:
                cols = {c[1] for c in cur.execute("PRAGMA table_info(esp32_devices);").fetchall()}
                d_rows = cur.execute("SELECT rowid, mac_address FROM esp32_devices ORDER BY rowid ASC;").fetchall()
                for idx, drow in enumerate(d_rows, 1):
                    updates = {
                        "friendly_name": f"Test Device {idx}",
                        "ip_address": f"198.51.100.{((idx - 1) % 250) + 1}",
                    }
                    if "jwt_secret" in cols:
                        updates["jwt_secret"] = f"test_jwt_secret_{idx:04d}_0000000000000000"
                    if "parent_pin" in cols:
                        updates["parent_pin"] = "1234"
                    set_sql = ", ".join(f"{col} = ?" for col in updates)
                    conn.execute(
                        f"UPDATE esp32_devices SET {set_sql} WHERE mac_address = ?;",
                        list(updates.values()) + [drow["mac_address"]],
                    )
                    stats["devices_scrubbed"] += 1

            # 5. media_items: strip any query tokens in cover_url / raw_metadata while keeping catalog fields
            if "media_items" in tables:
                m_rows = cur.execute("SELECT id, cover_url, raw_metadata FROM media_items;").fetchall()
                for mrow in m_rows:
                    cov = mrow["cover_url"] or ""
                    meta = mrow["raw_metadata"] or "{}"
                    new_cov = _QUERY_SECRET_RE.sub(r"\1REDACTED", cov)
                    new_meta = _QUERY_SECRET_RE.sub(r"\1REDACTED", meta)
                    if new_cov != cov or new_meta != meta:
                        conn.execute(
                            "UPDATE media_items SET cover_url = ?, raw_metadata = ? WHERE id = ?;",
                            (new_cov, new_meta, mrow["id"]),
                        )
                        stats["media_items_scrubbed"] += 1

            # 6. bridge_tracks: normalize external hosts in stream_url
            if "bridge_tracks" in tables:
                t_rows = cur.execute(
                    "SELECT id, stream_url FROM bridge_tracks WHERE stream_url IS NOT NULL AND stream_url != '';"
                ).fetchall()
                for trow in t_rows:
                    url = trow["stream_url"]
                    new_url = _URL_HOST_RE.sub("http://storydeck.test.invalid/", url)
                    if new_url != url:
                        conn.execute(
                            "UPDATE bridge_tracks SET stream_url = ? WHERE id = ?;",
                            (new_url, trow["id"]),
                        )
                        stats["bridge_tracks_scrubbed"] += 1

        conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
    finally:
        conn.close()

    return stats


def create_sanitized_snapshot(
    src: Optional[Path] = None,
    dest_path: Optional[Path] = None,
) -> Tuple[Path, Dict[str, int]]:
    """Creates a clean, scrubbed snapshot copy of ``src`` in temp storage."""
    resolved_src = Path(src) if src is not None else resolve_snapshot_source()
    if resolved_src is None or not resolved_src.is_file():
        raise FileNotFoundError("Local snapshot DB source is not configured or missing.")

    target = Path(dest_path) if dest_path is not None else (get_isolation_root() / f"snapshot_{os.getpid()}.db")
    copy_sqlite_readonly(resolved_src, target)
    stats = sanitize_snapshot_db(target)
    return target, stats

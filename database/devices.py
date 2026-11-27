"""ESP32 Hardware Controller Registry, Quarantine & Token Lifecycle."""
import json
import secrets
import sqlite3
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional, Tuple

from .connection import get_db_connection, logger

# Per-MAC monotonic timestamp throttle so frequent JWT polling doesn't write to SQLite on every request
_LAST_SEEN_TOUCH_CACHE: Dict[str, float] = {}


def compute_device_online_state(last_seen_str: Optional[str], threshold_sec: int = 180) -> Tuple[bool, Optional[int]]:
    """Parses SQLite UTC CURRENT_TIMESTAMP or ISO-8601 UTC string and returns (is_online, seconds_ago)."""
    raw = str(last_seen_str or "").strip()
    if not raw:
        return False, None
    parsed: Optional[datetime] = None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            parsed = datetime.strptime(raw.rstrip("Z"), fmt).replace(tzinfo=timezone.utc)
            break
        except ValueError:
            continue
    if parsed is None:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            return False, None
    secs = max(0, int((datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds()))
    return (secs <= threshold_sec), secs


def touch_esp32_device_last_seen(mac_address: str, ip_address: str = "", min_interval_sec: float = 0.0) -> bool:
    """Refreshes last_seen (and valid non-proxy LAN IP) with optional monotonic throttling."""
    mac_clean = (mac_address or "").lower().strip()
    if not mac_clean:
        return False
    now_mono = time.monotonic()
    if min_interval_sec > 0:
        last = _LAST_SEEN_TOUCH_CACHE.get(mac_clean, 0.0)
        if (now_mono - last) < min_interval_sec:
            return True
    _LAST_SEEN_TOUCH_CACHE[mac_clean] = now_mono

    ip_clean = (ip_address or "").strip()
    update_ip = bool(
        ip_clean
        and not ip_clean.startswith("127.")
        and not ip_clean.startswith("172.")
        and ip_clean != "::1"
    )
    conn = get_db_connection()
    try:
        with conn:
            if update_ip:
                conn.execute(
                    "UPDATE esp32_devices SET last_seen = CURRENT_TIMESTAMP, ip_address = ? WHERE mac_address = ?;",
                    (ip_clean, mac_clean),
                )
            else:
                conn.execute(
                    "UPDATE esp32_devices SET last_seen = CURRENT_TIMESTAMP WHERE mac_address = ?;",
                    (mac_clean,),
                )
        return True
    except Exception as e:
        logger.debug("Failed touching last_seen for %s: %s", mac_clean, e)
        return False
    finally:
        conn.close()


def register_esp32_device(
    mac_address: str,
    friendly_name: str,
    ip_address: str,
    firmware_version: str,
    boot_state: str = "",
    assigned_profile_id: str = "",
    lock_player: Optional[bool] = None,
    locked_player_id: str = "",
    parent_pin: str = "",
) -> Dict[str, Any]:
    """Registers an ESP32 device upon boot handshake (quarantined by default if new)."""
    mac_clean = mac_address.lower().strip()
    clean_boot = (boot_state or "").strip().lower()
    conn = get_db_connection()
    try:
        with conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT mac_address, friendly_name, assigned_profile_id, is_approved, target_firmware_version, ota_status FROM esp32_devices WHERE mac_address = ?;",
                (mac_clean,),
            )
            existing = cur.fetchone()
            if existing:
                if clean_boot == "rolled_back":
                    conn.execute("""
                    UPDATE esp32_devices
                    SET ip_address = ?, firmware_version = ?, ota_status = 'rolled_back',
                        ota_progress = 0, ota_last_error = 'Bootloader auto-rollback triggered',
                        last_seen = CURRENT_TIMESTAMP
                    WHERE mac_address = ?;
                    """, (ip_address, firmware_version, mac_clean))
                elif clean_boot == "verified" or (
                    existing["target_firmware_version"] == firmware_version
                    and (existing["ota_status"] or "") in ("pending", "downloading", "flashing", "flashing_done")
                ):
                    conn.execute("""
                    UPDATE esp32_devices
                    SET ip_address = ?, firmware_version = ?, ota_status = 'idle',
                        ota_progress = 100, ota_last_error = '', last_seen = CURRENT_TIMESTAMP
                    WHERE mac_address = ?;
                    """, (ip_address, firmware_version, mac_clean))
                else:
                    conn.execute("""
                    UPDATE esp32_devices
                    SET ip_address = ?, firmware_version = ?, last_seen = CURRENT_TIMESTAMP
                    WHERE mac_address = ?;
                    """, (ip_address, firmware_version, mac_clean))
                return {
                    "mac_address": mac_clean,
                    "friendly_name": existing["friendly_name"],
                    "assigned_profile_id": existing["assigned_profile_id"],
                    "is_approved": bool(existing["is_approved"]),
                    "status": "updated"
                }
            else:
                init_prof = (assigned_profile_id or "").strip() or None
                if init_prof:
                    cur.execute("SELECT 1 FROM profiles WHERE id = ?;", (init_prof,))
                    if not cur.fetchone():
                        logger.warning(
                            "Ignoring unknown initial assigned_profile '%s' during registration for %s",
                            init_prof, mac_clean,
                        )
                        init_prof = None
                init_locked_id = (locked_player_id or "").strip()
                init_lock = 1 if (bool(lock_player) or bool(init_locked_id)) else 0
                raw_pin = (parent_pin or "").strip()
                init_pin = raw_pin if (len(raw_pin) == 4 and raw_pin.isdigit()) else ""
                conn.execute("""
                INSERT INTO esp32_devices (
                    mac_address, friendly_name, ip_address, firmware_version, is_approved,
                    assigned_profile_id, lock_player, locked_player_id, parent_pin
                )
                VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?);
                """, (
                    mac_clean,
                    friendly_name or f"ESP32 ({mac_clean[-5:]})",
                    ip_address,
                    firmware_version,
                    init_prof,
                    init_lock,
                    init_locked_id,
                    init_pin,
                ))
                return {
                    "mac_address": mac_clean,
                    "friendly_name": friendly_name or f"ESP32 ({mac_clean[-5:]})",
                    "assigned_profile_id": init_prof,
                    "is_approved": False,
                    "status": "registered_quarantined"
                }
    finally:
        conn.close()
def get_esp32_device(mac_address: str) -> Optional[Dict[str, Any]]:
    """Retrieves device record by MAC address."""
    mac_clean = mac_address.lower().strip()
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM esp32_devices WHERE mac_address = ?;", (mac_clean,))
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()
def list_esp32_devices() -> List[Dict[str, Any]]:
    """Lists all registered devices for Admin Portal Hardware tab with computed online state."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM esp32_devices ORDER BY last_seen DESC;")
        devices: List[Dict[str, Any]] = []
        for r in cur.fetchall():
            d = dict(r)
            is_online, secs = compute_device_online_state(d.get("last_seen"))
            d["is_online"] = is_online
            d["last_seen_seconds_ago"] = secs
            devices.append(d)
        return devices
    finally:
        conn.close()
def update_esp32_device(mac_address: str, updates: Dict[str, Any]) -> bool:
    """Updates device friendly name, assigned default profile, or approval state."""
    mac_clean = mac_address.lower().strip()
    conn = get_db_connection()
    try:
        with conn:
            cur = conn.cursor()
            cur.execute("SELECT mac_address FROM esp32_devices WHERE mac_address = ?;", (mac_clean,))
            if not cur.fetchone():
                return False

            # Merge and serialize settings dictionary into settings_json if provided
            if "settings" in updates and isinstance(updates["settings"], dict):
                existing_settings = {}
                cur.execute("SELECT settings_json FROM esp32_devices WHERE mac_address = ?;", (mac_clean,))
                r = cur.fetchone()
                if r and r["settings_json"]:
                    try:
                        import json as _json
                        existing_settings = _json.loads(r["settings_json"]) or {}
                    except Exception:
                        existing_settings = {}
                existing_settings.update(updates["settings"])
                import json as _json
                updates["settings_json"] = _json.dumps(existing_settings)

            fields = []
            values = []
            for k in [
                "friendly_name", "assigned_profile_id", "is_approved",
                "firmware_version", "previous_firmware_version",
                "target_firmware_version", "ota_status", "ota_progress", "ota_last_error",
                "jwt_secret", "token_version", "last_token_issued", "parent_pin",
                "lock_player", "locked_player_id", "settings_json"
            ]:
                if k in updates:
                    fields.append(f"{k} = ?")
                    values.append(updates[k])
            if fields:
                values.append(mac_clean)
                query = f"UPDATE esp32_devices SET {', '.join(fields)}, last_seen = CURRENT_TIMESTAMP WHERE mac_address = ?;"
                conn.execute(query, tuple(values))
            else:
                conn.execute("UPDATE esp32_devices SET last_seen = CURRENT_TIMESTAMP WHERE mac_address = ?;", (mac_clean,))
        return True
    finally:
        conn.close()
def revoke_esp32_device(mac_address: str) -> bool:
    """Instantly revokes device tokens, increments token_version, and places in quarantine."""
    mac_clean = mac_address.lower().strip()
    conn = get_db_connection()
    try:
        with conn:
            cur = conn.cursor()
            cur.execute("SELECT mac_address, token_version FROM esp32_devices WHERE mac_address = ?;", (mac_clean,))
            row = cur.fetchone()
            if not row:
                return False
            curr_ver = int(row["token_version"] or 1)
            conn.execute("""
                UPDATE esp32_devices
                SET token_version = ?, is_approved = 0, last_seen = CURRENT_TIMESTAMP
                WHERE mac_address = ?;
            """, (curr_ver + 1, mac_clean))
        return True
    finally:
        conn.close()
def delete_esp32_device(mac_address: str) -> bool:
    """Deletes device registration."""
    mac_clean = mac_address.lower().strip()
    conn = get_db_connection()
    try:
        with conn:
            conn.execute("DELETE FROM esp32_devices WHERE mac_address = ?;", (mac_clean,))
        return True
    finally:
        conn.close()
def prune_stale_devices(hours: int = 48) -> int:
    """Purges unapproved or stale devices not seen in the last `hours`."""
    conn = get_db_connection()
    try:
        with conn:
            cur = conn.cursor()
            cur.execute("""
                DELETE FROM esp32_devices
                WHERE is_approved = 0 AND last_seen < datetime('now', '-' || ? || ' hours');
            """, (hours,))
            return cur.rowcount
    finally:
        conn.close()
# ============================================================================
# Media Catalog & Highlights Matrix DAO
# ============================================================================

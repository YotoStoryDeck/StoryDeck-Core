"""
Touch MicroSD Cache Inspection, Orphan Pruning & Purge Relay Blueprint (/api/admin/devices/*/cache*).
"""

from typing import Any, Dict, Optional, Tuple
import requests
from flask import Blueprint, jsonify, request
from core.auth import require_admin
from core.config import logger
from routes.admin_firmware import _validate_touch_lan_device

admin_device_cache_bp = Blueprint("admin_device_cache", __name__)


def _relay_touch_cache_command(
    mac_address: str,
    method: str,
    endpoint: str,
    action: str,
    include_pin: bool = True,
    extra_params: Optional[Dict[str, str]] = None,
    timeout: int = 8,
) -> Tuple[Any, int]:
    """Shared proxy helper forwarding authenticated MicroSD cache commands to a Touch controller."""
    dev, err_resp = _validate_touch_lan_device(mac_address)
    if err_resp:
        return err_resp

    ip_str = (dev.get("ip_address") or "").strip()
    params: Dict[str, str] = {}
    if include_pin:
        body = request.get_json(silent=True) or {}
        pin = str(
            body.get("pin")
            or request.args.get("pin")
            or dev.get("parent_pin")
            or "1234"
        ).strip()
        params["pin"] = pin
    if extra_params:
        params.update(extra_params)

    target_url = f"http://{ip_str}{endpoint}"
    try:
        resp = requests.request(method=method, url=target_url, params=params or None, timeout=timeout)
    except requests.RequestException as exc:
        logger.warning("Touch SD cache relay failed (%s %s): %s", method, target_url, exc)
        return jsonify({"ok": False, "error": f"Failed to reach ESP32 at {ip_str}: {exc}"}), 502

    try:
        payload = resp.json()
    except ValueError:
        payload = {"raw": resp.text}

    if not (200 <= resp.status_code < 300):
        return jsonify({
            "ok": False,
            "error": f"Device returned HTTP {resp.status_code} for SD {action}",
            "device_response": payload,
        }), 502

    result: Dict[str, Any] = {
        "ok": True,
        "mac": dev["mac_address"],
        "ip": ip_str,
        "action": action,
        "device_response": payload,
    }
    if action == "status":
        result["sd"] = payload
    return jsonify(result), 200



# Touch MicroSD Cache Inspection, Orphan Pruning & Purge Proxies (DLP-020)
# ============================================================================

@admin_device_cache_bp.route("/api/admin/devices/<mac_address>/cache/status", methods=["GET"])
@require_admin
def admin_device_cache_status(mac_address: str):
    """Fetches live /sd/status storage metrics from an approved Touch display on the LAN."""
    return _relay_touch_cache_command(
        mac_address=mac_address,
        method="GET",
        endpoint="/sd/status",
        action="status",
        include_pin=False,
        timeout=5,
    )


@admin_device_cache_bp.route("/api/admin/devices/<mac_address>/cache/prune", methods=["POST"])
@admin_device_cache_bp.route("/api/devices/<mac_address>/cache/prune", methods=["POST"])
@require_admin
def admin_device_cache_prune(mac_address: str):
    """Relays POST /sd/prune to unlink orphaned .565/.chap files on the Touch MicroSD card."""
    return _relay_touch_cache_command(
        mac_address=mac_address,
        method="POST",
        endpoint="/sd/prune",
        action="prune",
    )


@admin_device_cache_bp.route("/api/admin/devices/<mac_address>/cache/refresh", methods=["POST"])
@admin_device_cache_bp.route("/api/devices/<mac_address>/cache/refresh", methods=["POST"])
@require_admin
def admin_device_cache_refresh(mac_address: str):
    """Relays POST /sd/refresh to invalidate and re-warm a specific card's MicroSD cache."""
    body = request.get_json(silent=True) or {}
    card_id = str(body.get("card_id") or body.get("id") or request.args.get("card_id") or "").strip()
    extra = {"id": card_id} if card_id else None
    return _relay_touch_cache_command(
        mac_address=mac_address,
        method="POST",
        endpoint="/sd/refresh",
        action="refresh",
        extra_params=extra,
    )


@admin_device_cache_bp.route("/api/admin/devices/<mac_address>/cache", methods=["DELETE"])
@admin_device_cache_bp.route("/api/admin/devices/<mac_address>/cache/rebuild", methods=["POST"])
@admin_device_cache_bp.route("/api/devices/<mac_address>/cache/rebuild", methods=["POST"])
@require_admin
def admin_device_cache_purge(mac_address: str):
    """Relays DELETE /sd/cache?confirm=1 to flush /ythumbs/ on the Touch MicroSD card."""
    return _relay_touch_cache_command(
        mac_address=mac_address,
        method="DELETE",
        endpoint="/sd/cache",
        action="purge",
        extra_params={"confirm": "1"},
    )

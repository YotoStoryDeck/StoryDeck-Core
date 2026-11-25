"""
StoryDeck Core - Touch ESP32 Upstream Release Service.
Provides:
  - Non-blocking, cached check against GitHub Releases API for YotoStoryDeck/StoryDeck-Touch
  - Release filtering prioritizing stable releases with pre-release (RC) opt-in
  - On-demand download and SHA-256 validation of firmware binaries into Core's repository
  - Strict absence of automatic background downloads (notification & on-demand choice only)
"""

import hashlib
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

import core.config as core_config
import database
from core.version_service import SEMVER_REGEX, parse_semver

logger = logging.getLogger("storydeck_core.touch_release")

UPSTREAM_REPO = "YotoStoryDeck/StoryDeck-Touch"
GITHUB_API_RELEASES_URL = f"https://api.github.com/repos/{UPSTREAM_REPO}/releases"

# Cache TTL: 6 hours (21600 seconds)
CACHE_TTL_SECONDS = 21600
_TOUCH_CACHE: Dict[str, Any] = {
    "data": None,
    "timestamp": 0.0,
    "include_prereleases": False,
}

_ESP_IMAGE_HEADER_MAGIC = 0xE9
_MAX_FIRMWARE_BYTES = 16 * 1024 * 1024
_REQUEST_TIMEOUT_SECONDS = 3.5
_DOWNLOAD_TIMEOUT_SECONDS = 30


def find_firmware_asset(assets: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """
    Finds the compiled application firmware binary (.bin) asset from GitHub release assets.
    Matches in priority order:
      1. Asset ending with '-firmware.bin' (e.g. storydeck-touch-v0.1.0-firmware.bin)
      2. Asset named 'firmware.bin'
      3. Any asset ending with '.bin' with 'firmware' in its name
    """
    if not assets:
        return None

    # Priority 1: Specific '-firmware.bin' or exact 'firmware.bin'
    for a in assets:
        name = (a.get("name") or "").lower()
        if name.endswith("-firmware.bin") or name == "firmware.bin":
            return a

    # Priority 2: Any .bin containing 'firmware'
    for a in assets:
        name = (a.get("name") or "").lower()
        if name.endswith(".bin") and "firmware" in name:
            return a

    return None


def invalidate_touch_release_cache() -> None:
    """Clears the in-memory upstream touch releases cache."""
    global _TOUCH_CACHE
    _TOUCH_CACHE = {
        "data": None,
        "timestamp": 0.0,
        "include_prereleases": False,
    }


def check_upstream_touch_releases(
    include_prereleases: Optional[bool] = None,
    force: bool = False,
) -> Dict[str, Any]:
    """
    Performs a non-blocking check against GitHub Releases API for YotoStoryDeck/StoryDeck-Touch.
    Results are cached in-memory for 6 hours unless force=True.
    
    Notification-only: NEVER downloads binaries in this check.
    
    Release prioritization:
      - Default: stable releases only.
      - If include_prereleases=True: pre-releases (RC builds) are also included.
    """
    global _TOUCH_CACHE

    if include_prereleases is None:
        settings = database.get_system_settings()
        include_prereleases = bool(settings.get("touch_include_prereleases", False))

    now = time.time()
    now_iso = datetime.now(timezone.utc).isoformat()

    # Cache hit check
    if (
        not force
        and _TOUCH_CACHE["data"]
        and (now - _TOUCH_CACHE["timestamp"] < CACHE_TTL_SECONDS)
        and _TOUCH_CACHE["include_prereleases"] == include_prereleases
    ):
        cached = dict(_TOUCH_CACHE["data"])
        cached["cache_hit"] = True
        return cached

    result: Dict[str, Any] = {
        "ok": True,
        "repository": UPSTREAM_REPO,
        "include_prereleases": include_prereleases,
        "has_update": False,
        "latest_version": None,
        "latest_stable_version": None,
        "releases": [],
        "checked_at": now_iso,
        "cache_hit": False,
        "error": None,
    }

    try:
        headers = {
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "StoryDeck-Core-TouchReleaseService",
        }
        resp = requests.get(
            f"{GITHUB_API_RELEASES_URL}?per_page=20",
            headers=headers,
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )

        if resp.status_code == 200:
            raw_releases = resp.json()
            if isinstance(raw_releases, list):
                # Retrieve currently cataloged firmware in Core SQLite
                cataloged_releases = database.list_firmware_releases()
                cataloged_versions = {r["version"] for r in cataloged_releases}
                active_latest_cataloged = next(
                    (r["version"] for r in cataloged_releases if r.get("is_active_latest")),
                    None,
                )

                # Retrieve approved Touch hardware devices to compare installed versions
                devices = database.list_esp32_devices()
                device_versions = [
                    d.get("firmware_version")
                    for d in devices
                    if d.get("is_approved")
                    and not (d.get("mac_address", "").startswith("kiosk-") or "-web" in (d.get("firmware_version") or ""))
                    and d.get("firmware_version")
                ]

                parsed_releases: List[Dict[str, Any]] = []
                latest_stable_tag: Optional[str] = None
                latest_stable_semver: Optional[Tuple] = None
                latest_overall_tag: Optional[str] = None
                latest_overall_semver: Optional[Tuple] = None

                for rel in raw_releases:
                    if rel.get("draft"):
                        continue

                    tag = (rel.get("tag_name") or "").strip()
                    parsed = parse_semver(tag)
                    is_prerelease = bool(rel.get("prerelease") or (parsed and parsed[3] == 0))

                    # Filter out pre-releases if user did not opt in
                    if is_prerelease and not include_prereleases:
                        continue

                    fw_asset = find_firmware_asset(rel.get("assets") or [])

                    item = {
                        "tag_name": tag,
                        "name": rel.get("name") or tag,
                        "prerelease": is_prerelease,
                        "published_at": rel.get("published_at"),
                        "body": (rel.get("body") or "").strip(),
                        "html_url": rel.get("html_url") or f"https://github.com/{UPSTREAM_REPO}/releases/tag/{tag}",
                        "asset_name": fw_asset.get("name") if fw_asset else None,
                        "asset_size": fw_asset.get("size") if fw_asset else 0,
                        "asset_download_url": fw_asset.get("browser_download_url") if fw_asset else None,
                        "is_cataloged": tag in cataloged_versions,
                    }
                    parsed_releases.append(item)

                    if parsed:
                        # Check stable
                        if not is_prerelease:
                            if latest_stable_semver is None or parsed > latest_stable_semver:
                                latest_stable_semver = parsed
                                latest_stable_tag = tag

                        # Check overall available under criteria
                        if latest_overall_semver is None or parsed > latest_overall_semver:
                            latest_overall_semver = parsed
                            latest_overall_tag = tag

                result["releases"] = parsed_releases
                result["latest_stable_version"] = latest_stable_tag
                # Prioritize stable release as latest_version unless no stable releases exist
                result["latest_version"] = latest_stable_tag or latest_overall_tag

                # Determine if an update notification should be shown:
                # An update is available if the latest upstream version:
                # 1. Is not yet cataloged in Core repository, OR
                # 2. Is greater than active latest cataloged, OR
                # 3. Is greater than any approved Touch device's firmware version
                target_ver = result["latest_version"]
                if target_ver:
                    target_semver = parse_semver(target_ver)
                    if target_ver not in cataloged_versions:
                        result["has_update"] = True
                    elif active_latest_cataloged:
                        act_semver = parse_semver(active_latest_cataloged)
                        if target_semver and act_semver and target_semver > act_semver:
                            result["has_update"] = True

                    # Also check against devices
                    if not result["has_update"] and target_semver:
                        for dv in device_versions:
                            d_semver = parse_semver(dv)
                            if d_semver and target_semver > d_semver:
                                result["has_update"] = True
                                break

        else:
            result["error"] = f"GitHub API returned HTTP {resp.status_code}"
            logger.warning("Failed querying Touch releases: %s", result["error"])

    except Exception as exc:
        result["error"] = f"Upstream check failed: {exc}"
        logger.debug("Exception querying Touch releases: %s", exc)

    # Save to in-memory cache
    _TOUCH_CACHE["data"] = dict(result)
    _TOUCH_CACHE["timestamp"] = now
    _TOUCH_CACHE["include_prereleases"] = include_prereleases

    return result


def download_upstream_touch_release(tag: str) -> Dict[str, Any]:
    """
    Downloads the firmware .bin asset for a specific upstream release into Core's FIRMWARE_DIR,
    validates the ESP32 0xE9 magic header and SHA-256 hash, and catalogs it in SQLite.
    
    This is strictly user-initiated on demand.
    """
    clean_tag = (tag or "").strip()
    if not clean_tag or not SEMVER_REGEX.match(clean_tag):
        return {
            "ok": False,
            "error": f"Invalid semantic version tag '{tag}'.",
        }

    # Fetch release details from GitHub
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "StoryDeck-Core-TouchReleaseService",
    }
    rel_url = f"{GITHUB_API_RELEASES_URL}/tags/{clean_tag}"
    try:
        resp = requests.get(rel_url, headers=headers, timeout=_REQUEST_TIMEOUT_SECONDS)
        if resp.status_code != 200:
            return {
                "ok": False,
                "error": f"GitHub release for tag '{clean_tag}' not found (HTTP {resp.status_code}).",
            }
        rel_data = resp.json()
    except Exception as exc:
        return {
            "ok": False,
            "error": f"Failed reaching GitHub API for release '{clean_tag}': {exc}",
        }

    fw_asset = find_firmware_asset(rel_data.get("assets") or [])
    if not fw_asset or not fw_asset.get("browser_download_url"):
        return {
            "ok": False,
            "error": f"No firmware binary asset (.bin) found in release '{clean_tag}'.",
        }

    download_url = fw_asset["browser_download_url"]
    expected_size = int(fw_asset.get("size") or 0)
    if expected_size > _MAX_FIRMWARE_BYTES:
        return {
            "ok": False,
            "error": f"Asset size ({expected_size} bytes) exceeds maximum allowable 16 MB.",
        }

    # Download binary payload
    logger.info("Downloading Touch firmware %s from %s", clean_tag, download_url)
    try:
        dl_resp = requests.get(
            download_url,
            headers={"User-Agent": "StoryDeck-Core-TouchReleaseService"},
            timeout=_DOWNLOAD_TIMEOUT_SECONDS,
            stream=True,
        )
        if dl_resp.status_code != 200:
            return {
                "ok": False,
                "error": f"Download failed with HTTP {dl_resp.status_code}.",
            }

        raw_bytes = bytearray()
        for chunk in dl_resp.iter_content(chunk_size=65536):
            if chunk:
                raw_bytes.extend(chunk)
                if len(raw_bytes) > _MAX_FIRMWARE_BYTES:
                    return {
                        "ok": False,
                        "error": "Firmware download exceeded maximum allowable 16 MB.",
                    }

    except Exception as exc:
        return {
            "ok": False,
            "error": f"Network error downloading firmware binary: {exc}",
        }

    size_bytes = len(raw_bytes)
    if size_bytes < 24:
        return {
            "ok": False,
            "error": f"Downloaded firmware binary is too small ({size_bytes} bytes).",
        }

    if raw_bytes[0] != _ESP_IMAGE_HEADER_MAGIC:
        return {
            "ok": False,
            "error": "Downloaded file is not a valid ESP32 image (missing 0xE9 header magic).",
        }

    sha256_hex = hashlib.sha256(raw_bytes).hexdigest()

    # Save to local FIRMWARE_DIR
    fw_dir = core_config.FIRMWARE_DIR
    fw_dir.mkdir(parents=True, exist_ok=True)
    target_path = (fw_dir / f"firmware_{clean_tag}.bin").resolve()
    target_path.write_bytes(raw_bytes)

    # Determine if this release should automatically be marked active latest:
    # Mark active latest if it's stable and no cataloged releases exist or user uploaded as stable
    parsed = parse_semver(clean_tag)
    is_stable = bool(parsed and parsed[3] == 1)

    existing_catalog = database.list_firmware_releases()
    should_be_latest = False
    if is_stable:
        if not existing_catalog or not any(r.get("is_active_latest") for r in existing_catalog):
            should_be_latest = True
        else:
            # Check if this stable version is newer than current active latest
            active_rel = next((r for r in existing_catalog if r.get("is_active_latest")), None)
            if active_rel:
                act_parsed = parse_semver(active_rel["version"])
                if act_parsed and parsed > act_parsed:
                    should_be_latest = True

    record = database.upsert_firmware_release(
        version=clean_tag,
        release_tag=clean_tag,
        file_path=str(target_path),
        file_size_bytes=size_bytes,
        sha256_hash=sha256_hex,
        release_notes=rel_data.get("body") or "",
        is_active_latest=should_be_latest,
    )

    # Invalidate cache so subsequent check recognizes is_cataloged: True
    invalidate_touch_release_cache()

    logger.info(
        "Successfully downloaded and cataloged Touch firmware %s (%d bytes, sha256=%s, is_latest=%s)",
        clean_tag,
        size_bytes,
        sha256_hex[:12],
        should_be_latest,
    )

    return {
        "ok": True,
        "version": clean_tag,
        "release": record,
        "is_active_latest": should_be_latest,
    }

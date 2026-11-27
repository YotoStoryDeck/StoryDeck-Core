"""
Audiobookshelf playback progress tracking & listening session sync mixin.
"""

from typing import Any, Dict, Optional, Tuple
import requests
from core.config import logger


class ABSProgressMixin:
    """Media progress and listening session endpoints for ABSClient."""

    def get_media_progress(self, item_id: str, token: Optional[str] = None,
                           abs_user_id: str = "", abs_username: str = "") -> Optional[Dict[str, Any]]:
        """
        Fetches saved listening progress for an Audiobookshelf library item.
        1. Queries GET /api/me/progress/<item_id> with `token` (when valid).
        2. If `token` is missing or returns 401/403 (e.g. a root user where standard Admin master key
           could not mint a scoped API key), falls back to querying GET /api/users/<abs_user_id>
           with the master API key and extracting the matching mediaProgress entry.
        """
        if not item_id or not self.base_url:
            return None
        if token and token.strip() not in self._invalid_user_tokens:
            try:
                res = requests.get(
                    f"{self.base_url}/api/me/progress/{item_id}",
                    headers=self.get_headers(token),
                    timeout=8
                )
                if res.status_code == 200:
                    return res.json()
                if res.status_code in (401, 403) and self.api_key and token.strip() != self.api_key.strip():
                    self._invalid_user_tokens.add(token.strip())
            except Exception as e:
                logger.debug("ABS get_media_progress notice for %s: %s", item_id, e)

        # Fallback: lookup via master API key on /api/users/<abs_user_id>
        uid = (abs_user_id or "").strip()
        if not uid and abs_username:
            matched = self.match_abs_user(username=abs_username)
            if matched and matched.get("id"):
                uid = matched["id"]
        if uid and self.api_key:
            for mp in self.get_user_media_progress(uid):
                if str(mp.get("libraryItemId") or "") == item_id:
                    return mp

        if not token and self.api_key:
            try:
                res = requests.get(
                    f"{self.base_url}/api/me/progress/{item_id}",
                    headers=self.get_headers(self.api_key),
                    timeout=8
                )
                if res.status_code == 200:
                    return res.json()
            except Exception as e:
                logger.debug("ABS master get_media_progress notice for %s: %s", item_id, e)
        return None

    def patch_media_progress(self, item_id: str, current_time: float, duration: float,
                             is_finished: bool = False, token: Optional[str] = None) -> bool:
        """Directly updates listening progress on Audiobookshelf via PATCH /api/me/progress/{id}."""
        if not item_id or not self.base_url:
            return False
        dur = float(duration or 0.0)
        cur_t = max(0.0, float(current_time or 0.0))
        progress = 1.0 if is_finished else (min(1.0, cur_t / dur) if dur > 0 else 0.0)
        payload = {
            "currentTime": round(cur_t, 1),
            "duration": dur,
            "progress": round(progress, 4),
            "isFinished": bool(is_finished),
        }
        try:
            res = requests.patch(
                f"{self.base_url}/api/me/progress/{item_id}",
                headers=self.get_headers(token),
                json=payload,
                timeout=8
            )
            return res.status_code in (200, 204)
        except Exception as e:
            logger.warning("ABS patch_media_progress error for %s: %s", item_id, e)
            return False

    def start_playback_session(self, item_id: str, device_id: str, device_name: str = "Yoto",
                               token: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Opens an Audiobookshelf playback session via POST /api/items/{id}/play."""
        if not item_id or not self.base_url:
            return None
        payload = {
            "deviceInfo": {
                "deviceId": f"yoto-{device_id or 'storydeck'}",
                "clientName": "StoryDeck Core",
                "deviceName": device_name or "Yoto"
            },
            "forceDirectPlay": True,
            "forceTranscode": False,
            "mediaPlayer": "storydeck-core",
            "supportedMimeTypes": ["audio/mpeg", "audio/mp4", "audio/flac", "audio/ogg", "audio/aac"]
        }
        try:
            res = requests.post(
                f"{self.base_url}/api/items/{item_id}/play",
                headers=self.get_headers(token),
                json=payload,
                timeout=10
            )
            if res.status_code == 200:
                return res.json()
            logger.debug("ABS start_playback_session returned %d for %s", res.status_code, item_id)
        except Exception as e:
            logger.debug("ABS start_playback_session notice for %s: %s", item_id, e)
        return None

    def sync_playback_session(self, session_id: str, current_time: float, time_listened: float,
                              duration: float, token: Optional[str] = None) -> Tuple[bool, int]:
        """Reports interim playback progress to an open Audiobookshelf session."""
        if not session_id or not self.base_url:
            return False, 0
        payload = {
            "currentTime": round(max(0.0, float(current_time or 0.0)), 1),
            "timeListened": round(max(0.0, float(time_listened or 0.0)), 1),
            "duration": float(duration or 0.0)
        }
        try:
            res = requests.post(
                f"{self.base_url}/api/session/{session_id}/sync",
                headers=self.get_headers(token),
                json=payload,
                timeout=8
            )
            return res.status_code in (200, 204), res.status_code
        except Exception as e:
            logger.debug("ABS sync_playback_session notice for %s: %s", session_id, e)
            return False, 0

    def close_playback_session(self, session_id: str, current_time: float, time_listened: float,
                               duration: float, token: Optional[str] = None) -> bool:
        """Closes an active Audiobookshelf playback session with final listening telemetry."""
        if not session_id or not self.base_url:
            return False
        payload = {
            "currentTime": round(max(0.0, float(current_time or 0.0)), 1),
            "timeListened": round(max(0.0, float(time_listened or 0.0)), 1),
            "duration": float(duration or 0.0)
        }
        try:
            res = requests.post(
                f"{self.base_url}/api/session/{session_id}/close",
                headers=self.get_headers(token),
                json=payload,
                timeout=8
            )
            return res.status_code in (200, 204)
        except Exception as e:
            logger.debug("ABS close_playback_session notice for %s: %s", session_id, e)
            return False

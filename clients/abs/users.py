"""
Audiobookshelf user matching, per-user/per-profile API key provisioning & verification mixin.
"""

from typing import Any, Dict, List, Optional
import requests
from core.config import logger


class ABSUsersMixin:
    """User lookup, API key minting/revocation, and token resolution for ABSClient."""

    def get_user_token(self, username: str) -> Optional[str]:
        """
        Retrieves a scoped Bearer auth token for a given Audiobookshelf username.
        Prefers legacy user token if returned by /api/users, and automatically mints
        a scoped 'Act on behalf of user' API key via POST /api/api-keys if legacy tokens
        are absent or disabled. Caches results in-memory.
        """
        if not username or not self.api_key or not self.base_url:
            return None

        uname_clean = username.strip().lower()
        if uname_clean in self._user_token_cache:
            return self._user_token_cache[uname_clean]

        try:
            headers = self.get_headers(self.api_key)
            res = requests.get(f"{self.base_url}/api/users", headers=headers, timeout=6)
            if res.status_code == 200:
                matched_user = None
                for u in res.json().get("users", []):
                    u_name = (u.get("username") or "").strip().lower()
                    u_token = u.get("token")
                    if u_token:
                        self._user_token_cache[u_name] = u_token
                    if u_name == uname_clean:
                        matched_user = u
                if uname_clean in self._user_token_cache:
                    return self._user_token_cache[uname_clean]
                if matched_user and matched_user.get("id"):
                    minted = self.provision_user_api_key(
                        str(matched_user["id"]),
                        str(matched_user.get("username") or username),
                    )
                    if minted and minted.get("api_key"):
                        self._user_token_cache[uname_clean] = minted["api_key"]
                        return minted["api_key"]
        except Exception as e:
            logger.debug("Failed to retrieve Audiobookshelf user token for '%s': %s", username, e)
        return None

    def match_abs_user(self, email: str = "", username: str = "") -> Optional[Dict[str, Any]]:
        """
        Matches a StoryDeck user against Audiobookshelf /api/users using the same
        priority order as Authentik SSO:
        1. Case-insensitive non-empty email match
        2. Case-insensitive username match
        """
        clean_email = (email or "").strip().lower()
        clean_uname = (username or "").strip().lower()
        if not clean_email and not clean_uname:
            return None

        users = self.get_all_users()
        if not users:
            return None

        # 1. Priority: Email match
        if clean_email:
            for u in users:
                if not u.get("isActive", True):
                    continue
                u_email = str(u.get("email") or "").strip().lower()
                if u_email and u_email == clean_email:
                    return {
                        "id": str(u.get("id") or ""),
                        "username": str(u.get("username") or ""),
                        "email": str(u.get("email") or ""),
                        "type": str(u.get("type") or "user"),
                        "libraries_accessible": u.get("librariesAccessible") or [],
                        "token": str(u.get("token") or ""),
                        "matched_by": "email",
                    }

        # 2. Fallback: Username match
        if clean_uname:
            for u in users:
                if not u.get("isActive", True):
                    continue
                u_name = str(u.get("username") or "").strip().lower()
                if u_name and u_name == clean_uname:
                    return {
                        "id": str(u.get("id") or ""),
                        "username": str(u.get("username") or ""),
                        "email": str(u.get("email") or ""),
                        "type": str(u.get("type") or "user"),
                        "libraries_accessible": u.get("librariesAccessible") or [],
                        "token": str(u.get("token") or ""),
                        "matched_by": "username",
                    }

        return None

    @staticmethod
    def extract_api_key_id_from_jwt(token: str) -> str:
        """Extracts the ABS 'keyId' UUID from a v2.26+ Audiobookshelf API key JWT payload, if present."""
        clean = (token or "").strip()
        if clean.count(".") != 2:
            return ""
        try:
            import base64
            import json
            b64 = clean.split(".")[1]
            b64 += "=" * (-len(b64) % 4)
            claims = json.loads(base64.urlsafe_b64decode(b64).decode("utf-8"))
            if isinstance(claims, dict) and claims.get("keyId"):
                return str(claims["keyId"]).strip()
        except Exception:
            pass
        return ""

    def revoke_storydeck_api_keys_for_user(
        self,
        abs_user_id: str = "",
        abs_username: str = "",
        label_prefix: str = "StoryDeck Core",
        raw_api_key: str = "",
        exclude_key_ids: Optional[List[str]] = None,
    ) -> int:
        """
        Deletes previously auto-minted StoryDeck API keys for the given user on Audiobookshelf.
        Deletes both by exact JWT 'keyId' (extracted from raw_api_key) and by matching
        userId/username + label_prefix on GET /api/api-keys, while never deleting Core's own master key
        or any ID listed in exclude_key_ids.
        """
        clean_uid = (abs_user_id or "").strip()
        clean_uname = (abs_username or "").strip()
        if not self.api_key or not self.base_url:
            return 0

        master_key_id = self.extract_api_key_id_from_jwt(self.api_key)
        protected_ids = {kid for kid in (exclude_key_ids or []) if kid}
        if master_key_id:
            protected_ids.add(master_key_id)

        jwt_key_id = ""
        if raw_api_key and raw_api_key.strip() != self.api_key.strip():
            jwt_key_id = self.extract_api_key_id_from_jwt(raw_api_key)
            if jwt_key_id in protected_ids:
                jwt_key_id = ""

        deleted_ids = set()
        headers = self.get_headers(self.api_key)

        # 1. Direct deletion by exact JWT keyId if available
        if jwt_key_id:
            try:
                del_res = requests.delete(
                    f"{self.base_url}/api/api-keys/{jwt_key_id}",
                    headers=headers,
                    timeout=8,
                )
                if del_res.status_code in (200, 204):
                    deleted_ids.add(jwt_key_id)
            except Exception as e:
                logger.debug("ABS direct keyId delete notice for %s: %s", jwt_key_id, e)

        # 2. Sweep GET /api/api-keys for any matching StoryDeck keys for this user
        if clean_uid or clean_uname or jwt_key_id:
            try:
                res = requests.get(f"{self.base_url}/api/api-keys", headers=headers, timeout=8)
                if res.status_code == 200:
                    body = res.json()
                    keys = body.get("apiKeys", []) if isinstance(body, dict) else []
                    for k in keys:
                        k_id = str(k.get("id") or "").strip()
                        if not k_id or k_id in deleted_ids or k_id in protected_ids:
                            continue
                        k_uid = str(k.get("userId") or "").strip()
                        k_uname = str((k.get("user") or {}).get("username") or "").strip()
                        k_name = str(k.get("name") or "")
                        id_match = bool(jwt_key_id and k_id == jwt_key_id)
                        user_match = bool(
                            (clean_uid and k_uid == clean_uid)
                            or (clean_uname and k_uname and k_uname.lower() == clean_uname.lower())
                        )
                        if id_match or (user_match and k_name.startswith(label_prefix)):
                            del_res = requests.delete(
                                f"{self.base_url}/api/api-keys/{k_id}",
                                headers=headers,
                                timeout=8,
                            )
                            if del_res.status_code in (200, 204):
                                deleted_ids.add(k_id)
            except Exception as e:
                logger.debug("ABS revoke_storydeck_api_keys_for_user notice for %s: %s", clean_uname or clean_uid, e)

        if clean_uname and hasattr(self, "_user_token_cache"):
            self._user_token_cache.pop(clean_uname.lower(), None)
        return len(deleted_ids)

    def provision_user_api_key(
        self,
        abs_user_id: str,
        abs_username: str = "",
        label_prefix: str = "StoryDeck Core",
        fallback_token: str = "",
        old_api_key: str = "",
    ) -> Optional[Dict[str, str]]:
        """
        Mints a dedicated 'Act on behalf of user' API key in Audiobookshelf v2.26+
        via POST /api/api-keys using the master Admin/Root API key, and only deletes
        any prior StoryDeck Core key for that user AFTER the new key is minted so a
        403/failure never wipes out an existing working key. Falls back to legacy user
        token if /api/api-keys is unsupported or restricted.
        """
        clean_uid = (abs_user_id or "").strip()
        clean_uname = (abs_username or "").strip()
        if not clean_uid or not self.api_key or not self.base_url:
            return None

        key_name = f"{label_prefix} ({clean_uname or clean_uid})"
        last_status = 0
        try:
            payload = {
                "name": key_name,
                "description": f"Auto-provisioned by StoryDeck Core for ABS user '{clean_uname or clean_uid}'",
                "userId": clean_uid,
                "isActive": True,
            }
            res = requests.post(
                f"{self.base_url}/api/api-keys",
                headers=self.get_headers(self.api_key),
                json=payload,
                timeout=8,
            )
            last_status = res.status_code
            if res.status_code in (200, 201):
                data = res.json() or {}
                ak_obj = data.get("apiKey") if isinstance(data, dict) else {}
                raw_jwt = str((ak_obj or {}).get("apiKey") or "").strip()
                new_key_id = str((ak_obj or {}).get("id") or "").strip() or self.extract_api_key_id_from_jwt(raw_jwt)
                if raw_jwt:
                    self.revoke_storydeck_api_keys_for_user(
                        clean_uid,
                        clean_uname,
                        label_prefix=label_prefix,
                        raw_api_key=old_api_key,
                        exclude_key_ids=[new_key_id] if new_key_id else None,
                    )
                    if clean_uname and hasattr(self, "_user_token_cache"):
                        self._user_token_cache[clean_uname.lower()] = raw_jwt
                    return {
                        "api_key": raw_jwt,
                        "api_key_id": new_key_id,
                        "source": "api_key",
                    }
            logger.debug("ABS POST /api/api-keys returned HTTP %s", res.status_code)
        except Exception as e:
            logger.debug("ABS provision_user_api_key notice for %s: %s", clean_uname or clean_uid, e)

        # Fallback to legacy user.token if available
        tok = (fallback_token or "").strip()
        if not tok and clean_uname:
            for u in self.get_all_users():
                if str(u.get("id") or "") == clean_uid or str(u.get("username") or "").strip().lower() == clean_uname.lower():
                    tok = str(u.get("token") or "").strip()
                    break
        if tok:
            if clean_uname and hasattr(self, "_user_token_cache"):
                self._user_token_cache[clean_uname.lower()] = tok
            return {
                "api_key": tok,
                "api_key_id": "",
                "source": "legacy_token",
            }
        self._last_provision_http_status = last_status
        return None

    def check_master_key_permissions(self) -> Dict[str, Any]:
        """
        Verifies the configured Master ABS API key against GET /api/me and checks
        whether it is affiliated with an Admin/Root ABS user capable of auto-generating
        per-user and per-profile scoped API keys.
        """
        if not self.base_url or not self.api_key:
            return {
                "connected": False,
                "is_admin": False,
                "is_root": False,
                "username": "",
                "type": "",
                "warning": "",
                "note": "",
            }
        info = self.verify_user_token(self.api_key)
        if not info or not info.get("username"):
            return {
                "connected": False,
                "is_admin": False,
                "is_root": False,
                "username": "",
                "type": "",
                "warning": "",
                "note": "",
            }
        acc_type = str(info.get("type") or "user").strip().lower()
        uname = str(info.get("username") or "").strip()
        is_root = acc_type == "root"
        is_admin = acc_type in ("root", "admin")
        warning = ""
        note = ""
        if not is_admin:
            warning = (
                f"Master API key is affiliated with non-admin ABS user '{uname}' (type: {acc_type}). "
                "StoryDeck cannot auto-generate scoped ABS API keys—an ABS Admin will need to manually "
                "generate 'Act on behalf of' API keys for users and profiles for personalized listening settings."
            )
        elif not is_root:
            note = (
                f"Standard Admin key ('{uname}') can auto-generate scoped API keys for 'user' and 'admin' "
                "Audiobookshelf accounts, but Audiobookshelf restricts 'root' user key generation to 'root' API keys."
            )
        return {
            "connected": True,
            "is_admin": is_admin,
            "is_root": is_root,
            "username": uname,
            "type": acc_type,
            "warning": warning,
            "note": note,
        }

    def provision_profile_api_key(self, profile_id: str, abs_username: str) -> Optional[Dict[str, str]]:
        """
        Matches a Profile's assigned abs_username against ABS /api/users and auto-mints
        a dedicated 'Act on behalf of user' API key scoped to that profile.
        """
        clean_pid = (profile_id or "").strip().lower()
        clean_uname = (abs_username or "").strip()
        if not clean_pid or not clean_uname or not self.base_url or not self.api_key:
            return None
        matched = self.match_abs_user(username=clean_uname)
        if not matched or not matched.get("id"):
            return None
        return self.provision_user_api_key(
            matched["id"],
            matched.get("username") or clean_uname,
            label_prefix=f"StoryDeck Profile [{clean_pid}]",
            fallback_token=matched.get("token") or "",
        )

    def revoke_profile_api_key(self, profile_id: str, abs_username: str = "") -> int:
        """Revokes any auto-minted StoryDeck Profile [<profile_id>] API key in Audiobookshelf."""
        clean_pid = (profile_id or "").strip().lower()
        clean_uname = (abs_username or "").strip()
        if not clean_pid or not self.base_url or not self.api_key:
            return 0
        matched = self.match_abs_user(username=clean_uname) if clean_uname else None
        if matched and matched.get("id"):
            return self.revoke_storydeck_api_keys_for_user(
                matched["id"],
                matched.get("username") or clean_uname,
                label_prefix=f"StoryDeck Profile [{clean_pid}]",
            )
        return 0

    def get_all_users(self) -> List[Dict[str, Any]]:
        """Fetches all configured Audiobookshelf user accounts using the master API key."""
        if not self.api_key or not self.base_url:
            return []
        try:
            res = requests.get(f"{self.base_url}/api/users", headers=self.get_headers(self.api_key), timeout=8)
            if res.status_code == 200:
                return res.json().get("users", [])
        except Exception as e:
            logger.debug("ABS get_all_users error: %s", e)
        return []

    def get_user_media_progress(self, user_id: str) -> List[Dict[str, Any]]:
        """Fetches mediaProgress entries for a specific Audiobookshelf user ID."""
        if not user_id or not self.api_key or not self.base_url:
            return []
        try:
            res = requests.get(f"{self.base_url}/api/users/{user_id}", headers=self.get_headers(self.api_key), timeout=8)
            if res.status_code == 200:
                return res.json().get("mediaProgress", [])
        except Exception as e:
            logger.debug("ABS get_user_media_progress error for '%s': %s", user_id, e)
        return []


    def verify_user_token(self, token: str) -> Optional[Dict[str, Any]]:
        """
        Queries GET /api/me using a user's personal ABS API key/token to detect
        their ABS username, user ID, account type, and accessible libraries.
        """
        clean_tok = (token or "").strip()
        if not clean_tok or not self.base_url:
            return None
        try:
            res = requests.get(
                f"{self.base_url}/api/me",
                headers=self.get_headers(clean_tok),
                timeout=8,
            )
            if res.status_code == 200:
                data = res.json() or {}
                return {
                    "id": str(data.get("id") or ""),
                    "username": str(data.get("username") or ""),
                    "type": str(data.get("type") or "user"),
                    "is_active": bool(data.get("isActive", True)),
                    "libraries_accessible": data.get("librariesAccessible") or [],
                }
            logger.debug("ABS verify_user_token HTTP %s", res.status_code)
        except Exception as e:
            logger.warning("ABS verify_user_token error: %s", e)
        return None

    def resolve_token_for_profile(self, profile_id: Optional[str] = None,
                                  device_id: Optional[str] = None) -> Optional[str]:
        """
        Resolves the Audiobookshelf Bearer token scoped to an adult StoryDeck user ('user:<uid>'),
        a profile, or a Yoto player ID, falling back to the master API key if unassigned.
        """
        if not self.base_url:
            return None
        try:
            import database
            if profile_id and str(profile_id).startswith("user:"):
                uid = str(profile_id)[5:].strip()
                if not uid or uid == "lan_guest":
                    return self.api_key or None
                creds = database.get_user_abs_credentials(uid)
                if creds.get("abs_api_key"):
                    return creds["abs_api_key"]
                if creds.get("abs_username"):
                    resolved = self.get_user_token(creds["abs_username"])
                    if resolved:
                        return resolved
                return self.api_key or None

            profiles = database.get_all_profiles()
            target_cfg = None
            if profile_id and profile_id in profiles:
                target_cfg = profiles[profile_id]
            elif device_id:
                for p_cfg in profiles.values():
                    if p_cfg.get("default_player_id") == device_id or device_id in (p_cfg.get("player_ids") or []):
                        target_cfg = p_cfg
                        break

            if target_cfg:
                explicit = (target_cfg.get("abs_api_key") or target_cfg.get("abs_token") or "").strip()
                if explicit:
                    return explicit
                uname = (target_cfg.get("abs_username") or "").strip()
                if uname:
                    resolved = self.get_user_token(uname)
                    if resolved:
                        return resolved
        except Exception as e:
            logger.debug("Profile token resolution notice (%s/%s): %s", profile_id, device_id, e)

        return self.api_key or None


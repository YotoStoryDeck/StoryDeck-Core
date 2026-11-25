"""
Audiobookshelf API client for library, item, and user metadata querying.
"""

import re
from typing import Dict, Any, List, Optional, Tuple
import requests
from core.config import logger


class ABSClient:
    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip("/") if base_url else ""
        self.api_key = api_key.strip() if api_key else ""
        self._user_token_cache: Dict[str, str] = {}
        self._invalid_user_tokens: set[str] = set()

    @staticmethod
    def is_valid_audio_item(item: Dict[str, Any]) -> bool:
        """
        Returns True only if an Audiobookshelf library item is a playable audio item
        (not missing/invalid, has >0 audio tracks/files, and has >0 audio duration).
        Excludes ebook-only (EPUB, PDF, MOBI, AZW3, CBZ, CBR, TXT) and missing items.
        """
        if not isinstance(item, dict):
            return False
        if item.get("isMissing") or item.get("isInvalid"):
            return False
        media = item.get("media") if isinstance(item.get("media"), dict) else {}
        dur = float(media.get("duration") or 0.0)
        num_audio = int(
            media.get("numAudioFiles")
            or media.get("numTracks")
            or len(media.get("audioFiles") or [])
            or len(media.get("tracks") or [])
            or 0
        )
        return dur > 0.0 and num_audio > 0

    def get_headers(self, token: Optional[str] = None) -> Dict[str, str]:
        auth_token = token if token else self.api_key
        return {
            "Authorization": f"Bearer {auth_token}",
            "Accept": "application/json"
        }

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

    def get_libraries(self, token: Optional[str] = None) -> List[Dict[str, Any]]:
        """Fetches all ABS libraries visible to the supplied token (or default api_key)."""
        headers = self.get_headers(token)
        if not headers.get("Authorization"):
            return []
        try:
            res = requests.get(f"{self.base_url}/api/libraries", headers=headers, timeout=10)
            if res.status_code == 200:
                return res.json().get("libraries", [])
        except Exception as e:
            logger.error("ABS get_libraries error: %s", e)
        return []

    def get_items_for_profile(self, profile_cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Retrieves audiobooks scoped for a specific profile:
        1. Token/API Key:
           - Explicit override: `abs_api_key` or `abs_token`
           - Auto-resolved: queries profile account's token via `abs_username` using master key
           - Fallback: master `self.api_key` (if unassigned)
        2. Audiobookshelf Server Filtering:
           - If using profile account token, ABS automatically applies user permissions,
             allowed libraries, tag scoping (e.g. 'Children'), and explicit content limits!
        3. Local Overrides:
           - Limits scanning to `abs_library_id` if configured.
           - Filters by `abs_tag` if configured.
        """
        abs_username = (profile_cfg.get("abs_username") or "").strip()
        explicit_token = (profile_cfg.get("abs_api_key") or profile_cfg.get("abs_token") or "").strip()
        resolved_token = None

        if explicit_token:
            resolved_token = explicit_token
        elif abs_username:
            resolved_token = self.get_user_token(abs_username)

        token = resolved_token or self.api_key
        if not token:
            logger.warning("No ABS API key or profile token configured.")
            return []

        target_lib_id = (profile_cfg.get("abs_library_id") or "").strip()
        tag_filter = (profile_cfg.get("abs_tag") or "").strip().lower()

        headers = self.get_headers(token)
        libraries = self.get_libraries(token)
        results: List[Dict[str, Any]] = []

        for lib in libraries:
            lib_id = lib.get("id")
            media_type = lib.get("mediaType", "book")
            if media_type != "book":
                continue

            # Check library ID constraint if specified
            if target_lib_id and lib_id != target_lib_id and lib.get("name", "").lower() != target_lib_id.lower():
                continue

            try:
                url = f"{self.base_url}/api/libraries/{lib_id}/items?limit=0"
                res = requests.get(url, headers=headers, timeout=15)
                if res.status_code != 200:
                    continue

                items = res.json().get("results", [])
                for item in items:
                    if not self.is_valid_audio_item(item):
                        continue
                    media = item.get("media", {})
                    metadata = media.get("metadata", {})
                    tags = [t.lower() for t in (metadata.get("tags") or item.get("tags") or [])]

                    # If tag filter is specified, require match; otherwise include all items in library
                    if tag_filter and tag_filter not in tags:
                        continue

                    item_id = item.get("id")
                    title = metadata.get("title") or item.get("name") or "Untitled"
                    author = metadata.get("authorName") or metadata.get("author") or "Unknown"

                    # Parse all series names and sequence values if available
                    raw_series = metadata.get("seriesName") or ""
                    series_list = []
                    fallback_seq = None
                    fallback_seq_disp = None
                    if title:
                        m_range = re.search(r"\b(?:book|volume|vol\.?)\s*(\d+(?:\.\d+)?\s*[-–—]\s*\d+(?:\.\d+)?)", title, re.IGNORECASE)
                        if m_range:
                            fallback_seq_disp = re.sub(r"\s+", "", m_range.group(1))
                            m_lead = re.search(r"(\d+(?:\.\d+)?)", fallback_seq_disp)
                            if m_lead:
                                try:
                                    fallback_seq = float(m_lead.group(1))
                                except (ValueError, TypeError):
                                    pass
                        else:
                            m_book = re.search(r"\b(?:book|volume|vol\.?)\s*(\d+(?:\.\d+)?)", title, re.IGNORECASE)
                            if m_book:
                                try:
                                    fallback_seq = float(m_book.group(1))
                                    fallback_seq_disp = str(m_book.group(1))
                                except (ValueError, TypeError):
                                    pass

                    if raw_series:
                        parts = [p.strip() for p in str(raw_series).split(",") if p.strip()]
                        for part in parts:
                            s_name = part
                            s_seq = fallback_seq
                            s_seq_disp = fallback_seq_disp
                            m = re.match(r"^(.*?)\s*#(\d+(?:\.\d+)?(?:\s*[-–—]\s*\d+(?:\.\d+)?)?)", part)
                            if m:
                                s_name = m.group(1).strip()
                                raw_val = m.group(2).strip()
                                s_seq_disp = re.sub(r"\s+", "", raw_val)
                                fnum = re.match(r"^(\d+(?:\.\d+)?)", s_seq_disp)
                                if fnum:
                                    try:
                                        s_seq = float(fnum.group(1))
                                    except (ValueError, TypeError):
                                        pass
                            if s_name:
                                series_list.append({
                                    "name": s_name,
                                    "sequence": s_seq,
                                    "sequence_display": s_seq_disp
                                })

                    primary_series = series_list[0]["name"] if series_list else ""
                    primary_sequence = series_list[0]["sequence"] if series_list else fallback_seq
                    primary_sequence_disp = series_list[0]["sequence_display"] if series_list else fallback_seq_disp

                    results.append({
                        "id": item_id,
                        "type": "abs",
                        "title": title,
                        "author": author,
                        "thumb": f"/api/thumb?type=abs&id={item_id}",
                        "series": primary_series,
                        "sequence": primary_sequence,
                        "sequence_display": primary_sequence_disp,
                        "series_list": series_list
                    })
            except Exception as e:
                logger.error("Error querying ABS library %s: %s", lib_id, e)

        # Sort ignoring leading articles 'The', 'A', 'An'
        results.sort(key=lambda x: re.sub(r"^(the|a|an)\s+", "", x["title"].strip(), flags=re.IGNORECASE).lower())
        return results

    def get_item(self, item_id: str, token: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """
        Fetches full expanded item details including tracks, audioFiles, chapters, and description from Audiobookshelf.
        Automatically falls back to the master API key if a scoped user token returns 401/403 (and remembers invalid user tokens).
        """
        eff_tok = token if (token and token.strip() not in self._invalid_user_tokens) else None
        headers = self.get_headers(eff_tok)
        try:
            url = f"{self.base_url}/api/items/{item_id}"
            res = requests.get(url, headers=headers, params={"expanded": 1}, timeout=12)
            if res.status_code == 200:
                return res.json()
            if res.status_code in (401, 403) and eff_tok and self.api_key and eff_tok.strip() != self.api_key.strip():
                self._invalid_user_tokens.add(eff_tok.strip())
                logger.info("Scoped ABS token rejected (%d); falling back to master API key.", res.status_code)
                res_master = requests.get(url, headers=self.get_headers(self.api_key), params={"expanded": 1}, timeout=12)
                if res_master.status_code == 200:
                    return res_master.json()
        except Exception as e:
            logger.error("ABS get_item error for %s: %s", item_id, e)
        return None

    def update_item_chapters(self, item_id: str, chapters: List[Dict[str, Any]]) -> bool:
        """Updates the chapter structure / track titles on an Audiobookshelf book via POST /api/items/{id}/chapters."""
        if not self.api_key:
            logger.warning("Cannot update ABS chapters: master API key not configured")
            return False
        headers = self.get_headers()
        try:
            url = f"{self.base_url}/api/items/{item_id}/chapters"
            res = requests.post(url, headers=headers, json={"chapters": chapters}, timeout=15)
            if res.status_code in (200, 204):
                logger.info("Successfully updated %d chapters on ABS item %s", len(chapters), item_id)
                return True
            logger.warning("Failed to update ABS chapters for %s (%d): %s", item_id, res.status_code, res.text)
        except Exception as e:
            logger.error("Exception updating ABS chapters for %s: %s", item_id, e)
        return False

    def update_item_tags(self, item_id: str, tags: List[str]) -> bool:
        """Updates the tags on an Audiobookshelf book via PATCH /api/items/{id}/media."""
        if not self.api_key:
            logger.warning("Cannot update ABS tags: master API key not configured")
            return False
        headers = self.get_headers()
        try:
            url = f"{self.base_url}/api/items/{item_id}/media"
            res = requests.patch(url, headers=headers, json={"tags": tags}, timeout=12)
            if res.status_code in (200, 204):
                logger.info("Successfully updated tags on ABS item %s: %s", item_id, tags)
                return True
            logger.warning("Failed to update ABS tags for %s (%d): %s", item_id, res.status_code, res.text)
        except Exception as e:
            logger.error("Exception updating ABS tags for %s: %s", item_id, e)
        return False

    def get_all_server_tags(self) -> List[str]:
        """Queries the complete list of unique tags in Audiobookshelf via /api/tags."""
        if not self.api_key:
            return []
        headers = self.get_headers()
        try:
            url = f"{self.base_url}/api/tags"
            res = requests.get(url, headers=headers, timeout=10)
            if res.status_code == 200:
                tags = res.json().get("tags", [])
                tags.sort(key=lambda s: s.lower())
                return tags
        except Exception as e:
            logger.error("Exception fetching ABS server tags: %s", e)
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

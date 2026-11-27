"""
Audiobookshelf library discovery, profile catalog filtering & metadata mutation mixin.
"""

import re
from typing import Any, Dict, List, Optional
import requests
from core.config import logger


class ABSLibraryMixin:
    """Library/item querying and chapter/tag editing for ABSClient."""

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


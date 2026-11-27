"""
Composed ABSClient uniting user management, library/metadata, and progress sync mixins.
"""

from typing import Dict, Optional
from .users import ABSUsersMixin
from .library import ABSLibraryMixin
from .progress import ABSProgressMixin


class ABSClient(ABSUsersMixin, ABSLibraryMixin, ABSProgressMixin):
    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip("/") if base_url else ""
        self.api_key = api_key.strip() if api_key else ""
        self._user_token_cache: Dict[str, str] = {}
        self._invalid_user_tokens: set[str] = set()

    def get_headers(self, token: Optional[str] = None) -> Dict[str, str]:
        auth_token = token if token else self.api_key
        return {
            "Authorization": f"Bearer {auth_token}",
            "Accept": "application/json",
        }

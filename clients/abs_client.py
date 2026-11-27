"""
Backwards-compatible facade re-exporting modularized ABSClient.
Keeps `requests` imported at module level so existing tests patching `clients.abs_client.requests.*` continue to work.
"""

import requests  # noqa: F401
from clients.abs import ABSClient

__all__ = ["ABSClient"]

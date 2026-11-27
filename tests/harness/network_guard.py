"""Outbound HTTP network firewall for StoryDeck Core unit and snapshot tests.

Blocks unmocked HTTP/HTTPS calls inside ``urllib3`` so forgotten mocks fail in
0ms instead of hanging for 2-40 seconds on DNS resolution or TCP connect retries.
Tests wrapped with ``@patch("requests.get", ...)`` or client method mocks do not
reach ``urllib3`` and continue to work normally.
"""
from contextlib import contextmanager
import os
import threading


class NetworkBlockedInTestsError(RuntimeError):
    """Raised when a test attempts an unmocked outbound HTTP request."""


_ORIGINAL_URLOPEN = None
_STATE_LOCK = threading.Lock()
_THREAD_STATE = threading.local()


def _is_live_allowed() -> bool:
    if os.getenv("STORYDECK_ALLOW_NETWORK", "").lower() in ("1", "true", "yes"):
        return True
    return getattr(_THREAD_STATE, "allow_depth", 0) > 0


def _guarded_urlopen(self, method, url, *args, **kwargs):
    if not _is_live_allowed():
        host = getattr(self, "host", "<unknown>")
        port = getattr(self, "port", "")
        port_part = f":{port}" if port else ""
        raise NetworkBlockedInTestsError(
            f"Blocked unmocked outbound HTTP request in tests: "
            f"{method} http://{host}{port_part}{url}. "
            f"Mock the upstream client/requests call or use NetworkGuard.allow_live()."
        )
    return _ORIGINAL_URLOPEN(self, method, url, *args, **kwargs)


class NetworkGuard:
    """Process-wide guard against unmocked outbound HTTP traffic during tests."""

    @classmethod
    def install(cls) -> bool:
        """Installs the urllib3 urlopen guard if not already active."""
        global _ORIGINAL_URLOPEN
        with _STATE_LOCK:
            if _ORIGINAL_URLOPEN is not None:
                return True
            try:
                import urllib3.connectionpool as _cp
            except ImportError:
                return False
            _ORIGINAL_URLOPEN = _cp.HTTPConnectionPool.urlopen
            _cp.HTTPConnectionPool.urlopen = _guarded_urlopen
            return True

    @classmethod
    def uninstall(cls) -> None:
        """Restores the original urllib3 urlopen implementation."""
        global _ORIGINAL_URLOPEN
        with _STATE_LOCK:
            if _ORIGINAL_URLOPEN is None:
                return
            import urllib3.connectionpool as _cp
            _cp.HTTPConnectionPool.urlopen = _ORIGINAL_URLOPEN
            _ORIGINAL_URLOPEN = None

    @classmethod
    def is_active(cls) -> bool:
        """Returns True when the guard is installed and currently enforcing."""
        return _ORIGINAL_URLOPEN is not None and not _is_live_allowed()

    @classmethod
    @contextmanager
    def allow_live(cls):
        """Temporarily permits live outbound HTTP calls on the current thread."""
        prev = getattr(_THREAD_STATE, "allow_depth", 0)
        _THREAD_STATE.allow_depth = prev + 1
        try:
            yield
        finally:
            _THREAD_STATE.allow_depth = prev

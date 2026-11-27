"""Ephemeral filesystem, SQLite DB, credential, and singleton cache isolation."""
import atexit
import itertools
import logging
import os
from pathlib import Path
import shutil
import sys
import tempfile
from typing import Optional

from tests.harness.network_guard import NetworkGuard

_logger = logging.getLogger("storydeck.tests.harness")
_ISOLATION_DIR: Optional[Path] = None
_DB_COUNTER = itertools.count(1)
_DEFAULT_DB_PATH: Optional[Path] = None

_LIVE_CREDENTIAL_ENV_VARS = (
    "ABS_BASE_URL",
    "ABS_API_KEY",
    "YOTO_CLIENT_ID",
    "YOTO_CLIENT_SECRET",
    "YOTO_REFRESH_TOKEN",
    "YOTO_BRIDGE_URL",
    "YOTO_BRIDGE_API_KEY",
    "YOTO_UI_PIN",
    "TINKER_API_KEY",
    "GEMINI_API_KEY",
    "ANTHROPIC_API_KEY",
    "OPENROUTER_API_KEY",
)


def ensure_isolated_test_env() -> Path:
    """Sandboxes all writable StoryDeck storage paths & installs NetworkGuard.

    Safety guarantee: legacy ``STORYDECK_USE_LIVE_DB=1`` is NEVER permitted to
    open ``data/broker.db`` in-place; if detected without ``STORYDECK_SNAPSHOT_DB``,
    it is transparently redirected into safe read-only copy-and-sanitize mode.
    """
    global _ISOLATION_DIR, _DEFAULT_DB_PATH
    if _ISOLATION_DIR is not None:
        NetworkGuard.install()
        return _ISOLATION_DIR

    if os.getenv("STORYDECK_USE_LIVE_DB", "").lower() in ("1", "true", "yes"):
        _logger.warning(
            "STORYDECK_USE_LIVE_DB=1 is deprecated and never mutates data/broker.db in-place; "
            "redirecting to sanitized snapshot mode (STORYDECK_SNAPSHOT_DB=1)."
        )
        os.environ.setdefault("STORYDECK_SNAPSHOT_DB", "1")
        os.environ.pop("STORYDECK_USE_LIVE_DB", None)

    tmp = Path(tempfile.mkdtemp(prefix="storydeck_test_"))
    _ISOLATION_DIR = tmp
    _DEFAULT_DB_PATH = tmp / "test_broker.db"

    os.environ["STORYDECK_TESTING"] = "1"
    os.environ["STORYDECK_DISABLE_SCHEDULER"] = "1"
    os.environ["DB_PATH"] = str(_DEFAULT_DB_PATH)
    os.environ["CONFIG_PATH"] = str(tmp / "no_legacy_config.json")
    os.environ["CACHE_DIR"] = str(tmp / "cache")
    os.environ["AVATARS_DIR"] = str(tmp / "avatars")
    os.environ["CUSTOM_ICONS_DIR"] = str(tmp / "custom_icons")
    os.environ["FIRMWARE_DIR"] = str(tmp / "firmware")

    for env_key in _LIVE_CREDENTIAL_ENV_VARS:
        os.environ[env_key] = ""

    NetworkGuard.install()
    atexit.register(lambda p=tmp: shutil.rmtree(p, ignore_errors=True))
    return tmp


def get_isolation_root() -> Path:
    """Returns the shared ephemeral root directory for this test process."""
    return ensure_isolated_test_env()


def get_default_db_path() -> Path:
    """Returns the initial synthetic DB path inside the ephemeral root."""
    ensure_isolated_test_env()
    assert _DEFAULT_DB_PATH is not None
    return _DEFAULT_DB_PATH


def reset_runtime_caches() -> None:
    """Clears process-level caches and singletons so tests stay strictly atomic."""
    if "core.bridge.quiet" in sys.modules:
        sys.modules["core.bridge.quiet"]._quiet_spots = None

    if "core.bridge.manager" in sys.modules:
        sys.modules["core.bridge.manager"]._bridge_manager = None

    if "core.bridge.offline" in sys.modules:
        sys.modules["core.bridge.offline"]._offline_manager = None

    if "core.bridge.playback" in sys.modules:
        pb_mod = sys.modules["core.bridge.playback"]
        if getattr(pb_mod, "_playback_manager", None) is not None:
            try:
                pb_mod._playback_manager._sessions.clear()
            except Exception:
                pass
        pb_mod._playback_manager = None

    if "core.player_service" in sys.modules:
        ps = sys.modules["core.player_service"]
        ps._cached_players_map.clear()
        ps._cached_players_ts = 0.0
        ps._cards_cache.clear()
        ps._cards_cache_ts = 0.0
        ps._player_home_presence.clear()
        with ps._sleep_timer_lock:
            for entry in list(ps._player_sleep_timers.values()):
                timer = entry.get("timer")
                if timer:
                    try:
                        timer.cancel()
                    except Exception:
                        pass
            ps._player_sleep_timers.clear()

    if "core.version_service" in sys.modules:
        vc = getattr(sys.modules["core.version_service"], "_VERSION_CACHE", None)
        if isinstance(vc, dict):
            vc["ts"] = 0.0
            vc["data"] = None

    if "core.touch_release_service" in sys.modules:
        tc = getattr(sys.modules["core.touch_release_service"], "_TOUCH_CACHE", None)
        if isinstance(tc, dict):
            tc["ts"] = 0.0
            tc["data"] = None

    if "clients" in sys.modules and "database" in sys.modules:
        try:
            sys.modules["clients"].reload_clients()
        except Exception:
            pass


def rebind_test_db(new_db_path: Path) -> Path:
    """Updates all imported ``DB_PATH`` bindings to ``new_db_path`` and clears caches."""
    resolved = Path(new_db_path)
    os.environ["DB_PATH"] = str(resolved)
    for mod_name in ("database.connection", "database", "database.schema", "database.settings"):
        mod = sys.modules.get(mod_name)
        if mod is not None and hasattr(mod, "DB_PATH"):
            setattr(mod, "DB_PATH", resolved)
    reset_runtime_caches()
    return resolved


def reset_test_db(db_path: Optional[Path] = None) -> Path:
    """Switches to a brand-new SQLite database file and initializes schema v17.

    Using a monotonically numbered filename inside the temp dir avoids Windows
    SQLite WAL file-handle ``PermissionError`` contention when resetting rapidly.
    """
    root = ensure_isolated_test_env()
    target = Path(db_path) if db_path is not None else (root / f"unit_{next(_DB_COUNTER)}.db")
    rebind_test_db(target)
    if "database" in sys.modules:
        sys.modules["database"].init_db()
    else:
        import database
        database.init_db()
    reset_runtime_caches()
    return target

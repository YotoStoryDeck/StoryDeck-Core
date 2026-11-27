"""Base TestCase classes for isolated synthetic suites and optional local DB snapshots."""
import itertools
from pathlib import Path
import unittest

from tests.harness.isolation import (
    ensure_isolated_test_env,
    get_isolation_root,
    rebind_test_db,
    reset_test_db,
)
from tests.harness.network_guard import NetworkGuard
from tests.harness.snapshot import create_sanitized_snapshot, resolve_snapshot_source

_SNAP_COUNTER = itertools.count(1)


class IsolatedTestCase(unittest.TestCase):
    """Base TestCase enforcing clean per-class DB state and NetworkGuard."""

    reset_db_per_test = False

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        ensure_isolated_test_env()
        reset_test_db()

    def setUp(self):
        super().setUp()
        NetworkGuard.install()
        if self.reset_db_per_test:
            reset_test_db()


class SnapshotTestCase(unittest.TestCase):
    """Base TestCase for opt-in tests running against a sanitized local DB copy."""

    source_db_path: Path = None
    snapshot_db_path: Path = None
    sanitize_stats: dict = {}

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        ensure_isolated_test_env()
        src = resolve_snapshot_source()
        if src is None:
            raise unittest.SkipTest(
                "Optional local DB snapshot disabled/missing "
                "(run with --with-local-db or set STORYDECK_SNAPSHOT_DB=data/broker.db)"
            )

        cls.source_db_path = src
        snap_dest = get_isolation_root() / f"snapshot_{cls.__name__}_{next(_SNAP_COUNTER)}.db"
        snap_path, stats = create_sanitized_snapshot(src=src, dest_path=snap_dest)
        cls.snapshot_db_path = snap_path
        cls.sanitize_stats = stats

        rebind_test_db(snap_path)
        import database
        database.init_db()

    @classmethod
    def tearDownClass(cls):
        try:
            reset_test_db()
        finally:
            super().tearDownClass()

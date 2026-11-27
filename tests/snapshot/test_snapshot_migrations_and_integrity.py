"""Snapshot tests for SQLite schema migrations, integrity, and DB immutability."""

import hashlib
import os
import sqlite3
import tempfile
from pathlib import Path

import database
from database.schema import CURRENT_SCHEMA_VERSION
from tests.harness import SnapshotTestCase, create_sanitized_snapshot


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


class TestSnapshotMigrationsAndIntegrity(SnapshotTestCase):
    """Verify schema migration and integrity against a sanitized local DB clone."""

    def test_01_source_db_remains_byte_for_byte_untouched(self):
        src = str(self.source_db_path)
        self.assertTrue(os.path.isfile(src))
        before_sha = _sha256_file(src)
        before_mtime = os.stat(src).st_mtime_ns

        # Snapshot path must live in temp space, never in the data/ directory.
        self.assertNotEqual(os.path.abspath(src), os.path.abspath(str(self.snapshot_db_path)))

        with tempfile.TemporaryDirectory(prefix="storydeck_immute_") as tmpdir:
            extra_snap = os.path.join(tmpdir, "extra_snap.db")
            snap_out, stats = create_sanitized_snapshot(src, extra_snap)
            self.assertEqual(os.path.abspath(str(snap_out)), os.path.abspath(extra_snap))
            self.assertIsInstance(stats, dict)
            self.assertTrue(os.path.isfile(extra_snap))

        after_sha = _sha256_file(src)
        after_mtime = os.stat(src).st_mtime_ns
        self.assertEqual(before_sha, after_sha, "Source broker.db checksum changed!")
        self.assertEqual(before_mtime, after_mtime, "Source broker.db mtime changed!")

    def test_02_migrated_snapshot_reaches_current_schema_and_passes_checks(self):
        conn = database.get_db_connection()
        try:
            ver = conn.execute("PRAGMA user_version").fetchone()[0]
            self.assertEqual(ver, CURRENT_SCHEMA_VERSION)

            cols = {
                row[1]
                for row in conn.execute("PRAGMA table_info(bridge_slots)").fetchall()
            }
            self.assertIn("series_packing", cols)

            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            self.assertEqual(integrity, "ok")

            fk_violations = conn.execute("PRAGMA foreign_key_check").fetchall()
            self.assertEqual(fk_violations, [])
        finally:
            conn.close()

    def test_03_init_db_is_idempotent_and_preserves_counts(self):
        conn = database.get_db_connection()
        try:
            before_counts = {
                tbl: conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
                for tbl in ("media_items", "profiles", "bridge_slots", "bridge_tracks")
            }
        finally:
            conn.close()

        self.assertGreater(before_counts["media_items"], 0)
        self.assertGreaterEqual(before_counts["profiles"], 1)
        self.assertGreaterEqual(before_counts["bridge_slots"], 1)
        self.assertGreaterEqual(before_counts["bridge_tracks"], 1)

        # Second init_db() call must not fail or alter row counts.
        database.init_db()

        conn = database.get_db_connection()
        try:
            for tbl, count in before_counts.items():
                after = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
                self.assertEqual(after, count, f"Row count drifted for {tbl}")
            # Confirm source DB file was not migrated if it was on an older schema version.
            src_conn = sqlite3.connect(
                f"file:{Path(self.source_db_path).as_posix()}?mode=ro", uri=True
            )
            try:
                src_ver = src_conn.execute("PRAGMA user_version").fetchone()[0]
                self.assertLessEqual(src_ver, CURRENT_SCHEMA_VERSION)
            finally:
                src_conn.close()
        finally:
            conn.close()

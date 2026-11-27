"""Unit tests for offline planner/sync/pruner, quiet-spot ffmpeg snapping, scheduler, and legacy bridge migration."""
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from tests.harness import IsolatedTestCase
from app import app
import clients
from core.bridge.chapter_finder import build_proposal
from core.bridge.chapters import make_layout, plan_segments, plan_whole, window_start
from core.bridge.manager import BridgeManager, get_bridge_manager
from core.bridge.offline import get_offline_manager, normalize_offline_submode, prune_evicted_profile_playlists, refresh_offline_series_slots, resolve_series_packing
from core.bridge.quiet import QuietSpots
from core.bridge.series import get_local_item_progress
from core.bridge.streamer import AudioStreamer, AudioTrackSource
from core.bridge.transfer import TransferBook, match_tracks_to_books
import core.scheduler as scheduler
import database
from scripts.migrate_yoto_bridge import migrate_bridge_state


class TestBridgeOfflinePrunerAndMigration(IsolatedTestCase):
    reset_db_per_test = True

    def setUp(self):
        super().setUp()
        self.app = app.test_client()
        with self.app.session_transaction() as sess:
            sess["user_id"] = "test_off_admin"
            sess["username"] = "test_off_admin"
            sess["role"] = "admin"
        self.orig_settings = database.get_system_settings()
        database.upsert_profile("test_child", {
            "display_name": "Test Child",
            "default_playback_mode": "streaming",
            "offline_retention_limit": 3,
        })

    def tearDown(self):
        orig_rot = self.orig_settings.get("profile_last_rotation_dates")
        if orig_rot is not None:
            database.set_system_setting("profile_last_rotation_dates", orig_rot)
        super().tearDown()

    def test_01_lru_pruning_and_atomic_whole_series_retention(self):
        """Verifies LRU pruner removes unpinned evicted slots while keeping pinned items and active series sisters."""
        database.upsert_profile("phase2_kid", {
            "display_name": "Phase2 Kid",
            "default_playback_mode": "offline",
            "offline_retention_limit": 1,
            "yoto": {"inherit_shared": False}
        })
        database.upsert_media_items([
            {"id": "phase2_ser_1", "type": "abs", "title": "Wizards #1", "raw_metadata": {"series": "Phase2 Wizards", "series_list": [{"name": "Phase2 Wizards", "order": "1"}]}},
            {"id": "phase2_ser_2", "type": "abs", "title": "Wizards #2", "raw_metadata": {"series": "Phase2 Wizards", "series_list": [{"name": "Phase2 Wizards", "order": "2"}]}},
            {"id": "phase2_old_standalone", "type": "abs", "title": "Standalone Old Book", "raw_metadata": {}},
        ])

        with database.get_db_connection() as conn:
            conn.execute("""
                INSERT INTO bridge_slots (slot_id, abs_item_id, yoto_card_id, owner_profile_id, title, series_name, kind, mode, is_pinned, is_protected)
                VALUES
                    ('cache_phase2_kid_ser2', 'phase2_ser_2', 'yoto_ser2', 'phase2_kid', 'Wizards #2', 'Phase2 Wizards', 'profile_cache', 'offline', 0, 0),
                    ('cache_phase2_kid_old', 'phase2_old_standalone', 'yoto_old_1', 'phase2_kid', 'Standalone Old Book', '', 'profile_cache', 'offline', 0, 0),
                    ('slot_pinned', 'book_old_pinned', 'slot_pinned', 'phase2_kid', 'Pinned Book', '', 'ephemeral_offline', 'offline', 1, 1);
            """)
            conn.commit()

        database.record_playback("phase2_ser_1", "phase2_kid", "2026-11-20T12:00:00+00:00")
        database.record_playback("phase2_ser_2", "phase2_kid", "2026-11-18T12:00:00+00:00")
        database.record_playback("phase2_old_standalone", "phase2_kid", "2026-11-15T12:00:00+00:00")

        with patch.object(clients.yoto_client, "delete_playlist", return_value=True) as mock_del:
            res = prune_evicted_profile_playlists()
            evicted_slots = {p["slot_id"] for p in res["pruned_items"]}
            self.assertIn("cache_phase2_kid_old", evicted_slots)
            self.assertNotIn("cache_phase2_kid_ser2", evicted_slots)
            self.assertNotIn("slot_pinned", evicted_slots)
            mock_del.assert_any_call("yoto_old_1")

        mgr = BridgeManager()
        self.assertIsNone(mgr.get_slot("cache_phase2_kid_old"))
        self.assertIsNotNone(mgr.get_slot("cache_phase2_kid_ser2"))
        self.assertIsNotNone(mgr.get_slot("slot_pinned"))

    def test_02_profile_primary_offline_switch_archives_profile_cache(self):
        """Switching primary offline book archives the previous book as profile_cache and preserves SHAs."""
        database.upsert_profile("phase2_kid", {
            "display_name": "Phase2 Kid",
            "default_playback_mode": "offline",
            "offline_retention_limit": 2,
            "yoto": {"inherit_shared": False}
        })
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = BridgeManager()
            mgr.quiet_spots = QuietSpots(tmpdir)
            fake_item_1 = {
                "id": "phase2_book_1",
                "media": {
                    "duration": 600.0,
                    "chapters": [{"start": 0.0, "end": 300.0, "title": "Ch 1"}, {"start": 300.0, "end": 600.0, "title": "Ch 2"}],
                    "metadata": {"title": "Book One", "authorName": "Author A", "seriesName": "Phase2 Series"},
                },
            }
            fake_item_2 = {
                "id": "phase2_book_2",
                "media": {
                    "duration": 400.0,
                    "chapters": [{"start": 0.0, "end": 400.0, "title": "Part 1"}],
                    "metadata": {"title": "Book Two", "authorName": "Author B"},
                },
            }

            with patch.object(clients.abs_client, "get_item", side_effect=lambda bid, **kw: fake_item_1 if bid == "phase2_book_1" else fake_item_2):
                slot1 = mgr.update_profile_primary_card("phase2_kid", "phase2_book_1", mode_override="offline")
                self.assertEqual(slot1["kind"], "profile_primary")

                with database.get_db_connection() as conn:
                    conn.execute("UPDATE bridge_slots SET yoto_card_id = 'yoto_card_b1', offline_status = 'ready' WHERE slot_id = 'profile_phase2_kid';")
                    conn.execute("UPDATE bridge_tracks SET yoto_sha256 = 'sha_track_1' WHERE slot_id = 'profile_phase2_kid' AND track_idx = 1;")
                    conn.commit()

                slot2 = mgr.update_profile_primary_card("phase2_kid", "phase2_book_2", mode_override="offline")
                self.assertEqual(slot2["abs_item_id"], "phase2_book_2")
                self.assertNotEqual(slot2.get("yoto_card_id"), "yoto_card_b1")

                archived = mgr.get_slot("cache_phase2_kid_phase2_book_1")
                self.assertEqual(archived["kind"], "profile_cache")
                self.assertEqual(archived["yoto_card_id"], "yoto_card_b1")
                self.assertEqual(archived["tracks"][0]["yoto_sha256"], "sha_track_1")

                slot_back = mgr.update_profile_primary_card("phase2_kid", "phase2_book_1", mode_override="offline")
                self.assertEqual(slot_back["tracks"][0]["yoto_sha256"], "sha_track_1")

    def test_03_ffmpeg_silencedetect_and_quiet_spots_snapping(self):
        """Real ffmpeg silencedetect populates QuietSpots and snaps non-chapter track cuts."""
        ffmpeg_bin = shutil.which("ffmpeg")
        self.assertIsNotNone(ffmpeg_bin, "ffmpeg binary must be available for silence detection tests")

        with tempfile.TemporaryDirectory() as tmpdir:
            wav_path = str(Path(tmpdir) / "tone_pause_tone.wav")
            subprocess.run([
                ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=2.0",
                "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono:d=1.0",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=2.0",
                "-filter_complex", "[0:a][1:a][2:a]concat=n=3:v=0:a=1[out]",
                "-map", "[out]", wav_path,
            ], check=True)

            src = AudioTrackSource(url=wav_path, start=0.0, duration=5.0)
            streamer = AudioStreamer(ffmpeg_bin=ffmpeg_bin)
            spots = streamer.detect_quiet_spots([src], 0.0, 5.0)
            self.assertTrue(any(s <= 2.2 and e >= 2.8 for s, e in spots))

            qs = QuietSpots(tmpdir)
            self.assertEqual(qs.scan_windows("phase2_audio_book", [2.2], [src], streamer=streamer), 1)
            snap = QuietSpots(tmpdir).snapper("phase2_audio_book")
            snapped_cut = snap(2.2)
            self.assertGreaterEqual(snapped_cut, 2.3)
            self.assertLessEqual(snapped_cut, 2.7)

            mgr = BridgeManager()
            mgr.quiet_spots = QuietSpots(tmpdir)
            long_book = {
                "id": "phase2_snap_book",
                "media": {
                    "duration": 5400.0,
                    "chapters": [],
                    "audioFiles": [{"ino": "1", "duration": 5400.0, "local_path": wav_path}],
                    "metadata": {"title": "Long Unchaptered Book", "authorName": "Author"},
                },
            }

            def _fake_detect(sources, start_sec, end_sec, **kwargs):
                mid = (start_sec + end_sec) / 2.0
                if abs(mid - 1800.0) < 25.0:
                    return [(1804.0, 1806.0)]
                if abs(mid - 3600.0) < 25.0:
                    return [(3593.0, 3595.0)]
                return []

            with patch.object(clients.abs_client, "get_item", return_value=long_book), \
                 patch.object(AudioStreamer, "detect_quiet_spots", side_effect=_fake_detect):
                slot = mgr.assign_abs_book_to_slot(slot_id="phase2_snap_slot", abs_item_id="phase2_snap_book", scan_quiet_sync=True)
                self.assertEqual(slot["tracks"][0]["end_time"], 1805.0)
                self.assertEqual(slot["tracks"][1]["start_time"], 1805.0)
                self.assertEqual(slot["tracks"][1]["end_time"], 3594.0)

    def test_04_yoto_last_played_at_abs_progress_sync_and_daily_rotation(self):
        """perform_library_refresh() ingests Yoto/ABS playback history and _check_daily_profile_rotations honors hour."""
        database.upsert_profile("phase2_kid", {
            "display_name": "Phase2 Kid",
            "abs_username": "phase2_abs_user",
            "catalog_rotation_hour": 4,
            "yoto": {"inherit_shared": False}
        })
        mock_yoto_raw = [{"cardId": "phase2_yoto_card", "id": "phase2_yoto_card", "title": "Phase2 Story Card", "author": "Yoto", "lastPlayedAt": "2026-11-19T12:00:00Z"}]
        expected_abs_iso = datetime.fromtimestamp(1795000000.0, timezone.utc).isoformat()

        with patch.object(clients.yoto_client, "get_groups", return_value=[]), \
             patch.object(clients.yoto_client, "get_raw_library", return_value=mock_yoto_raw), \
             patch.object(clients.abs_client, "get_libraries", return_value=[]), \
             patch.object(clients.abs_client, "get_all_users", return_value=[{"id": "u_p2", "username": "phase2_abs_user"}]), \
             patch.object(clients.abs_client, "get_user_media_progress", return_value=[{"libraryItemId": "phase2_abs_book", "lastUpdate": 1795000000000}]), \
             patch("database.prune_stale_media_items", return_value=0), \
             patch("core.image_resizer.prune_orphan_thumbnails", return_value=0), \
             patch("core.bridge.offline.prune_evicted_profile_playlists", return_value={"pruned_count": 0, "pruned_items": []}) as mock_prune:
            self.assertEqual(scheduler.perform_library_refresh(), 1)
            mock_prune.assert_called_once()

        hist = database.get_playback_history("phase2_kid")
        self.assertEqual(hist.get("phase2_yoto_card"), "2026-11-19T12:00:00+00:00")
        self.assertEqual(hist.get("phase2_abs_book"), expected_abs_iso)

        last_dates = dict(database.get_system_settings().get("profile_last_rotation_dates") or {})
        last_dates.pop("phase2_kid", None)
        database.set_system_setting("profile_last_rotation_dates", last_dates)
        with patch.object(scheduler, "perform_library_refresh", return_value=0):
            self.assertNotIn("phase2_kid", scheduler._check_daily_profile_rotations(datetime(2026, 11, 20, 3, 59)))
            self.assertIn("phase2_kid", scheduler._check_daily_profile_rotations(datetime(2026, 11, 20, 4, 0)))
            self.assertNotIn("phase2_kid", scheduler._check_daily_profile_rotations(datetime(2026, 11, 20, 5, 0)))

    def test_05_5h_rolling_window_and_whole_book_packing(self):
        """Verifies 5-hour rolling window ('next') and 99-track binary-search whole-book packing ('book')."""
        self.assertEqual(normalize_offline_submode("offline_next"), "next")
        self.assertEqual(normalize_offline_submode("5h"), "next")
        self.assertEqual(normalize_offline_submode("offline_series"), "series")
        self.assertEqual(normalize_offline_submode("offline_book"), "book")

        chapters = [{"start": i * 3600.0, "end": (i + 1) * 3600.0, "title": f"Chapter {i + 1}"} for i in range(12)]
        segs = plan_segments(5400.0, 5400.0 + 5 * 3600.0, chapters, max_len=3300.0)
        self.assertGreaterEqual(len(segs), 5)
        self.assertAlmostEqual(segs[0][0], 5400.0, places=1)
        self.assertAlmostEqual(segs[-1][1], 5400.0 + 5 * 3600.0, places=1)

        many_chapters = [{"start": i * 600.0, "end": (i + 1) * 600.0, "title": f"Ch {i + 1}"} for i in range(150)]
        packed = plan_whole([(many_chapters, 150 * 600.0)], max_len=7000.0, limit=99)
        self.assertLessEqual(len(packed[0]), 99)

        self.assertEqual(window_start(0, 150), 0)
        self.assertEqual(window_start(95, 150), 51)
        self.assertEqual(len(make_layout([("item_long", "Long Book", 150 * 600.0, many_chapters, 95 * 600.0 + 10.0)])), 99)

        mgr = get_bridge_manager()
        fake_abs_item = {"id": "item_12h", "media": {"duration": 12 * 3600.0, "metadata": {"title": "Twelve Hour Epic"}, "chapters": chapters}}
        with patch.object(clients.abs_client, "get_item", return_value=fake_abs_item), \
             patch.object(clients.abs_client, "get_media_progress", return_value={"currentTime": 3 * 3600.0, "isFinished": False}):
            slot = mgr.assign_abs_book_to_slot(slot_id="slot_5h", abs_item_id="item_12h", title="Twelve Hour Epic", mode="offline")
            planned, meta = get_offline_manager()._plan_offline_tracks(slot, submode="next", hours=5.0, speed=1.0, abs_token=None, base_url="http://localhost:5000")
            self.assertEqual(meta["mode"], "next")
            self.assertAlmostEqual(meta["start"], 3600.0, places=1)
            self.assertAlmostEqual(meta["resume"]["pos"], 3 * 3600.0 - 15.0, places=1)
            self.assertAlmostEqual(sum(t["duration"] for t in planned), 5.0 * 3600.0, delta=60.0)

        short_chapters = [{"start": i * 1800.0, "end": (i + 1) * 1800.0, "title": f"Short Ch {i + 1}"} for i in range(6)]
        fake_short_item = {"id": "item_3h", "media": {"duration": 3 * 3600.0, "metadata": {"title": "Three Hour Story"}, "chapters": short_chapters}}
        with patch.object(clients.abs_client, "get_item", return_value=fake_short_item), \
             patch.object(clients.abs_client, "get_media_progress", return_value={"currentTime": 2.5 * 3600.0, "isFinished": False}):
            slot_short = mgr.assign_abs_book_to_slot(slot_id="slot_3h", abs_item_id="item_3h", title="Three Hour Story", mode="offline")
            planned_short, meta_short = get_offline_manager()._plan_offline_tracks(slot_short, submode="next", hours=5.0, speed=1.0, abs_token=None, base_url="http://localhost:5000")
            self.assertAlmostEqual(meta_short["start"], 0.0, places=1)
            self.assertAlmostEqual(sum(t["duration"] for t in planned_short), 3.0 * 3600.0, delta=10.0)

    def test_06_offline_mqtt_sync_and_chapter_finder_viterbi(self):
        """Verifies offline MQTT position sync to own_place plus Audible Chapter Finder & Viterbi transfer."""
        with database.get_db_connection() as conn:
            conn.execute("""
                INSERT INTO bridge_slots (slot_id, abs_item_id, yoto_card_id, title, total_duration, mode, own_place, offline_meta)
                VALUES ('slot_off_sync', 'book_off', 'XyZ12', 'Offline Book', 2000.0, 'offline', 1, ?);
            """, (json.dumps({"status": "ready", "mode": "next", "speed": 1.0, "books": ["book_off"], "cur_item": "book_off"}),))
            conn.execute("""
                INSERT INTO bridge_tracks (slot_id, track_idx, abs_item_id, book_title, book_duration, title, start_time, end_time, duration, yoto_sha256, stream_url)
                VALUES ('slot_off_sync', 1, 'book_off', 'Offline Book', 2000.0, 'Chapter 1', 500.0, 1500.0, 1000.0, 'sha256_track1', '/s/audio/slot_off_sync/1');
            """)
            conn.commit()

        get_offline_manager().on_mqtt_event("dev_1", {"cardId": "XyZ12", "chapterKey": "01", "trackKey": "01", "position": 120, "playbackStatus": "paused"})
        local_prog = get_local_item_progress("slot_off_sync", "book_off")
        self.assertIsNotNone(local_prog)
        self.assertAlmostEqual(float(local_prog.get("currentTime") or 0.0), 620.0, places=1)

        audible_data = {
            "runtimeLengthMs": 3000000,
            "isAccurate": True,
            "chapters": [
                {"startOffsetMs": 0, "lengthMs": 600000, "title": "Opening Credits"},
                {"startOffsetMs": 600000, "lengthMs": 1200000, "title": "Chapter 1: The Boy"},
                {"startOffsetMs": 1800000, "lengthMs": 1200000, "title": "Chapter 2: The Glass"},
            ],
        }
        prop = build_proposal(audible_data, duration=3005.0)
        self.assertEqual(prop["quality"], "good")
        self.assertEqual(prop["chapters"][-1]["end"], 3005.0)

        books = [
            TransferBook.from_abs_item({"id": "b1", "media": {"duration": 1800.0, "metadata": {"title": "Book One", "seriesName": "Saga"}, "chapters": [{"start": 0.0, "end": 600.0, "title": "Ch 1"}, {"start": 600.0, "end": 1200.0, "title": "Ch 2"}, {"start": 1200.0, "end": 1800.0, "title": "Ch 3"}]}}),
            TransferBook.from_abs_item({"id": "b2", "media": {"duration": 1000.0, "metadata": {"title": "Book Two", "seriesName": "Saga"}, "chapters": [{"start": 0.0, "end": 500.0, "title": "Ch 1"}, {"start": 500.0, "end": 1000.0, "title": "Ch 2"}]}}),
        ]
        card_tracks = [
            {"title": "Book One - Track 1", "duration": 600.0},
            {"title": "Book One - Track 2", "duration": 600.0},
            {"title": "Book One - Track 3", "duration": 600.0},
            {"title": "Book Two - Track 4", "duration": 500.0},
            {"title": "Book Two - Track 5", "duration": 500.0},
        ]
        matches = match_tracks_to_books(card_tracks, books)
        self.assertEqual([m.book for m in matches], [0, 0, 0, 1, 1])

    def test_07_offline_series_chapters_rolling_condense_and_start_points(self):
        """Verifies chapters/condense series packing, rolling finished books, and series_start options."""
        series_items, abs_items_by_id = [], {}
        for idx in range(1, 5):
            bid = f"ser_roll_b{idx}"
            title = f"Test Saga Book {idx}"
            chapters = [{"start": i * 600.0, "end": (i + 1) * 600.0, "title": f"Chapter {i + 1}"} for i in range(30)]
            series_items.append({"id": bid, "type": "abs", "title": title, "author": "Author", "raw_metadata": {"series": "Test Saga", "series_order": str(idx), "series_list": [{"name": "Test Saga", "order": str(idx)}], "duration": 18000.0}})
            abs_items_by_id[bid] = {"id": bid, "media": {"duration": 18000.0, "metadata": {"title": title, "seriesName": f"Test Saga #{idx}"}, "chapters": chapters}}

        huge_chapters = [{"start": i * 600.0, "end": (i + 1) * 600.0, "title": f"Huge Ch {i + 1}"} for i in range(120)]
        abs_items_by_id["ser_cond_huge"] = {"id": "ser_cond_huge", "media": {"duration": 72000.0, "metadata": {"title": "Huge Book"}, "chapters": huge_chapters}}

        database.upsert_media_items(series_items)
        database.set_system_setting("offline_series_packing", "chapters")
        mgr = get_bridge_manager()

        with patch.object(clients.abs_client, "get_item", side_effect=lambda item_id, token=None: abs_items_by_id.get(item_id)), \
             patch.object(clients.abs_client, "get_media_progress", return_value={}):
            slot = mgr.assign_abs_book_to_slot(slot_id="slot_series_roll", abs_item_id="ser_roll_b1", title="Test Saga Book 1", series_name="Test Saga", group_kind="series", mode="offline")
            with database.get_db_connection() as conn:
                conn.execute("UPDATE bridge_slots SET offline_mode = 'series' WHERE slot_id = 'slot_series_roll';")
                conn.commit()
            slot = mgr.get_slot("slot_series_roll")

            planned, meta = get_offline_manager()._plan_offline_tracks(slot, submode="series", hours=5.0, speed=1.0, abs_token=None, base_url="http://localhost:5055")
            self.assertEqual(meta["books"], ["ser_roll_b1", "ser_roll_b2", "ser_roll_b3"])
            self.assertEqual(meta["books_left_off"], 1)
            self.assertEqual(len(planned), 90)

            with database.get_db_connection() as conn:
                conn.execute("UPDATE bridge_tracks SET yoto_sha256 = 'sha_' || abs_item_id || '_' || track_idx WHERE slot_id = 'slot_series_roll' AND abs_item_id IN ('ser_roll_b2', 'ser_roll_b3');")
                conn.execute("UPDATE bridge_slots SET offline_status = 'ready' WHERE slot_id = 'slot_series_roll';")
                conn.commit()

        def _prog_after_b1(item_id, token=None, **kwargs):
            return {"currentTime": 18000.0, "isFinished": True, "lastUpdate": 2000} if item_id == "ser_roll_b1" else {}

        with patch.object(clients.abs_client, "get_item", side_effect=lambda item_id, token=None: abs_items_by_id.get(item_id)), \
             patch.object(clients.abs_client, "get_media_progress", side_effect=_prog_after_b1), \
             patch.object(get_offline_manager(), "queue_transcode") as mock_queue:
            self.assertEqual(refresh_offline_series_slots(), 1)
            mock_queue.assert_called_once()
            updated_slot = mgr.get_slot("slot_series_roll")
            self.assertEqual(updated_slot["abs_item_id"], "ser_roll_b2")
            planned2, meta2 = get_offline_manager()._plan_offline_tracks(updated_slot, submode="series", hours=5.0, speed=1.0, abs_token=None, base_url="http://localhost:5055")
            self.assertEqual(meta2["books"], ["ser_roll_b2", "ser_roll_b3", "ser_roll_b4"])
            self.assertTrue(all(t["yoto_sha256"] for t in planned2 if t["abs_item_id"] == "ser_roll_b2"))

        # Condense & single huge book fallback
        with patch.object(clients.abs_client, "get_item", side_effect=lambda item_id, token=None: abs_items_by_id.get(item_id)), \
             patch.object(clients.abs_client, "get_media_progress", return_value={}):
            with database.get_db_connection() as conn:
                conn.execute("UPDATE bridge_slots SET series_packing = 'condense', abs_item_id = 'ser_roll_b1' WHERE slot_id = 'slot_series_roll';")
                conn.commit()
            planned_cond, meta_cond = get_offline_manager()._plan_offline_tracks(mgr.get_slot("slot_series_roll"), submode="series", hours=5.0, speed=1.0, abs_token=None, base_url="http://localhost:5055")
            self.assertEqual(meta_cond["series_packing"], "condense")
            self.assertLessEqual(len(planned_cond), 100)

            slot_huge = mgr.assign_abs_book_to_slot(slot_id="slot_huge_ch", abs_item_id="ser_cond_huge", title="Huge Book", series_name="Unlisted", group_kind="series", mode="offline")
            with database.get_db_connection() as conn:
                conn.execute("UPDATE bridge_slots SET offline_mode = 'series', series_packing = 'chapters' WHERE slot_id = 'slot_huge_ch';")
                conn.commit()
            planned_huge, meta_huge = get_offline_manager()._plan_offline_tracks(mgr.get_slot("slot_huge_ch"), submode="series", hours=5.0, speed=1.0, abs_token=None, base_url="http://localhost:5055")
            self.assertLessEqual(len(planned_huge), 100)

        # Global vs per-slot packing precedence & series_start options
        self.app.put("/api/admin/settings/catalog", json={"offline_series_packing": "condense"})
        self.assertEqual(resolve_series_packing(None), "condense")
        with patch.object(clients.abs_client, "get_item", side_effect=lambda bid, **kw: abs_items_by_id.get(bid)), \
             patch.object(clients.abs_client, "get_media_progress", side_effect=_prog_after_b1), \
             patch.object(get_offline_manager(), "queue_transcode", return_value={"status": "queued"}):
            r_sel = self.app.post("/api/cards/assign", json={"slot_id": "fleet_ss", "abs_item_id": "ser_roll_b3", "mode": "streaming", "group_kind": "series", "series_name": "Test Saga", "series_start": "selected", "title": "Book 3"})
            self.assertEqual(r_sel.status_code, 200)
            self.assertEqual(r_sel.get_json()["abs_item_id"], "ser_roll_b3")

            r_beg = self.app.post("/api/cards/assign", json={"slot_id": "fleet_ss", "abs_item_id": "ser_roll_b3", "mode": "offline_series", "group_kind": "series", "series_name": "Test Saga", "series_start": "beginning", "title": "Book 3"})
            self.assertEqual(r_beg.status_code, 200)
            self.assertEqual(r_beg.get_json()["abs_item_id"], "ser_roll_b1")

            r_act = self.app.post("/api/cards/assign", json={"slot_id": "fleet_ss", "abs_item_id": "ser_roll_b3", "mode": "offline_series", "group_kind": "series", "series_name": "Test Saga", "series_start": "active", "title": "Book 3"})
            self.assertEqual(r_act.status_code, 200)
            self.assertEqual(r_act.get_json()["abs_item_id"], "ser_roll_b2")

    def test_08_offline_failure_retry_and_transfer_controls(self):
        """Verifies failed error persistence, retry, pause/stop/start/cancel transfer controls, and job superseding."""
        fake_item = {"id": "item_ctrl", "media": {"duration": 1200.0, "metadata": {"title": "Control Test Book"}, "chapters": [{"start": 0.0, "end": 600.0, "title": "Ch 1"}, {"start": 600.0, "end": 1200.0, "title": "Ch 2"}]}}
        mgr = get_bridge_manager()
        off_mgr = get_offline_manager()
        with patch.object(clients.abs_client, "get_item", return_value=fake_item), \
             patch.object(clients.abs_client, "get_media_progress", return_value={}):
            mgr.assign_abs_book_to_slot(slot_id="slot_ctrl", abs_item_id="item_ctrl", title="Control Test Book", mode="offline")
            with patch("core.bridge.offline.shutil.which", return_value=None):
                off_mgr._jobs["slot_ctrl"] = {"slot_id": "slot_ctrl", "status": "queued"}
                off_mgr._run_job("slot_ctrl", base_url="http://localhost:5055", offline_mode="book", hours=5.0)

        off_mgr._jobs.pop("slot_ctrl", None)
        card_entry = next(c for c in self.app.get("/api/cards").get_json() if c["slot_id"] == "slot_ctrl")
        self.assertEqual(card_entry["offline_status"], "failed")
        self.assertIn("ffmpeg", card_entry["transcode"]["error"].lower())

        with patch.object(off_mgr, "queue_transcode", return_value={"status": "queued"}) as mock_retry:
            self.assertEqual(self.app.post("/api/cards/slot_ctrl/retry").status_code, 200)
            mock_retry.assert_called_once()

        self.assertEqual(self.app.post("/api/cards/slot_ctrl/transfer", json={"action": "pause"}).get_json()["transcode"]["status"], "paused")
        off_mgr._jobs.pop("slot_ctrl", None)
        self.assertEqual(off_mgr.resume_interrupted_jobs(), 0)

        self.assertEqual(self.app.post("/api/cards/slot_ctrl/transfer", json={"action": "stop"}).get_json()["transcode"]["status"], "stopped")
        off_mgr._jobs.pop("slot_ctrl", None)
        self.assertEqual(off_mgr.resume_interrupted_jobs(), 0)

        with patch.object(off_mgr, "queue_transcode", return_value={"status": "queued"}) as mock_q:
            self.assertEqual(self.app.post("/api/cards/slot_ctrl/transfer", json={"action": "start"}).status_code, 200)
            mock_q.assert_called_once()

        self.assertEqual(self.app.post("/api/cards/slot_ctrl/transfer", json={"action": "cancel"}).get_json()["transcode"]["status"], "cancelled")
        self.assertEqual(self.app.get("/api/cards/slot_ctrl").get_json()["offline_status"], "none")

        with patch("threading.Thread"):
            off_mgr.queue_transcode("slot_ctrl", offline_mode="book")
            old_cancel = off_mgr._cancel_flags["slot_ctrl"]
            old_gen = off_mgr._job_gen["slot_ctrl"]
            j2 = off_mgr.queue_transcode("slot_ctrl", offline_mode="series", replace_existing=True)
            self.assertTrue(old_cancel.is_set())
            self.assertEqual(off_mgr._job_gen["slot_ctrl"], old_gen + 1)
            self.assertEqual(j2["offline_mode"], "series")

            self.assertEqual(self.app.post("/api/cards/slot_ctrl/mode", json={"mode": "streaming"}).status_code, 200)
            self.assertEqual(mgr.get_slot("slot_ctrl")["offline_status"], "none")

        self.assertEqual(self.app.post("/api/cards/slot_ctrl/transfer", json={"action": "invalid"}).status_code, 400)

    def test_09_migrate_legacy_bridge_state(self):
        """Verifies migration of legacy Yoto Bridge state.json into SQLite."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "state.json"
            state_file.write_text(json.dumps({
                "slots": {
                    "dino_card": {
                        "slot": "dino_card",
                        "kind": "card",
                        "item_id": "abs_dino_123",
                        "title": "Dinosaur Roar",
                        "yoto_card_id": "yoto_uuid_999",
                        "speed": 1.25,
                        "own_place": True,
                        "layout_id": "lay_1",
                        "layouts": {"lay_1": [{"start": 0.0, "end": 120.0, "title": "Chapter 1: The T-Rex"}, {"start": 120.0, "end": 300.0, "title": "Chapter 2: The Volcano"}]},
                    }
                }
            }), encoding="utf-8")

            self.assertEqual(migrate_bridge_state(state_file), 1)
            slot = get_bridge_manager().get_slot("dino_card")
            self.assertEqual(slot["abs_item_id"], "abs_dino_123")
            self.assertEqual(slot["yoto_card_id"], "yoto_uuid_999")
            self.assertEqual(slot["speed"], 1.25)
            self.assertEqual(len(slot["tracks"]), 2)


if __name__ == "__main__":
    unittest.main()

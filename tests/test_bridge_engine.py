"""
Unit tests for StoryDeck Core Bridge Engine & Playlist Management.
Tests slot and track CRUD, profile card assignments, delivery mode overrides,
Yoto Cloud playlist classification, manual deletions, LRU pruning, and catalog deduplication.
"""
import tests.test_isolation  # noqa: F401 - must precede app/database/clients imports
import unittest
from unittest.mock import MagicMock, patch
import json
import sqlite3
from app import app
import database
import clients
from core.bridge.manager import BridgeManager, get_bridge_manager
from core.bridge.offline import prune_evicted_profile_playlists


class TestBridgeEngine(unittest.TestCase):
    def setUp(self):
        self.app = app.test_client()
        with self.app.session_transaction() as sess:
            sess["user_id"] = "test_bridge_admin"
            sess["username"] = "test_bridge_admin"
            sess["role"] = "admin"
        self.app_context = app.app_context()
        self.app_context.push()

        # Seed test profile with default_playback_mode = 'streaming'
        conn = database.get_db_connection()
        try:
            with conn:
                cur = conn.cursor()
                cur.execute("DELETE FROM bridge_tracks;")
                cur.execute("DELETE FROM bridge_slots;")
                cur.execute("DELETE FROM profiles WHERE id = 'test_child';")
                cur.execute("""
                    INSERT INTO profiles (id, display_name, default_playback_mode, offline_retention_limit)
                    VALUES ('test_child', 'Test Child', 'streaming', 3);
                """)
        finally:
            conn.close()

    def tearDown(self):
        conn = database.get_db_connection()
        try:
            with conn:
                cur = conn.cursor()
                cur.execute("DELETE FROM bridge_tracks;")
                cur.execute("DELETE FROM bridge_slots;")
                cur.execute("DELETE FROM profiles WHERE id = 'test_child';")
        finally:
            conn.close()
        self.app_context.pop()

    def test_bridge_manager_slot_crud(self):
        """Verifies slot creation, speed clamping, own_place, and pin toggling."""
        mgr = get_bridge_manager()

        # 1. Assign book to fleet slot
        slot = mgr.assign_abs_book_to_slot(
            slot_id="fleet_dino",
            abs_item_id="item_123",
            title="Dinosaur Adventures",
            author="Author Dan",
            kind="permanent_myo",
            mode="streaming"
        )
        self.assertIsNotNone(slot)
        self.assertEqual(slot["slot_id"], "fleet_dino")
        self.assertEqual(slot["title"], "Dinosaur Adventures")

        # 2. Speed clamping test
        mgr.set_slot_speed("fleet_dino", 3.5)  # should clamp to 2.0
        slot = mgr.get_slot("fleet_dino")
        self.assertEqual(slot["speed"], 2.0)

        mgr.set_slot_speed("fleet_dino", 0.1)  # should clamp to 0.5
        slot = mgr.get_slot("fleet_dino")
        self.assertEqual(slot["speed"], 0.5)

        # 3. Own place and Pin toggle
        mgr.set_slot_own_place("fleet_dino", True)
        mgr.set_slot_pin("fleet_dino", True)
        slot = mgr.get_slot("fleet_dino")
        self.assertEqual(slot["own_place"], 1)
        self.assertEqual(slot["is_pinned"], 1)
        self.assertEqual(slot["is_protected"], 1)

    def test_profile_primary_card_assignment_and_mode_override(self):
        """Verifies profile primary card inherits profile default or applies override."""
        mgr = get_bridge_manager()

        # Case A: Mode omitted -> inherits profile default ("streaming")
        slot_stream = mgr.update_profile_primary_card(
            profile_id="test_child",
            abs_item_id="abs_book_1",
            mode_override="default"
        )
        self.assertEqual(slot_stream["slot_id"], "profile_test_child")
        self.assertEqual(slot_stream["mode"], "streaming")
        self.assertEqual(slot_stream["kind"], "profile_primary")

        # Case B: Mode overridden to "offline"
        slot_offline = mgr.update_profile_primary_card(
            profile_id="test_child",
            abs_item_id="abs_book_2",
            mode_override="offline"
        )
        self.assertEqual(slot_offline["mode"], "offline")

    def test_api_cards_endpoints(self):
        """Tests /api/cards REST operations."""
        mgr = get_bridge_manager()
        mgr.assign_abs_book_to_slot(
            slot_id="slot_mobile",
            abs_item_id="item_abc",
            title="Mobile Book",
            author="Jane Doe"
        )

        # GET /api/cards
        res = self.app.get("/api/cards")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(any(s["slot_id"] == "slot_mobile" for s in data))

        # PUT /api/cards/<slot_id>
        res = self.app.put("/api/cards/slot_mobile", json={"speed": 1.25, "is_pinned": True})
        self.assertEqual(res.status_code, 200)
        updated = res.get_json()
        self.assertEqual(updated["speed"], 1.25)
        self.assertEqual(updated["is_pinned"], 1)

        # DELETE /api/cards/<slot_id>
        res = self.app.delete("/api/cards/slot_mobile")
        self.assertEqual(res.status_code, 200)
        self.assertIsNone(mgr.get_slot("slot_mobile"))

    def test_yoto_playlists_classification(self):
        """Tests /api/yoto/playlists enriches cloud playlists with broker vs custom source and pin state."""
        mgr = get_bridge_manager()
        mgr.assign_abs_book_to_slot(
            slot_id="broker_slot_1",
            abs_item_id="item_xyz",
            title="StoryDeck Audiobook"
        )
        mgr.set_slot_pin("broker_slot_1", True)

        mock_playlists = [
            {
                "cardId": "broker_slot_1",
                "title": "StoryDeck Audiobook",
                "metadata": {"description": "Streams from Audiobookshelf via StoryDeck Core"}
            },
            {
                "cardId": "custom_yoto_card",
                "title": "Bedtime Lullabies",
                "shareType": "myo",
                "metadata": {"description": "Recorded songs from grandma"}
            }
        ]

        with patch.object(clients.yoto_client, "get_all_cloud_playlists", return_value=mock_playlists):
            res = self.app.get("/api/yoto/playlists")
            self.assertEqual(res.status_code, 200)
            items = res.get_json()
            self.assertEqual(len(items), 2)

            broker_item = next(x for x in items if x["card_id"] == "broker_slot_1")
            custom_item = next(x for x in items if x["card_id"] == "custom_yoto_card")

            self.assertEqual(broker_item["source"], "broker")
            self.assertTrue(broker_item["is_pinned"])

            self.assertEqual(custom_item["source"], "custom")
            self.assertTrue(custom_item["is_physical"])
            self.assertFalse(custom_item["is_pinned"])

    def test_lru_pruning_protects_pinned_and_active_recent_items(self):
        """Verifies LRU cache pruning only removes unpinned evicted items."""
        conn = database.get_db_connection()
        try:
            with conn:
                cur = conn.cursor()
                # Slot 1: Active recent book (should survive)
                cur.execute("""
                    INSERT INTO bridge_slots (slot_id, abs_item_id, owner_profile_id, title, kind, mode, is_pinned, is_protected)
                    VALUES ('slot_active', 'book_active', 'test_child', 'Active Book', 'ephemeral_offline', 'offline', 0, 0);
                """)
                # Slot 2: Evicted book but PINNED (should survive)
                cur.execute("""
                    INSERT INTO bridge_slots (slot_id, abs_item_id, owner_profile_id, title, kind, mode, is_pinned, is_protected)
                    VALUES ('slot_pinned', 'book_old_pinned', 'test_child', 'Pinned Old Book', 'ephemeral_offline', 'offline', 1, 1);
                """)
                # Slot 3: Evicted book UNPINNED (should be pruned)
                cur.execute("""
                    INSERT INTO bridge_slots (slot_id, abs_item_id, owner_profile_id, title, kind, mode, is_pinned, is_protected)
                    VALUES ('slot_evicted', 'book_old_evicted', 'test_child', 'Evicted Old Book', 'ephemeral_offline', 'offline', 0, 0);
                """)
        finally:
            conn.close()

        # Set playback history so only 'book_active' is in recent
        database.record_playback("book_active", "test_child")

        with patch.object(clients.yoto_client, "delete_playlist", return_value=True) as mock_del:
            result = prune_evicted_profile_playlists()
            self.assertEqual(result["pruned_count"], 1)
            self.assertEqual(result["pruned_items"][0]["slot_id"], "slot_evicted")
            mock_del.assert_called_once_with("slot_evicted")

        # Verify database state
        mgr = get_bridge_manager()
        self.assertIsNotNone(mgr.get_slot("slot_active"))
        self.assertIsNotNone(mgr.get_slot("slot_pinned"))
        self.assertIsNone(mgr.get_slot("slot_evicted"))

    def test_catalog_deduplication(self):
        """Verifies get_raw_library filters out broker-created playlists so they don't duplicate ABS items."""
        mgr = get_bridge_manager()
        mgr.assign_abs_book_to_slot(
            slot_id="profile_test_child",
            abs_item_id="book_123",
            title="Profile Active Book"
        )

        with database.get_db_connection() as conn:
            conn.execute("UPDATE bridge_slots SET yoto_card_id = 'e5btM' WHERE slot_id = 'profile_test_child';")
            conn.commit()

        mock_library = [
            {"cardId": "profile_test_child", "title": "Profile Active Book", "metadata": {"generator": "storydeck-core"}},
            {"cardId": "e5btM", "title": "Profile Active Book", "isMyo": True, "metadata": {}},
            {"cardId": "retail_card_999", "title": "Disney Frozen", "metadata": {"author": "Disney"}}
        ]

        with patch.object(clients.yoto_client, "load_local_family_library", return_value=[]), \
             patch.object(clients.yoto_client, "get_token", return_value="fake_token"), \
             patch("requests.get") as mock_get:

            mock_res = MagicMock()
            mock_res.status_code = 200
            mock_res.json.return_value = {"cards": mock_library}
            mock_get.return_value = mock_res

            raw_catalog = clients.yoto_client.get_raw_library()
            card_ids = [c.get("cardId") for c in raw_catalog]

            self.assertIn("retail_card_999", card_ids)
            self.assertNotIn("profile_test_child", card_ids)
            self.assertNotIn("e5btM", card_ids)

    def test_5h_rolling_window_and_whole_book_packing(self):
        """Verifies 5-hour rolling window ('next') and 99-track binary-search whole-book packing ('book')."""
        from core.bridge.chapters import plan_segments, plan_whole, make_layout, window_start
        from core.bridge.offline import get_offline_manager, normalize_offline_submode

        # 1. Submode normalization
        self.assertEqual(normalize_offline_submode("offline_next"), "next")
        self.assertEqual(normalize_offline_submode("5h"), "next")
        self.assertEqual(normalize_offline_submode("offline_series"), "series")
        self.assertEqual(normalize_offline_submode("offline_book"), "book")

        # 2. 5-hour rolling window plan_segments across 12 x 1-hour chapters starting at 1.5h -> 5 hours total
        chapters = [
            {"start": i * 3600.0, "end": (i + 1) * 3600.0, "title": f"Chapter {i + 1}"}
            for i in range(12)
        ]
        segs = plan_segments(5400.0, 5400.0 + 5 * 3600.0, chapters, max_len=3300.0)
        self.assertTrue(len(segs) >= 5)
        self.assertAlmostEqual(segs[0][0], 5400.0, places=1)
        self.assertAlmostEqual(segs[-1][1], 5400.0 + 5 * 3600.0, places=1)

        # 3. Whole-book packing: 150 chapters of 10m each (25 hours total) packed into <= 99 tracks
        many_chapters = [
            {"start": i * 600.0, "end": (i + 1) * 600.0, "title": f"Ch {i + 1}"}
            for i in range(150)
        ]
        packed = plan_whole([(many_chapters, 150 * 600.0)], max_len=7000.0, limit=99)
        self.assertEqual(len(packed), 1)
        self.assertLessEqual(len(packed[0]), 99)
        self.assertAlmostEqual(packed[0][0][0], 0.0)
        self.assertAlmostEqual(packed[0][-1][1], 150 * 600.0)

        # 4. 99-track sliding window (`window_start(index, total)` with step 90)
        self.assertEqual(window_start(0, 150), 0)
        self.assertEqual(window_start(89, 150), 0)
        self.assertEqual(window_start(95, 150), 51)
        layout = make_layout([("item_long", "Long Book", 150 * 600.0, many_chapters, 95 * 600.0 + 10.0)])
        self.assertEqual(len(layout), 99)

        # 5. OfflineTranscodeManager._plan_offline_tracks in 'next' (5h rolling window) mode:
        #    - For a >5h book (12h) with resume at 3.0h (10800s -> resume_start 10785s), lookback 1.5h (5400s -> 5385s)
        #      snaps back to the beginning of the chapter containing 5385s (Chapter 2: 3600s..7200s -> start = 3600.0s),
        #      giving 1.5h–2h of lookback tracks before the resume track plus 3h of forward tracks (5h total).
        #    - For a <=5h book (3h), the entire book is loaded from 0.0s.
        mgr = get_bridge_manager()
        slot = mgr.assign_abs_book_to_slot(
            slot_id="slot_5h",
            abs_item_id="item_12h",
            title="Twelve Hour Epic",
            mode="offline",
        )
        fake_abs_item = {
            "id": "item_12h",
            "media": {
                "duration": 12 * 3600.0,
                "metadata": {"title": "Twelve Hour Epic"},
                "chapters": chapters,
            },
        }
        with patch.object(clients.abs_client, "get_item", return_value=fake_abs_item), \
             patch.object(clients.abs_client, "get_media_progress", return_value={"currentTime": 3 * 3600.0, "isFinished": False}):
            planned, meta = get_offline_manager()._plan_offline_tracks(
                slot, submode="next", hours=5.0, speed=1.0, abs_token=None, base_url="http://localhost:5000"
            )
            self.assertEqual(meta["mode"], "next")
            # 3h (10800) - 15s = 10785s; 10785s - 1.5h (5400s) = 5385s -> inside Chapter 2 [3600..7200], so snaps to 3600.0s
            self.assertAlmostEqual(meta["start"], 3600.0, places=1)
            self.assertIn("resume", meta)
            self.assertAlmostEqual(meta["resume"]["pos"], 3 * 3600.0 - 15.0, places=1)
            total_planned_dur = sum(t["duration"] for t in planned)
            self.assertAlmostEqual(total_planned_dur, 5.0 * 3600.0, delta=60.0)

        # Short book (<= 5 hours): loads entire book from 0.0s even if resume point is at 2.5h
        short_chapters = [
            {"start": i * 1800.0, "end": (i + 1) * 1800.0, "title": f"Short Ch {i + 1}"}
            for i in range(6)
        ]
        fake_short_item = {
            "id": "item_3h",
            "media": {
                "duration": 3 * 3600.0,
                "metadata": {"title": "Three Hour Story"},
                "chapters": short_chapters,
            },
        }
        slot_short = mgr.assign_abs_book_to_slot(
            slot_id="slot_3h",
            abs_item_id="item_3h",
            title="Three Hour Story",
            mode="offline",
        )
        with patch.object(clients.abs_client, "get_item", return_value=fake_short_item), \
             patch.object(clients.abs_client, "get_media_progress", return_value={"currentTime": 2.5 * 3600.0, "isFinished": False}):
            planned_short, meta_short = get_offline_manager()._plan_offline_tracks(
                slot_short, submode="next", hours=5.0, speed=1.0, abs_token=None, base_url="http://localhost:5000"
            )
            self.assertAlmostEqual(meta_short["start"], 0.0, places=1)
            self.assertAlmostEqual(sum(t["duration"] for t in planned_short), 3.0 * 3600.0, delta=10.0)

    def test_tap_time_resume_pause_resume_and_chapter_jump(self):
        """Verifies resolve_track_request tap-time ABS resume, pause-without-Range resume, and silence + MQTT jump."""
        from core.bridge.playback import get_playback_manager

        mgr = get_bridge_manager()
        pb = get_playback_manager()
        conn = database.get_db_connection()
        try:
            with conn:
                conn.execute("DELETE FROM bridge_slots WHERE slot_id = 'slot_resume';")
                conn.execute("""
                    INSERT INTO bridge_slots (slot_id, abs_item_id, yoto_card_id, title, total_duration, mode, own_place, local_progress)
                    VALUES ('slot_resume', 'book_res', 'AbCd1', 'Resume Book', 3600.0, 'streaming', 1, ?);
                """, (json.dumps({"book_res": {"currentTime": 1500.0, "duration": 3600.0, "isFinished": False, "lastUpdate": 1000}}),))
                for idx, (s, e) in enumerate([(0.0, 1000.0), (1000.0, 2000.0), (2000.0, 3600.0)], 1):
                    conn.execute("""
                        INSERT INTO bridge_tracks (slot_id, track_idx, abs_item_id, book_title, book_duration, title, start_time, end_time, duration, stream_url)
                        VALUES ('slot_resume', ?, 'book_res', 'Resume Book', 3600.0, ?, ?, ?, ?, ?);
                    """, (idx, f"Chapter {idx}", s, e, e - s, f"/s/audio/slot_resume/{idx}"))
        finally:
            conn.close()

        slot = mgr.get_slot("slot_resume")
        tracks = slot["tracks"]

        # Case A: Listener is at 1500s (Track 2: 1000..2000), but player requests Track 1 -> returns {"jump": 2}
        from core.bridge.mqtt_client import get_mqtt_bridge
        with patch.object(type(get_mqtt_bridge()), "is_connected", new_callable=unittest.mock.PropertyMock, return_value=True):
            res_jump = pb.resolve_track_request(slot, tracks[0])
            self.assertEqual(res_jump.get("jump"), 2)

            # Case B: Player requests Track 2 (where 1500s lives) -> seeks to 1500 - 15 = 1485s
            res_seek = pb.resolve_track_request(slot, tracks[1])
            self.assertAlmostEqual(res_seek["start_time"], 1485.0, places=1)

            # Case C: Explicit chapter selection via mark_exact_next -> starts at exact start_time (0.0) without jumping
            pb.mark_exact_next("slot_resume")
            res_exact = pb.resolve_track_request(slot, tracks[0])
            self.assertEqual(res_exact["start_time"], 0.0)

    def test_offline_mqtt_sync_and_local_own_place_progress(self):
        """Verifies OfflineTranscodeManager.on_mqtt_event syncs offline card position to own_place / ABS."""
        from core.bridge.offline import get_offline_manager
        from core.bridge.series import get_local_item_progress

        conn = database.get_db_connection()
        try:
            with conn:
                conn.execute("DELETE FROM bridge_slots WHERE slot_id = 'slot_off_sync';")
                conn.execute("""
                    INSERT INTO bridge_slots (slot_id, abs_item_id, yoto_card_id, title, total_duration, mode, own_place, offline_meta)
                    VALUES ('slot_off_sync', 'book_off', 'XyZ12', 'Offline Book', 2000.0, 'offline', 1, ?);
                """, (json.dumps({"status": "ready", "mode": "next", "speed": 1.0, "books": ["book_off"], "cur_item": "book_off"}),))
                conn.execute("""
                    INSERT INTO bridge_tracks (slot_id, track_idx, abs_item_id, book_title, book_duration, title, start_time, end_time, duration, yoto_sha256, stream_url)
                    VALUES ('slot_off_sync', 1, 'book_off', 'Offline Book', 2000.0, 'Chapter 1', 500.0, 1500.0, 1000.0, 'sha256_track1', '/s/audio/slot_off_sync/1');
                """)
        finally:
            conn.close()

        get_offline_manager().on_mqtt_event("dev_1", {
            "cardId": "XyZ12",
            "chapterKey": "01",
            "trackKey": "01",
            "position": 120,
            "playbackStatus": "paused",
        })

        local_prog = get_local_item_progress("slot_off_sync", "book_off")
        self.assertIsNotNone(local_prog)
        self.assertAlmostEqual(float(local_prog.get("currentTime") or 0.0), 620.0, places=1)

    def test_procedural_icons_chapter_finder_and_viterbi_transfer(self):
        """Verifies 16x16 procedural chapter icon PNG generation, Audible chapter proposal, and Viterbi MYO transfer."""
        from core.bridge.icons import render_track_icon_png
        from core.bridge.chapter_finder import build_proposal
        from core.bridge.transfer import TransferBook, match_tracks_to_books

        # 1. Procedural 16x16 PNG icon
        png_bytes = render_track_icon_png(
            abs_item_id="nonexistent_item",
            track_idx=7,
            style="number",
        )
        self.assertTrue(png_bytes.startswith(b"\x89PNG\r\n\x1a\n"))

        # 2. Audible Chapter Finder proposal alignment
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
        self.assertIsNotNone(prop)
        self.assertEqual(prop["quality"], "good")
        self.assertEqual(len(prop["chapters"]), 3)
        self.assertEqual(prop["chapters"][-1]["end"], 3005.0)

        # 3. Viterbi MYO card track-to-book matcher
        books = [
            TransferBook.from_abs_item({
                "id": "b1",
                "media": {
                    "duration": 1800.0,
                    "metadata": {"title": "Book One", "seriesName": "Saga"},
                    "chapters": [
                        {"start": 0.0, "end": 600.0, "title": "Ch 1"},
                        {"start": 600.0, "end": 1200.0, "title": "Ch 2"},
                        {"start": 1200.0, "end": 1800.0, "title": "Ch 3"},
                    ],
                },
            }),
            TransferBook.from_abs_item({
                "id": "b2",
                "media": {
                    "duration": 1000.0,
                    "metadata": {"title": "Book Two", "seriesName": "Saga"},
                    "chapters": [
                        {"start": 0.0, "end": 500.0, "title": "Ch 1"},
                        {"start": 500.0, "end": 1000.0, "title": "Ch 2"},
                    ],
                },
            }),
        ]
        card_tracks = [
            {"title": "Book One - Track 1", "duration": 600.0},
            {"title": "Book One - Track 2", "duration": 600.0},
            {"title": "Book One - Track 3", "duration": 600.0},
            {"title": "Book Two - Track 4", "duration": 500.0},
            {"title": "Book Two - Track 5", "duration": 500.0},
        ]
        matches = match_tracks_to_books(card_tracks, books)
        self.assertEqual(len(matches), 5)
        self.assertEqual(matches[0].book, 0)
        self.assertEqual(matches[2].book, 0)
        self.assertEqual(matches[3].book, 1)
        self.assertAlmostEqual(matches[3].start, 0.0, places=1)
        self.assertAlmostEqual(matches[4].start, 500.0, places=1)

    def test_offline_series_chapters_mode_whole_books_and_rolling(self):
        """Verifies default offline series mode keeps chapters intact, loads whole books <= 100 tracks, and rolls finished books."""
        from core.bridge.offline import get_offline_manager, refresh_offline_series_slots

        series_items = []
        abs_items_by_id = {}
        for idx in range(1, 5):
            bid = f"ser_roll_b{idx}"
            title = f"Test Saga Book {idx}"
            chapters = [
                {"start": i * 600.0, "end": (i + 1) * 600.0, "title": f"Chapter {i + 1}"}
                for i in range(30)
            ]
            series_items.append({
                "id": bid,
                "type": "abs",
                "title": title,
                "author": "Author",
                "raw_metadata": {
                    "series": "Test Saga",
                    "series_order": str(idx),
                    "series_list": [{"name": "Test Saga", "order": str(idx)}],
                    "duration": 30 * 600.0,
                },
            })
            abs_items_by_id[bid] = {
                "id": bid,
                "media": {
                    "duration": 30 * 600.0,
                    "metadata": {"title": title, "seriesName": f"Test Saga #{idx}"},
                    "chapters": chapters,
                },
            }

        database.upsert_media_items(series_items)
        old_packing = database.get_system_settings().get("offline_series_packing")
        database.set_system_setting("offline_series_packing", "chapters")
        try:
            mgr = get_bridge_manager()
            with patch.object(clients.abs_client, "get_item", side_effect=lambda item_id, token=None: abs_items_by_id.get(item_id)), \
                 patch.object(clients.abs_client, "get_media_progress", return_value={}):
                slot = mgr.assign_abs_book_to_slot(
                    slot_id="slot_series_roll",
                    abs_item_id="ser_roll_b1",
                    title="Test Saga Book 1",
                    series_name="Test Saga",
                    group_kind="series",
                    mode="offline",
                )
                with database.get_db_connection() as conn:
                    conn.execute(
                        "UPDATE bridge_slots SET offline_mode = 'series' WHERE slot_id = ?;",
                        ("slot_series_roll",),
                    )
                    conn.commit()
                slot = mgr.get_slot("slot_series_roll")

                planned, meta = get_offline_manager()._plan_offline_tracks(
                    slot, submode="series", hours=5.0, speed=1.0, abs_token=None, base_url="http://localhost:5055"
                )
                self.assertEqual(meta["mode"], "series")
                self.assertEqual(meta["series_packing"], "chapters")
                # Books 1-3 = 90 uncondensed chapters (<= 100); Book 4 would push to 120 so it is held back intact.
                self.assertEqual(meta["books"], ["ser_roll_b1", "ser_roll_b2", "ser_roll_b3"])
                self.assertEqual(meta["books_left_off"], 1)
                self.assertEqual(len(planned), 90)
                self.assertFalse(any(" – " in t["title"] for t in planned))
                self.assertEqual(planned[0]["title"], "Test Saga Book 1: Chapter 1")
                self.assertEqual(planned[-1]["title"], "Test Saga Book 3: Chapter 30")

                # Simulate Books 2 & 3 already uploaded on card with cached Yoto SHA hashes
                with database.get_db_connection() as conn:
                    conn.execute(
                        "UPDATE bridge_tracks SET yoto_sha256 = 'sha_' || abs_item_id || '_' || track_idx "
                        "WHERE slot_id = 'slot_series_roll' AND abs_item_id IN ('ser_roll_b2', 'ser_roll_b3');"
                    )
                    conn.execute("UPDATE bridge_slots SET offline_status = 'ready' WHERE slot_id = 'slot_series_roll';")
                    conn.commit()

            # Mark Book 1 finished in ABS progress; verify rolling check detects finish and queues refresh
            def _prog_after_b1(item_id, token=None, **kwargs):
                if item_id == "ser_roll_b1":
                    return {"currentTime": 18000.0, "isFinished": True, "lastUpdate": 2000}
                return {}

            with patch.object(clients.abs_client, "get_item", side_effect=lambda item_id, token=None: abs_items_by_id.get(item_id)), \
                 patch.object(clients.abs_client, "get_media_progress", side_effect=_prog_after_b1):
                with patch.object(get_offline_manager(), "queue_transcode") as mock_queue:
                    rolled = refresh_offline_series_slots()
                    self.assertEqual(rolled, 1)
                    mock_queue.assert_called_once()
                    self.assertEqual(mock_queue.call_args.args[0], "slot_series_roll")

                # Now execute planning after refresh rolled slot to Book 2 -> Books 2, 3, 4 fit (90 tracks) and reuse SHAs for B2 & B3
                updated_slot = mgr.get_slot("slot_series_roll")
                self.assertEqual(updated_slot["abs_item_id"], "ser_roll_b2")
                planned2, meta2 = get_offline_manager()._plan_offline_tracks(
                    updated_slot, submode="series", hours=5.0, speed=1.0, abs_token=None, base_url="http://localhost:5055"
                )
                self.assertEqual(meta2["books"], ["ser_roll_b2", "ser_roll_b3", "ser_roll_b4"])
                self.assertEqual(meta2["books_left_off"], 0)
                self.assertEqual(len(planned2), 90)
                b2_shas = [t["yoto_sha256"] for t in planned2 if t["abs_item_id"] == "ser_roll_b2"]
                b4_shas = [t["yoto_sha256"] for t in planned2 if t["abs_item_id"] == "ser_roll_b4"]
                self.assertTrue(all(b2_shas))
                self.assertTrue(all(not s for s in b4_shas))
        finally:
            if old_packing is None:
                with database.get_db_connection() as conn:
                    conn.execute("DELETE FROM system_settings WHERE key = 'offline_series_packing';")
                    conn.commit()
            else:
                database.set_system_setting("offline_series_packing", old_packing)
            with database.get_db_connection() as conn:
                conn.execute("DELETE FROM media_items WHERE id LIKE 'ser_roll_b%';")
                conn.commit()

    def test_offline_series_condense_mode_and_single_book_overflow(self):
        """Verifies 'condense' merges chapters to pack max books and 'chapters' condenses only when a single book exceeds 100 tracks."""
        from core.bridge.offline import get_offline_manager

        series_items = []
        abs_items_by_id = {}
        for idx in range(1, 5):
            bid = f"ser_cond_b{idx}"
            title = f"Condense Saga {idx}"
            chapters = [
                {"start": i * 600.0, "end": (i + 1) * 600.0, "title": f"Ch {i + 1}"}
                for i in range(30)
            ]
            series_items.append({
                "id": bid,
                "type": "abs",
                "title": title,
                "author": "Author",
                "raw_metadata": {
                    "series": "Condense Saga",
                    "series_order": str(idx),
                    "series_list": [{"name": "Condense Saga", "order": str(idx)}],
                    "duration": 30 * 600.0,
                },
            })
            abs_items_by_id[bid] = {
                "id": bid,
                "media": {
                    "duration": 30 * 600.0,
                    "metadata": {"title": title, "seriesName": f"Condense Saga #{idx}"},
                    "chapters": chapters,
                },
            }

        huge_chapters = [
            {"start": i * 600.0, "end": (i + 1) * 600.0, "title": f"Huge Ch {i + 1}"}
            for i in range(120)
        ]
        abs_items_by_id["ser_cond_huge"] = {
            "id": "ser_cond_huge",
            "media": {
                "duration": 120 * 600.0,
                "metadata": {"title": "Huge Book"},
                "chapters": huge_chapters,
            },
        }
        database.upsert_media_items(series_items)
        try:
            mgr = get_bridge_manager()
            with patch.object(clients.abs_client, "get_item", side_effect=lambda item_id, token=None: abs_items_by_id.get(item_id)), \
                 patch.object(clients.abs_client, "get_media_progress", return_value={}):
                slot = mgr.assign_abs_book_to_slot(
                    slot_id="slot_series_cond",
                    abs_item_id="ser_cond_b1",
                    title="Condense Saga 1",
                    series_name="Condense Saga",
                    group_kind="series",
                    mode="offline",
                )
                with database.get_db_connection() as conn:
                    conn.execute(
                        "UPDATE bridge_slots SET offline_mode = 'series', series_packing = 'condense' WHERE slot_id = ?;",
                        ("slot_series_cond",),
                    )
                    conn.commit()
                slot = mgr.get_slot("slot_series_cond")

                planned_cond, meta_cond = get_offline_manager()._plan_offline_tracks(
                    slot, submode="series", hours=5.0, speed=1.0, abs_token=None, base_url="http://localhost:5055"
                )
                self.assertEqual(meta_cond["series_packing"], "condense")
                self.assertEqual(meta_cond["books"], ["ser_cond_b1", "ser_cond_b2", "ser_cond_b3", "ser_cond_b4"])
                self.assertLessEqual(len(planned_cond), 100)
                self.assertTrue(any(" – " in t["title"] for t in planned_cond))

                # Edge case: single huge book (> 100 chapters) in 'chapters' mode falls back cleanly for that one book
                slot_huge = mgr.assign_abs_book_to_slot(
                    slot_id="slot_huge_ch",
                    abs_item_id="ser_cond_huge",
                    title="Huge Book",
                    series_name="Unlisted Saga",
                    group_kind="series",
                    mode="offline",
                )
                with database.get_db_connection() as conn:
                    conn.execute(
                        "UPDATE bridge_slots SET offline_mode = 'series', series_packing = 'chapters' WHERE slot_id = ?;",
                        ("slot_huge_ch",),
                    )
                    conn.commit()
                slot_huge = mgr.get_slot("slot_huge_ch")
                planned_huge, meta_huge = get_offline_manager()._plan_offline_tracks(
                    slot_huge, submode="series", hours=5.0, speed=1.0, abs_token=None, base_url="http://localhost:5055"
                )
                self.assertEqual(meta_huge["series_packing"], "chapters")
                self.assertEqual(meta_huge["books"], ["ser_cond_huge"])
                self.assertLessEqual(len(planned_huge), 100)
        finally:
            with database.get_db_connection() as conn:
                conn.execute("DELETE FROM media_items WHERE id LIKE 'ser_cond_b%';")
                conn.commit()

    def test_catalog_settings_offline_series_packing_and_slot_override(self):
        """Verifies GET/PUT /api/admin/settings/catalog and per-slot series_packing override precedence."""
        from core.bridge.offline import resolve_series_packing

        old_packing = database.get_system_settings().get("offline_series_packing")
        try:
            res_get = self.app.get("/api/admin/settings/catalog")
            self.assertEqual(res_get.status_code, 200)
            self.assertEqual(res_get.get_json().get("offline_series_packing"), "chapters")

            res_put = self.app.put("/api/admin/settings/catalog", json={"offline_series_packing": "condense"})
            self.assertEqual(res_put.status_code, 200)
            self.assertEqual(res_put.get_json().get("offline_series_packing"), "condense")
            self.assertEqual(resolve_series_packing(None), "condense")

            mgr = get_bridge_manager()
            mgr.assign_abs_book_to_slot(slot_id="slot_pack_ov", abs_item_id="item_pack", title="Pack Book", mode="offline")
            res_slot = self.app.put("/api/cards/slot_pack_ov", json={
                "new_slot_id": "slot_pack_ov",
                "nickname": "Updated Fleet",
                "kind": "permanent_myo",
                "owner_profile_id": "",
                "series_packing": "chapters",
            })
            self.assertEqual(res_slot.status_code, 200)
            slot = mgr.get_slot("slot_pack_ov")
            self.assertIsNone(slot.get("owner_profile_id"))
            self.assertEqual(slot.get("nickname"), "Updated Fleet")
            self.assertEqual(slot.get("series_packing"), "chapters")
            self.assertEqual(resolve_series_packing(slot), "chapters")

            # Clearing per-card override falls back to global setting ('condense')
            res_clear = self.app.put("/api/cards/slot_pack_ov", json={"series_packing": ""})
            self.assertEqual(res_clear.status_code, 200)
            slot_cleared = mgr.get_slot("slot_pack_ov")
            self.assertEqual(resolve_series_packing(slot_cleared), "condense")
        finally:
            if old_packing is None:
                with database.get_db_connection() as conn:
                    conn.execute("DELETE FROM system_settings WHERE key = 'offline_series_packing';")
                    conn.commit()
            else:
                database.set_system_setting("offline_series_packing", old_packing)

    def test_series_start_point_options_selected_active_beginning(self):
        """Verifies POST /api/cards/assign supports series_start = 'selected', 'active', and 'beginning' across streaming and offline."""
        from core.bridge.offline import get_offline_manager

        series_items = []
        abs_items_by_id = {}
        for idx in range(1, 5):
            bid = f"ser_start_b{idx}"
            title = f"Start Saga Book {idx}"
            chapters = [
                {"start": i * 600.0, "end": (i + 1) * 600.0, "title": f"Ch {i + 1}"}
                for i in range(10)
            ]
            series_items.append({
                "id": bid,
                "type": "abs",
                "title": title,
                "author": "Author",
                "raw_metadata": {
                    "series": "Start Saga",
                    "series_order": str(idx),
                    "series_list": [{"name": "Start Saga", "order": str(idx)}],
                    "duration": 10 * 600.0,
                },
            })
            abs_items_by_id[bid] = {
                "id": bid,
                "media": {
                    "duration": 10 * 600.0,
                    "metadata": {"title": title, "seriesName": f"Start Saga #{idx}"},
                    "chapters": chapters,
                },
            }

        database.upsert_media_items(series_items)
        mgr = get_bridge_manager()
        try:
            # Book 1 finished, Book 2 in progress (more recently touched), Books 3 & 4 unplayed
            def _prog(item_id, token=None, **kwargs):
                if item_id == "ser_start_b1":
                    return {"currentTime": 6000.0, "isFinished": True, "lastUpdate": 1000}
                if item_id == "ser_start_b2":
                    return {"currentTime": 1800.0, "isFinished": False, "lastUpdate": 2000}
                return {}

            # Ensure fleet slot exists
            mgr.assign_abs_book_to_slot(slot_id="fleet_ser_start", abs_item_id="ser_start_b1", title="Init", mode="streaming")

            with patch.object(clients.abs_client, "get_item", side_effect=lambda item_id, token=None: abs_items_by_id.get(item_id)), \
                 patch.object(clients.abs_client, "get_media_progress", side_effect=_prog), \
                 patch.object(get_offline_manager(), "queue_transcode", return_value={"status": "queued"}) as mock_queue:

                # 1. series_start="selected" from Book 3 (streaming): starts on Book 3 and chains Book 4
                r_sel = self.app.post("/api/cards/assign", json={
                    "slot_id": "fleet_ser_start",
                    "abs_item_id": "ser_start_b3",
                    "mode": "streaming",
                    "group_kind": "series",
                    "series_name": "Start Saga",
                    "series_start": "selected",
                    "title": "Start Saga Book 3",
                })
                self.assertEqual(r_sel.status_code, 200)
                d_sel = r_sel.get_json()
                self.assertEqual(d_sel["abs_item_id"], "ser_start_b3")
                self.assertEqual(d_sel["tracks"][0]["abs_item_id"], "ser_start_b3")

                # 1b. series_start="selected" in offline_series passes anchor_current_book=True and keeps Book 3 first
                r_sel_off = self.app.post("/api/cards/assign", json={
                    "slot_id": "fleet_ser_start",
                    "abs_item_id": "ser_start_b3",
                    "mode": "offline_series",
                    "group_kind": "series",
                    "series_name": "Start Saga",
                    "series_start": "selected",
                    "title": "Start Saga Book 3",
                })
                self.assertEqual(r_sel_off.status_code, 200)
                self.assertTrue(mock_queue.call_args.kwargs.get("anchor_current_book"))
                planned_sel, meta_sel = get_offline_manager()._plan_offline_tracks(
                    r_sel_off.get_json(),
                    submode="series",
                    hours=5.0,
                    speed=1.0,
                    abs_token=None,
                    base_url="http://localhost:5055",
                    anchor_current_book=True,
                )
                self.assertEqual(meta_sel["books"], ["ser_start_b3", "ser_start_b4"])
                self.assertEqual(planned_sel[0]["abs_item_id"], "ser_start_b3")

                # 2. series_start="beginning" from Book 3: starts at Book 1 even though Book 1 was marked finished
                r_beg = self.app.post("/api/cards/assign", json={
                    "slot_id": "fleet_ser_start",
                    "abs_item_id": "ser_start_b3",
                    "mode": "offline_series",
                    "group_kind": "series",
                    "series_name": "Start Saga",
                    "series_start": "beginning",
                    "title": "Start Saga Book 3",
                })
                self.assertEqual(r_beg.status_code, 200)
                d_beg = r_beg.get_json()
                self.assertEqual(d_beg["abs_item_id"], "ser_start_b1")
                self.assertEqual(d_beg["title"], "Start Saga Book 1")
                self.assertTrue(mock_queue.call_args.kwargs.get("anchor_current_book"))
                planned_beg, meta_beg = get_offline_manager()._plan_offline_tracks(
                    d_beg,
                    submode="series",
                    hours=5.0,
                    speed=1.0,
                    abs_token=None,
                    base_url="http://localhost:5055",
                    anchor_current_book=True,
                )
                self.assertEqual(meta_beg["books"], ["ser_start_b1", "ser_start_b2", "ser_start_b3", "ser_start_b4"])
                self.assertEqual(planned_beg[0]["abs_item_id"], "ser_start_b1")

                # 3. series_start="active" from Book 3: resolves to Book 2 (active unfinished book)
                r_act = self.app.post("/api/cards/assign", json={
                    "slot_id": "fleet_ser_start",
                    "abs_item_id": "ser_start_b3",
                    "mode": "offline_series",
                    "group_kind": "series",
                    "series_name": "Start Saga",
                    "series_start": "active",
                    "title": "Start Saga Book 3",
                })
                self.assertEqual(r_act.status_code, 200)
                d_act = r_act.get_json()
                self.assertEqual(d_act["abs_item_id"], "ser_start_b2")
                self.assertEqual(d_act["title"], "Start Saga Book 2")
                self.assertFalse(mock_queue.call_args.kwargs.get("anchor_current_book"))
        finally:
            with database.get_db_connection() as conn:
                conn.execute("DELETE FROM media_items WHERE id LIKE 'ser_start_b%';")
                conn.commit()

    def test_offline_failure_persists_error_and_retry_endpoint(self):
        """Verifies failed offline builds persist error details in SQLite, expose them on /api/cards, and retry via POST /api/cards/<id>/retry."""
        from core.bridge.offline import get_offline_manager

        fake_item = {
            "id": "item_fail",
            "media": {
                "duration": 1200.0,
                "metadata": {"title": "Fail Test Book"},
                "chapters": [
                    {"start": 0.0, "end": 600.0, "title": "Ch 1"},
                    {"start": 600.0, "end": 1200.0, "title": "Ch 2"},
                ],
            },
        }
        mgr = get_bridge_manager()
        off_mgr = get_offline_manager()
        with patch.object(clients.abs_client, "get_item", return_value=fake_item), \
             patch.object(clients.abs_client, "get_media_progress", return_value={}):
            mgr.assign_abs_book_to_slot(
                slot_id="slot_fail_retry",
                abs_item_id="item_fail",
                title="Fail Test Book",
                mode="offline",
            )
            with patch("core.bridge.offline.shutil.which", return_value=None):
                off_mgr._jobs["slot_fail_retry"] = {"slot_id": "slot_fail_retry", "status": "queued"}
                off_mgr._run_job("slot_fail_retry", base_url="http://localhost:5055", offline_mode="book", hours=5.0)

        # Clear in-memory job cache to verify GET /api/cards reads persisted SQLite error state
        off_mgr._jobs.pop("slot_fail_retry", None)
        res_list = self.app.get("/api/cards")
        self.assertEqual(res_list.status_code, 200)
        card_entry = next((c for c in res_list.get_json() if c["slot_id"] == "slot_fail_retry"), None)
        self.assertIsNotNone(card_entry)
        self.assertEqual(card_entry["offline_status"], "failed")
        self.assertEqual(card_entry["transcode"]["status"], "failed")
        self.assertIn("ffmpeg", card_entry["transcode"]["error"].lower())

        with patch.object(off_mgr, "queue_transcode", return_value={"status": "queued"}) as mock_retry:
            res_retry = self.app.post("/api/cards/slot_fail_retry/retry")
            self.assertEqual(res_retry.status_code, 200)
            payload = res_retry.get_json()
            self.assertTrue(payload["success"])
            mock_retry.assert_called_once()
            self.assertEqual(mgr.get_slot("slot_fail_retry")["offline_status"], "building")


if __name__ == "__main__":
    unittest.main()

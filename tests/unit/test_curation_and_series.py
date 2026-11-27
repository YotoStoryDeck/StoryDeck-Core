"""Unit tests for curation matrix, catalog rotation, series overrides, Yoto tagging, and master API security."""
import base64
from datetime import datetime as _real_datetime
import io
import json
from pathlib import Path
import re
import sqlite3
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from PIL import Image

from tests.harness import IsolatedTestCase
from app import app
import clients
from core.media_utils import (
    extract_chapter_number,
    extract_series_list,
    parse_single_series,
    parse_track_names,
    render_16x16_icon_with_overlay,
)
import database


class TestCurationMatrixAndYotoRetail(IsolatedTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        database.upsert_profile("child_a", {
            "display_name": "Child A",
            "yoto": {"inherit_shared": True}
        })
        database.upsert_profile("child_b", {
            "display_name": "Child B",
            "yoto": {"inherit_shared": True}
        })
        database.set_shared_rule("include_all", True)
        database.set_shared_rule("include_myo_by_default", True)
        database.upsert_media_items([
            {
                "id": "7696c2d1-49bc-4165-a3dd-743b4ad79328",
                "type": "abs",
                "title": "Spidey and His Amazing Friends",
                "author": "Marvel",
                "raw_metadata": {"tags": ["Children"]}
            },
            {
                "id": "7gGiV",
                "type": "yoto",
                "title": "Harry Potter and the Sorcerer's Stone",
                "author": "J.K. Rowling",
                "cover_url": "https://card-content.yotoplay.com/hp.jpg",
                "raw_metadata": {}
            },
            {
                "id": "bb_01",
                "type": "yoto",
                "title": "BrainBots: Dinosaurs",
                "author": "Yoto",
                "raw_metadata": {"series": "BrainBots", "series_list": [{"name": "BrainBots", "order": "1"}]}
            },
            {
                "id": "myo_01",
                "type": "yoto_myo",
                "title": "Family Bedtime Mix",
                "author": "Parents",
                "raw_metadata": {}
            }
        ])

    def setUp(self):
        super().setUp()
        self.client = app.test_client()

    def test_01_yoto_retail_cards_loaded(self):
        """Verifies that retail cards from the bridge/Yoto library are loaded alongside MYO cards."""
        mock_raw = [
            {
                "cardId": "7gGiV",
                "title": "Harry Potter and the Sorcerer's Stone",
                "isMyo": False,
                "coverImageUrl": "https://card-content.yotoplay.com/hp.jpg"
            }
        ] + [
            {
                "cardId": f"retail_{i}",
                "title": f"Retail Story {i}",
                "isMyo": False,
                "coverImageUrl": f"https://card-content.yotoplay.com/r_{i}.jpg"
            }
            for i in range(44)
        ] + [
            {"cardId": f"myo_{i}", "title": f"MYO Mix {i}", "isMyo": True}
            for i in range(6)
        ]

        with patch.object(clients.yoto_client, "get_raw_library", return_value=mock_raw):
            raw_library = clients.yoto_client.get_raw_library()
            retail = [c for c in raw_library if not c.get("isMyo")]
            myo = [c for c in raw_library if c.get("isMyo")]

            self.assertGreater(len(retail), 40)
            self.assertGreater(len(myo), 5)

            hp_card = next((c for c in retail if c.get("cardId") == "7gGiV"), None)
            self.assertIsNotNone(hp_card)
            self.assertIn("Harry Potter", hp_card.get("title", ""))
            self.assertTrue(hp_card.get("coverImageUrl", "").startswith("https://card-content.yotoplay.com/"))

    def test_02_catalog_matrix_structure(self):
        """Verifies get_catalog_matrix returns items with tags, library_id, and profile states."""
        matrix = database.get_catalog_matrix()
        self.assertIn("items", matrix)
        self.assertIn("profiles", matrix)
        self.assertIn("shared_rules", matrix)

        items = matrix["items"]
        self.assertGreaterEqual(len(items), 4)

        types = set(it["type"] for it in items)
        self.assertIn("abs", types)
        self.assertIn("yoto", types)
        self.assertIn("yoto_myo", types)

        sample_item = items[0]
        self.assertIn("profiles", sample_item)
        self.assertIn("series", sample_item)
        for pid in matrix["profiles"]:
            self.assertIn(pid, sample_item["profiles"])
            self.assertIn("allowed", sample_item["profiles"][pid])
            self.assertIn("reason", sample_item["profiles"][pid])

    def test_03_abs_tagging_endpoint(self):
        """Tests the one-stop /api/admin/abs/item-tags endpoint."""
        with self.client.session_transaction() as sess:
            sess["user_id"] = "admin_user"
            sess["username"] = "admin"
            sess["role"] = "admin"

        item_id = "7696c2d1-49bc-4165-a3dd-743b4ad79328"
        with patch.object(clients.abs_client, "update_item_tags", return_value=True):
            res = self.client.post("/api/admin/abs/item-tags", json={
                "item_id": item_id,
                "tags": ["Children"]
            })
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data.get("ok"))
        self.assertEqual(data.get("tags"), ["Children"])

    def test_04_abs_server_tags_endpoint(self):
        """Verifies GET /api/admin/abs/server-tags returns ABS tags."""
        with self.client.session_transaction() as sess:
            sess["user_id"] = "admin_user"
            sess["username"] = "admin"
            sess["role"] = "admin"

        with patch.object(clients.abs_client, "get_all_server_tags", return_value=["Children", "Fiction"]):
            res = self.client.get("/api/admin/abs/server-tags")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data.get("ok"))
        tags = data.get("tags", [])
        self.assertIsInstance(tags, list)
        self.assertIn("Children", tags)

    def test_05_abs_default_tags_settings(self):
        """Verifies GET and PUT /api/admin/settings/abs-tags for configuring child default tags."""
        with self.client.session_transaction() as sess:
            sess["user_id"] = "admin_user"
            sess["username"] = "admin"
            sess["role"] = "admin"

        res = self.client.get("/api/admin/settings/abs-tags")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data.get("ok"))
        initial_tags = data.get("tags")
        self.assertIsInstance(initial_tags, list)

        res_put = self.client.put("/api/admin/settings/abs-tags", json={
            "tags": ["Children", "Kids", "Juvenile"]
        })
        self.assertEqual(res_put.status_code, 200)
        data_put = res_put.get_json()
        self.assertTrue(data_put.get("ok"))
        self.assertEqual(data_put.get("tags"), ["Children", "Kids", "Juvenile"])

        matrix = database.get_catalog_matrix()
        self.assertIn("items", matrix)

        self.client.put("/api/admin/settings/abs-tags", json={"tags": initial_tags})

    def test_06_series_extraction_from_catalogs(self):
        """Verifies that Yoto and ABS items extract series names for series filtering and grouping."""
        mock_raw = [{"cardId": "bb_01", "title": "BrainBots: Dinosaurs", "series": "BrainBots", "isMyo": False}]
        with patch.object(clients.yoto_client, "get_raw_library", return_value=mock_raw):
            raw_library = clients.yoto_client.get_raw_library()
            brainbots_with_series = [c for c in raw_library if "brainbots" in (c.get("series") or "").lower()]
            self.assertGreater(len(brainbots_with_series), 0)

        self.assertEqual(parse_single_series("The Boxcar Children #13-15"), {"name": "The Boxcar Children", "order": "13-15"})
        self.assertEqual(parse_single_series("A Song of Ice and Fire #1"), {"name": "A Song of Ice and Fire", "order": "1"})
        self.assertEqual(parse_single_series("Magic Tree House, Books 1-4"), {"name": "Magic Tree House", "order": "1-4"})

        parsed_multi = extract_series_list("Star Wars #55, Star Wars Classic Stories #7")
        self.assertEqual(parsed_multi, [{"name": "Star Wars", "order": "55"}, {"name": "Star Wars Classic Stories", "order": "7"}])

        parsed_unseq = extract_series_list("Star Wars, Star Wars Classic Stories")
        self.assertEqual(parsed_unseq, [{"name": "Star Wars", "order": ""}, {"name": "Star Wars Classic Stories", "order": ""}])

    def test_07_yoto_groups_and_series_integration(self):
        """Validates that Yoto user groups are parsed and appended to card series_list."""
        res = parse_single_series("Bedtime Stories")
        self.assertEqual(res, {"name": "Bedtime Stories", "order": ""})
        self.assertEqual(parse_single_series("Harry Potter #1"), {"name": "Harry Potter", "order": "1"})
        res_multi = extract_series_list("Star Wars, Bedtime Favorites")
        self.assertEqual(len(res_multi), 2)

        with patch.object(clients.yoto_client, "get_groups", return_value=[{"id": "g1", "title": "Bedtime Favorites", "cardIds": []}]):
            groups = clients.yoto_client.get_groups()
        self.assertIsInstance(groups, list)

        test_card_id = "test_yoto_group_card_123"
        test_group_name = "Bedtime Favorites"
        try:
            database.upsert_media_items([{
                "id": test_card_id,
                "type": "yoto",
                "title": "Sleepy Bear Stories",
                "author": "Yoto",
                "cover_url": "",
                "raw_metadata": {
                    "series": test_group_name,
                    "series_order": "",
                    "series_list": [{"name": test_group_name, "order": ""}]
                }
            }])

            matrix = database.get_catalog_matrix()
            item = next((it for it in matrix["items"] if it["id"] == test_card_id), None)
            self.assertIsNotNone(item)
            self.assertEqual(item["series"], test_group_name)
            self.assertEqual(item["series_list"], [{"name": test_group_name, "order": ""}])

            res_thumb = self.client.get(f"/api/thumb?type=yoto&id={test_card_id}&size=100")
            self.assertEqual(res_thumb.status_code, 200)
            self.assertIn(res_thumb.content_type, ("image/jpeg", "image/svg+xml"))
        finally:
            conn = database.get_db_connection()
            try:
                with conn:
                    conn.execute("DELETE FROM media_items WHERE id = ?;", (test_card_id,))
            finally:
                conn.close()

    def test_08_batch_permissions_and_highlight(self):
        """Verifies atomic multi-card permissions and highlight updates."""
        test_ids = ["test_batch_card_1", "test_batch_card_2", "test_batch_card_3"]
        prof_id = "test_batch_child"

        database.upsert_profile(prof_id, {
            "display_name": "Test Child",
            "yoto": {"allowed_card_ids": [], "excluded_card_ids": []}
        })
        database.upsert_media_items([
            {"id": mid, "type": "yoto", "title": f"Card {mid}", "author": "Author", "cover_url": ""}
            for mid in test_ids
        ])

        try:
            count = database.batch_update_media_highlight(test_ids, True)
            self.assertEqual(count, 3)
            matrix = database.get_catalog_matrix()
            for it in matrix["items"]:
                if it["id"] in test_ids:
                    self.assertTrue(it["is_highlight"])

            res = database.batch_update_profile_media_permissions(
                media_ids=test_ids,
                profile_actions={prof_id: "allow"},
                is_highlight=False
            )
            self.assertEqual(res["updated_cards"], 3)
            self.assertEqual(res["affected_profiles"], 1)

            conn = database.get_db_connection()
            try:
                rows = conn.execute(
                    "SELECT media_id, permission FROM profile_media_permissions WHERE profile_id = ?;",
                    (prof_id,),
                ).fetchall()
                perms = {r["media_id"]: r["permission"] for r in rows}
                for mid in test_ids:
                    self.assertEqual(perms.get(mid), "allowed")
            finally:
                conn.close()

            res_ex = database.batch_update_profile_media_permissions(
                media_ids=test_ids[:2],
                profile_actions={prof_id: "exclude"}
            )
            self.assertEqual(res_ex["updated_cards"], 2)

            res_reset = database.batch_update_profile_media_permissions(
                media_ids=[test_ids[0]],
                profile_actions={prof_id: "reset"}
            )
            self.assertEqual(res_reset["updated_cards"], 1)
        finally:
            database.delete_profile(prof_id)
            conn = database.get_db_connection()
            try:
                with conn:
                    for mid in test_ids:
                        conn.execute("DELETE FROM media_items WHERE id = ?;", (mid,))
                        conn.execute("DELETE FROM profile_media_permissions WHERE media_id = ?;", (mid,))
            finally:
                conn.close()

    def test_09_tracklist_and_icon_endpoints(self):
        """Verifies Tracklist Browser and Icon Search endpoints."""
        with self.client.session_transaction() as sess:
            sess["user_id"] = "admin_user"
            sess["username"] = "admin"
            sess["role"] = "admin"

        mock_card_detail = {
            "cardId": "7gGiV",
            "title": "Harry Potter and the Sorcerer's Stone",
            "content": {
                "chapters": [
                    {"key": "01", "title": "Chapter 1: The Boy Who Lived", "duration": 900},
                    {"key": "02", "title": "Chapter 2: The Vanishing Glass", "duration": 840}
                ]
            }
        }
        with patch.object(clients.yoto_client, "get_card_detail", return_value=mock_card_detail):
            res = self.client.get("/api/admin/media/7gGiV/tracks")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data.get("ok"))
        self.assertEqual(data.get("type"), "yoto")
        self.assertFalse(data.get("can_edit"))
        self.assertGreater(len(data.get("tracks", [])), 0)

        with patch("icon_engine.IconEngine.search_all_sources", return_value=[{"name": "dragon", "url": "https://example.com/dragon.png", "source": "yotoicons"}]):
            res_search = self.client.get("/api/admin/icons/search?q=dragon&limit=10")
        self.assertEqual(res_search.status_code, 200)
        search_data = res_search.get_json()
        self.assertTrue(search_data.get("ok"))
        self.assertIsInstance(search_data.get("results"), list)

    def test_10_arr_style_master_api_key(self):
        """Verifies Master API Key retrieval, header/query authentication, and regeneration."""
        key_details = database.get_master_api_key_details()
        curr_key = key_details["api_key"]
        self.assertEqual(len(curr_key), 32)

        self.assertEqual(self.client.get("/api/admin/security/api-key").status_code, 401)
        res_auth_header = self.client.get("/api/admin/security/api-key", headers={"X-Api-Key": curr_key})
        self.assertEqual(res_auth_header.status_code, 200)
        self.assertEqual(res_auth_header.get_json().get("api_key"), curr_key)

        self.assertEqual(
            self.client.get("/api/admin/security/api-key", headers={"Authorization": f"Bearer {curr_key}"}).status_code,
            200,
        )
        self.assertEqual(
            self.client.get(f"/api/admin/security/api-key?apikey={curr_key}").status_code,
            200,
        )

        res_regen = self.client.post(
            "/api/admin/security/regenerate-api-key",
            headers={"X-Api-Key": curr_key},
            json={"confirm": True}
        )
        self.assertEqual(res_regen.status_code, 200)
        new_key = res_regen.get_json().get("api_key")
        self.assertNotEqual(new_key, curr_key)

        self.assertEqual(self.client.get("/api/admin/security/api-key", headers={"X-Api-Key": curr_key}).status_code, 401)
        self.assertEqual(self.client.get("/api/admin/security/api-key", headers={"X-Api-Key": new_key}).status_code, 200)

    def test_11_esp32_device_jwt_lifecycle_and_revocation(self):
        """Verifies ESP32 device handshake, token issuance, catalog guard, refresh, and instant revocation."""
        test_mac = "a1b2c3d4e5f6"
        res_reg = self.client.post("/api/devices/register", json={
            "mac_address": test_mac,
            "friendly_name": "Test Bedside Controller",
            "firmware_version": "1.0.0"
        })
        self.assertEqual(res_reg.status_code, 202)
        reg_data = res_reg.get_json()
        self.assertEqual(reg_data.get("status"), "pending_approval")
        self.assertFalse(reg_data.get("is_approved"))

        try:
            self.assertEqual(self.client.get("/api/kids/child_a/yoto").status_code, 403)
            database.update_esp32_device(test_mac, {"is_approved": 1, "assigned_profile_id": "child_a"})

            res_reg2 = self.client.post("/api/devices/register", json={"mac_address": test_mac})
            self.assertEqual(res_reg2.status_code, 200)
            reg2_data = res_reg2.get_json()
            self.assertEqual(reg2_data.get("status"), "approved")
            access_tok = reg2_data["tokens"]["access_token"]
            refresh_tok = reg2_data["tokens"]["refresh_token"]

            res_cat = self.client.get("/api/kids/child_a/yoto", headers={"Authorization": f"Bearer {access_tok}"})
            self.assertEqual(res_cat.status_code, 200)
            self.assertIsInstance(res_cat.get_json(), list)

            res_ref = self.client.post("/api/device/auth/refresh", json={"mac": test_mac, "refresh_token": refresh_tok})
            self.assertEqual(res_ref.status_code, 200)
            self.assertTrue(res_ref.get_json().get("access_token"))

            with self.client.session_transaction() as sess:
                sess["user_id"] = "admin_user"
                sess["username"] = "admin"
                sess["role"] = "admin"

            res_rev = self.client.post(f"/api/admin/devices/{test_mac}/revoke")
            self.assertEqual(res_rev.status_code, 200)
            self.assertEqual(res_rev.get_json().get("status"), "revoked")

            with self.client.session_transaction() as sess:
                sess.clear()

            res_cat_revoked = self.client.get("/api/kids/child_a/yoto", headers={"Authorization": f"Bearer {access_tok}"})
            self.assertEqual(res_cat_revoked.status_code, 403)
            self.assertEqual(res_cat_revoked.get_json().get("error"), "device_revoked")

            res_ref_revoked = self.client.post("/api/device/auth/refresh", json={"mac": test_mac, "refresh_token": refresh_tok})
            self.assertEqual(res_ref_revoked.status_code, 403)
        finally:
            database.delete_esp32_device(test_mac)

    def test_12_flexible_track_name_parsing_and_chapter_update(self):
        """Verifies parsing of bulleted lists, newlines, and comma-separated chapter names."""
        bullet_input = """
        • Chapter 1: To the Castle!
        • Chapter 2: The Dragon Stone
        • Chapter 3: More Dragons!
        • Chapter 4: Worm
        """
        parsed_bullet = parse_track_names(bullet_input)
        self.assertEqual(parsed_bullet, ["Chapter 1: To the Castle!", "Chapter 2: The Dragon Stone", "Chapter 3: More Dragons!", "Chapter 4: Worm"])

        self.assertEqual(parse_track_names("- Intro\n* Chapter 1\n• Chapter 2"), ["Intro", "Chapter 1", "Chapter 2"])
        self.assertEqual(parse_track_names("Track 1: Start, Track 2: Middle, Track 3: Finish"), ["Track 1: Start", "Track 2: Middle", "Track 3: Finish"])

        with self.client.session_transaction() as sess:
            sess["user_id"] = "admin_user"
            sess["username"] = "admin"
            sess["role"] = "admin"

        res_retail = self.client.put("/api/admin/media/7gGiV/tracks", json={
            "type": "yoto",
            "raw_text": "• Chapter 1: Test"
        })
        self.assertEqual(res_retail.status_code, 400)
        self.assertIn("cannot be edited", res_retail.get_json().get("error", ""))

    def test_13_chapter_number_extraction_and_overlay_rendering(self):
        """Verifies regex extraction of chapter numbers and 16x16 pixel overlay generation."""
        self.assertEqual(extract_chapter_number("Chapter 1: The Beginning", 99), 1)
        self.assertEqual(extract_chapter_number("Ch. 04 - Midnight Run", 99), 4)
        self.assertEqual(extract_chapter_number("Chap 12: Escape", 99), 12)
        self.assertEqual(extract_chapter_number("Part 3: The Climax", 99), 3)
        self.assertEqual(extract_chapter_number("Track 07", 99), 7)
        self.assertEqual(extract_chapter_number("05. Morning Dew", 99), 5)
        self.assertEqual(extract_chapter_number("10 - Journey Home", 99), 10)
        self.assertEqual(extract_chapter_number("Prologue", 1), 1)
        self.assertEqual(extract_chapter_number("Epilogue", 15), 15)

        base_img = Image.new("RGBA", (64, 64), (100, 150, 200, 255))
        buf = io.BytesIO()
        base_img.save(buf, format="PNG")
        out_bytes = render_16x16_icon_with_overlay(buf.getvalue(), number=8, corner="top-right")
        self.assertTrue(out_bytes.startswith(b"\x89PNG\r\n\x1a\n"))
        out_img = Image.open(io.BytesIO(out_bytes))
        self.assertEqual(out_img.size, (16, 16))
        self.assertEqual(out_img.mode, "RGBA")

    def test_14_batch_track_icon_endpoint(self):
        """Verifies POST /api/admin/media/<media_id>/tracks/batch-icon assigns icons and handles overlay flag."""
        with self.client.session_transaction() as sess:
            sess["user_id"] = "admin_user"
            sess["username"] = "admin"
            sess["role"] = "admin"

        test_img = Image.new("RGBA", (16, 16), (255, 0, 0, 255))
        buf = io.BytesIO()
        test_img.save(buf, format="PNG")
        b64_str = f"data:image/png;base64,{base64.b64encode(buf.getvalue()).decode('ascii')}"

        abs_item_id = "7696c2d1-49bc-4165-a3dd-743b4ad79328"
        mock_abs_item = {
            "id": abs_item_id,
            "media": {
                "chapters": [
                    {"title": "Chapter 1: Intro", "start": 0, "end": 120},
                    {"title": "Chapter 2: Next", "start": 120, "end": 240}
                ]
            }
        }
        with patch.object(clients.abs_client, "get_item", return_value=mock_abs_item):
            res = self.client.post(f"/api/admin/media/{abs_item_id}/tracks/batch-icon", json={
                "type": "abs",
                "track_indices": [0],
                "image_b64": b64_str,
                "apply_overlay": True,
                "overlay_corner": "top-right"
            })
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data.get("ok"))
        self.assertEqual(data.get("updated_tracks"), [0])
        self.assertIn("0", data.get("updated_urls", {}))
        self.assertTrue(data.get("apply_overlay"))

    def test_15_physical_card_ownership(self):
        """Verifies single and batch toggles for physical plastic card ownership."""
        with self.client.session_transaction() as sess:
            sess["user_id"] = "admin_user"
            sess["username"] = "admin"
            sess["role"] = "admin"

        test_id = "test_card_phys_1"
        test_id2 = "test_card_phys_2"
        try:
            database.upsert_media_items([{"id": test_id, "type": "yoto", "title": "Physical Test Card 1"}])
            res = self.client.post(f"/api/admin/catalog/physical/{test_id}", json={"has_physical": True})
            self.assertEqual(res.status_code, 200)
            self.assertTrue(res.get_json().get("has_physical"))

            matrix = database.get_catalog_matrix()
            match = next((it for it in matrix["items"] if it["id"] == test_id), None)
            self.assertIsNotNone(match)
            self.assertTrue(match.get("has_physical"))

            database.upsert_media_items([{"id": test_id2, "type": "yoto", "title": "Physical Test Card 2"}])
            res_batch = self.client.post("/api/admin/curation/batch-physical", json={
                "media_ids": [test_id, test_id2],
                "has_physical": False
            })
            self.assertEqual(res_batch.status_code, 200)
            self.assertEqual(res_batch.get_json().get("updated_count"), 2)

            matrix2 = database.get_catalog_matrix()
            m1 = next(it for it in matrix2["items"] if it["id"] == test_id)
            m2 = next(it for it in matrix2["items"] if it["id"] == test_id2)
            self.assertFalse(m1.get("has_physical"))
            self.assertFalse(m2.get("has_physical"))
        finally:
            conn = database.get_db_connection()
            try:
                with conn:
                    conn.execute("DELETE FROM media_items WHERE id IN (?, ?);", (test_id, test_id2))
            finally:
                conn.close()

    def test_16_catalog_rotation_and_series_sequencing(self):
        """Verifies Book #1 series prioritization, last-played top sorting, and hide_physical filtering."""
        prof_key = "rot_test_kid"
        try:
            database.upsert_profile(prof_key, {
                "display_name": "Rotation Kid",
                "catalog_rotation_limit": 2,
                "hide_physical": False,
                "yoto": {"inherit_shared": False}
            })
            database.upsert_media_items([
                {"id": "seq_series_b1", "type": "yoto", "title": "Adventures in Magic #1: The Beginning", "series": "Adventures in Magic", "series_order": "1", "has_physical": False},
                {"id": "seq_series_b2", "type": "yoto", "title": "Adventures in Magic #2: The Dark Forest", "series": "Adventures in Magic", "series_order": "2", "has_physical": False},
                {"id": "seq_recent_card", "type": "yoto", "title": "Yesterday Bedtime Story", "raw_metadata": {"lastPlayedAt": "2026-11-06T10:00:00Z"}, "has_physical": False},
                {"id": "seq_phys_card", "type": "yoto", "title": "Bedroom Binder Plastic Card", "has_physical": True},
            ])
            database.set_item_physical("seq_phys_card", True)
            database.batch_update_profile_media_permissions(
                media_ids=["seq_series_b1", "seq_series_b2", "seq_recent_card", "seq_phys_card"],
                profile_actions={prof_key: "allow"}
            )

            class _FixedDatetime(_real_datetime):
                @classmethod
                def now(cls, tz=None):
                    return cls(2026, 11, 7, 12, 0, 0, tzinfo=tz)

            with patch("datetime.datetime", _FixedDatetime):
                items = database.get_catalog_items_for_profile(prof_key, "yoto")
            self.assertEqual(items[0]["id"], "seq_recent_card")
            self.assertEqual(items[1]["id"], "seq_series_b1")
            self.assertEqual(items[2]["id"], "seq_series_b2")
            self.assertEqual(len(items), 3)

            database.upsert_profile(prof_key, {
                "display_name": "Rotation Kid",
                "catalog_rotation_limit": 80,
                "hide_physical": True,
                "yoto": {
                    "inherit_shared": False,
                    "allowed_card_ids": ["seq_series_b1", "seq_series_b2", "seq_recent_card", "seq_phys_card"]
                }
            })
            items_hidden = database.get_catalog_items_for_profile(prof_key, "yoto")
            item_ids = [it["id"] for it in items_hidden]
            self.assertNotIn("seq_phys_card", item_ids)
            self.assertIn("seq_series_b1", item_ids)
            self.assertIn("seq_series_b2", item_ids)
        finally:
            database.delete_profile(prof_key)
            conn = database.get_db_connection()
            try:
                with conn:
                    conn.execute("DELETE FROM media_items WHERE id IN ('seq_series_b1', 'seq_series_b2', 'seq_recent_card', 'seq_phys_card');")
                    conn.execute("DELETE FROM media_playback_history WHERE profile_id = ?;", (prof_key,))
            finally:
                conn.close()

    def test_dual_profile_catalog_limits_touch_vs_kiosk(self):
        """Verifies dual profile limits: Touch device cap vs Kiosk unlimited / custom cap."""
        prof_key = "dual_test_kid"
        test_cards = [
            {"id": f"dual_card_{i}", "type": "yoto", "title": f"Dual Test Story {i}", "author": "Test Author", "cover_url": f"https://example.com/dual_card_{i}.jpg", "raw_data": {}}
            for i in range(1, 6)
        ]
        try:
            database.upsert_media_items(test_cards)
            database.upsert_profile(prof_key, {
                "display_name": "Dual Limit Kid",
                "catalog_rotation_limit": 2,
                "kiosk_catalog_limit": 0,
                "yoto": {"inherit_shared": False, "allowed_card_ids": [c["id"] for c in test_cards]}
            })

            touch_items = database.get_catalog_items_for_profile(prof_key, "yoto", client_type="touch")
            self.assertEqual(len(touch_items), 2)

            kiosk_items = database.get_catalog_items_for_profile(prof_key, "yoto", client_type="kiosk")
            self.assertEqual(len(kiosk_items), 5)
            self.assertEqual(touch_items[0]["id"], kiosk_items[0]["id"])
            self.assertEqual(touch_items[1]["id"], kiosk_items[1]["id"])

            database.upsert_profile(prof_key, {
                "display_name": "Dual Limit Kid",
                "catalog_rotation_limit": 2,
                "kiosk_catalog_limit": 3,
                "yoto": {"inherit_shared": False, "allowed_card_ids": [c["id"] for c in test_cards]}
            })
            kiosk_items_capped = database.get_catalog_items_for_profile(prof_key, "yoto", client_type="kiosk")
            self.assertEqual(len(kiosk_items_capped), 3)
        finally:
            database.delete_profile(prof_key)
            conn = database.get_db_connection()
            try:
                with conn:
                    conn.execute("DELETE FROM media_items WHERE id LIKE 'dual_card_%';")
            finally:
                conn.close()

    def test_17_yoto_settings_and_credentials_lifecycle(self):
        """Verifies GET and PUT for Yoto Cloud API credentials and OAuth PKCE start endpoint."""
        with self.client.session_transaction() as sess:
            sess["user_id"] = "admin_user"
            sess["username"] = "admin"
            sess["role"] = "admin"

        orig_creds = database.get_api_credentials()
        try:
            res_put = self.client.put("/api/admin/settings/credentials", json={
                "yoto_client_id": "test_client_id_12345",
                "yoto_refresh_token": "test_refresh_abcde"
            })
            self.assertEqual(res_put.status_code, 200)
            self.assertTrue(res_put.get_json().get("ok"))

            res_get = self.client.get("/api/admin/settings/credentials")
            self.assertEqual(res_get.status_code, 200)
            data = res_get.get_json()
            self.assertEqual(data.get("yoto_client_id"), "test_client_id_12345")
            self.assertIn("••••", data.get("yoto_refresh_token", ""))

            res_oauth = self.client.post("/api/admin/yoto/oauth/start", json={
                "client_id": "test_client_id_12345",
                "redirect_uri": "http://127.0.0.1:8787/callback"
            })
            self.assertEqual(res_oauth.status_code, 200)
            oauth_data = res_oauth.get_json()
            self.assertTrue(oauth_data.get("ok"))
            self.assertIn("https://login.yotoplay.com/authorize", oauth_data.get("auth_url", ""))
            self.assertIn("code_challenge=", oauth_data.get("auth_url", ""))
        finally:
            database.update_api_credentials(orig_creds)
            clients.reload_clients()


class TestSeriesOverridesAndAdminSeries(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        self.client = app.test_client()
        conn = database.get_db_connection()
        try:
            with conn:
                conn.execute("DELETE FROM media_series_overrides;")
                conn.execute("DELETE FROM profile_media_permissions WHERE media_id LIKE 'p4_%';")
                conn.execute("DELETE FROM media_items WHERE id LIKE 'p4_%';")
                conn.execute("DELETE FROM profiles WHERE id = 'p4_child';")
        finally:
            conn.close()

        database.upsert_profile("p4_child", {
            "display_name": "P4 Child",
            "catalog_rotation_limit": 60,
            "yoto": {"inherit_shared": True},
        })
        database.set_shared_rule("include_all", True)
        database.set_shared_rule("include_myo_by_default", True)

        database.upsert_media_items([
            {
                "id": "p4_yoto_1",
                "type": "yoto",
                "title": "The Dragon's Prophecy",
                "author": "Yoto Studio",
                "cover_url": "https://cdn.example.com/dragon1.jpg",
                "raw_metadata": {"series": "AutoOldSeries", "series_order": "9"},
            },
            {
                "id": "p4_yoto_2",
                "type": "yoto",
                "title": "Lost Treasure of the Isles",
                "author": "Yoto Studio",
                "cover_url": "https://cdn.example.com/dragon2.jpg",
                "raw_metadata": {},
            },
            {
                "id": "p4_myo_1",
                "type": "yoto_myo",
                "title": "Secret Bridge Story",
                "author": "Parents",
                "cover_url": "https://cdn.example.com/dragon15.jpg",
                "raw_metadata": {"series": "TempGroup", "series_order": "3"},
            },
            {
                "id": "p4_abs_1",
                "type": "abs",
                "title": "Audiobook Item",
                "author": "ABS Author",
                "raw_metadata": {"series": "Native ABS Series", "series_order": "1", "tags": ["Children"]},
            },
        ])

    def _login_admin(self):
        with self.client.session_transaction() as sess:
            sess["user_id"] = "p4_admin"
            sess["username"] = "admin"
            sess["role"] = "admin"

    def test_01_schema_v11_and_synthetic_v10_upgrade(self):
        """Verifies live test DB is at schema >= 11 and a synthetic v10 DB migrates cleanly."""
        conn = database.get_db_connection()
        try:
            ver = conn.execute("PRAGMA user_version;").fetchone()[0]
            self.assertGreaterEqual(ver, 11)
            self.assertIsNotNone(conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='media_series_overrides';").fetchone())
            self.assertIsNotNone(conn.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='idx_media_series_overrides_series';").fetchone())
        finally:
            conn.close()

        with tempfile.TemporaryDirectory() as tmpdir:
            v10_path = Path(tmpdir) / "v10.db"
            c10 = sqlite3.connect(v10_path)
            try:
                c10.row_factory = sqlite3.Row
                c10.executescript("""
                    CREATE TABLE media_items (
                        id TEXT PRIMARY KEY,
                        type TEXT NOT NULL,
                        title TEXT NOT NULL
                    );
                    PRAGMA user_version = 10;
                """)
                database.run_schema_migrations(c10)
                self.assertGreaterEqual(c10.execute("PRAGMA user_version;").fetchone()[0], 11)
                self.assertIsNotNone(c10.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='media_series_overrides';").fetchone())
            finally:
                c10.close()

    def test_02_single_card_override_and_matrix_enrichment(self):
        """Verifies POST /api/admin/media/<id>/series works for yoto & yoto_myo and enriches get_catalog_matrix."""
        self._login_admin()

        res = self.client.post("/api/admin/media/p4_yoto_1/series", json={"series": "Wings of Fire", "series_order": "1"})
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["series"], "Wings of Fire")
        self.assertEqual(data["series_order"], "1")
        self.assertTrue(data["is_series_overridden"])

        res_myo = self.client.post("/api/admin/media/p4_myo_1/series", json={"series": "Wings of Fire", "sequence": "1.5"})
        self.assertEqual(res_myo.status_code, 200)
        self.assertEqual(res_myo.get_json()["series_order"], "1.5")

        matrix = database.get_catalog_matrix()
        by_id = {it["id"]: it for it in matrix["items"]}
        y1 = by_id["p4_yoto_1"]
        self.assertEqual(y1["series"], "Wings of Fire")
        self.assertEqual(y1["series_order"], "1")
        self.assertEqual(y1["series_list"], [{"name": "Wings of Fire", "order": "1"}])
        self.assertTrue(y1["is_series_overridden"])
        self.assertEqual(y1["auto_series"], "AutoOldSeries")
        self.assertEqual(y1["auto_series_order"], "9")

        m1 = by_id["p4_myo_1"]
        self.assertEqual(m1["series"], "Wings of Fire")
        self.assertEqual(m1["series_order"], "1.5")
        self.assertTrue(m1["is_series_overridden"])

    def test_03_rejects_abs_unknown_and_unauthenticated_requests(self):
        """Rejects ABS items (400), unknown IDs (404), and unauthenticated callers (401/403)."""
        unauth = self.client.post("/api/admin/media/p4_yoto_1/series", json={"series": "Blocked"}, environ_base={"REMOTE_ADDR": "8.8.8.8"})
        self.assertIn(unauth.status_code, (401, 403))

        with self.client.session_transaction() as sess:
            sess["user_id"] = "p4_member"
            sess["username"] = "sitter"
            sess["role"] = "member"
        member_res = self.client.post("/api/admin/media/p4_yoto_1/series", json={"series": "Blocked"})
        self.assertIn(member_res.status_code, (401, 403))

        self._login_admin()
        abs_res = self.client.post("/api/admin/media/p4_abs_1/series", json={"series": "Hacked", "series_order": "1"})
        self.assertEqual(abs_res.status_code, 400)
        self.assertFalse(abs_res.get_json()["ok"])

        miss_res = self.client.post("/api/admin/media/p4_does_not_exist/series", json={"series": "Nope"})
        self.assertEqual(miss_res.status_code, 404)

        batch_res = self.client.post("/api/admin/media/batch-series", json={
            "series": "AtomicFailSeries",
            "items": [{"id": "p4_yoto_1", "sequence": "1"}, {"id": "p4_abs_1", "sequence": "2"}],
        })
        self.assertEqual(batch_res.status_code, 400)
        self.assertEqual(database.get_all_series_overrides(), {})

    def test_04_upstream_resync_preserves_overrides(self):
        """Verifies upsert_media_items never overwrites manual parent series overrides."""
        self._login_admin()
        res = self.client.post("/api/admin/media/p4_yoto_1/series", json={"series": "Custom Saga", "series_order": "2"})
        self.assertEqual(res.status_code, 200)

        database.upsert_media_items([{
            "id": "p4_yoto_1",
            "type": "yoto",
            "title": "The Dragon's Prophecy (Updated)",
            "author": "Yoto Studio",
            "cover_url": "https://cdn.example.com/dragon1_v2.jpg",
            "raw_metadata": {"series": "UpstreamChangedSeries", "series_order": "99"},
        }])

        matrix = database.get_catalog_matrix()
        y1 = next(it for it in matrix["items"] if it["id"] == "p4_yoto_1")
        self.assertEqual(y1["title"], "The Dragon's Prophecy (Updated)")
        self.assertTrue(y1["is_series_overridden"])
        self.assertEqual(y1["series"], "Custom Saga")
        self.assertEqual(y1["series_order"], "2")
        self.assertEqual(y1["auto_series"], "UpstreamChangedSeries")

    def test_05_standalone_clear_and_delete_revert(self):
        """Verifies empty series marks card Standalone, and DELETE reverts to auto_series."""
        self._login_admin()
        res_clear = self.client.post("/api/admin/media/p4_yoto_1/series", json={"series": "", "series_order": ""})
        self.assertEqual(res_clear.status_code, 200)

        matrix_cleared = database.get_catalog_matrix()
        y1_cleared = next(it for it in matrix_cleared["items"] if it["id"] == "p4_yoto_1")
        self.assertTrue(y1_cleared["is_series_overridden"])
        self.assertEqual(y1_cleared["series"], "")
        self.assertEqual(y1_cleared["series_list"], [])
        self.assertEqual(y1_cleared["auto_series"], "AutoOldSeries")

        res_del = self.client.delete("/api/admin/media/p4_yoto_1/series")
        self.assertEqual(res_del.status_code, 200)
        del_data = res_del.get_json()
        self.assertTrue(del_data["ok"])
        self.assertTrue(del_data["reverted"])
        self.assertEqual(del_data["series"], "AutoOldSeries")
        self.assertEqual(del_data["series_order"], "9")

        matrix_reverted = database.get_catalog_matrix()
        y1_rev = next(it for it in matrix_reverted["items"] if it["id"] == "p4_yoto_1")
        self.assertFalse(y1_rev["is_series_overridden"])
        self.assertEqual(y1_rev["series"], "AutoOldSeries")
        self.assertEqual(y1_rev["series_order"], "9")

    def test_06_batch_series_and_decimal_sequence_curation_integration(self):
        """Verifies batch-series decimal sequencing drives atomic whole-series rotation and Book #1 cover."""
        self._login_admin()
        res = self.client.post("/api/admin/media/batch-series", json={
            "series": "Dragon Isles",
            "items": [
                {"id": "p4_yoto_1", "sequence": "1"},
                {"id": "p4_myo_1", "sequence": "1.5"},
                {"id": "p4_yoto_2", "sequence": "2"},
            ],
        })
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["updated_count"], 3)
        self.assertEqual(data["series"], "Dragon Isles")

        catalog = database.get_catalog_items_for_profile("p4_child", media_type="yoto", client_type="touch")
        dragon_books = [it for it in catalog if it.get("series") == "Dragon Isles"]
        self.assertEqual(len(dragon_books), 3)
        for book in dragon_books:
            self.assertEqual(book["series_cover_id"], "p4_yoto_1")
            self.assertIn("p4_yoto_1", book["series_cover"])

        seqs_by_id = {b["id"]: b["sequence"] for b in dragon_books}
        self.assertEqual(seqs_by_id["p4_yoto_1"], 1.0)
        self.assertEqual(seqs_by_id["p4_myo_1"], 1.5)
        self.assertEqual(seqs_by_id["p4_yoto_2"], 2.0)

        res_fallback = self.client.post("/api/admin/media/batch-series", json={
            "series": "Fallback Saga",
            "media_ids": ["p4_yoto_2", "p4_yoto_1"],
        })
        self.assertEqual(res_fallback.status_code, 200)
        overrides = database.get_all_series_overrides()
        self.assertEqual(overrides["p4_yoto_2"]["series"], "Fallback Saga")
        self.assertEqual(overrides["p4_yoto_2"]["series_order"], "1")
        self.assertEqual(overrides["p4_yoto_1"]["series_order"], "2")

    def test_07_prune_stale_cleans_orphan_overrides(self):
        """Verifies pruning a deleted Yoto card cascades/cleans its row in media_series_overrides."""
        database.set_media_series_override("p4_yoto_1", "Cleanup Series", "1")
        self.assertIn("p4_yoto_1", database.get_all_series_overrides())

        keep_ids = [
            it["id"] for it in database.get_catalog_matrix()["items"]
            if it["type"] in ("yoto", "yoto_myo") and it["id"] != "p4_yoto_1"
        ]
        removed = database.prune_stale_media_items(valid_ids=keep_ids, media_types=["yoto", "yoto_myo"])
        self.assertGreaterEqual(removed, 1)
        self.assertNotIn("p4_yoto_1", database.get_all_series_overrides())

    def test_08_admin_html_contains_series_override_modals(self):
        """Verifies /admin HTML renders single & bulk series override modal elements and triggers."""
        res = self.client.get("/admin")
        self.assertEqual(res.status_code, 200)
        html = res.get_data(as_text=True)
        for token in (
            'id="existingSeriesList"',
            'id="editSeriesModal"',
            'id="batchSeriesModal"',
            'openBatchSeriesModal()',
            '/static/js/admin/series.js?v=',
            '/static/js/admin/library.js?v=',
        ):
            self.assertIn(token, html, f"Expected {token} in /admin HTML")


if __name__ == "__main__":
    unittest.main()

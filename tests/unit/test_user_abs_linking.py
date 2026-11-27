"""Unit tests for user & profile Audiobookshelf account linking, key minting, and revocation."""
import base64
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from tests.harness import IsolatedTestCase
import app as app_module
import clients
from clients.abs_client import ABSClient
from core.bridge.manager import get_bridge_manager
from core.bridge.playback import PlaybackManager
import database


class TestUserABSAccountLinking(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        self.client = app_module.app.test_client()
        database.init_db()
        if not database.get_user_by_username("abs_adult"):
            database.create_admin_user("abs_adult", "StrongPass123!", "Adult Listener", role="member")
        if not database.get_profile_by_id("kid_abs_test"):
            database.upsert_profile("kid_abs_test", {
                "display_name": "Kiddo",
                "default_playback_mode": "streaming",
                "primary_card_id": "kid_card_original",
            })
        self.adult_user = database.get_user_by_username("abs_adult")
        self.client.post("/api/admin/auth/login", json={"username": "abs_adult", "password": "StrongPass123!"})

    def test_01_schema_v15_migration_from_v14(self):
        with tempfile.TemporaryDirectory() as tmpd:
            db_path = Path(tmpd) / "synth_v14.db"
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            conn.execute("""
                CREATE TABLE admin_users (
                    id TEXT PRIMARY KEY,
                    username TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    salt TEXT NOT NULL,
                    display_name TEXT DEFAULT '',
                    role TEXT DEFAULT 'admin',
                    sso_provider TEXT DEFAULT '',
                    is_super_admin BOOLEAN DEFAULT 0,
                    email TEXT DEFAULT '',
                    sso_subject TEXT DEFAULT '',
                    last_login DATETIME,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                );
            """)
            conn.execute("""
                CREATE TABLE bridge_slots (
                    slot_id TEXT PRIMARY KEY,
                    abs_item_id TEXT NOT NULL,
                    yoto_card_id TEXT,
                    owner_profile_id TEXT,
                    title TEXT NOT NULL,
                    nickname TEXT DEFAULT ''
                );
            """)
            conn.execute("PRAGMA user_version = 14;")
            conn.commit()
            try:
                database.run_schema_migrations(conn)
                self.assertGreaterEqual(conn.execute("PRAGMA user_version;").fetchone()[0], 15)
                au_cols = {r[1] for r in conn.execute("PRAGMA table_info(admin_users);").fetchall()}
                bs_cols = {r[1] for r in conn.execute("PRAGMA table_info(bridge_slots);").fetchall()}
            finally:
                conn.close()
            self.assertTrue({"abs_api_key", "abs_username", "abs_user_id"}.issubset(au_cols))
            self.assertIn("owner_user_id", bs_cols)

    def test_02_verify_abs_token_and_lan_guest_blocked(self):
        fake_resp = MagicMock(status_code=200)
        fake_resp.json.return_value = {
            "id": "abs_u_99",
            "username": "mom_abs",
            "type": "admin",
            "isActive": True,
            "librariesAccessible": ["lib1", "lib2"],
        }
        old_abs = clients.abs_client
        clients.abs_client = ABSClient("http://abs.local:13378", "master_key_xyz")
        try:
            with patch("clients.abs_client.requests.get", return_value=fake_resp):
                res = self.client.post("/api/auth/abs/verify", json={"abs_api_key": "adult_personal_tok_123"})
                self.assertEqual(res.status_code, 200)
                data = res.get_json()
                self.assertTrue(data["ok"])
                self.assertEqual(data["abs_username"], "mom_abs")
                self.assertEqual(data["abs_user_id"], "abs_u_99")
                self.assertEqual(data["libraries_count"], 2)

            bad_resp = MagicMock(status_code=401)
            with patch("clients.abs_client.requests.get", return_value=bad_resp):
                res_bad = self.client.post("/api/auth/abs/verify", json={"abs_api_key": "wrong_tok"})
                self.assertEqual(res_bad.status_code, 401)
        finally:
            clients.abs_client = old_abs

        anon = app_module.app.test_client()
        res_guest = anon.post(
            "/api/auth/abs/verify",
            json={"abs_api_key": "tok"},
            environ_base={"REMOTE_ADDR": "192.168.1.55"},
        )
        self.assertEqual(res_guest.status_code, 403)

    def test_03_put_auth_me_links_hides_raw_key_and_unlinks(self):
        fake_resp = MagicMock(status_code=200)
        fake_resp.json.return_value = {
            "id": "abs_u_42",
            "username": "dad_abs",
            "type": "user",
            "isActive": True,
            "librariesAccessible": ["lib_adult"],
        }
        abs_inst = ABSClient("http://abs.local:13378", "master_global_key")
        old_abs = clients.abs_client
        clients.abs_client = abs_inst
        try:
            with patch("clients.abs_client.requests.get", return_value=fake_resp):
                res = self.client.put("/api/auth/me", json={
                    "display_name": "Adult Listener",
                    "abs_api_key": "secret_dad_abs_key_987",
                })
                self.assertEqual(res.status_code, 200)
                body = res.get_json()
                self.assertTrue(body["ok"])
                user_obj = body["user"]
                self.assertTrue(user_obj["has_abs_api_key"])
                self.assertEqual(user_obj["abs_username"], "dad_abs")
                self.assertNotIn("abs_api_key", user_obj)

            me_res = self.client.get("/api/auth/me")
            me_user = me_res.get_json()["user"]
            self.assertTrue(me_user["has_abs_api_key"])
            self.assertEqual(me_user["abs_username"], "dad_abs")
            self.assertNotIn("abs_api_key", me_user)

            resolved = abs_inst.resolve_token_for_profile(f"user:{self.adult_user['id']}")
            self.assertEqual(resolved, "secret_dad_abs_key_987")

            clr_res = self.client.put("/api/auth/me", json={"clear_abs_account": True})
            self.assertEqual(clr_res.status_code, 200)
            clr_user = clr_res.get_json()["user"]
            self.assertFalse(clr_user["has_abs_api_key"])
            self.assertEqual(clr_user["abs_username"], "")
            self.assertEqual(abs_inst.resolve_token_for_profile(f"user:{self.adult_user['id']}"), "master_global_key")
        finally:
            clients.abs_client = old_abs

    def test_04_assign_use_user_abs_and_playback_override(self):
        database.update_user_abs_account(
            self.adult_user["id"],
            abs_api_key="adult_stream_tok_abc",
            abs_username="mom_abs",
            abs_user_id="abs_u_1",
        )
        fake_abs = MagicMock()
        fake_abs.base_url = "http://abs.local:13378"
        fake_abs.api_key = "master_key"
        fake_abs.get_item.return_value = {
            "id": "adult_book_1",
            "media": {
                "duration": 600.0,
                "metadata": {"title": "Adult Sci-Fi", "authorName": "A. Author"},
                "chapters": [{"id": 0, "start": 0.0, "end": 600.0, "title": "Part 1"}],
                "audioFiles": [],
            },
        }
        fake_abs.start_playback_session.return_value = {"id": "sess_adult_1"}
        fake_yoto = MagicMock()
        fake_yoto.save_stream_playlist.return_value = "yoto_user_stream_1"

        old_abs, old_yoto = clients.abs_client, clients.yoto_client
        clients.abs_client, clients.yoto_client = fake_abs, fake_yoto
        try:
            res = self.client.post("/api/cards/assign", json={
                "use_user_abs": True,
                "abs_item_id": "adult_book_1",
                "mode": "streaming",
                "title": "Adult Sci-Fi",
            })
            self.assertEqual(res.status_code, 200)
            slot = res.get_json()
            self.assertEqual(slot["slot_id"], f"user_{self.adult_user['id']}")
            self.assertEqual(slot["kind"], "user_primary")
            self.assertIsNone(slot.get("owner_profile_id"))
            self.assertEqual(slot.get("owner_user_id"), self.adult_user["id"])
            self.assertEqual(slot.get("yoto_card_id"), "yoto_user_stream_1")

            kid_prof = database.get_profile_by_id("kid_abs_test")
            self.assertEqual(kid_prof.get("primary_card_id"), "kid_card_original")

            fake_abs.get_item.assert_called_with("adult_book_1", token="adult_stream_tok_abc")

            pb = PlaybackManager()
            pb.expect("child-a-mini", "yoto_user_stream_1", abs_token_override="adult_stream_tok_abc", owner_override=f"user:{self.adult_user['id']}")
            db_slot = get_bridge_manager().get_slot(slot["slot_id"])
            trk = db_slot["tracks"][0]
            sess = pb.open_stream(db_slot, trk, device_id="child-a-mini")
            self.assertEqual(sess.abs_token, "adult_stream_tok_abc")
            self.assertEqual(sess.owner_profile_id, f"user:{self.adult_user['id']}")
        finally:
            clients.abs_client, clients.yoto_client = old_abs, old_yoto

    def test_05_registered_guest_can_link_and_lan_guest_uses_master_abs(self):
        """Verifies registered guest accounts can link personal ABS keys, while unregistered LAN guests stay on master ABS key."""
        guest_u = database.create_admin_user(
            "test_reg_guest_abs",
            "GuestPass123!",
            display_name="Registered Guest",
            role="guest",
        )
        old_abs, old_yoto = clients.abs_client, clients.yoto_client
        try:
            abs_cli = ABSClient.__new__(ABSClient)
            abs_cli.base_url = "http://127.0.0.1:13378"
            abs_cli.api_key = "master_api_key_only"
            abs_cli.verify_user_token = MagicMock(
                return_value={
                    "id": "abs_g1",
                    "username": "guest_abs",
                    "type": "user",
                    "libraries_accessible": ["lib1"],
                }
            )
            abs_cli.get_item = MagicMock(return_value=None)
            fake_yoto = MagicMock()
            fake_yoto.save_stream_playlist.return_value = "yoto_guest_stream_1"
            clients.abs_client = abs_cli
            clients.yoto_client = fake_yoto

            client = app_module.app.test_client()
            with client.session_transaction() as sess:
                sess["user_id"] = guest_u["id"]
                sess["username"] = guest_u["username"]
                sess["role"] = "guest"

            res_verify = client.post(
                "/api/auth/abs/verify",
                json={"abs_api_key": "reg_guest_abs_key"},
            )
            self.assertEqual(res_verify.status_code, 200)
            data_verify = json.loads(res_verify.data)
            self.assertTrue(data_verify.get("ok"))
            self.assertEqual(data_verify.get("abs_username"), "guest_abs")

            res_put = client.put(
                "/api/auth/me",
                json={"abs_api_key": "reg_guest_abs_key"},
            )
            self.assertEqual(res_put.status_code, 200)
            data_put = json.loads(res_put.data)
            user_out = data_put.get("user") or {}
            self.assertTrue(user_out.get("has_abs_api_key"))
            self.assertEqual(user_out.get("abs_username"), "guest_abs")
            self.assertNotIn("abs_api_key", user_out)

            self.assertEqual(
                abs_cli.resolve_token_for_profile(f"user:{guest_u['id']}"),
                "reg_guest_abs_key",
            )

            res_clear = client.put(
                "/api/auth/me",
                json={"clear_abs_account": True},
            )
            self.assertEqual(res_clear.status_code, 200)
            self.assertEqual(
                abs_cli.resolve_token_for_profile(f"user:{guest_u['id']}"),
                "master_api_key_only",
            )

            lan_client = app_module.app.test_client()
            res_lan_verify = lan_client.post(
                "/api/auth/abs/verify",
                json={"abs_api_key": "lan_key"},
                environ_base={"REMOTE_ADDR": "192.168.1.88"},
            )
            self.assertEqual(res_lan_verify.status_code, 403)

            res_lan_put = lan_client.put(
                "/api/auth/me",
                json={"abs_api_key": "lan_key"},
                environ_base={"REMOTE_ADDR": "192.168.1.88"},
            )
            self.assertEqual(res_lan_put.status_code, 403)

            res_lan_assign = lan_client.post(
                "/api/cards/assign",
                json={"use_user_abs": True, "abs_item_id": "item_1"},
                environ_base={"REMOTE_ADDR": "192.168.1.88"},
            )
            self.assertEqual(res_lan_assign.status_code, 200)
            self.assertEqual(res_lan_assign.get_json()["slot_id"], "guest_stream")

            self.assertEqual(
                abs_cli.resolve_token_for_profile("user:lan_guest"),
                "master_api_key_only",
            )
            self.assertEqual(
                abs_cli.resolve_token_for_profile(profile_id=None, device_id="unassigned-player"),
                "master_api_key_only",
            )
        finally:
            clients.abs_client, clients.yoto_client = old_abs, old_yoto
            database.delete_user(guest_u["id"])

    def test_06_auto_match_abs_by_email_and_username_with_api_key_minting_and_revoke(self):
        """Verifies email -> username ABS auto-matching, POST /api/api-keys scoped key minting, and key revocation on unlink."""
        member_u = database.create_admin_user(
            "test_automatch_member",
            "MemberPass123!",
            display_name="Auto Match Member",
            role="member",
            email="automatch@example.com",
        )
        old_abs = clients.abs_client
        try:
            abs_cli = ABSClient("http://127.0.0.1:13378", "master_admin_api_key")
            sample_abs_users = [
                {
                    "id": "abs_uid_email_match",
                    "username": "abs_diff_uname",
                    "email": "AutoMatch@example.com",
                    "type": "user",
                    "isActive": True,
                    "librariesAccessible": ["lib_a", "lib_b"],
                    "token": "legacy_fallback_tok",
                },
                {
                    "id": "abs_uid_uname_match",
                    "username": "test_automatch_member",
                    "email": "other@example.com",
                    "type": "user",
                    "isActive": True,
                    "librariesAccessible": ["lib_a"],
                    "token": "legacy_tok_2",
                },
            ]
            abs_cli.get_all_users = MagicMock(return_value=sample_abs_users)

            m_email = abs_cli.match_abs_user(email="automatch@example.com", username="test_automatch_member")
            self.assertIsNotNone(m_email)
            self.assertEqual(m_email["id"], "abs_uid_email_match")
            self.assertEqual(m_email["matched_by"], "email")

            m_uname = abs_cli.match_abs_user(email="nomatch@example.com", username="test_automatch_member")
            self.assertIsNotNone(m_uname)
            self.assertEqual(m_uname["id"], "abs_uid_uname_match")
            self.assertEqual(m_uname["matched_by"], "username")

            clients.abs_client = abs_cli

            fake_get_keys_resp = MagicMock()
            fake_get_keys_resp.status_code = 200
            fake_get_keys_resp.json.return_value = {
                "apiKeys": [
                    {
                        "id": "old_key_id_1",
                        "name": "StoryDeck Core (abs_diff_uname)",
                        "userId": "abs_uid_email_match",
                    }
                ]
            }
            fake_del_resp = MagicMock(status_code=200)

            fake_post_key_resp = MagicMock(status_code=200)
            fake_post_key_resp.json.return_value = {
                "apiKey": {
                    "id": "minted_key_id_99",
                    "name": "StoryDeck Core (abs_diff_uname)",
                    "userId": "abs_uid_email_match",
                    "apiKey": "minted_jwt_for_abs_diff_uname",
                }
            }

            with patch("clients.abs_client.requests.get", return_value=fake_get_keys_resp), \
                 patch("clients.abs_client.requests.delete", return_value=fake_del_resp) as mock_del, \
                 patch("clients.abs_client.requests.post", return_value=fake_post_key_resp) as mock_post:
                client = app_module.app.test_client()
                with client.session_transaction() as sess:
                    sess["user_id"] = member_u["id"]
                    sess["username"] = member_u["username"]
                    sess["role"] = "member"

                res_am = client.post("/api/auth/abs/auto-match", json={})
                self.assertEqual(res_am.status_code, 200)
                am_data = json.loads(res_am.data)
                self.assertTrue(am_data.get("ok"))
                self.assertEqual(am_data.get("abs_username"), "abs_diff_uname")
                self.assertEqual(am_data.get("matched_by"), "email")
                self.assertEqual(am_data.get("key_source"), "api_key")
                self.assertNotIn("abs_api_key", am_data)
                mock_post.assert_called_once()

                self.assertEqual(
                    abs_cli.resolve_token_for_profile(f"user:{member_u['id']}"),
                    "minted_jwt_for_abs_diff_uname",
                )

                res_unlink = client.put("/api/auth/me", json={"clear_abs_account": True})
                self.assertEqual(res_unlink.status_code, 200)
                self.assertGreaterEqual(mock_del.call_count, 2)
                self.assertEqual(
                    abs_cli.resolve_token_for_profile(f"user:{member_u['id']}"),
                    "master_admin_api_key",
                )
        finally:
            clients.abs_client = old_abs
            database.delete_user(member_u["id"])

    def test_07_abs_master_key_admin_check_profile_keygen_and_self_service_toggle(self):
        """Verifies master key admin permission warning, profile ABS API key auto-generation, and self-service toggle."""
        member_u = database.create_admin_user(
            "test_selfservice_member",
            "MemberPass123!",
            display_name="Self Service Member",
            role="member",
            email="selfservice@example.com",
        )
        old_abs = clients.abs_client
        try:
            abs_cli = ABSClient("http://127.0.0.1:13378", "master_test_key")

            abs_cli.verify_user_token = MagicMock(
                return_value={"id": "u1", "username": "throwaway_bridge", "type": "user", "is_active": True}
            )
            perm_non_admin = abs_cli.check_master_key_permissions()
            self.assertTrue(perm_non_admin["connected"])
            self.assertFalse(perm_non_admin["is_admin"])
            self.assertIn("manually generate", perm_non_admin["warning"])

            abs_cli.verify_user_token = MagicMock(
                return_value={"id": "u0", "username": "parent_root", "type": "root", "is_active": True}
            )
            perm_admin = abs_cli.check_master_key_permissions()
            self.assertTrue(perm_admin["connected"])
            self.assertTrue(perm_admin["is_admin"])
            self.assertEqual(perm_admin["warning"], "")

            abs_cli.get_all_users = MagicMock(
                return_value=[
                    {
                        "id": "abs_prof_uid_1",
                        "username": "child_abs",
                        "email": "child_a@example.com",
                        "type": "user",
                        "isActive": True,
                        "token": "",
                    }
                ]
            )
            fake_get_keys = MagicMock(status_code=200)
            fake_get_keys.json.return_value = {"apiKeys": []}
            fake_post_key = MagicMock(status_code=200)
            fake_post_key.json.return_value = {
                "apiKey": {
                    "id": "prof_key_1",
                    "name": "StoryDeck Profile [child_a] (child_abs)",
                    "userId": "abs_prof_uid_1",
                    "apiKey": "minted_profile_jwt_child_a",
                }
            }
            with patch("clients.abs_client.requests.get", return_value=fake_get_keys), \
                 patch("clients.abs_client.requests.post", return_value=fake_post_key):
                minted_prof = abs_cli.provision_profile_api_key("child_a", "child_abs")
                self.assertIsNotNone(minted_prof)
                self.assertEqual(minted_prof["api_key"], "minted_profile_jwt_child_a")
                self.assertEqual(minted_prof["source"], "api_key")

            clients.abs_client = abs_cli
            database.set_system_setting("abs_allow_user_self_service_key_gen", False)
            client = app_module.app.test_client()
            with client.session_transaction() as sess:
                sess["user_id"] = member_u["id"]
                sess["username"] = member_u["username"]
                sess["role"] = "member"

            res_me = client.get("/api/auth/me")
            self.assertEqual(res_me.status_code, 200)
            self.assertFalse(json.loads(res_me.data)["user"]["abs_self_service_enabled"])

            res_am = client.post("/api/auth/abs/auto-match", json={})
            self.assertEqual(res_am.status_code, 403)

            res_ver = client.post("/api/auth/abs/verify", json={"abs_api_key": "some_key"})
            self.assertEqual(res_ver.status_code, 403)

            res_put_abs = client.put("/api/auth/me", json={"abs_api_key": "some_key"})
            self.assertEqual(res_put_abs.status_code, 403)

            res_put_dn = client.put("/api/auth/me", json={"display_name": "Updated Member Name"})
            self.assertEqual(res_put_dn.status_code, 200)
        finally:
            database.set_system_setting("abs_allow_user_self_service_key_gen", True)
            clients.abs_client = old_abs
            database.delete_user(member_u["id"])

    def test_08_unlink_and_force_recreate_delete_old_api_key_by_jwt_key_id(self):
        """Verifies both POST /api/auth/abs/unlink and force recreation delete the previous ABS API key."""
        def _make_fake_abs_jwt(key_id: str) -> str:
            hdr = base64.urlsafe_b64encode(b'{"alg":"HS256","typ":"JWT"}').decode().rstrip("=")
            body = base64.urlsafe_b64encode(
                json.dumps({"keyId": key_id, "type": "api"}).encode("utf-8")
            ).decode().rstrip("=")
            return f"{hdr}.{body}.sig"

        member_u = database.create_admin_user(
            "test_recreate_unlink",
            "MemberPass123!",
            display_name="Recreate Unlink Member",
            role="member",
            email="recreate@example.com",
        )
        old_abs = clients.abs_client
        try:
            master_jwt = _make_fake_abs_jwt("master-key-uuid-do-not-delete")
            key_1_jwt = _make_fake_abs_jwt("key-uuid-1111")
            key_2_jwt = _make_fake_abs_jwt("key-uuid-2222")

            abs_cli = ABSClient("http://127.0.0.1:13378", master_jwt)
            abs_cli.get_all_users = MagicMock(
                return_value=[
                    {
                        "id": "abs_user_rec_1",
                        "username": "recreate_abs",
                        "email": "recreate@example.com",
                        "type": "user",
                        "isActive": True,
                        "librariesAccessible": ["lib1"],
                    }
                ]
            )
            clients.abs_client = abs_cli

            fake_get_keys = MagicMock(status_code=200)
            fake_get_keys.json.return_value = {"apiKeys": []}
            fake_del = MagicMock(status_code=200)

            post_responses = [
                MagicMock(
                    status_code=200,
                    json=MagicMock(
                        return_value={
                            "apiKey": {
                                "id": "key-uuid-1111",
                                "name": "StoryDeck Core (recreate_abs)",
                                "userId": "abs_user_rec_1",
                                "apiKey": key_1_jwt,
                            }
                        }
                    ),
                ),
                MagicMock(
                    status_code=200,
                    json=MagicMock(
                        return_value={
                            "apiKey": {
                                "id": "key-uuid-2222",
                                "name": "StoryDeck Core (recreate_abs)",
                                "userId": "abs_user_rec_1",
                                "apiKey": key_2_jwt,
                            }
                        }
                    ),
                ),
            ]

            with patch("clients.abs_client.requests.get", return_value=fake_get_keys), \
                 patch("clients.abs_client.requests.delete", return_value=fake_del) as mock_del, \
                 patch("clients.abs_client.requests.post", side_effect=post_responses):
                client = app_module.app.test_client()
                with client.session_transaction() as sess:
                    sess["user_id"] = member_u["id"]
                    sess["username"] = member_u["username"]
                    sess["role"] = "member"

                r1 = client.post("/api/auth/abs/auto-match", json={})
                self.assertEqual(r1.status_code, 200)
                self.assertEqual(
                    abs_cli.resolve_token_for_profile(f"user:{member_u['id']}"),
                    key_1_jwt,
                )

                r2 = client.post("/api/auth/abs/auto-match", json={})
                self.assertEqual(r2.status_code, 200)
                self.assertEqual(
                    abs_cli.resolve_token_for_profile(f"user:{member_u['id']}"),
                    key_2_jwt,
                )
                deleted_urls_after_recreate = [c.args[0] for c in mock_del.call_args_list]
                self.assertTrue(
                    any("key-uuid-1111" in u for u in deleted_urls_after_recreate),
                    f"Expected key-uuid-1111 to be deleted on recreate, got: {deleted_urls_after_recreate}",
                )

                r3 = client.post("/api/auth/abs/unlink", json={})
                self.assertEqual(r3.status_code, 200)
                deleted_urls_after_unlink = [c.args[0] for c in mock_del.call_args_list]
                self.assertTrue(
                    any("key-uuid-2222" in u for u in deleted_urls_after_unlink),
                    f"Expected key-uuid-2222 to be deleted on unlink, got: {deleted_urls_after_unlink}",
                )
                self.assertEqual(
                    abs_cli.resolve_token_for_profile(f"user:{member_u['id']}"),
                    master_jwt,
                )

                database.update_user_abs_account(
                    member_u["id"],
                    abs_api_key=key_1_jwt,
                    abs_username="parent_root",
                    abs_user_id="abs_root_1",
                )
                abs_cli.get_all_users = MagicMock(
                    return_value=[
                        {
                            "id": "abs_root_1",
                            "username": "parent_root",
                            "email": "recreate@example.com",
                            "type": "root",
                            "isActive": True,
                            "librariesAccessible": ["lib1"],
                        }
                    ]
                )
                abs_cli.verify_user_token = MagicMock(
                    return_value={
                        "id": "abs_bridge_admin",
                        "username": "yoto-bridge",
                        "type": "admin",
                        "is_active": True,
                    }
                )
                mock_del.reset_mock()
                with patch(
                    "clients.abs_client.requests.post",
                    return_value=MagicMock(status_code=403, text="Forbidden"),
                ):
                    r4 = client.post("/api/auth/abs/auto-match", json={})
                    self.assertEqual(r4.status_code, 400)
                    r4_data = json.loads(r4.data)
                    self.assertIn("only a 'root' API key can generate API keys for a 'root' user", r4_data.get("error", ""))
                    self.assertEqual(mock_del.call_count, 0)
                    self.assertEqual(
                        abs_cli.resolve_token_for_profile(f"user:{member_u['id']}"),
                        key_1_jwt,
                    )
        finally:
            clients.abs_client = old_abs
            database.delete_user(member_u["id"])


if __name__ == "__main__":
    unittest.main()

"""Unit tests for RBAC, trusted LAN auth, Primary Super Admin guards, and user CRUD."""
import json
import sqlite3
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path

from tests.harness import IsolatedTestCase
from app import app
import database
from core.auth import is_trusted_lan_ip


class _FormPasswordParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.form_depth = 0
        self.orphan_passwords = []
        self.ids = set()

    def handle_starttag(self, tag, attrs):
        t = tag.lower()
        attr_map = {k.lower(): (v or "") for k, v in attrs}
        if attr_map.get("id"):
            self.ids.add(attr_map["id"])
        if t == "form":
            self.form_depth += 1
        elif t == "input" and attr_map.get("type", "").lower() == "password":
            if self.form_depth == 0:
                self.orphan_passwords.append(attr_map.get("id") or attr_map.get("name") or "<unnamed>")

    def handle_endtag(self, tag):
        if tag.lower() == "form" and self.form_depth > 0:
            self.form_depth -= 1


class TestRBACAndLANAuth(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        self.client = app.test_client()
        conn = database.get_db_connection()
        try:
            with conn:
                conn.execute("DELETE FROM admin_users WHERE username LIKE 'test_%';")
        finally:
            conn.close()

    def tearDown(self):
        conn = database.get_db_connection()
        try:
            with conn:
                conn.execute("DELETE FROM admin_users WHERE username LIKE 'test_%';")
        finally:
            conn.close()
        super().tearDown()

    def test_01_is_trusted_lan_ip(self):
        """Verifies RFC1918 private subnets and loopback are identified as trusted LAN."""
        self.assertTrue(is_trusted_lan_ip("127.0.0.1"))
        self.assertTrue(is_trusted_lan_ip("::1"))
        self.assertTrue(is_trusted_lan_ip("192.168.1.50"))
        self.assertTrue(is_trusted_lan_ip("192.168.0.1"))
        self.assertTrue(is_trusted_lan_ip("10.0.1.15"))
        self.assertTrue(is_trusted_lan_ip("172.16.0.1"))
        self.assertTrue(is_trusted_lan_ip("172.31.255.255"))

        self.assertFalse(is_trusted_lan_ip("8.8.8.8"))
        self.assertFalse(is_trusted_lan_ip("1.1.1.1"))
        self.assertFalse(is_trusted_lan_ip("104.28.1.1"))
        self.assertFalse(is_trusted_lan_ip("172.32.0.1"))
        self.assertFalse(is_trusted_lan_ip(""))
        self.assertFalse(is_trusted_lan_ip("invalid_ip"))

    def test_02_database_user_roles_and_helpers(self):
        """Verifies admin vs member user creation, updates, and deletion rules."""
        admin_u = database.create_admin_user("test_admin_1", "Password123!", display_name="Admin One", role="admin")
        self.assertEqual(admin_u["role"], "admin")

        member_u = database.create_admin_user("test_member_1", "Password123!", display_name="Member One", role="member")
        self.assertEqual(member_u["role"], "member")

        fetched_member = database.get_user_by_id(member_u["id"])
        self.assertIsNotNone(fetched_member)
        self.assertEqual(fetched_member["username"], "test_member_1")
        self.assertEqual(fetched_member["role"], "member")

        self.assertTrue(database.update_user_role(member_u["id"], "admin"))
        updated = database.get_user_by_id(member_u["id"])
        self.assertEqual(updated["role"], "admin")

        self.assertTrue(database.update_user_password(member_u["id"], "NewPassword456!"))
        auth_res = database.authenticate_user("test_member_1", "NewPassword456!")
        self.assertIsNotNone(auth_res)
        self.assertIsNone(database.authenticate_user("test_member_1", "Password123!"))

        self.assertTrue(database.delete_user(member_u["id"]))
        self.assertIsNone(database.get_user_by_id(member_u["id"]))

    def test_03_prevent_deleting_last_admin(self):
        """Verifies that the database layer refuses to delete the last remaining administrator."""
        conn = database.get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT id FROM admin_users WHERE role = 'admin';")
            admins = cur.fetchall()
            if len(admins) == 1:
                with self.assertRaises(ValueError):
                    database.delete_user(admins[0]["id"])
        finally:
            conn.close()

    def test_04_auth_me_lan_guest_vs_remote(self):
        """Verifies /api/auth/me detects LAN guest on RFC1918 IPs and blocks remote WAN without session."""
        res_lan = self.client.get("/api/auth/me", environ_base={"REMOTE_ADDR": "192.168.1.88"})
        self.assertEqual(res_lan.status_code, 200)
        data_lan = json.loads(res_lan.data)
        self.assertTrue(data_lan["authenticated"])
        self.assertEqual(data_lan["user"]["role"], "guest")
        self.assertTrue(data_lan["user"]["is_lan_guest"])

        res_wan = self.client.get("/api/auth/me", environ_base={"REMOTE_ADDR": "8.8.8.8"})
        self.assertEqual(res_wan.status_code, 200)
        data_wan = json.loads(res_wan.data)
        self.assertFalse(data_wan["authenticated"])
        self.assertIsNone(data_wan["user"])

    def test_05_admin_users_api_rbac(self):
        """Verifies that /api/admin/users strictly enforces role == 'admin'."""
        admin_u = database.create_admin_user("test_admin_2", "AdminPass123!", display_name="Admin Two", role="admin")
        member_u = database.create_admin_user("test_member_2", "MemberPass123!", display_name="Member Two", role="member")

        res_lan = self.client.get("/api/admin/users", environ_base={"REMOTE_ADDR": "192.168.1.88"})
        self.assertEqual(res_lan.status_code, 401)

        with self.client.session_transaction() as sess:
            sess["user_id"] = member_u["id"]
            sess["username"] = member_u["username"]
            sess["role"] = member_u["role"]

        res_member = self.client.get("/api/admin/users")
        self.assertEqual(res_member.status_code, 403)

        with self.client.session_transaction() as sess:
            sess["user_id"] = admin_u["id"]
            sess["username"] = admin_u["username"]
            sess["role"] = admin_u["role"]

        res_admin = self.client.get("/api/admin/users")
        self.assertEqual(res_admin.status_code, 200)
        users_data = json.loads(res_admin.data)
        self.assertTrue(users_data["ok"])
        usernames = [u["username"] for u in users_data["users"]]
        self.assertIn("test_admin_2", usernames)
        self.assertIn("test_member_2", usernames)

    def test_06_admin_create_update_delete_user_api(self):
        """Tests admin API CRUD operations for users."""
        admin_u = database.create_admin_user("test_admin_3", "AdminPass123!", display_name="Admin Three", role="admin")

        with self.client.session_transaction() as sess:
            sess["user_id"] = admin_u["id"]
            sess["username"] = admin_u["username"]
            sess["role"] = admin_u["role"]

        create_res = self.client.post("/api/admin/users", json={
            "username": "test_created_user",
            "password": "SecurePassword123!",
            "display_name": "Created Sitter",
            "role": "member"
        })
        self.assertEqual(create_res.status_code, 201)
        created_data = json.loads(create_res.data)
        self.assertTrue(created_data["ok"])
        created_id = created_data["user"]["id"]

        update_res = self.client.put(f"/api/admin/users/{created_id}", json={
            "display_name": "Updated Sitter Name",
            "role": "member"
        })
        self.assertEqual(update_res.status_code, 200)
        updated_data = json.loads(update_res.data)
        self.assertEqual(updated_data["user"]["display_name"], "Updated Sitter Name")

        del_res = self.client.delete(f"/api/admin/users/{created_id}")
        self.assertEqual(del_res.status_code, 200)
        del_data = json.loads(del_res.data)
        self.assertTrue(del_data["ok"])

    def test_07_schema_version_10_elements(self):
        """Verifies schema version 10 tables and profile playback columns exist."""
        conn = database.get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute("PRAGMA user_version;")
            version = cur.fetchone()[0]
            self.assertGreaterEqual(version, 10)

            cur.execute("PRAGMA table_info(profiles);")
            cols = [c[1] for c in cur.fetchall()]
            self.assertIn("default_playback_mode", cols)
            self.assertIn("primary_card_id", cols)
            self.assertIn("offline_retention_limit", cols)

            cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='bridge_slots';")
            self.assertIsNotNone(cur.fetchone())

            cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='bridge_tracks';")
            self.assertIsNotNone(cur.fetchone())

            cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='profile_catalog_permissions';")
            self.assertIsNotNone(cur.fetchone())
        finally:
            conn.close()


class TestSuperAdminAndUserProfileManagement(IsolatedTestCase):
    reset_db_per_test = True

    def setUp(self):
        super().setUp()
        app.config["TESTING"] = True
        self.client = app.test_client()

    def _login_user(self, user_dict):
        with self.client.session_transaction() as sess:
            sess["user_id"] = user_dict["id"]
            sess["username"] = user_dict["username"]
            sess["display_name"] = user_dict.get("display_name", user_dict["username"])
            sess["role"] = user_dict["role"]
            sess["is_super_admin"] = bool(user_dict.get("is_super_admin", False))

    def test_01_schema_v12_and_super_admin_backfill(self):
        """Schema reaches v12 and backfills is_super_admin=1 only on the earliest admin row."""
        conn = database.get_db_connection()
        try:
            ver = conn.execute("PRAGMA user_version;").fetchone()[0]
            self.assertGreaterEqual(ver, 12)
            cols = [c[1] for c in conn.execute("PRAGMA table_info(admin_users);").fetchall()]
            self.assertIn("is_super_admin", cols)
        finally:
            conn.close()

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "v11.db"
            c11 = sqlite3.connect(db_path)
            try:
                c11.row_factory = sqlite3.Row
                c11.executescript(
                    """
                    CREATE TABLE admin_users (
                        id TEXT PRIMARY KEY,
                        username TEXT UNIQUE NOT NULL,
                        password_hash TEXT NOT NULL,
                        salt TEXT NOT NULL,
                        display_name TEXT DEFAULT '',
                        role TEXT DEFAULT 'admin',
                        sso_provider TEXT DEFAULT '',
                        last_login DATETIME,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    );
                    INSERT INTO admin_users (id, username, password_hash, salt, role, created_at)
                    VALUES
                        ('u_mem', 'sitter', 'h', 's', 'member', '2026-01-01 00:00:00'),
                        ('u_first', 'founder', 'h', 's', 'admin', '2026-01-02 00:00:00'),
                        ('u_second', 'co_admin', 'h', 's', 'admin', '2026-01-03 00:00:00');
                    PRAGMA user_version = 11;
                    """
                )
                database.run_schema_migrations(c11)
                self.assertGreaterEqual(c11.execute("PRAGMA user_version;").fetchone()[0], 12)
                rows = {r["id"]: r["is_super_admin"] for r in c11.execute("SELECT id, is_super_admin FROM admin_users;").fetchall()}
                self.assertEqual(rows["u_mem"], 0)
                self.assertEqual(rows["u_first"], 1)
                self.assertEqual(rows["u_second"], 0)
            finally:
                c11.close()

    def test_02_setup_creates_super_admin_and_subsequent_admins_are_regular(self):
        """Setup wizard promotes first account to Super Admin; subsequent admins are regular admins."""
        setup_res = self.client.post("/api/admin/auth/setup", json={
            "username": "founder_parent",
            "display_name": "Founder Parent",
            "password": "FounderPassword1!",
        })
        self.assertEqual(setup_res.status_code, 201)
        founder = setup_res.get_json()["user"]
        self.assertTrue(founder["is_super_admin"])
        self.assertEqual(founder["role"], "admin")

        self._login_user(founder)
        create_res = self.client.post("/api/admin/users", json={
            "username": "second_admin",
            "display_name": "Co Parent",
            "role": "admin",
            "password": "SecondPassword1!",
        })
        self.assertEqual(create_res.status_code, 201)
        second = create_res.get_json()["user"]
        self.assertFalse(second["is_super_admin"])
        self.assertEqual(second["role"], "admin")

    def test_03_regular_admin_cannot_manage_super_admin(self):
        """Regular admins are blocked (403) from editing or deleting the primary Super Admin account."""
        super_u = database.create_admin_user("sa_primary", "SuperPass1!", "Founder", role="admin")
        reg_u = database.create_admin_user("sa_regular", "RegularPass1!", "Co-Admin", role="admin")
        self.assertTrue(super_u["is_super_admin"])
        self.assertFalse(reg_u["is_super_admin"])

        self._login_user(reg_u)

        list_res = self.client.get("/api/admin/users")
        self.assertEqual(list_res.status_code, 200)
        list_data = list_res.get_json()
        self.assertFalse(list_data["current_is_super_admin"])
        super_row = next(u for u in list_data["users"] if u["id"] == super_u["id"])
        self.assertTrue(super_row["is_super_admin"])
        self.assertFalse(super_row["can_edit"])
        self.assertFalse(super_row["can_change_role"])
        self.assertFalse(super_row["can_delete"])

        for payload in (
            {"display_name": "Hijacked Name"},
            {"username": "hijacked_user"},
            {"role": "member"},
            {"password": "NewPassword99!"},
            {"sso_provider": "authentik"},
        ):
            put_res = self.client.put(f"/api/admin/users/{super_u['id']}", json=payload)
            self.assertEqual(put_res.status_code, 403, f"Expected 403 for {payload}, got {put_res.status_code}")

        del_res = self.client.delete(f"/api/admin/users/{super_u['id']}")
        self.assertEqual(del_res.status_code, 403)

        still_super = database.get_user_by_id(super_u["id"])
        self.assertEqual(still_super["username"], "sa_primary")
        self.assertEqual(still_super["display_name"], "Founder")
        self.assertEqual(still_super["role"], "admin")
        self.assertTrue(still_super["is_super_admin"])

    def test_04_regular_admin_can_manage_other_admins_and_members(self):
        """Regular admins can edit username, display name, role, SSO link, password, and delete other non-super users."""
        database.create_admin_user("sa_founder", "SuperPass1!", "Founder", role="admin")
        reg_actor = database.create_admin_user("reg_actor", "ActorPass1!", "Actor Admin", role="admin")
        other_admin = database.create_admin_user("reg_peer", "PeerPass1!", "Peer Admin", role="admin")
        member_u = database.create_admin_user("sitter_one", "SitterPass1!", "Sitter One", role="member")

        self._login_user(reg_actor)

        dup_res = self.client.put(f"/api/admin/users/{other_admin['id']}", json={
            "username": "sa_founder",
            "display_name": "Should Not Apply",
        })
        self.assertEqual(dup_res.status_code, 409)
        self.assertEqual(database.get_user_by_id(other_admin["id"])["display_name"], "Peer Admin")

        edit_peer = self.client.put(f"/api/admin/users/{other_admin['id']}", json={
            "username": "reg_peer_renamed",
            "display_name": "Updated Peer",
            "role": "member",
            "sso_provider": "authentik",
            "password": "UpdatedPeerPass2!",
        })
        self.assertEqual(edit_peer.status_code, 200)
        u_peer = edit_peer.get_json()["user"]
        self.assertEqual(u_peer["username"], "reg_peer_renamed")
        self.assertEqual(u_peer["display_name"], "Updated Peer")
        self.assertEqual(u_peer["role"], "member")
        self.assertEqual(u_peer["sso_provider"], "authentik")
        self.assertIsNotNone(database.authenticate_user("reg_peer_renamed", "UpdatedPeerPass2!"))

        prom_res = self.client.put(f"/api/admin/users/{member_u['id']}", json={"role": "admin"})
        self.assertEqual(prom_res.status_code, 200)
        self.assertEqual(prom_res.get_json()["user"]["role"], "admin")

        del_peer = self.client.delete(f"/api/admin/users/{other_admin['id']}")
        self.assertEqual(del_peer.status_code, 200)
        self.assertIsNone(database.get_user_by_id(other_admin["id"]))

        self.assertEqual(self.client.put(f"/api/admin/users/{reg_actor['id']}", json={"role": "member"}).status_code, 400)
        self.assertEqual(self.client.delete(f"/api/admin/users/{reg_actor['id']}").status_code, 400)

    def test_05_super_admin_can_manage_all_others_and_update_self_safely(self):
        """Super Admin can manage regular admins/members and update own profile, while protected from self-demotion/delete."""
        super_u = database.create_admin_user("sa_owner", "OwnerPass1!", "Primary Owner", role="admin")
        other_admin = database.create_admin_user("co_owner", "CoPass1!", "Co Owner", role="admin")
        self._login_user(super_u)

        res_other = self.client.put(f"/api/admin/users/{other_admin['id']}", json={
            "display_name": "Managed Co-Owner",
            "role": "member",
        })
        self.assertEqual(res_other.status_code, 200)
        self.assertEqual(res_other.get_json()["user"]["role"], "member")

        res_self = self.client.put(f"/api/admin/users/{super_u['id']}", json={
            "display_name": "Updated Super Owner",
            "password": "SuperOwnerNew2!",
        })
        self.assertEqual(res_self.status_code, 200)
        self.assertEqual(res_self.get_json()["user"]["display_name"], "Updated Super Owner")
        self.assertIsNotNone(database.authenticate_user("sa_owner", "SuperOwnerNew2!"))

        self.assertEqual(self.client.put(f"/api/admin/users/{super_u['id']}", json={"role": "member"}).status_code, 400)
        self.assertEqual(self.client.delete(f"/api/admin/users/{super_u['id']}").status_code, 400)

    def test_06_admin_html_contains_edit_user_modal_and_valid_password_forms(self):
        """Rendered /admin includes Edit User modal and wraps every password input in a <form>."""
        res = self.client.get("/admin")
        self.assertEqual(res.status_code, 200)
        parser = _FormPasswordParser()
        parser.feed(res.get_data(as_text=True))
        for expected_id in ("editUserModal", "editUserForm", "editUserUsername", "editUserPassword", "editUserRole"):
            self.assertIn(expected_id, parser.ids)
        self.assertEqual(parser.orphan_passwords, [], f"Password input(s) outside <form>: {parser.orphan_passwords}")


if __name__ == "__main__":
    unittest.main()

"""Unit tests for StoryDeck Web UI Overhaul, Shared Core Offline Awareness, and SSO/Authentik Integration."""
import base64
import json
import sqlite3
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

from tests.harness import IsolatedTestCase
from app import app
import database


class _SectionAndFormParser(HTMLParser):
    """Collects section/form nesting to ensure dedicated tabs and proper form containment."""

    def __init__(self):
        super().__init__()
        self.form_depth = 0
        self.orphan_passwords = []
        self.id_to_section = {}
        self._section_stack = []

    def handle_starttag(self, tag, attrs):
        t = tag.lower()
        attr_map = {k.lower(): (v or "") for k, v in attrs}
        if t == "section":
            self._section_stack.append(attr_map.get("id", ""))
        el_id = attr_map.get("id")
        if el_id:
            self.id_to_section[el_id] = list(self._section_stack)
        if t == "form":
            self.form_depth += 1
        elif t == "input" and attr_map.get("type", "").lower() == "password":
            if self.form_depth == 0:
                self.orphan_passwords.append(attr_map.get("id") or attr_map.get("name") or "<unnamed>")

    def handle_endtag(self, tag):
        t = tag.lower()
        if t == "form" and self.form_depth > 0:
            self.form_depth -= 1
        elif t == "section" and self._section_stack:
            self._section_stack.pop()


class TestUIOverhaulAndSSO(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        app.config["TESTING"] = True
        self.client = app.test_client()
        self._orig_sso_cfg = database.get_sso_config()
        self._orig_lan = database.get_system_settings().get("lan_sitter_access_enabled", True)
        conn = database.get_db_connection()
        try:
            with conn:
                conn.execute("DELETE FROM admin_users WHERE username LIKE 'sso_ui_%';")
                conn.execute("DELETE FROM profiles WHERE id = 'ui_test_child';")
        finally:
            conn.close()

    def tearDown(self):
        database.set_system_setting("sso_config", self._orig_sso_cfg)
        database.set_system_setting("lan_sitter_access_enabled", self._orig_lan)
        conn = database.get_db_connection()
        try:
            with conn:
                conn.execute("DELETE FROM admin_users WHERE username LIKE 'sso_ui_%';")
                conn.execute("DELETE FROM profiles WHERE id = 'ui_test_child';")
        finally:
            conn.close()
        super().tearDown()

    def _login_session(self, role="admin", user_id="ui_admin_1", username="ui_admin"):
        with self.client.session_transaction() as sess:
            sess["user_id"] = user_id
            sess["username"] = username
            sess["display_name"] = username
            sess["role"] = role

    def test_01_portal_renders_sidebar_avatar_carousel_and_offline_awareness(self):
        """Parent Portal renders collapsible sidebar, /admin link, avatar carousel paired with player select, and offline UI."""
        res = self.client.get("/", environ_base={"REMOTE_ADDR": "192.168.1.100"})
        self.assertEqual(res.status_code, 200)
        html = res.get_data(as_text=True)

        for expected in (
            'id="appSidebar"',
            'id="sidebarCollapseBtn"',
            'href="/admin"',
            'id="portalContextBar"',
            'id="profileAvatarCarousel"',
            'Target Yoto Player',
            'id="playerDeviceSelect"',
            'id="coreConnectionPill"',
            'id="coreOfflineBanner"',
            'id="btnRetryCoreHealth"',
            '/static/js/sidebar.js',
            '/static/js/core_health.js',
        ):
            self.assertIn(expected, html)

        self.assertNotIn('class="bottom-nav"', html)
        self.assertNotIn('id="headerProfileSelect"', html)

    def test_02_admin_renders_mirror_sidebar_users_tab_and_offline_awareness(self):
        """Admin Console mirrors sidebar + Parent Portal link, isolates Users & Access tab, and encloses all passwords in forms."""
        res = self.client.get("/admin")
        self.assertEqual(res.status_code, 200)
        html = res.get_data(as_text=True)

        for expected in (
            'id="appSidebar"',
            'id="sidebarCollapseBtn"',
            'href="/"',
            'data-tab="users"',
            'id="tab-users"',
            'id="coreConnectionPill"',
            'id="coreOfflineBanner"',
            'id="btnRetryCoreHealth"',
            '/static/js/sidebar.js',
            '/static/js/core_health.js',
        ):
            self.assertIn(expected, html)

        self.assertNotIn('class="dashboard-tabs"', html)
        self.assertNotIn('id="coreVersionBadge"', html)

        with open("static/js/core_health.js", "r", encoding="utf-8") as f:
            ch_js = f.read()
        self.assertIn("function formatVersionTag", ch_js)
        self.assertNotIn("`v${state.lastVersion}`", ch_js)

        parser = _SectionAndFormParser()
        parser.feed(html)
        self.assertEqual(parser.orphan_passwords, [], f"Password input(s) outside <form>: {parser.orphan_passwords}")
        self.assertIn("tab-users", parser.id_to_section.get("cardUserAccounts", []))
        self.assertIn("tab-users", parser.id_to_section.get("cardSsoConfig", []))
        self.assertIn("tab-users", parser.id_to_section.get("toggleLanSitterAccess", []))
        self.assertNotIn("tab-settings", parser.id_to_section.get("cardUserAccounts", []))
        self.assertNotIn("tab-settings", parser.id_to_section.get("toggleLanSitterAccess", []))

    def test_03_public_profiles_and_health_endpoints(self):
        """GET /api/profiles works for Portal clients without admin cookie and exposes avatar & playback metadata."""
        database.upsert_profile("ui_test_child", {
            "display_name": "Child B",
            "default_player_id": "yoto_dev_1",
            "player_ids": ["yoto_dev_1"],
            "default_playback_mode": "offline",
            "avatar_color": "#10b981",
            "avatar_initial": "C",
        })

        prof_res = self.client.get("/api/profiles", environ_base={"REMOTE_ADDR": "192.168.1.100"})
        self.assertEqual(prof_res.status_code, 200)
        profiles = prof_res.get_json()
        child = next((p for p in profiles if p["id"] == "ui_test_child"), None)
        self.assertIsNotNone(child)
        self.assertIn("/api/avatar/ui_test_child", child["avatar_url"])
        self.assertEqual(child["default_playback_mode"], "offline")
        self.assertEqual(child["default_player_id"], "yoto_dev_1")
        self.assertEqual(child["avatar_color"], "#10b981")
        self.assertEqual(child["avatar_initial"], "C")

        health_res = self.client.get("/health", environ_base={"REMOTE_ADDR": "192.168.1.100"})
        self.assertEqual(health_res.status_code, 200)
        self.assertEqual(health_res.get_json().get("status"), "healthy")

    def test_04_sso_config_api_rbac_persistence_and_validation(self):
        """Verifies RBAC, secret masking/preservation, CIDR validation, LAN toggle, and OIDC callback placeholder."""
        lan_res = self.client.get("/api/admin/auth/sso-config", environ_base={"REMOTE_ADDR": "192.168.1.88"})
        self.assertEqual(lan_res.status_code, 401)

        self._login_session(role="member", user_id="ui_mem_1", username="ui_member")
        mem_res = self.client.get("/api/admin/auth/sso-config")
        self.assertEqual(mem_res.status_code, 403)

        self._login_session(role="admin", user_id="ui_adm_1", username="ui_admin")
        bad_cidr = self.client.put("/api/admin/auth/sso-config", json={
            "sso_config": {"extra_trusted_proxies": ["not_an_ip"]}
        })
        self.assertEqual(bad_cidr.status_code, 400)

        save_res = self.client.put("/api/admin/auth/sso-config", json={
            "lan_sitter_access_enabled": False,
            "sso_config": {
                "forward_auth_enabled": True,
                "provider_name": "Authentik",
                "admin_group": "storydeck-admins",
                "default_role": "member",
                "auto_provision_users": True,
                "extra_trusted_proxies": ["10.10.0.5"],
                "logout_url": "https://auth.example.com/logout",
                "oidc_enabled": True,
                "oidc_issuer_url": "https://auth.example.com/application/o/storydeck/",
                "oidc_client_id": "sd-client",
                "oidc_client_secret": "supersecret_oidc_value_99",
            }
        })
        self.assertEqual(save_res.status_code, 200)
        payload = save_res.get_json()
        self.assertFalse(payload["lan_sitter_access_enabled"])
        self.assertIn("••••", payload["sso_config"]["oidc_client_secret"])
        self.assertNotIn("supersecret_oidc_value_99", payload["sso_config"]["oidc_client_secret"])

        resave_res = self.client.put("/api/admin/auth/sso-config", json={
            "sso_config": {
                "provider_name": "Authentik",
                "oidc_client_secret": payload["sso_config"]["oidc_client_secret"],
            }
        })
        self.assertEqual(resave_res.status_code, 200)
        self.assertEqual(database.get_sso_config().get("oidc_client_secret"), "supersecret_oidc_value_99")

        cb_res = self.client.get("/api/auth/oidc/callback")
        self.assertEqual(cb_res.status_code, 302)
        self.assertIn("/login?error=", cb_res.headers.get("Location", ""))

    def test_05_sso_forward_auth_group_mapping_auto_provision_and_disable(self):
        """Authentik group headers map admin_group -> admin vs member, support auto-provision, and respect disable toggle."""
        database.set_system_setting("sso_config", {
            "forward_auth_enabled": True,
            "provider_name": "Authentik",
            "header_username": "X-authentik-username",
            "header_display_name": "X-authentik-name",
            "header_groups": "X-authentik-groups",
            "admin_group": "storydeck-admins",
            "default_role": "member",
            "auto_provision_users": True,
            "extra_trusted_proxies": [],
        })

        me_member = self.client.get(
            "/api/auth/me",
            environ_base={"REMOTE_ADDR": "127.0.0.1"},
            headers={
                "X-authentik-username": "sso_ui_sitter",
                "X-authentik-name": "Aunt Sitter",
                "X-authentik-groups": "family|sitters",
            },
        )
        self.assertEqual(me_member.status_code, 200)
        d_mem = me_member.get_json()
        self.assertTrue(d_mem["authenticated"])
        self.assertEqual(d_mem["user"]["role"], "member")
        self.assertEqual(d_mem["user"]["auth_type"], "sso")
        self.assertEqual(d_mem["user"]["display_name"], "Aunt Sitter")
        db_mem = database.get_user_by_username("sso_ui_sitter")
        self.assertIsNotNone(db_mem)
        self.assertEqual(db_mem["sso_provider"], "authentik")

        adm_denied = self.client.get(
            "/api/admin/users",
            environ_base={"REMOTE_ADDR": "127.0.0.1"},
            headers={
                "X-authentik-username": "sso_ui_sitter",
                "X-authentik-groups": "family|sitters",
            },
        )
        self.assertEqual(adm_denied.status_code, 403)

        adm_allowed = self.client.get(
            "/api/admin/users",
            environ_base={"REMOTE_ADDR": "127.0.0.1"},
            headers={
                "X-authentik-username": "sso_ui_parent",
                "X-authentik-name": "Mom Admin",
                "X-authentik-groups": "family|storydeck-admins",
            },
        )
        self.assertEqual(adm_allowed.status_code, 200)

        cfg = database.get_sso_config()
        cfg["forward_auth_enabled"] = False
        database.set_system_setting("sso_config", cfg)
        disabled_res = self.client.get(
            "/api/admin/users",
            environ_base={"REMOTE_ADDR": "127.0.0.1"},
            headers={
                "X-authentik-username": "sso_ui_parent",
                "X-authentik-groups": "storydeck-admins",
            },
        )
        self.assertEqual(disabled_res.status_code, 401)

    def test_06_create_sso_prelinked_user_without_local_password(self):
        """Admins can pre-create an SSO-linked account without entering a local password."""
        self._login_session(role="admin", user_id="ui_adm_2", username="ui_admin_2")

        short_res = self.client.post("/api/admin/users", json={
            "username": "sso_ui_local_short",
            "display_name": "Short Pw",
            "role": "member",
            "password": "123",
        })
        self.assertEqual(short_res.status_code, 400)

        sso_res = self.client.post("/api/admin/users", json={
            "username": "sso_ui_prelinked",
            "display_name": "Authentik Uncle",
            "email": "sso_ui_uncle@example.com",
            "role": "member",
            "sso_provider": "authentik",
            "password": "",
        })
        self.assertEqual(sso_res.status_code, 201)
        u = sso_res.get_json()["user"]
        self.assertEqual(u["sso_provider"], "authentik")
        self.assertEqual(u["email"], "sso_ui_uncle@example.com")

    def test_07_sso_initial_match_by_email_then_username_and_uid_binding(self):
        """Matches unbound accounts by email first, then username, binds immutable UID, and survives later renames."""
        by_email_user = database.create_admin_user(
            "sso_ui_local_alice",
            "AliceSecret1!",
            display_name="Alice Local",
            role="admin",
            email="sso_ui_alice@family.local",
        )
        res1 = self.client.get(
            "/api/auth/me",
            environ_base={"REMOTE_ADDR": "127.0.0.1"},
            headers={
                "X-authentik-uid": "uid_test_alice_001",
                "X-authentik-username": "alice_renamed_in_idp",
                "X-authentik-email": "SSO_UI_ALICE@family.local",
            },
        )
        self.assertEqual(res1.status_code, 200)
        d1 = res1.get_json()["user"]
        self.assertEqual(d1["id"], by_email_user["id"])
        self.assertEqual(d1["role"], "admin")
        self.assertEqual(d1["sso_subject"], "uid_test_alice_001")

        res1_renamed = self.client.get(
            "/api/auth/me",
            environ_base={"REMOTE_ADDR": "127.0.0.1"},
            headers={
                "X-authentik-uid": "uid_test_alice_001",
                "X-authentik-username": "totally_new_handle",
                "X-authentik-email": "new.alice@family.local",
            },
        )
        self.assertEqual(res1_renamed.status_code, 200)
        self.assertEqual(res1_renamed.get_json()["user"]["id"], by_email_user["id"])

        by_uname_user = database.create_admin_user(
            "sso_ui_local_bob",
            "BobSecret1!",
            display_name="Bob Local",
            role="member",
        )
        res2 = self.client.get(
            "/api/auth/me",
            environ_base={"REMOTE_ADDR": "127.0.0.1"},
            headers={
                "X-authentik-uid": "uid_test_bob_002",
                "X-authentik-username": "SSO_UI_LOCAL_BOB",
                "X-authentik-email": "sso_ui_bob@family.local",
            },
        )
        self.assertEqual(res2.status_code, 200)
        d2 = res2.get_json()["user"]
        self.assertEqual(d2["id"], by_uname_user["id"])
        refetched_bob = database.get_user_by_id(by_uname_user["id"])
        self.assertEqual(refetched_bob["sso_subject"], "uid_test_bob_002")
        self.assertEqual(refetched_bob["email"], "sso_ui_bob@family.local")

    def test_08_sso_unmatched_user_auto_added_and_prevents_hijack(self):
        """Unmatched SSO user is auto-added; another UID with same username/email cannot hijack the bound account."""
        res_new = self.client.get(
            "/api/auth/me",
            environ_base={"REMOTE_ADDR": "127.0.0.1"},
            headers={
                "X-authentik-uid": "uid_test_carol_003",
                "X-authentik-username": "sso_ui_carol",
                "X-authentik-email": "sso_ui_carol@family.local",
                "X-authentik-name": "Carol Sitter",
            },
        )
        self.assertEqual(res_new.status_code, 200)
        u_carol = res_new.get_json()["user"]
        self.assertEqual(u_carol["role"], "member")
        self.assertEqual(u_carol["sso_subject"], "uid_test_carol_003")
        carol_db = database.get_user_by_sso_subject("uid_test_carol_003", "authentik")
        self.assertIsNotNone(carol_db)
        self.assertEqual(carol_db["email"], "sso_ui_carol@family.local")

        res_other = self.client.get(
            "/api/auth/me",
            environ_base={"REMOTE_ADDR": "127.0.0.1"},
            headers={
                "X-authentik-uid": "uid_test_other_999",
                "X-authentik-username": "sso_ui_carol",
                "X-authentik-email": "sso_ui_carol@family.local",
            },
        )
        self.assertEqual(res_other.status_code, 200)
        u_other = res_other.get_json()["user"]
        self.assertNotEqual(u_other["id"], carol_db["id"])
        self.assertTrue(u_other["username"].startswith("sso_ui_carol_"))
        self.assertEqual(u_other["sso_subject"], "uid_test_other_999")

    def test_09_schema_v13_migration_and_admin_email_subject_crud(self):
        """Verifies schema >= 13 migration, admin UI fields, duplicate email 409, and UID unlink via PUT."""
        conn = database.get_db_connection()
        try:
            self.assertGreaterEqual(conn.execute("PRAGMA user_version;").fetchone()[0], 13)
            cols = [c[1] for c in conn.execute("PRAGMA table_info(admin_users);").fetchall()]
            self.assertIn("email", cols)
            self.assertIn("sso_subject", cols)
        finally:
            conn.close()

        with tempfile.TemporaryDirectory() as tmpdir:
            p12 = Path(tmpdir) / "v12.db"
            c12 = sqlite3.connect(p12)
            try:
                c12.execute(
                    """
                    CREATE TABLE admin_users (
                        id TEXT PRIMARY KEY,
                        username TEXT UNIQUE NOT NULL,
                        password_hash TEXT NOT NULL,
                        salt TEXT NOT NULL,
                        display_name TEXT DEFAULT '',
                        role TEXT DEFAULT 'admin',
                        sso_provider TEXT DEFAULT '',
                        is_super_admin BOOLEAN DEFAULT 0,
                        last_login DATETIME,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    );
                    """
                )
                c12.execute("PRAGMA user_version = 12;")
                database.run_schema_migrations(c12)
                self.assertGreaterEqual(c12.execute("PRAGMA user_version;").fetchone()[0], 13)
                m_cols = [c[1] for c in c12.execute("PRAGMA table_info(admin_users);").fetchall()]
                self.assertIn("email", m_cols)
                self.assertIn("sso_subject", m_cols)
            finally:
                c12.close()

        self._login_session(role="admin", user_id="ui_adm_v13", username="ui_admin_v13")
        u1 = database.create_admin_user(
            "sso_ui_m1",
            "PassWord1!",
            role="member",
            email="sso_ui_m1@test.local",
            sso_provider="authentik",
            sso_subject="uid_test_m1",
            is_super_admin=False,
        )
        u2 = database.create_admin_user(
            "sso_ui_m2",
            "PassWord1!",
            role="member",
            email="sso_ui_m2@test.local",
            is_super_admin=False,
        )

        dup_email_res = self.client.put(f"/api/admin/users/{u2['id']}", json={"email": "sso_ui_m1@test.local"})
        self.assertEqual(dup_email_res.status_code, 409)

        unlink_res = self.client.put(f"/api/admin/users/{u1['id']}", json={
            "email": "sso_ui_m1_new@test.local",
            "sso_subject": "",
        })
        self.assertEqual(unlink_res.status_code, 200)
        u1_after = unlink_res.get_json()["user"]
        self.assertEqual(u1_after["email"], "sso_ui_m1_new@test.local")
        self.assertEqual(u1_after["sso_subject"], "")

    def test_10_centralized_sidebar_and_self_service_profile_modal_parity(self):
        """Verifies Portal (/) and Admin (/admin) share the centralized sidebar and self-service profile modal without duplicate IDs."""
        self._login_session(role="admin", user_id="ui_adm_nav", username="ui_admin_nav")

        for route in ("/", "/admin"):
            res = self.client.get(route)
            self.assertEqual(res.status_code, 200)
            html = res.get_data(as_text=True)

            for shared_id in (
                "appSidebar",
                "sidebarUserIdentity",
                "sidebarUserName",
                "sidebarUserRole",
                "sidebarUserActions",
                "portalEditProfileModal",
                "portalEditProfileForm",
                "portalEditAbsGroup",
                "btnAutoMatchPortalAbs",
                "btnPortalUnlinkAbs",
            ):
                id_marker = f'id="{shared_id}"'
                count = html.count(id_marker)
                self.assertEqual(
                    count,
                    1,
                    f"Expected exactly 1 #{shared_id} in {route}, found {count}",
                )

            self.assertIn('onclick="openSelfServiceProfileModal()"', html)
            self.assertIn('onclick="logoutCurrentUser()"', html)
            self.assertNotIn('id="sidebarAdminName"', html)
            self.assertNotIn('id="sidebarAdminRole"', html)

    def test_11_sso_login_modal_ui_and_public_sso_config(self):
        """Login modal includes SSO button + error alert container, and public /api/auth/sso/config exposes active mode."""
        res = self.client.get("/login")
        self.assertEqual(res.status_code, 200)
        html = res.get_data(as_text=True)

        alias_res = self.client.get("/admin/login")
        self.assertEqual(alias_res.status_code, 200)

        for expected in (
            'id="loginErrorAlert"',
            'id="loginSsoContainer"',
            'id="btnSsoLogin"',
            'id="ssoButtonText"',
            'onclick="handleSsoLogin()"',
            'id="ssoOidcLogoutReturnUri"',
            'onclick="applySuggestedLogoutUri()"',
        ):
            self.assertIn(expected, html)

        database.set_system_setting("sso_config", {
            "forward_auth_enabled": True,
            "provider_name": "Authelia",
            "oidc_enabled": False,
        })
        cfg_res = self.client.get("/api/auth/sso/config")
        self.assertEqual(cfg_res.status_code, 200)
        sso_meta = cfg_res.get_json()["sso"]
        self.assertTrue(sso_meta["enabled"])
        self.assertEqual(sso_meta["login_mode"], "forward_auth")
        self.assertEqual(sso_meta["provider_name"], "Authelia")
        self.assertEqual(sso_meta["login_url"], "/api/auth/sso/login")

        database.set_system_setting("sso_config", {
            "forward_auth_enabled": True,
            "provider_name": "Authentik",
            "oidc_enabled": True,
            "oidc_issuer_url": "https://auth.example.com/application/o/storydeck/",
            "oidc_client_id": "storydeck-client",
        })
        status_res = self.client.get("/api/admin/auth/status")
        self.assertEqual(status_res.status_code, 200)
        status_sso = status_res.get_json()["sso"]
        self.assertTrue(status_sso["enabled"])
        self.assertTrue(status_sso["oidc_ready"])
        self.assertEqual(status_sso["login_mode"], "oidc")

        database.set_system_setting("sso_config", {
            "forward_auth_enabled": False,
            "provider_name": "Reverse Proxy",
            "oidc_enabled": True,
            "oidc_issuer_url": "",
            "oidc_client_id": "",
        })
        norm_cfg_res = self.client.get("/api/auth/sso/config")
        self.assertEqual(norm_cfg_res.status_code, 200)
        norm_sso = norm_cfg_res.get_json()["sso"]
        self.assertTrue(norm_sso["enabled"])
        self.assertEqual(norm_sso["login_mode"], "oidc")
        self.assertEqual(norm_sso["provider_name"], "SSO")

        norm_status_res = self.client.get("/api/admin/auth/status")
        self.assertEqual(norm_status_res.status_code, 200)
        norm_status_sso = norm_status_res.get_json()["sso"]
        self.assertEqual(norm_status_sso["provider_name"], "SSO")
        self.assertEqual(norm_status_sso["login_mode"], "oidc")

    def test_12_sso_login_dispatcher_forward_auth(self):
        """GET /api/auth/sso/login authenticates via trusted ForwardAuth headers or redirects with diagnostic error."""
        database.set_system_setting("sso_config", {
            "forward_auth_enabled": True,
            "provider_name": "Authentik",
            "oidc_enabled": False,
            "auto_provision_users": True,
            "default_role": "member",
        })

        no_hdr = self.client.get("/api/auth/sso/login?next=/", environ_base={"REMOTE_ADDR": "192.168.1.99"})
        self.assertEqual(no_hdr.status_code, 302)
        loc_err = no_hdr.headers.get("Location", "")
        self.assertIn("/login?error=", loc_err)

        with_hdr = self.client.get(
            "/api/auth/sso/login?next=/",
            environ_base={"REMOTE_ADDR": "127.0.0.1"},
            headers={
                "X-authentik-uid": "uid_sso_disp_1",
                "X-authentik-username": "sso_ui_disp_user",
                "X-authentik-email": "sso_ui_disp@family.local",
                "X-authentik-name": "Disp User",
            },
        )
        self.assertEqual(with_hdr.status_code, 302)
        self.assertEqual(with_hdr.headers.get("Location"), "/")
        with self.client.session_transaction() as sess:
            self.assertEqual(sess.get("username"), "sso_ui_disp_user")
            self.assertEqual(sess.get("auth_type"), "sso")

    def test_13_native_oidc_authorization_code_flow(self):
        """Full Native OIDC flow: discovery, redirect with state, code exchange, claims resolution, provisioning, and session."""
        database.set_system_setting("sso_config", {
            "forward_auth_enabled": False,
            "provider_name": "Authentik",
            "admin_group": "storydeck-admins",
            "default_role": "member",
            "auto_provision_users": True,
            "oidc_enabled": True,
            "oidc_issuer_url": "https://auth.example.com/application/o/storydeck/",
            "oidc_client_id": "sd-oidc-client",
            "oidc_client_secret": "sd-oidc-secret",
        })

        discovery_doc = {
            "issuer": "https://auth.example.com/application/o/storydeck/",
            "authorization_endpoint": "https://auth.example.com/application/o/authorize/",
            "token_endpoint": "https://auth.example.com/application/o/token/",
            "userinfo_endpoint": "https://auth.example.com/application/o/userinfo/",
            "scopes_supported": ["openid", "email", "profile", "groups"],
            "token_endpoint_auth_methods_supported": ["client_secret_post", "client_secret_basic"],
        }

        current_userinfo = {
            "sub": "oidc-sub-007",
            "preferred_username": "sso_ui_oidc_admin",
            "email": "sso_ui_oidc@family.local",
            "name": "OIDC Admin",
            "groups": ["family", "storydeck-admins"],
        }

        def fake_get(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            if url.endswith("/.well-known/openid-configuration"):
                resp.json.return_value = discovery_doc
            elif url == discovery_doc["userinfo_endpoint"]:
                resp.json.return_value = dict(current_userinfo)
            else:
                resp.status_code = 404
            return resp

        captured_posts = []

        def fake_post(url, **kwargs):
            captured_posts.append({"url": url, "data": dict(kwargs.get("data") or {}), "auth": kwargs.get("auth")})
            sub_val = current_userinfo["sub"]
            header_b64 = base64.urlsafe_b64encode(json.dumps({"alg": "RS256"}).encode()).decode().rstrip("=")
            payload_b64 = base64.urlsafe_b64encode(json.dumps({"sub": sub_val}).encode()).decode().rstrip("=")
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {
                "access_token": "acc_tok_123",
                "id_token": f"{header_b64}.{payload_b64}.fakesig",
                "token_type": "Bearer",
            }
            return resp

        with patch("routes.admin_auth.requests.get", side_effect=fake_get), \
             patch("routes.admin_auth.requests.post", side_effect=fake_post):
            disp_res = self.client.get("/api/auth/sso/login?next=/admin")
            self.assertEqual(disp_res.status_code, 302)
            self.assertIn("/api/auth/oidc/login?next=/admin", disp_res.headers.get("Location", ""))

            login_res = self.client.get("/api/auth/oidc/login?next=/admin")
            self.assertEqual(login_res.status_code, 302)
            auth_location = login_res.headers.get("Location", "")
            self.assertTrue(auth_location.startswith(discovery_doc["authorization_endpoint"]))
            qs = parse_qs(urlparse(auth_location).query)
            self.assertEqual(qs.get("client_id"), ["sd-oidc-client"])
            self.assertEqual(qs.get("response_type"), ["code"])
            self.assertEqual(qs.get("code_challenge_method"), ["S256"])
            self.assertTrue(qs.get("code_challenge"))
            self.assertIn("groups", qs.get("scope", [""])[0].split())
            state = qs["state"][0]

            bad_cb = self.client.get("/api/auth/oidc/callback?code=c1&state=forged_state")
            self.assertEqual(bad_cb.status_code, 302)
            self.assertIn("/login?error=", bad_cb.headers.get("Location", ""))

            ok_cb = self.client.get(f"/api/auth/oidc/callback?code=authcode_xyz&state={state}")
            self.assertEqual(ok_cb.status_code, 302)
            self.assertEqual(ok_cb.headers.get("Location"), "/admin")
            self.assertEqual(len(captured_posts), 1)
            self.assertIsNone(captured_posts[0]["auth"])
            self.assertEqual(captured_posts[0]["data"].get("client_secret"), "sd-oidc-secret")
            self.assertTrue(captured_posts[0]["data"].get("code_verifier"))

        me_res = self.client.get("/api/auth/me")
        self.assertEqual(me_res.status_code, 200)
        me_data = me_res.get_json()
        self.assertTrue(me_data["authenticated"])
        self.assertEqual(me_data["user"]["username"], "sso_ui_oidc_admin")
        self.assertEqual(me_data["user"]["role"], "admin")
        self.assertEqual(me_data["user"]["auth_type"], "oidc")

        status_res = self.client.get("/api/admin/auth/status")
        self.assertTrue(status_res.get_json()["authenticated"])
        self.assertTrue(status_res.get_json()["portal_authenticated"])
        self.assertEqual(status_res.get_json()["user"]["auth_type"], "oidc")

        with self.client.session_transaction() as sess:
            sess.clear()
        current_userinfo.update({
            "sub": "oidc-sub-member-008",
            "preferred_username": "sso_ui_oidc_member",
            "email": "sso_ui_member@family.local",
            "name": "OIDC Member",
            "groups": ["family"],
        })
        with patch("routes.admin_auth.requests.get", side_effect=fake_get), \
             patch("routes.admin_auth.requests.post", side_effect=fake_post):
            mem_login = self.client.get("/api/auth/oidc/login?next=/admin")
            mem_state = parse_qs(urlparse(mem_login.headers.get("Location", "")).query)["state"][0]
            mem_cb = self.client.get(f"/api/auth/oidc/callback?code=authcode_mem&state={mem_state}")
            self.assertEqual(mem_cb.status_code, 302)
            self.assertEqual(mem_cb.headers.get("Location"), "/")

        mem_status = self.client.get("/api/admin/auth/status").get_json()
        self.assertFalse(mem_status["authenticated"])
        self.assertTrue(mem_status["portal_authenticated"])
        self.assertEqual(mem_status["portal_user"]["role"], "member")

        cfg_ext = database.get_sso_config()
        cfg_ext["logout_url"] = "https://auth.example.com/application/o/storydeck/end-session/"
        database.set_system_setting("sso_config", cfg_ext)
        logout_res = self.client.post("/api/admin/auth/logout", headers={"X-Forwarded-Proto": "https", "X-Forwarded-Host": "storydeck.example.com"})
        self.assertEqual(logout_res.status_code, 200)
        logout_data = logout_res.get_json()
        self.assertIn("post_logout_redirect_uri=https%3A%2F%2Fstorydeck.example.com%2Flogin", logout_data.get("logout_url", ""))
        self.assertIn("id_token_hint=", logout_data.get("logout_url", ""))

    def test_14_user_profile_self_service_display_name_and_password_update(self):
        """Covers routes/user_profile.py self-service display name update and password change validation."""
        u = database.create_admin_user("sso_ui_self_svc", "OldSelfPass1!", display_name="Before", role="member")
        self._login_session(role="member", user_id=u["id"], username=u["username"])

        bad_pw = self.client.put("/api/auth/me", json={"password": "short"})
        self.assertEqual(bad_pw.status_code, 400)

        ok_res = self.client.put("/api/auth/me", json={
            "display_name": "Updated Sitter",
            "password": "NewSelfPass2!",
        })
        self.assertEqual(ok_res.status_code, 200)
        self.assertEqual(ok_res.get_json()["user"]["display_name"], "Updated Sitter")
        self.assertIsNotNone(database.authenticate_user("sso_ui_self_svc", "NewSelfPass2!"))


if __name__ == "__main__":
    unittest.main()

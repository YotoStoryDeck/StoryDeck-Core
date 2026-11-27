"""Unit tests for Yoto Cloud OAuth PKCE flow and ABS/Yoto client modules (zero network calls)."""
import time
import unittest
from unittest.mock import MagicMock, patch

import requests

from tests.harness import IsolatedTestCase
from app import app
import clients
from clients.abs_client import ABSClient
from clients.yoto_client import YotoClient
import database
import routes.yoto_oauth as yoto_oauth


class TestYotoOAuthFlow(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        self.app = app.test_client()
        self.app_context = app.app_context()
        self.app_context.push()

        self.orig_creds = database.get_api_credentials()
        database.set_system_setting("yoto_oauth_pkce_flows", {})
        yoto_oauth.YOTO_PKCE_FLOWS.clear()

        with self.app.session_transaction() as sess:
            sess["user_id"] = "admin_user"
            sess["username"] = "admin"
            sess["role"] = "admin"

    def tearDown(self):
        database.set_system_setting("yoto_oauth_pkce_flows", {})
        yoto_oauth.YOTO_PKCE_FLOWS.clear()
        database.update_api_credentials(self.orig_creds)
        clients.reload_clients()
        self.app_context.pop()
        super().tearDown()

    def test_01_reverse_proxy_https_detection(self):
        """Verifies X-Forwarded-Proto/Host resolves to https://<host>/s/yoto-callback."""
        res = self.app.post(
            "/api/admin/yoto/oauth/start",
            json={"client_id": "pub_client_https"},
            headers={"X-Forwarded-Proto": "https", "X-Forwarded-Host": "storydeck.example.com"},
        )
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["redirect_uri"], "https://storydeck.example.com/s/yoto-callback")
        self.assertTrue(data["auto_callback"])

    def test_02_client_origin_loopback_and_plain_http_lan_fallback(self):
        """Verifies loopback origins auto-callback while plain HTTP LAN IPs use :8787 fallback."""
        for origin in ("http://127.0.0.1:5055", "http://localhost:5055"):
            res = self.app.post("/api/admin/yoto/oauth/start", json={"client_id": "pub_client_local", "client_origin": origin})
            self.assertEqual(res.status_code, 200)
            data = res.get_json()
            self.assertEqual(data["redirect_uri"], f"{origin}/s/yoto-callback")
            self.assertTrue(data["auto_callback"])

        res_lan = self.app.post(
            "/api/admin/yoto/oauth/start",
            json={"client_id": "pub_client_lan", "client_origin": "http://192.168.1.50:5055"},
        )
        self.assertEqual(res_lan.status_code, 200)
        lan_data = res_lan.get_json()
        self.assertEqual(lan_data["redirect_uri"], "http://127.0.0.1:8787/callback")
        self.assertFalse(lan_data["auto_callback"])

    def test_03_callback_landing_exchanges_code_server_side_without_secret(self):
        """Verifies GET /s/yoto-callback exchanges PKCE code directly without client_secret."""
        start_res = self.app.post(
            "/api/admin/yoto/oauth/start",
            json={"client_id": "pub_client_auto", "client_origin": "https://storydeck.example.com"},
        )
        state = start_res.get_json()["state"]

        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {"access_token": "acc_token_auto", "refresh_token": "ref_token_auto_987", "expires_in": 3600}

        with patch("routes.yoto_oauth.requests.post", return_value=mock_resp) as mock_post:
            cb_res = self.app.get(f"/s/yoto-callback?code=auth_code_123&state={state}")

        self.assertEqual(cb_res.status_code, 200)
        html_body = cb_res.get_data(as_text=True)
        self.assertIn("BroadcastChannel", html_body)
        self.assertIn("YOTO_AUTH_COMPLETE", html_body)

        mock_post.assert_called_once()
        sent_payload = mock_post.call_args.kwargs["json"]
        self.assertEqual(sent_payload["grant_type"], "authorization_code")
        self.assertEqual(sent_payload["client_id"], "pub_client_auto")
        self.assertEqual(sent_payload["code"], "auth_code_123")
        self.assertEqual(sent_payload["redirect_uri"], "https://storydeck.example.com/s/yoto-callback")
        self.assertNotIn("client_secret", sent_payload)

        creds = database.get_api_credentials()
        self.assertEqual(creds["yoto_client_id"], "pub_client_auto")
        self.assertEqual(creds["yoto_refresh_token"], "ref_token_auto_987")

        status_res = self.app.get(f"/api/admin/yoto/oauth/status?state={state}")
        self.assertEqual(status_res.status_code, 200)
        self.assertEqual(status_res.get_json()["status"], "completed")

    def test_04_multi_worker_sqlite_flow_persistence(self):
        """Clearing in-memory YOTO_PKCE_FLOWS simulates a second Gunicorn worker handling callback."""
        start_res = self.app.post(
            "/api/admin/yoto/oauth/start",
            json={"client_id": "pub_client_worker2", "client_origin": "http://localhost:5055"},
        )
        state = start_res.get_json()["state"]
        yoto_oauth.YOTO_PKCE_FLOWS.clear()

        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {"access_token": "acc_worker2", "refresh_token": "ref_worker2_abc", "expires_in": 3600}

        with patch("routes.yoto_oauth.requests.post", return_value=mock_resp):
            cb_res = self.app.get(f"/s/yoto-callback?code=worker2_code&state={state}")

        self.assertEqual(cb_res.status_code, 200)
        self.assertEqual(database.get_api_credentials()["yoto_refresh_token"], "ref_worker2_abc")

    def test_05_idempotent_finish_after_callback_completion(self):
        """Calling /finish after /s/yoto-callback completed returns 200 without re-calling Auth0."""
        start_res = self.app.post(
            "/api/admin/yoto/oauth/start",
            json={"client_id": "pub_client_idem", "client_origin": "https://storydeck.example.com"},
        )
        state = start_res.get_json()["state"]

        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {"access_token": "acc_idem", "refresh_token": "ref_idem_111", "expires_in": 3600}

        with patch("routes.yoto_oauth.requests.post", return_value=mock_resp) as mock_post:
            self.assertEqual(self.app.get(f"/s/yoto-callback?code=idem_code&state={state}").status_code, 200)
            mock_post.assert_called_once()
            mock_post.reset_mock()
            fin_res = self.app.post("/api/admin/yoto/oauth/finish", json={"url_or_code": "idem_code", "state": state})
            self.assertEqual(fin_res.status_code, 200)
            self.assertTrue(fin_res.get_json()["ok"])
            mock_post.assert_not_called()

    def test_06_manual_paste_8787_callback_url(self):
        """Verifies manual copy-paste of http://127.0.0.1:8787/callback?code=... completes flow."""
        start_res = self.app.post(
            "/api/admin/yoto/oauth/start",
            json={"client_id": "pub_client_manual", "client_origin": "http://192.168.1.50:5055"},
        )
        start_data = start_res.get_json()
        state = start_data["state"]
        self.assertFalse(start_data["auto_callback"])

        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {"access_token": "acc_manual", "refresh_token": "ref_manual_xyz", "expires_in": 3600}

        with patch("routes.yoto_oauth.requests.post", return_value=mock_resp) as mock_post:
            fin_res = self.app.post(
                "/api/admin/yoto/oauth/finish",
                json={"url_or_code": f"http://127.0.0.1:8787/callback?code=manual_xyz&state={state}"},
            )

        self.assertEqual(fin_res.status_code, 200)
        self.assertTrue(fin_res.get_json()["ok"])
        sent_payload = mock_post.call_args.kwargs["json"]
        self.assertEqual(sent_payload["redirect_uri"], "http://127.0.0.1:8787/callback")
        self.assertNotIn("client_secret", sent_payload)
        self.assertEqual(database.get_api_credentials()["yoto_refresh_token"], "ref_manual_xyz")


class TestABSClientScopingUnit(IsolatedTestCase):
    """Mocked unit tests for modular clients/abs/{users,library,progress}.py."""

    def test_01_get_user_token_and_audio_only_filter(self):
        """Verifies get_user_token resolution and strict ABSClient.is_valid_audio_item filtering."""
        abs_cli = ABSClient("http://abs.test.invalid:13378", "master_key_unit")
        users_resp = MagicMock(status_code=200)
        users_resp.json.return_value = {
            "users": [
                {"id": "u1", "username": "child_a", "token": "child_a_jwt_tok_123456", "type": "user", "isActive": True},
                {"id": "u2", "username": "child_b", "token": "child_b_jwt_tok_123456", "type": "user", "isActive": True},
            ]
        }
        with patch("clients.abs_client.requests.get", return_value=users_resp):
            self.assertEqual(abs_cli.get_user_token("child_a"), "child_a_jwt_tok_123456")
            self.assertEqual(abs_cli.get_user_token("child_b"), "child_b_jwt_tok_123456")
            self.assertIsNone(abs_cli.get_user_token("nonexistent"))

        self.assertTrue(ABSClient.is_valid_audio_item({"mediaType": "book", "media": {"duration": 300.0, "numAudioFiles": 2, "metadata": {"title": "Audio Book"}}}))
        self.assertFalse(ABSClient.is_valid_audio_item({"mediaType": "book", "media": {"duration": 0.0, "numAudioFiles": 0, "numEbooks": 1, "metadata": {"title": "PDF Only"}}}))
        self.assertFalse(ABSClient.is_valid_audio_item({"isMissing": True, "mediaType": "book", "media": {"duration": 300.0, "numAudioFiles": 1, "metadata": {"title": "Missing Book"}}}))
        self.assertFalse(ABSClient.is_valid_audio_item({"mediaType": "book", "media": {"duration": 0.0, "numAudioFiles": 1, "metadata": {"title": "Zero Duration"}}}))

    def test_02_get_items_for_profile_scopes_with_user_token(self):
        """Verifies get_items_for_profile queries ABS with resolved child token and filters by audio."""
        abs_cli = ABSClient("http://abs.test.invalid:13378", "master_key_unit")
        abs_cli.get_user_token = MagicMock(return_value="child_scoped_tok")
        abs_cli.get_libraries = MagicMock(return_value=[{"id": "lib_books", "mediaType": "book"}])

        items_resp = MagicMock(status_code=200)
        items_resp.json.return_value = {
            "results": [
                {
                    "id": "b1",
                    "mediaType": "book",
                    "media": {
                        "duration": 1800.0,
                        "numAudioFiles": 1,
                        "tags": ["Children"],
                        "metadata": {"title": "Kid Audiobook", "authorName": "Author K"},
                    },
                },
                {
                    "id": "e1",
                    "mediaType": "book",
                    "media": {
                        "duration": 0.0,
                        "numAudioFiles": 0,
                        "numEbooks": 1,
                        "metadata": {"title": "Ebook Only", "authorName": "Author E"},
                    },
                },
            ]
        }
        with patch("clients.abs_client.requests.get", return_value=items_resp) as mock_get:
            scoped = abs_cli.get_items_for_profile({"abs_username": "child_a"})

        self.assertEqual(len(scoped), 1)
        self.assertEqual(scoped[0]["id"], "b1")
        self.assertEqual(mock_get.call_args.kwargs["headers"]["Authorization"], "Bearer child_scoped_tok")


class TestYotoClientUploadResilience(IsolatedTestCase):
    def _make_client(self):
        cli = YotoClient(client_id="cid_test", refresh_token="ref_test")
        cli.access_token = "tok_initial"
        cli.expires_at = time.time() + 3600.0
        return cli

    def test_01_upload_audio_survives_transient_dns_error_during_transcode_poll(self):
        """Transient ConnectionError (DNS failure) during /transcoded polling retries cleanly without aborting upload."""
        cli = self._make_client()
        upload_url_resp = MagicMock(status_code=200)
        upload_url_resp.json.return_value = {"upload": {"uploadUrl": "https://s3.test/put", "uploadId": "up_1"}}
        put_resp = MagicMock(status_code=200)
        poll_ok = MagicMock(status_code=200)
        poll_ok.json.return_value = {
            "transcode": {
                "transcodedSha256": "deadbeef1234567890abcdef12345678",
                "transcodedInfo": {"duration": 1800.0, "fileSize": 14400000, "channels": "mono", "format": "mp3"},
            }
        }
        dns_err = requests.exceptions.ConnectionError(
            "Failed to resolve 'api.yotoplay.com' ([Errno -5] No address associated with hostname)"
        )

        with patch("clients.yoto.playlists.requests.get", side_effect=[upload_url_resp, dns_err, dns_err, poll_ok]) as mock_get, \
             patch("clients.yoto.playlists.requests.put", return_value=put_resp) as mock_put, \
             patch("clients.yoto.playlists.time.sleep"):
            result = cli.upload_audio(b"ID3" + b"\x00" * 64, title="Castle Mystery Part 3")

        self.assertEqual(result["sha"], "deadbeef1234567890abcdef12345678")
        self.assertEqual(result["duration"], 1800.0)
        self.assertEqual(mock_put.call_count, 1)
        self.assertEqual(mock_get.call_count, 4)

    def test_02_upload_audio_retries_upload_url_put_and_401_refresh(self):
        """upload_audio recovers from transient uploadUrl ConnectionError, 503 S3 PUT, and 401 token expiry mid-poll."""
        cli = self._make_client()
        dns_err = requests.exceptions.ConnectionError("temporary DNS failure")
        upload_url_ok = MagicMock(status_code=200)
        upload_url_ok.json.return_value = {"upload": {"uploadUrl": "https://s3.test/put2", "uploadId": "up_2"}}
        put_503 = MagicMock(status_code=503, text="SlowDown")
        put_ok = MagicMock(status_code=200)
        poll_401 = MagicMock(status_code=401)
        poll_ok = MagicMock(status_code=200)
        poll_ok.json.return_value = {"transcode": {"transcodedSha256": "sha_after_refresh_1234567890"}}

        with patch.object(cli, "get_token", side_effect=["tok_initial", "tok_refreshed"]) as mock_tok, \
             patch("clients.yoto.playlists.requests.get", side_effect=[dns_err, upload_url_ok, poll_401, poll_ok]), \
             patch("clients.yoto.playlists.requests.put", side_effect=[put_503, put_ok]) as mock_put, \
             patch("clients.yoto.playlists.time.sleep"):
            result = cli.upload_audio(b"MP3BYTES", title="Track Recovery")

        self.assertEqual(result["sha"], "sha_after_refresh_1234567890")
        self.assertEqual(mock_put.call_count, 2)
        self.assertEqual(mock_tok.call_count, 2)
        self.assertTrue(mock_tok.call_args_list[1].kwargs.get("force_refresh"))

    def test_03_save_track_playlist_retries_on_transient_network_error(self):
        """save_track_playlist retries transient RequestException/5xx on POST /content and returns cardId."""
        cli = self._make_client()
        post_ok = MagicMock(status_code=200)
        post_ok.json.return_value = {"card": {"cardId": "AbCdE"}}
        tracks = [{"track_idx": 1, "title": "Ch 1", "sha": "sha1", "duration": 60.0, "fileSize": 1000}]

        with patch("clients.yoto.playlists.requests.post", side_effect=[requests.exceptions.ConnectionError("dns blip"), post_ok]) as mock_post, \
             patch("clients.yoto.playlists.time.sleep"):
            cid = cli.save_track_playlist("Mystery Series", tracks, card_id="AbCdE")

        self.assertEqual(cid, "AbCdE")
        self.assertEqual(mock_post.call_count, 2)


if __name__ == "__main__":
    unittest.main()

"""Unit tests for CI failure commit comment reporter and run_tests.py --json-report."""

import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import urllib.error
from unittest.mock import patch

from tests.harness import IsolatedTestCase


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
REPORTER_PATH = REPO_ROOT / ".github" / "scripts" / "post_ci_failure_comment.py"

_SPEC = importlib.util.spec_from_file_location("post_ci_failure_comment", str(REPORTER_PATH))
reporter = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(reporter)


class _FakeHttpResponse:
    def __init__(self, payload):
        self._raw = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class TestCIFailureReporter(IsolatedTestCase):
    def test_01_build_failure_comment_renders_unit_and_js_failures(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            report_file = Path(tmpdir) / "unit_report.json"
            js_file = Path(tmpdir) / "js_check.log"
            report_file.write_text(
                json.dumps(
                    {
                        "ok": False,
                        "tests_run": 117,
                        "elapsed_sec": 4.2,
                        "skipped": 0,
                        "failures": [
                            {
                                "test": "tests.unit.test_example.TestFoo.test_bar",
                                "kind": "FAIL",
                                "traceback": "AssertionError: 1 != 2",
                            }
                        ],
                        "errors": [
                            {
                                "test": "tests.unit.test_example.TestFoo.test_baz",
                                "kind": "ERROR",
                                "traceback": "RuntimeError: boom",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            js_file.write_text("static/kiosk/js/app.js:10\nSyntaxError: Unexpected token\n", encoding="utf-8")

            body = reporter.build_failure_comment(
                unit_outcome="failure",
                unit_report_path=str(report_file),
                js_outcome="failure",
                js_log_path=str(js_file),
                branch="dev",
                sha="abcdef1234567",
                run_url="https://github.com/YotoStoryDeck/StoryDeck-Core/actions/runs/999",
            )

        self.assertTrue(body.startswith(reporter.MARKER))
        self.assertIn("1 failure(s), 1 error(s) out of 117 tests", body)
        self.assertIn("ERROR: tests.unit.test_example.TestFoo.test_baz", body)
        self.assertIn("FAIL: tests.unit.test_example.TestFoo.test_bar", body)
        self.assertIn("JS SYNTAX ERROR", body)
        self.assertIn("SyntaxError: Unexpected token", body)
        self.assertIn("actions/runs/999", body)

    def test_02_build_failure_comment_handles_missing_report_and_truncates_safely(self):
        fallback_body = reporter.build_failure_comment(
            unit_outcome="failure",
            unit_report_path=None,
            js_outcome="success",
            js_log_path=None,
            branch="dev",
            sha="1234567",
            run_url="https://github.com/YotoStoryDeck/StoryDeck-Core/actions/runs/111",
        )
        self.assertIn("before writing a JSON report", fallback_body)

        with tempfile.TemporaryDirectory() as tmpdir:
            report_file = Path(tmpdir) / "huge_report.json"
            huge_tb = "Traceback line\n" * 1500
            report_file.write_text(
                json.dumps(
                    {
                        "ok": False,
                        "tests_run": 10,
                        "elapsed_sec": 1.0,
                        "failures": [
                            {"test": f"test_{i}", "kind": "FAIL", "traceback": huge_tb}
                            for i in range(12)
                        ],
                        "errors": [],
                    }
                ),
                encoding="utf-8",
            )
            oversized = reporter.build_failure_comment(
                unit_outcome="failure",
                unit_report_path=str(report_file),
                js_outcome="success",
                js_log_path=None,
                branch="dev",
                sha="1234567",
                run_url="https://github.com/YotoStoryDeck/StoryDeck-Core/actions/runs/222",
            )

        self.assertLessEqual(len(oversized), reporter.MAX_BODY_CHARS)
        self.assertIn(reporter.MARKER, oversized)
        self.assertIn("actions/runs/222", oversized)
        self.assertIn("omitted to fit comment limit", oversized)

    def test_03_post_creates_when_absent_and_patches_when_marker_exists(self):
        calls = []

        def fake_urlopen(req, timeout=20):
            calls.append((req.get_method(), req.full_url, req.data))
            if req.get_method() == "GET":
                return _FakeHttpResponse([])
            return _FakeHttpResponse({"id": 42})

        with patch.object(reporter.urllib.request, "urlopen", side_effect=fake_urlopen):
            res_create = reporter.post_or_update_commit_comment(
                "YotoStoryDeck/StoryDeck-Core",
                "abcdef1234567",
                f"{reporter.MARKER}\nFailed test",
                "fake_token",
            )

        self.assertEqual(res_create, "created")
        self.assertEqual([c[0] for c in calls], ["GET", "POST"])
        self.assertTrue(calls[1][1].endswith("/repos/YotoStoryDeck/StoryDeck-Core/commits/abcdef1234567/comments"))

        calls.clear()

        def fake_urlopen_existing(req, timeout=20):
            calls.append((req.get_method(), req.full_url, req.data))
            if req.get_method() == "GET":
                return _FakeHttpResponse([{"id": 777, "body": f"{reporter.MARKER}\nOld run"}])
            return _FakeHttpResponse({"id": 777})

        with patch.object(reporter.urllib.request, "urlopen", side_effect=fake_urlopen_existing):
            res_patch = reporter.post_or_update_commit_comment(
                "YotoStoryDeck/StoryDeck-Core",
                "abcdef1234567",
                f"{reporter.MARKER}\nNew run",
                "fake_token",
            )

        self.assertEqual(res_patch, "updated")
        self.assertEqual([c[0] for c in calls], ["GET", "PATCH"])
        self.assertTrue(calls[1][1].endswith("/repos/YotoStoryDeck/StoryDeck-Core/comments/777"))

    def test_04_missing_token_or_http_403_returns_gracefully(self):
        self.assertEqual(
            reporter.post_or_update_commit_comment("owner/repo", "abcdef1", "body", ""),
            "skipped_no_token",
        )

        http_err = urllib.error.HTTPError(
            url="https://api.github.com/repos/owner/repo/commits/abcdef1/comments",
            code=403,
            msg="Forbidden",
            hdrs=None,
            fp=io.BytesIO(b"{}"),
        )
        with patch.object(reporter.urllib.request, "urlopen", side_effect=http_err):
            res = reporter.post_or_update_commit_comment("owner/repo", "abcdef1", "body", "tok")
        self.assertEqual(res, "error_http_403")

    def test_05_run_tests_json_report_flag_outputs_valid_schema(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out_json = Path(tmpdir) / "report.json"
            proc = subprocess.run(
                [
                    sys.executable,
                    "scripts/run_tests.py",
                    "--unit-only",
                    "-k",
                    "test_01_network_guard_blocks_unmocked_outbound_http_immediately",
                    "--json-report",
                    str(out_json),
                ],
                cwd=str(REPO_ROOT),
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue(out_json.is_file())
            parsed = json.loads(out_json.read_text(encoding="utf-8"))
            for key in ("ok", "tests_run", "elapsed_sec", "skipped", "failures", "errors"):
                self.assertIn(key, parsed)
            self.assertTrue(parsed["ok"])
            self.assertGreaterEqual(parsed["tests_run"], 1)

#!/usr/bin/env python3
"""Post or update a structured GitHub commit comment when StoryDeck Core CI fails."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional


MARKER = "<!-- storydeck-ci-failure -->"
MAX_BODY_CHARS = 24000
MAX_ITEM_CHARS = 3500
API_VERSION = "2022-11-28"


def _sanitize_fence(text: str, limit: int = MAX_ITEM_CHARS) -> str:
    cleaned = (text or "").strip().replace("```", "'''")
    if len(cleaned) > limit:
        cleaned = cleaned[:limit].rstrip() + "\n... (truncated; see full CI logs)"
    return cleaned


def _load_unit_report(report_path: Optional[str]) -> Optional[Dict[str, Any]]:
    if not report_path:
        return None
    p = Path(report_path)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _read_text_file(path: Optional[str]) -> str:
    if not path:
        return ""
    p = Path(path)
    if not p.is_file():
        return ""
    try:
        return p.read_text(encoding="utf-8", errors="replace").strip()
    except Exception:
        return ""


def build_failure_comment(
    *,
    unit_outcome: str,
    unit_report_path: Optional[str],
    js_outcome: str,
    js_log_path: Optional[str],
    branch: str,
    sha: str,
    run_url: str,
) -> str:
    """Render a compact Markdown failure report capped safely below GitHub's limit."""
    short_sha = (sha or "")[:7] or "unknown"
    report = _load_unit_report(unit_report_path)
    js_log = _read_text_file(js_log_path)

    unit_status = "✅ Passed" if unit_outcome == "success" else f"❌ Failed (`{unit_outcome}`)"
    if report is not None:
        fails = report.get("failures") or []
        errs = report.get("errors") or []
        total_run = int(report.get("tests_run") or 0)
        if unit_outcome != "success" or fails or errs:
            unit_status = f"❌ Failed ({len(fails)} failure(s), {len(errs)} error(s) out of {total_run} tests)"
        else:
            unit_status = f"✅ Passed ({total_run} tests)"

    js_status = "✅ Passed" if js_outcome == "success" else f"❌ Failed (`{js_outcome}`)"

    header_lines: List[str] = [
        MARKER,
        "### ❌ StoryDeck Core CI Failed",
        "",
        "| Check | Outcome | Details |",
        "| :--- | :--- | :--- |",
        f"| **Unit Tests (`--unit-only`)** | {unit_status} | Branch `{branch}` (`{short_sha}`) |",
        f"| **Frontend JS Syntax (`node --check`)** | {js_status} | [View full workflow run]({run_url}) |",
        "",
    ]

    sections: List[str] = []

    if unit_outcome != "success":
        if report is None:
            sections.append(
                "> ⚠️ The unit test step failed before writing a JSON report "
                f"(possible import, setup, or environment failure). See the [full workflow run]({run_url})."
            )
            sections.append("")
        else:
            combined = list(report.get("errors") or []) + list(report.get("failures") or [])
            for entry in combined:
                kind = str(entry.get("kind") or "FAIL")
                test_name = str(entry.get("test") or "unknown_test")
                tb = _sanitize_fence(str(entry.get("traceback") or "(no traceback recorded)"))
                sections.extend(
                    [
                        f"<details><summary><code>{kind}: {test_name}</code></summary>",
                        "",
                        "```text",
                        tb,
                        "```",
                        "",
                        "</details>",
                        "",
                    ]
                )

    if js_outcome != "success":
        js_body = _sanitize_fence(js_log) if js_log else f"(No stderr captured; inspect {run_url})"
        sections.extend(
            [
                "<details><summary><code>JS SYNTAX ERROR (node --check)</code></summary>",
                "",
                "```text",
                js_body,
                "```",
                "",
                "</details>",
                "",
            ]
        )

    header = "\n".join(header_lines)
    footer = f"\n<sub>Posted automatically by StoryDeck Core CI · [Workflow Run]({run_url})</sub>\n"
    trunc_note = f"\n\n> ⚠️ Additional failure details omitted to fit comment limit — see [full workflow run]({run_url})."

    body = header + "\n".join(sections)
    if len(body) + len(footer) > MAX_BODY_CHARS:
        budget = max(0, MAX_BODY_CHARS - len(footer) - len(trunc_note))
        body = body[:budget].rstrip() + trunc_note

    return (body + footer).strip() + "\n"


def write_step_summary(markdown_body: str, summary_path: Optional[str] = None) -> bool:
    """Mirror the rendered failure report to GitHub Actions Step Summary when set."""
    target = summary_path if summary_path is not None else os.getenv("GITHUB_STEP_SUMMARY", "")
    if not target:
        return False
    try:
        with open(target, "a", encoding="utf-8") as fh:
            fh.write("\n" + markdown_body + "\n")
        return True
    except Exception as exc:
        print(f"Warning: failed writing GITHUB_STEP_SUMMARY: {exc}", file=sys.stderr)
        return False


def _github_request(url: str, token: str, method: str = "GET", payload: Optional[Dict[str, Any]] = None) -> Any:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": API_VERSION,
        "User-Agent": "storydeck-core-ci-reporter",
    }
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=20) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else {}


def post_or_update_commit_comment(repo: str, sha: str, body: str, token: str) -> str:
    """Create a commit comment or update the existing CI marker comment in-place."""
    if not token:
        print("Warning: GITHUB_TOKEN not set; skipping commit comment post.", file=sys.stderr)
        return "skipped_no_token"
    if not repo or not sha:
        print("Warning: repo or commit sha missing; skipping commit comment post.", file=sys.stderr)
        return "skipped_missing_target"

    comments_url = f"https://api.github.com/repos/{repo}/commits/{sha}/comments?per_page=100"
    try:
        existing_comments = _github_request(comments_url, token, method="GET") or []
        existing_id = None
        for comment in existing_comments:
            if isinstance(comment, dict) and MARKER in str(comment.get("body") or ""):
                existing_id = comment.get("id")
                break

        if existing_id is not None:
            patch_url = f"https://api.github.com/repos/{repo}/comments/{existing_id}"
            _github_request(patch_url, token, method="PATCH", payload={"body": body})
            print(f"Updated existing CI failure comment #{existing_id} on {sha[:7]}.")
            return "updated"

        post_url = f"https://api.github.com/repos/{repo}/commits/{sha}/comments"
        created = _github_request(post_url, token, method="POST", payload={"body": body}) or {}
        created_id = created.get("id", "?")
        print(f"Posted CI failure comment #{created_id} on {sha[:7]}.")
        return "created"
    except urllib.error.HTTPError as exc:
        print(
            f"Warning: GitHub commit comment API returned HTTP {exc.code}; continuing so CI fails on test error.",
            file=sys.stderr,
        )
        return f"error_http_{exc.code}"
    except urllib.error.URLError as exc:
        print(
            f"Warning: Network error contacting GitHub API ({exc.reason}); continuing so CI fails on test error.",
            file=sys.stderr,
        )
        return "error_network"


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Post or update a commit comment on CI test failure.")
    parser.add_argument("--repo", default=os.getenv("GITHUB_REPOSITORY", ""), help="owner/repo")
    parser.add_argument("--sha", default=os.getenv("GITHUB_SHA", ""), help="Target commit SHA")
    parser.add_argument("--branch", default=os.getenv("GITHUB_REF_NAME", "unknown"), help="Branch name")
    parser.add_argument("--run-url", default="", help="URL to the failing GitHub Actions run")
    parser.add_argument("--unit-outcome", default="failure", help="Unit test step outcome")
    parser.add_argument("--unit-report", default="", help="Path to run_tests.py --json-report output")
    parser.add_argument("--js-outcome", default="success", help="JS syntax check step outcome")
    parser.add_argument("--js-log", default="", help="Path to node --check stderr capture")
    parser.add_argument("--dry-run", action="store_true", help="Print Markdown body without calling GitHub API")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    run_url = args.run_url or f"https://github.com/{args.repo}/actions"
    body = build_failure_comment(
        unit_outcome=args.unit_outcome,
        unit_report_path=args.unit_report or None,
        js_outcome=args.js_outcome,
        js_log_path=args.js_log or None,
        branch=args.branch,
        sha=args.sha,
        run_url=run_url,
    )
    write_step_summary(body)
    if args.dry_run:
        print(body)
        return 0
    post_or_update_commit_comment(
        repo=args.repo,
        sha=args.sha,
        body=body,
        token=os.getenv("GITHUB_TOKEN", ""),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

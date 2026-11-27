# StoryDeck Core Test Suite & Authoring Guide

This directory contains the test architecture for **StoryDeck Core**, designed around three core guarantees:

1. **Zero external dependencies by default** — running the unit suite requires no real Audiobookshelf server, no Yoto cloud credentials, and no local `data/broker.db`.
2. **Hardened process isolation** — outbound HTTP connections are blocked at the socket/urllib3 pool layer by `NetworkGuard`, and all SQLite operations execute inside an ephemeral temporary directory.
3. **Optional read-only local database snapshots** — developers with a local `data/broker.db` can run realistic migration, curation, and bridge validation tests against an in-temp sanitized clone without ever touching or exposing real credentials.

---

## 1. Directory Layout

```text
tests/
├── README.md                         <- This guide
├── test_isolation.py                 <- Backward-compatible entrypoint forwarding to tests.harness
├── harness/
│   ├── __init__.py
│   ├── base.py                       <- IsolatedTestCase & SnapshotTestCase base classes
│   ├── fixtures.py                   <- Synthetic DB seed helpers (users, profiles, media, devices)
│   ├── isolation.py                  <- Temp DB rebinding, runtime cache clearing, legacy env guard
│   ├── network_guard.py              <- Outbound HTTP/urllib3 blocker with explicit allow_live() gate
│   └── snapshot.py                   <- Read-only SQLite backup + mandatory PII/secret scrubber
├── unit/                             <- Fast, 100% synthetic domain unit tests (CI default)
│   ├── test_admin_profiles_settings_tracks.py
│   ├── test_auth_rbac.py
│   ├── test_bridge_cards_and_icons.py
│   ├── test_bridge_offline_and_pruner.py
│   ├── test_bridge_streaming_playback.py
│   ├── test_ci_failure_reporter.py
│   ├── test_clients_abs_and_yoto.py
│   ├── test_curation_and_series.py
│   ├── test_devices_ota_and_cache.py
│   ├── test_portal_kiosk_and_player.py
│   ├── test_security_and_harness.py
│   ├── test_sso_and_ui.py
│   └── test_user_abs_linking.py
├── snapshot/                         <- Opt-in tests against sanitized local DB snapshots
│   ├── test_snapshot_bridge_and_security.py
│   ├── test_snapshot_curation_and_kiosk.py
│   └── test_snapshot_migrations_and_integrity.py
└── live/                             <- Opt-in tests requiring a real ABS test server
    └── test_live_abs_scoping.py
```

---

## 2. Running Tests

Use the unified CLI runner `scripts/run_tests.py`:

```bash
# Default CI / contributor mode: 100% synthetic unit suite, zero local DB, zero network
python scripts/run_tests.py --unit-only -v

# Unit suite + sanitized read-only snapshot tests using data/broker.db
python scripts/run_tests.py --with-local-db -v

# Snapshot suite only against an explicit DB path
python scripts/run_tests.py --snapshot-only --local-db data/broker.db -v

# Filter specific tests and stop on first failure
python scripts/run_tests.py -k curation -f -v
```

Standard `unittest` discovery also works:

```bash
python -m unittest discover -s tests -t . -v
```

*(When `STORYDECK_SNAPSHOT_DB` and `STORYDECK_LIVE_ABS_TESTS` are unset, `tests/snapshot/` and `tests/live/` skip cleanly so `unittest discover` passes out of the box on clean checkouts.)*

---

## 3. Rules for Writing Atomic Tests

Every new test file added to `tests/unit/` MUST follow these rules:

1. **Inherit from `IsolatedTestCase`** (from `tests.harness`):
   - Automatically ensures `ensure_isolated_test_env()`, `NetworkGuard.install()`, and `reset_test_db()` run in `setUpClass`.
   - Set `reset_db_per_test = True` on test classes whose methods mutate shared tables without explicit per-test IDs, or create unique IDs per test method.
2. **Never bypass `NetworkGuard` in unit tests**:
   - All `requests` / `urllib3` calls (including `abs_cli.get_item`, `yoto_client`, webhook alerts, firmware checks) MUST be patched with `unittest.mock.patch` at the module import site or mock helper boundary.
   - If a test triggers a background HTTP request or unmocked retry loop, `NetworkGuard` fails immediately in milliseconds rather than waiting 5–30 seconds on TCP retries.
3. **Never write to `data/broker.db` or assume live env vars**:
   - `STORYDECK_USE_LIVE_DB=1` is automatically neutralized by `tests.harness.isolation` so unit runs cannot accidentally migrate or overwrite a developer's database.
4. **Reset singletons and caches cleanly**:
   - `reset_test_db()` wipes SQLite tables, clears `clients.abs_client` / `clients.yoto_client` / `clients.yoto_oauth_client`, resets `core.bridge.quiet._quiet_spots`, and clears rate-limit buckets. If you introduce a new module-level singleton cache in production code, reset it inside `tests/harness/isolation.py:reset_runtime_caches()`.
5. **Keep tests focused and independent (FIRST)**:
   - Avoid numeric test-name ordering dependencies (`test_01_` depending on side effects from `test_02_`) unless all assertions share a clear workflow class; prefer self-contained fixtures from `tests.harness.fixtures`.

---

## 4. How Optional Local DB Snapshot Sanitization Works

When `--with-local-db` (or `STORYDECK_SNAPSHOT_DB=1`) is enabled, `SnapshotTestCase` runs the following pipeline in `setUpClass()`:

1. **Read-Only Copy (`?mode=ro`)**: Connects to the source SQLite database via SQLite's read-only URI mode and streams a copy via `sqlite3.Connection.backup()` into a temporary file inside the test isolation directory. Source files (`data/broker.db`, `-wal`, `-shm`) are never opened for writing, migrated, or touched (`mtime` and SHA-256 remain identical).
2. **Comprehensive PII & Credential Scrubbing (`sanitize_snapshot_db`)**:
   - **`system_settings`**: Blanks API keys, OAuth secrets, refresh/access tokens, session secrets, Yoto/ABS tokens, AI keys, SSO secrets, and query-string tokens; replaces `abs_base_url`, `yoto_bridge_url`, and OIDC endpoints with `.test.invalid` / `.example.test` hostnames.
   - **`admin_users`**: Replaces usernames (`test_user_<n>`), display names (`Test User <n>`), and emails (`user_<n>@example.test`), resets password hashes/salts to a shared non-production test hash (`TEST_SNAPSHOT_PASSWORD`), and clears `sso_subject`, `abs_user_id`, `abs_username`, and `abs_api_key`.
   - **`profiles`**: Pseudonymizes `display_name` (`Profile <n>`) and clears `abs_username` and `abs_api_key` while preserving row IDs for foreign-key joins.
   - **`esp32_devices`**: Replaces `friendly_name` (`Test Device <n>`) and LAN IPs with RFC 5737 test documentation addresses (`198.51.100.<n>`), resets `jwt_secret` to deterministic test tokens, and resets `parent_pin` to `"1234"`.
   - **`media_items` / `bridge_tracks`**: Redacts query-string tokens in `cover_url` / `raw_metadata` and rewrites hosts in `bridge_tracks.stream_url` to `http://storydeck.test.invalid/` while preserving catalog and chapter layout shapes.
   - **Master API key**: Installs a known deterministic key (`TEST_MASTER_API_KEY`) strictly in the temporary snapshot for API route testing.
3. **Fail-Closed Leak Assertions**: Snapshot tests explicitly scan sanitized SQLite tables and Flask JSON responses to confirm zero emails outside `@example.test`, zero real password hashes, zero JWTs/API keys, and zero RFC1918 LAN IPs ever appear.

---

## 5. Lessons Learned from Overhauling the Legacy Suite

The original flat `tests/test_*.py` suite suffered from chronic slowness (~83 seconds) and subtle flakiness. The following concrete engineering lessons drove the redesign:

### Lesson 1: Unmocked HTTP Retries Accounted for ~87% of Suite Runtime
**Symptom:** Four unit files (`test_bridge_engine.py`, `test_user_abs_linking.py`, `test_refactor_standalone_fixes.py`, `test_card_pairing.py`) accounted for **72.3 seconds of an 82.95-second total runtime**.
**Root Cause:** Production clients (`requests` + `urllib3.Retry`) silently retried DNS resolution failures against unreachable URLs like `http://audiobookshelf:80` or `http://127.0.0.1:13378` because HTTP was not blocked globally in tests.
**Fix:** Installing `NetworkGuard` (`HTTPConnectionPool.urlopen` hook) immediately raises `NetworkBlockedInTestsError` on any unmocked HTTP call. Fixing the missing mocks cut the full unit suite runtime from ~83s down to **~4–5 seconds on Linux CI (~14–16 seconds on Windows)**.

### Lesson 2: SQLite Read-Only Backup Protects Both Developers and Migrations
**Symptom:** A legacy `STORYDECK_USE_LIVE_DB=1` toggle could point `database.DB_PATH` directly at `data/broker.db`, causing `database.init_db()` to mutate schema version (`PRAGMA user_version`) or wipe/seed test rows right in the live local DB.
**Fix:** `tests/harness/isolation.py` neutralizes `STORYDECK_USE_LIVE_DB=1` and redirects snapshot testing through `copy_sqlite_readonly()` (`?mode=ro` + `sqlite3.backup()`). Even schema migrations run exclusively on the disposable sanitized clone.

### Lesson 3: Module-Level Re-Bindings Create Ghost DB Bugs
**Symptom:** Overriding `database.DB_PATH` alone failed when submodules used `from database.connection import DB_PATH` or cached settings/singletons (`clients.abs_client`, `core.bridge.quiet._quiet_spots`).
**Fix:** `rebind_test_db()` updates `DB_PATH` across `database`, `database.connection`, `database.schema`, and `database.settings`, while `reset_runtime_caches()` explicitly clears runtime singletons and auth rate-limiter state between suites.

### Lesson 4: Order-Dependent Database Pollution Breaks When Tests Are Filtered
**Symptom:** Running `test_ui_overhaul_and_sso.py` alone passed or failed differently depending on execution order because prior tests left rows in `admin_users` or `profiles`, triggering redirect logic on `/login` vs `/setup`.
**Fix:** Every test class now inherits `IsolatedTestCase`, ensuring a clean schema reset at class setup and optional `reset_db_per_test = True` for stateful endpoint flows.

### Lesson 5: Patch Targets Must Match Call-Site Imports
**Symptom:** Patching `requests.get` or `requests.post` missed `routes/admin_device_cache.py` because it called `requests.request(...)`, and patching a package attribute missed modules that imported a symbol with `from mod import fn`.
**Fix:** Mock tests explicitly patch `requests.request` (or the imported reference inside the module under test), verified end-to-end with `NetworkGuard` active so any missed patch fails loudly instead of hanging or hitting localhost.

### Lesson 6: Sanitization Must Scrub Before Any Flask Handler Runs
**Symptom:** Even when testing against a copy of `broker.db`, booting the Flask `app` reads `system_settings` and initializes clients or SSO config immediately if endpoints hit auth/config paths.
**Fix:** `SnapshotTestCase.setUpClass()` scrubs the temporary copy *before* rebinding `DB_PATH` or calling `database.init_db()`, ensuring Flask never observes a single real credential, webhook URL, or email address.

### Lesson 7: Keep Test Suites Domain-Aligned and Standard-Library Clean
**Symptom:** Phase-named test files (`test_phase2_...`, `test_phase3_...`, `test_refactor_standalone_fixes.py`) made it unclear which production module each test owned, and tests requiring non-installed third-party runners (`pytest`) broke contributors.
**Fix:** Tests are grouped by production domain (`auth_rbac`, `curation_and_series`, `bridge_streaming_playback`, `portal_kiosk_and_player`, etc.) and written purely with Python standard library `unittest` + `unittest.mock`, matching `requirements.txt`.

### Lesson 8: Enforce Target Python 3.11 Syntax & System Binaries (`ffmpeg`) in CI
**Symptom:** Local tests on Python 3.12 passed, while GitHub Actions on Python 3.11 (`ubuntu-latest`) failed on two environment differences: (1) a PEP 701 backslash inside an f-string expression (`f"...{html.count(f'id=\"{x}\"')}"`) threw `SyntaxError` on Python 3.11 (and even bypassed Python 3.12's `ast.parse(..., feature_version=(3, 11))` check), and (2) `ffmpeg` is not preinstalled on `ubuntu-latest`, causing `test_03_ffmpeg_silencedetect_and_quiet_spots_snapping` in `tests/unit/test_bridge_offline_and_pruner.py` to fail.
**Fix:** Bind intermediate strings/counts outside f-string `{...}` expressions (never embed backslashes or same-quote reuse inside f-string expressions), and explicitly install `ffmpeg` via `apt-get` in `.github/workflows/ci.yml` before invoking the unit test suite.

---

## 6. Continuous Integration (`.github/workflows/ci.yml`)

Every push and pull request targeting `main` or `dev` executes the `unit-tests` job on `ubuntu-latest` using Node 24-native action majors (`actions/checkout@v7`, `actions/setup-python@v7`, `actions/setup-node@v7`):

1. Sets up **Python 3.11** and installs dependencies from `requirements.txt`.
2. Installs **`ffmpeg`** via `apt-get install -y --no-install-recommends ffmpeg` for real audio silence-detection coverage.
3. Runs `python scripts/run_tests.py --unit-only -v --json-report /tmp/unit_report.json` (126 synthetic unit tests, `NetworkGuard` enabled, zero local DB).
4. Sets up **Node 24** (`node-version: "24"`) and runs `node --check` over every file in `static/kiosk/js/*.js` and `static/js/*.js`.
5. On any test or JS syntax failure (for pushes and same-repo PRs), `.github/scripts/post_ci_failure_comment.py` (with `permissions: contents: write`) mirrors the Markdown report to `$GITHUB_STEP_SUMMARY` and creates or updates a single `<!-- storydeck-ci-failure -->` commit comment on the triggering commit before failing the job.

*(Release packaging in `.github/workflows/release.yml` likewise uses Node 24-native action majors: `actions/checkout@v7`, `softprops/action-gh-release@v3`, `docker/setup-buildx-action@v4`, `docker/login-action@v4`, and `docker/build-push-action@v7`.)*

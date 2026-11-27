"""Unified CLI runner for StoryDeck Core unit, snapshot, and live test suites."""

import argparse
import os
import sys
import time
import unittest
from collections import defaultdict
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent


class TimedTextTestResult(unittest.TextTestResult):
    """TestResult that records elapsed seconds per test module."""

    def __init__(self, stream, descriptions, verbosity):
        super().__init__(stream, descriptions, verbosity)
        self.module_times = defaultdict(float)
        self._test_start = 0.0

    def startTest(self, test):
        self._test_start = time.perf_counter()
        super().startTest(test)

    def stopTest(self, test):
        elapsed = time.perf_counter() - self._test_start
        module_name = getattr(type(test), "__module__", "unknown")
        self.module_times[module_name] += elapsed
        super().stopTest(test)


class TimedTextTestRunner(unittest.TextTestRunner):
    """TextTestRunner using TimedTextTestResult."""

    resultclass = TimedTextTestResult


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Run StoryDeck Core unit, sanitized snapshot, or opt-in live tests."
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--unit-only",
        action="store_true",
        help="Run fast synthetic unit tests only (default; never touches data/broker.db).",
    )
    mode_group.add_argument(
        "--with-local-db",
        action="store_true",
        help="Run unit tests plus sanitized read-only snapshot tests against data/broker.db.",
    )
    mode_group.add_argument(
        "--snapshot-only",
        action="store_true",
        help="Run only sanitized local DB snapshot tests.",
    )
    mode_group.add_argument(
        "--live",
        action="store_true",
        help="Run opt-in live ABS integration tests (requires STORYDECK_LIVE_ABS_TESTS=1).",
    )
    parser.add_argument(
        "--local-db",
        default=None,
        help="Optional path to a local SQLite DB to clone and sanitize for snapshot tests.",
    )
    parser.add_argument(
        "-k",
        dest="pattern",
        default=None,
        help="Only run tests whose names match this substring pattern.",
    )
    parser.add_argument(
        "-f",
        "--failfast",
        action="store_true",
        help="Stop the test run on the first error or failure.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Verbose test output.",
    )
    return parser.parse_args(argv)


def _configure_environment(args):
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))

    # Never inherit an unexpected live DB toggle in unit-only runs.
    os.environ.pop("STORYDECK_USE_LIVE_DB", None)

    if args.local_db:
        if not args.snapshot_only:
            args.with_local_db = True
        os.environ["STORYDECK_SNAPSHOT_DB"] = str(Path(args.local_db).resolve())
    elif args.with_local_db or args.snapshot_only:
        os.environ["STORYDECK_SNAPSHOT_DB"] = "1"
    else:
        os.environ.pop("STORYDECK_SNAPSHOT_DB", None)

    if args.live:
        os.environ["STORYDECK_LIVE_ABS_TESTS"] = "1"

    from tests.harness import NetworkGuard, ensure_isolated_test_env, get_isolation_root
    from tests.harness.snapshot import resolve_snapshot_source

    ensure_isolated_test_env()
    NetworkGuard.install()

    if args.local_db and not Path(args.local_db).is_file():
        print(f"ERROR: --local-db file does not exist: {args.local_db}", file=sys.stderr)
        sys.exit(2)

    if (args.with_local_db or args.snapshot_only) and not resolve_snapshot_source():
        print(
            "ERROR: Snapshot mode requested, but no local database was found "
            "(checked --local-db, STORYDECK_SNAPSHOT_SOURCE, and data/broker.db).",
            file=sys.stderr,
        )
        sys.exit(2)

    return get_isolation_root(), resolve_snapshot_source()


def _print_banner(args, isolation_root, snapshot_source):
    if args.live:
        mode = "LIVE INTEGRATION (explicit opt-in)"
    elif args.snapshot_only:
        mode = "SNAPSHOT ONLY (read-only sanitized local DB clone)"
    elif args.with_local_db:
        mode = "UNIT + SNAPSHOT (synthetic unit + read-only sanitized local DB clone)"
    else:
        mode = "UNIT ONLY (100% synthetic, zero local DB, zero network)"

    snap_label = (
        str(snapshot_source)
        if (args.with_local_db or args.snapshot_only) and snapshot_source
        else "DISABLED"
    )
    print("=" * 72)
    print(f"StoryDeck Core Test Runner")
    print(f"  Mode           : {mode}")
    print(f"  Isolation Root : {isolation_root}")
    print(f"  Network Guard  : {'INSTALLED (outbound HTTP blocked)' if not args.live else 'ALLOW_LIVE opt-in available'}")
    print(f"  Snapshot Source: {snap_label}")
    print("=" * 72)


def main(argv=None):
    args = parse_args(argv)
    isolation_root, snapshot_source = _configure_environment(args)
    _print_banner(args, isolation_root, snapshot_source)

    loader = unittest.TestLoader()
    if args.pattern:
        loader.testNamePatterns = [f"*{args.pattern}*"]

    suite = unittest.TestSuite()
    if args.live:
        suite.addTests(loader.discover(start_dir=str(REPO_ROOT / "tests" / "live"), top_level_dir=str(REPO_ROOT)))
    elif args.snapshot_only:
        suite.addTests(loader.discover(start_dir=str(REPO_ROOT / "tests" / "snapshot"), top_level_dir=str(REPO_ROOT)))
    elif args.with_local_db:
        suite.addTests(loader.discover(start_dir=str(REPO_ROOT / "tests" / "unit"), top_level_dir=str(REPO_ROOT)))
        suite.addTests(loader.discover(start_dir=str(REPO_ROOT / "tests" / "snapshot"), top_level_dir=str(REPO_ROOT)))
    else:
        suite.addTests(loader.discover(start_dir=str(REPO_ROOT / "tests" / "unit"), top_level_dir=str(REPO_ROOT)))

    runner = TimedTextTestRunner(
        verbosity=2 if args.verbose else 1,
        failfast=args.failfast,
    )
    t0 = time.perf_counter()
    result = runner.run(suite)
    total = time.perf_counter() - t0

    print("\n" + "-" * 72)
    print(f"Module Timings (total {total:.2f}s):")
    for mod, sec in sorted(result.module_times.items(), key=lambda kv: -kv[1]):
        print(f"  {sec:6.2f}s  {mod}")
    print("-" * 72)

    if result.wasSuccessful():
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

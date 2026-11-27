"""Backwards-compatible test isolation bootstrap shim.

Delegates to ``tests.harness.isolation`` so legacy imports of
``tests.test_isolation`` automatically receive full SQLite sandboxing and
``NetworkGuard`` protection without ever mutating ``data/broker.db``.
"""
from tests.harness.isolation import ensure_isolated_test_env

ensure_isolated_test_env()

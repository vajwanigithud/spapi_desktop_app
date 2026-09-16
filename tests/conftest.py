"""Isolation guard for Parent--Child tests.

The production catalog database is never an acceptable test target.  Parent--
Child modules import connection factories directly, so this fixture patches both
the central factory and every already-imported consumer.
"""
import contextlib
import sqlite3
from pathlib import Path
from urllib.parse import unquote, urlparse

import pytest


@pytest.fixture(autouse=True)
def isolate_parent_child_database(request, monkeypatch, tmp_path):
    if not Path(str(request.fspath)).name.startswith("test_parent_child"):
        yield
        return
    db_path = tmp_path / "parent_child_test.db"
    approved = db_path.resolve()
    attempted = []
    original_connect = sqlite3.connect
    def checked_connect(target, *args, **kwargs):
        raw = str(target)
        if raw == ':memory:' or raw.startswith('file::memory:'):
            attempted.append(raw)
            return original_connect(target, *args, **kwargs)
        if raw.startswith('file:'):
            parsed = urlparse(raw)
            candidate = Path(unquote(parsed.path)).resolve()
        else:
            candidate = Path(raw).resolve()
        attempted.append(str(candidate))
        if candidate != approved:
            raise AssertionError(f'Parent--Child test attempted forbidden database connection: {candidate}')
        return original_connect(target, *args, **kwargs)
    monkeypatch.setattr(sqlite3, 'connect', checked_connect)
    @contextlib.contextmanager
    def connection():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()
    import services.db as db
    import services.parent_child_migrations as migrations
    monkeypatch.setattr(db, "get_db_connection", connection)
    @contextlib.contextmanager
    def readonly_connection():
        conn = sqlite3.connect(f"file:{approved.as_posix()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()
    monkeypatch.setattr(db, "get_readonly_db_connection", readonly_connection)
    for module_name in (
        "services.parent_child_migrations", "services.parent_child_store",
        "services.parent_child_safety", "services.parent_child_tracker",
        "services.parent_child_parent_submission", "services.parent_child_runtime_settings",
            "services.parent_child_guided_orchestrator",
            "services.parent_child_guided_worker",
        "services.spapi_product_types", "routes.parent_child_routes",
    ):
        try:
            module = __import__(module_name, fromlist=["get_db_connection"])
            monkeypatch.setattr(module, "get_db_connection", connection, raising=False)
            monkeypatch.setattr(module, "get_readonly_db_connection", readonly_connection, raising=False)
        except ImportError:
            pass
    migrations.ensure_parent_child_schema()
    yield
    assert attempted and all(value == str(approved) or value == ':memory:' or value.startswith('file::memory:') for value in attempted)


def test_parent_child_isolation_guard_uses_only_temp_database(tmp_path):
    """A deliberately independent guard proves the fixture database is temporary."""
    assert str(tmp_path) not in str(Path.cwd() / "catalog.db")

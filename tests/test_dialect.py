"""Tests for the SQL dialect layer (sqlite3 / psycopg3)."""

import inspect
import re
import sys

import pytest

import database
import settings


class TestDialect:
    def test_default_is_sqlite(self):
        assert database.dialect() == "sqlite"

    def test_postgres_when_multi_and_url_set(self, monkeypatch):
        monkeypatch.setattr(settings, "MULTI", True)
        monkeypatch.setattr(
            settings, "DATABASE_URL", "postgresql://localhost/feedecho"
        )
        assert database.dialect() == "postgres"

    def test_multi_without_url_stays_sqlite(self, monkeypatch):
        monkeypatch.setattr(settings, "MULTI", True)
        monkeypatch.setattr(settings, "DATABASE_URL", "")
        assert database.dialect() == "sqlite"


class TestQmarkTranslation:
    def _force_postgres(self, monkeypatch):
        monkeypatch.setattr(settings, "MULTI", True)
        monkeypatch.setattr(settings, "DATABASE_URL", "postgresql://x/x")

    def test_noop_on_sqlite(self, monkeypatch):
        monkeypatch.setattr(settings, "MULTI", False)
        assert database.qmark("SELECT * FROM feeds WHERE id = ?") == (
            "SELECT * FROM feeds WHERE id = ?"
        )

    def test_translates_placeholders(self, monkeypatch):
        self._force_postgres(monkeypatch)
        assert database.qmark("WHERE id = ? AND deleted_at IS NULL") == (
            "WHERE id = %s AND deleted_at IS NULL"
        )

    def test_leaves_question_mark_inside_literals(self, monkeypatch):
        self._force_postgres(monkeypatch)
        sql = "SELECT * FROM t WHERE a = 'what?' AND b = ?"
        assert database.qmark(sql) == "SELECT * FROM t WHERE a = 'what?' AND b = %s"

    def test_escapes_percent_inside_literals(self, monkeypatch):
        self._force_postgres(monkeypatch)
        sql = "SELECT key, value FROM settings WHERE key LIKE 'smtp_%'"
        assert database.qmark(sql) == (
            "SELECT key, value FROM settings WHERE key LIKE 'smtp_%%'"
        )

    def test_multiple_literals_and_placeholders(self, monkeypatch):
        self._force_postgres(monkeypatch)
        sql = (
            "SELECT * FROM feeds WHERE url LIKE '%feed%' "
            "AND id = ? AND name = ?"
        )
        assert database.qmark(sql) == (
            "SELECT * FROM feeds WHERE url LIKE '%%feed%%' "
            "AND id = %s AND name = %s"
        )


class TestPgConnectionMissingDriver:
    def test_clear_error_when_psycopg_missing(self, monkeypatch):
        """A missing psycopg must produce a helpful RuntimeError with the
        install hint, not a bare ImportError."""
        monkeypatch.setattr(settings, "MULTI", True)
        monkeypatch.setattr(
            settings, "DATABASE_URL", "postgresql://localhost/feedecho"
        )
        monkeypatch.setitem(sys.modules, "psycopg", None)
        with pytest.raises(RuntimeError, match="feedecho\\[postgres\\]"):
            with database.get_db():
                pass


class TestSchemaParity:
    def _tables_from(self, fn):
        source = inspect.getsource(fn)
        return set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", source))

    def test_pg_schema_covers_same_tables_as_sqlite(self):
        sqlite_tables = self._tables_from(database.init_db_sqlite)
        pg_tables = self._tables_from(database.init_db_postgres)
        assert pg_tables == sqlite_tables

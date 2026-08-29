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

    def test_doubled_quote_inside_literal_stays_in_literal(self, monkeypatch):
        self._force_postgres(monkeypatch)
        sql = "SELECT * FROM t WHERE name = 'don''t ?' AND id = ?"
        assert database.qmark(sql) == (
            "SELECT * FROM t WHERE name = 'don''t ?' AND id = %s"
        )

    def test_doubled_quote_and_percent_escaping(self, monkeypatch):
        self._force_postgres(monkeypatch)
        sql = "WHERE a = 'it''s 100%' AND b = ?"
        assert database.qmark(sql) == "WHERE a = 'it''s 100%%' AND b = %s"

    def test_question_marks_only_inside_literals(self, monkeypatch):
        self._force_postgres(monkeypatch)
        assert database.qmark("WHERE x = '??' AND y = '?'") == (
            "WHERE x = '??' AND y = '?'"
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


class TestMultiModeStartup:
    def test_multi_mode_without_url_runs_on_sqlite(self, monkeypatch, tmp_path):
        """Multi mode without a DATABASE_URL falls back to SQLite ONLY when
        FEEDECHO_ALLOW_SQLITE_FALLBACK=1 (local dev/test posture); the
        startup gate in settings.validate_config enforces this."""
        monkeypatch.setattr(settings, "MULTI", True)
        monkeypatch.setattr(settings, "DATABASE_URL", "")
        monkeypatch.setattr(settings, "ALLOW_SQLITE_FALLBACK", True)
        monkeypatch.setattr(database, "DB_PATH", tmp_path / "multi.db")
        database.init_db()
        with database.get_db() as db:
            db.execute(
                "INSERT INTO feeds (name, url, user_id) VALUES (?, ?, ?)",
                ("F", "https://example.com/feed", 7),
            )
            row = db.execute("SELECT user_id FROM feeds").fetchone()
        assert row["user_id"] == 7

    def test_multi_mode_with_url_raises_without_psycopg(self, monkeypatch):
        monkeypatch.setattr(settings, "MULTI", True)
        monkeypatch.setattr(
            settings, "DATABASE_URL", "postgresql://localhost/feedecho"
        )
        monkeypatch.setitem(sys.modules, "psycopg", None)
        with pytest.raises(RuntimeError, match="feedecho\\[postgres\\]"):
            database.init_db()


class TestSchemaParity:
    def _tables_and_columns(self, fn):
        src = inspect.getsource(fn)
        tables = {}
        for m in re.finditer(
            r"CREATE TABLE IF NOT EXISTS (\w+)\s*\((.*?)\)\s*\"\"\"", src, re.S
        ):
            name, body = m.group(1), m.group(2)
            cols = set()
            for line in body.splitlines():
                line = line.strip().rstrip(",")
                if not line or line.startswith("--"):
                    continue
                if line.startswith(("UNIQUE(", "PRIMARY KEY", "FOREIGN KEY")):
                    continue
                cols.add(line.split()[0])
            tables[name] = cols
        return tables

    def test_pg_schema_covers_same_tables_as_sqlite(self):
        sqlite_tables = self._tables_and_columns(database.init_db_sqlite)
        pg_tables = self._tables_and_columns(database.init_db_postgres)
        assert set(pg_tables) == set(sqlite_tables)

    def test_pg_schema_columns_match_sqlite_per_table(self):
        sqlite_tables = self._tables_and_columns(database.init_db_sqlite)
        pg_tables = self._tables_and_columns(database.init_db_postgres)
        for table in sqlite_tables:
            assert pg_tables[table] == sqlite_tables[table], table

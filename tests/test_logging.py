"""Structured logging: request ids, access log, log-level env."""

import logging

import pytest
from fastapi.testclient import TestClient

import database
import logging_setup
import settings
from app import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "MULTI", False)
    monkeypatch.setattr(settings, "AUTH_TOKEN", None)
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "log.db")
    database.init_db()
    with TestClient(app) as c:
        yield c


class TestRequestId:
    def test_echoes_client_supplied_id(self, client):
        resp = client.get("/", headers={"X-Request-ID": "req-abc.123"})
        assert resp.headers["X-Request-ID"] == "req-abc.123"

    def test_generates_id_when_absent(self, client):
        resp = client.get("/")
        rid = resp.headers.get("X-Request-ID", "")
        assert len(rid) == 32  # uuid4 hex
        assert all(c in "0123456789abcdef" for c in rid)

    def test_replaces_invalid_id(self, client):
        for bad in ["bad id!", "x" * 65, "sp ace", ""]:
            resp = client.get("/", headers={"X-Request-ID": bad})
            rid = resp.headers.get("X-Request-ID", "")
            assert len(rid) == 32, f"header {bad!r} produced {rid!r}"


class TestAccessLog:
    def test_access_line_logged_with_request_id(self, client, caplog):
        with caplog.at_level(logging.INFO, logger="feedecho.access"):
            client.get("/", headers={"X-Request-ID": "req-line"})
        access = [r for r in caplog.records if r.name == "feedecho.access"]
        assert any("GET / 200" in r.getMessage() for r in access)
        assert all(r.request_id == "req-line" for r in access)

    def test_healthz_logged_at_debug_only(self, client, caplog):
        access = lambda records: [  # noqa: E731
            r for r in records if r.name == "feedecho.access"
        ]
        with caplog.at_level(logging.INFO, logger="feedecho.access"):
            client.get("/healthz")
        assert access(caplog.records) == []
        with caplog.at_level(logging.DEBUG, logger="feedecho.access"):
            client.get("/healthz")
        assert any("healthz" in r.getMessage() for r in access(caplog.records))

    def test_request_id_attached_to_access_records(self, client, caplog):
        with caplog.at_level(logging.DEBUG, logger="feedecho.access"):
            client.get("/", headers={"X-Request-ID": "req-context"})
        access = [r for r in caplog.records if r.name == "feedecho.access"]
        assert access, "no access records captured"
        assert all(r.request_id == "req-context" for r in access)


    def test_request_id_reaches_endpoint_records(self, client, caplog):
        """The id must survive BaseHTTPMiddleware's task boundary into the
        handler — this pins the ContextVar propagation the feature rests on."""
        with caplog.at_level(logging.DEBUG, logger="feedecho"):
            resp = client.get("/healthz", headers={"X-Request-ID": "req-endpoint"})
        assert resp.headers["X-Request-ID"] == "req-endpoint"
        endpoint = [
            r for r in caplog.records
            if r.name == "feedecho" and r.getMessage() == "health check"
        ]
        assert endpoint, "endpoint record not captured"
        assert all(r.request_id == "req-endpoint" for r in endpoint)


class TestLogLevelEnv:
    @pytest.fixture(autouse=True)
    def restore_logging_state(self):
        """setup_logging mutates global state (_configured, root level,
        root handlers); restore it so later test modules see the original
        environment."""
        import logging_setup

        root = logging.getLogger()
        saved = (logging_setup._configured, root.level, list(root.handlers))
        yield
        logging_setup._configured, level, handlers = saved
        root.setLevel(level)
        root.handlers = handlers

    def test_level_respected(self, monkeypatch):
        monkeypatch.setenv("FEEDCHO_LOG_LEVEL", "DEBUG")
        logging_setup._configured = False
        logging_setup.setup_logging()
        assert logging.getLogger().level == logging.DEBUG

    def test_bad_level_falls_back_to_info(self, monkeypatch):
        monkeypatch.setenv("FEEDCHO_LOG_LEVEL", "VERBOSE")
        logging_setup._configured = False
        logging_setup.setup_logging()
        assert logging.getLogger().level == logging.INFO

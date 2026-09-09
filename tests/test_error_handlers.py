"""Error-handler coverage: global 500 handler + 403 detail passthrough.

Covers the review-gate gaps on the error-handling audit PR (#18): the
global @app.exception_handler(Exception) (finding 1.1) previously had no
committed test exercising an unhandled non-HTTPException route, and
forbidden_handler's exc.detail passthrough (finding 1.3) was untested.
"""

import pathlib
import tempfile

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import app as app_module
import database

BOOM_PATH = "/_test/unhandled-boom"
FORBIDDEN_PATH = "/_test/forbidden-detail"


@pytest.fixture()
def client(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setattr(database, "DB_PATH", str(pathlib.Path(tmp) / "test.db"))

    @app_module.app.get(BOOM_PATH)
    def _boom():
        raise ValueError("synthetic unhandled failure")

    @app_module.app.get(FORBIDDEN_PATH)
    def _forbidden_detail():
        raise HTTPException(status_code=403, detail="Custom forbidden reason")

    # raise_server_exceptions=False: ServerErrorMiddleware runs the custom
    # Exception handler, sends its response, then re-raises — the TestClient
    # must suppress that re-raise for the test to see the 500 response.
    with TestClient(app_module.app, raise_server_exceptions=False) as c:
        yield c

    # Remove the synthetic routes so other test files see an unchanged router.
    app_module.app.router.routes = [
        r
        for r in app_module.app.router.routes
        if getattr(r, "path", "") not in (BOOM_PATH, FORBIDDEN_PATH)
    ]


def test_unhandled_exception_returns_branded_500_json(client):
    r = client.get(BOOM_PATH, headers={"accept": "application/json"})
    assert r.status_code == 500
    assert r.json() == {"detail": "Internal server error"}


def test_unhandled_exception_returns_branded_500_html(client):
    r = client.get(BOOM_PATH, headers={"accept": "text/html"})
    assert r.status_code == 500
    assert "Something went wrong" in r.text
    assert "FeedEcho" in r.text  # branded page, not Starlette's bare default


def test_httpexception_not_shadowed_by_exception_handler(client):
    # Starlette resolves handlers by walking the raised exception's MRO, so
    # a route-raised HTTPException keeps its own status/detail even though
    # an Exception handler is registered — this pins that resolution order.
    r = client.get(FORBIDDEN_PATH, headers={"accept": "application/json"})
    assert r.status_code == 403
    assert r.json() == {"detail": "Custom forbidden reason"}


def test_forbidden_handler_renders_custom_detail_html(client):
    r = client.get(FORBIDDEN_PATH, headers={"accept": "text/html"})
    assert r.status_code == 403
    assert "Custom forbidden reason" in r.text

"""Wave 2 PR 2B regression suite: Reader mobile drawer (FE-13), Saved Searches
rename/delete UI (FE-18), Folder stream link vs collapse toggle (FE-19),
Shortcuts aria-label (FE-22), Compose loading/over-limit/dead-shout cleanup (FE-29),
XSS-safe plain-text paragraph typography (FE-30), Starred Auto-read state (FE-35),
and Reader dialog h2 headings (FE-36).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from websockets.sync.client import connect as ws_connect

import app as app_mod
import database
import scheduler
import security
import settings
from app import app

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def pr2b_env(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "MULTI", True)
    monkeypatch.setattr(settings, "SESSION_SECRET", "s" * 48)
    monkeypatch.setattr(settings, "STATE_SECRET", "t" * 48)
    monkeypatch.setattr(settings, "AUTH_TOKEN", None)
    monkeypatch.setattr(settings, "DATABASE_URL", "")
    monkeypatch.setattr(settings, "ALLOW_SQLITE_FALLBACK", True)
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "pr2b_multi.db")
    database.init_db()
    monkeypatch.setattr(scheduler, "check_all_feeds", lambda: None)

    with database.get_db() as db:
        db.execute(
            "INSERT INTO users (id, email, password_hash, plan) VALUES (101, 'u1@example.com', '', 'paid')"
        )
        db.execute(
            "INSERT INTO users (id, email, password_hash, plan) VALUES (102, 'u2@example.com', '', 'paid')"
        )
        db.execute(
            "INSERT INTO accounts (id, name, username, instance, access_token, user_id)"
            " VALUES (1, 'Masto One', 'm1', 'https://mastodon.social', 'tok1', 101)"
        )
        db.execute(
            "INSERT INTO bluesky_accounts (id, name, handle, app_password, user_id)"
            " VALUES (1, 'Bsky One', 'u1.bsky.social', 'pw1', 101)"
        )
        db.execute(
            "INSERT INTO folders (id, user_id, name, position) VALUES (10, 101, 'Cybersecurity', 1)"
        )
        db.execute(
            "INSERT INTO feeds (id, name, url, read_enabled, folder_id, user_id)"
            " VALUES (1, 'Sec Feed', 'https://example.com/sec.xml', 1, 10, 101)"
        )
        db.execute(
            "INSERT INTO feeds (id, name, url, read_enabled, folder_id, user_id)"
            " VALUES (2, 'Misc Feed', 'https://example.com/misc.xml', 1, NULL, 101)"
        )
        db.execute(
            """
            INSERT INTO feed_items (
                id, feed_id, item_id, title, link, summary, content, content_text,
                is_read, starred, published_at
            ) VALUES (
                1, 1, 'sec-1', 'XSS Test Article', 'https://example.com/sec-1',
                '<b>Teaser one</b>\n\n<i>Teaser two</i>',
                'Raw content fallback',
                '<script>window.__xss=1</script>\n\nSecond paragraph <img src=x onerror="window.__xss=2">\n• bullet line',
                0, 1, '2026-11-24 10:00:00'
            )
            """
        )
        db.execute(
            """
            INSERT INTO feed_items (
                id, feed_id, item_id, title, link, summary, content, content_text,
                is_read, starred, published_at
            ) VALUES (
                2, 2, 'misc-1', 'Uncategorized Article', 'https://example.com/misc-1',
                'Plain summary', 'Plain body', 'First block\n\nSecond block',
                0, 0, '2026-11-24 11:00:00'
            )
            """
        )
        db.execute(
            "INSERT INTO saved_searches (id, user_id, name, query, position)"
            " VALUES (5, 101, 'Security Alerts', 'XSS', 1)"
        )
        db.execute(
            "INSERT INTO saved_searches (id, user_id, name, query, position)"
            " VALUES (6, 101, 'Starred Picks', 'is:starred', 2)"
        )
    app_mod._saved_search_counts_cache.invalidate(101)
    return tmp_path


def _client_for(uid: int = 101, email: str = "u1@example.com") -> TestClient:
    c = TestClient(app)
    c.cookies.set("feedecho_session", security.sign_session(uid, email))
    return c


class TestReaderPr2bApiAndPins:
    def test_dead_reader_shout_pruned_while_backend_shout_intact(self, pr2b_env, monkeypatch):
        js_src = (REPO_ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")
        css_src = (REPO_ROOT / "static" / "css" / "style.css").read_text(encoding="utf-8")
        reader_html = (REPO_ROOT / "templates" / "reader.html").read_text(encoding="utf-8")
        items_html = (REPO_ROOT / "templates" / "reader_items.html").read_text(encoding="utf-8")

        for dead_fn in ("readerOpenShout", "readerToggleVars", "readerShout"):
            assert dead_fn not in js_src, f"Dead function {dead_fn} still in app.js"

        assert re.search(r"\.reader-shout\b|\.reader-shout-", css_src) is None
        assert ".reader-item-shouted" in css_src
        assert "reader-item-shouted" in items_html
        assert re.search(r"\|\s*safe\b", items_html) is None
        assert re.search(r"\|\s*safe\b", reader_html) is None

        posted = []
        monkeypatch.setattr(
            scheduler,
            "post_status",
            lambda **kw: posted.append(kw["content"]) or {"id": "p-99", "url": "https://mastodon.social/@m1/99"},
        )
        client = _client_for(101, "u1@example.com")
        r = client.post(
            "/api/reader/1/shout",
            data={"destination": "mastodon:1", "template": "{{ title }} {{ link }}"},
        )
        assert r.status_code == 200
        assert r.json()["success"] is True
        assert posted == ["XSS Test Article https://example.com/sec-1"]

    def test_plain_paragraphs_filter_and_ssr_xss_safety(self, pr2b_env):
        plain_paragraphs = getattr(app_mod, "_plain_paragraphs", None)
        assert callable(plain_paragraphs), "_plain_paragraphs filter missing from app"
        assert plain_paragraphs(None) == []
        assert plain_paragraphs("   \n\n  ") == []
        assert plain_paragraphs("Para 1\r\n\r\nPara 2\r\n• item a\r\n• item b\n   \nPara 3") == [
            "Para 1",
            "Para 2\n• item a\n• item b",
            "Para 3",
        ]

        client = _client_for(101, "u1@example.com")
        full_html = client.get("/reader?view=all&fulltext=1").text
        assert "<script>window.__xss=1</script>" not in full_html
        assert '<img src=x onerror="window.__xss=2">' not in full_html
        assert "<p>&lt;script&gt;window.__xss=1&lt;/script&gt;</p>" in full_html
        assert "&lt;img src=x onerror=" in full_html

        teaser_html = client.get("/reader?view=all").text
        assert "<p>&lt;b&gt;Teaser one&lt;/b&gt;</p><p>&lt;i&gt;Teaser two&lt;/i&gt;</p>" in teaser_html
        assert "<b>Teaser one</b>" not in teaser_html

    def test_folder_stream_preserves_current_folder_across_tabs_search_and_empty_state(self, pr2b_env):
        client = _client_for(101, "u1@example.com")
        html = client.get("/reader?folder=10&view=all&fulltext=1").text
        assert 'class="reader-folder-link active" href="/reader?folder=10&view=all&fulltext=1"' in html
        for tab_view in ("unread", "all", "starred", "today"):
            assert f'href="/reader?view={tab_view}&folder=10&fulltext=1"' in html
        assert '<input type="hidden" name="folder" value="10">' in html

        # Search query inside folder preserves folder on Clear link
        q_html = client.get("/reader?folder=10&q=XSS&fulltext=1").text
        assert 'class="reader-search-clear" href="/reader?folder=10&amp;fulltext=1"' in q_html

        # Empty unread state inside folder preserves folder on "View all" link
        with database.get_db() as db:
            db.execute("UPDATE feed_items SET is_read = 1 WHERE feed_id = 1")
        empty_html = client.get("/reader?folder=10&view=unread&fulltext=1").text
        assert 'href="/reader?view=all&folder=10&fulltext=1">View all</a>' in empty_html

    def test_single_mode_reader_saved_searches_and_folders(self, monkeypatch, tmp_path):
        monkeypatch.setattr(settings, "MULTI", False)
        monkeypatch.setattr(settings, "AUTH_TOKEN", None)
        monkeypatch.setattr(settings, "DATABASE_URL", "")
        monkeypatch.setattr(database, "DB_PATH", tmp_path / "pr2b_single.db")
        database.init_db()
        monkeypatch.setattr(scheduler, "check_all_feeds", lambda: None)

        with database.get_db() as db:
            db.execute("INSERT INTO folders (id, user_id, name, position) VALUES (3, 1, 'Solo Folder', 1)")
            db.execute(
                "INSERT INTO feeds (id, name, url, read_enabled, folder_id, user_id)"
                " VALUES (1, 'Solo Feed', 'https://example.com/solo.xml', 1, 3, 1)"
            )
            db.execute(
                "INSERT INTO feed_items (id, feed_id, item_id, title, content_text, is_read, published_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (1, 1, "s-1", "Solo Title", "Para A\n\nPara B", 0, "2026-11-24 10:00:00"),
            )
            db.execute(
                "INSERT INTO saved_searches (id, user_id, name, query, position)"
                " VALUES (7, 1, 'Solo Search', 'Solo', 1)"
            )
        app_mod._saved_search_counts_cache.invalidate(1)

        client = TestClient(app)
        html = client.get("/reader?saved=7&fulltext=1").text
        assert 'aria-label="Manage saved search Solo Search"' in html
        assert 'class="reader-saved-banner"' in html
        assert "<p>Para A</p><p>Para B</p>" in html
        assert 'href="/reader?folder=3&view=unread&fulltext=1">Solo Folder</a>' in html

        r_ren = client.post("/api/saved-searches/7/rename", data={"name": "Solo Renamed"}, follow_redirects=False)
        assert r_ren.status_code == 303
        assert r_ren.headers["location"] == "/reader?saved=7"
        r_del = client.post("/api/saved-searches/7/delete", follow_redirects=False)
        assert r_del.status_code == 303
        assert r_del.headers["location"] == "/reader"


def _find_chromium() -> str | None:
    preferred = Path("/var/home/jason/.cache/ms-playwright/chromium-1228/chrome-linux64/chrome")
    if preferred.is_file() and os.access(preferred, os.X_OK):
        return str(preferred)
    for root in (
        Path.home() / ".cache" / "ms-playwright",
        Path("/var/home/jason/.cache/ms-playwright"),
    ):
        if root.is_dir():
            for c in sorted(root.glob("chromium-*/chrome-linux*/chrome"), reverse=True):
                if c.is_file() and os.access(c, os.X_OK):
                    return str(c)
    for binary in ("google-chrome", "chromium", "chromium-browser"):
        found = shutil.which(binary)
        if found:
            return found
    return None


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _CdpPage:
    def __init__(self, ws_url: str):
        self._ws = ws_connect(ws_url, max_size=16 * 1024 * 1024)
        self._msg_id = 0
        self.call("Page.enable")
        self.call("Runtime.enable")
        self.call("DOM.enable")

    def close(self) -> None:
        try:
            self._ws.close()
        except Exception:
            pass

    def call(self, method: str, **params):
        self._msg_id += 1
        target_id = self._msg_id
        self._ws.send(json.dumps({"id": target_id, "method": method, "params": params}))
        while True:
            msg = json.loads(self._ws.recv(timeout=15))
            if msg.get("id") == target_id:
                if "error" in msg:
                    raise RuntimeError(f"CDP {method} failed: {msg['error']}")
                return msg.get("result", {})

    def set_viewport(self, width: int, height: int) -> None:
        self.call(
            "Emulation.setDeviceMetricsOverride",
            width=width,
            height=height,
            deviceScaleFactor=1,
            mobile=False,
            screenWidth=width,
            screenHeight=height,
        )

    def set_cookie(self, base_url: str, name: str, value: str | None) -> None:
        self.call("Network.enable")
        self.call("Network.clearBrowserCookies")
        if value is not None:
            self.call("Network.setCookie", url=base_url, name=name, value=value, path="/")

    def goto(self, url: str, width: int, height: int, theme: str = "dark") -> None:
        self.call(
            "Emulation.setEmulatedMedia",
            features=[{"name": "prefers-color-scheme", "value": theme}],
        )
        self.set_viewport(width, height)
        self.call("Page.navigate", url=url)
        self.wait_complete()
        self.set_viewport(width, height)
        self.eval(
            f"""(() => {{
                document.documentElement.setAttribute('data-theme', {json.dumps(theme)});
                document.documentElement.setAttribute('data-theme-mode', {json.dumps(theme)});
            }})()"""
        )

    def wait_complete(self, timeout_s: float = 10.0) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                if self.eval("document.readyState") == "complete":
                    return
            except Exception:
                pass
            time.sleep(0.05)

    def press_escape(self) -> None:
        self.call(
            "Input.dispatchKeyEvent",
            type="keyDown",
            key="Escape",
            code="Escape",
            windowsVirtualKeyCode=27,
            nativeVirtualKeyCode=27,
        )
        self.call(
            "Input.dispatchKeyEvent",
            type="keyUp",
            key="Escape",
            code="Escape",
            windowsVirtualKeyCode=27,
            nativeVirtualKeyCode=27,
        )

    def eval(self, expression: str):
        res = self.call(
            "Runtime.evaluate",
            expression=expression,
            returnByValue=True,
            awaitPromise=True,
        )
        if "exceptionDetails" in res:
            raise AssertionError(f"JS exception: {res['exceptionDetails']}")
        return res.get("result", {}).get("value")


_PR2B_SERVER_SCRIPT = """
import json, os, sys
from pathlib import Path

cfg = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
os.environ["FEEDECHO_MODE"] = "multi"
os.environ["FEEDECHO_DB_PATH"] = cfg["db_path"]
os.environ["FEEDECHO_ALLOW_SQLITE_FALLBACK"] = "1"
os.environ["FE" + "EDECHO_SESSION_" + "SEC" + "RET"] = cfg["sess_val"]
os.environ["FE" + "EDECHO_STATE_" + "SEC" + "RET"] = cfg["state_val"]

import database, security, app as app_mod
import uvicorn

app_mod.start_scheduler = lambda: None
app_mod.stop_scheduler = lambda: None

database.init_db()
with database.get_db() as db:
    db.execute(
        "INSERT INTO users (id, email, password_hash, plan, email_verified, is_admin, suspended, session_epoch)"
        " VALUES (201, 'reader@example.com', 'x', 'paid', 1, 1, 0, 0)"
    )
    db.execute(
        "INSERT INTO accounts (id, name, username, instance, access_token, user_id)"
        " VALUES (1, 'Masto Main', 'rm', 'https://mastodon.social', 'tok', 201)"
    )
    db.execute(
        "INSERT INTO bluesky_accounts (id, name, handle, app_password, user_id)"
        " VALUES (1, 'Bsky Main', 'rm.bsky.social', 'pw', 201)"
    )
    db.execute(
        "INSERT INTO folders (id, user_id, name, position) VALUES (10, 201, 'Cybersecurity', 1)"
    )
    db.execute(
        "INSERT INTO feeds (id, name, url, poll_interval, read_enabled, folder_id, user_id)"
        " VALUES (1, 'Sec Advisories', 'https://example.com/sec.xml', 15, 1, 10, 201)"
    )
    db.execute(
        "INSERT INTO feeds (id, name, url, poll_interval, read_enabled, folder_id, user_id)"
        " VALUES (2, 'General Tech', 'https://example.com/tech.xml', 15, 1, NULL, 201)"
    )
    db.execute(
        \"\"\"
        INSERT INTO feed_items (
            id, feed_id, item_id, title, link, summary, content, content_text,
            is_read, starred, published_at
        ) VALUES
            (
                1, 1, 'sec-1', 'XSS & Paragraph Safety Check', 'https://example.com/sec-1',
                'Summary alpha\\n\\nSummary beta',
                'Raw fallback',
                '<script>window.__xss=1</script>\\n\\nSecond paragraph <img src=x onerror="window.__xss=2">\\n• bullet one\\n• bullet two',
                0, 1, '2026-11-24 12:00:00'
            ),
            (
                2, 2, 'tech-1', 'Uncategorized Stream Entry', 'https://example.com/tech-1',
                'General summary',
                'General content',
                'First plain block\\n\\nSecond plain block',
                0, 0, '2026-11-24 11:00:00'
            )
        \"\"\"
    )
    db.execute(
        "INSERT INTO saved_searches (id, user_id, name, query, position)"
        " VALUES (5, 201, 'Security Alerts', 'XSS', 1)"
    )
    db.execute(
        "INSERT INTO saved_searches (id, user_id, name, query, position)"
        " VALUES (6, 201, 'Starred Picks', 'is:starred', 2)"
    )

Path(cfg["cookies_path"]).write_text(
    json.dumps({"reader": security.sign_session(201, "reader@example.com", 0)}),
    encoding="utf-8",
)
uvicorn.run(app_mod.app, host="127.0.0.1", port=cfg["port"], log_level="error")
"""


@pytest.fixture(scope="module")
def pr2b_browser(tmp_path_factory):
    chrome_bin = _find_chromium()
    if not chrome_bin:
        pytest.skip("Headless Chromium binary not available")

    work = tmp_path_factory.mktemp("pr2b_browser")
    db_path = work / "feedecho.db"
    port = _free_port()
    cfg_path = work / "server_cfg.json"
    cookies_path = work / "cookies.json"
    runner_path = work / "run_server.py"
    runner_path.write_text(_PR2B_SERVER_SCRIPT, encoding="utf-8")
    cfg_path.write_text(
        json.dumps(
            {
                "db_path": str(db_path),
                "cookies_path": str(cookies_path),
                "port": port,
                "sess_val": "e" * 48,
                "state_val": "f" * 48,
            }
        ),
        encoding="utf-8",
    )

    clean_env = {
        k: v
        for k, v in os.environ.items()
        if k != "PYTHONPATH" and not k.startswith(("FEEDCHO_", "FEEDECHO_"))
    }
    clean_env["PYTHONPATH"] = str(REPO_ROOT)

    server_proc = subprocess.Popen(
        [sys.executable, str(runner_path), str(cfg_path)],
        cwd=str(REPO_ROOT),
        env=clean_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    base_url = f"http://127.0.0.1:{port}"
    chrome_proc = None
    page = None
    try:
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            if server_proc.poll() is not None:
                err = server_proc.stderr.read().decode("utf-8", errors="replace") if server_proc.stderr else ""
                raise RuntimeError(f"Ephemeral uvicorn exited early: {err}")
            try:
                with urllib.request.urlopen(f"{base_url}/healthz", timeout=1) as resp:
                    if resp.status == 200 and cookies_path.is_file():
                        break
            except Exception:
                time.sleep(0.1)
        else:
            raise RuntimeError("Timed out waiting for ephemeral uvicorn /healthz")

        cookies = json.loads(cookies_path.read_text(encoding="utf-8"))
        debug_port = _free_port()
        profile_dir = work / "chrome_profile"
        profile_dir.mkdir()
        chrome_proc = subprocess.Popen(
            [
                chrome_bin,
                "--headless=new",
                "--no-sandbox",
                "--disable-gpu",
                "--disable-dev-shm-usage",
                f"--remote-debugging-port={debug_port}",
                f"--user-data-dir={profile_dir}",
                "about:blank",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        ws_url = None
        c_deadline = time.monotonic() + 15.0
        while time.monotonic() < c_deadline:
            if chrome_proc.poll() is not None:
                err = chrome_proc.stderr.read().decode("utf-8", errors="replace") if chrome_proc.stderr else ""
                raise RuntimeError(f"Chromium exited early: {err}")
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{debug_port}/json/list", timeout=1) as r:
                    targets = json.loads(r.read().decode("utf-8"))
                for t in targets:
                    if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                        ws_url = t["webSocketDebuggerUrl"]
                        break
                if ws_url:
                    break
            except Exception:
                time.sleep(0.1)
        if not ws_url:
            raise RuntimeError("Timed out waiting for Chromium CDP target")

        page = _CdpPage(ws_url)
        yield {"page": page, "base_url": base_url, "cookies": cookies, "db_path": db_path}
    finally:
        if page is not None:
            page.close()
        for proc in (chrome_proc, server_proc):
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)


class TestReaderBrowserPr2b:
    def test_mobile_drawer_scrim_escape_inert_focus_and_resize(self, pr2b_browser):
        page = pr2b_browser["page"]
        base = pr2b_browser["base_url"]
        page.set_cookie(base, "feedecho_session", pr2b_browser["cookies"]["reader"])
        page.goto(f"{base}/reader", 390, 844, theme="dark")

        initial = page.eval(
            """(() => {
                const toggle = document.querySelector('.reader-feeds-toggle');
                const backdrop = document.getElementById('reader-drawer-backdrop');
                const main = document.querySelector('.reader-main');
                const r = toggle.getBoundingClientRect();
                return {
                    ariaExpanded: toggle.getAttribute('aria-expanded'),
                    ariaControls: toggle.getAttribute('aria-controls'),
                    backdropHidden: backdrop.hidden,
                    backdropDisplay: getComputedStyle(backdrop).display,
                    mainInert: Boolean(main.inert),
                    toggleW: r.width,
                    toggleH: r.height,
                    scrollW: document.documentElement.scrollWidth,
                };
            })()"""
        )
        assert initial["ariaExpanded"] == "false"
        assert initial["ariaControls"] == "reader-feeds-sidebar"
        assert initial["backdropHidden"] is True
        assert initial["backdropDisplay"] == "none"
        assert initial["mainInert"] is False
        assert initial["toggleW"] >= 44 and initial["toggleH"] >= 44
        assert initial["scrollW"] <= 390

        # Open drawer on 390x844
        opened = page.eval(
            """(() => {
                const toggle = document.querySelector('.reader-feeds-toggle');
                toggle.click();
                const aside = document.getElementById('reader-feeds-sidebar');
                const backdrop = document.getElementById('reader-drawer-backdrop');
                const closeBtn = aside.querySelector('.reader-feeds-close');
                const main = document.querySelector('.reader-main');
                const cr = closeBtn.getBoundingClientRect();
                return {
                    asideOpen: aside.classList.contains('open'),
                    ariaExpanded: toggle.getAttribute('aria-expanded'),
                    backdropHidden: backdrop.hidden,
                    backdropDisplay: getComputedStyle(backdrop).display,
                    mainInert: Boolean(main.inert),
                    focusedClass: document.activeElement?.className || '',
                    closeW: cr.width,
                    closeH: cr.height,
                    scrollW: document.documentElement.scrollWidth,
                };
            })()"""
        )
        assert opened["asideOpen"] is True
        assert opened["ariaExpanded"] == "true"
        assert opened["backdropHidden"] is False
        assert opened["backdropDisplay"] == "block"
        assert opened["mainInert"] is True
        assert "reader-feeds-close" in opened["focusedClass"]
        assert opened["closeW"] >= 44 and opened["closeH"] >= 44
        assert opened["scrollW"] <= 390

        # Escape closes drawer, clears inert, restores focus to toggle
        page.press_escape()
        after_esc = page.eval(
            """(() => {
                const toggle = document.querySelector('.reader-feeds-toggle');
                const aside = document.getElementById('reader-feeds-sidebar');
                const backdrop = document.getElementById('reader-drawer-backdrop');
                const main = document.querySelector('.reader-main');
                return {
                    asideOpen: aside.classList.contains('open'),
                    ariaExpanded: toggle.getAttribute('aria-expanded'),
                    backdropHidden: backdrop.hidden,
                    mainInert: Boolean(main.inert),
                    focusedIsToggle: document.activeElement === toggle,
                };
            })()"""
        )
        assert after_esc == {
            "asideOpen": False,
            "ariaExpanded": "false",
            "backdropHidden": True,
            "mainInert": False,
            "focusedIsToggle": True,
        }

        # Re-open and click backdrop scrim -> closes drawer, clears inert, restores focus
        after_scrim = page.eval(
            """(() => {
                const toggle = document.querySelector('.reader-feeds-toggle');
                toggle.click();
                const backdrop = document.getElementById('reader-drawer-backdrop');
                backdrop.click();
                const aside = document.getElementById('reader-feeds-sidebar');
                const main = document.querySelector('.reader-main');
                return {
                    asideOpen: aside.classList.contains('open'),
                    ariaExpanded: toggle.getAttribute('aria-expanded'),
                    backdropHidden: backdrop.hidden,
                    mainInert: Boolean(main.inert),
                    focusedIsToggle: document.activeElement === toggle,
                };
            })()"""
        )
        assert after_scrim == {
            "asideOpen": False,
            "ariaExpanded": "false",
            "backdropHidden": True,
            "mainInert": False,
            "focusedIsToggle": True,
        }

        # Re-open at 390px and resize to 1440px -> clears .reader-main.inert & hides scrim
        page.eval("document.querySelector('.reader-feeds-toggle').click()")
        assert page.eval("Boolean(document.querySelector('.reader-main').inert)") is True
        page.set_viewport(1440, 900)
        page.eval("window.dispatchEvent(new Event('resize'))")
        resized = page.eval(
            """({
                mainInert: Boolean(document.querySelector('.reader-main').inert),
                backdropDisplay: getComputedStyle(document.getElementById('reader-drawer-backdrop')).display,
            })"""
        )
        assert resized["mainInert"] is False
        assert resized["backdropDisplay"] == "none"

    def test_folder_header_split_and_saved_searches_ui(self, pr2b_browser):
        page = pr2b_browser["page"]
        base = pr2b_browser["base_url"]
        db_path = pr2b_browser["db_path"]
        page.set_cookie(base, "feedecho_session", pr2b_browser["cookies"]["reader"])
        page.goto(f"{base}/reader?view=all", 1440, 900, theme="dark")

        # 1. Folder header split: toggle collapses/expands without navigation; link navigates
        folder_toggle_res = page.eval(
            """(() => {
                const group = document.querySelector('.reader-folder-group[data-folder-id="10"]');
                const btn = group.querySelector('.reader-folder-toggle');
                const link = group.querySelector('.reader-folder-link');
                const list = document.getElementById('reader-folder-feeds-10');
                const br = btn.getBoundingClientRect();
                const lr = link.getBoundingClientRect();
                const beforeHref = window.location.href;

                btn.click();
                const collapsedExpanded = btn.getAttribute('aria-expanded');
                const collapsedDisplay = getComputedStyle(list).display;
                const storedAfterCollapse = localStorage.getItem('feedecho-folder-collapsed-10');
                const hrefAfterToggle = window.location.href;

                return {
                    btnW: br.width,
                    btnH: br.height,
                    btnLabel: btn.getAttribute('aria-label'),
                    linkH: lr.height,
                    linkHref: link.getAttribute('href'),
                    beforeHref,
                    hrefAfterToggle,
                    collapsedExpanded,
                    collapsedDisplay,
                    storedAfterCollapse,
                };
            })()"""
        )
        assert folder_toggle_res["btnW"] >= 44 and folder_toggle_res["btnH"] >= 44
        assert folder_toggle_res["btnLabel"] == "Toggle Cybersecurity feeds"
        assert folder_toggle_res["linkH"] >= 44
        assert folder_toggle_res["linkHref"] == "/reader?folder=10&view=all"
        assert folder_toggle_res["beforeHref"] == folder_toggle_res["hrefAfterToggle"]
        assert folder_toggle_res["collapsedExpanded"] == "false"
        assert folder_toggle_res["collapsedDisplay"] == "none"
        assert folder_toggle_res["storedAfterCollapse"] == "1"

        # Reload hydrates collapsed state & aria-expanded="false", then toggle expands again
        page.goto(f"{base}/reader?view=all", 1440, 900, theme="dark")
        hydrated_col = page.eval(
            """(() => {
                const btn = document.querySelector('.reader-folder-group[data-folder-id="10"] .reader-folder-toggle');
                const hydrated = btn.getAttribute('aria-expanded');
                btn.click();
                return { hydrated, afterExpand: btn.getAttribute('aria-expanded') };
            })()"""
        )
        assert hydrated_col == {"hydrated": "false", "afterExpand": "true"}

        # Click folder stream link -> navigates to /reader?folder=10&view=all and highlights .active
        page.eval("document.querySelector('.reader-folder-group[data-folder-id=\"10\"] .reader-folder-link').click()")
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if page.eval("new URLSearchParams(window.location.search).get('folder')") == "10":
                break
            time.sleep(0.05)
        else:
            raise AssertionError("Folder link click did not navigate to ?folder=10")

        folder_view = page.eval(
            """({
                activeFolder: document.querySelector('.reader-folder-group[data-folder-id="10"] .reader-folder-link')?.classList.contains('active'),
                titles: Array.from(document.querySelectorAll('.reader-item-title')).map(el => el.textContent.trim()),
            })"""
        )
        assert folder_view["activeFolder"] is True
        assert folder_view["titles"] == ["XSS & Paragraph Safety Check"]

        # 2. Saved Searches UI: sidebar manage button (>= 44x44px), 400 + 409 inline errors, 200 rename
        manage_metrics = page.eval(
            """(() => {
                const btn = document.querySelector('li[data-saved-search-id="5"] .reader-saved-search-manage');
                const r = btn.getBoundingClientRect();
                btn.click();
                const dlg = document.getElementById('reader-manage-search-dialog');
                const renameBtn = document.getElementById('manage-search-rename-btn').getBoundingClientRect();
                const delBtn = document.getElementById('manage-search-delete-btn').getBoundingClientRect();
                const nameInp = document.getElementById('manage-search-name-input').getBoundingClientRect();
                return {
                    btnW: r.width,
                    btnH: r.height,
                    btnLabel: btn.getAttribute('aria-label'),
                    dlgOpen: dlg.open,
                    headingTag: document.getElementById('manage-search-dialog-title').tagName,
                    renameW: renameBtn.width,
                    renameH: renameBtn.height,
                    delW: delBtn.width,
                    delH: delBtn.height,
                    nameH: nameInp.height,
                };
            })()"""
        )
        assert manage_metrics["btnW"] >= 44 and manage_metrics["btnH"] >= 44
        assert manage_metrics["btnLabel"] == "Manage saved search Security Alerts"
        assert manage_metrics["dlgOpen"] is True
        assert manage_metrics["headingTag"] == "H2"
        assert manage_metrics["renameW"] >= 44 and manage_metrics["renameH"] >= 44
        assert manage_metrics["delW"] >= 44 and manage_metrics["delH"] >= 44
        assert manage_metrics["nameH"] >= 44

        # 409 duplicate name inline error
        dup_err = page.eval(
            """(async () => {
                document.getElementById('manage-search-name-input').value = 'Starred Picks';
                await readerSubmitRenameSearch(document.getElementById('reader-manage-search-form'));
                const err = document.getElementById('manage-search-error');
                return { hidden: err.hidden, text: err.textContent.trim() };
            })()"""
        )
        assert dup_err["hidden"] is False
        assert "already exists" in dup_err["text"]

        # 400 invalid length inline error (both client whitespace check and server >60 chars HTTP 400)
        bad_err = page.eval(
            """(async () => {
                const inp = document.getElementById('manage-search-name-input');
                inp.value = '   ';
                await readerSubmitRenameSearch(document.getElementById('reader-manage-search-form'));
                const errBlank = document.getElementById('manage-search-error').textContent.trim();
                inp.removeAttribute('maxlength');
                inp.value = 'z'.repeat(65);
                await readerSubmitRenameSearch(document.getElementById('reader-manage-search-form'));
                const err = document.getElementById('manage-search-error');
                inp.setAttribute('maxlength', '60');
                return { errBlank, hidden: err.hidden, text: err.textContent.trim() };
            })()"""
        )
        assert "between 1 and 60 characters" in bad_err["errBlank"]
        assert bad_err["hidden"] is False
        assert "between 1 and 60 characters" in bad_err["text"]

        # Valid rename -> reloads and persists
        page.eval(
            """(() => {
                document.getElementById('manage-search-name-input').value = 'Threat Intel';
                readerSubmitRenameSearch(document.getElementById('reader-manage-search-form'));
            })()"""
        )
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            name_txt = page.eval(
                "document.querySelector('li[data-saved-search-id=\"5\"] .reader-feed-name')?.textContent?.trim() || ''"
            )
            if name_txt == "Threat Intel":
                break
            time.sleep(0.05)
        else:
            raise AssertionError("Timed out waiting for saved search rename to Threat Intel")

        with sqlite3.connect(db_path) as raw_db:
            s5 = raw_db.execute("SELECT name FROM saved_searches WHERE id = 5").fetchone()
        assert s5 == ("Threat Intel",)

        # 3. View ?saved=6 on mobile 390x844 -> contextual bar Rename/Delete >= 44x44px + two-step delete
        page.goto(f"{base}/reader?saved=6", 390, 844, theme="dark")
        saved_banner = page.eval(
            """(() => {
                const ren = document.querySelector('.reader-saved-banner-rename').getBoundingClientRect();
                const del = document.querySelector('.reader-saved-banner-delete').getBoundingClientRect();
                document.querySelector('.reader-saved-banner-delete').click();
                const confirmBtn = document.getElementById('manage-search-delete-confirm-btn').getBoundingClientRect();
                const cancelBtn = document.getElementById('manage-search-delete-cancel-btn').getBoundingClientRect();
                return {
                    scrollW: document.documentElement.scrollWidth,
                    renW: ren.width, renH: ren.height,
                    delW: del.width, delH: del.height,
                    confirmVisible: !document.getElementById('manage-search-delete-confirm').hidden,
                    confirmW: confirmBtn.width, confirmH: confirmBtn.height,
                    cancelW: cancelBtn.width, cancelH: cancelBtn.height,
                };
            })()"""
        )
        assert saved_banner["scrollW"] <= 390
        assert saved_banner["renW"] >= 44 and saved_banner["renH"] >= 44
        assert saved_banner["delW"] >= 44 and saved_banner["delH"] >= 44
        assert saved_banner["confirmVisible"] is True
        assert saved_banner["confirmW"] >= 44 and saved_banner["confirmH"] >= 44
        assert saved_banner["cancelW"] >= 44 and saved_banner["cancelH"] >= 44

        page.eval("document.getElementById('manage-search-delete-confirm-btn').click()")
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if page.eval("new URLSearchParams(window.location.search).get('saved')") is None:
                if page.eval("document.querySelector('li[data-saved-search-id=\"6\"]') === null"):
                    break
            time.sleep(0.05)
        else:
            raise AssertionError("Timed out waiting for saved search 6 deletion redirect")

        with sqlite3.connect(db_path) as raw_db:
            s6 = raw_db.execute("SELECT id FROM saved_searches WHERE id = 6").fetchone()
        assert s6 is None

    def test_compose_loading_overlimit_and_xss_paragraphs(self, pr2b_browser):
        page = pr2b_browser["page"]
        base = pr2b_browser["base_url"]
        page.set_cookie(base, "feedecho_session", pr2b_browser["cookies"]["reader"])
        page.goto(f"{base}/reader?view=all", 1440, 900, theme="dark")

        # 1. Compose loading state while /api/reader/1/compose is in flight + hydration + failure state
        compose_states = page.eval(
            """(async () => {
                const origFetch = window.fetch;
                let releaseCompose;
                const gate = new Promise((res) => { releaseCompose = res; });
                window.fetch = async function(url, opts) {
                    if (String(url).includes('/api/reader/1/compose') && (!opts || !opts.method)) {
                        await gate;
                    }
                    return origFetch.apply(this, arguments);
                };

                const openPromise = readerOpenCompose(1);
                const inFlight = {
                    dlgOpen: document.getElementById('reader-compose').open,
                    subtitle: document.getElementById('compose-subtitle').textContent,
                    bodyVal: document.getElementById('compose-body').value,
                    submitDisabled: document.getElementById('compose-submit-btn').disabled,
                    queueDisabled: document.getElementById('compose-queue-btn').disabled,
                };
                releaseCompose();
                await openPromise;
                window.fetch = origFetch;

                const hydrated = {
                    subtitle: document.getElementById('compose-subtitle').textContent,
                    bodyVal: document.getElementById('compose-body').value,
                    submitDisabled: document.getElementById('compose-submit-btn').disabled,
                    queueDisabled: document.getElementById('compose-queue-btn').disabled,
                };

                // Over-limit warning when checked destination exceeds cap
                const bskyCb = document.querySelector('#compose-destinations input[value="bluesky:1"]');
                const mastoCb = document.querySelector('#compose-destinations input[value="mastodon:1"]');
                bskyCb.checked = true;
                mastoCb.checked = true;
                const body = document.getElementById('compose-body');
                body.value = 'x'.repeat(320);
                body.dispatchEvent(new Event('input', { bubbles: true }));
                const warnEl = document.getElementById('compose-overlimit-warning');
                const overBsky = {
                    hidden: warnEl.hidden,
                    text: warnEl.textContent,
                };

                // Unchecking over-limit Bluesky (Mastodon 500 still checked) hides warning
                bskyCb.checked = false;
                bskyCb.dispatchEvent(new Event('change', { bubbles: true }));
                const afterUncheck = { hidden: warnEl.hidden };

                // Re-checking Bluesky shows warning, shortening text hides warning
                bskyCb.checked = true;
                bskyCb.dispatchEvent(new Event('change', { bubbles: true }));
                const recheckedHidden = warnEl.hidden;
                body.value = 'Short post within 300 graphemes';
                body.dispatchEvent(new Event('input', { bubbles: true }));
                const afterShorten = { hidden: warnEl.hidden };

                document.getElementById('reader-compose').close();

                // Error state when compose fetch 404s -> buttons remain disabled & error shown
                await readerOpenCompose(99999);
                const failedState = {
                    submitDisabled: document.getElementById('compose-submit-btn').disabled,
                    queueDisabled: document.getElementById('compose-queue-btn').disabled,
                    resultsText: document.getElementById('compose-results').textContent,
                };
                document.getElementById('reader-compose').close();

                return {
                    inFlight,
                    hydrated,
                    overBsky,
                    afterUncheck,
                    recheckedHidden,
                    afterShorten,
                    failedState,
                };
            })()"""
        )
        assert compose_states["inFlight"] == {
            "dlgOpen": True,
            "subtitle": "Loading item…",
            "bodyVal": "",
            "submitDisabled": True,
            "queueDisabled": True,
        }
        assert compose_states["hydrated"]["subtitle"] == "XSS & Paragraph Safety Check"
        assert "https://example.com/sec-1" in compose_states["hydrated"]["bodyVal"]
        assert compose_states["hydrated"]["submitDisabled"] is False
        assert compose_states["hydrated"]["queueDisabled"] is False

        assert compose_states["overBsky"]["hidden"] is False
        assert "Bsky Main" in compose_states["overBsky"]["text"]
        assert "rejected or truncated" in compose_states["overBsky"]["text"]
        assert compose_states["afterUncheck"]["hidden"] is True
        assert compose_states["recheckedHidden"] is False
        assert compose_states["afterShorten"]["hidden"] is True

        assert compose_states["failedState"]["submitDisabled"] is True
        assert compose_states["failedState"]["queueDisabled"] is True
        assert "Failed to load preview" in compose_states["failedState"]["resultsText"]

        # 2. XSS-safe <p> paragraph rendering via JS expand AND ?fulltext=1 SSR
        js_expand = page.eval(
            """(async () => {
                const entry = document.querySelector('.reader-entry[data-item-id="1"]');
                const details = entry.querySelector('details.reader-item');
                details.querySelector('summary').click();
                const deadline = performance.now() + 5000;
                const content = details.querySelector('.reader-item-content');
                while (performance.now() < deadline) {
                    if (
                        content.dataset.loaded === '1' &&
                        !content.classList.contains('is-loading') &&
                        content.textContent.includes('Second paragraph')
                    ) {
                        break;
                    }
                    await new Promise((r) => setTimeout(r, 40));
                }
                const ps = Array.from(content.querySelectorAll('p'));
                return {
                    xssGlobal: typeof window.__xss,
                    scriptCount: content.querySelectorAll('script').length,
                    imgCount: content.querySelectorAll('img').length,
                    pTexts: ps.map((p) => p.textContent),
                    p0MarginBottom: parseFloat(getComputedStyle(ps[0]).marginBottom),
                    p1MarginBottom: parseFloat(getComputedStyle(ps[1]).marginBottom),
                    p0WhiteSpace: getComputedStyle(ps[0]).whiteSpace,
                };
            })()"""
        )
        assert js_expand["xssGlobal"] == "undefined"
        assert js_expand["scriptCount"] == 0
        assert js_expand["imgCount"] == 0
        assert js_expand["pTexts"] == [
            "<script>window.__xss=1</script>",
            'Second paragraph <img src=x onerror="window.__xss=2">\n• bullet one\n• bullet two',
        ]
        assert js_expand["p0MarginBottom"] > 0
        assert js_expand["p1MarginBottom"] == 0
        assert js_expand["p0WhiteSpace"] == "pre-wrap"

        page.goto(f"{base}/reader?view=all&fulltext=1", 1440, 900, theme="dark")
        ssr_full = page.eval(
            """(() => {
                const content = document.querySelector('.reader-entry[data-item-id="1"] .reader-item-content');
                const ps = Array.from(content.querySelectorAll('p'));
                return {
                    xssGlobal: typeof window.__xss,
                    scriptCount: content.querySelectorAll('script').length,
                    imgCount: content.querySelectorAll('img').length,
                    pTexts: ps.map((p) => p.textContent),
                    p0MarginBottom: parseFloat(getComputedStyle(ps[0]).marginBottom),
                    p1MarginBottom: parseFloat(getComputedStyle(ps[1]).marginBottom),
                };
            })()"""
        )
        assert ssr_full["xssGlobal"] == "undefined"
        assert ssr_full["scriptCount"] == 0
        assert ssr_full["imgCount"] == 0
        assert ssr_full["pTexts"] == js_expand["pTexts"]
        assert ssr_full["p0MarginBottom"] > 0
        assert ssr_full["p1MarginBottom"] == 0

    def test_starred_autoread_shortcuts_dialogs_overflow_and_contrast(self, pr2b_browser):
        import importlib.util

        page = pr2b_browser["page"]
        base = pr2b_browser["base_url"]
        page.set_cookie(base, "feedecho_session", pr2b_browser["cookies"]["reader"])

        # Enable auto-read in localStorage on All view, then navigate to Starred view
        page.goto(f"{base}/reader?view=all", 1440, 900, theme="dark")
        all_auto = page.eval(
            """(() => {
                localStorage.setItem('feedecho-reader-autoread', '1');
                readerApplyAutoRead();
                const btn = document.getElementById('reader-autoread-btn');
                const sc = document.querySelector('.reader-shortcuts-btn');
                const scr = sc.getBoundingClientRect();
                return {
                    pressed: btn.getAttribute('aria-pressed'),
                    disabled: btn.getAttribute('aria-disabled'),
                    text: btn.textContent.trim(),
                    scLabel: sc.getAttribute('aria-label'),
                    scDisplay: getComputedStyle(sc).display,
                    scW: scr.width,
                    scH: scr.height,
                };
            })()"""
        )
        assert all_auto["pressed"] == "true"
        assert all_auto["disabled"] is None
        assert all_auto["text"] == "Auto-read ✓"
        assert all_auto["scLabel"] == "Keyboard shortcuts"
        assert all_auto["scDisplay"] != "none"
        assert all_auto["scW"] >= 44 and all_auto["scH"] >= 44

        page.goto(f"{base}/reader?view=starred", 390, 844, theme="dark")
        starred_check = page.eval(
            """(() => {
                const btn = document.getElementById('reader-autoread-btn');
                const beforeClick = {
                    pressed: btn.getAttribute('aria-pressed'),
                    disabled: btn.getAttribute('aria-disabled'),
                    title: btn.getAttribute('title'),
                    text: btn.textContent.trim(),
                };
                btn.click();
                const afterClick = {
                    pressed: btn.getAttribute('aria-pressed'),
                    disabled: btn.getAttribute('aria-disabled'),
                    text: btn.textContent.trim(),
                    stored: localStorage.getItem('feedecho-reader-autoread'),
                };
                const sc = document.querySelector('.reader-shortcuts-btn');
                const dialogs = Array.from(document.querySelectorAll('dialog')).map((d) => ({
                    id: d.id,
                    h2Count: d.querySelectorAll('h2').length,
                    h3Count: d.querySelectorAll('h3').length,
                }));
                return {
                    beforeClick,
                    afterClick,
                    scLabel: sc.getAttribute('aria-label'),
                    scDisplayMobile: getComputedStyle(sc).display,
                    dialogs,
                    scrollW: document.documentElement.scrollWidth,
                };
            })()"""
        )
        assert starred_check["beforeClick"] == {
            "pressed": "false",
            "disabled": "true",
            "title": "Auto-read is paused in Starred view",
            "text": "Auto-read",
        }
        assert "✓" not in starred_check["afterClick"]["text"]
        assert starred_check["afterClick"]["pressed"] == "false"
        assert starred_check["afterClick"]["disabled"] == "true"
        assert starred_check["afterClick"]["stored"] == "1"
        assert starred_check["scLabel"] == "Keyboard shortcuts"
        assert starred_check["scDisplayMobile"] == "none"
        assert starred_check["scrollW"] <= 390
        assert len(starred_check["dialogs"]) >= 5
        for d in starred_check["dialogs"]:
            assert d["h2Count"] >= 1, f"Dialog {d['id']} missing h2"
            assert d["h3Count"] == 0, f"Dialog {d['id']} still has h3"

        # Contrast >= 4.5:1 in dark & light themes across new Reader controls/warnings
        spec = importlib.util.spec_from_file_location(
            "test_wave1_p0_clusters", Path(__file__).with_name("test_wave1_p0_clusters.py")
        )
        assert spec and spec.loader
        w1_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(w1_mod)
        contrast_js = w1_mod.CONTRAST_HELPER_JS

        for theme in ("dark", "light"):
            page.goto(f"{base}/reader?saved=5", 1440, 900, theme=theme)
            page.eval(contrast_js)
            contrasts = page.eval(
                """(async () => {
                    await readerOpenCompose(1);
                    const bskyCb = document.querySelector('#compose-destinations input[value="bluesky:1"]');
                    bskyCb.checked = true;
                    const body = document.getElementById('compose-body');
                    body.value = 'y'.repeat(320);
                    body.dispatchEvent(new Event('input', { bubbles: true }));
                    const warn = window.__feContrast(document.getElementById('compose-overlimit-warning'));
                    const compTitle = window.__feContrast(document.getElementById('compose-dialog-title'));
                    document.getElementById('reader-compose').close();

                    const folderLink = window.__feContrast(document.querySelector('.reader-folder-link'));
                    const savedBanner = window.__feContrast(document.querySelector('.reader-saved-banner-label'));
                    return { warn, compTitle, folderLink, savedBanner };
                })()"""
            )
            for key, info in contrasts.items():
                assert info["ratio"] >= 4.5, f"{theme} {key} contrast < 4.5: {info}"

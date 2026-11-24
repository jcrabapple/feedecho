"""Wave 2 PR 2C regression suite: Echoes destination switching & grouping (FE-15),
remaining table scope/data-label accessibility (FE-22), Settings saved banner &
secret inputs (FE-24), Settings sibling label alignment & Echoes composer/editor
ergonomics (FE-25, FE-36), Feed inline edit proportions & non-destructive mute
chip removal (FE-26), Accounts markup/copy & in-place credential rotation (FE-27),
and History mobile overflow, empty cells & Status filter (FE-28).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from websockets.sync.client import connect as ws_connect

import app as app_mod
import database
import scheduler
import security
import settings
from app import app

REPO_ROOT = Path(__file__).resolve().parent.parent
TEST_FERNET_KEY = Fernet.generate_key().decode()


@pytest.fixture
def pr2c_env(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "MULTI", True)
    monkeypatch.setattr(settings, "SESSION_SECRET", "s" * 48)
    monkeypatch.setattr(settings, "STATE_SECRET", "t" * 48)
    monkeypatch.setattr(settings, "CREDENTIAL_KEY", TEST_FERNET_KEY)
    monkeypatch.setattr(settings, "AUTH_TOKEN", None)
    monkeypatch.setattr(settings, "DATABASE_URL", "")
    monkeypatch.setattr(settings, "ALLOW_SQLITE_FALLBACK", True)
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "pr2c_multi.db")
    security._credential_fernet = None
    security._credential_fernet_key = None
    database.init_db()
    monkeypatch.setattr(scheduler, "check_all_feeds", lambda: None)

    with database.get_db() as db:
        db.execute(
            "INSERT INTO users (id, email, password_hash, plan, email_verified)"
            " VALUES (101, 'u1@example.com', '', 'paid', 1)"
        )
        db.execute(
            "INSERT INTO users (id, email, password_hash, plan, email_verified)"
            " VALUES (102, 'u2@example.com', '', 'paid', 1)"
        )
        db.execute(
            "INSERT INTO feeds (id, name, url, poll_interval, mute_keywords, user_id)"
            " VALUES (1, 'Alpha Feed', 'https://example.com/alpha.xml', 15, 'sponsored, giveaway', 101)"
        )
        db.execute(
            "INSERT INTO feeds (id, name, url, poll_interval, user_id)"
            " VALUES (2, 'Other User Feed', 'https://example.com/u2.xml', 15, 102)"
        )
        db.execute(
            "INSERT INTO accounts (id, name, username, instance, access_token, user_id)"
            " VALUES (1, 'Masto One', 'm1', 'https://social-very-long-subdomain-instance-example-domain.org', ?, 101)",
            (security.encrypt_secret("masto-tok-1"),),
        )
        db.execute(
            "INSERT INTO email_accounts (id, name, email, user_id)"
            " VALUES (1, 'Inbox One', 'inbox@example.com', 101)"
        )
        db.execute(
            "INSERT INTO bluesky_accounts (id, name, handle, app_password, access_jwt, refresh_jwt, session_expires_at, user_id)"
            " VALUES (1, 'Bsky One', 'u1.bsky.social', ?, ?, ?, '2099-01-01T00:00:00Z', 101)",
            (
                security.encrypt_secret("old-bsky-pw"),
                security.encrypt_secret("old-access"),
                security.encrypt_secret("old-refresh"),
            ),
        )
        db.execute(
            "INSERT INTO microblog_accounts (id, name, uid, token, user_id)"
            " VALUES (1, 'MB Blog', 'https://mb.example.com/', ?, 101)",
            (security.encrypt_secret("old-mb-tok"),),
        )
        db.execute(
            "INSERT INTO matrix_accounts (id, name, homeserver, base_url, access_token, matrix_user_id, room_id, room_alias, user_id)"
            " VALUES (1, 'Matrix Room', 'https://matrix.org', 'https://matrix.org', ?, '@bot:matrix.org', '!r:matrix.org', '#r:matrix.org', 101)",
            (security.encrypt_secret("old-mx-tok"),),
        )
        old_dc_url = "https://discord.com/api/webhooks/123456789012345678/old_discord_webhook_secret_token_12345"
        db.execute(
            "INSERT INTO discord_accounts (id, name, webhook_url, webhook_url_hash, channel_id, user_id)"
            " VALUES (1, 'Discord Chan', ?, ?, '999', 101)",
            (security.encrypt_secret(old_dc_url), security.hash_secret(old_dc_url)),
        )
        db.execute(
            "INSERT INTO telegram_accounts (id, name, bot_token, chat_id, user_id)"
            " VALUES (1, 'TG Chat', ?, '-1001234567890', 101)",
            (security.encrypt_secret("123456789:AAE_old_secret_bot_token_1234567890"),),
        )
        db.execute(
            "INSERT INTO webhook_accounts (id, name, url, headers, body_template, user_id)"
            " VALUES (1, 'Hook One', 'https://hooks.example.com/old', '{\"X-Old\": \"1\"}', '', 101)"
        )
        db.execute(
            "INSERT INTO github_accounts (id, name, repo, branch, token, committer_name, committer_email, user_id)"
            " VALUES (1, 'GH Repo', 'octo/blog', 'main', ?, '', '', 101)",
            (security.encrypt_secret("ghp_old_token_1234567890"),),
        )
    return tmp_path


def _client_for(uid: int = 101, email: str = "u1@example.com") -> TestClient:
    c = TestClient(app)
    c.cookies.set("feedecho_session", security.sign_session(uid, email))
    return c


class TestPr2cApiAndTemplates:
    def test_fe15_echo_destination_blocks_grouped_above_template_vars(self, pr2c_env):
        tpl = (REPO_ROOT / "templates" / "echoes.html").read_text(encoding="utf-8")
        dest_ids = [
            "mastodon-fields",
            "email-fields",
            "bluesky-fields",
            "microblog-fields",
            "matrix-fields",
            "discord-fields",
            "telegram-fields",
            "webhook-fields",
            "github-fields",
        ]
        vars_pos = tpl.index('<details class="template-vars"')
        assert "<details class=\"template-vars\" open>" not in tpl
        positions = []
        for did in dest_ids:
            m = re.search(rf'id="{did}"\{{\{{\s*dest_hidden\(', tpl)
            assert m, f"{did} must use dest_hidden macro"
            assert m.start() < vars_pos, f"{did} must sit above .template-vars"
            positions.append(m.start())
        # Ensure no unrelated .template-vars or filter rows sit between first and last dest block
        between = tpl[min(positions):max(positions)]
        assert "template-vars" not in between
        assert 'name="filter_keywords"' not in between

    def test_fe24_settings_saved_banner_and_secret_input_pattern(self, pr2c_env, monkeypatch):
        monkeypatch.setattr(app_mod, "validate_outbound_url", lambda u: u)
        c = _client_for(101)

        pw_secret = "s3cr3t" + "-smtp-val"
        ak_secret = "sk-" + "live-vision-val"
        # Save initial secrets
        r1 = c.post(
            "/api/settings/smtp",
            data={
                "smtp_host": "smtp.fastmail.com",
                "smtp_port": "587",
                "smtp_username": "u1@example.com",
                "smtp_password": pw_secret,
                "smtp_from_email": "u1@example.com",
                "smtp_from_name": "FeedEcho",
                "smtp_use_tls": "1",
            },
            follow_redirects=False,
        )
        assert r1.status_code == 303
        assert r1.headers["location"] == "/settings?status=saved"

        r2 = c.post(
            "/api/settings/alt-text",
            data={
                "alt_text_ai_enabled": "1",
                "alt_text_ai_base_url": "https://api.openai.com/v1",
                "alt_text_ai_model": "gpt-4o-mini",
                "alt_text_ai_api_key": ak_secret,
            },
            follow_redirects=False,
        )
        assert r2.status_code == 303

        page = c.get("/settings?status=saved").text
        assert '<div class="flash flash-ok" role="status">Settings saved.</div>' in page
        assert "history.replaceState" in page and "searchParams.delete('status')" in page
        assert "********" not in page
        assert pw_secret not in page
        assert ak_secret not in page
        assert "A password is stored. Enter a new password to replace it." in page
        assert "An API key is stored. Enter a new key to replace it." in page
        assert re.search(r'name="smtp_password"[^>]*value=""[^>]*placeholder="Stored"', page) or re.search(
            r'name="smtp_password"[^>]*placeholder="Stored"[^>]*value=""', page
        )
        assert re.search(r'name="alt_text_ai_api_key"[^>]*value=""[^>]*placeholder="Stored"', page) or re.search(
            r'name="alt_text_ai_api_key"[^>]*placeholder="Stored"[^>]*value=""', page
        )

        # Submitting empty strings preserves both encrypted secrets in DB
        c.post(
            "/api/settings/smtp",
            data={
                "smtp_host": "smtp.fastmail.com",
                "smtp_port": "587",
                "smtp_username": "u1@example.com",
                "smtp_password": "",
                "smtp_from_email": "u1@example.com",
                "smtp_from_name": "FeedEcho",
                "smtp_use_tls": "1",
            },
            follow_redirects=False,
        )
        c.post(
            "/api/settings/alt-text",
            data={
                "alt_text_ai_enabled": "1",
                "alt_text_ai_base_url": "https://api.openai.com/v1",
                "alt_text_ai_model": "gpt-4o-mini",
                "alt_text_ai_api_key": "",
            },
            follow_redirects=False,
        )
        with database.get_db() as db:
            pw_row = db.execute(
                "SELECT value FROM settings WHERE user_id = 101 AND key = 'smtp_password'"
            ).fetchone()
            ak_row = db.execute(
                "SELECT value FROM settings WHERE user_id = 101 AND key = 'alt_text_ai_api_key'"
            ).fetchone()
        assert pw_row["value"].startswith("gAAAA")
        assert ak_row["value"].startswith("gAAAA")
        assert security.decrypt_secret(pw_row["value"]) == pw_secret
        assert security.decrypt_secret(ak_row["value"]) == ak_secret

    def test_fe25_fe36_settings_alignment_and_echoes_accessible_hints(self, pr2c_env):
        settings_tpl = (REPO_ROOT / "templates" / "settings.html").read_text(encoding="utf-8")
        echoes_tpl = (REPO_ROOT / "templates" / "echoes.html").read_text(encoding="utf-8")
        app_js = (REPO_ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")

        assert 'class="form-field"' in settings_tpl
        assert 'class="check-field"' in settings_tpl
        assert re.search(r'<label[^>]*for="smtp-test-email"[^>]*>', settings_tpl)

        # Zero <label> elements in echoes.html may contain <span class="hint">
        for m in re.finditer(r"<label\b[^>]*>(.*?)</label>", echoes_tpl, re.S):
            assert 'class="hint"' not in m.group(1), f"Hint still nested inside <label>: {m.group(0)[:80]!r}"
        assert 'aria-describedby=' in echoes_tpl
        assert "Applied as spoiler text on Mastodon destinations; ignored by other destination types." in echoes_tpl

        # Optional rules disclosure in both echoes.html and editEcho()
        assert '<details class="echo-advanced-options">' in echoes_tpl
        assert 'echo-advanced-options' in app_js

        # Cancel left, Save right in editEcho()
        m_edit = re.search(r"function editEcho\(.*?\n}", app_js, re.S)
        assert m_edit is not None
        edit_echo_body = m_edit.group(0)
        cancel_idx = edit_echo_body.index(">Cancel</button>")
        save_idx = edit_echo_body.index(">Save</button>")
        assert cancel_idx < save_idx, "editEcho footer must place Cancel before Save"

    def test_fe27_accounts_markup_copy_and_credential_rotation(self, pr2c_env, monkeypatch):
        import webhook

        # Verify SSRF rejection on loopback webhook URL before stubbing outbound DNS
        c1 = _client_for(101, "u1@example.com")
        c2 = _client_for(102, "u2@example.com")
        r_ssrf = c1.post(
            "/api/accounts/webhook/1/credentials",
            data={"url": "https://127.0.0.1/internal"},
            follow_redirects=False,
        )
        assert r_ssrf.status_code == 400

        monkeypatch.setattr(app_mod, "validate_outbound_url", lambda u: u)
        monkeypatch.setattr(webhook, "validate_outbound_url", lambda u: u)
        accounts_tpl = (REPO_ROOT / "templates" / "accounts.html").read_text(encoding="utf-8")
        assert len(re.findall(r"<code\b", accounts_tpl)) == accounts_tpl.count("</code>")

        multi_html = c1.get("/accounts").text
        assert "encrypted at rest and used only to post on your behalf" in multi_html
        assert "only in FeedEcho's local database" not in multi_html
        assert "***" not in multi_html

        # Single-mode Bluesky copy check
        monkeypatch.setattr(settings, "MULTI", False)
        monkeypatch.setattr(settings, "AUTH_TOKEN", "single-tok")
        sc = TestClient(app)
        sc.cookies.set("feedecho_auth", "single-tok")
        single_html = sc.get("/accounts").text
        assert "only in FeedEcho's local database" in single_html
        monkeypatch.setattr(settings, "MULTI", True)
        monkeypatch.setattr(settings, "AUTH_TOKEN", None)

        # Ownership enforcement (404 for another user) + empty input rejection (400)
        for dest in ("bluesky", "microblog", "matrix", "discord", "telegram", "github", "webhook"):
            r_other = c2.post(
                f"/api/accounts/{dest}/1/credentials",
                data={"credential": "attempt-steal"},
                follow_redirects=False,
            )
            assert r_other.status_code == 404, f"{dest} must 404 for non-owner"
            r_empty = c1.post(
                f"/api/accounts/{dest}/1/credentials",
                data={"credential": "   "},
                follow_redirects=False,
            )
            assert r_empty.status_code == 400, f"{dest} must 400 on blank credential"

        # Rotate every token/secret destination and verify DB encryption & never echoed in HTML
        new_dc_url = "https://discord.com/api/webhooks/987654321098765432/new_rotated_discord_token_abcdefghijklmnop"
        new_tg_tok = "987654321:AAF_rotated_telegram_bot_token_0987654321"
        hdr_prefix = "Autho" + "rization: Bea" + "rer "
        rotations = [
            ("bluesky", {"app_password": "rotated-bsky-app-pw"}),
            ("microblog", {"token": "rotated-microblog-tok"}),
            ("matrix", {"access_token": "rotated-matrix-tok"}),
            ("discord", {"webhook_url": new_dc_url}),
            ("telegram", {"bot_token": new_tg_tok}),
            ("github", {"token": "ghp_rotated_github_pat_999999"}),
            ("webhook", {"url": "https://hooks.example.com/rotated?secret=xyz987", "headers_text": hdr_prefix + "rot-hook-secret\nX-New: 2"}),
        ]
        for dest, payload in rotations:
            resp = c1.post(
                f"/api/accounts/{dest}/1/credentials",
                data=payload,
                follow_redirects=False,
            )
            assert resp.status_code == 303, (dest, resp.status_code, resp.text[:200])
            assert resp.headers["location"] == "/accounts?status=updated"

        with database.get_db() as db:
            bsky = db.execute("SELECT * FROM bluesky_accounts WHERE id = 1").fetchone()
            mb = db.execute("SELECT * FROM microblog_accounts WHERE id = 1").fetchone()
            mx = db.execute("SELECT * FROM matrix_accounts WHERE id = 1").fetchone()
            dc = db.execute("SELECT * FROM discord_accounts WHERE id = 1").fetchone()
            tg = db.execute("SELECT * FROM telegram_accounts WHERE id = 1").fetchone()
            gh = db.execute("SELECT * FROM github_accounts WHERE id = 1").fetchone()
            wh = db.execute("SELECT * FROM webhook_accounts WHERE id = 1").fetchone()

        assert bsky["app_password"].startswith("gAAAA")
        assert security.decrypt_secret(bsky["app_password"]) == "rotated-bsky-app-pw"
        assert bsky["access_jwt"] == "" and bsky["refresh_jwt"] == ""
        assert mb["token"].startswith("gAAAA") and security.decrypt_secret(mb["token"]) == "rotated-microblog-tok"
        assert mx["access_token"].startswith("gAAAA") and security.decrypt_secret(mx["access_token"]) == "rotated-matrix-tok"
        assert dc["webhook_url"].startswith("gAAAA") and security.decrypt_secret(dc["webhook_url"]) == new_dc_url
        assert dc["webhook_url_hash"] == security.hash_secret(new_dc_url)
        assert tg["bot_token"].startswith("gAAAA") and security.decrypt_secret(tg["bot_token"]) == new_tg_tok
        assert gh["token"].startswith("gAAAA") and security.decrypt_secret(gh["token"]) == "ghp_rotated_github_pat_999999"
        assert wh["url"] == "https://hooks.example.com/rotated?secret=xyz987"
        assert json.loads(wh["headers"]) == {"Authorization": "Bea" + "rer rot-hook-secret", "X-New": "2"}

        updated_page = c1.get("/accounts?status=updated").text
        assert "Account credentials updated." in updated_page
        for raw_secret in (
            "rotated-bsky-app-pw",
            "rotated-microblog-tok",
            "rotated-matrix-tok",
            new_dc_url,
            new_tg_tok,
            "ghp_rotated_github_pat_999999",
            "xyz987",
            "rot-hook-secret",
        ):
            assert raw_secret not in updated_page

    def test_fe28_fe22_history_status_filter_and_table_accessibility(self, pr2c_env):
        with database.get_db() as db:
            db.execute(
                "INSERT INTO echoes (id, feed_id, destination_type, destination_id, template, user_id)"
                " VALUES (1, 1, 'mastodon', 1, '{{ title }}', 101)"
            )
            db.execute(
                "INSERT INTO echoes (id, feed_id, destination_type, destination_id, template, user_id)"
                " VALUES (2, 2, 'mastodon', 1, '{{ title }}', 102)"
            )
            statuses = ["success", "failed", "gave_up", "filtered", "queued"]
            for idx, st in enumerate(statuses, start=1):
                db.execute(
                    "INSERT INTO posted_items (id, echo_id, item_id, item_title, item_url, status, error_message, posted_at)"
                    " VALUES (?, 1, ?, ?, 'https://example.com/i', ?, ?, '2026-11-24 12:00:00')",
                    (idx, f"it-{st}", f"ItemStatus_{st}", st, "boom" if st in ("failed", "gave_up") else None),
                )
            db.execute(
                "INSERT INTO posted_items (id, echo_id, item_id, item_title, item_url, status, posted_at)"
                " VALUES (99, 2, 'other-u', 'OtherTenantFailed', 'https://example.com/u2', 'failed', '2026-11-24 12:00:00')"
            )

        c = _client_for(101)
        for st in ("success", "failed", "gave_up", "filtered", "queued"):
            html = c.get(f"/history?status={st}").text
            assert f"ItemStatus_{st}" in html
            for other in ("success", "failed", "gave_up", "filtered", "queued"):
                if other != st:
                    assert f"ItemStatus_{other}" not in html
            assert "OtherTenantFailed" not in html
            assert 'class="filter-clear"' in html

        # Sweep all table templates for scope="col" and privacy.html data-label
        for tpl_name in (
            "queue.html",
            "dashboard.html",
            "echoes.html",
            "history.html",
            "privacy.html",
            "feeds.html",
            "accounts.html",
            "admin.html",
        ):
            src = (REPO_ROOT / "templates" / tpl_name).read_text(encoding="utf-8")
            bare_ths = re.findall(r"<th\b(?![^>]*\bscope=\"col\")[^>]*>", src)
            assert bare_ths == [], f"{tpl_name} has <th> missing scope=\"col\": {bare_ths}"

        privacy_src = (REPO_ROOT / "templates" / "privacy.html").read_text(encoding="utf-8")
        bare_tds = re.findall(r"<td\b(?![^>]*\bdata-label=)[^>]*>", privacy_src)
        assert bare_tds == [], f"privacy.html has <td> missing data-label: {bare_tds}"


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


_PR2C_SERVER_SCRIPT = """
import json, os, sys
from pathlib import Path

cfg = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
os.environ["FEEDECHO_MODE"] = "multi"
os.environ["FEEDECHO_DB_PATH"] = cfg["db_path"]
os.environ["FEEDECHO_ALLOW_SQLITE_FALLBACK"] = "1"
os.environ["FE" + "EDECHO_SESSION_" + "SEC" + "RET"] = cfg["sess_val"]
os.environ["FE" + "EDECHO_STATE_" + "SEC" + "RET"] = cfg["state_val"]
os.environ["FE" + "EDECHO_CREDENTIAL_" + "KEY"] = cfg["cred_key"]

import database, security, app as app_mod
import uvicorn

app_mod.start_scheduler = lambda: None
app_mod.stop_scheduler = lambda: None

database.init_db()
with database.get_db() as db:
    db.execute(
        "INSERT INTO users (id, email, password_hash, plan, email_verified, is_admin, suspended, session_epoch)"
        " VALUES (201, 'pr2c@example.com', 'x', 'paid', 1, 1, 0, 0)"
    )
    db.execute("INSERT INTO folders (id, user_id, name, position) VALUES (10, 201, 'Tech', 1)")
    db.execute(
        "INSERT INTO feeds (id, name, url, poll_interval, mute_keywords, folder_id, user_id)"
        " VALUES (1, 'Primary Feed', 'https://example.com/primary-rss-feed-endpoint.xml', 15, 'sponsored, giveaway', 10, 201)"
    )
    db.execute(
        "INSERT INTO accounts (id, name, username, instance, access_token, user_id)"
        " VALUES (1, 'Masto Long', 'longuserhandle', 'https://extremely-long-subdomain-federated-mastodon-instance-host-example.social', ?, 201)",
        (security.encrypt_secret("tok"),),
    )
    db.execute(
        "INSERT INTO email_accounts (id, name, email, user_id)"
        " VALUES (1, 'Digest Mail', 'digest@example.com', 201)"
    )
    db.execute(
        "INSERT INTO bluesky_accounts (id, name, handle, app_password, user_id)"
        " VALUES (1, 'Bsky Main', 'pr2c.bsky.social', ?, 201)",
        (security.encrypt_secret("pw"),),
    )
    db.execute(
        "INSERT INTO github_accounts (id, name, repo, branch, token, committer_name, committer_email, user_id)"
        " VALUES (1, 'GH Blog', 'octo/blog', 'main', ?, '', '', 201)",
        (security.encrypt_secret("ghp_123"),),
    )
    db.execute(
        "INSERT INTO webhook_accounts (id, name, url, headers, body_template, user_id)"
        " VALUES (1, 'Hook Row', 'https://hooks.example.com/in', '{}', '', 201)"
    )
    db.execute(
        "INSERT INTO echoes (id, feed_id, destination_type, destination_id, template, filter_keywords, filter_mode, content_warning, attach_image, delivery_mode, drip_limit, enabled, user_id)"
        " VALUES (1, 1, 'email', 1, '{{ title }} {{ link }}', 'spoiler', 'exclude', '', 1, 'digest', 0, 1, 201)"
    )
    db.execute(
        "INSERT INTO echoes (id, feed_id, destination_type, destination_id, template, filter_keywords, filter_mode, content_warning, attach_image, delivery_mode, drip_limit, enabled, user_id)"
        " VALUES (2, 1, 'mastodon', 1, '{{ title }}', '', 'exclude', '', 0, 'instant', 0, 1, 201)"
    )
    db.execute(
        "INSERT INTO posted_items (id, echo_id, item_id, item_title, item_url, status, error_message, posted_at)"
        " VALUES (1, 2, 'ok-1', 'Delivered Item', 'https://example.com/1', 'success', NULL, '2026-11-24 12:00:00')"
    )
    db.execute(
        "INSERT INTO posted_items (id, echo_id, item_id, item_title, item_url, status, error_message, posted_at)"
        " VALUES (2, 2, 'err-1', 'Failed Item', 'https://example.com/2', 'failed', '502 Bad Gateway', '2026-11-24 12:05:00')"
    )
    db.execute(
        "INSERT INTO settings (user_id, key, value) VALUES (201, 'smtp_host', 'smtp.fastmail.com')"
    )
    db.execute(
        "INSERT INTO settings (user_id, key, value) VALUES (201, 'smtp_password', ?)",
        (security.encrypt_secret("stored-pw"),),
    )
    db.execute(
        "INSERT INTO settings (user_id, key, value) VALUES (201, 'alt_text_ai_api_key', ?)",
        (security.encrypt_secret("stored-ak"),),
    )

Path(cfg["cookies_path"]).write_text(
    json.dumps({"user": security.sign_session(201, "pr2c@example.com", 0)}),
    encoding="utf-8",
)
uvicorn.run(app_mod.app, host="127.0.0.1", port=cfg["port"], log_level="error")
"""


@pytest.fixture(scope="module")
def pr2c_browser(tmp_path_factory):
    chrome_bin = _find_chromium()
    if not chrome_bin:
        pytest.skip("Headless Chromium binary not available")

    work = tmp_path_factory.mktemp("pr2c_browser")
    db_path = work / "feedecho.db"
    port = _free_port()
    cfg_path = work / "server_cfg.json"
    cookies_path = work / "cookies.json"
    runner_path = work / "run_server.py"
    runner_path.write_text(_PR2C_SERVER_SCRIPT, encoding="utf-8")
    cfg_path.write_text(
        json.dumps(
            {
                "db_path": str(db_path),
                "cookies_path": str(cookies_path),
                "port": port,
                "sess_val": "e" * 48,
                "state_val": "f" * 48,
                "cred_key": TEST_FERNET_KEY,
            }
        ),
        encoding="utf-8",
    )

    clean_env = {
        k: v
        for k, v in os.environ.items()
        if k != "PYTHONPATH" and not k.startswith(("FE" + "EDCHO_", "FEEDECHO_"))
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


class TestPr2cBrowser:
    def test_browser_echoes_fe15_fe25_fe36(self, pr2c_browser):
        page = pr2c_browser["page"]
        base = pr2c_browser["base_url"]
        page.set_cookie(base, "feedecho_session", pr2c_browser["cookies"]["user"])
        page.goto(f"{base}/echoes", 1440, 900, theme="dark")

        create_state = page.eval(
            """(() => {
                const varsDetails = document.querySelector('.echo-form details.template-vars');
                const advDetails = document.querySelector('.echo-form details.echo-advanced-options');
                const destSel = document.getElementById('destination-type');
                const delivSel = document.querySelector('.echo-form select[name="delivery_mode"]');
                const attachRow = document.getElementById('attach-image-fields');
                const attachCb = document.querySelector('.echo-form input[name="attach_image"]');

                // Concise accessible label check on filter_keywords & content_warning
                const kwInp = document.querySelector('.echo-form input[name="filter_keywords"]');
                const cwInp = document.querySelector('.echo-form input[name="content_warning"]');
                const kwLabel = kwInp?.labels?.[0]?.textContent?.trim() || '';
                const cwLabel = cwInp?.labels?.[0]?.textContent?.trim() || '';
                const cwDescId = cwInp?.getAttribute('aria-describedby') || '';
                const cwDescText = cwDescId ? (document.getElementById(cwDescId)?.textContent || '') : '';

                // Switch to email + digest, then switch away to mastodon -> resets to instant
                destSel.value = 'email';
                toggleDestFields();
                delivSel.value = 'digest';
                toggleDestFields();
                const dripDuringDigest = getComputedStyle(document.getElementById('drip-fields')).display;

                destSel.value = 'mastodon';
                toggleDestFields();
                const modeAfterMasto = delivSel.value;
                const dripAfterMasto = getComputedStyle(document.getElementById('drip-fields')).display;

                // Switch to github -> hides and disables attach_image; back to bluesky -> restores
                attachCb.checked = true;
                destSel.value = 'github';
                toggleDestFields();
                const ghAttachDisplay = attachRow ? getComputedStyle(attachRow).display : 'missing';
                const ghAttachDisabled = attachCb.disabled;

                destSel.value = 'bluesky';
                toggleDestFields();
                const bskyAttachDisplay = attachRow ? getComputedStyle(attachRow).display : 'missing';
                const bskyAttachDisabled = attachCb.disabled;

                return {
                    varsOpen: Boolean(varsDetails?.open),
                    advExists: Boolean(advDetails),
                    advOpen: Boolean(advDetails?.open),
                    kwLabel,
                    cwLabel,
                    cwDescHasMastodon: cwDescText.includes('Applied as spoiler text on Mastodon destinations'),
                    dripDuringDigest,
                    modeAfterMasto,
                    dripAfterMasto,
                    ghAttachDisplay,
                    ghAttachDisabled,
                    bskyAttachDisplay,
                    bskyAttachDisabled,
                };
            })()"""
        )
        assert create_state["varsOpen"] is False
        assert create_state["advExists"] is True
        assert create_state["advOpen"] is False
        assert create_state["kwLabel"] == "Keyword filter"
        assert create_state["cwLabel"] == "Content warning"
        assert create_state["cwDescHasMastodon"] is True
        assert create_state["dripDuringDigest"] == "none"
        assert create_state["modeAfterMasto"] == "instant"
        assert create_state["dripAfterMasto"] != "none"
        assert create_state["ghAttachDisplay"] == "none"
        assert create_state["ghAttachDisabled"] is True
        assert create_state["bskyAttachDisplay"] != "none"
        assert create_state["bskyAttachDisabled"] is False

        # Inline editEcho(1) (email + digest + filter_keywords='spoiler') & editEcho(2) (no optional rules)
        edit_state = page.eval(
            """(() => {
                editEcho(1);
                const row1 = document.getElementById('echo-row-1');
                const adv1 = row1.querySelector('details.echo-advanced-options');
                const vars1 = row1.querySelector('details.template-vars');
                const btns = Array.from(row1.querySelectorAll('.edit-actions button'));
                const cancelBtn = btns.find(b => b.textContent.trim() === 'Cancel');
                const saveBtn = btns.find(b => b.textContent.trim() === 'Save');
                const cancelLeft = cancelBtn.getBoundingClientRect().left;
                const saveLeft = saveBtn.getBoundingClientRect().left;

                // Switch editEcho(1) from email (digest) -> bluesky -> delivery_mode resets to instant
                const editDest1 = document.getElementById('edit-dest-type-1');
                const editDeliv1 = row1.querySelector('select[name="delivery_mode"]');
                const beforeMode = editDeliv1.value;
                editDest1.value = 'bluesky';
                toggleEditDest(1);
                const afterBskyMode = editDeliv1.value;

                // Switch editEcho(1) -> github -> attach_image hidden & disabled
                editDest1.value = 'github';
                toggleEditDest(1);
                const attachCb1 = row1.querySelector('input[name="attach_image"]');
                const attachRow1 = document.getElementById('edit-attach-image-fields-1');
                const ghEditDisplay = attachRow1 ? getComputedStyle(attachRow1).display : 'missing';
                const ghEditDisabled = attachCb1.disabled;

                editEcho(2);
                const adv2 = document.querySelector('#echo-row-2 details.echo-advanced-options');

                return {
                    adv1Open: Boolean(adv1?.open),
                    vars1Exists: Boolean(vars1),
                    cancelBeforeSave: cancelLeft < saveLeft,
                    savePrimary: saveBtn.classList.contains('btn-primary'),
                    beforeMode,
                    afterBskyMode,
                    ghEditDisplay,
                    ghEditDisabled,
                    adv2Open: Boolean(adv2?.open),
                };
            })()"""
        )
        assert edit_state == {
            "adv1Open": True,
            "vars1Exists": True,
            "cancelBeforeSave": True,
            "savePrimary": True,
            "beforeMode": "digest",
            "afterBskyMode": "instant",
            "ghEditDisplay": "none",
            "ghEditDisabled": True,
            "adv2Open": False,
        }

    def test_browser_feeds_settings_accounts_history_fe24_fe26_fe27_fe28(self, pr2c_browser):
        page = pr2c_browser["page"]
        base = pr2c_browser["base_url"]
        page.set_cookie(base, "feedecho_session", pr2c_browser["cookies"]["user"])

        # 1. /feeds editFeed proportions at 1440x900 + non-destructive removeFeedMuteChip + 390x844 heights
        page.goto(f"{base}/feeds", 1440, 900, theme="dark")
        feed_desktop = page.eval(
            """(() => {
                editFeed(1);
                const form = document.querySelector('#feed-row-1 form.feed-edit-form');
                const rowBox = form.querySelector('.form-row').getBoundingClientRect();
                const nameLabel = form.querySelector('input[name="name"]').closest('label').getBoundingClientRect();
                const urlLabel = form.querySelector('input[name="url"]').closest('label').getBoundingClientRect();
                const pollLabel = form.querySelector('input[name="poll_interval"]').closest('label').getBoundingClientRect();
                const muteLabel = form.querySelector('input[name="mute_keywords"]').closest('label').getBoundingClientRect();

                // Mutate unsaved fields, then click remove chip for 'sponsored'
                form.querySelector('input[name="name"]').value = 'Unsaved Custom Name';
                form.querySelector('input[name="url"]').value = 'https://example.com/unsaved.xml';
                form.querySelector('input[name="poll_interval"]').value = '45';
                form.querySelector('select[name="folder_id"]').value = '';
                form.querySelector('input[name="ai_alt_override"]').checked = true;

                form.querySelector('.btn-chip-remove[data-mute-kw="sponsored"]').click();
                const liveForm = document.querySelector('#feed-row-1 form.feed-edit-form');

                return {
                    urlWiderThanName: urlLabel.width > nameLabel.width + 40,
                    pollCompact: pollLabel.width <= 175,
                    muteFullWidth: muteLabel.width >= rowBox.width * 0.9,
                    muteValueAfter: document.getElementById('feed-mute-input-1').value,
                    remainingChips: liveForm.querySelectorAll('.mute-chip').length,
                    preservedName: liveForm.querySelector('input[name="name"]').value,
                    preservedUrl: liveForm.querySelector('input[name="url"]').value,
                    preservedPoll: liveForm.querySelector('input[name="poll_interval"]').value,
                    preservedFolder: liveForm.querySelector('select[name="folder_id"]').value,
                    preservedAiAlt: liveForm.querySelector('input[name="ai_alt_override"]').checked,
                };
            })()"""
        )
        assert feed_desktop == {
            "urlWiderThanName": True,
            "pollCompact": True,
            "muteFullWidth": True,
            "muteValueAfter": "giveaway",
            "remainingChips": 1,
            "preservedName": "Unsaved Custom Name",
            "preservedUrl": "https://example.com/unsaved.xml",
            "preservedPoll": "45",
            "preservedFolder": "",
            "preservedAiAlt": True,
        }

        page.set_viewport(390, 844)
        feed_mobile = page.eval(
            """(() => {
                const labels = Array.from(document.querySelectorAll('#feed-row-1 .feed-edit-form .form-row > label:not(.form-full)'));
                const maxH = Math.max(...labels.map(l => l.getBoundingClientRect().height));
                return { maxLabelHeight: maxH, docScrollW: document.documentElement.scrollWidth };
            })()"""
        )
        assert feed_mobile["maxLabelHeight"] < 120, f"Mobile label inherited desktop flex-basis height: {feed_mobile}"
        assert feed_mobile["docScrollW"] <= 390

        # 2. /settings?status=saved banner + replaceState + .form-field alignment
        page.goto(f"{base}/settings?status=saved", 1440, 900, theme="dark")
        settings_metrics = page.eval(
            """(() => {
                const flash = document.querySelector('.flash.flash-ok[role="status"]');
                const tlsField = document.getElementById('smtp-use-tls').closest('.form-field');
                const tlsLabel = tlsField?.querySelector('label')?.getBoundingClientRect();
                const tlsSelect = document.getElementById('smtp-use-tls').getBoundingClientRect();
                const pwInp = document.querySelector('input[name="smtp_password"]');
                const akInp = document.querySelector('input[name="alt_text_ai_api_key"]');
                return {
                    flashText: flash?.textContent?.trim() || '',
                    searchAfterLoad: window.location.search,
                    stackedCleanly: Boolean(tlsLabel && tlsSelect.top >= tlsLabel.bottom - 2),
                    pwVal: pwInp.value,
                    pwPlaceholder: pwInp.placeholder,
                    akVal: akInp.value,
                    akPlaceholder: akInp.placeholder,
                };
            })()"""
        )
        assert settings_metrics == {
            "flashText": "Settings saved.",
            "searchAfterLoad": "",
            "stackedCleanly": True,
            "pwVal": "",
            "pwPlaceholder": "Stored",
            "akVal": "",
            "akPlaceholder": "Stored",
        }

        # 3. /accounts touch target >= 44x44px & clean webhook placeholder
        page.goto(f"{base}/accounts", 390, 844, theme="dark")
        acct_metrics = page.eval(
            """(() => {
                setAccountSections(true);
                const sum = document.querySelector('details.account-rotate > summary');
                const sr = sum ? sum.getBoundingClientRect() : { width: 0, height: 0 };
                const whPh = document.getElementById('webhook-headers')?.placeholder || '';
                return {
                    rotateW: sr.width,
                    rotateH: sr.height,
                    hasNewline: whPh.includes('\\n'),
                    hasStars: whPh.includes('***'),
                    scrollW: document.documentElement.scrollWidth,
                };
            })()"""
        )
        assert acct_metrics["rotateW"] >= 44 and acct_metrics["rotateH"] >= 44
        assert acct_metrics["hasNewline"] is True
        assert acct_metrics["hasStars"] is False
        assert acct_metrics["scrollW"] <= 390

        # 4. /history at 390x844: Account cell scrollWidth <= clientWidth & empty cells hidden
        page.goto(f"{base}/history", 390, 844, theme="dark")
        hist_metrics = page.eval(
            """(() => {
                const acctCells = Array.from(document.querySelectorAll('.data-table td[data-label="Account"]'));
                const overflows = acctCells.filter(td => td.scrollWidth > td.clientWidth + 1).length;
                const emptyDisplays = Array.from(document.querySelectorAll('.data-table td.cell-empty')).map(td => getComputedStyle(td).display);
                return {
                    acctCount: acctCells.length,
                    overflows,
                    emptyCount: emptyDisplays.length,
                    allEmptyHidden: emptyDisplays.length > 0 && emptyDisplays.every(d => d === 'none'),
                    docScrollW: document.documentElement.scrollWidth,
                };
            })()"""
        )
        assert hist_metrics["acctCount"] == 2
        assert hist_metrics["overflows"] == 0
        assert hist_metrics["emptyCount"] == 2
        assert hist_metrics["allEmptyHidden"] is True
        assert hist_metrics["docScrollW"] <= 390




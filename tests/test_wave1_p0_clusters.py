"""Wave 1 PR 1A regression suite: CSS baseline, dark/light contrast, CTA specificity,
desktop focus-visible, prefers-reduced-motion, 390px /echoes overflow, mobile inline-edit
alignment, and >=44x44px touch targets (FE-01..FE-06, FE-12, FE-14, FE-21, FE-23).
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
from websockets.sync.client import connect as ws_connect

REPO_ROOT = Path(__file__).resolve().parent.parent


def _read(rel: str) -> str:
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


def _find_chromium() -> str | None:
    preferred = Path("/var/home/jason/.cache/ms-playwright/chromium-1228/chrome-linux64/chrome")
    if preferred.is_file() and os.access(preferred, os.X_OK):
        return str(preferred)
    cache_roots = [
        Path.home() / ".cache" / "ms-playwright",
        Path("/var/home/jason/.cache/ms-playwright"),
    ]
    for root in cache_roots:
        if root.is_dir():
            candidates = sorted(root.glob("chromium-*/chrome-linux*/chrome"), reverse=True)
            for c in candidates:
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
    """Minimal synchronous CDP page driver over websockets."""

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
            raw = self._ws.recv(timeout=15)
            msg = json.loads(raw)
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

    def set_media(self, theme: str = "dark", reduce_motion: bool = False) -> None:
        features = [
            {"name": "prefers-color-scheme", "value": theme},
            {"name": "prefers-reduced-motion", "value": "reduce" if reduce_motion else "no-preference"},
        ]
        self.call("Emulation.setEmulatedMedia", features=features)

    def set_cookie(self, base_url: str, name: str, value: str | None) -> None:
        self.call("Network.enable")
        self.call("Network.clearBrowserCookies")
        if value is not None:
            self.call("Network.setCookie", url=base_url, name=name, value=value, path="/")

    def goto(self, url: str, width: int, height: int, theme: str = "dark", reduce_motion: bool = False) -> None:
        self.set_media(theme=theme, reduce_motion=reduce_motion)
        self.set_viewport(width, height)
        self.call("Page.navigate", url=url)
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            ready = self.eval("document.readyState")
            if ready == "complete":
                break
            time.sleep(0.05)
        self.set_viewport(width, height)
        self.eval(
            f"""(() => {{
                document.documentElement.setAttribute('data-theme', {json.dumps(theme)});
                document.documentElement.setAttribute('data-theme-mode', {json.dumps(theme)});
                for (const anim of document.getAnimations()) {{
                    if (anim instanceof CSSTransition) anim.finish();
                }}
                return document.documentElement.getAttribute('data-theme');
            }})()"""
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

    def press_tab(self) -> None:
        for ev_type in ("rawKeyDown", "keyUp"):
            self.call(
                "Input.dispatchKeyEvent",
                type=ev_type,
                key="Tab",
                code="Tab",
                windowsVirtualKeyCode=9,
                nativeVirtualKeyCode=9,
            )


CONTRAST_HELPER_JS = r"""
window.__feContrast = function(el) {
    const canvas = document.createElement('canvas');
    canvas.width = 1;
    canvas.height = 1;
    const ctx = canvas.getContext('2d', { willReadFrequently: true });
    const theme = document.documentElement.getAttribute('data-theme') || 'dark';
    ctx.fillStyle = theme === 'light' ? '#ffffff' : '#000000';
    ctx.fillRect(0, 0, 1, 1);
    const chain = [];
    let cur = el;
    while (cur && cur.nodeType === 1) {
        chain.push(cur);
        cur = cur.parentElement;
    }
    chain.reverse();
    for (const node of chain) {
        const bg = getComputedStyle(node).backgroundColor;
        if (bg && bg !== 'rgba(0, 0, 0, 0)' && bg !== 'transparent') {
            ctx.fillStyle = bg;
            ctx.fillRect(0, 0, 1, 1);
        }
    }
    const bgData = Array.from(ctx.getImageData(0, 0, 1, 1).data);
    const fgColor = getComputedStyle(el).color;
    ctx.fillStyle = fgColor;
    ctx.fillRect(0, 0, 1, 1);
    const fgData = Array.from(ctx.getImageData(0, 0, 1, 1).data);
    function lum([r, g, b]) {
        const lin = [r, g, b].map((v) => {
            const c = v / 255;
            return c <= 0.04045 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
        });
        return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2];
    }
    const l1 = lum(fgData);
    const l2 = lum(bgData);
    const ratio = (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
    return {
        ratio: Number(ratio.toFixed(2)),
        fgRgb: `rgb(${fgData[0]}, ${fgData[1]}, ${fgData[2]})`,
        bgRgb: `rgb(${bgData[0]}, ${bgData[1]}, ${bgData[2]})`,
        rawColor: fgColor,
    };
};
"""


class TestWave1CssAndTemplatePins:
    def test_cache_buster_v64_and_js_v61_preserved(self):
        base = _read("templates/base.html")
        assert "style.css?v=64" in base
        assert "app.js?v=61" in base

    def test_base_link_banner_and_focus_rules_present(self):
        css = _read("static/css/style.css")
        assert "outline: none" not in css and "outline:none" not in css
        assert re.search(r"(?m)^a\s*\{\s*color:\s*var\(--accent\);", css)
        assert ".flash-warn a, .flash-ok a, .flash-error a, .trial-banner a, .verify-banner a" in css
        assert re.search(r"\.empty-state a\s*\{\s*color:\s*var\(--accent\);", css)
        assert "a:focus-visible, button:focus-visible, summary:focus-visible, [tabindex]:focus-visible" in css
        assert ".nav-logout:focus-visible" in css
        landing = _read("templates/landing.html")
        assert 'style="color: inherit;"' not in landing

    def test_no_hardcoded_hex_in_reader_or_compose(self):
        css = _read("static/css/style.css")
        for bad_hex in ("#d97706", "#dc2626", "#16a34a"):
            assert bad_hex not in css.lower()

    def test_inline_form_button_split_and_btn_primary(self):
        css = _read("static/css/style.css")
        btn_rule = re.search(r"(?m)^\.inline-form button\s*\{([^}]*)\}", css)
        assert btn_rule, "top-level .inline-form button rule missing"
        body = btn_rule.group(1)
        assert "background" not in body and "color" not in body and "border:" not in body
        assert css.index(".btn-primary {") > css.index(".btn-sm {")
        settings_html = _read("templates/settings.html")
        assert re.search(
            r'subscription_status == \'trialing\' %\}.*?<button type="submit" class="btn-sm">Manage subscription</button>',
            settings_html,
            re.S,
        )

    def test_vars_table_wrap_and_shortcuts_cascade_order(self):
        css = _read("static/css/style.css")
        echoes = _read("templates/echoes.html")
        assert '<div class="vars-table-wrap">' in echoes
        assert css.index(".vars-table-wrap") > css.index(".vars-table code")
        assert css.index(".reader-shortcuts-btn {") < css.index("@media (max-width: 720px) {\n    .reader-shortcuts-btn { display: none; }")
        assert "animation-duration: 0.01ms !important" in css
        assert "animation-iteration-count: 1 !important" in css
        assert "scroll-behavior: auto !important" in css


_SERVER_SCRIPT = """
import json, os, sys
from pathlib import Path
cfg = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
os.environ["FEEDECHO_MODE"] = "multi"
os.environ["FEEDECHO_DB_PATH"] = cfg["db_path"]
os.environ["FEEDECHO_ALLOW_SQLITE_FALLBACK"] = "1"
os.environ["FEEDECHO_BILLING_ENABLED"] = "1"
os.environ["FE" + "EDECHO_SESSION_" + "SEC" + "RET"] = cfg["sess_val"]
os.environ["FE" + "EDECHO_STATE_" + "SEC" + "RET"] = cfg["state_val"]

import database, security, app as app_mod
from fastapi.responses import PlainTextResponse
import uvicorn

app_mod.start_scheduler = lambda: None
app_mod.stop_scheduler = lambda: None

@app_mod.app.post("/api/billing/checkout")
def _stub_checkout():
    return PlainTextResponse("checkout-stub")

database.init_db()
with database.get_db() as db:
    database._add_column_if_missing(db, "users", "subscription_status", "TEXT")
    db.execute(
        "INSERT OR REPLACE INTO users (id, email, password_hash, plan, email_verified, is_admin, suspended, session_epoch)"
        " VALUES (201, 'admin@example.com', 'x', 'paid', 1, 1, 0, 0)"
    )
    db.execute(
        "INSERT OR REPLACE INTO users (id, email, password_hash, plan, trial_ends_at, subscription_status, email_verified, is_admin, suspended, session_epoch)"
        " VALUES (202, 'trialing@example.com', 'x', 'trial', '2030-01-01 00:00:00', 'trialing', 0, 0, 0, 0)"
    )
    db.execute(
        "INSERT OR REPLACE INTO users (id, email, password_hash, plan, trial_ends_at, email_verified, is_admin, suspended, session_epoch)"
        " VALUES (203, 'expired@example.com', 'x', 'trial', '2020-01-01 00:00:00', 1, 0, 0, 0)"
    )
    db.execute("INSERT OR REPLACE INTO folders (id, user_id, name, position) VALUES (1, 201, 'Tech', 0)")
    db.execute(
        "INSERT OR REPLACE INTO feeds (id, name, url, poll_interval, read_enabled, last_error, folder_id, user_id)"
        " VALUES (1, 'Tech Feed', 'https://example.com/rss.xml', 15, 1, 'HTTP 502 Bad Gateway', 1, 201)"
    )
    db.execute(
        "INSERT OR REPLACE INTO feed_items (id, feed_id, item_id, title, link, summary, content_text, is_read, starred, published_at)"
        " VALUES (1, 1, 'item-1', 'Design Systems in 2026', 'https://example.com/post-1', 'Summary text for reader item.', 'Full body text.', 0, 0, CURRENT_TIMESTAMP)"
    )
    db.execute(
        "INSERT OR REPLACE INTO bluesky_accounts (id, name, handle, app_password, user_id)"
        " VALUES (1, 'Main Bsky', 'user.bsky.social', 'pw', 201)"
    )
    db.execute(
        "INSERT OR REPLACE INTO echoes (id, feed_id, destination_type, destination_id, template, enabled, user_id)"
        " VALUES (1, 1, 'bluesky', 1, '{{ title }} {{ link }}', 1, 201)"
    )
    db.execute(
        "INSERT OR REPLACE INTO posted_items (id, echo_id, item_id, item_title, item_url, status, error_message, posted_at)"
        " VALUES (1, 1, 'err-1', 'Broken Post', 'https://example.com/err', 'failed', 'Upstream 502 timeout', CURRENT_TIMESTAMP)"
    )

cookies_out = {
    "admin": security.sign_session(201, "admin@example.com", 0),
    "trialing": security.sign_session(202, "trialing@example.com", 0),
    "expired": security.sign_session(203, "expired@example.com", 0),
}
Path(cfg["cookies_path"]).write_text(json.dumps(cookies_out), encoding="utf-8")
uvicorn.run(app_mod.app, host="127.0.0.1", port=cfg["port"], log_level="error")
"""


@pytest.fixture(scope="module")
def wave1_browser(tmp_path_factory):
    chrome_bin = _find_chromium()
    if not chrome_bin:
        pytest.skip("Headless Chromium binary not available")

    work = tmp_path_factory.mktemp("wave1_browser")
    db_dir = work / "db"
    db_dir.mkdir()
    port = _free_port()
    cfg_path = work / "server_cfg.json"
    cookies_path = work / "cookies.json"
    runner_path = work / "run_server.py"
    runner_path.write_text(_SERVER_SCRIPT, encoding="utf-8")
    cfg_path.write_text(
        json.dumps(
            {
                "db_path": str(db_dir / "feedecho.db"),
                "cookies_path": str(cookies_path),
                "port": port,
                "sess_val": "a" * 48,
                "state_val": "b" * 48,
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
        yield {"page": page, "base_url": base_url, "cookies": cookies}
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


class TestWave1BrowserRegressions:
    def test_echoes_390px_no_overflow_and_desktop_nowrap(self, wave1_browser):
        page = wave1_browser["page"]
        base = wave1_browser["base_url"]
        page.set_cookie(base, "feedecho_session", wave1_browser["cookies"]["admin"])

        page.goto(f"{base}/echoes", 390, 844, theme="dark")
        metrics = page.eval(
            """(() => {
                const details = document.querySelector('details.template-vars');
                if (details) details.open = true;
                const swOpen = document.documentElement.scrollWidth;
                editEcho(1);
                const swEdit = document.documentElement.scrollWidth;
                const editAlign = getComputedStyle(document.querySelector('.echo-edit-form')).textAlign;
                const tdAlign = getComputedStyle(document.querySelector('#echo-row-1 td[colspan]')).textAlign;
                return { swOpen, swEdit, editAlign, tdAlign };
            })()"""
        )
        assert metrics["swOpen"] <= 390, f"/echoes 390px overflow with template-vars open: {metrics}"
        assert metrics["swEdit"] <= 390, f"/echoes 390px overflow with editEcho open: {metrics}"
        assert metrics["editAlign"] == "left"
        assert metrics["tdAlign"] == "left"

        page.goto(f"{base}/echoes", 1440, 900, theme="dark")
        ws = page.eval("getComputedStyle(document.querySelector('.vars-table td:first-child')).whiteSpace")
        assert ws == "nowrap"

    def test_links_and_banners_contrast_light_and_dark(self, wave1_browser):
        page = wave1_browser["page"]
        base = wave1_browser["base_url"]

        for theme in ("dark", "light"):
            # Anonymous landing + login
            page.set_cookie(base, "feedecho_session", None)
            page.goto(f"{base}/", 1440, 900, theme=theme)
            page.eval(CONTRAST_HELPER_JS)
            landing_links = page.eval(
                """Array.from(document.querySelectorAll('.landing-trial-note a')).map(a => ({
                    href: a.getAttribute('href'),
                    ...window.__feContrast(a)
                }))"""
            )
            assert len(landing_links) == 2
            for item in landing_links:
                assert item["fgRgb"] != "rgb(0, 0, 238)", f"{theme} landing link stayed UA blue: {item}"
                assert item["ratio"] >= 4.5, f"{theme} landing link contrast < 4.5: {item}"

            page.goto(f"{base}/login", 390, 844, theme=theme)
            page.eval(CONTRAST_HELPER_JS)
            login_links = page.eval(
                """Array.from(document.querySelectorAll('.auth-card a')).map(a => window.__feContrast(a))"""
            )
            assert login_links
            for item in login_links:
                assert item["fgRgb"] != "rgb(0, 0, 238)"
                assert item["ratio"] >= 4.5, f"{theme} /login link contrast < 4.5: {item}"

            # Authed /howto + .empty-state a on /queue
            page.set_cookie(base, "feedecho_session", wave1_browser["cookies"]["admin"])
            page.goto(f"{base}/howto", 1440, 900, theme=theme)
            page.eval(CONTRAST_HELPER_JS)
            howto_link = page.eval("window.__feContrast(document.querySelector('main a'))")
            assert howto_link["fgRgb"] != "rgb(0, 0, 238)"
            assert howto_link["ratio"] >= 4.5, f"{theme} /howto link contrast < 4.5: {howto_link}"

            page.goto(f"{base}/queue", 1440, 900, theme=theme)
            page.eval(CONTRAST_HELPER_JS)
            empty_link = page.eval("window.__feContrast(document.querySelector('.empty-state a'))")
            assert empty_link["ratio"] >= 4.5, f"{theme} .empty-state a contrast < 4.5: {empty_link}"

            # Expired trial dashboard -> .flash-warn a and .trial-banner a
            page.set_cookie(base, "feedecho_session", wave1_browser["cookies"]["expired"])
            page.goto(f"{base}/", 390, 844, theme=theme)
            page.eval(CONTRAST_HELPER_JS)
            banners = page.eval(
                """({
                    flashWarn: window.__feContrast(document.querySelector('.flash-warn a')),
                    trialBanner: window.__feContrast(document.querySelector('.trial-banner a')),
                })"""
            )
            for name, res in banners.items():
                assert res["fgRgb"] != "rgb(0, 0, 238)", f"{theme} {name} stayed UA blue: {res}"
                assert res["ratio"] >= 4.5, f"{theme} {name} contrast < 4.5: {res}"

    def test_reader_compose_usage_contrast_and_reduced_motion(self, wave1_browser):
        page = wave1_browser["page"]
        base = wave1_browser["base_url"]
        page.set_cookie(base, "feedecho_session", wave1_browser["cookies"]["admin"])

        for theme in ("dark", "light"):
            page.goto(f"{base}/reader", 1440, 900, theme=theme)
            page.eval(CONTRAST_HELPER_JS)
            reader_res = page.eval(
                """(() => {
                    const dlg = document.getElementById('reader-compose');
                    dlg.showModal();
                    const counter = dlg.querySelector('.compose-dest-counter');
                    counter.textContent = '320/300';
                    counter.classList.add('is-over');
                    const results = document.getElementById('compose-results');
                    results.style.display = 'flex';
                    results.innerHTML = '<div class="compose-res-item is-success">Sent</div><div class="compose-res-item is-error">Failed</div>';
                    return {
                        feedError: window.__feContrast(document.querySelector('.reader-feed-error')),
                        counterOver: window.__feContrast(counter),
                        resSuccess: window.__feContrast(results.querySelector('.is-success')),
                        resError: window.__feContrast(results.querySelector('.is-error')),
                    };
                })()"""
            )
            for key, info in reader_res.items():
                assert info["ratio"] >= 4.5, f"{theme} {key} contrast < 4.5: {info}"

            for vp_w, vp_h in ((390, 844), (1440, 900)):
                page.goto(f"{base}/admin", vp_w, vp_h, theme=theme)
                page.eval(CONTRAST_HELPER_JS)
                usage_res = page.eval(
                    """(() => {
                        const details = document.querySelector('.usage-row details');
                        details.open = true;
                        return {
                            cellAlign: getComputedStyle(document.querySelector('td.usage-cell')).textAlign,
                            summary: window.__feContrast(document.querySelector('.usage-row .usage-summary')),
                            fail: window.__feContrast(document.querySelector('.usage-row .usage-fail')),
                            hint: window.__feContrast(document.querySelector('.usage-row .usage-detail .hint')),
                        };
                    })()"""
                )
                assert usage_res["cellAlign"] == "left"
                for key in ("summary", "fail", "hint"):
                    assert usage_res[key]["ratio"] >= 4.5, f"{theme} {vp_w}px usage {key} < 4.5: {usage_res[key]}"

        page.goto(f"{base}/reader", 1440, 900, theme="dark", reduce_motion=True)
        try:
            anim = page.eval(
                """(() => {
                    const el = document.querySelector('.reader-item-content');
                    document.querySelector('details.reader-item').open = true;
                    el.classList.add('is-loading');
                    const cs = getComputedStyle(el, '::after');
                    return { duration: parseFloat(cs.animationDuration), count: cs.animationIterationCount };
                })()"""
            )
            assert anim["duration"] < 0.001, f"reader-shimmer not suppressed under reduced-motion: {anim}"
            assert anim["count"] == "1"
        finally:
            page.set_media(theme="dark", reduce_motion=False)

    def test_inline_form_button_hierarchy_and_reader_btn_primary(self, wave1_browser):
        page = wave1_browser["page"]
        base = wave1_browser["base_url"]

        # Feeds: primary Add Feed vs helper Find feed URL (.btn-sm)
        page.set_cookie(base, "feedecho_session", wave1_browser["cookies"]["admin"])
        page.goto(f"{base}/feeds", 1440, 900, theme="dark")
        feeds_btns = page.eval(
            """(() => {
                const addBtn = document.querySelector('.add-feed-form button[type="submit"]');
                const helperBtn = document.querySelector('.add-feed-form button.btn-sm');
                return {
                    addBg: getComputedStyle(addBtn).backgroundColor,
                    helperBg: getComputedStyle(helperBtn).backgroundColor,
                };
            })()"""
        )
        assert feeds_btns["addBg"] != feeds_btns["helperBg"], f"Add Feed flattened to .btn-sm: {feeds_btns}"

        # Trialing settings: Manage subscription (.btn-sm) vs Start my subscription now (primary)
        page.set_cookie(base, "feedecho_session", wave1_browser["cookies"]["trialing"])
        page.goto(f"{base}/settings", 1440, 900, theme="dark")
        billing_btns = page.eval(
            """(() => {
                const portalBtn = document.querySelector('form[action="/api/billing/portal"] button');
                const upgradeBtn = document.querySelector('form[action="/api/billing/upgrade-now"] button');
                return {
                    portalBg: getComputedStyle(portalBtn).backgroundColor,
                    upgradeBg: getComputedStyle(upgradeBtn).backgroundColor,
                    portalHasBtnSm: portalBtn.classList.contains('btn-sm'),
                };
            })()"""
        )
        assert billing_btns["portalHasBtnSm"] is True
        assert billing_btns["portalBg"] == feeds_btns["helperBg"]
        assert billing_btns["upgradeBg"] == feeds_btns["addBg"]

        # Paid settings: Manage subscription stays primary <button type="submit">
        page.set_cookie(base, "feedecho_session", wave1_browser["cookies"]["admin"])
        page.goto(f"{base}/settings", 1440, 900, theme="dark")
        paid_portal_bg = page.eval(
            "getComputedStyle(document.querySelector('form[action=\"/api/billing/portal\"] button')).backgroundColor"
        )
        assert paid_portal_bg == feeds_btns["addBg"]

        # Reader <dialog> modals: #compose-submit-btn, #mute-submit-btn, #save-search-submit-btn
        page.goto(f"{base}/reader", 1440, 900, theme="dark")
        modal_btns = page.eval(
            """(() => {
                const res = {};
                for (const [dlgId, submitId] of [
                    ['reader-compose', 'compose-submit-btn'],
                    ['reader-mute-dialog', 'mute-submit-btn'],
                    ['reader-save-search-dialog', 'save-search-submit-btn'],
                ]) {
                    const dlg = document.getElementById(dlgId);
                    dlg.showModal();
                    const primary = document.getElementById(submitId);
                    const secondary = dlg.querySelector('.compose-actions button:not(.btn-primary)');
                    res[submitId] = {
                        primaryBg: getComputedStyle(primary).backgroundColor,
                        secondaryBg: getComputedStyle(secondary).backgroundColor,
                    };
                    dlg.close();
                }
                return res;
            })()"""
        )
        for submit_id, pair in modal_btns.items():
            assert pair["primaryBg"] != pair["secondaryBg"], f"{submit_id} lacks primary fill: {pair}"
            assert pair["primaryBg"] == feeds_btns["addBg"]

    def test_desktop_navbar_focus_visible(self, wave1_browser):
        page = wave1_browser["page"]
        base = wave1_browser["base_url"]
        page.set_cookie(base, "feedecho_session", wave1_browser["cookies"]["admin"])
        page.goto(f"{base}/feeds", 1440, 900, theme="dark")

        seen = {}
        for _ in range(18):
            page.press_tab()
            info = page.eval(
                """(() => {
                    const el = document.activeElement;
                    if (!el) return null;
                    const cs = getComputedStyle(el);
                    return {
                        cls: el.className || '',
                        tag: el.tagName,
                        matchesFocusVisible: el.matches(':focus-visible'),
                        outlineStyle: cs.outlineStyle,
                        outlineWidth: cs.outlineWidth,
                    };
                })()"""
            )
            if not info:
                continue
            cls = info["cls"]
            if "nav-brand" in cls:
                seen["nav-brand"] = info
            elif info["tag"] == "A" and page.eval("!!document.activeElement.closest('.nav-links')"):
                seen.setdefault("nav-links-a", info)
            elif "nav-logout" in cls:
                seen["nav-logout"] = info
                break

        for key in ("nav-brand", "nav-links-a", "nav-logout"):
            assert key in seen, f"Tab navigation did not reach {key}: {seen}"
            assert seen[key]["matchesFocusVisible"] is True
            assert seen[key]["outlineStyle"] == "solid", f"{key} missing solid focus outline: {seen[key]}"
            assert seen[key]["outlineWidth"] == "2px", f"{key} outlineWidth != 2px: {seen[key]}"

    def test_mobile_inline_edit_alignment(self, wave1_browser):
        page = wave1_browser["page"]
        base = wave1_browser["base_url"]
        page.set_cookie(base, "feedecho_session", wave1_browser["cookies"]["admin"])

        page.goto(f"{base}/feeds", 390, 844, theme="dark")
        feed_align = page.eval(
            """(() => {
                editFeed(1);
                return {
                    form: getComputedStyle(document.querySelector('.feed-edit-form')).textAlign,
                    td: getComputedStyle(document.querySelector('#feed-row-1 td[colspan]')).textAlign,
                };
            })()"""
        )
        assert feed_align == {"form": "left", "td": "left"}

    def test_touch_targets_44x44(self, wave1_browser):
        page = wave1_browser["page"]
        base = wave1_browser["base_url"]
        page.set_cookie(base, "feedecho_session", wave1_browser["cookies"]["admin"])

        # Desktop 1440x900: .reader-shortcuts-btn visible and >=44x44
        page.goto(f"{base}/reader", 1440, 900, theme="dark")
        desktop_shortcuts = page.eval(
            """(() => {
                const btn = document.querySelector('.reader-shortcuts-btn');
                const r = btn.getBoundingClientRect();
                return { w: r.width, h: r.height, display: getComputedStyle(btn).display };
            })()"""
        )
        assert desktop_shortcuts["display"] != "none"
        assert desktop_shortcuts["w"] >= 44 and desktop_shortcuts["h"] >= 44, desktop_shortcuts

        # Mobile 390x844 Reader: drawer mark-read, feed-link, star, compose-dest-label, shortcuts hidden
        page.goto(f"{base}/reader", 390, 844, theme="dark")
        reader_mobile = page.eval(
            """(() => {
                readerToggleFeeds();
                document.querySelector('details.reader-item').open = true;
                const dlg = document.getElementById('reader-compose');
                dlg.showModal();
                const rect = (sel) => {
                    const el = document.querySelector(sel);
                    const r = el.getBoundingClientRect();
                    return { w: r.width, h: r.height };
                };
                return {
                    shortcutsDisplay: getComputedStyle(document.querySelector('.reader-shortcuts-btn')).display,
                    folderMarkRead: rect('.reader-folder-mark-read'),
                    feedLink: rect('.reader-feed-link'),
                    star: rect('.action-btn--star'),
                    composeDest: rect('.compose-dest-label'),
                    toolbarBtnSm: rect('.reader-feeds-toggle'),
                };
            })()"""
        )
        assert reader_mobile["shortcutsDisplay"] == "none", "shortcuts btn must stay hidden <=720px"
        for name in ("folderMarkRead", "feedLink", "star", "composeDest", "toolbarBtnSm"):
            box = reader_mobile[name]
            assert box["w"] >= 44 and box["h"] >= 44, f"{name} < 44x44px: {box}"

        # Mobile 390x844 Feeds: .btn-sm buttons and Export OPML <a class="btn-sm">
        page.goto(f"{base}/feeds", 390, 844, theme="dark")
        feeds_mobile = page.eval(
            """(() => {
                for (const d of document.querySelectorAll('details.account-section')) d.open = true;
                const opmlLink = document.querySelector('.opml-actions > a.btn-sm');
                const opmlWrap = document.querySelector('.opml-actions');
                const editBtn = document.querySelector('.action-cell button.btn-sm');
                const rLink = opmlLink.getBoundingClientRect();
                const rWrap = opmlWrap.getBoundingClientRect();
                const rEdit = editBtn.getBoundingClientRect();
                return {
                    opmlW: rLink.width, opmlH: rLink.height, wrapW: rWrap.width,
                    editW: rEdit.width, editH: rEdit.height,
                };
            })()"""
        )
        assert feeds_mobile["opmlW"] >= 44 and feeds_mobile["opmlH"] >= 44, feeds_mobile
        assert feeds_mobile["opmlW"] < feeds_mobile["wrapW"] - 20, f"Export OPML stretched full-width: {feeds_mobile}"
        assert feeds_mobile["editW"] >= 44 and feeds_mobile["editH"] >= 44, feeds_mobile

        # Settings: Download backup <a class="btn-sm">
        page.goto(f"{base}/settings", 390, 844, theme="dark")
        backup_box = page.eval(
            """(() => {
                const r = document.querySelector('a.btn-sm[href="/api/export"]').getBoundingClientRect();
                return { w: r.width, h: r.height };
            })()"""
        )
        assert backup_box["w"] >= 44 and backup_box["h"] >= 44, backup_box

        # Error page (403 /admin as non-admin): .error-page a
        page.set_cookie(base, "feedecho_session", wave1_browser["cookies"]["trialing"])
        page.goto(f"{base}/admin", 390, 844, theme="dark")
        err_box = page.eval(
            """(() => {
                const r = document.querySelector('.error-page a').getBoundingClientRect();
                return { w: r.width, h: r.height };
            })()"""
        )
        assert err_box["w"] >= 44 and err_box["h"] >= 44, err_box




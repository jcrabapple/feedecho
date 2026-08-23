# FeedEcho

Self-hosted RSS feed cross-poster. Route items from RSS, Atom, and JSON feeds to Mastodon, Bluesky, and email using configurable templates.

Built as a replacement for [Echofeed](https://rknight.me/blog/shutting-down-echofeed/), which shut down in August 2026.

## Features

- **RSS/Atom/JSON feed support** via feedparser
- **Mastodon OAuth** — connect accounts with one click, no manual token creation
- **Bluesky support** — connect accounts with an App Password; posts get auto-detected link facets, 300-grapheme truncation, and image embeds with alt text
- **Template engine** — sandboxed Jinja2 templates with conditionals, filters, and a live Preview button: `{{ title }}`, `{{ link }}`, `{{ summary }}`, `{{ content }}`, `{{ author }}`, `{{ date }}`, `{{ date_iso }}`, `{{ date_short }}`, `{{ tags }}`, `{{ hashtags }}`, `{{ image_url }}`, `{{ feed_name }}`, and the full `{{ item }}` dict
- **Multiple accounts** — post to multiple Mastodon instances and Bluesky accounts
- **Per-feed poll intervals** — each feed checked on its own schedule
- **Post history** with success/failure tracking and error messages
- **Visibility settings** — public, unlisted, private, direct (Mastodon)
- **Drip mode** — cap an echo at N posts per hour; bursts queue up and release as the sliding window allows instead of flooding your timeline
- **Content warnings** — per-echo CW text applied as Mastodon spoiler text
- **Image attachments** — automatically upload the feed item's first image (Mastodon and Bluesky)
- **AI alt text** — optionally generate image descriptions via an OpenAI-compatible vision API
- **Digest mode** — batch email deliveries into hourly digests instead of one email per item
- **Mobile-responsive** — tables convert to cards, forms stack, 44px touch targets
- **Idempotent posting** — failed posts are retried, duplicates are prevented
- **Auto-initialization** — feeds set their baseline on first fetch, no manual init needed
- **Email destination** — echo to email via SMTP in addition to Mastodon and Bluesky

## Tech Stack

- **Backend**: Python + FastAPI
- **Database**: SQLite (WAL mode)
- **Frontend**: Jinja2 server-rendered templates + vanilla JS
- **Feed parsing**: feedparser (RSS/Atom) + native JSON Feed parser
- **Scheduler**: APScheduler (background feed checker)
- **HTTP client**: httpx

## Quick Start

### Docker (recommended)

Pre-built multi-arch images (amd64 + arm64) are published to GHCR on every release — no local build needed:

```bash
mkdir feedecho && cd feedecho

cat > .env <<'EOF'
FEEDCHO_AUTH_TOKEN=change-me-to-a-long-random-string
FEEDCHO_CALLBACK_URL=http://localhost:8453/oauth/callback
EOF

curl -O https://raw.githubusercontent.com/jcrabapple/feedecho/master/docker-compose.yml
# Then edit docker-compose.yml: comment out `build: .` and uncomment the `image:` line
docker compose up -d
```

Or clone the repo and build locally:

```bash
git clone https://github.com/jcrabapple/feedecho.git
cd feedecho

# Set your access token (required) and public URL (for Mastodon OAuth)
cat > .env <<'EOF'
FEEDCHO_AUTH_TOKEN=change-me-to-a-long-random-string
FEEDCHO_CALLBACK_URL=http://localhost:8453/oauth/callback
EOF

docker compose up -d
```

Open `http://localhost:8453` and log in with your `FEEDCHO_AUTH_TOKEN`.

Data lives in the `feedecho-data` volume (`/app/data` in the container) — your feeds, accounts, and history survive `docker compose up -d --build` rebuilds.

Plain Docker without compose:

```bash
docker build -t feedecho .
docker run -d --name feedecho \
  -p 8453:8453 \
  -v feedecho-data:/app/data \
  -e FEEDCHO_AUTH_TOKEN=change-me-to-a-long-random-string \
  -e FEEDCHO_CALLBACK_URL=http://localhost:8453/oauth/callback \
  feedecho
```

### Environment variables

| Variable | Required | Purpose |
|----------|----------|---------|
| `FEEDCHO_AUTH_TOKEN` | yes (for any real deployment) | Shared-secret login for the web UI. If unset, auth is **disabled** — only safe on localhost. |
| `FEEDCHO_CALLBACK_URL` | for Mastodon OAuth | Public callback URL, e.g. `https://feedecho.example.com/oauth/callback`. Must match the URL reachable by your browser. |
| `FEEDCHO_DB_PATH` | no | SQLite path (default `/app/data/feedecho.db` in Docker, `./feedecho.db` otherwise) |
| `FEEDCHO_STATE_SECRET` | no | OAuth state signing secret (defaults to `FEEDCHO_AUTH_TOKEN`) |

Behind a reverse proxy (nginx, Caddy, Traefik), point the proxy at port `8453` and set `FEEDCHO_CALLBACK_URL` to the public HTTPS URL.

### From source

```bash
git clone https://github.com/jcrabapple/feedecho.git
cd feedecho
python -m venv .venv
source .venv/bin/activate
pip install fastapi "uvicorn[standard]" jinja2 python-multipart feedparser httpx apscheduler
FEEDCHO_AUTH_TOKEN=your-token python -m uvicorn app:app --host 0.0.0.0 --port 8453
```

### NixOS

FeedEcho ships a Nix flake and a NixOS module. See [`nix/README.md`](nix/README.md) for full instructions.

```nix
{
  inputs.feedecho.url = "github:jcrabapple/feedecho";
  outputs = { self, nixpkgs, feedecho, ... }: {
    nixosConfigurations.myhost = nixpkgs.lib.nixosSystem {
      modules = [
        feedecho.nixosModules.default
        {
          services.feedecho = {
            enable = true;
            authTokenFile = "/run/secrets/feedecho-token";
            callbackUrl = "https://feedecho.example.com/oauth/callback";
          };
        }
      ];
    };
  };
}
```

## Usage

1. **Add a Mastodon account** — Go to `/accounts`, enter your instance URL, click "Connect Account". OAuth handles the rest.
2. **Add a Bluesky account** — Create an App Password in Bluesky (Settings → Privacy & Security → App Passwords), then enter your handle and the app password on `/accounts`. FeedEcho verifies the credentials, resolves your PDS, and caches a session.
3. **Add a feed** — Go to `/feeds`, paste an RSS/Atom/JSON feed URL.
4. **Create an echo** — Go to `/echoes`, select a feed + destination, write a template like `{{ title }} {{ link }}`.
5. **Watch it run** — The scheduler checks feeds every 2 minutes and posts new items.

### Bluesky details

- Accounts connect via **App Passwords**, which are scoped to creating posts (and other app activity) and can be revoked individually without changing your main password.
- Sessions are cached per account (access + refresh JWTs in SQLite) and refreshed automatically; expired tokens trigger a transparent re-login and one retry.
- Posts are truncated to **300 graphemes** (Unicode-aware) and URLs in the text get proper link facets, so links are clickable everywhere.
- Image attachments upload through the PDS blob API with an `app.bsky.embed.images` embed; alt text uses your AI vision config when enabled. Images are capped at 1 MB and jpeg/png/webp/gif (Bluesky's limits).
- Content warnings and visibility settings are Mastodon-only and are ignored for Bluesky posts.

## Template Variables

Templates are sandboxed Jinja2, so they support conditionals, filters, and
direct access to the item dict. The variables table below lists the flat
variables; the `{{ item }}` dict exposes every parsed field (`{{ item.title }}`,
`{{ item['link'] }}`, `{{ item['tags'] | first }}`). Indexing into an empty
or missing list raises at render time — use `| first` or `| default(...)`.

| Variable | Description |
|----------|-------------|
| `{{ title }}` | Post title |
| `{{ link }}` | Post URL |
| `{{ summary }}` | Post summary/excerpt |
| `{{ content }}` | Full post content (HTML stripped to plain text) |
| `{{ author }}` | Author name |
| `{{ date }}` | Publication date (raw) |
| `{{ date_iso }}` | ISO 8601 date (2024-01-15T09:30:00) |
| `{{ date_short }}` | Short date (2024-01-15) |
| `{{ tags }}` | Raw tag list |
| `{{ hashtags }}` | Feed tags as #hashtags |
| `{{ image_url }}` | First image URL from the item |
| `{{ feed_name }}` | Name of the source feed |

Legacy spellings `{{ date:iso }}` and `{{ date:short }}` keep working.

Examples:

```
{% if summary %}{{ title }} - {{ summary | truncate(90) }}{% else %}{{ title }} {{ link }}{% endif %}
{{ title }} by {{ author | default('unknown', true) }} {{ link }}
{{ feed_name }}: {{ title }} {{ link }}
```

The echo form has a **Preview** button that renders the current template
against the feed's three most recent items, and template syntax errors are
rejected at save time. Rendered posts longer than the platform limit are
truncated before posting (500 chars Mastodon, 300 graphemes Bluesky); use
`| truncate(N)` to control where the cut happens.

## Code Breakdown

FeedEcho is ~1,600 lines of Python across 8 modules. No framework magic, no ORMs, no build step. Here's what each piece does:

### `app.py` (613 lines) — Web server and routes

The FastAPI application. Defines every HTTP route: dashboard, feed CRUD, account management, echo CRUD, post history, settings, and the OAuth callback endpoints. Renders Jinja2 templates server-side. Also starts/stops the background scheduler on app startup/shutdown. This is the only module that talks to the user's browser.

### `database.py` (143 lines) — SQLite layer

Creates and manages 8 tables: `accounts` (Mastodon connections), `feeds` (RSS sources), `echoes` (feed-to-destination mappings), `email_accounts`, `bluesky_accounts` (Bluesky handles, app passwords, DID/PDS, and cached session JWTs), `settings` (key-value config like SMTP), `posted_items` (post history with status tracking), and `oauth_apps` (cached OAuth client credentials per instance). Uses SQLite WAL mode for concurrent read/write. Includes lightweight migrations (column additions, schema resets) so existing databases upgrade in place. The unique index on `posted_items(echo_id, item_id)` enforces the pending-row dedup pattern.

### `feed_parser.py` (212 lines) — Feed fetching and normalization

Fetches feed URLs via httpx with a 10 MB size cap (prevents OOM from hostile feeds). Parses RSS/Atom via feedparser and JSON Feed natively. Normalizes all feed formats into a common item shape: `id`, `title`, `link`, `summary`, `content`, `author`, `date`, `tags`. Strips HTML to plain text (Mastodon statuses are plain text). Synthesizes stable item IDs from content hashes when feeds lack GUIDs. The `get_new_items()` function implements cursor-based new-item detection: on first run it sets a baseline (no backlog posting), and if the cursor scrolled off the feed, it posts only the newest item to avoid spam.

### `scheduler.py` (302 lines) — Background feed checker

The core dispatch engine. Runs on APScheduler (every 2 minutes). For each due feed: fetch new items, find enabled echoes, render templates, dispatch to Mastodon or email. Uses a **pending-row pattern** for idempotent posting: each (echo, item) pair is claimed via `INSERT OR IGNORE` with `status='pending'` before dispatch, then `UPDATE`d to `success` or `failed` after. The unique index prevents duplicate claims. The cursor only advances past items where all echoes succeeded, so failed posts are retried on the next poll. All network I/O happens outside DB transactions to avoid lock contention.

### `mastodon.py` (67 lines) — Mastodon API client

Thin httpx wrapper around three Mastodon REST endpoints: `POST /api/v1/statuses` (post), `GET /api/v1/accounts/verify_credentials` (validate token), and the connection test helper. No state, no caching, no surprises.

### `bluesky.py` — Bluesky (AT Protocol) client

Handles the Bluesky side: handle normalization, DID/PDS discovery (`resolveHandle` + PLC directory, with SSRF validation on resolved endpoints), app-password sessions (`createSession` / `refreshSession`), blob uploads, and `createRecord` posts. Includes URL facet building with UTF-8 byte offsets, Unicode grapheme-aware truncation for the 300-grapheme limit, and `app.bsky.embed.images` embeds with alt text.

### `oauth.py` (110 lines) — Mastodon OAuth 2.0 flow

Implements the full OAuth dance: register an app on the target instance (`POST /api/v1/apps`), build the authorize URL with a CSRF state token, exchange the callback code for an access token (`POST /oauth/token`). Caches OAuth app credentials per instance in the `oauth_apps` table so re-registration isn't needed. The state parameter carries a random token plus the instance URL so the callback knows which instance to exchange with.

### `template_engine.py` (76 lines) — Template rendering

Regex-based variable substitution. Replaces `{{ variable }}` placeholders with feed item data. Supports 9 variables including two date format variants. No eval, no code execution — pure string replacement. Tags are sanitized to alphanumeric for hashtag safety.

### `email_sender.py` (99 lines) — SMTP email dispatch

Sends rendered template content as plain-text email. Reads SMTP config (host, port, username, password, TLS mode) from the `settings` table. Supports both implicit TLS (port 465) and STARTTLS (port 587). Includes a connection test helper.

### `templates/` — Jinja2 HTML templates

9 templates: `base.html` (layout + nav), `dashboard.html` (overview stats), `feeds.html`, `accounts.html`, `echoes.html`, `history.html` (post log), `settings.html` (SMTP config), `login.html` (shared-secret auth), `404.html`. All use Jinja2 autoescaping.

### `static/` — CSS and JavaScript

`style.css` (mobile-responsive, table-to-card at 640px breakpoint) and `app.js` (inline echo editing, account test buttons, feed preview). Vanilla JS, no frameworks, no build step.

### `tests/` — 197 pytest tests

Test modules covering the database layer, feed parser (item detection, HTML stripping, truncation, date parsing), template engine (variable substitution, date formatting, hashtag generation), security features (SSRF protection, OAuth state signing), content warnings and image attachments, digest delivery, retry/notification logic, and Bluesky integration (handle normalization, grapheme truncation, facets, session caching, dispatch, and account API routes).

## Security

FeedEcho handles OAuth tokens, Bluesky app passwords, and posts to your connected accounts. Here's what it does and doesn't do:

### SSRF protection

Feed URLs are validated before fetching. The SSRF filter blocks:
- Non-http(s) schemes (`file://`, `gopher://`, etc.)
- Direct IP addresses in private ranges (10.x, 172.16-31.x, 192.168.x, 127.x, 169.254.x, ::1, fc00::, fe80::)
- Hostnames that resolve to private/internal IPs (DNS resolution is checked before the request is made)

This prevents a user from pointing FeedEcho at cloud metadata endpoints (`169.254.169.254`), internal services, or localhost.

### Web UI authentication

FeedEcho supports optional shared-secret authentication via the `FEEDCHO_AUTH_TOKEN` environment variable:

- **If set**: all requests must include the token as either a cookie (set by the login page at `/login`) or an `X-Auth-Token` header (for API/programmatic access). Unauthenticated browser requests are redirected to `/login`; API requests get 401.
- **If unset**: auth is disabled (original behavior). The app is open to anyone who can reach the port.

The OAuth callback endpoints (`/oauth/connect`, `/oauth/callback`) are exempt from auth so Mastodon's redirect flow works without a cookie. The HMAC-signed state token provides CSRF protection on those endpoints.

### OAuth flow

- The OAuth state parameter is **HMAC-signed** (`hmac.compare_digest`, SHA-256). Format: `<nonce>|<instance>|<signature>`. The signature covers the nonce and instance, preventing CSRF and tampering with the instance field. A forged state token without the secret is rejected.
- The callback URL is configurable via the `FEEDCHO_CALLBACK_URL` environment variable. If unset, it defaults to `https://feedecho.example.com/oauth/callback`. Self-hosters should set this to their public URL.

### Secrets handling

- **Mastodon OAuth tokens** are stored in the SQLite database (`accounts.access_token`). The database file is local to the server. There is no encryption at rest — if an attacker gains filesystem access, they can read the tokens.
- **Bluesky app passwords** are stored in the SQLite database (`bluesky_accounts.app_password`), along with cached session JWTs. App passwords can only create posts (and other app-level actions) and are revoked individually in Bluesky settings — they never expose the account's main password. Same at-rest caveat as Mastodon tokens.
- **SMTP passwords** are stored in the `settings` table in plaintext. They are **masked** (`********`) when sent to the browser on the settings and accounts pages. The save endpoint skips password updates when the mask placeholder is submitted, so the existing password is preserved.
- **OAuth client secrets** (per-instance app credentials) are cached in the `oauth_apps` table in plaintext.
- FeedEcho **does not** log tokens, passwords, or secrets to the application log. Log messages contain echo IDs, feed names, and error messages only.
- The `FEEDCHO_AUTH_TOKEN` env var doubles as the HMAC signing key for OAuth state tokens if set, so a single secret secures both layers.

### Input handling

- Feed content from external RSS/Atom/JSON feeds is treated as untrusted. HTML is stripped to plain text before posting to Mastodon. Feed item titles and URLs are never rendered as HTML in the UI without Jinja2 autoescaping.
- Template variables are substituted via regex — there is no `eval()` or code execution path. A malformed template produces empty or garbled output, not a security hole.
- Feed fetches are capped at 10 MB to prevent memory exhaustion from hostile feeds.
- The inline echo editor in `app.js` stores original row HTML in an in-memory Map rather than serializing it into a DOM attribute, avoiding an XSS vector that was present in an earlier version.

### Network

- All outbound HTTP uses httpx with a 30-second timeout. FeedEcho makes requests to: the feed URL (user-provided), the Mastodon instance API (user-provided), and the SMTP server (admin-configured). No telemetry, no phone-home, no analytics.
- Even without `FEEDCHO_AUTH_TOKEN`, FeedEcho is designed to run behind a reverse proxy or tunnel (Cloudflare Tunnel, nginx, etc.) with access control at the network layer. The built-in auth is a lightweight fallback for when a reverse proxy isn't available.

### Configuration

| Environment variable | Purpose | Default |
|---------------------|---------|---------|
| `FEEDCHO_AUTH_TOKEN` | Shared-secret auth token (enables login page + API auth, also signs OAuth state) | Unset (auth disabled) |
| `FEEDCHO_CALLBACK_URL` | Public URL for OAuth callback | `https://feedecho.example.com/oauth/callback` |
| `FEEDCHO_DB_PATH` | Path to SQLite database | `./feedecho.db` |

### What FeedEcho does NOT do

- Does not encrypt secrets at rest (tokens and passwords are plaintext in SQLite)
- Does not rate-limit its own feed polling (relies on APScheduler intervals)
- Does not validate SSL certificates beyond httpx defaults
- Does not sandbox feed parsing (feedparser runs in-process)

If any of these are a concern for your deployment, wrap FeedEcho behind an authenticated reverse proxy and restrict filesystem access to the database file.

## Deployment

### systemd user service

```ini
[Unit]
Description=FeedEcho
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=%h/feedecho
ExecStart=%h/feedecho/.venv/bin/python -m uvicorn app:app --host 0.0.0.0 --port 8453
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

### Cloudflare Tunnel

For public access with HTTPS, use a Cloudflare Tunnel:

```bash
cloudflared tunnel create feedecho
cloudflared tunnel route dns <TUNNEL_ID> feedecho.yourdomain.com

cat > ~/.cloudflared/feedecho.yml << 'EOF'
tunnel: feedecho
credentials-file: ~/.cloudflared/<TUNNEL_ID>.json

ingress:
  - hostname: feedecho.yourdomain.com
    service: http://127.0.0.1:8453
  - service: http_status:404
EOF
```

If using OAuth, set `FEEDCHO_CALLBACK_URL` to your public URL:

```bash
export FEEDCHO_CALLBACK_URL="https://feedecho.yourdomain.com/oauth/callback"
export FEEDCHO_AUTH_TOKEN="your-secret-token"  # optional: enable web UI auth
```

### Hosted multi-tenant stack (Postgres + Caddy)

The same codebase runs a hosted, multi-tenant deployment
(`FEEDCHO_MODE=multi`): per-user accounts with sessions, tenant-scoped
feeds/echoes/settings, and Postgres storage. One compose file brings the
whole stack up:

```bash
cp .env.example.multi .env       # fill in real secrets; chmod 600
docker compose -f docker-compose.multi.yml up -d
```

- `postgres:17-alpine` — database, exposed only on the compose network
- `feedecho` — the app image (`ghcr.io/jcrabapple/feedecho`), multi mode
- `caddy:2` — automatic HTTPS and reverse proxy

Required environment (see `.env.example.multi`): `FEEDCHO_MODE=multi`,
`FEEDCHO_BASE_URL` (public base URL, used for OAuth callbacks),
`FEEDCHO_SESSION_SECRET` (>= 32 chars), and the Postgres credentials.
The app refuses to start if a multi-mode deployment is missing a
database URL or session secret.

CI exercises both modes: the single-mode suite on SQLite, the multi-mode
suite (`-m multi`), and a live Postgres job (`-m pg`) against
`postgres:17-alpine`.

## Testing

```bash
source .venv/bin/activate
python -m pytest tests/ -v
```

## Project Structure

```
feedecho/
├── app.py              # FastAPI app — routes, auth middleware, OAuth callbacks
├── database.py         # SQLite layer (7 tables)
├── feed_parser.py     # RSS/Atom/JSON feed fetching + SSRF protection
├── mastodon.py        # Mastodon API client
├── oauth.py           # Mastodon OAuth 2.0 flow (HMAC-signed state)
├── scheduler.py       # APScheduler background feed checker
├── template_engine.py # Variable substitution
├── email_sender.py    # SMTP email dispatch
├── templates/         # Jinja2 HTML templates (9)
├── static/            # CSS + JS
└── tests/             # 62 tests (pytest)
```

## License

MIT

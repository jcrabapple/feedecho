<div align="center">
  <img src="static/img/logo.svg" width="96" alt="FeedEcho logo: RSS arcs doubling as echo waves">
  <h1>FeedEcho</h1>
  <p><em>Your feeds, echoed everywhere.</em></p>
</div>

Self-hosted RSS feed cross-poster. Route items from RSS, Atom, and JSON feeds to Mastodon, Bluesky, micro.blog, and email using configurable templates.

Inspired by, and built as a replacement for [Echofeed](https://rknight.me/blog/shutting-down-echofeed/), which began shutting down in August 2026.

## Features

- **RSS/Atom/JSON feed support** via feedparser
- **Mastodon OAuth** — connect accounts with one click, no manual token creation
- **Bluesky support** — connect accounts with an App Password; posts get auto-detected link facets, 300-grapheme truncation, and image embeds with alt text
- **micro.blog support** — connect with a Micropub app token; FeedEcho discovers every blog the token can post to and posts with the item's image attached
- **Template engine** — sandboxed Jinja2 templates with conditionals, filters, and a live Preview button: `{{ title }}`, `{{ link }}`, `{{ summary }}`, `{{ content }}`, `{{ author }}`, `{{ date }}`, `{{ date_iso }}`, `{{ date_short }}`, `{{ tags }}`, `{{ hashtags }}`, `{{ image_url }}`, `{{ feed_name }}`, and the full `{{ item }}` dict
- **Multiple accounts** — post to multiple Mastodon instances, Bluesky accounts, and micro.blog blogs
- **Per-feed poll intervals** — each feed checked on its own schedule
- **Post history** with success/failure tracking and error messages
- **Visibility settings** — public, unlisted, private, direct (Mastodon)
- **Drip mode** — cap an echo at N posts per hour; bursts queue up and release as the sliding window allows instead of flooding your timeline
- **Content warnings** — per-echo CW text applied as Mastodon spoiler text
- **Image attachments** — automatically upload the feed item's first image (Mastodon and Bluesky upload as media; micro.blog fetches it by URL)
- **AI alt text** — optionally generate image descriptions via an OpenAI-compatible vision API
- **Digest mode** — batch email deliveries into hourly digests instead of one email per item
- **Mobile-responsive** — tables convert to cards, forms stack, 44px touch targets
- **Idempotent posting** — failed posts are retried, duplicates are prevented
- **Auto-initialization** — feeds set their baseline on first fetch, no manual init needed
- **Email destination** — echo to email via SMTP in addition to Mastodon, Bluesky, and micro.blog

## Tech Stack

- **Backend**: Python + FastAPI
- **Database**: SQLite (WAL mode) or Postgres
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
| `FEEDCHO_CALLBACK_URL` | for Mastodon OAuth | Public callback URL, e.g. `https://feedecho.example.com/oauth/callback`. Must match the URL reachable by your browser. Derived from `FEEDCHO_BASE_URL` when unset. |
| `FEEDCHO_BASE_URL` | no | Public base URL of your install. Used to derive the OAuth callback and the app website shown on posts. |
| `FEEDCHO_APP_WEBSITE` | no | Link behind the "FeedEcho" application name on Mastodon posts. Defaults to `FEEDCHO_BASE_URL`, then to the project repo. |
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
3. **Add a micro.blog blog** — Create an app token at [micro.blog/account/apps](https://micro.blog/account/apps), then paste it under Connect Micro.blog on `/accounts`. FeedEcho discovers every blog the token can post to and connects each one.
4. **Add a feed** — Go to `/feeds`, paste an RSS/Atom/JSON feed URL.
5. **Create an echo** — Go to `/echoes`, select a feed + destination, write a template like `{{ title }} {{ link }}`.
6. **Watch it run** — The scheduler checks feeds every 2 minutes and posts new items.

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
truncated before posting (500 chars Mastodon, 300 graphemes Bluesky;
micro.blog has no hard cap); use `| truncate(N)` to control where the cut
happens.

## Architecture

FeedEcho is ~8,000 lines of Python across a dozen small modules — no ORM, no build step. The shape:

- `app.py` — FastAPI routes, auth middleware, OAuth callbacks
- `database.py` — dual-dialect storage layer (SQLite WAL / Postgres) with idempotent migrations
- `feed_parser.py` — feed fetching with SSRF validation, size caps, and normalized item shapes
- `scheduler.py` — the dispatch engine: per-feed polling, atomic post claims, retries, drip/digest queues
- `mastodon.py` / `bluesky.py` / `microblog.py` — one client module per destination
- `oauth.py` — Mastodon OAuth 2.0 flow with signed state
- `plans.py` / `invites.py` — hosted-mode plan limits and registration gating (dormant in self-hosted mode)
- `template_engine.py` — sandboxed Jinja2 rendering
- `email_sender.py` — SMTP dispatch

Contributors: the tests are the best documentation — each module has a suite covering its behavior edge cases, and CI runs all of them plus a live Postgres job on every pull request.

## Security

FeedEcho handles OAuth tokens, Bluesky app passwords, and posts to your connected accounts. Here's what it does and doesn't do:

### SSRF protection

Feed URLs are validated before fetching, and every redirect hop is validated too. The SSRF filter blocks non-http(s) schemes, embedded credentials, and any address in private, loopback, link-local, or reserved ranges — including addresses that hostnames resolve to, with connections pinned to the validated address so DNS can't be flipped between check and fetch.

This prevents pointing FeedEcho at cloud metadata endpoints, internal services, or localhost.

### Web UI authentication

FeedEcho supports optional shared-secret authentication via the `FEEDCHO_AUTH_TOKEN` environment variable:

- **If set**: all requests must include the token as either a cookie (set by the login page at `/login`) or an `X-Auth-Token` header (for API/programmatic access). Unauthenticated browser requests are redirected to `/login`; API requests get 401.
- **If unset**: auth is disabled (original behavior). The app is open to anyone who can reach the port.

Only the endpoints that require unauthenticated access (OAuth callback, health check, static assets) are exempt from auth — everything else requires the session or token.

### OAuth flow

- The OAuth state parameter is **HMAC-signed and verified with a constant-time comparison**. The signature covers both the nonce and the instance, preventing CSRF and tampering with the instance field. A forged state token without the secret is rejected.
- The callback URL is configurable via the `FEEDCHO_CALLBACK_URL` environment variable. If unset, it is derived from `FEEDCHO_BASE_URL` (`<base>/oauth/callback`), and only falls back to `https://feedecho.example.com/oauth/callback` when neither is set. Self-hosters should set one of the two.
- Mastodon shows an application name on every post, linked to the `website` recorded when FeedEcho registered its OAuth app on your instance. That website is `FEEDCHO_APP_WEBSITE` if set, otherwise `FEEDCHO_BASE_URL`, otherwise the project repo. Both the website and the callback URL are stored alongside the cached client credentials in `oauth_apps`, and changing either re-registers the app on the next connect — Mastodon's API has no way to edit an existing registration, so a drifted callback URL would otherwise fail as a redirect mismatch. **Already-connected accounts keep the old link** — their access token is bound to the old app registration, so reconnect the account to update what appears on new posts.

### Secrets handling

- **Mastodon OAuth tokens, Bluesky app passwords and session JWTs, micro.blog app tokens, and OAuth client secrets** are stored in the local database, unencrypted at rest. If an attacker gains filesystem access to the server, they can read them. All of these credentials are scoped (app passwords and platform tokens can be revoked individually at the source platform without touching your main passwords).
- **SMTP passwords** are stored server-side and are **masked** in the web UI; saving the masked placeholder preserves the existing password.
- FeedEcho **does not** log tokens, passwords, or secrets to the application log. Log messages contain echo IDs, feed names, and error messages only.
- The `FEEDCHO_AUTH_TOKEN` env var doubles as the HMAC signing key for OAuth state tokens if set, so a single secret secures both layers.

### Input handling

- Feed content from external RSS/Atom/JSON feeds is treated as untrusted. HTML is stripped to plain text before posting to Mastodon. Feed item titles and URLs are never rendered as HTML in the UI without Jinja2 autoescaping.
- Templates render in a sandboxed Jinja2 environment — no `eval()`, no code execution path. A malformed template produces a validation error at save time, not a security hole.
- Feed fetches are capped at 10 MB to prevent memory exhaustion from hostile feeds.
- All rendered output is Jinja2-autoescaped; user-facing JS keeps untrusted markup out of the DOM.

### Network

- All outbound HTTP uses httpx with a 30-second timeout. FeedEcho makes requests to: the feed URL (user-provided), the Mastodon instance API (user-provided), micro.blog's Micropub endpoints (via your token), and the SMTP server (admin-configured). No telemetry, no phone-home, no analytics.
- Even without `FEEDCHO_AUTH_TOKEN`, FeedEcho is designed to run behind a reverse proxy or tunnel (Cloudflare Tunnel, nginx, etc.) with access control at the network layer. The built-in auth is a lightweight fallback for when a reverse proxy isn't available.

### Configuration

| Environment variable | Purpose | Default |
|---------------------|---------|---------|
| `FEEDCHO_AUTH_TOKEN` | Shared-secret auth token (enables login page + API auth, also signs OAuth state) | Unset (auth disabled) |
| `FEEDCHO_CALLBACK_URL` | Public URL for OAuth callback | `<FEEDCHO_BASE_URL>/oauth/callback`, else `https://feedecho.example.com/oauth/callback` |
| `FEEDCHO_APP_WEBSITE` | Website registered with the Mastodon OAuth app (the link on posts) | `FEEDCHO_BASE_URL`, else `https://github.com/jcrabapple/feedecho` |
| `FEEDCHO_DB_PATH` | Path to SQLite database | `./feedecho.db` |

### Operator hardening notes

For a public deployment, run FeedEcho behind an authenticated reverse proxy, restrict filesystem access to the server, keep the host patched, and back up the data directory. Secrets stored by the app are scoped platform credentials — revoke any of them at the source platform without affecting your main passwords.

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

## Hosted version

FeedEcho is also run as a hosted service with accounts, plans, and a
free trial at [feedecho.net](https://feedecho.net). The self-hosted,
single-tenant mode documented here is the complete cross-posting product;
hosted-specific machinery (accounts, billing, quotas) is not part of the
self-hosted distribution.

## Testing

```bash
source .venv/bin/activate
FEEDCHO_MODE=single python -m pytest tests/ -v
```

CI additionally runs a multi-mode suite and a live Postgres dialect suite on every push.

## License

MIT

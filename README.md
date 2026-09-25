<div align="center">
  <img src="static/img/logo.svg" width="96" alt="FeedEcho logo: RSS arcs doubling as echo waves">
  <h1>FeedEcho</h1>
  <p><em>Your feeds, echoed everywhere.</em></p>
</div>

Self-hosted RSS feed cross-poster. Route items from RSS, Atom, and JSON feeds to Mastodon, Bluesky, micro.blog, Matrix, Discord, Telegram, generic webhooks, and email using configurable templates.

Inspired by, and built as a replacement for [Echofeed](https://rknight.me/blog/shutting-down-echofeed/), which began shutting down in August 2026.

A hosted version with accounts, plans, and a 14-day free trial is live at [feedecho.net](https://feedecho.net). The self-hosted, single-tenant mode documented here is the complete cross-posting product.

## Features

- **RSS/Atom/JSON feed support** via feedparser
- **Mastodon OAuth** — connect accounts with one click, no manual token creation
- **Bluesky support** — connect accounts with an App Password; posts get auto-detected link and hashtag facets (links clickable, hashtags as real, searchable tags), 300-grapheme truncation, and image embeds with alt text
- **micro.blog support** — connect with a Micropub app token; FeedEcho discovers every blog the token can post to and posts with the item's image attached
- **Matrix support** — connect a room with an access token; posts go in as `m.room.message` events with clickable links, uploaded images, and homeserver-side de-duplication on retries (optionally with full-HTML formatted bodies via the render_html echo flag)
- **Discord support** — connect a channel with a webhook URL; posts land in the channel with an embed carrying the title, link, and image
- **Telegram support** — connect a bot token + chat; posts land in the chat (public channels get a t.me link in post history)
- **Generic webhooks** — POST items as JSON to any HTTP endpoint: Slack and Mattermost incoming webhooks, ntfy, Gotify, Zapier, n8n, push services like brrr, or anything you run yourself (optional custom JSON body template per endpoint)
- **Template engine** — sandboxed Jinja2 templates with conditionals, filters, and a live Preview button: `{{ title }}`, `{{ link }}`, `{{ content_link }}`, `{{ summary }}`, `{{ content }}`, `{{ content_html }}` (sanitized HTML passthrough for markup-aware destinations), `{{ author }}`, `{{ date }}`, `{{ date_iso }}`, `{{ date_short }}`, `{{ tags }}`, `{{ hashtags }}`, `{{ image_url }}`, `{{ feed_name }}`, and the full `{{ item }}` dict
- **Multiple accounts** — post to multiple Mastodon instances, Bluesky accounts, micro.blog blogs, Matrix rooms, Discord channels, Telegram chats, and webhook endpoints
- **Per-feed poll intervals** — each feed checked on its own schedule
- **Built-in feed reader** — read items in place instead of a third-party app: folders with OPML import/export, unread and starred tracking with CSV/JSON export of starred items, saved searches, a full-text view, and a compose desk that turns any item into a post without leaving FeedEcho
- **Post history** with success/failure tracking, error messages, and per-feed / per-destination filtering
- **Visibility settings** — public, unlisted, private, direct (Mastodon)
- **Drip mode** — cap an echo at N posts per hour; bursts queue up and release as the sliding window allows instead of flooding your timeline
- **Content warnings** — per-echo CW text applied as Mastodon spoiler text
- **Image attachments** — automatically attach the feed item's images: up to 4 per post on Mastodon and Bluesky, up to 4 inline on email, and the first image on Matrix, micro.blog, Discord, and Telegram
- **AI alt text** — optionally generate image descriptions via an OpenAI-compatible vision API
- **Digest mode** — batch email deliveries into hourly digests instead of one email per item
- **Mobile-responsive** — tables convert to cards, forms stack, 44px touch targets
- **Idempotent posting** — failed posts are retried, duplicates are prevented
- **Auto-initialization** — feeds set their baseline on first fetch, no manual init needed
- **Import/export** — back up or move your whole setup (feeds, echoes, and per-destination accounts) as one JSON file; import is idempotent and re-links echoes to the recreated rows
- **Email destination** — echo to email via SMTP in addition to Mastodon, Bluesky, micro.blog, Matrix, Discord, and webhooks, with optional full-HTML rendering (instant mode) and inline image embedding

## Tech Stack

- **Backend**: Python + FastAPI
- **Database**: SQLite (WAL mode) or Postgres
- **Frontend**: Jinja2 server-rendered templates + vanilla JS
- **Feed parsing**: feedparser (RSS/Atom) + native JSON Feed parser
- **Scheduler**: APScheduler (background feed checker)
- **HTTP client**: httpx

## Quick Start

### One-click deploy

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/jcrabapple/feedecho)

**Render.** The button reads [`render.yaml`](render.yaml): one web service running the published GHCR image, a 1 GB persistent disk at `/app/data`, and generated values for `FEEDECHO_AUTH_TOKEN` and `FEEDECHO_CREDENTIAL_KEY`. Render's free instances cannot attach a disk, so the Blueprint uses the paid Starter plan; without a disk every restart would wipe your data. Once it is live, copy `FEEDECHO_AUTH_TOKEN` from the service's **Environment** tab and log in with it. Render does not pull new images on its own: to upgrade, use **Manual Deploy → Deploy latest reference**.

**Railway.** Create a project from the image instead of the repo, and set the variables before the first deploy (FeedEcho refuses to start without `FEEDECHO_AUTH_TOKEN`, so a deploy without it crash-loops until you add it):

1. **New Project → Deploy a Docker Image** → `ghcr.io/jcrabapple/feedecho:latest`.
2. Right-click the service → **Attach Volume**, mount path `/app/data`.
3. Under **Variables**, set `FEEDECHO_AUTH_TOKEN` to a long random string (`openssl rand -base64 32`) and, optionally, `FEEDECHO_CREDENTIAL_KEY` to a Fernet key (`python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`).
4. Under **Settings → Networking**, click **Generate Domain** and set the target port to `8453`.
5. Deploy the staged changes. If the service went out before the domain existed, redeploy once so the container sees it.

On both platforms FeedEcho picks up the service's public URL by itself (`RENDER_EXTERNAL_URL` on Render, `RAILWAY_PUBLIC_DOMAIN` on Railway), uses it for the Mastodon OAuth callback and poke URLs, and marks the login cookie `Secure` because the platform terminates HTTPS in front of the container. Set `FEEDECHO_BASE_URL` only when you put a custom domain in front of the service. Keep `FEEDECHO_CREDENTIAL_KEY` once it is set: changing or losing it leaves stored credentials unreadable, and every destination has to be reconnected.

### Docker (recommended)

Pre-built multi-arch images (amd64 + arm64) are published to GHCR on every release — no local build needed:

```bash
mkdir feedecho && cd feedecho

cat > .env <<'EOF'
FEEDECHO_AUTH_TOKEN=change-me-to-a-long-random-string
FEEDECHO_CALLBACK_URL=http://localhost:8453/oauth/callback
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
FEEDECHO_AUTH_TOKEN=change-me-to-a-long-random-string
FEEDECHO_CALLBACK_URL=http://localhost:8453/oauth/callback
EOF

docker compose up -d
```

Open `http://localhost:8453` and log in with your `FEEDECHO_AUTH_TOKEN`.

Data lives in the `feedecho-data` volume (`/app/data` in the container) — your feeds, accounts, and history survive `docker compose up -d --build` rebuilds.

Plain Docker without compose:

```bash
docker build -t feedecho .
docker run -d --name feedecho \
  -p 8453:8453 \
  -v feedecho-data:/app/data \
  -e FEEDECHO_AUTH_TOKEN=change-me-to-a-long-random-string \
  -e FEEDECHO_CALLBACK_URL=http://localhost:8453/oauth/callback \
  feedecho
```

### Environment variables

| Variable | Required | Purpose |
|----------|----------|---------|
| `FEEDECHO_AUTH_TOKEN` | yes | Shared-secret login for the web UI. **The app refuses to start without it** unless you set `FEEDECHO_ALLOW_INSECURE=1` (no auth at all — only sane behind your own authenticated reverse proxy). |
| `FEEDECHO_ALLOW_INSECURE` | no | Set to `1` to boot single-user mode with no `FEEDECHO_AUTH_TOKEN` and no authentication. Logs a loud warning at startup. |
| `FEEDECHO_CALLBACK_URL` | for Mastodon OAuth | Public callback URL, e.g. `https://feedecho.example.com/oauth/callback`. Must match the URL reachable by your browser. Derived from `FEEDECHO_BASE_URL` when unset. |
| `FEEDECHO_BASE_URL` | no | Public base URL of your install. Used to derive the OAuth callback and the app website shown on posts. On Render and Railway it defaults to the service's platform URL. |
| `FEEDECHO_FORCE_SECURE_COOKIE` | no | Set to `1` to mark the login cookie `Secure` when TLS terminates at a reverse proxy in front of the app. On by default when a Render or Railway URL is detected; set `0` to turn it off. |
| `FEEDECHO_APP_WEBSITE` | no | Link behind the "FeedEcho" application name on Mastodon posts. Defaults to `FEEDECHO_BASE_URL`, then to the project repo. |
| `FEEDECHO_DB_PATH` | no | SQLite path (default `/app/data/feedecho.db` in Docker, `./feedecho.db` otherwise) |
| `FEEDECHO_STATE_SECRET` | required in multi mode (32+ chars) | OAuth state signing secret. In multi mode the app refuses to start without it. Single mode falls back to `FEEDECHO_AUTH_TOKEN`, then a random per-process value. |
| `FEEDECHO_ALLOW_BACKDATED_ENTRIES` | no | Set to `1` to deliver feed items that appear positionally older than the cursor but whose publish date is within `FEEDECHO_MAX_BACKDATED_ENTRY_DAYS` of now. Off by default. |
| `FEEDECHO_MAX_BACKDATED_ENTRY_DAYS` | no | How many days back to accept backdated entries (default `3`). Only consulted when `FEEDECHO_ALLOW_BACKDATED_ENTRIES=1`. |
| `FEEDECHO_BOOSTER_URL` / `FEEDECHO_BOOSTER_TOKEN` | no | FeedBooster service URL and shared token. When both are set, Mastodon destinations get a Boost toggle — per account on /accounts and per echo on /echoes (echoed posts are announced by the booster's fediverse account). Without them, the feature is hidden and inert. |

Behind a reverse proxy (nginx, Caddy, Traefik), point the proxy at port `8453` and set `FEEDECHO_CALLBACK_URL` to the public HTTPS URL.

### From source

```bash
git clone https://github.com/jcrabapple/feedecho.git
cd feedecho
python -m venv .venv
source .venv/bin/activate
pip install fastapi "uvicorn[standard]" jinja2 python-multipart feedparser httpx apscheduler cryptography pillow
FEEDECHO_AUTH_TOKEN=your-token python -m uvicorn app:app --host 0.0.0.0 --port 8453
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
4. **Add a Matrix room** — Copy the access token of the account that should post (Element: Settings → Help & About → Access Token), then enter the homeserver, token, and room ID or alias under Connect Matrix on `/accounts`. That account must already be in the room.
5. **Add a Discord channel** — In Discord, open the channel you want FeedEcho to post to, then Server Settings (or channel settings) → Integrations → Webhooks → New Webhook → Copy Webhook URL, and paste it under Connect Discord on `/accounts`.
6. **Add a Telegram chat** — Create a bot with @BotFather (`/newbot` and copy the API token), add the bot to the target chat (for channels, make it an admin), then paste the token and the chat ID or `@channelname` under Connect Telegram on `/accounts`.
7. **Add a webhook** — Under Connect Webhook on `/accounts`, enter any HTTP endpoint and optional custom headers (one per line, `Authorization: Bearer ...`). Each item arrives as one JSON object.
8. **Add a feed** — Go to `/feeds`, paste an RSS/Atom/JSON feed URL.
9. **Create an echo** — Go to `/echoes`, select a feed + destination, write a template like `{{ title }} {{ link }}`.
10. **Watch it run** — The scheduler checks feeds every 2 minutes and posts new items.

### Bluesky details

- Accounts connect via **App Passwords**, which are scoped to creating posts (and other app activity) and can be revoked individually without changing your main password.
- Sessions are cached per account (access + refresh JWTs in SQLite) and refreshed automatically; expired tokens trigger a transparent re-login and one retry.
- Posts are truncated to **300 graphemes** (Unicode-aware) and, separately, to Bluesky's 3000-byte limit on the post text; URLs and #hashtags in the text get proper richtext facets, so links are clickable and hashtags become real, searchable Bluesky tags.
- Image attachments upload through the PDS blob API with an `app.bsky.embed.images` embed carrying **up to 4 images** per post; each image's alt text uses the feed's own description when it provides one, otherwise your AI vision config when enabled. Images are capped at 2 MB each and jpeg/png/webp/gif (Bluesky's limits); oversized JPEG/PNG/WebP sources are downscaled or re-encoded to fit automatically, and one image that fails to fetch or upload is skipped without dropping the rest of the post.
- Content warnings and visibility settings are Mastodon-only and are ignored for Bluesky posts.

### Matrix details

- Authentication is an **access token** for the account that posts. FeedEcho never logs in with a password and never joins rooms: the account must already be a member of the target room, which keeps a stolen token from silently spreading into new rooms.
- `/.well-known/matrix/client` delegation is followed at connect time, so a server name that delegates its client API (`example.com` → `matrix.example.com`) works; the resolved base URL is stored per account.
- Room aliases are resolved to canonical room IDs at connect time, because aliases can be repointed at another room later.
- Messages are sent as `m.room.message` / `m.text` with an `org.matrix.custom.html` body so links are clickable in every client, and the transaction ID is derived from the echo and feed item — a retry after a lost response is de-duplicated by the homeserver instead of double-posting.
- Attached images are uploaded to the homeserver's media repo and sent as a second `m.image` event with alt text in `body` (Matrix has no combined text+image message). An image failure never fails the item: the text has already been delivered.
- Post history links to the message via `matrix.to`. Content warnings and visibility settings are Mastodon-only and are ignored for Matrix.

### Discord details

- Accounts connect via a **webhook URL**, which is the credential and the posting endpoint in one. Anyone with the URL can post to that channel, so treat it like a token; FeedEcho never renders it back in the UI.
- The webhook URL is verified at connect time with a read-only metadata request, and the webhook's own name pre-fills the display name.
- Posts send the rendered template as the message content (truncated to Discord's 2000-character limit). When image attachments are on and the item has one, a single embed carries the title, link, and image — Discord fetches the embed image itself, so an unusable image simply renders the post without the picture.
- Webhooks reply with no message ID and no guild link, so post history has no per-message URL for Discord. Content warnings and visibility settings are Mastodon-only and are ignored for Discord.
- Deleted or revoked webhooks fail permanently until reconnected; Discord's 30-messages-per-minute rate limit is treated as a transient error and rides the normal retry pipeline.

### Telegram details

- Accounts connect via a **bot token** (from @BotFather) plus a chat ID or `@channelname`. The bot must already be in the target chat; connect time verifies the token (`getMe`) and the chat (`getChat`) without posting anything, and stores Telegram's canonical numeric chat ID — so a channel rename can't break delivery and reconnecting via the number can't duplicate the row.
- The token is encrypted at rest and never rendered back in the UI. Reconnecting the same chat updates the stored row instead of duplicating it.
- Templates without `{{ content_html }}` send with no parse mode: Telegram auto-links URLs and hashtags, and raw prose can never break entity parsing. Templates embedding `{{ content_html }}` send as HTML, with the sanitized article reduced to Telegram's supported tags (links, bold/italic/underline/strikethrough, code, pre, blockquote; other markup degrades to text, and all text is entity-escaped). If the reduced HTML doesn't fit Telegram's 4096-character message cap (or the 1024-character photo caption cap), the send falls back to plain text so truncation can never leave a broken tag behind.
- With image attachments on, the first image goes out as a photo with the text as the caption (Telegram's 1024-character caption cap); a failed image fetch degrades to a text-only message.
- Public-channel posts get a `t.me` link in post history; private chats and groups have no public URL. Content warnings and visibility settings are Mastodon-only and are ignored for Telegram.
- A rejected token or a chat the bot can no longer see fails permanently until reconnected; Telegram rate limits (429 with `retry_after`) are transient and ride the normal retry pipeline.

### Webhook details

- Each item is POSTed as one flat JSON object: `text` (your template output), `id`, `title`, `link`, `summary`, `content`, `content_link`, `author`, `published`, `tags`, `image_url`, `image_alt`, and `feed_name`. Receivers map whatever shape they need; FeedEcho never downloads the image, the consumer fetches `image_url` itself if it wants it.
- **Custom JSON body (optional):** paste a JSON template to replace the default flat payload entirely — same variables as echo templates, and it must render to a JSON object (validated at connect time against a sample item containing quotes and newlines). Embed text values with the `tojson` filter and no surrounding quotes — `{{ title | tojson }}`, never `"{{ title }}"` — or any real item with a quote or newline breaks the JSON. Handy for push services: the shape brrr (brrr.now) expects is `{"title": {{ title | tojson }}, "message": {{ (summary or title) | tojson }}, "open_url": {{ link | tojson }}, "thread_id": {{ feed_name | tojson }}}`. Blank keeps any stored template on reconnect; `{}` clears one. A template that fails to render at dispatch time is a permanent delivery failure, like a rejected payload.
- Custom headers are optional and entered one per line (`Authorization: Bearer ...`). Header values are stored like credentials — never rendered back in the UI and never written to logs or error history.
- Connect stores the endpoint without posting to it. The Test button sends a real test delivery — a generic webhook has no read-only check.
- Self-hosted mode allows http and LAN/loopback targets (post to ntfy on your own network). The hosted service requires https and validates every URL against the SSRF guard, then sends through the pinned-IP transport, so a URL can never reach private addresses from our servers — and your header credentials never go out in cleartext.
- Redirects are never followed. Auth failures (401/403), vanished endpoints (404/410), and rejected payloads (400/422) fail permanently until you reconnect; rate limits and 5xx ride the retry pipeline. No per-message URL in post history, and visibility/content-warning settings don't apply.

### FeedBooster details

- When a booster is configured (`FEEDECHO_BOOSTER_URL` + `FEEDECHO_BOOSTER_TOKEN`; the hosted service runs one at `@feedbooster@feedecho.net`), each Mastodon account row on `/accounts` gains a **Boost: On/Off** toggle.
- With the toggle on, every post FeedEcho publishes to that account is also announced (boosted) by the booster's fediverse account: it shows up on the booster's profile, in the original author's notifications, and in the timelines of people who follow the booster. Posts made directly on the account, outside FeedEcho, are not boosted.
- Public and unlisted echoes are boosted; followers-only and direct posts never are, because a boost would leak them.
- Boost can also be enabled per echo: each Mastodon echo row on /echoes gains its own **Boost: On/Off** toggle. This is for picking specific echoes while the account setting is off — if the destination account has Boost on, every echo to it is boosted and the per-echo control is locked (shown as "Boost: Account"). The two settings are mutually exclusive: turn off the account-level Boost to control individual echoes. The per-echo toggle applies from the moment it is switched on; posts already published are not boosted retroactively.
- The boost request is fire-and-forget: a booster outage never delays or fails the post itself. The booster deduplicates per post and keeps its own retry queue.
- Self-hosters can run their own copy of [FeedBooster](https://github.com/jcrabapple/feedbooster) and point the two variables at it. Without a booster configured, the toggle is hidden and nothing changes.

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
| `{{ content_link }}` | First outbound link inside the item's content (link-blogs) |
| `{{ content_html }}` | Full content as sanitized HTML — links, formatting, and images kept; scripts/handlers/foreign schemes stripped at ingest. Renders in webhook bodies, email HTML parts (render_html echo flag), and Matrix formatted bodies. On Bluesky it converts to clean text with clickable link facets for article links. On Mastodon it embeds as HTML (Mastodon renders a safe subset); other plain-text destinations show the raw tags, so use `{{ content }}` there |
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

FeedEcho is ~19,000 lines of Python across 27 focused modules — no ORM, no build step. The shape:

- `app.py` — FastAPI routes, auth middleware, OAuth callbacks
- `database.py` — dual-dialect storage layer (SQLite WAL / Postgres) with idempotent migrations
- `feed_parser.py` — feed fetching with SSRF validation, size caps, and normalized item shapes
- `scheduler.py` — the dispatch engine: per-feed polling, atomic post claims, retries, drip/digest queues
- `mastodon.py` / `bluesky.py` / `microblog.py` / `matrix.py` / `discord.py` / `webhook.py` — one client module per destination
- `oauth.py` — Mastodon OAuth 2.0 flow with signed state
- `reader` views live in `app.py` + `templates/reader*.html` + `static/js/app.js` (folders, unread/star state, saved searches, compose desk)
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

FeedEcho uses shared-secret authentication via the `FEEDECHO_AUTH_TOKEN` environment variable:

- **If set**: all requests must include the token as either a cookie (set by the login page at `/login`) or an `X-Auth-Token` header (for API/programmatic access). Unauthenticated browser requests are redirected to `/login`; API requests get 401.
- **If unset**: the app **refuses to start**. A bare `docker run` or source install binds `0.0.0.0`, and starting unauthenticated there would expose the full UI to anyone who can reach the port. To run without auth on purpose (behind your own authenticated reverse proxy, or on a private bind), set `FEEDECHO_ALLOW_INSECURE=1` — the app then boots with a startup warning and auth fully disabled.

Only the endpoints that require unauthenticated access (OAuth callback, health check, static assets) are exempt from auth — everything else requires the session or token.

### OAuth flow

- The OAuth state parameter is **HMAC-signed and verified with a constant-time comparison**. The signature covers both the nonce and the instance, preventing CSRF and tampering with the instance field. A forged state token without the secret is rejected.
- The callback URL is configurable via the `FEEDECHO_CALLBACK_URL` environment variable. If unset, it is derived from `FEEDECHO_BASE_URL` (`<base>/oauth/callback`), and only falls back to `https://feedecho.example.com/oauth/callback` when neither is set. Self-hosters should set one of the two.
- Mastodon shows an application name on every post, linked to the `website` recorded when FeedEcho registered its OAuth app on your instance. That website is `FEEDECHO_APP_WEBSITE` if set, otherwise `FEEDECHO_BASE_URL`, otherwise the project repo. Both the website and the callback URL are stored alongside the cached client credentials in `oauth_apps`, and changing either re-registers the app on the next connect — Mastodon's API has no way to edit an existing registration, so a drifted callback URL would otherwise fail as a redirect mismatch. **Already-connected accounts keep the old link** — their access token is bound to the old app registration, so reconnect the account to update what appears on new posts.

### Secrets handling

- **Mastodon OAuth tokens, Bluesky app passwords and session JWTs, micro.blog app tokens, Matrix access tokens, Telegram bot tokens, Discord webhook URLs, and OAuth client secrets** are scoped credentials — revoke any of them at the source platform without touching your main passwords. At rest they are encrypted (Fernet) whenever `FEEDECHO_CREDENTIAL_KEY` is set — required in multi-tenant mode, optional in single-tenant mode, where it keeps copied database backups from being credential dumps. Keep the key stable once set: rows encrypted under it are unreadable without it (reconnect the affected accounts if you lose it). Without a key, single-tenant mode stores credentials plaintext because you own the database and the encryption key would live beside it.
- **SMTP passwords** are stored server-side and are **masked** in the web UI; saving the masked placeholder preserves the existing password.
- FeedEcho **does not** log tokens, passwords, or secrets to the application log. Log messages contain echo IDs, feed names, and error messages only.
- The `FEEDECHO_AUTH_TOKEN` env var doubles as the HMAC signing key for OAuth state tokens if set, so a single secret secures both layers.

### Input handling

- Feed content from external RSS/Atom/JSON feeds is treated as untrusted. HTML is stripped to plain text before posting to Mastodon. Feed item titles and URLs are never rendered as HTML in the UI without Jinja2 autoescaping.
- Templates render in a sandboxed Jinja2 environment — no `eval()`, no code execution path. A malformed template produces a validation error at save time, not a security hole.
- Feed fetches are capped at 10 MB to prevent memory exhaustion from hostile feeds.
- All rendered output is Jinja2-autoescaped; user-facing JS keeps untrusted markup out of the DOM.

### Network

- All outbound HTTP uses httpx with bounded timeouts (30 s by default, 60 s for Mastodon media uploads). FeedEcho makes requests to: the feed URL and any images in it (user-provided), the APIs of the destinations you connect — Mastodon instances, Bluesky's AppView/PDS/PLC directory, micro.blog's Micropub endpoints, your Matrix homeserver, Discord and generic webhook URLs (all user-provided) — and the SMTP server (admin-configured). No telemetry, no phone-home, no analytics.
- With `FEEDECHO_ALLOW_INSECURE=1` (no `FEEDECHO_AUTH_TOKEN`), FeedEcho is designed to run behind a reverse proxy or tunnel (Cloudflare Tunnel, nginx, etc.) with access control at the network layer. The built-in auth is the default; the flag exists for operators who enforce access at the network layer instead.

### Configuration

| Environment variable | Purpose | Default |
|---------------------|---------|---------|
| `FEEDECHO_AUTH_TOKEN` | Shared-secret auth token (enables login page + API auth, also signs OAuth state) | Unset — startup refused unless `FEEDECHO_ALLOW_INSECURE=1` |
| `FEEDECHO_ALLOW_INSECURE` | Boot single-user mode with no auth at all | Unset (auth required) |
| `FEEDECHO_CREDENTIAL_KEY` | Fernet key encrypting third-party credentials at rest | Unset (plaintext in single mode; required in multi mode) |
| `FEEDECHO_CALLBACK_URL` | Public URL for OAuth callback | `<FEEDECHO_BASE_URL>/oauth/callback`, else `https://feedecho.example.com/oauth/callback` |
| `FEEDECHO_APP_WEBSITE` | Website registered with the Mastodon OAuth app (the link on posts) | `FEEDECHO_BASE_URL`, else `https://github.com/jcrabapple/feedecho` |
| `FEEDECHO_DB_PATH` | Path to SQLite database | `./feedecho.db` |

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

If using OAuth, set `FEEDECHO_CALLBACK_URL` to your public URL:

```bash
export FEEDECHO_CALLBACK_URL="https://feedecho.yourdomain.com/oauth/callback"
export FEEDECHO_AUTH_TOKEN="your-secret-token"  # required unless FEEDECHO_ALLOW_INSECURE=1
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
FEEDECHO_MODE=single python -m pytest tests/ -v
```

CI additionally runs a multi-mode suite and a live Postgres dialect suite on every push.

## License

MIT

Built with the assistance of open source/open weight LLMs and reviewed by Claude.

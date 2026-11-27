# YTMLS: YouTube Music Library Sync

A self-hosted container that mirrors the parts of your YouTube Music library you choose into a folder Jellyfin uses as a music library. You pick albums and playlists in a web dashboard, and YTMLS keeps them in sync.

YTMLS **only reads** your YouTube Music account. It never likes, adds, removes or edits anything there. A test (`tests/test_read_only.py`) fails the build if code that could change your account is ever added.

## Status

| Step | What it covers | State |
| --- | --- | --- |
| 1. Foundation | Container, secure dashboard login, encrypted YouTube cookies, library list with sync checkboxes | Done |
| 2. Sync engine | Scheduled downloads (every 4 to 6 h), MP3 with tags and cover art, Jellyfin folder layout, `.m3u` playlists, removals waiting for confirmation, optional Premium quality | Planned |
| 3. Notifications and history | Browser push for new library items, sync summaries and removals waiting for you, plus a full change history | Planned (basic history page exists) |
| 4. Metadata editor | Edit tags and cover art in the dashboard, optional Jellyfin rescan | Planned |

Subscribed artists are left out for now.

## Quick start

1. Edit `compose.yaml`:
   - Set the `/path/to/jellyfin/music` volume to your Jellyfin music folder.
   - Set `user:` to the UID:GID that owns that folder.
2. `mkdir data && sudo chown 1000:1000 data`, using the same UID:GID as above.
3. `docker compose up -d --build`
4. Get the one-time setup token: `docker compose logs ytmls | grep "setup token"`
5. Open the dashboard, enter the token and create your admin account.
6. In **Settings → YouTube Music account**, add your cookies (see below). Your library loads as soon as they're accepted.
7. In **Settings → Two-factor authentication**, turn on 2FA.

### Getting cookies

1. Open a private/incognito window and sign in at `music.youtube.com`.
2. Export that site's cookies in Netscape `cookies.txt` format, for example with the "Get cookies.txt LOCALLY" browser extension.
3. Close the private window **without signing out**. Signing out invalidates the cookies.
4. Upload the file in Settings. A raw `Cookie:` request header copied from your browser's dev tools also works.

Cookies eventually expire. When a refresh fails, the dashboard tells you, and you can upload fresh ones.

## Exposing it with Cloudflare Tunnel

The dashboard is built to be reachable from the internet, but put more than one layer in front of it:

- **Publish no ports**, or bind them to `127.0.0.1` as `compose.yaml` does, so the tunnel is the only way in. `YTMLS_TRUST_PROXY_HEADERS=true` is safe only in that case, because it trusts `CF-Connecting-IP` for lockouts.
- **Cloudflare Access (recommended):** add a free Access application for the hostname with an email one-time-PIN or SSO policy. Then strangers never reach the login page at all.
- **Turn on 2FA** in Settings right after setup.
- Leave `YTMLS_COOKIE_SECURE=true`, since the tunnel serves HTTPS.

### What the app itself does

- A one-time setup token (from the container logs) is required to create the admin account, so nobody can claim a fresh instance.
- Passwords are hashed with Argon2id and must be at least 12 characters.
- Optional TOTP two-factor codes, with replay protection.
- Sessions are kept on the server. Cookies are `HttpOnly`, `Secure` and `SameSite=Strict`. Sessions expire after 72 hours idle or 30 days total.
- Every state-changing request needs a CSRF token and a matching `Origin`.
- Lockout: an IP is blocked after 5 failed attempts in 15 minutes, and all sign-ins pause after 20 failures from anywhere.
- Strict Content-Security-Policy, `X-Frame-Options: DENY`, HSTS and `no-store` caching. The API docs endpoints are disabled.
- YouTube cookies and TOTP secrets are encrypted at rest (Fernet).
  - The key is generated at `/data/secret.key` on first start.
  - To keep the key off the data volume (for example out of backups), set `YTMLS_SECRET_KEY` or `YTMLS_SECRET_KEY_FILE`, such as a Docker secret.
- The container runs as non-root on a read-only filesystem, with all capabilities dropped.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `YTMLS_DATA_DIR` | `/data` | Database and key file |
| `YTMLS_MUSIC_DIR` | `/music` | Jellyfin music folder |
| `YTMLS_COOKIE_SECURE` | `true` | Set to `false` only for plain-http local testing |
| `YTMLS_TRUST_PROXY_HEADERS` | `false` | Use `CF-Connecting-IP` / `X-Forwarded-For` as the client IP |
| `YTMLS_SECRET_KEY` / `YTMLS_SECRET_KEY_FILE` | (generated) | Encryption key for stored secrets |
| `YTMLS_SESSION_IDLE_HOURS` | `72` | Idle session timeout |
| `YTMLS_SESSION_MAX_DAYS` | `30` | Absolute session lifetime |
| `YTMLS_IP_MAX_FAILURES` | `5` | Failed sign-ins per IP before lockout |
| `YTMLS_ACCOUNT_MAX_FAILURES` | `20` | Failed sign-ins from all IPs before pausing |
| `YTMLS_LOCKOUT_MINUTES` | `15` | Lockout window |

## Forgot your password?

Stop the container and delete the account row. The next start prints a new setup token:

```sh
docker compose run --rm --entrypoint python ytmls -c \
  "import sqlite3; c=sqlite3.connect('/data/ytmls.db'); c.execute('DELETE FROM users'); c.commit()"
```

## Development

```sh
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest
YTMLS_DATA_DIR=./data YTMLS_COOKIE_SECURE=false uvicorn app.main:get_app --factory --reload
```

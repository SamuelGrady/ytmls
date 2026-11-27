# YTMLS: YouTube Music Library Sync

A self-hosted container that mirrors the parts of your YouTube Music library you choose into a folder Jellyfin uses as a music library. You pick albums and playlists in a web dashboard, and YTMLS keeps them in sync.

YTMLS **only reads** your YouTube Music account. It never likes, adds, removes or edits anything there. A test (`tests/test_read_only.py`) fails the build if code that could change your account is ever added.

## Status

| Step | What it covers | State |
| --- | --- | --- |
| 1. Foundation | Container, secure dashboard login, encrypted YouTube cookies, library list with sync checkboxes | Done |
| 2. Sync engine | Scheduled downloads (every 4 to 6 h), MP3 with tags and cover art, Jellyfin folder layout, `.m3u8` playlists, removals waiting for confirmation, optional Premium quality | Done |
| 3. Notifications and history | Browser push for new library items, sync summaries and removals waiting for you, plus a full change history | Done |
| 4. Metadata editor | Edit tags and cover art in the dashboard, optional Jellyfin rescan, security checklist | Done |

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

### Getting cookies that last

Exported cookies usually die within a day when the browser they came from keeps using the same session, because Google then rotates the values and the exported copy is left behind. Export from a private window that you then close, so YTMLS is the only thing using that session:

1. Open a new private/incognito window (close any other private windows first) and sign in at `music.youtube.com`.
2. In the same tab, open `https://music.youtube.com/robots.txt`, so YouTube's scripts don't rotate the cookies while you export.
3. Export that site's cookies in Netscape `cookies.txt` format, for example with the "Get cookies.txt LOCALLY" browser extension.
4. Close the private window right away, **without signing out**. Signing out ends the session.
5. Upload the file in Settings. A raw `Cookie:` request header copied from your browser's dev tools also works, but a `cookies.txt` file lasts longer.

After that YTMLS keeps the session alive on its own. It saves the refreshed cookie values YouTube sends back after every library read and download, and it makes a small read-only sign-in check about once an hour between syncs. If YouTube stops accepting the cookies anyway (two failed checks in a row), downloads pause and you get one push notification asking you to upload fresh ones.

## How syncing works

- Every few hours (6 by default, set in **Settings → Sync**), and whenever you press **Sync now**, YTMLS:
  1. Reads your library.
  2. Reads the tracks of every checked album and playlist.
  3. Downloads anything missing.
  4. Writes the playlist files.
- New songs added to a checked playlist are downloaded automatically.
- **Files:** `Album Artist/Album/NN - Title.mp3` in your music folder, with title, artists, album artist, album, track number, year and cover art embedded. Songs without an album, such as music videos, are filed as singles: `Artist/Title/Title.mp3`.
- **Playlists:** each checked playlist becomes `Playlists/<name>.m3u8`, which points at the album files. A song that's in several playlists is stored only once.
- **Removals never happen on their own.** These all add a request to the **Removals** page:
  - a song leaves a playlist
  - an album or playlist leaves your library
  - you uncheck an item

  You choose **Delete** (the file goes, and empty folders are cleaned up) or **Keep** (the file stays and you won't be asked again).
- **Quality:**
  - YTMLS takes the best audio stream YouTube offers and encodes it to MP3, 320 kbps by default. V0, 256 and 192 are also available.
  - With **Premium quality** turned on, it asks for the 256 kbps AAC stream that Premium accounts get. YouTube's streams are already lossy, so 320 kbps is the setting that loses the least extra quality.
- **Pace:** downloads are spaced a few seconds apart, and at most 200 run per sync (adjustable). A large first sync spreads over several runs.
- **Failures:** a failed download is retried on later runs, up to 5 times. You can retry it sooner from the album or playlist's page.
- **Read-only:** yt-dlp runs with `mark_watched` off, so nothing appears in your YouTube watch history.

## Notifications

Open **Settings → Notifications** on each device and press **Turn on for this device**. You'll get:

- a notification when new albums or playlists appear in your library, so you can decide whether to sync them (tapping it opens the library filtered to new items)
- a short summary after every sync: what was downloaded, which playlists got new songs, and any removals waiting for you (you can skip it when nothing changed)
- a notification when a sync fails, for example because your cookies expired

Push notifications need HTTPS, so turn them on while using your Cloudflare Tunnel address. On iPhone or iPad, add YTMLS to your Home Screen first (Share → Add to Home Screen), because Safari only allows web push for installed web apps. Set `YTMLS_VAPID_SUBJECT` to a `mailto:` address or your site URL if you want push services to have a real contact.

## Editing metadata

- **Tracks** lists every downloaded song, with search. Open one to change its title, artists, album, album artist, track number, year or cover art (JPEG or PNG).
- Saving rewrites the MP3's tags. If the album artist, album, track number or title changed, it also renames or moves the file to match, and updates every playlist file that includes it.
- On a synced album's page, **Edit album details** changes the album name, album artist, year or cover for every track at once. Use it to fix albums Jellyfin splits or groups wrongly.
- Edited tracks are marked **edited**, and later syncs never overwrite your changes.
- Edits wait if a sync is running.

## Jellyfin rescan (optional)

In **Settings → Jellyfin**, enter Jellyfin's address as the container sees it, for example `http://jellyfin:8096` when both are on the same Docker network. Then add an API key from Jellyfin's Dashboard → API Keys. YTMLS tests the connection when you save. Afterwards it asks Jellyfin to rescan shortly after a sync downloads something, after you confirm deletions, and after metadata edits. Bursts of changes are combined into one rescan.

## History

The **History** page records every change, grouped by day. You can filter it to syncs, library changes, tracks, removals, your sync choices, or account and security events. Each sync can be expanded to show exactly what it downloaded, what changed in each playlist, and what's waiting for removal.

### Keeping yt-dlp current

YouTube changes often, and old yt-dlp versions stop working. If downloads start failing for every track, rebuild the image to pick up the newest yt-dlp:

```sh
docker compose build --pull && docker compose up -d
```

## Exposing it with Cloudflare Tunnel

The dashboard is built to be reachable from the internet, but put more than one layer in front of it:

- **Publish no ports**, or bind them to `127.0.0.1` as `compose.yaml` does, so the tunnel is the only way in. `YTMLS_TRUST_PROXY_HEADERS=true` is safe only in that case, because it trusts `CF-Connecting-IP` for lockouts.
- **Cloudflare Access (recommended):** add a free Access application for the hostname with an email one-time-PIN or SSO policy. Then strangers never reach the login page at all.
- **Turn on 2FA** in Settings right after setup.
- Set `YTMLS_PUBLIC_URL` to your tunnel address. If you only ever use the tunnel, you can also set `YTMLS_COOKIE_SECURE=true`.
- If you see "Request blocked", the page explains which check failed. The container log has the details too.

**Settings → Security checklist** shows which of these are in place for the connection you're using right now. That includes whether the request came through Cloudflare Access.

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
| `YTMLS_COOKIE_SECURE` | `auto` | `auto` marks cookies Secure when the request came over HTTPS (the tunnel). `true` forces it, so plain-http access won't work |
| `YTMLS_PUBLIC_URL` | (none) | Your tunnel address, e.g. `https://music.example.com`. Accepted as a valid form origin |
| `YTMLS_TRUST_PROXY_HEADERS` | `false` | Use `CF-Connecting-IP` / `X-Forwarded-For` as the client IP |
| `YTMLS_SECRET_KEY` / `YTMLS_SECRET_KEY_FILE` | (generated) | Encryption key for stored secrets |
| `YTMLS_VAPID_SUBJECT` | `mailto:ytmls@example.com` | Contact sent to push services with each notification |
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
YTMLS_DATA_DIR=./data uvicorn app.main:get_app --factory --reload
```

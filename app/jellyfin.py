"""Optional: ask Jellyfin to rescan its libraries after YTMLS changes files."""

from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from .db import Database
from .security import Vault

log = logging.getLogger("ytmls.jellyfin")


class JellyfinError(Exception):
    pass


def normalise_url(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise JellyfinError("Enter the Jellyfin address with http:// or https://, e.g. http://jellyfin:8096")
    if parts.username or parts.password:
        raise JellyfinError("Don't put a username or password in the address; use an API key instead.")
    return url


class Jellyfin:
    def __init__(self, db: Database, vault: Vault, opener=None):
        self.db = db
        self.vault = vault
        self._open = opener or urllib.request.urlopen
        self._timer: threading.Timer | None = None
        self._timer_lock = threading.Lock()

    def configured(self) -> tuple[str, str] | None:
        url = self.db.get_setting("jellyfin_url")
        enc = self.db.get_setting("jellyfin_key_enc")
        if not url or not enc:
            return None
        return url, self.vault.decrypt(enc)

    def save(self, url: str, api_key: str | None) -> None:
        url = normalise_url(url)
        self.db.set_setting("jellyfin_url", url)
        if api_key:
            if len(api_key) > 200 or not api_key.isalnum():
                raise JellyfinError("That doesn't look like a Jellyfin API key.")
            self.db.set_setting("jellyfin_key_enc", self.vault.encrypt(api_key))

    def clear(self) -> None:
        for key in ("jellyfin_url", "jellyfin_key_enc"):
            self.db.set_setting(key, None)

    def _request(self, method: str, path: str, url: str, key: str) -> bytes:
        req = urllib.request.Request(
            url + path, method=method, data=b"" if method == "POST" else None,
            headers={"Authorization": f'MediaBrowser Client="YTMLS", Version="1", Token="{key}"',
                     "Accept": "application/json"})
        try:
            with self._open(req, timeout=15) as resp:
                return resp.read(1024 * 1024)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise JellyfinError("Jellyfin rejected the API key.") from exc
            raise JellyfinError(f"Jellyfin answered with HTTP {exc.code}.") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise JellyfinError(f"Couldn't reach Jellyfin: {getattr(exc, 'reason', exc)}") from exc

    def test(self) -> str:
        cfg = self.configured()
        if not cfg:
            raise JellyfinError("Jellyfin isn't set up yet.")
        info = json.loads(self._request("GET", "/System/Info", *cfg) or b"{}")
        return f"{info.get('ServerName', 'Jellyfin')} {info.get('Version', '')}".strip()

    def refresh_now(self) -> None:
        cfg = self.configured()
        if not cfg:
            return
        try:
            self._request("POST", "/Library/Refresh", *cfg)
            self.db.set_setting("jellyfin_last_refresh", json.dumps({"ok": True}))
            log.info("Asked Jellyfin to rescan")
        except JellyfinError as exc:
            self.db.set_setting("jellyfin_last_refresh", json.dumps({"ok": False, "error": str(exc)}))
            self.db.log_event("settings", f"Jellyfin rescan failed: {exc}")

    def request_refresh(self, delay: float = 30.0) -> None:
        """Schedule a rescan shortly, collapsing bursts of changes into one."""
        if not self.configured():
            return
        with self._timer_lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(delay, self.refresh_now)
            self._timer.daemon = True
            self._timer.start()

"""Keeps the stored YouTube Music cookies alive.

Google expires an exported cookie set quickly when nothing uses it, or when
the browser it came from keeps using the same session and rotates the values.
This module:

* stores the cookies encrypted (as before),
* merges back the refreshed values YouTube sends on every request,
* counts failed sign-in checks and raises one alert when they stop working.

A "generation" token changes on every upload, so a sync that started with the
old cookies can never write its refreshed copy over a freshly uploaded set.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from collections.abc import Callable

from .db import Database
from .security import Vault
from .ytm import merge_cookies, parse_cookies

log = logging.getLogger("ytmls.ytauth")

# Two failed checks in a row before alerting, so one network blip stays quiet.
FAILURES_BEFORE_ALERT = 2
KEEPALIVE_SECONDS = 3600

KEYS = ("ytm_cookies_enc", "ytm_cookies_saved_at", "ytm_cookies_expiry", "ytm_status", "ytm_cookies_gen",
        "ytm_cookies_refreshed_at", "ytm_fail_streak", "ytm_alerted", "ytm_last_check", "ytm_last_error")

# Phrases yt-dlp uses when YouTube rejects the sign-in, as opposed to one bad video.
AUTH_ERROR_HINTS = (
    "sign in to confirm",
    "cookies are no longer valid",
    "use --cookies",
    "login required",
)


def looks_like_auth_error(message: str) -> bool:
    msg = message.lower()
    return any(h in msg for h in AUTH_ERROR_HINTS)


class CookieStore:
    def __init__(self, db: Database, vault: Vault):
        self.db = db
        self.vault = vault
        self._lock = threading.Lock()
        # Called once when the sign-in stops working (after FAILURES_BEFORE_ALERT checks).
        self.on_expired: Callable[[str], None] | None = None

    # ------------------------------------------------------------ storage

    def load(self) -> tuple[str | None, str | None]:
        """Returns (cookies.txt text, generation)."""
        enc = self.db.get_setting("ytm_cookies_enc")
        if not enc:
            return None, None
        return self.vault.decrypt(enc), self.db.get_setting("ytm_cookies_gen")

    def netscape(self) -> str | None:
        return self.load()[0]

    def replace(self, netscape: str) -> str | None:
        """Store an uploaded set. Returns the previous encrypted value, for rollback."""
        with self._lock:
            previous = self.db.get_setting("ytm_cookies_enc")
            self.db.set_setting("ytm_cookies_enc", self.vault.encrypt(netscape))
            self.db.set_setting("ytm_cookies_gen", secrets.token_hex(8))
            return previous

    def rollback(self, previous_enc: str | None) -> None:
        with self._lock:
            self.db.set_setting("ytm_cookies_enc", previous_enc)
            self.db.set_setting("ytm_cookies_gen", secrets.token_hex(8))

    def uploaded(self, earliest_expiry: float | None) -> None:
        self.db.set_setting("ytm_cookies_saved_at", str(time.time()))
        self.db.set_setting("ytm_cookies_expiry", str(earliest_expiry) if earliest_expiry else None)
        self.db.set_setting("ytm_cookies_refreshed_at", None)
        self.record_ok()

    def clear(self) -> None:
        with self._lock:
            for key in KEYS:
                self.db.set_setting(key, None)

    def save_refreshed(self, updates: str | None, generation: str | None) -> bool:
        """Merge refreshed values from a library read or a yt-dlp run. Returns True if saved."""
        if not updates:
            return False
        with self._lock:
            current, gen = self.load()
            if current is None or gen != generation:
                return False  # cookies were replaced or removed meanwhile
            try:
                merged = merge_cookies(current, updates)
                if merged is None:
                    return False
                parsed = parse_cookies(merged)
            except Exception:
                log.warning("Ignoring refreshed cookies that could not be read", exc_info=True)
                return False
            self.db.set_setting("ytm_cookies_enc", self.vault.encrypt(parsed.netscape))
            self.db.set_setting("ytm_cookies_refreshed_at", str(time.time()))
            if parsed.earliest_expiry:
                self.db.set_setting("ytm_cookies_expiry", str(parsed.earliest_expiry))
            return True

    # ------------------------------------------------------------ health

    def record_ok(self) -> None:
        self.db.set_setting("ytm_status", "ok")
        self.db.set_setting("ytm_last_check", str(time.time()))
        self.db.set_setting("ytm_fail_streak", None)
        self.db.set_setting("ytm_last_error", None)
        if self.db.get_setting("ytm_alerted"):
            self.db.set_setting("ytm_alerted", None)
            self.db.log_event("security", "YouTube Music sign-in is working again")

    def record_failure(self, reason: str) -> None:
        self.db.set_setting("ytm_last_check", str(time.time()))
        self.db.set_setting("ytm_last_error", reason[:300])
        streak = int(self.db.get_setting("ytm_fail_streak") or 0) + 1
        self.db.set_setting("ytm_fail_streak", str(streak))
        if streak < FAILURES_BEFORE_ALERT:
            return
        self.db.set_setting("ytm_status", "error")
        if self.db.get_setting("ytm_alerted"):
            return
        self.db.set_setting("ytm_alerted", "1")
        self.db.log_event("security", "YouTube Music sign-in stopped working", reason[:300])
        if self.on_expired:
            try:
                self.on_expired(reason)
            except Exception:
                log.exception("Expiry alert failed")

    def keepalive_due(self, now: float | None = None) -> bool:
        if not self.db.get_setting("ytm_cookies_enc"):
            return False
        last = float(self.db.get_setting("ytm_last_check") or 0)
        return (now or time.time()) - last >= KEEPALIVE_SECONDS

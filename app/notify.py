"""Browser push notifications (Web Push with VAPID)."""

from __future__ import annotations

import base64
import json
import logging
import threading
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from .db import Database
from .security import Vault

log = logging.getLogger("ytmls.notify")

# Only real push services are accepted as subscription endpoints, so a
# compromised session can't use the server to make requests elsewhere.
PUSH_HOST_SUFFIXES = (
    ".googleapis.com",          # Chrome, Edge, Brave, Android
    ".push.services.mozilla.com",  # Firefox
    ".push.apple.com",          # Safari / iOS
    ".notify.windows.com",      # legacy Edge
)
MAX_SUBSCRIPTIONS = 20


def valid_endpoint(endpoint: str) -> bool:
    try:
        parts = urlsplit(endpoint)
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and any(host.endswith(s) or host == s.lstrip(".") for s in PUSH_HOST_SUFFIXES)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class Notifier:
    def __init__(self, db: Database, vault: Vault, subject: str,
                 sender: Callable[..., Any] | None = None, background: bool = True):
        self.db = db
        self.vault = vault
        self.subject = subject
        self._sender = sender
        self.background = background

    # ------------------------------------------------------------ keys

    def _vapid(self):
        from py_vapid import Vapid01

        enc = self.db.get_setting("vapid_private_enc")
        if enc:
            return Vapid01.from_pem(self.vault.decrypt(enc).encode())
        v = Vapid01()
        v.generate_keys()
        self.db.set_setting("vapid_private_enc", self.vault.encrypt(v.private_pem().decode()))
        return v

    def public_key(self) -> str:
        from cryptography.hazmat.primitives import serialization

        v = self._vapid()
        raw = v.public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        return _b64url(raw)

    # ------------------------------------------------------------ subscriptions

    def subscribe(self, sub: dict[str, Any], label: str) -> None:
        endpoint = str(sub.get("endpoint") or "")
        keys = sub.get("keys") or {}
        if not valid_endpoint(endpoint):
            raise ValueError("That push service isn't supported.")
        if not isinstance(keys.get("p256dh"), str) or not isinstance(keys.get("auth"), str):
            raise ValueError("Subscription keys are missing.")
        if len(endpoint) > 2048 or len(keys["p256dh"]) > 200 or len(keys["auth"]) > 100:
            raise ValueError("Subscription is malformed.")
        count = self.db.one("SELECT COUNT(*) AS n FROM push_subscriptions")["n"]
        exists = self.db.one("SELECT 1 FROM push_subscriptions WHERE endpoint = ?", (endpoint,))
        if not exists and count >= MAX_SUBSCRIPTIONS:
            raise ValueError(f"At most {MAX_SUBSCRIPTIONS} devices can be subscribed. Remove one first.")
        self.db.execute(
            "INSERT INTO push_subscriptions(endpoint, p256dh, auth, label, created_at) VALUES(?, ?, ?, ?, ?) "
            "ON CONFLICT(endpoint) DO UPDATE SET p256dh = excluded.p256dh, auth = excluded.auth, "
            "label = excluded.label",
            (endpoint, keys["p256dh"], keys["auth"], label[:120], time.time()),
        )

    def unsubscribe(self, endpoint: str | None = None, sub_id: int | None = None) -> None:
        if sub_id is not None:
            self.db.execute("DELETE FROM push_subscriptions WHERE id = ?", (sub_id,))
        elif endpoint:
            self.db.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))

    def subscriptions(self) -> list:
        return self.db.all("SELECT * FROM push_subscriptions ORDER BY created_at")

    # ------------------------------------------------------------ sending

    def send(self, title: str, body: str, url: str = "/", tag: str | None = None) -> None:
        payload = json.dumps({"title": title, "body": body[:400], "url": url, "tag": tag or "ytmls"})
        subs = self.subscriptions()
        if not subs:
            return
        if self.background:
            threading.Thread(target=self._deliver, args=(subs, payload), daemon=True).start()
        else:
            self._deliver(subs, payload)

    def _deliver(self, subs: list, payload: str) -> None:
        sender = self._sender
        if sender is None:
            from pywebpush import webpush as sender
        try:
            vapid = self._vapid()
        except Exception:
            log.exception("Could not load push keys")
            return
        for s in subs:
            info = {"endpoint": s["endpoint"], "keys": {"p256dh": s["p256dh"], "auth": s["auth"]}}
            try:
                sender(subscription_info=info, data=payload, vapid_private_key=vapid,
                       vapid_claims={"sub": self.subject}, ttl=24 * 3600, timeout=15)
                self.db.execute("UPDATE push_subscriptions SET last_ok = ?, failures = 0 WHERE id = ?",
                                (time.time(), s["id"]))
            except Exception as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status in (404, 410):
                    # The browser dropped this subscription; forget it.
                    self.db.execute("DELETE FROM push_subscriptions WHERE id = ?", (s["id"],))
                    log.info("Removed expired push subscription %s", s["id"])
                else:
                    self.db.execute("UPDATE push_subscriptions SET failures = failures + 1 WHERE id = ?",
                                    (s["id"],))
                    log.warning("Push to subscription %s failed: %s", s["id"], exc)

    # ------------------------------------------------------------ events

    def new_library_items(self, titles: list[str]) -> None:
        if not titles:
            return
        n = len(titles)
        shown = ", ".join(titles[:5]) + (f" and {n - 5} more" if n > 5 else "")
        self.send(f"{n} new item{'s' if n != 1 else ''} in your library",
                  f"{shown}. Open YTMLS to choose whether to sync {'them' if n != 1 else 'it'}.",
                  "/?filter=new", tag="ytmls-new")

    def cookies_expired(self, reason: str) -> None:
        self.send("YouTube Music sign-in expired",
                  "YouTube stopped accepting the saved cookies, so syncing is paused. "
                  "Open Settings to upload fresh ones.",
                  "/settings#youtube", tag="ytmls-cookies")

    def sync_finished(self, status: str, headline: str, details: list[str], removals: int, error: str | None,
                      quiet_if_unchanged: bool) -> None:
        if status == "error":
            self.send("Sync failed", (error or "Unknown error")[:300], "/history", tag="ytmls-sync")
            return
        if quiet_if_unchanged and headline == "No changes":
            return
        body = headline
        if details:
            body += "\n" + "\n".join(details[:4])
        url = "/removals" if removals else "/history"
        self.send("Sync finished" if status == "ok" else "Sync finished with problems", body, url, tag="ytmls-sync")

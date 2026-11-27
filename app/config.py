"""Runtime configuration, read from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw else default


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    music_dir: Path
    # Fernet key used to encrypt secrets (YouTube cookies, TOTP seed) at rest.
    # When unset, a key file is generated in data_dir on first start.
    secret_key: str | None
    secret_key_file: Path | None
    # Send cookies with the Secure flag. Keep on when served over HTTPS
    # (e.g. Cloudflare Tunnel). Turn off only for plain-http local testing.
    cookie_secure: bool
    # Trust CF-Connecting-IP / X-Forwarded-For for the client IP. Only enable
    # when the app is reachable exclusively through your proxy/tunnel.
    trust_proxy_headers: bool
    session_idle_hours: int
    session_max_days: int
    # Lockout policy
    ip_max_failures: int
    account_max_failures: int
    lockout_minutes: int

    @property
    def db_path(self) -> Path:
        return self.data_dir / "ytmls.db"


def load_settings() -> Settings:
    data_dir = Path(os.environ.get("YTMLS_DATA_DIR", "/data"))
    key_file = os.environ.get("YTMLS_SECRET_KEY_FILE")
    return Settings(
        data_dir=data_dir,
        music_dir=Path(os.environ.get("YTMLS_MUSIC_DIR", "/music")),
        secret_key=os.environ.get("YTMLS_SECRET_KEY") or None,
        secret_key_file=Path(key_file) if key_file else None,
        cookie_secure=_bool("YTMLS_COOKIE_SECURE", True),
        trust_proxy_headers=_bool("YTMLS_TRUST_PROXY_HEADERS", False),
        session_idle_hours=_int("YTMLS_SESSION_IDLE_HOURS", 72),
        session_max_days=_int("YTMLS_SESSION_MAX_DAYS", 30),
        ip_max_failures=_int("YTMLS_IP_MAX_FAILURES", 5),
        account_max_failures=_int("YTMLS_ACCOUNT_MAX_FAILURES", 20),
        lockout_minutes=_int("YTMLS_LOCKOUT_MINUTES", 15),
    )

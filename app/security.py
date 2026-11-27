"""Authentication and secret-handling primitives.

Covers: password hashing (Argon2id), at-rest encryption (Fernet), TOTP
two-factor codes with replay protection, server-side sessions with CSRF
tokens, and login lockout.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from cryptography.fernet import Fernet, InvalidToken

from .config import Settings
from .db import Database

MIN_PASSWORD_LENGTH = 12
PENDING_2FA_SECONDS = 300

_hasher = PasswordHasher()  # Argon2id with library defaults (RFC 9106 low-memory profile)

# A real hash of a random password, used so failed logins for unknown
# usernames take the same time as for the real one.
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(16))


# ---------------------------------------------------------------- encryption


def load_or_create_key(settings: Settings) -> bytes:
    if settings.secret_key:
        key = settings.secret_key.encode()
    else:
        path = settings.secret_key_file or (settings.data_dir / "secret.key")
        key = _read_or_create_key_file(path)
    Fernet(key)  # validates the key format
    return key


def _read_or_create_key_file(path: Path) -> bytes:
    if path.exists():
        return path.read_bytes().strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(key)
    return key


class Vault:
    """Encrypts small secrets for storage in the database."""

    def __init__(self, key: bytes):
        self._fernet = Fernet(key)

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken as exc:
            raise ValueError("Stored secret could not be decrypted; was the secret key changed?") from exc


# ---------------------------------------------------------------- passwords


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
    except (VerificationError, InvalidHashError):
        return False


def password_problem(password: str, confirm: str | None = None) -> str | None:
    if confirm is not None and password != confirm:
        return "Passwords do not match."
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
    if len(password) > 1024:
        return "Password is too long."
    return None


# ---------------------------------------------------------------- TOTP


def new_totp_secret() -> str:
    return pyotp.random_base32()


def totp_uri(secret: str, username: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name="YTMLS")


def verify_totp(secret: str, code: str, last_step: int, now: float | None = None) -> int | None:
    """Return the matched time step if the code is valid and not replayed."""
    code = "".join(ch for ch in code if ch.isdigit())
    if len(code) != 6:
        return None
    totp = pyotp.TOTP(secret)
    now = time.time() if now is None else now
    current = int(now // totp.interval)
    for step in (current - 1, current, current + 1):
        if step <= last_step:
            continue
        if hmac.compare_digest(totp.generate_otp(step), code):
            return step
    return None


# ---------------------------------------------------------------- sessions


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass
class Session:
    token_hash: str
    user_id: int
    username: str
    stage: str
    csrf_token: str


class SessionStore:
    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings

    def create(self, user_id: int, stage: str, ip: str | None, user_agent: str | None) -> tuple[str, str]:
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        now = time.time()
        self.db.execute(
            "INSERT INTO sessions(token_hash, user_id, stage, csrf_token, created_at, last_seen, ip, user_agent) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
            (_hash_token(token), user_id, stage, csrf, now, now, ip, (user_agent or "")[:300]),
        )
        return token, csrf

    def get(self, token: str | None) -> Session | None:
        if not token or len(token) > 200:
            return None
        row = self.db.one(
            "SELECT s.*, u.username FROM sessions s JOIN users u ON u.id = s.user_id WHERE token_hash = ?",
            (_hash_token(token),),
        )
        if row is None:
            return None
        now = time.time()
        if row["stage"] == "pending_2fa":
            expired = now - row["created_at"] > PENDING_2FA_SECONDS
        else:
            expired = (
                now - row["last_seen"] > self.settings.session_idle_hours * 3600
                or now - row["created_at"] > self.settings.session_max_days * 86400
            )
        if expired:
            self.db.execute("DELETE FROM sessions WHERE token_hash = ?", (row["token_hash"],))
            return None
        if now - row["last_seen"] > 60:
            self.db.execute("UPDATE sessions SET last_seen = ? WHERE token_hash = ?", (now, row["token_hash"]))
        return Session(row["token_hash"], row["user_id"], row["username"], row["stage"], row["csrf_token"])

    def delete(self, token_hash: str) -> None:
        self.db.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))

    def delete_all_for_user(self, user_id: int, except_hash: str | None = None) -> None:
        self.db.execute(
            "DELETE FROM sessions WHERE user_id = ? AND token_hash != ?", (user_id, except_hash or "")
        )

    def count_for_user(self, user_id: int) -> int:
        return self.db.one("SELECT COUNT(*) AS n FROM sessions WHERE user_id = ? AND stage = 'full'", (user_id,))["n"]


def csrf_matches(expected: str | None, provided: str | None) -> bool:
    return bool(expected) and bool(provided) and hmac.compare_digest(expected, provided)


# ---------------------------------------------------------------- lockout


class LoginThrottle:
    """Blocks an IP after repeated failures, and pauses all logins after a
    larger number of failures from anywhere (slows distributed guessing)."""

    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings

    @property
    def window(self) -> float:
        return self.settings.lockout_minutes * 60

    def _failures_since(self, since: float, ip: str | None = None) -> int:
        if ip is None:
            sql, params = "SELECT COUNT(*) AS n FROM login_attempts WHERE success = 0 AND ts > ?", (since,)
        else:
            sql = "SELECT COUNT(*) AS n FROM login_attempts WHERE success = 0 AND ts > ? AND ip = ?"
            params = (since, ip)
        return self.db.one(sql, params)["n"]

    def blocked_reason(self, ip: str) -> str | None:
        since = time.time() - self.window
        if self._failures_since(since, ip) >= self.settings.ip_max_failures:
            return f"Too many failed attempts. Try again in {self.settings.lockout_minutes} minutes."
        if self._failures_since(since) >= self.settings.account_max_failures:
            return f"Sign-in is temporarily paused after many failed attempts. Try again in {self.settings.lockout_minutes} minutes."
        return None

    def record(self, ip: str, success: bool) -> None:
        now = time.time()
        self.db.execute("INSERT INTO login_attempts(ip, ts, success) VALUES(?, ?, ?)", (ip, now, int(success)))
        if success:
            # A successful login clears that IP's failure count.
            self.db.execute("DELETE FROM login_attempts WHERE ip = ? AND success = 0", (ip,))
        self.db.execute("DELETE FROM login_attempts WHERE ts < ?", (now - 86400,))

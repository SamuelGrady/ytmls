"""Read-only access to the YouTube Music library.

Only the read methods listed in ``ReadOnlyYTMusic`` are ever called. The
underlying ytmusicapi client is kept private, and a test scans the codebase
to make sure no account-changing ytmusicapi method is referenced anywhere.
"""

from __future__ import annotations

import functools
import time
from dataclasses import dataclass
from typing import Any, Protocol

YTM_ORIGIN = "https://music.youtube.com"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
REQUIRED_COOKIE = "__Secure-3PAPISID"


class CookieError(ValueError):
    pass


@dataclass
class _Cookie:
    domain: str
    path: str
    secure: bool
    expires: int
    name: str
    value: str


@dataclass
class ParsedCookies:
    """Cookies for youtube.com, kept as a Netscape cookies.txt string so the
    downloader (yt-dlp) can use the exact same credentials later."""

    netscape: str
    cookie_header: str
    count: int
    earliest_expiry: float | None


def _netscape_from_header(raw: str) -> str:
    lines = ["# Netscape HTTP Cookie File"]
    expiry = int(time.time()) + 365 * 86400
    for part in raw.split(";"):
        if "=" not in part:
            continue
        name, value = part.strip().split("=", 1)
        if name:
            lines.append(f".youtube.com\tTRUE\t/\tTRUE\t{expiry}\t{name}\t{value}")
    return "\n".join(lines) + "\n"


def parse_cookies(text: str) -> ParsedCookies:
    """Accept either a Netscape cookies.txt export or a raw ``Cookie:`` header."""
    text = text.strip().lstrip("﻿")
    if not text:
        raise CookieError("No cookies were provided.")
    if len(text) > 512 * 1024:
        raise CookieError("Cookie data is too large.")

    if "\t" not in text:
        header = text[len("cookie:"):].strip() if text.lower().startswith("cookie:") else text
        text = _netscape_from_header(header)

    yt: list[_Cookie] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_"):]
        elif not line or line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) != 7:
            raise CookieError(f"Line {lineno} of the cookies file is not in Netscape format.")
        domain, _flag, path, secure, expires, name, value = fields
        if not domain.lstrip(".").endswith("youtube.com"):
            continue
        try:
            exp = int(float(expires))
        except ValueError:
            exp = 0
        yt.append(_Cookie(domain, path or "/", secure.upper() == "TRUE", exp, name, value))

    if not yt:
        raise CookieError("No youtube.com cookies found. Export cookies while on music.youtube.com.")
    if REQUIRED_COOKIE not in {c.name for c in yt}:
        raise CookieError(
            f"The {REQUIRED_COOKIE} cookie is missing. Make sure you are signed in to music.youtube.com "
            "when you export, and that the export includes secure/HttpOnly cookies."
        )

    out = ["# Netscape HTTP Cookie File"]
    for c in yt:
        flag = "TRUE" if c.domain.startswith(".") else "FALSE"
        out.append("\t".join([c.domain, flag, c.path, "TRUE" if c.secure else "FALSE", str(c.expires), c.name, c.value]))
    expiries = [c.expires for c in yt if c.expires]
    header = "; ".join(f"{c.name}={c.value}" for c in yt)
    return ParsedCookies("\n".join(out) + "\n", header, len(yt), float(min(expiries)) if expiries else None)


def cookie_header_from_netscape(netscape: str) -> str:
    return parse_cookies(netscape).cookie_header


# ---------------------------------------------------------------- refreshed cookies
#
# Google keeps a signed-in session alive by sending new values for some cookies
# (SIDCC, __Secure-3PSIDCC, __Secure-1PSIDTS ...) on normal requests. If those
# new values are thrown away, the copy YTMLS stores goes stale and stops working
# after a day or so. The helpers below merge the new values back into the stored
# copy. They never invent cookies and never drop any.

MAX_STORED_COOKIES = 300


def _read_youtube_cookies(text: str) -> list[_Cookie]:
    out = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_"):]
        elif not line or line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) != 7:
            continue
        domain, _flag, path, secure, expires, name, value = fields
        if not name or not domain.lstrip(".").endswith("youtube.com"):
            continue
        try:
            exp = int(float(expires))
        except ValueError:
            exp = 0
        out.append(_Cookie(domain, path or "/", secure.upper() == "TRUE", exp, name, value))
    return out


def _to_netscape(cookies: list[_Cookie]) -> str:
    out = ["# Netscape HTTP Cookie File"]
    for c in cookies:
        flag = "TRUE" if c.domain.startswith(".") else "FALSE"
        out.append("\t".join([c.domain, flag, c.path, "TRUE" if c.secure else "FALSE", str(c.expires), c.name, c.value]))
    return "\n".join(out) + "\n"


def netscape_from_jar(jar: Any) -> str:
    """Turn a requests/http.cookiejar jar into cookies.txt text (youtube.com only)."""
    cookies = [
        _Cookie(c.domain, c.path or "/", bool(c.secure), int(c.expires or 0), c.name, c.value or "")
        for c in jar if c.domain and c.domain.lstrip(".").endswith("youtube.com") and c.name
    ]
    return _to_netscape(cookies)


def merge_cookies(stored: str, updates: str, now: float | None = None) -> str | None:
    """Apply newer cookie values from ``updates`` on top of ``stored``.

    Returns the merged cookies.txt text, or None when nothing changed. Cookies
    that are being deleted (empty or already expired) are ignored, so a bad
    response can never wipe the sign-in.
    """
    now = time.time() if now is None else now
    current = _read_youtube_cookies(stored)
    index = {(c.domain, c.path, c.name): i for i, c in enumerate(current)}
    changed = False
    for c in _read_youtube_cookies(updates):
        if not c.value or (c.expires and c.expires < now):
            continue
        key = (c.domain, c.path, c.name)
        if key in index:
            old = current[index[key]]
            if old.value != c.value or (c.expires and c.expires != old.expires):
                # A session-only value keeps the stored expiry, so it isn't treated as expiring now.
                current[index[key]] = _Cookie(old.domain, old.path, old.secure or c.secure,
                                              c.expires or old.expires, c.name, c.value)
                changed = True
        elif len(current) < MAX_STORED_COOKIES:
            index[key] = len(current)
            current.append(c)
            changed = True
    if not changed or REQUIRED_COOKIE not in {c.name for c in current}:
        return None
    return _to_netscape(current)


# ---------------------------------------------------------------- library


@dataclass
class LibraryEntry:
    id: str
    kind: str  # 'album' | 'playlist'
    title: str
    subtitle: str | None
    year: str | None
    track_count: int | None
    thumbnail_url: str | None


class LibrarySource(Protocol):
    def library_albums(self) -> list[LibraryEntry]: ...
    def library_playlists(self) -> list[LibraryEntry]: ...
    def album(self, browse_id: str) -> dict[str, Any]: ...
    def playlist(self, playlist_id: str) -> dict[str, Any]: ...

    # Optional extras (ReadOnlyYTMusic has them; test fakes may not):
    #   ping() -> None                       small read to check the sign-in still works
    #   refreshed_cookies() -> str | None    cookies.txt with values YouTube sent back


def thumb_url(item: dict[str, Any]) -> str | None:
    return _thumb(item)


def _thumb(item: dict[str, Any]) -> str | None:
    thumbs = item.get("thumbnails") or []
    if not thumbs:
        return None
    best = max(thumbs, key=lambda t: (t.get("width") or 0))
    return best.get("url")


def _count(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        digits = "".join(ch for ch in value if ch.isdigit())
        return int(digits) if digits else None
    return None


def album_entry(item: dict[str, Any]) -> LibraryEntry | None:
    browse_id = item.get("browseId")
    if not browse_id:
        return None
    artists = ", ".join(a.get("name", "") for a in item.get("artists") or [] if a.get("name"))
    return LibraryEntry(
        id=browse_id,
        kind="album",
        title=item.get("title") or "Untitled album",
        subtitle=artists or None,
        year=str(item["year"]) if item.get("year") else None,
        track_count=None,
        thumbnail_url=_thumb(item),
    )


def playlist_entry(item: dict[str, Any]) -> LibraryEntry | None:
    playlist_id = item.get("playlistId")
    if not playlist_id:
        return None
    author = item.get("author")
    if isinstance(author, list):
        author = ", ".join(a.get("name", "") for a in author if a.get("name"))
    elif isinstance(author, dict):
        author = author.get("name")
    return LibraryEntry(
        id=playlist_id,
        kind="playlist",
        title=item.get("title") or "Untitled playlist",
        subtitle=author or None,
        year=None,
        track_count=_count(item.get("count")),
        thumbnail_url=_thumb(item),
    )


class ReadOnlyYTMusic:
    """Wraps ytmusicapi and exposes read operations only."""

    def __init__(self, cookie_header: str):
        from ytmusicapi import YTMusic  # imported lazily to keep tests light

        headers = {
            "cookie": cookie_header,
            "x-goog-authuser": "0",
            # ytmusicapi recomputes this from the cookie on every request; the
            # placeholder only tells it to use browser-style auth.
            "authorization": "SAPISIDHASH placeholder",
            "origin": YTM_ORIGIN,
            "x-origin": YTM_ORIGIN,
            "user-agent": USER_AGENT,
            "accept": "*/*",
            "content-type": "application/json",
        }
        import requests

        # Our own session, so we can see the refreshed cookies YouTube sends back.
        # ytmusicapi sets the Cookie header itself, so the jar only collects them.
        self.__session = requests.Session()
        self.__session.request = functools.partial(self.__session.request, timeout=30)  # type: ignore[method-assign]
        self.__client = YTMusic(auth=headers, requests_session=self.__session)

    def ping(self) -> None:
        """Cheapest library read; raises if YouTube no longer accepts the sign-in."""
        # get_account_info fails outright when signed out, unlike library reads,
        # which can quietly come back empty.
        info = self.__client.get_account_info()
        if not info or not info.get("accountName"):
            raise RuntimeError("YouTube Music says this browser session is signed out.")

    def refreshed_cookies(self) -> str | None:
        text = netscape_from_jar(self.__session.cookies)
        return text if text.count("\n") > 1 else None

    def library_albums(self) -> list[LibraryEntry]:
        items = self.__client.get_library_albums(limit=None)
        return [e for e in (album_entry(i) for i in items) if e]

    def library_playlists(self) -> list[LibraryEntry]:
        items = self.__client.get_library_playlists(limit=None)
        return [e for e in (playlist_entry(i) for i in items) if e]

    def album(self, browse_id: str) -> dict[str, Any]:
        return self.__client.get_album(browse_id)

    def playlist(self, playlist_id: str) -> dict[str, Any]:
        return self.__client.get_playlist(playlist_id, limit=None)

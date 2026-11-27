"""Web dashboard and API."""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
import secrets
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import segno
from fastapi import Body, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import security
from .config import Settings, load_settings
from .db import Database
from .downloader import Downloader, YtDlpDownloader
from .library import refresh_library
from .media import MP3_QUALITIES
from .notify import Notifier
from .sync import DEFAULTS, Scheduler, SyncEngine, SyncSummary, get_option
from .security import LoginThrottle, Session, SessionStore, Vault
from .ytm import CookieError, LibrarySource, ReadOnlyYTMusic, parse_cookies

log = logging.getLogger("ytmls")

BASE = Path(__file__).parent
SESSION_COOKIE = "ytmls_session"
PRE_COOKIE = "ytmls_pre"
MAX_COOKIE_UPLOAD = 512 * 1024

MESSAGES = {
    "cookies_saved": "YouTube Music cookies saved and verified.",
    "cookies_removed": "YouTube Music cookies removed.",
    "password_changed": "Password changed. Other sessions were signed out.",
    "2fa_enabled": "Two-factor authentication is on.",
    "2fa_disabled": "Two-factor authentication is off.",
    "sessions_revoked": "All other sessions were signed out.",
    "refreshed": "Library refreshed.",
    "sync_started": "Sync started. This page updates as it runs.",
    "sync_busy": "A sync is already running.",
    "sync_saved": "Sync settings saved.",
    "removals_done": "Done.",
    "push_test": "Test notification sent to every subscribed device.",
    "push_removed": "Device removed.",
    "notify_saved": "Notification settings saved.",
}

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; "
    "img-src 'self' data: https://*.googleusercontent.com https://*.ggpht.com https://i.ytimg.com; "
    "connect-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'; object-src 'none'"
)


class LoginRequired(Exception):
    def __init__(self, api: bool):
        self.api = api


class Forbidden(Exception):
    def __init__(self, reason: str, detail: str = ""):
        self.reason = reason
        self.detail = detail


def create_app(
    settings: Settings | None = None,
    ytm_factory: Callable[[str], LibrarySource] | None = None,
    downloader: Downloader | None = None,
    start_scheduler: bool = True,
    push_sender: Callable[..., Any] | None = None,
) -> FastAPI:
    settings = settings or load_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    db = Database(settings.db_path)
    vault = Vault(security.load_or_create_key(settings))
    sessions = SessionStore(db, settings)
    throttle = LoginThrottle(db, settings)
    ytm_factory = ytm_factory or ReadOnlyYTMusic
    templates = Jinja2Templates(directory=str(BASE / "templates"))
    templates.env.filters["ago"] = _ago
    templates.env.filters["until"] = lambda ts: _until(ts) or ""
    templates.env.filters["clock"] = lambda ts: time.strftime("%H:%M", time.localtime(ts))
    templates.env.filters["datetime"] = lambda ts: time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))

    def ytm_cookies() -> str | None:
        enc = db.get_setting("ytm_cookies_enc")
        return vault.decrypt(enc) if enc else None

    engine = SyncEngine(
        db, settings.music_dir, settings.data_dir / "tmp", ytm_cookies, ytm_factory,
        downloader or YtDlpDownloader(),
    )
    scheduler = Scheduler(engine, db)
    notifier = Notifier(db, vault, settings.vapid_subject, sender=push_sender, background=push_sender is None)

    def after_sync(status: str, summary: SyncSummary, error: str | None) -> None:
        notifier.new_library_items(summary.library_new)
        notifier.sync_finished(status, summary.headline(), summary.detail_lines(include_library=False),
                               len(summary.removals_queued), error,
                               quiet_if_unchanged=db.get_setting("notify_quiet_unchanged") == "1")

    engine.on_finished = after_sync

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # A run interrupted by a restart would otherwise look like it's still going.
        db.execute("UPDATE sync_runs SET status = 'error', error = 'Interrupted by restart', finished_at = ? "
                   "WHERE status = 'running'", (time.time(),))
        if start_scheduler:
            scheduler.start()
        yield
        scheduler.stop()

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.engine = engine
    app.state.scheduler = scheduler
    app.state.notifier = notifier
    app.state.db = db
    app.state.settings = settings
    app.state.setup_token = None
    app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")

    def has_admin() -> bool:
        return db.one("SELECT 1 FROM users LIMIT 1") is not None

    if not has_admin():
        app.state.setup_token = secrets.token_urlsafe(18)
        log.warning("No admin account yet. Open /setup and enter this setup token: %s", app.state.setup_token)
        print(f"[ytmls] First-run setup token: {app.state.setup_token}", flush=True)

    # ------------------------------------------------------------ helpers

    def client_ip(request: Request) -> str:
        if settings.trust_proxy_headers:
            cf = request.headers.get("cf-connecting-ip")
            if cf:
                return cf.strip()[:64]
            xff = request.headers.get("x-forwarded-for")
            if xff:
                return xff.split(",")[0].strip()[:64]
        return request.client.host if request.client else "unknown"

    def is_https(request: Request) -> bool:
        if request.url.scheme == "https":
            return True
        proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
        return proto == "https" or '"scheme":"https"' in request.headers.get("cf-visitor", "").replace(" ", "")

    def cookie_secure(request: Request) -> bool:
        return settings.cookie_secure if settings.cookie_secure is not None else is_https(request)

    def set_cookie(request: Request, resp: Response, name: str, value: str, max_age: int | None) -> None:
        resp.set_cookie(
            name, value, max_age=max_age, httponly=True, secure=cookie_secure(request),
            samesite="strict", path="/",
        )

    def current_session(request: Request) -> Session | None:
        return sessions.get(request.cookies.get(SESSION_COOKIE))

    def require(request: Request, api: bool = False) -> Session:
        s = current_session(request)
        if s is None or s.stage != "full":
            raise LoginRequired(api)
        return s

    def check_origin(request: Request) -> None:
        origin = request.headers.get("origin")
        if not origin or origin == "null":
            return
        allowed = {request.headers.get("host", "").lower()}
        fwd = request.headers.get("x-forwarded-host")
        if fwd:
            allowed.add(fwd.split(",")[0].strip().lower())
        if settings.public_url:
            allowed.add(urlsplit(settings.public_url).netloc.lower())
        got = urlsplit(origin).netloc.lower()
        if got not in allowed:
            raise Forbidden("origin", f"form posted from {got}, but this server answers as {', '.join(sorted(allowed))}")

    def require_csrf(request: Request, s: Session, token: str | None) -> None:
        check_origin(request)
        if not security.csrf_matches(s.csrf_token, token):
            raise Forbidden("token")

    def pre_token(request: Request) -> str:
        return request.cookies.get(PRE_COOKIE) or secrets.token_urlsafe(24)

    def require_pre_csrf(request: Request, token: str) -> None:
        check_origin(request)
        cookie = request.cookies.get(PRE_COOKIE)
        if cookie is None:
            raise Forbidden("no_cookie")
        if not security.csrf_matches(cookie, token):
            raise Forbidden("token")

    def render(request: Request, name: str, ctx: dict[str, Any], status: int = 200,
               pre: str | None = None) -> HTMLResponse:
        msg = request.query_params.get("msg")
        ctx.setdefault("notice", MESSAGES.get(msg or ""))
        if ctx.get("s"):
            ctx.setdefault("pending_removals", db.one(
                "SELECT COUNT(*) AS n FROM removals WHERE decision IS NULL")["n"])
        resp = templates.TemplateResponse(request, name, ctx, status_code=status)
        if pre:
            set_cookie(request, resp, PRE_COOKIE, pre, 3600)
        return resp

    def user_row(user_id: int):
        return db.one("SELECT * FROM users WHERE id = ?", (user_id,))

    def start_session(request: Request, user_id: int, stage: str, to: str) -> RedirectResponse:
        old = request.cookies.get(SESSION_COOKIE)
        if old:
            existing = sessions.get(old)
            if existing:
                sessions.delete(existing.token_hash)
        token, _ = sessions.create(user_id, stage, client_ip(request), request.headers.get("user-agent"))
        resp = RedirectResponse(to, status_code=303)
        max_age = security.PENDING_2FA_SECONDS if stage == "pending_2fa" else settings.session_max_days * 86400
        set_cookie(request, resp, SESSION_COOKIE, token, max_age)
        resp.delete_cookie(PRE_COOKIE, path="/")
        return resp

    def do_refresh() -> str:
        netscape = ytm_cookies()
        if not netscape:
            raise CookieError("Add your YouTube Music cookies in Settings first.")
        header = parse_cookies(netscape).cookie_header
        try:
            result = refresh_library(db, ytm_factory(header))
            if not result.first_import:
                notifier.new_library_items([e.title for e in result.added])
        except CookieError:
            raise
        except Exception as exc:  # network / auth failures from ytmusicapi
            db.set_setting("ytm_status", f"error:{type(exc).__name__}")
            log.exception("Library refresh failed")
            raise CookieError(
                "YouTube Music did not accept the request. The cookies may have expired; export fresh ones."
            ) from exc
        db.set_setting("ytm_status", "ok")
        return result.summary()

    # ------------------------------------------------------------ middleware & errors

    @app.middleware("http")
    async def headers_mw(request: Request, call_next):
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > MAX_COOKIE_UPLOAD + 64 * 1024:
            return Response("Request too large", status_code=413)
        resp = await call_next(request)
        resp.headers["Content-Security-Policy"] = CSP
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        resp.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        if is_https(request):
            resp.headers["Strict-Transport-Security"] = "max-age=31536000"
        if not request.url.path.startswith("/static"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, exc: LoginRequired):
        if exc.api:
            return JSONResponse({"error": "not signed in"}, status_code=401)
        target = "/setup" if not has_admin() else "/login"
        return RedirectResponse(target, status_code=303)

    @app.exception_handler(Forbidden)
    async def _forbidden(request: Request, exc: Forbidden):
        if exc.reason == "no_cookie":
            if settings.cookie_secure and not is_https(request):
                why = ("Your browser didn't send back its security cookie. This page was opened over plain "
                       "http, but YTMLS_COOKIE_SECURE is \"true\", so the browser only keeps the cookie on https. "
                       "Use the https address, or set YTMLS_COOKIE_SECURE to \"auto\".")
            else:
                why = ("Your browser didn't send back its security cookie. Check that cookies are allowed for "
                       "this site, then reload the page and try again.")
        elif exc.reason == "origin":
            why = ("The form was sent from a different address than this server expected "
                   f"({exc.detail}). If you reach YTMLS through a tunnel or proxy, set YTMLS_PUBLIC_URL to the "
                   "address you use, for example https://music.example.com.")
        else:
            why = "The form expired, for example because you signed in again in another tab. Reload and try again."
        log.warning("Rejected %s %s: %s %s", request.method, request.url.path, exc.reason, exc.detail)
        back = request.url.path if request.url.path in {"/login", "/setup"} else "/"
        return templates.TemplateResponse(request, "forbidden.html", {"why": why, "back": back}, status_code=403)

    # ------------------------------------------------------------ public

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/setup")
    def setup_page(request: Request):
        if has_admin():
            return RedirectResponse("/login", status_code=303)
        pre = pre_token(request)
        return render(request, "setup.html", {"pre": pre}, pre=pre)

    @app.post("/setup")
    def setup_submit(
        request: Request,
        pre: str = Form(""),
        setup_token: str = Form(""),
        username: str = Form(""),
        password: str = Form(""),
        confirm: str = Form(""),
    ):
        require_pre_csrf(request, pre)
        if has_admin():
            return RedirectResponse("/login", status_code=303)
        ip = client_ip(request)
        if reason := throttle.blocked_reason(ip):
            return render(request, "setup.html", {"pre": pre, "error": reason}, 429)
        error = None
        if not security.csrf_matches(app.state.setup_token, setup_token.strip()):
            throttle.record(ip, False)
            error = "Setup token is incorrect. Find it in the container logs."
        elif not (username := username.strip()) or len(username) > 64:
            error = "Choose a username (up to 64 characters)."
        else:
            error = security.password_problem(password, confirm)
        if error:
            return render(request, "setup.html", {"pre": pre, "error": error, "username": username}, 400)
        cur = db.execute(
            "INSERT INTO users(username, password_hash, created_at) VALUES(?, ?, ?)",
            (username, security.hash_password(password), time.time()),
        )
        app.state.setup_token = None
        db.log_event("security", f"Admin account '{username}' created")
        return start_session(request, cur.lastrowid, "full", "/settings")

    @app.get("/login")
    def login_page(request: Request):
        if not has_admin():
            return RedirectResponse("/setup", status_code=303)
        s = current_session(request)
        if s and s.stage == "full":
            return RedirectResponse("/", status_code=303)
        pre = pre_token(request)
        return render(request, "login.html", {"pre": pre}, pre=pre)

    @app.post("/login")
    def login_submit(request: Request, pre: str = Form(""), username: str = Form(""), password: str = Form("")):
        require_pre_csrf(request, pre)
        ip = client_ip(request)
        if reason := throttle.blocked_reason(ip):
            return render(request, "login.html", {"pre": pre, "error": reason}, 429)
        row = db.one("SELECT * FROM users WHERE username = ?", (username.strip(),))
        if not security.verify_password(row["password_hash"] if row else None, password[:1024]):
            throttle.record(ip, False)
            log.warning("Failed sign-in from %s", ip)
            return render(request, "login.html", {"pre": pre, "error": "Incorrect username or password."}, 401)
        if row["totp_enabled"]:
            return start_session(request, row["id"], "pending_2fa", "/login/2fa")
        throttle.record(ip, True)
        db.log_event("security", f"Signed in from {ip}")
        return start_session(request, row["id"], "full", "/")

    @app.get("/login/2fa")
    def twofa_page(request: Request):
        s = current_session(request)
        if s is None or s.stage != "pending_2fa":
            return RedirectResponse("/login", status_code=303)
        return render(request, "twofa.html", {"csrf": s.csrf_token})

    @app.post("/login/2fa")
    def twofa_submit(request: Request, csrf_token: str = Form(""), code: str = Form("")):
        s = current_session(request)
        if s is None or s.stage != "pending_2fa":
            return RedirectResponse("/login", status_code=303)
        require_csrf(request, s, csrf_token)
        ip = client_ip(request)
        if reason := throttle.blocked_reason(ip):
            sessions.delete(s.token_hash)
            return render(request, "login.html", {"pre": pre_token(request), "error": reason}, 429)
        row = user_row(s.user_id)
        step = security.verify_totp(vault.decrypt(row["totp_secret_enc"]), code, row["totp_last_step"])
        if step is None:
            throttle.record(ip, False)
            return render(request, "twofa.html", {"csrf": s.csrf_token, "error": "That code didn't work."}, 401)
        db.execute("UPDATE users SET totp_last_step = ? WHERE id = ?", (step, row["id"]))
        throttle.record(ip, True)
        db.log_event("security", f"Signed in with two-factor from {ip}")
        return start_session(request, row["id"], "full", "/")

    @app.post("/logout")
    def logout(request: Request, csrf_token: str = Form("")):
        s = current_session(request)
        if s:
            require_csrf(request, s, csrf_token)
            sessions.delete(s.token_hash)
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(SESSION_COOKIE, path="/")
        return resp

    # ------------------------------------------------------------ dashboard

    @app.get("/")
    def library_page(request: Request):
        s = require(request)
        items = db.all(
            "SELECT li.*, "
            "(SELECT COUNT(*) FROM item_tracks it JOIN tracks t ON t.video_id = it.video_id "
            " WHERE it.item_id = li.id AND it.removed_at IS NULL AND t.status = 'downloaded') AS downloaded, "
            "(SELECT COUNT(*) FROM item_tracks it WHERE it.item_id = li.id AND it.removed_at IS NULL) AS known "
            "FROM library_items li ORDER BY li.removed_at IS NOT NULL, li.kind DESC, li.title COLLATE NOCASE")
        last = db.get_setting("library_last_refresh")
        return render(request, "library.html", {
            "s": s, "items": items, "csrf": s.csrf_token,
            "last_refresh": float(last) if last else None,
            "status": sync_status(),
            "has_cookies": db.get_setting("ytm_cookies_enc") is not None,
            "new_count": sum(1 for i in items if not i["reviewed"] and i["removed_at"] is None),
            "error": request.query_params.get("error") == "refresh" and db.get_setting("last_refresh_error"),
        })

    @app.post("/library/refresh")
    def library_refresh(request: Request, csrf_token: str = Form("")):
        s = require(request)
        require_csrf(request, s, csrf_token)
        try:
            do_refresh()
        except CookieError as exc:
            db.set_setting("last_refresh_error", str(exc))
            return RedirectResponse("/?error=refresh", status_code=303)
        return RedirectResponse("/?msg=refreshed", status_code=303)

    @app.post("/api/items/{item_id}/sync")
    def api_toggle(request: Request, item_id: str, payload: dict = Body(...)):
        s = require(request, api=True)
        require_csrf(request, s, request.headers.get("x-csrf-token"))
        enabled = bool(payload.get("enabled"))
        row = db.one("SELECT title, sync_enabled FROM library_items WHERE id = ?", (item_id,))
        if row is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        db.execute("UPDATE library_items SET sync_enabled = ?, reviewed = 1 WHERE id = ?", (int(enabled), item_id))
        if bool(row["sync_enabled"]) != enabled:
            db.log_event("selection", f"{'Sync turned on' if enabled else 'Sync turned off'}: {row['title']}")
        return {"id": item_id, "enabled": enabled}

    @app.post("/api/items/review-all")
    def api_review_all(request: Request):
        s = require(request, api=True)
        require_csrf(request, s, request.headers.get("x-csrf-token"))
        db.execute("UPDATE library_items SET reviewed = 1 WHERE reviewed = 0")
        return {"ok": True}

    # ------------------------------------------------------------ sync

    def sync_status() -> dict[str, Any]:
        last = db.one("SELECT * FROM sync_runs WHERE status != 'running' ORDER BY id DESC LIMIT 1")
        last_summary = SyncSummary.from_json(last["summary"]) if last else None
        headline = last_summary.headline() if last_summary else None
        return {
            "running": engine.running or scheduler.queued,
            "progress": dict(engine.progress),
            "last_run": {"finished_at": last["finished_at"], "status": last["status"], "headline": headline,
                         "error": last["error"]} if last else None,
            "next_run": scheduler.next_run_at(),
            "pending_removals": db.one("SELECT COUNT(*) AS n FROM removals WHERE decision IS NULL")["n"],
        }

    @app.post("/sync/now")
    def sync_now(request: Request, csrf_token: str = Form("")):
        s = require(request)
        require_csrf(request, s, csrf_token)
        started = scheduler.trigger_now()
        return RedirectResponse(f"/?msg={'sync_started' if started else 'sync_busy'}", status_code=303)

    @app.get("/api/status")
    def api_status(request: Request):
        require(request, api=True)
        st = sync_status()
        st["last_run_ago"] = _ago(st["last_run"]["finished_at"]) if st["last_run"] else None
        st["next_run_in"] = _until(st["next_run"])
        return st

    @app.get("/items/{item_id}")
    def item_page(request: Request, item_id: str):
        s = require(request)
        item = db.one("SELECT * FROM library_items WHERE id = ?", (item_id,))
        if item is None:
            return Response("Not found", status_code=404)
        tracks = db.all(
            "SELECT t.*, it.position, it.removed_at AS link_removed FROM item_tracks it "
            "JOIN tracks t ON t.video_id = it.video_id WHERE it.item_id = ? "
            "ORDER BY it.removed_at IS NOT NULL, it.position", (item_id,))
        return render(request, "item.html", {"s": s, "csrf": s.csrf_token, "item": item, "tracks": tracks})

    @app.post("/tracks/{video_id}/retry")
    def track_retry(request: Request, video_id: str, csrf_token: str = Form(""), back: str = Form("/")):
        s = require(request)
        require_csrf(request, s, csrf_token)
        db.execute("UPDATE tracks SET attempts = 0, status = 'pending', error = NULL WHERE video_id = ? "
                   "AND status = 'failed'", (video_id,))
        target = back if back.startswith("/items/") else "/"
        return RedirectResponse(target, status_code=303)

    @app.get("/removals")
    def removals_page(request: Request):
        s = require(request)
        pending = db.all("SELECT * FROM removals WHERE decision IS NULL ORDER BY created_at, title")
        decided = db.all("SELECT * FROM removals WHERE decision IS NOT NULL ORDER BY decided_at DESC LIMIT 50")
        return render(request, "removals.html", {"s": s, "csrf": s.csrf_token, "pending": pending,
                                                 "decided": decided})

    @app.post("/removals/decide")
    def removals_decide(request: Request, csrf_token: str = Form(""), action: str = Form(""),
                        ids: list[int] = Form(default_factory=list)):
        s = require(request)
        require_csrf(request, s, csrf_token)
        if action not in {"delete", "keep"}:
            return RedirectResponse("/removals", status_code=303)
        for rid in ids[:5000]:
            engine.decide_removal(rid, delete=(action == "delete"))
        return RedirectResponse("/removals?msg=removals_done", status_code=303)

    @app.post("/settings/sync")
    def settings_sync(request: Request, csrf_token: str = Form(""), interval: str = Form("6"),
                      quality: str = Form("320"), premium: str = Form(""), max_per_run: str = Form("200")):
        s = require(request)
        require_csrf(request, s, csrf_token)
        try:
            hours = min(48.0, max(1.0, float(interval)))
            per_run = min(2000, max(1, int(max_per_run)))
        except ValueError:
            return render(request, "settings.html", settings_ctx(s, sync_error="Enter numbers for the interval "
                                                                    "and the per-run limit."), 400)
        if quality not in MP3_QUALITIES:
            quality = DEFAULTS["mp3_quality"]
        db.set_setting("sync_interval_hours", f"{hours:g}")
        db.set_setting("mp3_quality", quality)
        db.set_setting("premium_quality", "1" if premium else "0")
        db.set_setting("max_downloads_per_run", str(per_run))
        db.log_event("settings", f"Sync settings: every {hours:g}h, MP3 {quality}, "
                                 f"Premium quality {'on' if premium else 'off'}, up to {per_run} downloads per run")
        return RedirectResponse("/settings?msg=sync_saved#sync", status_code=303)

    # ------------------------------------------------------------ push notifications

    @app.get("/sw.js")
    def service_worker():
        resp = Response((BASE / "static" / "sw.js").read_text(), media_type="text/javascript")
        resp.headers["Service-Worker-Allowed"] = "/"
        return resp

    @app.get("/api/push/key")
    def push_key(request: Request):
        require(request, api=True)
        return {"key": notifier.public_key()}

    @app.post("/api/push/subscribe")
    def push_subscribe(request: Request, payload: dict = Body(...)):
        s = require(request, api=True)
        require_csrf(request, s, request.headers.get("x-csrf-token"))
        label = str(payload.get("label") or _device_label(request.headers.get("user-agent", "")))
        try:
            notifier.subscribe(payload.get("subscription") or {}, label)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        db.log_event("settings", f"Notifications turned on for {label}")
        return {"ok": True}

    @app.post("/api/push/unsubscribe")
    def push_unsubscribe(request: Request, payload: dict = Body(...)):
        s = require(request, api=True)
        require_csrf(request, s, request.headers.get("x-csrf-token"))
        notifier.unsubscribe(endpoint=str(payload.get("endpoint") or ""))
        return {"ok": True}

    @app.post("/settings/push/remove")
    def push_remove(request: Request, csrf_token: str = Form(""), sub_id: int = Form(0)):
        s = require(request)
        require_csrf(request, s, csrf_token)
        notifier.unsubscribe(sub_id=sub_id)
        db.log_event("settings", "Removed a device from notifications")
        return RedirectResponse("/settings?msg=push_removed#notifications", status_code=303)

    @app.post("/settings/push/test")
    def push_test(request: Request, csrf_token: str = Form("")):
        s = require(request)
        require_csrf(request, s, csrf_token)
        notifier.send("YTMLS test", "Notifications are working.", "/settings#notifications", tag="ytmls-test")
        return RedirectResponse("/settings?msg=push_test#notifications", status_code=303)

    @app.post("/settings/notifications")
    def notify_settings(request: Request, csrf_token: str = Form(""), quiet: str = Form("")):
        s = require(request)
        require_csrf(request, s, csrf_token)
        db.set_setting("notify_quiet_unchanged", "1" if quiet else "0")
        return RedirectResponse("/settings?msg=notify_saved#notifications", status_code=303)

    @app.get("/history")
    def history_page(request: Request, kind: str = "all", before: int | None = None):
        s = require(request)
        kinds = HISTORY_FILTERS.get(kind)
        if kinds is None:
            kind, kinds = "all", []
        where, params = [], []
        if kinds:
            where.append(f"kind IN ({','.join('?' for _ in kinds)})")
            params += kinds
        if before:
            where.append("id < ?")
            params.append(before)
        sql = "SELECT * FROM events" + (" WHERE " + " AND ".join(where) if where else "")
        rows = db.all(sql + " ORDER BY id DESC LIMIT ?", (*params, HISTORY_PAGE + 1))
        more = len(rows) > HISTORY_PAGE
        rows = rows[:HISTORY_PAGE]
        days: list[dict[str, Any]] = []
        for r in rows:
            label = _day_label(r["ts"])
            if not days or days[-1]["label"] != label:
                days.append({"label": label, "events": []})
            days[-1]["events"].append(_event_view(r))
        return render(request, "history.html", {
            "s": s, "csrf": s.csrf_token, "days": days, "kind": kind, "filters": list(HISTORY_FILTERS),
            "older": rows[-1]["id"] if more and rows else None,
        })

    # ------------------------------------------------------------ settings

    def settings_ctx(s: Session, **extra) -> dict[str, Any]:
        row = user_row(s.user_id)
        expiry = db.get_setting("ytm_cookies_expiry")
        ctx = {
            "s": s, "csrf": s.csrf_token, "user": row,
            "has_cookies": db.get_setting("ytm_cookies_enc") is not None,
            "cookies_saved_at": float(db.get_setting("ytm_cookies_saved_at") or 0) or None,
            "cookies_expiry": float(expiry) if expiry else None,
            "ytm_status": db.get_setting("ytm_status"),
            "other_sessions": sessions.count_for_user(s.user_id) - 1,
            "sync": {k: get_option(db, k) for k in DEFAULTS},
            "push_subs": notifier.subscriptions(),
            "notify_quiet": db.get_setting("notify_quiet_unchanged") == "1",
            "qualities": list(MP3_QUALITIES),
        }
        ctx.update(extra)
        return ctx

    @app.get("/settings")
    def settings_page(request: Request):
        s = require(request)
        return render(request, "settings.html", settings_ctx(s))

    @app.post("/settings/youtube")
    async def settings_youtube(
        request: Request,
        csrf_token: str = Form(""),
        cookies_text: str = Form(""),
        cookies_file: UploadFile | None = File(None),
    ):
        s = require(request)
        require_csrf(request, s, csrf_token)
        text = cookies_text
        if cookies_file is not None and cookies_file.filename:
            raw = await cookies_file.read(MAX_COOKIE_UPLOAD + 1)
            if len(raw) > MAX_COOKIE_UPLOAD:
                return render(request, "settings.html", settings_ctx(s, yt_error="That file is too large."), 400)
            text = raw.decode("utf-8", errors="replace")
        try:
            parsed = parse_cookies(text)
        except CookieError as exc:
            return render(request, "settings.html", settings_ctx(s, yt_error=str(exc)), 400)

        previous = db.get_setting("ytm_cookies_enc")
        db.set_setting("ytm_cookies_enc", vault.encrypt(parsed.netscape))
        from starlette.concurrency import run_in_threadpool
        try:
            await run_in_threadpool(do_refresh)
        except CookieError as exc:
            db.set_setting("ytm_cookies_enc", previous)
            return render(request, "settings.html", settings_ctx(s, yt_error=str(exc)), 400)
        db.set_setting("ytm_cookies_saved_at", str(time.time()))
        db.set_setting("ytm_cookies_expiry", str(parsed.earliest_expiry) if parsed.earliest_expiry else None)
        db.log_event("security", "YouTube Music cookies updated")
        return RedirectResponse("/settings?msg=cookies_saved", status_code=303)

    @app.post("/settings/youtube/remove")
    def settings_youtube_remove(request: Request, csrf_token: str = Form("")):
        s = require(request)
        require_csrf(request, s, csrf_token)
        for key in ("ytm_cookies_enc", "ytm_cookies_saved_at", "ytm_cookies_expiry", "ytm_status"):
            db.set_setting(key, None)
        db.log_event("security", "YouTube Music cookies removed")
        return RedirectResponse("/settings?msg=cookies_removed", status_code=303)

    @app.post("/settings/password")
    def settings_password(
        request: Request,
        csrf_token: str = Form(""),
        current: str = Form(""),
        new: str = Form(""),
        confirm: str = Form(""),
    ):
        s = require(request)
        require_csrf(request, s, csrf_token)
        row = user_row(s.user_id)
        if not security.verify_password(row["password_hash"], current[:1024]):
            return render(request, "settings.html", settings_ctx(s, pw_error="Current password is incorrect."), 400)
        if error := security.password_problem(new, confirm):
            return render(request, "settings.html", settings_ctx(s, pw_error=error), 400)
        db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (security.hash_password(new), s.user_id))
        sessions.delete_all_for_user(s.user_id, except_hash=s.token_hash)
        db.log_event("security", "Password changed")
        return RedirectResponse("/settings?msg=password_changed", status_code=303)

    @app.post("/settings/2fa/start")
    def settings_2fa_start(request: Request, csrf_token: str = Form("")):
        s = require(request)
        require_csrf(request, s, csrf_token)
        secret = security.new_totp_secret()
        db.set_setting(f"totp_pending_{s.user_id}", vault.encrypt(secret))
        uri = security.totp_uri(secret, s.username)
        qr = segno.make(uri, error="m").svg_inline(scale=5, dark="#111", light="#fff")
        return render(request, "settings.html", settings_ctx(s, totp_setup={"secret": secret, "qr": qr}))

    @app.post("/settings/2fa/confirm")
    def settings_2fa_confirm(request: Request, csrf_token: str = Form(""), code: str = Form("")):
        s = require(request)
        require_csrf(request, s, csrf_token)
        enc = db.get_setting(f"totp_pending_{s.user_id}")
        if not enc:
            return RedirectResponse("/settings", status_code=303)
        secret = vault.decrypt(enc)
        step = security.verify_totp(secret, code, 0)
        if step is None:
            uri = security.totp_uri(secret, s.username)
            qr = segno.make(uri, error="m").svg_inline(scale=5, dark="#111", light="#fff")
            return render(request, "settings.html", settings_ctx(
                s, totp_setup={"secret": secret, "qr": qr, "error": "That code didn't work. Try the next one."}), 400)
        db.execute(
            "UPDATE users SET totp_secret_enc = ?, totp_enabled = 1, totp_last_step = ? WHERE id = ?",
            (enc, step, s.user_id),
        )
        db.set_setting(f"totp_pending_{s.user_id}", None)
        db.log_event("security", "Two-factor authentication turned on")
        return RedirectResponse("/settings?msg=2fa_enabled", status_code=303)

    @app.post("/settings/2fa/disable")
    def settings_2fa_disable(request: Request, csrf_token: str = Form(""), password: str = Form(""),
                             code: str = Form("")):
        s = require(request)
        require_csrf(request, s, csrf_token)
        row = user_row(s.user_id)
        ok = security.verify_password(row["password_hash"], password[:1024])
        step = ok and row["totp_enabled"] and security.verify_totp(
            vault.decrypt(row["totp_secret_enc"]), code, row["totp_last_step"])
        if not step:
            return render(request, "settings.html", settings_ctx(
                s, totp_error="Enter your password and a current code to turn this off."), 400)
        db.execute("UPDATE users SET totp_secret_enc = NULL, totp_enabled = 0, totp_last_step = 0 WHERE id = ?",
                   (s.user_id,))
        db.log_event("security", "Two-factor authentication turned off")
        return RedirectResponse("/settings?msg=2fa_disabled", status_code=303)

    @app.post("/settings/sessions/revoke")
    def settings_revoke(request: Request, csrf_token: str = Form("")):
        s = require(request)
        require_csrf(request, s, csrf_token)
        sessions.delete_all_for_user(s.user_id, except_hash=s.token_hash)
        db.log_event("security", "Signed out all other sessions")
        return RedirectResponse("/settings?msg=sessions_revoked", status_code=303)

    return app


def _ago(ts: float | None) -> str:
    if not ts:
        return "never"
    delta = max(0, time.time() - ts)
    for size, unit in ((86400, "day"), (3600, "hour"), (60, "minute")):
        if delta >= size:
            n = int(delta // size)
            return f"{n} {unit}{'s' if n != 1 else ''} ago"
    return "just now"


HISTORY_PAGE = 150
HISTORY_FILTERS = {
    "all": [],
    "syncs": ["sync"],
    "library": ["library"],
    "tracks": ["tracks", "download"],
    "removals": ["removal"],
    "selection": ["selection"],
    "account": ["security", "settings"],
}


def _day_label(ts: float) -> str:
    day = time.localtime(ts)
    today = time.localtime()
    yesterday = time.localtime(time.time() - 86400)
    if day[:3] == today[:3]:
        return "Today"
    if day[:3] == yesterday[:3]:
        return "Yesterday"
    return time.strftime("%A, %B %-d, %Y", day)


def _event_view(row) -> dict[str, Any]:
    """Turn an event row into something the history template can show, with
    structured details for sync runs and plain lists for everything else."""
    view = {"ts": row["ts"], "kind": row["kind"], "message": row["message"], "sections": [], "detail": None}
    raw = row["details"]
    if not raw:
        return view
    try:
        data = json.loads(raw)
    except ValueError:
        view["detail"] = raw
        return view
    if row["kind"] == "sync" and isinstance(data, dict):
        summ = SyncSummary.from_json(raw)
        if summ:
            sections = []
            for title, tracks in summ.tracks_new.items():
                sections.append((f"New in {title}", tracks))
            for title, tracks in summ.tracks_dropped.items():
                sections.append((f"No longer in {title}", tracks))
            if summ.downloaded_titles:
                sections.append(("Downloaded", summ.downloaded_titles))
            if summ.library_new:
                sections.append(("New in your library", summ.library_new))
            if summ.library_gone:
                sections.append(("Left your library", summ.library_gone))
            if summ.removals_queued:
                sections.append(("Waiting for your OK to remove", summ.removals_queued))
            if summ.failed:
                sections.append(("Failed downloads", summ.failed))
            if summ.errors:
                sections.append(("Errors", summ.errors))
            view["sections"] = sections
    elif isinstance(data, list):
        view["sections"] = [("Items", [str(x) for x in data])]
    elif isinstance(data, dict):
        view["detail"] = ", ".join(f"{k}: {v}" for k, v in data.items())
    return view


def _device_label(ua: str) -> str:
    browser = next((b for b in ("Edg", "Firefox", "Chrome", "Safari") if b in ua), "Browser")
    browser = {"Edg": "Edge"}.get(browser, browser)
    system = next((o for o in ("Android", "iPhone", "iPad", "Windows", "Mac OS", "Linux") if o in ua), "")
    system = {"Mac OS": "macOS"}.get(system, system)
    return f"{browser} on {system}" if system else browser


def _until(ts: float | None) -> str | None:
    if ts is None:
        return None
    delta = ts - time.time()
    if delta <= 60:
        return "any moment"
    hours, rem = divmod(int(delta), 3600)
    return f"in {hours}h {rem // 60}m" if hours else f"in {rem // 60}m"


def get_app() -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return create_app()

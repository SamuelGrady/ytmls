import time

import pyotp

from .conftest import PASSWORD, csrf_from, do_setup, pre_token


def test_protected_pages_redirect_to_setup_then_login(client, app):
    assert client.get("/", follow_redirects=False).headers["location"] == "/setup"
    do_setup(client, app)
    client.cookies.clear()
    assert client.get("/", follow_redirects=False).headers["location"] == "/login"
    assert client.get("/settings", follow_redirects=False).status_code == 303


def test_setup_requires_token(client, app):
    pre = pre_token(client, "/setup")
    r = client.post("/setup", data={"pre": pre, "setup_token": "wrong", "username": "admin",
                                    "password": PASSWORD, "confirm": PASSWORD})
    assert r.status_code == 400
    assert "Setup token is incorrect" in r.text
    assert app.state.db.one("SELECT COUNT(*) AS n FROM users")["n"] == 0


def test_setup_rejects_short_password(client, app):
    pre = pre_token(client, "/setup")
    r = client.post("/setup", data={"pre": pre, "setup_token": app.state.setup_token, "username": "admin",
                                    "password": "short", "confirm": "short"})
    assert r.status_code == 400


def test_setup_only_once(client, app):
    do_setup(client, app)
    client.cookies.clear()
    assert client.get("/setup", follow_redirects=False).headers["location"] == "/login"
    assert app.state.setup_token is None


def test_login_and_logout(client, app):
    do_setup(client, app)
    client.cookies.clear()
    pre = pre_token(client, "/login")
    r = client.post("/login", data={"pre": pre, "username": "admin", "password": PASSWORD}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    page = client.get("/")
    assert page.status_code == 200
    r = client.post("/logout", data={"csrf_token": csrf_from(page.text)}, follow_redirects=False)
    assert r.headers["location"] == "/login"
    assert client.get("/", follow_redirects=False).status_code == 303


def test_login_without_pre_csrf_is_forbidden(client, app):
    do_setup(client, app)
    client.cookies.clear()
    r = client.post("/login", data={"pre": "x", "username": "admin", "password": PASSWORD})
    assert r.status_code == 403


def test_session_cookie_flags(client, app, settings):
    r = do_setup(client, app)
    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie.replace("Strict", "strict")


def test_ip_lockout_after_failures(client, app):
    do_setup(client, app)
    client.cookies.clear()
    for _ in range(5):
        pre = pre_token(client, "/login")
        r = client.post("/login", data={"pre": pre, "username": "admin", "password": "nope-nope-nope"})
        assert r.status_code == 401
    pre = pre_token(client, "/login")
    r = client.post("/login", data={"pre": pre, "username": "admin", "password": PASSWORD})
    assert r.status_code == 429
    assert "Too many failed attempts" in r.text


def test_proxy_header_ignored_unless_trusted(client, app):
    do_setup(client, app)
    client.cookies.clear()
    for i in range(5):
        pre = pre_token(client, "/login")
        client.post("/login", data={"pre": pre, "username": "admin", "password": "bad-bad-bad"},
                    headers={"CF-Connecting-IP": f"10.0.0.{i}"})
    pre = pre_token(client, "/login")
    r = client.post("/login", data={"pre": pre, "username": "admin", "password": PASSWORD},
                    headers={"CF-Connecting-IP": "10.0.0.99"})
    assert r.status_code == 429  # spoofed header did not dodge the lockout


def test_csrf_required_on_settings(authed):
    r = authed.post("/settings/sessions/revoke", data={"csrf_token": "wrong"})
    assert r.status_code == 403


def test_cross_origin_post_rejected(authed):
    csrf = csrf_from(authed.get("/settings").text)
    r = authed.post("/settings/sessions/revoke", data={"csrf_token": csrf},
                    headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_security_headers(client):
    r = client.get("/login", follow_redirects=True)
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["cache-control"] == "no-store"


def test_api_docs_disabled(client):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


def test_totp_enrolment_and_login(authed, app):
    csrf = csrf_from(authed.get("/settings").text)
    r = authed.post("/settings/2fa/start", data={"csrf_token": csrf})
    assert "<svg" in r.text
    import re
    secret = re.search(r"Enter this key: <code>([A-Z2-7]+)</code>", r.text).group(1)
    r = authed.post("/settings/2fa/confirm", data={"csrf_token": csrf, "code": pyotp.TOTP(secret).now()},
                    follow_redirects=False)
    assert r.headers["location"] == "/settings?msg=2fa_enabled"

    authed.cookies.clear()
    pre = pre_token(authed, "/login")
    r = authed.post("/login", data={"pre": pre, "username": "admin", "password": PASSWORD}, follow_redirects=False)
    assert r.headers["location"] == "/login/2fa"
    # Password alone does not grant access.
    assert authed.get("/", follow_redirects=False).status_code == 303
    page = authed.get("/login/2fa")
    tcsrf = csrf_from(page.text)
    r = authed.post("/login/2fa", data={"csrf_token": tcsrf, "code": "000000"})
    assert r.status_code == 401
    # The code used at enrolment was consumed, so use the next time step.
    code = pyotp.TOTP(secret).at(time.time() + 30)
    r = authed.post("/login/2fa", data={"csrf_token": tcsrf, "code": code}, follow_redirects=False)
    assert r.headers["location"] == "/"
    assert authed.get("/").status_code == 200


def test_totp_replay_rejected():
    from app.security import verify_totp
    secret = pyotp.random_base32()
    now = time.time()
    code = pyotp.TOTP(secret).at(now)
    step = verify_totp(secret, code, 0, now)
    assert step is not None
    assert verify_totp(secret, code, step, now) is None


def test_password_change_revokes_other_sessions(authed, app):
    from fastapi.testclient import TestClient
    other = TestClient(app)
    pre = pre_token(other, "/login")
    other.post("/login", data={"pre": pre, "username": "admin", "password": PASSWORD})
    assert other.get("/", follow_redirects=False).status_code == 200

    csrf = csrf_from(authed.get("/settings").text)
    new = "an even better passphrase"
    r = authed.post("/settings/password", data={"csrf_token": csrf, "current": PASSWORD, "new": new, "confirm": new},
                    follow_redirects=False)
    assert r.status_code == 303
    assert other.get("/", follow_redirects=False).status_code == 303
    assert authed.get("/", follow_redirects=False).status_code == 200

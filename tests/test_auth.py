"""SSO, session and CSRF tests with a fake CAS / OAuth identity provider."""

from __future__ import annotations

from urllib.parse import parse_qsl, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from seatlab.api.app import create_app
from seatlab.auth import AuthConfig

CAS_OK = """<?xml version="1.0" encoding="UTF-8"?>
<cas:serviceResponse xmlns:cas="http://www.yale.edu/tp/cas">
  <cas:authenticationSuccess>
    <cas:user>{user}</cas:user>
    <cas:attributes>
      <cas:displayName>{name}</cas:displayName>
      <cas:mail>{email}</cas:mail>
      <cas:role>{role}</cas:role>
    </cas:attributes>
  </cas:authenticationSuccess>
</cas:serviceResponse>"""

CAS_FAIL = """<?xml version="1.0" encoding="UTF-8"?>
<cas:serviceResponse xmlns:cas="http://www.yale.edu/tp/cas">
  <cas:authenticationFailure code="INVALID_TICKET">Ticket ST-bad not recognised</cas:authenticationFailure>
</cas:serviceResponse>"""


def cas_config(**overrides) -> AuthConfig:
    base = dict(
        mode="cas",
        trust_header=False,
        public_url="https://lib.example.edu",
        session_secret="test-secret-" * 4,
        cas_base="https://cas.example.edu/cas",
        cas_validate="https://cas.example.edu/cas/serviceValidate",
        cas_service="https://lib.example.edu/auth/callback",
        cookie_secure=False,
        admin_ids=("20241009",),
    )
    base.update(overrides)
    return AuthConfig(**base)


def cas_client(user="20241001", name="王小明", role="student") -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/serviceValidate")
        ticket = request.url.params.get("ticket")
        if ticket == "ST-good":
            return httpx.Response(
                200,
                text=CAS_OK.format(user=user, name=name, email="student@example.edu", role=role),
            )
        return httpx.Response(200, text=CAS_FAIL)

    return httpx.Client(transport=httpx.MockTransport(handler))


def oauth_config(**overrides) -> AuthConfig:
    base = dict(
        mode="oauth",
        trust_header=False,
        public_url="https://lib.example.edu",
        session_secret="test-secret-" * 4,
        oauth_authorize="https://idp.example.edu/oauth/authorize",
        oauth_token="https://idp.example.edu/oauth/token",
        oauth_userinfo="https://idp.example.edu/oauth/userinfo",
        oauth_client_id="seatlab",
        oauth_client_secret="client-secret",
        cookie_secure=False,
        admin_roles=("admin", "librarian"),
    )
    base.update(overrides)
    return AuthConfig(**base)


def oauth_client(claims: dict) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            body = dict(parse_qsl(request.content.decode()))
            assert body.get("grant_type") == "authorization_code"
            return httpx.Response(200, json={"access_token": "at-123", "token_type": "Bearer"})
        if request.url.path.endswith("/userinfo"):
            assert request.headers.get("Authorization") == "Bearer at-123"
            return httpx.Response(200, json=claims)
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler))


def start_cas_login(
    tc: TestClient, next_path: str = "/app", expect_relay_state: bool = True
) -> str:
    response = tc.get(f"/auth/login?next={next_path}")
    assert response.status_code == 302
    location = response.headers["location"]
    assert "cas.example.edu/cas/login" in location
    assert "service=https%3A%2F%2Flib.example.edu%2Fauth%2Fcallback" in location
    cookie = response.cookies["seatlab_auth_state"]
    state = cookie.split(".")[0]
    if expect_relay_state:
        assert f"RelayState={state}" in location
    else:
        assert "RelayState=" not in location
    return state


def test_cas_login_callback_and_csrf(tmp_path):
    app = create_app(
        db_path=str(tmp_path / "cas.db"),
        admin_key="admin-secret",
        auth_config=cas_config(),
        auth_client=cas_client(),
    )
    tc = TestClient(app, follow_redirects=False)
    state = start_cas_login(tc)
    callback = tc.get(f"/auth/callback?ticket=ST-good&state={state}")
    assert callback.status_code == 303
    assert callback.headers["location"] == "/app"
    me = tc.get("/auth/me").json()
    assert me["authenticated"] is True
    assert me["student_id"] == "20241001"
    assert me["name"] == "王小明"
    assert me["via"] == "cookie"
    assert me["csrf"]
    # Read endpoints work with the session cookie alone.
    assert tc.get("/api/v1/app/availability").status_code == 200
    assert tc.get("/api/v1/app/me").json()["student_id"] == "20241001"
    # Cookie-authenticated writes need the CSRF header.
    blocked = tc.post("/api/v1/app/reservations", json={"zone": "A"})
    assert blocked.status_code == 403
    assert "CSRF" in blocked.json()["detail"]
    ok = tc.post(
        "/api/v1/app/reservations",
        json={"zone": "A"},
        headers={"X-CSRF-Token": me["csrf"]},
    )
    assert ok.status_code == 200
    assert ok.json()["student_id"] == "20241001"
    # Bearer tokens are not CSRF-prone and skip the header.
    token_cookie = tc.cookies["seatlab_session"]
    bearer = tc.get("/api/v1/app/me", headers={"Authorization": f"Bearer {token_cookie}"})
    assert bearer.status_code == 200
    assert bearer.json()["student_id"] == "20241001"
    assert tc.post(
        "/api/v1/app/reservations",
        json={"zone": "B"},
        headers={"Authorization": f"Bearer {token_cookie}"},
    ).status_code in (200, 409)


def test_invalid_ticket_and_state_tampering_are_rejected(tmp_path):
    app = create_app(
        db_path=str(tmp_path / "cas.db"),
        auth_config=cas_config(admin_ids=()),
        auth_client=cas_client(),
    )
    tc = TestClient(app, follow_redirects=False)
    state = start_cas_login(tc)
    bad = tc.get(f"/auth/callback?ticket=ST-bad&state={state}")
    assert bad.status_code == 401
    assert "INVALID_TICKET" in bad.text
    # A fresh login is needed because the state cookie was consumed.
    tc.cookies.clear()
    start_cas_login(tc)
    tampered = tc.get("/auth/callback?ticket=ST-good&state=deadbeef")
    assert tampered.status_code == 400
    assert "mismatch" in tampered.text
    # A failed callback clears the state cookie, so start once more for the
    # open-redirect check. The post-login path is signed into the cookie, never
    # taken from a query param.
    state3 = start_cas_login(tc)
    assert (
        tc.get(f"/auth/callback?ticket=ST-good&state={state3}&next=//evil.example").status_code
        == 303
    )


def test_login_state_signature_and_expiry():
    cfg = cas_config()
    from seatlab.auth import AuthError, AuthService
    from seatlab.store import Store

    store = Store(":memory:")
    auth = AuthService(cfg, store)
    _, cookie_value = auth.login_target("https://lib.example.edu", "//evil.example")
    state = cookie_value.split(".")[0]
    assert auth.verify_state(state, cookie_value) == "/app"
    _, cookie_value = auth.login_target("https://lib.example.edu", "/app")
    state = cookie_value.split(".")[0]
    assert auth.verify_state(state, cookie_value) == "/app"
    with pytest.raises(AuthError):
        auth.verify_state(state, cookie_value + "x")
    with pytest.raises(AuthError):
        auth.verify_state("other", cookie_value)
    store.close()


def test_sso_mode_never_trusts_the_student_header(tmp_path):
    app = create_app(
        db_path=str(tmp_path / "cas.db"), auth_config=cas_config(), auth_client=cas_client()
    )
    tc = TestClient(app)
    assert tc.get("/api/v1/app/availability").status_code == 200
    assert tc.get("/auth/me", headers={"X-Student-ID": "20241001"}).json()["authenticated"] is False
    assert tc.get("/api/v1/app/me", headers={"X-Student-ID": "20241001"}).status_code == 401
    assert (
        tc.post(
            "/api/v1/app/reservations", json={"zone": "A"}, headers={"X-Student-ID": "20241001"}
        ).status_code
        == 401
    )


def test_oauth_admin_session_reaches_control_plane(tmp_path):
    claims = {
        "sub": "librarian-01",
        "name": "张老师",
        "email": "zhang@example.edu",
        "role": ["staff", "librarian"],
    }
    app = create_app(
        db_path=str(tmp_path / "oauth.db"),
        admin_key="break-glass-key",
        auth_config=oauth_config(),
        auth_client=oauth_client(claims),
    )
    tc = TestClient(app, follow_redirects=False)
    login = tc.get("/auth/login?next=/admin")
    assert login.status_code == 302
    params = dict(parse_qsl(urlsplit(login.headers["location"]).query))
    assert params["response_type"] == "code"
    assert params["client_id"] == "seatlab"
    state = params["state"]
    callback = tc.get(f"/auth/callback?code=abc&state={state}")
    assert callback.status_code == 303
    assert callback.headers["location"] == "/admin"
    me = tc.get("/auth/me").json()
    assert me["role"] == "admin"
    assert me["name"] == "张老师"
    # No X-Admin-Key, but the admin session is enough.
    assert tc.get("/api/v1/admin/dashboard").status_code == 200
    blocked = tc.patch("/api/v1/admin/venue", json={"name": "新馆名"})
    assert blocked.status_code == 403
    ok = tc.patch(
        "/api/v1/admin/venue",
        json={"name": "新馆名"},
        headers={"X-CSRF-Token": me["csrf"]},
    )
    assert ok.status_code == 200
    assert ok.json()["name"] == "新馆名"
    # The break-glass key still works without any session.
    tc.cookies.clear()
    assert (
        tc.get("/api/v1/admin/dashboard", headers={"X-Admin-Key": "break-glass-key"}).status_code
        == 200
    )


def test_plain_student_session_is_denied_the_control_plane(tmp_path):
    app = create_app(
        db_path=str(tmp_path / "oauth.db"),
        admin_key=None,
        auth_config=oauth_config(admin_roles=("librarian",)),
        auth_client=oauth_client({"sub": "20241001", "name": "学生"}),
    )
    tc = TestClient(app, follow_redirects=False)
    login = tc.get("/auth/login?next=/app")
    state = dict(parse_qsl(urlsplit(login.headers["location"]).query))["state"]
    tc.get(f"/auth/callback?code=abc&state={state}")
    assert tc.get("/api/v1/admin/dashboard").status_code == 401
    assert tc.get("/api/v1/admin/reservations").status_code == 401


def test_admin_id_attribute_promotes_a_cas_user(tmp_path):
    app = create_app(
        db_path=str(tmp_path / "cas.db"),
        admin_key=None,
        auth_config=cas_config(admin_ids=("20241009",)),
        auth_client=cas_client(user="20241009", name="馆员"),
    )
    tc = TestClient(app, follow_redirects=False)
    state = start_cas_login(tc, "/admin")
    tc.get(f"/auth/callback?ticket=ST-good&state={state}")
    assert tc.get("/api/v1/admin/dashboard").status_code == 200


def test_cas_without_relay_state_requires_explicit_opt_out(tmp_path):
    """Missing RelayState is a login-CSRF risk unless the operator opted out."""
    strict = create_app(
        db_path=str(tmp_path / "strict.db"),
        auth_config=cas_config(admin_ids=()),
        auth_client=cas_client(),
    )
    tc = TestClient(strict, follow_redirects=False)
    start_cas_login(tc)
    rejected = tc.get("/auth/callback?ticket=ST-good")
    assert rejected.status_code == 400
    assert tc.get("/auth/me").json()["authenticated"] is False

    loose = create_app(
        db_path=str(tmp_path / "loose.db"),
        auth_config=cas_config(admin_ids=(), cas_relay_state=False),
        auth_client=cas_client(),
    )
    tc2 = TestClient(loose, follow_redirects=False)
    start_cas_login(tc2, expect_relay_state=False)
    callback = tc2.get("/auth/callback?ticket=ST-good")
    assert callback.status_code == 303
    assert callback.headers["location"] == "/app"
    assert tc2.get("/auth/me").json()["student_id"] == "20241001"


def test_logout_revokes_the_session(tmp_path):
    app = create_app(
        db_path=str(tmp_path / "cas.db"),
        auth_config=cas_config(admin_ids=()),
        auth_client=cas_client(),
    )
    tc = TestClient(app, follow_redirects=False)
    state = start_cas_login(tc)
    tc.get(f"/auth/callback?ticket=ST-good&state={state}")
    assert tc.get("/auth/me").json()["authenticated"] is True
    assert tc.post("/auth/logout").status_code == 200
    assert tc.get("/auth/me").json()["authenticated"] is False
    assert tc.get("/api/v1/app/me").status_code == 401


def test_config_endpoint_hides_secrets_and_reports_mode(tmp_path):
    app = create_app(
        db_path=str(tmp_path / "cas.db"),
        auth_config=cas_config(),
        auth_client=cas_client(),
    )
    tc = TestClient(app)
    body = tc.get("/auth/config").json()
    assert body["mode"] == "cas"
    assert body["sso"] is True
    assert body["header_allowed"] is False
    assert "test-secret" not in str(body)
    app_cfg = tc.get("/api/v1/app/config").json()
    assert app_cfg["sso"] is True
    assert app_cfg["login_url"] == "/auth/login"


def test_header_dev_mode_still_works_without_sso(tmp_path):
    app = create_app(db_path=str(tmp_path / "dev.db"))
    tc = TestClient(app)
    assert tc.get("/auth/config").json()["mode"] == "header"
    assert tc.get("/auth/login").status_code == 404
    assert tc.get("/api/v1/app/me", headers={"X-Student-ID": "20241001"}).status_code == 200
    assert tc.get("/healthz").json()["storage"] == "sqlite"


def test_expired_session_is_rejected(tmp_path):
    from datetime import datetime, timedelta

    clock = datetime(2026, 11, 21, 9, 0, 0)
    app = create_app(
        db_path=str(tmp_path / "exp.db"),
        auth_config=cas_config(),
        auth_client=cas_client(),
        now=lambda: clock,
    )
    tc = TestClient(app, follow_redirects=False)
    state = start_cas_login(tc)
    tc.get(f"/auth/callback?ticket=ST-good&state={state}")
    assert tc.get("/auth/me").json()["authenticated"] is True
    # The auth service and HTTP layer share the injected clock; jump 13 hours.
    app.state.auth._now = lambda: clock + timedelta(hours=13)
    assert tc.get("/auth/me").json()["authenticated"] is False

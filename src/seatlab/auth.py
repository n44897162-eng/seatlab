"""Campus single sign-on for SeatLab.

Three authentication modes are supported, chosen with ``SEATLAB_AUTH_MODE``:

``header`` (default, development)
    Trust the ``X-Student-ID`` request header. This is what the bundled pages
    and the API examples use when SeatLab runs inside a trusted network or
    behind an authenticating reverse proxy. **Never expose this mode to the
    public internet.**

``cas`` (Apereo CAS 2.0/3.0 protocol)
    ``GET /auth/login`` redirects to ``/cas/login?service=...``; the callback
    validates the service ticket against ``/cas/serviceValidate`` and reads the
    student id from the configured attribute.

``oauth`` (OAuth 2.0 authorization-code / OIDC)
    Works with Keycloak, Authing, 微信企业号网关, 统一身份认证网关 and most
    campus IdPs. The callback exchanges the code at the token endpoint, reads
    userinfo, and maps claims to a student id and an optional admin role.

Sessions are opaque random tokens stored **hashed** in the database, so they
survive restarts, work across uvicorn workers and can be revoked centrally.
Cookie-authenticated writes require an ``X-CSRF-Token`` header; the token is
returned by ``GET /auth/me`` and stored in memory by the pages.

The login ``state`` is double-submitted: a signed, short-lived cookie holds the
state and the post-login path, and the IdP echoes the state in the query string.
A tampered or replayed callback is rejected before any ticket is validated.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import html
import os
import re
import secrets
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx

from .store import Store


class AuthError(Exception):
    """An authentication failure with an HTTP status code."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass(frozen=True)
class AuthConfig:
    """Runtime configuration, read from the environment at process start."""

    mode: str = "header"
    trust_header: bool = True
    public_url: str = ""
    session_secret: str = ""
    session_ttl_hours: float = 12.0
    cookie_name: str = "seatlab_session"
    cookie_secure: bool | None = None
    id_pattern: str = r"^[A-Za-z0-9][A-Za-z0-9_.@-]{2,39}$"
    admin_ids: tuple[str, ...] = ()
    admin_roles: tuple[str, ...] = ("admin", "librarian", "staff")
    admin_sso: bool = True
    # CAS
    cas_base: str = ""
    cas_service: str = ""
    cas_validate: str = ""
    cas_relay_state: bool = True
    cas_id_attr: str = "user"
    cas_name_attr: str = "displayName"
    cas_email_attr: str = "mail"
    # OAuth / OIDC
    oauth_authorize: str = ""
    oauth_token: str = ""
    oauth_userinfo: str = ""
    oauth_client_id: str = ""
    oauth_client_secret: str = ""
    oauth_scope: str = "openid profile email"
    oauth_id_field: str = "sub"
    oauth_name_field: str = "name"
    oauth_email_field: str = "email"
    oauth_role_field: str = "role"
    oauth_groups_field: str = "groups"
    oauth_extra_authorize: dict[str, str] = field(default_factory=dict)

    @property
    def is_sso(self) -> bool:
        return self.mode in ("cas", "oauth")

    @property
    def callback_path(self) -> str:
        return "/auth/callback"

    @classmethod
    def from_env(cls) -> AuthConfig:
        env = os.environ

        def text(key: str, default: str = "") -> str:
            return (env.get(key) or default).strip()

        def flag(key: str, default: bool) -> bool:
            raw = env.get(key)
            if raw is None or raw == "":
                return default
            return raw.strip().lower() in ("1", "true", "yes", "on")

        mode = text("SEATLAB_AUTH_MODE", "header").lower()
        if mode not in ("header", "cas", "oauth"):
            raise RuntimeError("SEATLAB_AUTH_MODE must be header, cas or oauth")
        public_url = text("SEATLAB_PUBLIC_URL").rstrip("/")
        cookie_secure: bool | None
        if env.get("SEATLAB_COOKIE_SECURE"):
            cookie_secure = flag("SEATLAB_COOKIE_SECURE", True)
        elif public_url:
            cookie_secure = public_url.startswith("https://")
        else:
            cookie_secure = None
        cas_base = text("SEATLAB_CAS_BASE").rstrip("/")
        service = text("SEATLAB_CAS_SERVICE")
        if not service and public_url:
            service = public_url + "/auth/callback"
        validate = text("SEATLAB_CAS_VALIDATE")
        if not validate and cas_base:
            validate = cas_base + "/serviceValidate"
        admin_ids = tuple(x.strip() for x in text("SEATLAB_ADMIN_IDS").split(",") if x.strip())
        admin_roles = tuple(
            x.strip().lower()
            for x in text("SEATLAB_ADMIN_ROLES", "admin,librarian,staff").split(",")
            if x.strip()
        )
        secret = text("SEATLAB_SESSION_SECRET")
        if mode in ("cas", "oauth") and not secret:
            raise RuntimeError(
                "SEATLAB_SESSION_SECRET is required in cas/oauth mode "
                "(generate one with: python -c 'import secrets;print(secrets.token_urlsafe(48))')"
            )
        if mode == "cas" and not cas_base:
            raise RuntimeError(
                "SEATLAB_CAS_BASE is required in cas mode (e.g. https://cas.example.edu/cas)"
            )
        if mode == "oauth" and not (
            text("SEATLAB_OAUTH_AUTHORIZE") and text("SEATLAB_OAUTH_TOKEN")
        ):
            raise RuntimeError(
                "SEATLAB_OAUTH_AUTHORIZE and SEATLAB_OAUTH_TOKEN are required in oauth mode"
            )
        return cls(
            mode=mode,
            trust_header=flag("SEATLAB_TRUST_HEADER", mode == "header"),
            public_url=public_url,
            session_secret=secret,
            session_ttl_hours=float(text("SEATLAB_SESSION_TTL_H", "12") or 12),
            cookie_name=text("SEATLAB_COOKIE_NAME", "seatlab_session"),
            cookie_secure=cookie_secure,
            id_pattern=text("SEATLAB_ID_PATTERN", r"^[A-Za-z0-9][A-Za-z0-9_.@-]{2,39}$"),
            admin_ids=admin_ids,
            admin_roles=admin_roles,
            admin_sso=flag("SEATLAB_ADMIN_SSO", True),
            cas_base=cas_base,
            cas_service=service,
            cas_validate=validate,
            cas_relay_state=flag("SEATLAB_CAS_RELAY_STATE", True),
            cas_id_attr=text("SEATLAB_CAS_ID_ATTR", "user"),
            cas_name_attr=text("SEATLAB_CAS_NAME_ATTR", "displayName"),
            cas_email_attr=text("SEATLAB_CAS_EMAIL_ATTR", "mail"),
            oauth_authorize=text("SEATLAB_OAUTH_AUTHORIZE"),
            oauth_token=text("SEATLAB_OAUTH_TOKEN"),
            oauth_userinfo=text("SEATLAB_OAUTH_USERINFO"),
            oauth_client_id=text("SEATLAB_OAUTH_CLIENT_ID"),
            oauth_client_secret=text("SEATLAB_OAUTH_CLIENT_SECRET"),
            oauth_scope=text("SEATLAB_OAUTH_SCOPE", "openid profile email"),
            oauth_id_field=text("SEATLAB_OAUTH_ID_FIELD", "sub"),
            oauth_name_field=text("SEATLAB_OAUTH_NAME_FIELD", "name"),
            oauth_email_field=text("SEATLAB_OAUTH_EMAIL_FIELD", "email"),
            oauth_role_field=text("SEATLAB_OAUTH_ROLE_FIELD", "role"),
            oauth_groups_field=text("SEATLAB_OAUTH_GROUPS_FIELD", "groups"),
        )

    def describe(self) -> dict[str, Any]:
        """Non-secret configuration for ``/auth/config`` and the CLI."""
        return {
            "mode": self.mode,
            "trust_header": self.trust_header,
            "public_url": self.public_url,
            "cookie_secure": self.cookie_secure,
            "session_ttl_hours": self.session_ttl_hours,
            "admin_sso": self.admin_sso,
            "admin_ids": len(self.admin_ids),
            "cas": {"base": self.cas_base, "validate": self.cas_validate}
            if self.mode == "cas"
            else None,
            "oauth": {
                "authorize": self.oauth_authorize,
                "userinfo": self.oauth_userinfo,
                "client_id": self.oauth_client_id,
            }
            if self.mode == "oauth"
            else None,
        }


@dataclass(frozen=True)
class Identity:
    student_id: str
    name: str = ""
    email: str = ""
    role: str = "student"
    claims: dict[str, Any] = field(default_factory=dict, compare=False)


@dataclass(frozen=True)
class Session:
    token_hash: str
    student_id: str
    role: str
    name: str
    email: str
    csrf: str
    expires_at: str
    via: str  # "cookie" or "bearer"

    def public(self) -> dict[str, Any]:
        return {
            "student_id": self.student_id,
            "role": self.role,
            "name": self.name,
            "email": self.email,
            "csrf": self.csrf,
            "expires_at": self.expires_at,
            "via": self.via,
        }


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _b64(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")


def _unb64(value: str) -> str:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding).decode("utf-8")


def _safe_next(path: str | None) -> str:
    """Only allow same-site relative paths, never protocol-relative or absolute."""
    if not path or not path.startswith("/") or path.startswith("//") or "\\" in path:
        return "/app"
    parsed = urlparse(path)
    if parsed.scheme or parsed.netloc:
        return "/app"
    return path[:300]


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


class AuthService:
    """SSO flows + database-backed sessions.

    ``client`` is an ``httpx.Client``-compatible object; tests inject one backed
    by ``httpx.MockTransport`` instead of standing up a real IdP.
    """

    def __init__(
        self,
        config: AuthConfig,
        store: Store,
        *,
        client: httpx.Client | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self._client = client or httpx.Client(timeout=8.0, follow_redirects=False)
        self._now = now or datetime.now

    # ------------------------------------------------------------------ login
    def login_target(self, public_base: str, next_path: str | None) -> tuple[str, str]:
        """Return ``(redirect_url, state_cookie_value)``.

        The cookie value is signed and expires in ten minutes; the redirect
        carries only the random state, which the IdP echoes back.
        """
        state = secrets.token_urlsafe(24)
        next_path = _safe_next(next_path)
        expires = int(time.time()) + 600
        payload = f"{state}|{_b64(next_path)}|{expires}"
        sig = hmac.new(
            self.config.session_secret.encode(), payload.encode(), hashlib.sha256
        ).hexdigest()
        cookie_value = f"{state}.{_b64(next_path)}.{expires}.{sig}"
        base = (public_base or self.config.public_url).rstrip("/")
        callback = base + self.config.callback_path
        if self.config.mode == "cas":
            query = {"service": self.config.cas_service or callback}
            if self.config.cas_relay_state:
                # Apereo CAS echoes RelayState; some older servers ignore it, in
                # which case the signed state cookie alone is verified at the callback.
                query["RelayState"] = state
            url = f"{self.config.cas_base}/login?{urlencode(query)}"
        elif self.config.mode == "oauth":
            params = {
                "response_type": "code",
                "client_id": self.config.oauth_client_id,
                "redirect_uri": callback,
                "scope": self.config.oauth_scope,
                "state": state,
            }
            params.update(self.config.oauth_extra_authorize)
            url = f"{self.config.oauth_authorize}?{urlencode(params)}"
        else:
            raise AuthError(404, "single sign-on is not enabled")
        return url, cookie_value

    def verify_state(self, state: str, cookie_value: str | None) -> str:
        """Validate the double-submitted state and return the post-login path."""
        if not state or not cookie_value:
            raise AuthError(400, "login state missing; start again at /auth/login")
        try:
            parts = cookie_value.split(".")
            if len(parts) != 4:
                raise AuthError(400, "login state malformed")
            cookie_state, next_b64, expires_s, sig = parts
            if not cookie_state or not expires_s.isdigit():
                raise AuthError(400, "login state malformed")
            if not hmac.compare_digest(state, cookie_state):
                raise AuthError(400, "login state mismatch")
            payload = f"{cookie_state}|{next_b64}|{expires_s}"
            expected = hmac.new(
                self.config.session_secret.encode(), payload.encode(), hashlib.sha256
            ).hexdigest()
            if not hmac.compare_digest(sig, expected):
                raise AuthError(400, "login state signature invalid")
            if int(expires_s) < int(time.time()):
                raise AuthError(400, "login state expired; start again at /auth/login")
            return _safe_next(_unb64(next_b64))
        except AuthError:
            raise
        except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
            raise AuthError(400, "login state malformed") from exc

    # ------------------------------------------------------------------- CAS
    def cas_identity(self, ticket: str, service_url: str) -> Identity:
        if not ticket:
            raise AuthError(400, "CAS ticket missing")
        params = {"service": service_url, "ticket": ticket}
        try:
            response = self._client.get(self.config.cas_validate, params=params)
        except httpx.HTTPError as exc:
            raise AuthError(502, f"CAS validation endpoint unreachable: {exc}") from exc
        if response.status_code != 200:
            raise AuthError(502, f"CAS validation returned HTTP {response.status_code}")
        body = response.text
        if len(body) > 512_000:
            raise AuthError(502, "CAS validation response too large")
        try:
            root = ET.fromstring(body)
        except ET.ParseError as exc:
            raise AuthError(502, "CAS validation returned invalid XML") from exc
        success = next((e for e in root.iter() if _local(e.tag) == "authenticationSuccess"), None)
        if success is None:
            failure = next(
                (e for e in root.iter() if _local(e.tag) == "authenticationFailure"), None
            )
            if failure is not None:
                code = failure.get("code") or "CAS_FAILURE"
                message = (failure.text or "").strip()
                detail = f"{code}: {message}"[:220]
            else:
                detail = "authentication failed"
            raise AuthError(401, f"CAS rejected the ticket ({detail})")
        user = next(
            (e.text.strip() for e in success.iter() if _local(e.tag) == "user" and e.text), ""
        )
        if not user:
            raise AuthError(502, "CAS response did not contain a user")
        attributes: dict[str, Any] = {}
        for element in success.iter():
            for child in list(element):
                name = _local(child.tag)
                text = (child.text or "").strip()
                if name and text:
                    if name in attributes:
                        old = attributes[name]
                        attributes[name] = old + [text] if isinstance(old, list) else [old, text]
                    else:
                        attributes[name] = text
        name = str(
            attributes.get(self.config.cas_name_attr) or attributes.get("displayName") or user
        )
        email = str(attributes.get(self.config.cas_email_attr) or attributes.get("mail") or "")
        identity_id = str(attributes.get(self.config.cas_id_attr) or user)
        role = self.role_for(identity_id, attributes)
        self._check_id(identity_id)
        return Identity(identity_id, name, email, role, claims=attributes)

    # ----------------------------------------------------------------- OAuth
    def oauth_identity(self, code: str, redirect_uri: str) -> Identity:
        if not code:
            raise AuthError(400, "OAuth code missing")
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": self.config.oauth_client_id,
            "client_secret": self.config.oauth_client_secret,
        }
        try:
            token_response = self._client.post(self.config.oauth_token, data=data)
        except httpx.HTTPError as exc:
            raise AuthError(502, f"OAuth token endpoint unreachable: {exc}") from exc
        if token_response.status_code != 200:
            raise AuthError(401, "OAuth code exchange failed")
        try:
            tokens = token_response.json()
        except ValueError as exc:
            raise AuthError(502, "OAuth token endpoint returned non-JSON") from exc
        access_token = tokens.get("access_token")
        if not access_token:
            raise AuthError(401, "OAuth token response had no access_token")
        if not self.config.oauth_userinfo:
            claims = tokens.get("id_token_claims") or {}
            if not isinstance(claims, dict) or not claims:
                raise AuthError(502, "SEATLAB_OAUTH_USERINFO is unset and the token had no claims")
        else:
            try:
                info = self._client.get(
                    self.config.oauth_userinfo,
                    headers={"Authorization": f"Bearer {access_token}"},
                )
            except httpx.HTTPError as exc:
                raise AuthError(502, f"OAuth userinfo endpoint unreachable: {exc}") from exc
            if info.status_code != 200:
                raise AuthError(502, f"OAuth userinfo returned HTTP {info.status_code}")
            try:
                claims = info.json()
            except ValueError as exc:
                raise AuthError(502, "OAuth userinfo returned non-JSON") from exc
            if not isinstance(claims, dict):
                raise AuthError(502, "OAuth userinfo must be a JSON object")
        identity_id = str(claims.get(self.config.oauth_id_field) or "")
        if not identity_id:
            raise AuthError(502, f"userinfo has no '{self.config.oauth_id_field}' claim")
        name = str(claims.get(self.config.oauth_name_field) or "")
        email = str(claims.get(self.config.oauth_email_field) or "")
        role = self.role_for(identity_id, claims)
        self._check_id(identity_id)
        return Identity(identity_id, name, email, role, claims=claims)

    # ---------------------------------------------------------- roles & ids
    def role_for(self, identity_id: str, claims: dict[str, Any]) -> str:
        if identity_id in self.config.admin_ids:
            return "admin"
        raw_roles = [claims.get(self.config.oauth_role_field)]
        raw_groups = [claims.get(self.config.oauth_groups_field)]
        for raw in raw_roles + raw_groups:
            values: list[str] = []
            if isinstance(raw, str):
                values = [x.strip().lower() for x in re.split(r"[,;\s]+", raw) if x.strip()]
            elif isinstance(raw, (list, tuple)):
                values = [str(x).strip().lower() for x in raw]
            if any(
                v in self.config.admin_roles or v.split("/")[-1] in self.config.admin_roles
                for v in values
            ):
                return "admin"
        return "student"

    def _check_id(self, identity_id: str) -> None:
        if not re.fullmatch(self.config.id_pattern, identity_id):
            raise AuthError(403, "identity is not a valid campus account id")

    # --------------------------------------------------------------- sessions
    def issue_session(self, identity: Identity) -> tuple[str, Session]:
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(24)
        now = self._now()
        expires = now + timedelta(hours=float(self.config.session_ttl_hours))
        expires_iso = expires.isoformat(timespec="seconds")
        self.store.create_session(
            {
                "token_hash": _sha256(token),
                "student_id": identity.student_id,
                "role": identity.role,
                "name": identity.name,
                "email": identity.email,
                "csrf": csrf,
                "created_at": now.isoformat(timespec="seconds"),
                "expires_at": expires_iso,
            }
        )
        session = Session(
            token_hash=_sha256(token),
            student_id=identity.student_id,
            role=identity.role,
            name=identity.name,
            email=identity.email,
            csrf=csrf,
            expires_at=expires_iso,
            via="cookie",
        )
        return token, session

    def _token_from_request(self, request: Any) -> tuple[str, str] | None:
        auth = request.headers.get("Authorization") or ""
        if auth.lower().startswith("bearer "):
            return auth[7:].strip(), "bearer"
        cookie = request.cookies.get(self.config.cookie_name)
        if cookie:
            return cookie, "cookie"
        return None

    def session_from_request(self, request: Any) -> Session | None:
        found = self._token_from_request(request)
        if not found:
            return None
        token, via = found
        if not token:
            return None
        row = self.store.session(_sha256(token))
        if not row:
            return None
        try:
            expiry = datetime.fromisoformat(str(row["expires_at"]))
            now = self._now()
            # Sessions issued before the timezone-aware change may be naive; compare
            # absolute timestamps so a mixed deployment does not crash on login.
            if expiry.timestamp() <= now.timestamp():
                return None
        except (ValueError, TypeError):
            return None
        return Session(
            token_hash=row["token_hash"],
            student_id=row["student_id"],
            role=row["role"],
            name=row.get("name") or "",
            email=row.get("email") or "",
            csrf=row["csrf"],
            expires_at=row["expires_at"],
            via=via,
        )

    def require_csrf(self, request: Any, session: Session) -> None:
        method = (request.method or "GET").upper()
        if session.via == "bearer" or method not in {"POST", "PUT", "PATCH", "DELETE"}:
            return
        supplied = request.headers.get("X-CSRF-Token") or ""
        if not supplied or not hmac.compare_digest(supplied, session.csrf):
            raise AuthError(403, "X-CSRF-Token missing or invalid")

    def logout(self, request: Any) -> bool:
        found = self._token_from_request(request)
        if not found:
            return False
        return self.store.revoke_session(_sha256(found[0]))

    # ---------------------------------------------------------------- cookies
    def set_cookie(self, response: Any, token: str) -> None:
        response.set_cookie(
            self.config.cookie_name,
            token,
            max_age=int(float(self.config.session_ttl_hours) * 3600),
            httponly=True,
            samesite="lax",
            secure=bool(self.config.cookie_secure),
            path="/",
        )

    def set_state_cookie(self, response: Any, value: str) -> None:
        response.set_cookie(
            "seatlab_auth_state",
            value,
            max_age=600,
            httponly=True,
            samesite="lax",
            secure=bool(self.config.cookie_secure),
            path="/auth",
        )

    def clear_state_cookie(self, response: Any) -> None:
        response.delete_cookie("seatlab_auth_state", path="/auth")

    def clear_cookie(self, response: Any) -> None:
        response.delete_cookie(self.config.cookie_name, path="/")
        response.delete_cookie("seatlab_auth_state", path="/auth")

    # ------------------------------------------------------------------ helper
    @staticmethod
    def service_url(public_base: str, config: AuthConfig) -> str:
        return config.cas_service or (public_base.rstrip("/") + config.callback_path)

    def raw_session_count(self) -> int:
        now = self._now()
        return len(
            self.store.sessions(
                active_only=True, limit=10_000, now_iso=now.isoformat(timespec="seconds")
            )
        )


def esc(value: str) -> str:
    """HTML-escape a value for the tiny server-side login fallback page."""
    return html.escape(value, quote=True)

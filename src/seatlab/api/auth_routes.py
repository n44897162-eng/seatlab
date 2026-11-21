"""HTTP routes for single sign-on: login, callback, session, logout."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from ..auth import AuthConfig, AuthError, AuthService, esc


def _public_base(request: Request, config: AuthConfig) -> str:
    if config.public_url:
        return config.public_url.rstrip("/")
    forwarded_proto = (request.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip()
    forwarded_host = (request.headers.get("X-Forwarded-Host") or "").split(",")[0].strip()
    if forwarded_host:
        scheme = forwarded_proto or request.url.scheme
        return f"{scheme}://{forwarded_host}".rstrip("/")
    return str(request.base_url).rstrip("/")


def _failure(message: str, status: int) -> HTMLResponse:
    body = f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<title>登录失败 · SeatLab</title>
<style>body{{font-family:system-ui,-apple-system,"PingFang SC",sans-serif;background:#F4F0E7;
color:#12302E;display:grid;place-items:center;min-height:100vh;margin:0}}
.card{{background:#fff;border:1px solid #E2DCCD;border-radius:14px;padding:32px;max-width:520px}}
h1{{font-size:20px;margin:0 0 10px}}p{{color:#5B6B66;line-height:1.7;margin:0 0 18px}}
a{{display:inline-block;background:#1E5A54;color:#fff;text-decoration:none;padding:10px 16px;border-radius:8px}}
</style><body><div class="card"><h1>登录未完成</h1>
<p>{esc(message)}</p><a href="/auth/login?next=/app">重新登录</a></div></body></html>"""
    return HTMLResponse(body, status_code=status)


def build_auth_router(
    auth: AuthService | None, config: AuthConfig, admin_key_enabled: bool = False
) -> APIRouter:
    router = APIRouter(tags=["auth"])

    @router.get("/auth/config")
    def auth_config() -> dict[str, Any]:
        return {
            "mode": config.mode,
            "sso": config.is_sso,
            "login_url": "/auth/login" if config.is_sso else None,
            "header_allowed": bool(config.trust_header),
            "admin_sso": bool(config.is_sso and config.admin_sso),
            "admin_key_required": admin_key_enabled,
            "cookie_secure": config.cookie_secure,
        }

    @router.get("/auth/login", include_in_schema=False)
    def login(request: Request, next: str | None = None) -> Any:
        if auth is None or not config.is_sso:
            return JSONResponse(
                {"detail": "single sign-on is not enabled; set SEATLAB_AUTH_MODE=cas or oauth"},
                status_code=404,
            )
        try:
            target, state_cookie = auth.login_target(_public_base(request, config), next)
        except AuthError as exc:
            return _failure(exc.detail, exc.status)
        response = RedirectResponse(target, status_code=302)
        auth.set_state_cookie(response, state_cookie)
        return response

    @router.get("/auth/callback", include_in_schema=False)
    def callback(request: Request) -> Any:
        def fail(message: str, status: int) -> HTMLResponse:
            response = _failure(message, status)
            if auth is not None:
                auth.clear_state_cookie(response)
            return response

        if auth is None or not config.is_sso:
            return fail("单点登录未启用", 404)
        params = request.query_params
        if params.get("error"):
            return fail(
                f"身份提供方拒绝了登录（{params.get('error')}: {params.get('error_description') or ''}）",
                401,
            )
        state = params.get("state") or params.get("RelayState") or ""
        cookie_state = request.cookies.get("seatlab_auth_state")
        if (
            not state
            and cookie_state
            and cookie_state.count(".") == 3
            and config.mode == "cas"
            and not config.cas_relay_state
        ):
            # Strict by default: a missing RelayState is a login-CSRF risk. Only
            # accept the signed cookie when the operator explicitly told SeatLab
            # that the local CAS server drops RelayState.
            state = cookie_state.split(".")[0]
        try:
            next_path = auth.verify_state(state, cookie_state)
            base = _public_base(request, config)
            if config.mode == "cas":
                identity = auth.cas_identity(
                    params.get("ticket") or "", config.cas_service or base + config.callback_path
                )
            else:
                identity = auth.oauth_identity(
                    params.get("code") or "", base + config.callback_path
                )
            token, _session = auth.issue_session(identity)
        except AuthError as exc:
            return fail(exc.detail, exc.status)
        except Exception:  # pragma: no cover - defensive; never leak a stack trace to a browser
            return fail("登录处理失败，请稍后重试或联系图书馆管理员。", 500)
        response = RedirectResponse(next_path, status_code=303)
        auth.set_cookie(response, token)
        auth.clear_state_cookie(response)
        return response

    @router.get("/auth/me")
    def me(request: Request) -> dict[str, Any]:
        session = auth.session_from_request(request) if auth else None
        if session is None:
            header_id = (request.headers.get("X-Student-ID") or "").strip()
            if config.trust_header and header_id:
                return {
                    "authenticated": True,
                    "mode": config.mode,
                    "via": "header",
                    "student_id": header_id,
                    "role": "student",
                    "name": header_id,
                    "csrf": "",
                }
            return {"authenticated": False, "mode": config.mode}
        return {"authenticated": True, "mode": config.mode, **session.public()}

    @router.post("/auth/logout")
    def logout(request: Request) -> Any:
        if auth is None:
            return {"logged_out": False}
        auth.logout(request)
        response = JSONResponse({"logged_out": True})
        auth.clear_cookie(response)
        return response

    return router

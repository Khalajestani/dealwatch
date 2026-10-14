"""Login, CSRF protection and security headers, applied to every request.

One password, a signed cookie for 90 days per device. There is no sign-out: a new password (make-env.sh --replace)
comes with a new signing key, which signs every device out.
"""

from __future__ import annotations

import logging
import secrets
import time
from collections import deque
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

from .config import Env

log = logging.getLogger(__name__)

# Open without the login: the login page itself, what the browser fetches for it and for the Home Screen, and the
# container's health check.
PUBLIC_PREFIXES = ("/login", "/static/", "/manifest.webmanifest", "/sw.js", "/apple-touch-icon", "/healthz")
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
}
SESSION_DAYS = 90

# Wrong passwords are counted for the whole app, not per address: there is one user, and behind the proxy every
# request comes from the same address anyway.
MAX_FAILURES = 8
FAILURE_WINDOW_S = 600


def install(app: FastAPI, env: Env) -> None:
    if not env.auth_enabled:
        log.warning("DEALWATCH_AUTH_PASSWORD is not set: the app is open to anyone who can reach it.")
    app.state.login_failures = deque(maxlen=MAX_FAILURES)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        if env.auth_enabled and not path.startswith(PUBLIC_PREFIXES) and not request.session.get("auth"):
            target = path + (f"?{request.url.query}" if request.url.query else "")
            return RedirectResponse(f"/login?next={quote(target)}", status_code=303)
        # The login cookie travels with every request, so state-changing ones must come from our own pages.
        if request.method not in SAFE_METHODS and not _same_origin(request):
            return PlainTextResponse("Cross-site request blocked", status_code=403)
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response

    # Added last so it is the outermost layer: request.session must exist before guard runs.
    app.add_middleware(
        SessionMiddleware,
        secret_key=env.secret_key.get_secret_value(),
        session_cookie="dealwatch_session",
        max_age=60 * 60 * 24 * SESSION_DAYS,
        same_site="lax",
        https_only=env.cookie_secure,
    )


def login_ok(request: Request, env: Env, password: str) -> bool | None:
    """True for the right password, False for a wrong one, None while locked out after too many."""
    failures: deque = request.app.state.login_failures
    if len(failures) == MAX_FAILURES and time.monotonic() - failures[0] < FAILURE_WINDOW_S:
        return None
    if secrets.compare_digest(password.encode(), env.auth_password.get_secret_value().encode()):
        failures.clear()
        return True
    failures.append(time.monotonic())
    return False


def safe_next(value: str | None, default: str = "/") -> str:
    """Only allow local redirect targets."""
    if value and value.startswith("/") and not value.startswith("//") and "\\" not in value:
        return value
    return default


def _same_origin(request: Request) -> bool:
    source = request.headers.get("origin") or request.headers.get("referer")
    if source is None:
        return True  # not a browser (curl, scripts); browsers always send Origin on cross-site POSTs
    return urlsplit(source).netloc == request.headers.get("host")

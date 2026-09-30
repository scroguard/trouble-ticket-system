"""FastAPI entrypoint: `uvicorn app.main:app`."""

import html
import logging
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import auth, site, tickets, users
from app.config import get_settings
from app.db import get_db
from app.errors import APIError, register_error_handlers

settings = get_settings()
logging.basicConfig(
    level=settings.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)

app = FastAPI(title=settings.app_name)
register_error_handlers(app)

SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}
STATIC_DIR = Path(__file__).parent / "static"
CDN = "https://cdn.jsdelivr.net"
# The dashboard never uses inline script/style or third-party calls, so a strict
# policy costs nothing and neutralizes most XSS if a bug ever slips through.
DASHBOARD_CSP = "; ".join([
    "default-src 'self'",
    f"script-src 'self' {CDN}",
    f"style-src 'self' {CDN}",
    f"font-src {CDN}",
    "img-src 'self' data:",
    "connect-src 'self'",
    "frame-ancestors 'none'",
    "base-uri 'none'",
    "form-action 'self'",
    "object-src 'none'",
])


@app.middleware("http")
async def reject_cross_origin_writes(request: Request, call_next):
    """CSRF defence #2 (after SameSite=Lax cookies): browsers always send Origin on
    cross-site POST/PATCH/DELETE, so refuse writes from origins other than our own.
    Non-browser clients (curl, scripts) send no Origin and are unaffected."""
    if request.method not in SAFE_METHODS:
        origin = request.headers.get("origin")
        if origin is not None and origin.rstrip("/") not in settings.allowed_origins:
            return JSONResponse({"error": "Cross-origin request rejected"}, status_code=403)
    return await call_next(request)


@app.middleware("http")
async def dashboard_security_headers(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path.startswith("/static/"):
        response.headers["Content-Security-Policy"] = DASHBOARD_CSP
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        # Revalidate (cheap 304s via ETag) so a deploy is picked up immediately.
        response.headers["Cache-Control"] = "no-cache"
    return response


app.include_router(auth.router)
app.include_router(tickets.router)
app.include_router(users.router)
app.include_router(users.admin_router)
app.include_router(site.router)


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


DASHBOARD_TEMPLATE = (STATIC_DIR / "index.html").read_text(encoding="utf-8")


@app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
def dashboard() -> HTMLResponse:
    """The single-page agent dashboard (talks to the API with the session cookie).
    The admin-configurable site name is filled in here, HTML-escaped, so it is right
    on first paint, including the sign-in screen."""
    name = html.escape(site.current_site_name(), quote=True)
    return HTMLResponse(DASHBOARD_TEMPLATE.replace("{{SITE_NAME}}", name))


@app.get("/healthz", include_in_schema=False)
def healthz(db: Session = Depends(get_db)) -> dict[str, str]:
    try:
        db.execute(text("SELECT 1"))
    except Exception as exc:
        raise APIError(503, "Database unavailable") from exc
    return {"status": "ok"}

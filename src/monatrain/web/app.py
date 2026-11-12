"""FastAPI application factory of the Monatrain web site."""

import logging
import mimetypes
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from monatrain.budget import QuotaBudget
from monatrain.config import Settings
from monatrain.db import connect, init_schema
from monatrain.ratelimit import SlidingWindowLimiter
from monatrain.repo import Repo
from monatrain.sncf.client import SncfAuthError, SncfClient, SncfError
from monatrain.timeutil import now_paris

log = logging.getLogger(__name__)

mimetypes.add_type("application/manifest+json", ".webmanifest")

HERE = Path(__file__).parent
RATE_LIMIT = 30
RATE_WINDOW_S = 600

MSG_RATE = "Trop de recherches, réessaie dans quelques minutes."
MSG_SNCF_DOWN = "Horaires SNCF momentanément indisponibles."
MSG_NOT_FOUND = "Page introuvable."


def create_app(
    settings: Settings | None = None,
    *,
    source=None,
    repo: Repo | None = None,
    http: httpx.AsyncClient | None = None,
    clock: Callable[[], datetime] | None = None,
) -> FastAPI:
    """Build the site. `source`, `repo`, `http` and `clock` are injected by the tests."""
    from monatrain.web.routes import LoginRequired, RateLimited, current_user, redirect, router, templates

    settings = settings or Settings.from_env()
    clock = clock or now_paris
    if repo is None:
        conn = connect(settings.db_path)
        init_schema(conn)
        repo = Repo(conn)
    owned_client: SncfClient | None = None
    if source is None:
        owned_client = SncfClient(settings.sncf_token, repo, QuotaBudget(repo), "web", http=http, clock=clock)
        source = owned_client

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        if owned_client is not None:
            await owned_client.aclose()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.repo = repo
    app.state.source = source
    app.state.clock = clock
    app.state.limiter = SlidingWindowLimiter(RATE_LIMIT, RATE_WINDOW_S)

    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret,
        https_only=settings.public_url.startswith("https"),
        same_site="lax",
    )
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    app.include_router(router)

    def error_page(request: Request, status: int, message: str) -> HTMLResponse:
        return templates.TemplateResponse(
            request, "erreur.html", {"message": message, "status": status, "user": current_user(request), "tab": None},
            status_code=status,
        )

    @app.exception_handler(LoginRequired)
    async def on_login_required(request: Request, exc: LoginRequired):
        return redirect("/login")

    @app.exception_handler(RateLimited)
    async def on_rate_limited(request: Request, exc: RateLimited):
        return error_page(request, 429, MSG_RATE)

    @app.exception_handler(SncfError)
    async def on_sncf_error(request: Request, exc: SncfError):
        if isinstance(exc, SncfAuthError):
            log.error("Token SNCF refusé : %s", exc)
        else:
            log.warning("API SNCF indisponible : %s", exc)
        return error_page(request, 503, MSG_SNCF_DOWN)

    @app.exception_handler(StarletteHTTPException)
    async def on_http_error(request: Request, exc: StarletteHTTPException):
        message = MSG_NOT_FOUND if exc.status_code == 404 else "Une erreur est survenue."
        return error_page(request, exc.status_code, message)

    return app

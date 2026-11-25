"""Discord OAuth2 login of the web site: /login, /auth/callback and /logout."""

import logging
import secrets
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from monatrain.web.routes import current_user, redirect, templates

log = logging.getLogger(__name__)

AUTHORIZE_URL = "https://discord.com/oauth2/authorize"
TOKEN_URL = "https://discord.com/api/oauth2/token"
ME_URL = "https://discord.com/api/users/@me"
SCOPE = "identify applications.commands"
TIMEOUT_S = 10.0

MSG_FAIL = "Connexion Discord impossible, réessaie."
MSG_UNCONFIGURED = "Connexion Discord non configurée"

router = APIRouter()


def _error_page(request: Request, status: int, message: str) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "erreur.html", {"message": message, "status": status, "user": current_user(request), "tab": None},
        status_code=status,
    )


def _failed(request: Request) -> HTMLResponse:
    return _error_page(request, 400, MSG_FAIL)


class DiscordError(Exception):
    pass


async def _discord_user(http: httpx.AsyncClient, settings, code: str) -> dict:
    """Exchange the authorization code for the Discord identity. The access token never leaves this function."""
    try:
        token_resp = await http.post(
            TOKEN_URL,
            data={
                "client_id": settings.discord_client_id,
                "client_secret": settings.discord_client_secret,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": f"{settings.public_url}/auth/callback",
            },
            timeout=TIMEOUT_S,
        )
        if token_resp.status_code != 200:
            raise DiscordError(f"token endpoint answered {token_resp.status_code}")
        access_token = token_resp.json()["access_token"]
        me_resp = await http.get(ME_URL, headers={"Authorization": f"Bearer {access_token}"}, timeout=TIMEOUT_S)
        if me_resp.status_code != 200:
            raise DiscordError(f"users/@me answered {me_resp.status_code}")
        me = me_resp.json()
        discord_id, username = me["id"], me["username"]
        avatar = me.get("avatar")
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        raise DiscordError(type(exc).__name__) from None
    if not isinstance(discord_id, str) or not isinstance(username, str) or not isinstance(access_token, str):
        raise DiscordError("unexpected answer")
    return {"id": discord_id, "username": username, "avatar": avatar if isinstance(avatar, str) else None}


@router.get("/login")
async def login(request: Request):
    settings = request.app.state.settings
    if not settings.discord_client_id or not settings.discord_client_secret:
        return _error_page(request, 503, MSG_UNCONFIGURED)
    state = secrets.token_urlsafe(32)
    request.session["oauth_state"] = state
    query = urlencode(
        {
            "client_id": settings.discord_client_id,
            "response_type": "code",
            "redirect_uri": f"{settings.public_url}/auth/callback",
            "scope": SCOPE,
            "integration_type": "1",
            "prompt": "none",
            "state": state,
        }
    )
    return RedirectResponse(f"{AUTHORIZE_URL}?{query}", status_code=303)


@router.get("/auth/callback")
async def callback(request: Request, code: str = "", state: str = "", error: str = ""):
    expected = request.session.pop("oauth_state", None)  # single use
    if not expected or not secrets.compare_digest(state.encode(), expected.encode()):
        return _failed(request)
    if error or not code:
        return _failed(request)
    state_app = request.app.state
    try:
        user = await _discord_user(state_app.http, state_app.settings, code)
    except DiscordError as exc:
        log.warning("Connexion Discord refusée : %s", exc)
        return _failed(request)
    state_app.repo.upsert_user(user["id"], user["username"], user["avatar"])
    state_app.repo.set_dm_ok(user["id"], True)
    request.session.clear()
    request.session["uid"] = user["id"]
    return redirect("/")


@router.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return redirect("/trajet")

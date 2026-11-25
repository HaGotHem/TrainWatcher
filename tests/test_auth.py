"""Task 8: Discord OAuth2 login of the web site (monatrain.web.auth, included by create_app).

Interface pinned here:
- The Discord HTTP calls go through the `http` httpx.AsyncClient passed to
  `create_app(settings, *, source=..., repo=..., http=..., clock=...)`. The tests give it an
  `httpx.MockTransport` that plays Discord:
  `POST https://discord.com/api/oauth2/token` and `GET https://discord.com/api/users/@me`.
- `GET /login` redirects (302/303/307) to `https://discord.com/oauth2/authorize` with `client_id`,
  `response_type=code`, `redirect_uri={PUBLIC_URL}/auth/callback`,
  `scope="identify applications.commands"`, `integration_type=1`, `prompt=none` and a random
  `state` kept in the session.
- `GET /auth/callback?code=&state=` checks the state, exchanges the code (client credentials in the
  form body or in HTTP Basic auth), reads `/users/@me` with `Authorization: Bearer <access_token>`,
  then `upsert_user(id, username, avatar)`, `set_dm_ok(id, True)`, `session["uid"] = id` and
  redirects to `/`. Bad/missing state, an `error=` query from Discord or an HTTP error from Discord:
  error page with status 400 and "Connexion Discord impossible, réessaie.", nobody logged in.
- `POST /logout` clears the session and redirects to `/trajet`.
- Missing `DISCORD_CLIENT_ID` or `DISCORD_CLIENT_SECRET`: `/login` answers **503** with the page
  text "Connexion Discord non configurée" (no redirect to Discord).
- The Discord access token never reaches the session cookie or any page.
"""

import asyncio
import base64
import html
import json
import re
from urllib.parse import parse_qs, urlsplit

import httpx
import itsdangerous
import pytest
from fastapi.testclient import TestClient

from monatrain.config import Settings
from monatrain.models import Fetched
from monatrain.web.app import create_app

SECRET = "test-secret"
PUBLIC_URL = "http://testserver"
CLIENT_ID = "1234567890123"
CLIENT_SECRET = "client-secret-9f3a"
ACCESS_TOKEN = "discord-access-token-5e1b7c"
REFRESH_TOKEN = "discord-refresh-token-0d2f"
CODE = "auth-code-abc"

DISCORD_ID = "80351110224678912"
DISCORD_ME = {
    "id": DISCORD_ID,
    "username": "nelly",
    "global_name": "Nelly",
    "avatar": "8342729096ea3675442027381ff50dfe",
    "discriminator": "0",
}

AUTHORIZE_URL = "https://discord.com/oauth2/authorize"
TOKEN_URL = "https://discord.com/api/oauth2/token"
ME_URL = "https://discord.com/api/users/@me"

MSG_FAIL = "Connexion Discord impossible, réessaie."
MSG_UNCONFIGURED = "Connexion Discord non configurée"

REDIRECTS = (302, 303, 307)


class FakeSource:
    """Empty SNCF source: the auth tests never need trains."""

    def __init__(self, clock) -> None:
        self.clock = clock

    async def journeys(self, from_id, to_id, when, count=8):
        return Fetched([], self.clock(), False)

    async def departures(self, stop_id, count=10):
        return Fetched([], self.clock(), False)


class FakeDiscord:
    """httpx.MockTransport handler playing Discord's token and /users/@me endpoints."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.token_status = 200
        self.token_body: dict = {
            "access_token": ACCESS_TOKEN,
            "token_type": "Bearer",
            "expires_in": 604800,
            "refresh_token": REFRESH_TOKEN,
            "scope": "identify applications.commands",
        }
        self.me_status = 200
        self.me_body: dict = dict(DISCORD_ME)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url).split("?")[0]
        if request.method == "POST" and url == TOKEN_URL:
            return httpx.Response(self.token_status, json=self.token_body)
        if request.method == "GET" and url == ME_URL:
            if request.headers.get("authorization") != f"Bearer {ACCESS_TOKEN}":
                return httpx.Response(401, json={"message": "401: Unauthorized", "code": 0})
            return httpx.Response(self.me_status, json=self.me_body)
        return httpx.Response(404, json={"message": "404: Not Found", "code": 0})

    def calls(self, method: str, url: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == method and str(r.url).split("?")[0] == url]


def make_settings(client_id: str | None = CLIENT_ID, client_secret: str | None = CLIENT_SECRET) -> Settings:
    return Settings(
        sncf_token="x",
        discord_bot_token=None,
        discord_client_id=client_id,
        discord_client_secret=client_secret,
        session_secret=SECRET,
        public_url=PUBLIC_URL,
        db_path=":memory:",
    )


@pytest.fixture
def discord() -> FakeDiscord:
    return FakeDiscord()


@pytest.fixture
def make_web(repo, clock, discord):
    """make_web(settings=None) -> TestClient over a fresh app whose `http` goes to FakeDiscord."""
    opened: list[tuple[TestClient, httpx.AsyncClient]] = []

    def factory(settings: Settings | None = None) -> TestClient:
        http = httpx.AsyncClient(transport=httpx.MockTransport(discord))
        app = create_app(settings or make_settings(), source=FakeSource(clock), repo=repo, http=http, clock=clock)
        client = TestClient(app)
        client.__enter__()
        opened.append((client, http))
        return client

    yield factory
    for client, http in opened:
        client.__exit__(None, None, None)
        asyncio.run(http.aclose())


def text(resp) -> str:
    return html.unescape(resp.text)


def session_of(client: TestClient) -> dict:
    """Decode the Starlette session cookie of the client ({} when there is none)."""
    raw = client.cookies.get("session")
    if not raw:
        return {}
    try:
        payload = itsdangerous.TimestampSigner(SECRET).unsign(raw.encode("utf-8"))
    except itsdangerous.BadSignature:  # e.g. the "null" value Starlette sends when it drops the session
        return {}
    return json.loads(base64.b64decode(payload))


def login_redirect(client: TestClient) -> tuple[str, dict[str, list[str]]]:
    resp = client.get("/login", follow_redirects=False)
    assert resp.status_code in REDIRECTS, resp.text
    parts = urlsplit(resp.headers["location"])
    return f"{parts.scheme}://{parts.netloc}{parts.path}", parse_qs(parts.query)


def start_login(client: TestClient) -> str:
    """GET /login and return the state put in the authorize URL."""
    _, query = login_redirect(client)
    return query["state"][0]


def token_form(request: httpx.Request) -> dict[str, str]:
    """Fields of the token request; client credentials may come from HTTP Basic auth instead."""
    form = {k: v[0] for k, v in parse_qs(request.content.decode("utf-8")).items()}
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("basic "):
        user, _, password = base64.b64decode(auth[6:]).decode("utf-8").partition(":")
        form.setdefault("client_id", user)
        form.setdefault("client_secret", password)
    return form


# --- /login ---------------------------------------------------------------------------------------


def test_login_redirect_has_scopes_and_state(make_web):
    client = make_web()

    url, query = login_redirect(client)

    assert url == AUTHORIZE_URL
    assert query["client_id"] == [CLIENT_ID]
    assert query["response_type"] == ["code"]
    assert query["redirect_uri"] == [f"{PUBLIC_URL}/auth/callback"]
    assert query["scope"] == ["identify applications.commands"]
    assert query["integration_type"] == ["1"]
    assert query["prompt"] == ["none"]
    state = query["state"][0]
    assert len(state) >= 16
    # the state is kept server side (in the signed session), not only in the URL
    assert state in json.dumps(session_of(client))


def test_login_state_is_random(make_web):
    first = start_login(make_web())
    second = start_login(make_web())

    assert first != second


@pytest.mark.parametrize(
    ("client_id", "client_secret"),
    [(None, None), (None, CLIENT_SECRET), (CLIENT_ID, None)],
    ids=["both-missing", "id-missing", "secret-missing"],
)
def test_login_unconfigured_message(make_web, discord, client_id, client_secret):
    client = make_web(make_settings(client_id, client_secret))

    resp = client.get("/login", follow_redirects=False)

    assert resp.status_code == 503
    assert MSG_UNCONFIGURED in text(resp)
    assert "discord.com/oauth2/authorize" not in resp.headers.get("location", "")
    assert discord.requests == []


def test_site_usable_without_discord_config(make_web):
    client = make_web(make_settings(None, None))

    resp = client.get("/trajet")

    assert resp.status_code == 200


# --- /auth/callback -------------------------------------------------------------------------------


def test_callback_creates_user_and_session(make_web, discord, repo):
    client = make_web()
    state = start_login(client)

    resp = client.get("/auth/callback", params={"code": CODE, "state": state}, follow_redirects=False)

    assert resp.status_code in REDIRECTS, resp.text
    assert urlsplit(resp.headers["location"]).path == "/"
    # token exchange
    (token_req,) = discord.calls("POST", TOKEN_URL)
    form = token_form(token_req)
    assert form["client_id"] == CLIENT_ID
    assert form["client_secret"] == CLIENT_SECRET
    assert form["grant_type"] == "authorization_code"
    assert form["code"] == CODE
    assert form["redirect_uri"] == f"{PUBLIC_URL}/auth/callback"
    # identity
    (me_req,) = discord.calls("GET", ME_URL)
    assert me_req.headers["authorization"] == f"Bearer {ACCESS_TOKEN}"
    # user stored, DMs considered possible (the app is installed on the account)
    stored = repo.get_user(DISCORD_ID)
    assert stored is not None
    assert stored["username"] == "nelly"
    assert stored["avatar"] == DISCORD_ME["avatar"]
    assert stored["dm_ok"] == 1
    # logged in
    assert str(session_of(client).get("uid")) == DISCORD_ID
    home = client.get("/", follow_redirects=False)
    assert home.status_code == 200


def test_callback_never_exposes_access_token(make_web, discord):
    client = make_web()
    state = start_login(client)

    resp = client.get("/auth/callback", params={"code": CODE, "state": state}, follow_redirects=False)
    home = client.get("/")

    assert discord.calls("GET", ME_URL)  # the flow really ran
    session_dump = json.dumps(session_of(client))
    for secret in (ACCESS_TOKEN, REFRESH_TOKEN, CLIENT_SECRET):
        assert secret not in session_dump
        assert secret not in client.cookies.get("session", "")
        assert secret not in resp.text
        assert secret not in resp.headers.get("location", "")
        assert secret not in home.text


def test_callback_existing_user_updated_and_dm_ok_reset(make_web, discord, repo):
    repo.upsert_user(DISCORD_ID, "ancien-nom", None)
    repo.set_dm_ok(DISCORD_ID, False)
    client = make_web()
    state = start_login(client)

    resp = client.get("/auth/callback", params={"code": CODE, "state": state}, follow_redirects=False)

    assert resp.status_code in REDIRECTS
    stored = repo.get_user(DISCORD_ID)
    assert stored["username"] == "nelly"
    assert stored["avatar"] == DISCORD_ME["avatar"]
    assert stored["dm_ok"] == 1


def test_callback_state_cannot_be_replayed(make_web, discord, repo):
    client = make_web()
    state = start_login(client)
    first = client.get("/auth/callback", params={"code": CODE, "state": state}, follow_redirects=False)
    assert first.status_code in REDIRECTS
    client.post("/logout", follow_redirects=False)

    again = client.get("/auth/callback", params={"code": CODE, "state": state}, follow_redirects=False)

    assert again.status_code == 400
    assert MSG_FAIL in text(again)
    assert "uid" not in session_of(client)
    assert len(discord.calls("POST", TOKEN_URL)) == 1


def _assert_failed(resp, client, discord, repo, *, token_called: bool) -> None:
    assert resp.status_code == 400
    assert MSG_FAIL in text(resp)
    assert "uid" not in session_of(client)
    assert repo.get_user(DISCORD_ID) is None
    assert bool(discord.calls("POST", TOKEN_URL)) is token_called


@pytest.mark.parametrize(
    "params",
    [
        {"code": CODE, "state": "pas-le-bon-state"},
        {"code": CODE},
        {"code": CODE, "state": ""},
    ],
    ids=["wrong-state", "missing-state", "empty-state"],
)
def test_callback_bad_state_400(make_web, discord, repo, params):
    client = make_web()
    start_login(client)

    resp = client.get("/auth/callback", params=params, follow_redirects=False)

    _assert_failed(resp, client, discord, repo, token_called=False)


def test_callback_without_login_400(make_web, discord, repo):
    """No /login before: there is no state in the session to match."""
    client = make_web()

    resp = client.get("/auth/callback", params={"code": CODE, "state": "x" * 32}, follow_redirects=False)

    _assert_failed(resp, client, discord, repo, token_called=False)


def test_callback_state_from_another_browser_400(make_web, discord, repo):
    victim = make_web()
    attacker = make_web()
    attacker_state = start_login(attacker)
    start_login(victim)

    resp = victim.get("/auth/callback", params={"code": CODE, "state": attacker_state}, follow_redirects=False)

    _assert_failed(resp, victim, discord, repo, token_called=False)


@pytest.mark.parametrize("failing", ["token-400", "token-500", "me-401", "me-500"])
def test_callback_discord_error_400(make_web, discord, repo, failing):
    if failing == "token-400":
        discord.token_status, discord.token_body = 400, {"error": "invalid_grant"}
    elif failing == "token-500":
        discord.token_status, discord.token_body = 500, {"message": "Internal Server Error"}
    elif failing == "me-401":
        discord.me_status, discord.me_body = 401, {"message": "401: Unauthorized", "code": 0}
    else:
        discord.me_status, discord.me_body = 500, {"message": "Internal Server Error"}
    client = make_web()
    state = start_login(client)

    resp = client.get("/auth/callback", params={"code": CODE, "state": state}, follow_redirects=False)

    _assert_failed(resp, client, discord, repo, token_called=True)


def test_callback_user_refused_400(make_web, discord, repo):
    """Discord sends the user back with ?error=access_denied (and no code) when they refuse."""
    client = make_web()
    state = start_login(client)

    resp = client.get(
        "/auth/callback",
        params={"error": "access_denied", "error_description": "The resource owner denied the request", "state": state},
        follow_redirects=False,
    )

    _assert_failed(resp, client, discord, repo, token_called=False)


def test_callback_network_error_400(make_web, repo, clock):
    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("discord unreachable", request=request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(broken))
    app = create_app(make_settings(), source=FakeSource(clock), repo=repo, http=http, clock=clock)
    try:
        with TestClient(app) as client:
            state = start_login(client)
            resp = client.get("/auth/callback", params={"code": CODE, "state": state}, follow_redirects=False)

            assert resp.status_code == 400
            assert MSG_FAIL in text(resp)
            assert "uid" not in session_of(client)
            assert repo.get_user(DISCORD_ID) is None
    finally:
        asyncio.run(http.aclose())


# --- /logout --------------------------------------------------------------------------------------


def test_logout_clears_session(make_web, discord):
    client = make_web()
    state = start_login(client)
    client.get("/auth/callback", params={"code": CODE, "state": state}, follow_redirects=False)
    assert str(session_of(client).get("uid")) == DISCORD_ID

    resp = client.post("/logout", follow_redirects=False)

    assert resp.status_code in REDIRECTS
    assert urlsplit(resp.headers["location"]).path == "/trajet"
    assert "uid" not in session_of(client)
    home = client.get("/", follow_redirects=False)
    assert home.status_code == 303
    assert urlsplit(home.headers["location"]).path == "/trajet"


# --- DM banner install link -----------------------------------------------------------------------


def test_dm_banner_has_install_link(make_web, discord, repo):
    client = make_web()
    state = start_login(client)
    client.get("/auth/callback", params={"code": CODE, "state": state}, follow_redirects=False)
    repo.set_dm_ok(DISCORD_ID, False)

    page = client.get("/").text

    expected_query = {"client_id": [CLIENT_ID], "integration_type": ["1"], "scope": ["applications.commands"]}
    install = [
        parts
        for parts in (urlsplit(html.unescape(h)) for h in re.findall(r"""href\s*=\s*["']([^"']*)["']""", page))
        if f"{parts.scheme}://{parts.netloc}{parts.path}" == AUTHORIZE_URL and parse_qs(parts.query) == expected_query
    ]
    assert install, "no Discord install link in the DM banner"

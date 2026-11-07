import os
from collections.abc import Mapping
from dataclasses import dataclass, field

from dotenv import find_dotenv, load_dotenv

DEFAULT_PUBLIC_URL = "http://localhost:8000"
DEFAULT_DB_PATH = "data/monatrain.db"


@dataclass(frozen=True)
class Settings:
    sncf_token: str = field(repr=False)
    discord_bot_token: str | None = field(repr=False)
    discord_client_id: str | None
    discord_client_secret: str | None = field(repr=False)
    session_secret: str = field(repr=False)
    public_url: str = DEFAULT_PUBLIC_URL
    db_path: str = DEFAULT_DB_PATH

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        """Build the settings from an explicit mapping, or from the process environment.

        With an explicit mapping, that mapping is the only source. With None, the
        `.env` file found from the working directory is loaded first (values already
        in the process environment win), then os.environ is read.
        """
        if env is None:
            load_dotenv(find_dotenv(usecwd=True))
            source: Mapping[str, str] = os.environ
        else:
            source = env

        sncf_token = _value(source, "SNCF_TOKEN")
        if sncf_token is None:
            raise ValueError("Variable d'environnement manquante ou vide : SNCF_TOKEN")

        session_secret = _value(source, "SESSION_SECRET")
        if session_secret is None:
            raise ValueError("Variable d'environnement manquante ou vide : SESSION_SECRET")

        return cls(
            sncf_token=sncf_token,
            discord_bot_token=_value(source, "DISCORD_BOT_TOKEN"),
            discord_client_id=_value(source, "DISCORD_CLIENT_ID"),
            discord_client_secret=_value(source, "DISCORD_CLIENT_SECRET"),
            session_secret=session_secret,
            public_url=_value(source, "PUBLIC_URL") or DEFAULT_PUBLIC_URL,
        )


def _value(source: Mapping[str, str], name: str) -> str | None:
    """Return the variable's value, or None when it is absent or empty."""
    return source.get(name) or None

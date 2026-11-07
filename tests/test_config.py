import dataclasses

import pytest

from monatrain.config import Settings

# Settings.from_env(env) with an explicit mapping uses ONLY that mapping
# (no .env file, no os.environ), so these tests are independent of the
# developer's real .env.

FULL_ENV = {
    "SNCF_TOKEN": "tok-sncf",
    "DISCORD_BOT_TOKEN": "tok-bot",
    "DISCORD_CLIENT_ID": "123456789",
    "DISCORD_CLIENT_SECRET": "client-secret",
    "SESSION_SECRET": "sess-secret",
    "PUBLIC_URL": "https://train.example.test",
}


# --- Brief test ------------------------------------------------------------

def test_settings_missing_token_names_variable():
    with pytest.raises(ValueError, match="SNCF_TOKEN"): Settings.from_env({"SESSION_SECRET": "x"})


# --- Additional pinning tests ----------------------------------------------

def test_settings_missing_session_secret_names_variable():
    with pytest.raises(ValueError, match="SESSION_SECRET"):
        Settings.from_env({"SNCF_TOKEN": "x"})


def test_settings_empty_required_value_counts_as_missing():
    # .env.example ships empty values; copying it unchanged must fail loudly.
    with pytest.raises(ValueError, match="SNCF_TOKEN"):
        Settings.from_env({"SNCF_TOKEN": "", "SESSION_SECRET": "x"})
    with pytest.raises(ValueError, match="SESSION_SECRET"):
        Settings.from_env({"SNCF_TOKEN": "x", "SESSION_SECRET": ""})


def test_settings_full_env():
    s = Settings.from_env(FULL_ENV)
    assert s.sncf_token == "tok-sncf"
    assert s.discord_bot_token == "tok-bot"
    assert s.discord_client_id == "123456789"
    assert s.discord_client_secret == "client-secret"
    assert s.session_secret == "sess-secret"
    assert s.public_url == "https://train.example.test"


def test_settings_minimal_env_uses_defaults():
    s = Settings.from_env({"SNCF_TOKEN": "a", "SESSION_SECRET": "b"})
    assert s.sncf_token == "a"
    assert s.session_secret == "b"
    assert s.discord_bot_token is None
    assert s.discord_client_id is None
    assert s.discord_client_secret is None
    assert s.public_url == "http://localhost:8000"
    assert s.db_path == "data/monatrain.db"


def test_settings_empty_optional_discord_values_are_none():
    env = {"SNCF_TOKEN": "a", "SESSION_SECRET": "b", "DISCORD_BOT_TOKEN": "",
           "DISCORD_CLIENT_ID": "", "DISCORD_CLIENT_SECRET": ""}
    s = Settings.from_env(env)
    assert s.discord_bot_token is None
    assert s.discord_client_id is None
    assert s.discord_client_secret is None


def test_settings_explicit_mapping_ignores_process_environment(monkeypatch):
    monkeypatch.setenv("SNCF_TOKEN", "from-os-environ")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "from-os-environ")
    s = Settings.from_env({"SNCF_TOKEN": "from-mapping", "SESSION_SECRET": "b"})
    assert s.sncf_token == "from-mapping"
    assert s.discord_bot_token is None


def test_settings_from_process_environment_when_env_is_none(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SNCF_TOKEN", "env-token")
    monkeypatch.setenv("SESSION_SECRET", "env-secret")
    monkeypatch.setenv("PUBLIC_URL", "https://env.example.test")
    for var in ("DISCORD_BOT_TOKEN", "DISCORD_CLIENT_ID", "DISCORD_CLIENT_SECRET"):
        monkeypatch.setenv(var, "")
    s = Settings.from_env()
    # Values already in the process environment win over any .env file.
    assert s.sncf_token == "env-token"
    assert s.session_secret == "env-secret"
    assert s.public_url == "https://env.example.test"


def test_settings_is_frozen():
    s = Settings.from_env({"SNCF_TOKEN": "a", "SESSION_SECRET": "b"})
    assert dataclasses.is_dataclass(s)
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.sncf_token = "other"


def test_settings_constructor_defaults():
    s = Settings(sncf_token="a", discord_bot_token=None, discord_client_id=None,
                 discord_client_secret=None, session_secret="b")
    assert s.public_url == "http://localhost:8000"
    assert s.db_path == "data/monatrain.db"


def test_settings_repr_does_not_leak_secrets():
    s = Settings.from_env(FULL_ENV)
    text = repr(s)
    assert "tok-sncf" not in text
    assert "tok-bot" not in text
    assert "client-secret" not in text
    assert "sess-secret" not in text

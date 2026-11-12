"""Task 10 : Dockerfile, docker-compose (Cloudflare Tunnel), README."""

import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]


def _ignore_lines(name: str) -> set[str]:
    path = ROOT / name
    assert path.exists(), f"{name} manquant"
    lines = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            lines.add(line.lstrip("/").rstrip("/"))
    return lines


def _load_compose() -> dict:
    path = ROOT / "docker-compose.yml"
    assert path.exists(), "docker-compose.yml manquant"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict) and isinstance(data.get("services"), dict)
    return data


def _as_text(value) -> str:
    if isinstance(value, list):
        return " ".join(str(v) for v in value)
    return str(value)


def _volume_strings(service: dict) -> list[str]:
    out = []
    for v in service.get("volumes") or []:
        if isinstance(v, dict):
            out.append(f"{v.get('source')}:{v.get('target')}")
        else:
            out.append(str(v))
    return out


def test_env_ignored_by_git_and_docker():
    for name in (".gitignore", ".dockerignore"):
        lines = _ignore_lines(name)
        assert ".env" in lines, f".env absent de {name}"
        assert "data" in lines, f"data/ absent de {name}"


def test_compose_has_three_services_and_prod_profile():
    services = _load_compose()["services"]
    assert {"web", "bot", "cloudflared"} <= set(services)

    assert services["cloudflared"].get("profiles") == ["prod"]
    assert not services["web"].get("profiles")
    assert not services["bot"].get("profiles")

    for name in ("web", "bot"):
        vols = _volume_strings(services[name])
        assert any(
            re.fullmatch(r"\./data:/app/data(:rw)?", v) for v in vols
        ), f"volume ./data:/app/data absent de {name}"
        env_file = services[name].get("env_file")
        env_files = env_file if isinstance(env_file, list) else [env_file]
        assert ".env" in [e if isinstance(e, str) else e.get("path") for e in env_files]

    ports = services["web"].get("ports") or []
    assert ports, "web doit publier le port 8000"
    for p in ports:
        assert isinstance(p, str) and p.startswith("127.0.0.1:"), p
    assert "127.0.0.1:8000:8000" in ports
    assert not services["bot"].get("ports")
    assert not services["cloudflared"].get("ports")

    for name in ("web", "bot", "cloudflared"):
        assert services[name].get("init") is True, f"init: true manquant sur {name}"
        assert services[name].get("restart") == "unless-stopped", name

    bot_cmd = _as_text(services["bot"].get("command", ""))
    assert re.search(r"python\s+-m\s+monatrain\.bot\b", bot_cmd), bot_cmd

    cf = services["cloudflared"]
    assert str(cf.get("image", "")).startswith("cloudflare/cloudflared")
    env = cf.get("environment")
    if isinstance(env, list):
        env = dict(e.split("=", 1) for e in env)
    assert env.get("TUNNEL_TOKEN") == "${CLOUDFLARE_TUNNEL_TOKEN}"
    depends = cf.get("depends_on")
    assert "web" in (depends if isinstance(depends, (list, dict)) else [depends])


def test_dockerfile_basics():
    path = ROOT / "Dockerfile"
    assert path.exists(), "Dockerfile manquant"
    text = path.read_text(encoding="utf-8")

    assert re.search(r"^FROM\s+python:3\.12-slim\b", text, re.M | re.I)
    assert re.search(r"\bTZ[=\s]+\"?Europe/Paris\b", text)

    users = re.findall(r"^USER\s+(\S+)", text, re.M | re.I)
    assert users, "aucune instruction USER"
    assert users[-1].split(":")[0] not in ("root", "0")

    for line in text.splitlines():
        if re.match(r"\s*(COPY|ADD)\b", line, re.I):
            assert not re.search(r"\.env(?!\.example)\b", line), line


def _sncf_token() -> str | None:
    token = os.environ.get("SNCF_TOKEN")
    if not token:
        env_path = ROOT / ".env"
        if env_path.exists():
            token = dotenv_values(env_path).get("SNCF_TOKEN")
    return token.strip() if token else None


def test_no_secret_literals_in_repo():
    token = _sncf_token()
    if not token or len(token) < 8:
        pytest.skip("SNCF_TOKEN non disponible")
    needle = token.encode("utf-8")

    files = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True
    ).stdout.decode("utf-8").split("\0")
    leaked = []
    for rel in filter(None, files):
        path = ROOT / rel
        if path.is_file() and needle in path.read_bytes():
            leaked.append(rel)
    # Ne jamais afficher le token : on ne liste que les chemins.
    assert not leaked, f"secret SNCF trouvé dans des fichiers suivis : {leaked}"


def test_readme_mentions_key_steps():
    path = ROOT / "README.md"
    assert path.exists(), "README.md manquant"
    text = path.read_text(encoding="utf-8")
    assert "docker compose --profile prod up -d" in text
    assert "train.sbertacchi.dev/auth/callback" in text
    assert "PUBLIC_URL" in text
    assert "slash final" in text.lower(), "le README doit avertir : PUBLIC_URL sans slash final"

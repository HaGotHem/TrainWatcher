"""Capture real SNCF (Navitia) responses into tests/fixtures/.

Usage: python scripts/capture_fixtures.py

Reads only SNCF_TOKEN from the environment (or .env). The token is never
printed nor written anywhere. Responses are trimmed to a few items to keep
the fixtures small: section geojson shapes are dropped, and so are the
disruptions not referenced by the kept items.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv

BASE_URL = "https://api.sncf.com/v1/coverage/sncf"
MONACO = "stop_area:SNCF:87756403"
NICE = "stop_area:SNCF:87756056"
PARIS = ZoneInfo("Europe/Paris")
KEEP = 4  # items kept per response
FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"


def _disruption_ids(items: list[dict]) -> set[str]:
    ids: set[str] = set()

    def walk(node: object) -> None:
        if isinstance(node, dict):
            if node.get("rel") == "disruptions" and node.get("type") == "disruption" and "id" in node:
                ids.add(node["id"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(items)
    return ids


def _trim(payload: dict, key: str) -> dict:
    items = payload.get(key, [])[:KEEP]
    payload[key] = items
    for item in items:  # geojson shapes are bulky and unused
        for section in item.get("sections", []):
            section.pop("geojson", None)
    if "disruptions" in payload:
        keep_ids = _disruption_ids(items)
        payload["disruptions"] = [d for d in payload["disruptions"] if d.get("id") in keep_ids]
    return payload


def _get(client: httpx.Client, path: str, params: dict) -> dict:
    response = client.get(f"{BASE_URL}{path}", params=params)
    response.raise_for_status()
    return response.json()


def _save(name: str, payload: dict) -> None:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    path = FIXTURES / name
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"saved {path.relative_to(FIXTURES.parent.parent)}")


def main() -> None:
    load_dotenv()
    token = os.environ["SNCF_TOKEN"]
    now = datetime.now(PARIS).strftime("%Y%m%dT%H%M%S")

    with httpx.Client(auth=(token, ""), timeout=20) as client:
        journeys = _get(
            client,
            "/journeys",
            {
                "from": MONACO,
                "to": NICE,
                "datetime": now,
                "datetime_represents": "departure",
                "data_freshness": "realtime",
                "max_nb_transfers": 0,
                "direct_path": "none",
                "count": 8,
            },
        )
        _save("journeys_monaco_nice.json", _trim(journeys, "journeys"))

        for name, stop in (("departures_monaco.json", MONACO), ("departures_nice.json", NICE)):
            departures = _get(
                client,
                f"/stop_areas/{stop}/departures",
                {"data_freshness": "realtime", "count": 10},
            )
            _save(name, _trim(departures, "departures"))


if __name__ == "__main__":
    main()

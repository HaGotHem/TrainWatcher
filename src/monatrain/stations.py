import re
import unicodedata
from dataclasses import dataclass


@dataclass(frozen=True)
class Station:
    id: str
    name: str


STATIONS: tuple[Station, ...] = (
    Station("stop_area:SNCF:87757583", "Théoule-sur-Mer"),
    Station("stop_area:SNCF:87757591", "Mandelieu-la-Napoule"),
    Station("stop_area:SNCF:87757617", "Cannes La Bocca"),
    Station("stop_area:SNCF:87757625", "Cannes"),
    Station("stop_area:SNCF:87757757", "Le Bosquet"),
    Station("stop_area:SNCF:87757732", "Ranguin"),
    Station("stop_area:SNCF:87401323", "Mouans-Sartoux"),
    Station("stop_area:SNCF:87757724", "Grasse"),
    Station("stop_area:SNCF:87757641", "Golfe-Juan – Vallauris"),
    Station("stop_area:SNCF:87757666", "Juan-les-Pins"),
    Station("stop_area:SNCF:87757674", "Antibes"),
    Station("stop_area:SNCF:87757690", "Biot"),
    Station("stop_area:SNCF:87756304", "Villeneuve-Loubet – Plage"),
    Station("stop_area:SNCF:87756320", "Cagnes-sur-Mer"),
    Station("stop_area:SNCF:87756338", "Cros-de-Cagnes"),
    Station("stop_area:SNCF:87756346", "Saint-Laurent-du-Var"),
    Station("stop_area:SNCF:87756254", "Nice Saint-Augustin"),
    Station("stop_area:SNCF:87756056", "Nice-Ville"),
    Station("stop_area:SNCF:87756353", "Nice Riquier"),
    Station("stop_area:SNCF:87756361", "Villefranche-sur-Mer"),
    Station("stop_area:SNCF:87756379", "Beaulieu-sur-Mer"),
    Station("stop_area:SNCF:87756387", "Èze"),
    Station("stop_area:SNCF:87756395", "Cap-d'Ail"),
    Station("stop_area:SNCF:87756403", "Monaco Monte-Carlo"),
    Station("stop_area:SNCF:87756478", "Carnolès"),
    Station("stop_area:SNCF:87756460", "Roquebrune-Cap-Martin"),
    Station("stop_area:SNCF:87756486", "Menton"),
    Station("stop_area:SNCF:87756494", "Menton Garavan"),
    Station("stop_area:SNCF:83045013", "Vintimille"),
)

_BY_ID: dict[str, Station] = {station.id: station for station in STATIONS}
_UIC_RE = re.compile(r"(?<!\d)\d{8}(?!\d)")


def get_station(station_id: str) -> Station | None:
    return _BY_ID.get(station_id)


def search_stations(query: str, limit: int = 25) -> list[Station]:
    """Find stations whose name contains the query, ignoring case, accents, hyphens and apostrophes.

    Names starting with the query come first, shorter names first; then inner matches.
    A blank query returns no station.
    """
    needle = _normalize(query)
    if not needle:
        return []

    prefix_matches: list[Station] = []
    inner_matches: list[Station] = []
    for station in STATIONS:
        normalized = _normalize(station.name)
        if normalized.startswith(needle):
            prefix_matches.append(station)
        elif needle in normalized:
            inner_matches.append(station)

    prefix_matches.sort(key=lambda s: len(_normalize(s.name)))
    return (prefix_matches + inner_matches)[:limit]


def uic_code(stop_id: str) -> str:
    """Return the 8-digit UIC code of a stop id, e.g. "87756403" for "stop_area:SNCF:87756403"."""
    match = _UIC_RE.search(stop_id)
    if match is None:
        raise ValueError(f"Pas de code UIC à 8 chiffres dans l'identifiant : {stop_id!r}")
    return match.group(0)


def _normalize(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    without_accents = "".join(char for char in decomposed if not unicodedata.combining(char))
    spaced = without_accents.lower().replace("-", " ").replace("'", " ")
    return " ".join(spaced.split())

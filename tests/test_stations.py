from monatrain.stations import STATIONS, Station, get_station, search_stations, uic_code

# Spec § 4.4, in the spec's geographic order (names and IDs verbatim).
EXPECTED_STATIONS = (
    ("stop_area:SNCF:87757583", "Théoule-sur-Mer"),
    ("stop_area:SNCF:87757591", "Mandelieu-la-Napoule"),
    ("stop_area:SNCF:87757617", "Cannes La Bocca"),
    ("stop_area:SNCF:87757625", "Cannes"),
    ("stop_area:SNCF:87757757", "Le Bosquet"),
    ("stop_area:SNCF:87757732", "Ranguin"),
    ("stop_area:SNCF:87401323", "Mouans-Sartoux"),
    ("stop_area:SNCF:87757724", "Grasse"),
    ("stop_area:SNCF:87757641", "Golfe-Juan – Vallauris"),
    ("stop_area:SNCF:87757666", "Juan-les-Pins"),
    ("stop_area:SNCF:87757674", "Antibes"),
    ("stop_area:SNCF:87757690", "Biot"),
    ("stop_area:SNCF:87756304", "Villeneuve-Loubet – Plage"),
    ("stop_area:SNCF:87756320", "Cagnes-sur-Mer"),
    ("stop_area:SNCF:87756338", "Cros-de-Cagnes"),
    ("stop_area:SNCF:87756346", "Saint-Laurent-du-Var"),
    ("stop_area:SNCF:87756254", "Nice Saint-Augustin"),
    ("stop_area:SNCF:87756056", "Nice-Ville"),
    ("stop_area:SNCF:87756353", "Nice Riquier"),
    ("stop_area:SNCF:87756361", "Villefranche-sur-Mer"),
    ("stop_area:SNCF:87756379", "Beaulieu-sur-Mer"),
    ("stop_area:SNCF:87756387", "Èze"),
    ("stop_area:SNCF:87756395", "Cap-d'Ail"),
    ("stop_area:SNCF:87756403", "Monaco Monte-Carlo"),
    ("stop_area:SNCF:87756478", "Carnolès"),
    ("stop_area:SNCF:87756460", "Roquebrune-Cap-Martin"),
    ("stop_area:SNCF:87756486", "Menton"),
    ("stop_area:SNCF:87756494", "Menton Garavan"),
    ("stop_area:SNCF:83045013", "Vintimille"),
)


def names(stations):
    return [s.name for s in stations]


# --- Brief tests -----------------------------------------------------------

def test_station_list_matches_spec():
    assert len(STATIONS) == 29
    assert get_station("stop_area:SNCF:87756403").name == "Monaco Monte-Carlo"
    assert get_station("stop_area:SNCF:83045013").name == "Vintimille"


def test_search_accent_insensitive():
    assert search_stations("eze")[0].name == "Èze"
    assert search_stations("nice ville")[0].id == "stop_area:SNCF:87756056"
    assert search_stations("cannes")[0].name == "Cannes"   # avant "Cannes La Bocca"


def test_uic_code():
    assert uic_code("stop_point:SNCF:87756403:Train") == uic_code("stop_area:SNCF:87756403") == "87756403"


# --- Additional pinning tests ----------------------------------------------

def test_stations_is_tuple_equal_to_spec_table_in_order():
    assert isinstance(STATIONS, tuple)
    assert STATIONS == tuple(Station(id=i, name=n) for i, n in EXPECTED_STATIONS)


def test_station_has_id_and_name_fields():
    s = Station(id="stop_area:SNCF:87756056", name="Nice-Ville")
    assert s.id == "stop_area:SNCF:87756056"
    assert s.name == "Nice-Ville"


def test_station_ids_are_unique():
    ids = [s.id for s in STATIONS]
    assert len(set(ids)) == len(ids)


def test_get_station_every_spec_station():
    for station_id, name in EXPECTED_STATIONS:
        station = get_station(station_id)
        assert station is not None
        assert station.id == station_id
        assert station.name == name


def test_get_station_unknown_returns_none():
    assert get_station("stop_area:SNCF:87686006") is None  # Paris-Gare-de-Lyon, not in list
    assert get_station("") is None


def test_search_cannes_returns_both_cannes_stations_exact_first():
    result = search_stations("cannes")
    assert names(result)[0] == "Cannes"
    assert set(names(result)) == {"Cannes", "Cannes La Bocca"}


def test_search_is_case_insensitive():
    assert names(search_stations("MONACO")) == ["Monaco Monte-Carlo"]
    assert names(search_stations("ViNtImIlLe")) == ["Vintimille"]


def test_search_accent_in_query_or_name_is_ignored():
    assert names(search_stations("èze")) == ["Èze"]
    assert names(search_stations("EZE")) == ["Èze"]
    assert names(search_stations("theoule")) == ["Théoule-sur-Mer"]
    assert names(search_stations("carnoles")) == ["Carnolès"]


def test_search_ignores_hyphens_and_spaces():
    # "-" in the name or the query is treated as a space
    assert search_stations("nice-ville")[0].name == "Nice-Ville"
    assert search_stations("monaco monte carlo")[0].name == "Monaco Monte-Carlo"
    assert search_stations("juan les pins")[0].name == "Juan-les-Pins"
    assert search_stations("saint laurent du var")[0].name == "Saint-Laurent-du-Var"


def test_search_ignores_apostrophes():
    assert search_stations("cap d ail")[0].name == "Cap-d'Ail"
    assert search_stations("cap d'ail")[0].name == "Cap-d'Ail"


def test_search_prefix_matches_come_before_inner_matches():
    # "Golfe-Juan – Vallauris" precedes "Juan-les-Pins" in STATIONS, but only
    # "Juan-les-Pins" starts with "juan".
    assert names(search_stations("juan")) == ["Juan-les-Pins", "Golfe-Juan – Vallauris"]


def test_search_matches_inside_name():
    assert names(search_stations("bocca")) == ["Cannes La Bocca"]
    assert names(search_stations("garavan")) == ["Menton Garavan"]


def test_search_menton_returns_both():
    result = names(search_stations("menton"))
    assert result[0] == "Menton"
    assert set(result) == {"Menton", "Menton Garavan"}


def test_search_no_match_returns_empty_list():
    assert search_stations("marseille") == []


def test_search_returns_list_of_stations():
    result = search_stations("nice")
    assert isinstance(result, list)
    assert all(isinstance(s, Station) for s in result)
    assert set(names(result)) == {"Nice Saint-Augustin", "Nice-Ville", "Nice Riquier"}


def test_search_respects_limit():
    # many station names contain an "e"
    assert len(search_stations("e", limit=3)) == 3
    assert len(search_stations("e", limit=1)) == 1
    assert len(search_stations("e")) <= 25


def test_uic_code_other_ids():
    assert uic_code("stop_area:SNCF:83045013") == "83045013"
    assert uic_code("stop_point:SNCF:87756056:Train") == "87756056"
    assert uic_code("stop_area:SNCF:87401323") == "87401323"

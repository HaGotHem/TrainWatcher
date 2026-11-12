# Monatrain — Plan de réalisation

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Règle de Thomas (CLAUDE.md) :** dans chaque tâche, l'étape « Écrire les tests » est faite par un agent **`opus`** à partir de ce plan et de la spec, **avant** tout code. L'implémentation est faite par un agent **`sonnet`** (ou `haiku` pour les tâches mécaniques marquées *[haiku]*). L'implémenteur **ne modifie pas** les tests ; s'il pense qu'un test est faux, il le signale et Opus le corrige. Chaque tâche se termine par une revue `opus`, et l'ensemble par une revue `opus` finale.

**Goal :** Construire Monatrain, un site mobile first et une app Discord qui affichent les horaires temps réel des TER de la Côte d'Azur et envoient un DM quand un train suivi est en retard, supprimé ou remplacé par un car. Le tout tourne sous Docker.

**Architecture :** Un paquet Python `monatrain` et une image Docker lancent deux services : `web` (FastAPI + Jinja2) et `bot` (discord.py + boucle de surveillance). Ils partagent une base SQLite (WAL) sur un volume. Cette base contient aussi le **cache des réponses SNCF** et le **compteur de quota** : une même requête sert donc les deux processus et tous les utilisateurs. En prod, `cloudflared` expose `web` sur `train.sbertacchi.dev`.

**Tech Stack :** Python ≥ 3.12, FastAPI, uvicorn, Jinja2, httpx (async), itsdangerous (sessions), python-multipart, discord.py ≥ 2.5, tzdata, python-dotenv ; tests : pytest, pytest-asyncio, `httpx.MockTransport` (aucun appel réseau réel). Docker + Compose, `cloudflare/cloudflared`.

**Spec :** `docs/superpowers/specs/2026-11-06-monatrain-design.md`

## Global Constraints

- Secrets uniquement via variables d'environnement / `.env` : `SNCF_TOKEN`, `DISCORD_BOT_TOKEN`, `DISCORD_CLIENT_ID`, `DISCORD_CLIENT_SECRET`, `SESSION_SECRET`, `PUBLIC_URL`, `CLOUDFLARE_TUNNEL_TOKEN`. Jamais en dur, jamais logués. `.env` dans `.gitignore` **et** `.dockerignore`.
- API : `https://api.sncf.com/v1/coverage/sncf`, HTTP Basic (token, mot de passe vide). Dates `YYYYMMDDTHHMMSS`, heure locale **Europe/Paris** ; toutes les `datetime` manipulées sont *aware* `ZoneInfo("Europe/Paris")`.
- Quota : 5 000 req/jour, 150 000/mois ; **2 000/jour réservées au watcher** ; au-delà de **3 000 req « web »/jour**, le site ne sert plus que du cache. Cache frais **60 s** ; données périmées conservées **24 h** pour le repli.
- Alertes : fenêtre de surveillance à partir de **60 min avant le départ prévu** ; DM si retard **≥ 5 min** ; nouveau DM seulement si le retard augmente encore de **≥ 5 min** par rapport au dernier DM, ou si le train passe à supprimé ou remplacé par car. Aucun DM « tout va bien ». Interrogation d'un même train au plus toutes les **120 s**.
- Suppressions (R25) : `journeys` et `departures` recoupent le temps réel avec `data_freshness=base_schedule` (cache 300 s, compté dans le quota, jamais repli du temps réel) et ajoutent les trains `CANCELLED` absents du temps réel.
- Introuvable : après **2 jours prévus consécutifs** introuvable → **un seul DM**, qui propose le train le plus proche à **± 15 min**.
- Limites : **30 requêtes / 10 min par IP** pour les recherches et actions web (`/trajet`, `/gare`, `POST /suivis`…) et **120 / 10 min par IP** pour `GET /` et `/fragments/*` (R18) ; **20 interactions / 10 min par utilisateur Discord** qui appellent l'API ; **10 trains suivis max** par utilisateur.
- Gares : uniquement la liste de la spec § 4.4 (29 gares, IDs exacts).
- Interface (site, DM, commandes) **en français**. Thème clair et sombre selon `prefers-color-scheme`. Manifest PWA, sans mode hors ligne.
- Jours de circulation codés en masque de bits : lun = 1, mar = 2, mer = 4, jeu = 8, ven = 16, sam = 32, dim = 64 ; préréglages : semaine = 31, tous les jours = 127, week-end = 96.

## Review Focus

1. **Minuit et changement d'heure** : un train à 23:50 en retard jusqu'à 00:05, un tick du watcher à 00:10 pour un train de 00:40, et le passage à l'heure d'hiver. Attendu : fenêtres et retards corrects (datetimes *aware*), aucun DM en double ou manquant. Test dans la tâche 6 (`test_window_across_midnight`).
2. **Données temps réel absentes ou partielles** : `data_freshness == base_schedule`, ou `departure_date_time` absent sur un `NO_SERVICE`. Attendu : « à l'heure » sans alerte dans le premier cas, « supprimé » sans plantage dans le second. Tests dans la tâche 2 (`test_base_schedule_is_on_time`, `test_no_service_without_realtime_times`).
3. **API SNCF indisponible** (timeout, 5xx, 429) pendant un tick ou une recherche web. Attendu : pas de plantage, aucune alerte ; le watcher réessaie au tick suivant ; le site affiche les données en cache avec « données d'il y a X min », ou un message d'erreur en français. Tests dans les tâches 3 et 6 (`test_unavailable_serves_stale`, `test_tick_survives_source_error`).
4. **Même train suivi par plusieurs personnes, dont une aux DM bloqués**. Attendu : une seule requête API, un DM par personne, et l'échec d'un DM n'empêche pas les autres. Test dans la tâche 6 (`test_group_one_request_many_dms`).
5. **Saisie des gares** : « eze » doit trouver « Èze », « nice ville » doit trouver « Nice-Ville », et départ = arrivée doit être refusé avec un message clair. Tests dans les tâches 1 et 5 (`test_search_accent_insensitive`, `test_follow_same_station_rejected`).

---

## Structure des fichiers

```
monatrain/
  pyproject.toml  Dockerfile  docker-compose.yml  .env.example  .gitignore  .dockerignore  README.md
  scripts/capture_fixtures.py          # capture de vraies réponses SNCF → tests/fixtures
  src/monatrain/
    __init__.py
    config.py        # Settings.from_env()
    stations.py      # STATIONS, get_station, search_stations, uic_code
    timeutil.py      # PARIS, now_paris, parse_navitia_dt, format_navitia_dt, days masks
    models.py        # Status, TrainInfo, Fetched
    sncf/parse.py    # JSON Navitia → TrainInfo
    sncf/client.py   # SncfClient (HTTP, erreurs, cache SQLite, quota)
    db.py            # connect(), init_schema()
    repo.py          # Repo : users, followed, daily_state, quota, cache, kv
    budget.py        # QuotaBudget
    follow.py        # services partagés web/bot : list_trains_around, resolve_train, add_follow, today_statuses
    alerts.py        # decide() pur
    messages.py      # textes FR des DM
    watcher.py       # Watcher.tick()
    ratelimit.py     # SlidingWindowLimiter
    web/app.py  web/auth.py  web/routes.py
    web/templates/   base.html accueil.html trajet.html gare.html suivre.html _trains.html erreur.html
    web/static/      app.css app.js manifest.webmanifest icon-192.png icon-512.png
    bot/__main__.py  bot/client.py  bot/handlers.py  bot/notifier.py
  tests/
    conftest.py  fixtures/*.json  test_*.py
```

---

### Task 1 : Socle du projet, config, gares, temps *[haiku pour l'implémentation]*

**Files :**
- Create : `pyproject.toml`, `.gitignore`, `.dockerignore`, `.env.example`, `src/monatrain/__init__.py`, `src/monatrain/config.py`, `src/monatrain/stations.py`, `src/monatrain/timeutil.py`
- Test : `tests/test_stations.py`, `tests/test_timeutil.py`, `tests/test_config.py`

**Interfaces :**
- Produces :
  - `Settings` (dataclass gelée) : `sncf_token: str`, `discord_bot_token: str | None`, `discord_client_id: str | None`, `discord_client_secret: str | None`, `session_secret: str`, `public_url: str = "http://localhost:8000"`, `db_path: str = "data/monatrain.db"` ; `Settings.from_env(env: Mapping[str, str] | None = None) -> Settings` (charge `.env` via python-dotenv si présent, lève `ValueError` en nommant la variable si `SNCF_TOKEN` ou `SESSION_SECRET` manque).
  - `Station(id: str, name: str)` ; `STATIONS: tuple[Station, ...]` (les 29 de la spec, dans l'ordre géographique de la spec) ; `get_station(id) -> Station | None` ; `search_stations(query: str, limit: int = 25) -> list[Station]` (insensible à la casse, aux accents et aux tirets/espaces ; les correspondances en début de nom passent en premier) ; `uic_code(stop_id: str) -> str` (le nombre de 8 chiffres d'un id `stop_area:SNCF:87756403` ou `stop_point:SNCF:87756403:Train`).
  - `PARIS = ZoneInfo("Europe/Paris")` ; `now_paris() -> datetime` ; `parse_navitia_dt(s: str) -> datetime` ; `format_navitia_dt(dt: datetime) -> str` ; `WEEKDAYS=31, ALL_DAYS=127, WEEKEND=96` ; `day_bit(d: date) -> int` ; `runs_on(mask: int, d: date) -> bool` ; `parse_days(text: str) -> int` (accepte `semaine`, `tous`, `week-end`/`weekend`, ou une liste `lun,mer,ven` ; lève `ValueError` en français sinon) ; `format_days(mask: int) -> str` (`"lun-ven"`, `"tous les jours"`, `"week-end"`, sinon `"lun, mer, ven"`) ; `next_service_date(mask: int, hhmm: str, now: datetime) -> date` (aujourd'hui si l'heure n'est pas passée et que le jour est inclus, sinon le prochain jour inclus).

- [ ] **Step 1 : `git init`, puis créer `pyproject.toml` (paquet `src/`, deps de la Tech Stack, extra `dev` = pytest, pytest-asyncio ; `asyncio_mode = "auto"`), `.gitignore` (`.env`, `data/`, `.superpowers/`, `__pycache__/`, `.venv/`, `.pytest_cache/`), `.dockerignore` (les mêmes + `.git`, `tests/`, `docs/`) et `.env.example` (les 7 variables, vides).**

- [ ] **Step 2 : Écrire les tests (opus)**

```python
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
def test_navitia_dt_roundtrip_is_paris_aware():
    dt = parse_navitia_dt("20261106T164600"); assert dt.tzinfo is not None and dt.utcoffset() == timedelta(hours=1)
    assert format_navitia_dt(dt) == "20261106T164600"
def test_days_parse_and_format():
    assert parse_days("semaine") == 31 and parse_days("lun,mer,ven") == 1|4|16
    assert format_days(31) == "lun-ven" and format_days(127) == "tous les jours" and format_days(96) == "week-end"
    assert format_days(1|4|16) == "lun, mer, ven"
    with pytest.raises(ValueError): parse_days("lundi-bof")
def test_next_service_date():  # vendredi 2026-11-06 à 09:00, train 08:04 en semaine → lundi 09
    now = datetime(2026, 11, 6, 9, 0, tzinfo=PARIS)
    assert next_service_date(31, "08:04", now) == date(2026, 11, 9)
    assert next_service_date(31, "18:12", now) == date(2026, 11, 6)
def test_settings_missing_token_names_variable():
    with pytest.raises(ValueError, match="SNCF_TOKEN"): Settings.from_env({"SESSION_SECRET": "x"})
```

- [ ] **Step 3 : Lancer `pytest -q`, attendu : FAIL (imports introuvables).**
- [ ] **Step 4 : Implémenter `config.py`, `stations.py` et `timeutil.py` avec les signatures ci-dessus.** Normalisation de la recherche : `unicodedata.normalize("NFKD")`, suppression des diacritiques, minuscules, remplacement de `-` et `'` par des espaces.
- [ ] **Step 5 : Lancer `pytest -q`, attendu : PASS.**
- [ ] **Step 6 : Commit** : `git add -A && git commit -m "chore: socle projet, gares, temps, config"` (inclut la spec et ce plan).

---

### Task 2 : Modèles et parsing des réponses SNCF

**Files :**
- Create : `scripts/capture_fixtures.py`, `src/monatrain/models.py`, `src/monatrain/sncf/__init__.py`, `src/monatrain/sncf/parse.py`
- Test : `tests/fixtures/*.json`, `tests/test_parse.py`

**Interfaces :**
- Consumes : `parse_navitia_dt`, `uic_code` (Task 1).
- Produces :
  - `class Status(StrEnum)` : `ON_TIME="on_time"`, `DELAYED="delayed"`, `CANCELLED="cancelled"`, `REPLACED_BY_BUS="replaced_by_bus"`.
  - `@dataclass(frozen=True) TrainInfo` : `from_id: str`, `to_id: str | None`, `base_departure: datetime`, `departure: datetime`, `base_arrival: datetime | None`, `arrival: datetime | None`, `train_number: str`, `line: str`, `direction: str`, `physical_mode: str`, `status: Status`, `delay_min: int`, `reason: str | None`, `realtime: bool`.
  - `@dataclass(frozen=True) Fetched[T]` : `items: T`, `fetched_at: datetime`, `stale: bool`.
  - `parse_journeys(payload: dict, from_id: str, to_id: str) -> list[TrainInfo]` ; `parse_departures(payload: dict, stop_id: str) -> list[TrainInfo]`.

Règles de classement (dans `parse.py`, par ordre de priorité) :
1. `CANCELLED` si `journeys[].status == "NO_SERVICE"`, **ou** si une disruption liée au train (`display_informations.links[rel=disruptions].id` → `disruptions[]` racine) a `severity.effect == "NO_SERVICE"`, **ou** si cette disruption a un `impacted_stops[]` dont `uic_code(stop_point.id)` est celui de la gare de départ **ou** de la gare d'arrivée de l'utilisateur, avec `stop_time_effect == "deleted"`. Un arrêt supprimé ailleurs sur la ligne ne compte pas.
2. `REPLACED_BY_BUS` si `physical_mode` contient « autocar » ou « bus » (insensible à la casse).
3. `DELAYED` si `realtime` et `delay_min ≥ 1`, avec `delay_min = (departure - base_departure)` en minutes entières (R9). `realtime` vaut `data_freshness != "base_schedule"` : `adapted_schedule` est du temps réel.
4. Sinon `ON_TIME`. Si `data_freshness == "base_schedule"`, on a `realtime=False` et `ON_TIME`.

Un élément sans heure de départ exploitable est ignoré et journalisé, sans faire échouer le reste de la réponse.

Autres règles : `reason` = `messages[0].text` de la première disruption liée, sinon `None`. Si `departure_date_time` est absent, `departure = base_departure` et `delay_min = 0`. Les sections `crow_fly` sont ignorées. Pour `/departures`, `to_id=None` et `direction` = `display_informations.direction`.

- [ ] **Step 1 : Écrire `scripts/capture_fixtures.py` (lit `SNCF_TOKEN` via `Settings.from_env`, enregistre `journeys_monaco_nice.json`, `departures_monaco.json` et `departures_nice.json` dans `tests/fixtures/`) et le lancer une fois : `python scripts/capture_fixtures.py`. Vérifier qu'aucun fichier ne contient le token : `Select-String -Path tests\fixtures\*.json -Pattern $env:SNCF_TOKEN` ne doit rien renvoyer.**
- [ ] **Step 2 : Écrire les tests (opus).** À partir des fixtures réelles, Opus dérive des variantes minimales éditées à la main : `journey_delayed.json` (+8 min, motif « Réutilisation d'un train »), `journey_no_service.json` (sans `departure_date_time`), `journey_stop_deleted_at_arrival.json`, `journey_stop_deleted_elsewhere.json`, `journey_bus.json` (`physical_mode: "Autocar"`), `departure_base_schedule.json`.

```python
def test_delayed_journey():
    t = parse_journeys(load("journey_delayed.json"), MONACO, NICE)[0]
    assert t.status is Status.DELAYED and t.delay_min == 8 and t.reason == "Réutilisation d'un train"
    assert t.base_departure.strftime("%H:%M") == "08:04" and t.train_number == "86053"
def test_no_service_without_realtime_times():
    t = parse_journeys(load("journey_no_service.json"), MONACO, NICE)[0]
    assert t.status is Status.CANCELLED and t.departure == t.base_departure and t.delay_min == 0
def test_stop_deleted_at_users_arrival_is_cancelled():
    assert parse_journeys(load("journey_stop_deleted_at_arrival.json"), MONACO, NICE)[0].status is Status.CANCELLED
def test_stop_deleted_elsewhere_is_not_cancelled():
    assert parse_journeys(load("journey_stop_deleted_elsewhere.json"), MONACO, NICE)[0].status is not Status.CANCELLED
def test_bus_replacement():
    assert parse_journeys(load("journey_bus.json"), MONACO, NICE)[0].status is Status.REPLACED_BY_BUS
def test_base_schedule_is_on_time():
    t = parse_departures(load("departure_base_schedule.json"), MONACO)[0]
    assert t.status is Status.ON_TIME and t.realtime is False and t.to_id is None and t.direction
def test_real_fixtures_parse_without_error():
    assert parse_journeys(load("journeys_monaco_nice.json"), MONACO, NICE)
    assert parse_departures(load("departures_nice.json"), NICE)
```

- [ ] **Step 3 : Lancer `pytest tests/test_parse.py -q`, attendu : FAIL.**
- [ ] **Step 4 : Implémenter `models.py` et `sncf/parse.py` selon les règles ci-dessus.**
- [ ] **Step 5 : Lancer `pytest -q`, attendu : PASS.**
- [ ] **Step 6 : Commit** `feat: parsing des réponses SNCF`.

---

### Task 3 : Base SQLite, dépôt, budget de quota, client SNCF

**Files :**
- Create : `src/monatrain/db.py`, `src/monatrain/repo.py`, `src/monatrain/budget.py`, `src/monatrain/sncf/client.py`
- Test : `tests/conftest.py`, `tests/test_repo.py`, `tests/test_budget.py`, `tests/test_client.py`

**Interfaces :**
- Consumes : `TrainInfo`, `Fetched`, `parse_journeys`, `parse_departures` (Task 2) ; `PARIS`, `format_navitia_dt` (Task 1).
- Produces :
  - `connect(path: str) -> sqlite3.Connection` (WAL, `foreign_keys=ON`, `row_factory=sqlite3.Row`, `check_same_thread=False`, crée le dossier parent) ; `init_schema(conn) -> None` (idempotent).
  - Tables : `users(discord_id PK, username, avatar, dm_ok INT DEFAULT 1, created_at)` ; `followed(id PK, user_id FK, from_id, to_id, dep_time 'HH:MM', days INT, train_number, suggestion_sent INT DEFAULT 0, created_at, UNIQUE(user_id, from_id, to_id, dep_time))` ; `daily_state(followed_id FK ON DELETE CASCADE, service_date, found INT NULL, last_delay INT NULL, notified_status TEXT NULL, notified_delay INT NULL, PK(followed_id, service_date))` ; `quota(day, consumer, count, PK(day, consumer))` ; `api_cache(key PK, fetched_at, body)` ; `kv(key PK, value)`.
  - `@dataclass Follow` : `id, user_id, from_id, to_id, dep_time, days, train_number, suggestion_sent`. `@dataclass DailyState` : `found: bool | None`, `last_delay: int | None`, `notified_status: str | None`, `notified_delay: int | None`.
  - `class Repo(conn)` avec :
    - utilisateurs : `upsert_user(discord_id, username, avatar)`, `get_user(discord_id) -> dict | None`, `set_dm_ok(discord_id, ok: bool)` ;
    - trains suivis : `add_follow(user_id, from_id, to_id, dep_time, days, train_number) -> Follow` (lève `FollowLimitError` au-delà de **10**, `DuplicateFollowError` si doublon), `list_follows(user_id) -> list[Follow]`, `all_follows() -> list[Follow]`, `delete_follow(user_id, follow_id) -> bool` (seulement le propriétaire), `replace_dep_time(user_id, follow_id, dep_time, train_number) -> bool` (remet aussi `suggestion_sent` à 0), `mark_suggestion_sent(follow_id)` ;
    - état du jour : `get_daily(follow_id, d: date) -> DailyState`, `set_found(follow_id, d, found: bool)` (une fois à `True`, la valeur ne repasse plus à `False` ce jour-là), `set_last_delay(follow_id, d, delay: int)`, `set_notified(follow_id, d, status, delay)`, `missing_streak(follow_id) -> int` (nombre de lignes `found=0` consécutives les plus récentes) ;
    - quota : `quota_count(day: date, consumer: str | None = None) -> int`, `quota_month(year, month) -> int`, `quota_incr(day, consumer)` ;
    - cache : `cache_get(key) -> tuple[dict, datetime] | None`, `cache_put(key, body: dict, fetched_at)`, `cache_purge(older_than: datetime)` ;
    - divers : `kv_get(key) -> str | None`, `kv_set(key, value)`.
  - `class QuotaBudget(repo, *, daily_limit=5000, monthly_limit=150000, web_limit=3000)` : `allow(consumer: Literal["web","watcher"], now: datetime) -> bool`, `record(consumer, now)`, `block_until(dt)` (stocké en kv `quota_blocked_until`, utilisé après un 429). Règles : `watcher` est autorisé si le total du jour < 5000, le total du mois < 150000 et qu'aucun blocage n'est actif ; `web` doit en plus avoir son propre compteur du jour < 3000.
  - Exceptions : `SncfError`, `SncfAuthError(SncfError)` (401), `SncfQuotaError(SncfError)` (429 ou budget refusé sans cache), `SncfUnavailable(SncfError)` (timeout, 5xx, JSON invalide).
  - `class SncfClient(token, repo, budget, consumer: Literal["web","watcher"], *, http: httpx.AsyncClient | None = None, base_url=BASE_URL, fresh_ttl=60, clock=now_paris)` :
    - `async journeys(from_id, to_id, when: datetime, count: int = 8) -> Fetched[list[TrainInfo]]` (paramètres de la spec, annexe A ; `when` est tronqué à la minute pour la clé de cache) ;
    - `async departures(stop_id, count: int = 10) -> Fetched[list[TrainInfo]]` (le cache est arrondi à la minute courante).
    - Algorithme de `_get(path, params)` : (1) entrée en cache plus récente que 60 s → la renvoyer avec `stale=False` ; (2) sinon, si `budget.allow()` répond oui → appel HTTP, puis `budget.record`, `cache_put`, et renvoi avec `stale=False` ; (3) en cas de refus du budget, d'erreur 429 (avec `block_until` minuit Paris) ou d'erreur 5xx/timeout → renvoyer l'entrée en cache, même périmée, avec `stale=True`, sinon lever l'erreur correspondante ; (4) une erreur 401 lève toujours `SncfAuthError`. Le token n'apparaît dans aucun log ni message d'erreur.
  - `tests/conftest.py` fournit les fixtures `repo` (SQLite `:memory:` + schéma), `make_client(handler)` (client relié à un `httpx.MockTransport`) et `fixed_now`.

- [ ] **Step 1 : Écrire les tests (opus)**

```python
def test_follow_limit_10(repo): ...  # 10 ajouts OK, le 11e → FollowLimitError
def test_delete_follow_only_owner(repo): ...  # delete_follow("autre", id) is False
def test_missing_streak_counts_recent_consecutive(repo): ...  # found 1,0,0 → 2 ; 0,1 → 0
def test_found_true_is_sticky_for_the_day(repo): ...
def test_budget_web_cut_at_3000_watcher_continues(repo):
    for _ in range(3000): b.record("web", now)
    assert not b.allow("web", now) and b.allow("watcher", now)
def test_budget_daily_total_5000_blocks_all(repo): ...
def test_budget_block_until_after_429(repo): ...
async def test_basic_auth_and_params(make_client): ...  # Authorization = Basic base64(token + ":"), max_nb_transfers=0, direct_path=none, data_freshness=realtime
async def test_cache_hit_within_60s_makes_one_request(make_client): ...  # 2 appels → 1 requête HTTP, quota_count == 1
async def test_unavailable_serves_stale(make_client): ...  # cache âgé de 5 min + 503 → Fetched(stale=True)
async def test_429_without_cache_raises_and_blocks(make_client): ...
async def test_401_raises_auth_error_without_token_in_message(make_client): ...
```

- [ ] **Step 2 : Lancer `pytest -q`, attendu : FAIL.**
- [ ] **Step 3 : Implémenter `db.py`, `repo.py`, `budget.py` et `sncf/client.py` selon les interfaces.**
- [ ] **Step 4 : Lancer `pytest -q`, attendu : PASS.**
- [ ] **Step 5 : Commit** `feat: base SQLite, quota et client SNCF`.

---

### Task 4 : Décision d'alerte et messages

**Files :**
- Create : `src/monatrain/alerts.py`, `src/monatrain/messages.py`
- Test : `tests/test_alerts.py`, `tests/test_messages.py`

**Interfaces :**
- Consumes : `TrainInfo`, `Status` (Task 2) ; `DailyState` (Task 3) ; `get_station` (Task 1).
- Produces :
  - `class AlertKind(StrEnum)` : `DELAYED`, `CANCELLED`, `REPLACED_BY_BUS`.
  - `decide(state: DailyState, train: TrainInfo) -> AlertKind | None` (fonction pure). Règles :
    - `CANCELLED`, et le dernier statut notifié n'est pas `cancelled` → `CANCELLED` ;
    - `REPLACED_BY_BUS`, et le dernier statut notifié n'est ni `replaced_by_bus` ni `cancelled` → `REPLACED_BY_BUS` ;
    - `status == DELAYED`, `delay_min ≥ 5`, et (rien n'a été notifié, ou le dernier DM était `delayed` avec `delay_min ≥ notified_delay + 5`) → `DELAYED` ;
    - sinon `None`. Après un DM `cancelled` ou `replaced_by_bus`, un retard ne génère plus de DM.
  - `format_alert(kind: AlertKind, train: TrainInfo) -> str` ; `format_missing(follow: Follow, suggestion: TrainInfo | None) -> str`.

Textes exacts (le motif n'apparaît que s'il existe) :
- Retard : `⚠️ Retard · {Départ} → {Arrivée}\nTrain {n} prévu à {HH:MM}, départ estimé {HH:MM} (+{d} min).\nMotif : {reason}`
- Supprimé : `❌ Supprimé · {Départ} → {Arrivée}\nLe train {n} de {HH:MM} est supprimé.\nMotif : {reason}`
- Car : `🚌 Remplacé par car · {Départ} → {Arrivée}\nLe train {n} de {HH:MM} est remplacé par un car.\nMotif : {reason}`
- Introuvable avec proposition : `❓ Ton train {Départ} → {Arrivée} de {HH:MM} est introuvable depuis 2 jours (horaires modifiés ?).\nLe plus proche : {HH:MM} (train {n}). Tu veux le suivre à la place ?`
- Introuvable sans proposition : `❓ Ton train {Départ} → {Arrivée} de {HH:MM} est introuvable depuis 2 jours, et aucun train direct n'existe à ± 15 min. Tu peux le retirer avec /retirer.`

- [ ] **Step 1 : Écrire les tests (opus)** : `test_first_delay_5_alerts`, `test_delay_4_no_alert`, `test_delay_grows_by_5_realerts` (notifié 8, maintenant 12 → None ; 13 → DELAYED), `test_cancel_after_delay_alerts`, `test_cancel_once_only`, `test_delay_after_cancel_silent`, `test_bus_once`, `test_on_time_never_alerts`, `test_format_delay_exact_text`, `test_format_without_reason_has_no_motif_line`, `test_format_missing_with_and_without_suggestion`.
- [ ] **Step 2 : Lancer `pytest tests/test_alerts.py tests/test_messages.py -q`, attendu : FAIL.**
- [ ] **Step 3 : Implémenter `alerts.py` et `messages.py`.**
- [ ] **Step 4 : Lancer `pytest -q`, attendu : PASS.**
- [ ] **Step 5 : Commit** `feat: règles d'alerte et messages`.

---

### Task 5 : Services « suivre un train » partagés par le web et le bot

**Files :**
- Create : `src/monatrain/follow.py`
- Test : `tests/test_follow.py`

**Interfaces :**
- Consumes : `SncfClient.journeys` (Task 3, ou un faux objet ayant la même signature) ; `Repo` (Task 3) ; `next_service_date`, `runs_on`, `get_station` (Task 1).
- Produces :
  - `class FollowError(ValueError)`, dont le message en français est destiné à l'utilisateur.
  - `async list_trains_around(source, from_id, to_id, hhmm: str, on: date, count: int = 8) -> list[TrainInfo]` (journeys à `on` + `hhmm` − 15 min).
  - `async resolve_train(source, from_id, to_id, hhmm, days, now) -> TrainInfo` : cherche le train dont l'heure prévue est la plus proche de `hhmm` à ± 15 min, le jour `next_service_date`. Lève `FollowError("Aucun train direct trouvé autour de HH:MM.")` si rien ne correspond, et `FollowError("Les gares de départ et d'arrivée doivent être différentes.")` si départ = arrivée, ou si une gare est hors liste.
  - `async add_follow(repo, source, user_id, from_id, to_id, hhmm, days, now) -> Follow` : appelle `resolve_train`, enregistre l'heure **exacte** du train trouvé et son numéro, et convertit `FollowLimitError` / `DuplicateFollowError` en `FollowError` (« Tu suis déjà 10 trains. » / « Tu suis déjà ce train. »).
  - `@dataclass TodayItem` : `follow: Follow`, `state: Literal["upcoming","live","past","missing"]`, `train: TrainInfo | None`, `stale: bool`.
  - `async today_statuses(repo, source, user_id, now) -> list[TodayItem]` : liste les trains suivis qui circulent aujourd'hui, triés par heure. Appelle l'API **uniquement** pour les trains dont l'heure prévue est dans `[now − 30 min, now + 3 h]` (`live`). Les autres sont `upcoming` ou `past`, sans appel. Un train `live` absent de la réponse prend l'état `missing` si son `daily_state.found` vaut `False`, sinon `live` avec `train=None`. Un train dont l'heure est passée reste `missing` (sans appel) si son `found` du jour vaut `False`, sinon `past`.
  - `async nearest_alternative(source, follow, on: date) -> TrainInfo | None` (± 15 min, hors heure actuelle).

- [ ] **Step 1 : Écrire les tests (opus)**, avec un `FakeSource` qui enregistre ses appels : `test_resolve_exact_time`, `test_resolve_nearest_within_15_min_saves_exact_time` (saisie 08:00 → enregistré 08:04), `test_resolve_none_raises_french_message`, `test_follow_same_station_rejected`, `test_follow_unknown_station_rejected`, `test_add_follow_limit_message`, `test_today_statuses_only_calls_api_for_live_window` (3 trains à 06:00, 09:00 et 20:00, `now` = 08:30 → 1 seul appel), `test_today_statuses_skips_days_not_running`.
- [ ] **Step 2 : Lancer `pytest tests/test_follow.py -q`, attendu : FAIL.**
- [ ] **Step 3 : Implémenter `follow.py`.**
- [ ] **Step 4 : Lancer `pytest -q`, attendu : PASS.**
- [ ] **Step 5 : Commit** `feat: services de suivi de trains`.

---

### Task 6 : Watcher (surveillance et envoi des alertes)

**Files :**
- Create : `src/monatrain/watcher.py`
- Test : `tests/test_watcher.py`

**Interfaces :**
- Consumes : `Repo`, `SncfClient` (consommateur `"watcher"`) (Task 3) ; `decide`, `format_alert`, `format_missing` (Task 4) ; `nearest_alternative` (Task 5) ; `runs_on` (Task 1).
- Produces :
  - `class Notifier(Protocol)` : `async send(user_id: str, text: str, *, suggestion: tuple[int, TrainInfo] | None = None) -> bool`. La méthode renvoie `False` si le DM est refusé ; `suggestion` contient `(follow_id, train proposé)`.
  - `class Watcher(repo, source, notifier, *, poll_interval_s=120, window_before_min=60)` avec `async tick(now: datetime) -> None` et `async run_forever(interval_s=60)`. `run_forever` appelle `tick(now_paris())` toutes les 60 s, intercepte et logue toute exception, et appelle `repo.cache_purge(now − 24 h)` une fois par jour.

Algorithme de `tick(now)` :
1. Pour chaque `Follow` dont le jour de service *d* inclut un départ prévu `base = d + dep_time` qui vérifie `base − 60 min ≤ now ≤ base + max(daily_state.last_delay, 0)`, en considérant *d* = hier, aujourd'hui **et** demain (R15 : trains autour de minuit et changement d'heure), on regroupe par clé `(from_id, to_id, d, dep_time)`.
2. Pour chaque groupe dont la dernière interrogation date de plus de 120 s (mémoire du processus) : un seul `source.journeys(from_id, to_id, base − 1 min, count=4)`, puis on cherche le train dont l'heure de départ prévue vaut exactement `base`. Une `SncfError` est loguée, et on passe au groupe suivant.
3. Si le train est trouvé : `set_found(True)` et `set_last_delay(train.delay_min)` pour chaque abonné du groupe, puis `decide()`. Si une alerte en sort : `notifier.send`, puis `set_notified` **seulement si** l'envoi a réussi. En cas d'échec, `set_dm_ok(False)`. L'échec d'un abonné n'arrête pas les autres.
4. Fin de fenêtre (`now > base`) sans que le train ait jamais été vu ce jour-là : `set_found(False)` (une fois). Si `missing_streak ≥ 2` et `suggestion_sent == 0` : `nearest_alternative`, puis `send(format_missing(...), suggestion=...)` et `mark_suggestion_sent`.

- [ ] **Step 1 : Écrire les tests (opus)**, avec un `FakeSource` qui compte ses appels et un `FakeNotifier` qui enregistre les envois et peut refuser certains destinataires :
  - `test_not_watched_before_60_min` ;
  - `test_delay_alert_sent_once_then_on_growth` ;
  - `test_group_one_request_many_dms` (3 utilisateurs, un aux DM refusés : 1 appel API, 3 tentatives, 2 `set_notified`, `dm_ok=0` pour celui qui refuse) ;
  - `test_poll_interval_120s` (ticks à t et t + 60 s → 1 appel) ;
  - `test_window_across_midnight` (train 23:50 à +15 min, tick à 00:03 → toujours surveillé, pas de DM en double) ;
  - `test_tick_survives_source_error` ;
  - `test_missing_two_days_sends_one_suggestion` ;
  - `test_absent_train_never_sends_cancel` (un train simplement absent ne déclenche aucune alerte ; un train supprimé n'arrive au watcher que par le recoupement `base_schedule` du client, R25) ;
  - `test_day_not_in_mask_ignored`.
- [ ] **Step 2 : Lancer `pytest tests/test_watcher.py -q`, attendu : FAIL.**
- [ ] **Step 3 : Implémenter `watcher.py` selon l'algorithme.**
- [ ] **Step 4 : Lancer `pytest -q`, attendu : PASS.**
- [ ] **Step 5 : Commit** `feat: watcher d'alertes`.

---

### Task 7 : Site web : pages, recherche, suivi, limite par IP

**Files :**
- Create : `src/monatrain/ratelimit.py`, `src/monatrain/web/__init__.py`, `web/app.py`, `web/routes.py`, `web/templates/*.html`, `web/static/app.css`, `web/static/app.js`, `web/static/manifest.webmanifest`, les icônes
- Test : `tests/test_ratelimit.py`, `tests/test_web.py`

**Interfaces :**
- Consumes : `Settings` (Task 1), `Repo`, `connect`, `init_schema`, `QuotaBudget`, `SncfClient` (consommateur `"web"`) (Task 3) ; `follow.*` (Task 5) ; `STATIONS`, `search_stations`, `format_days` (Task 1).
- Produces :
  - `class SlidingWindowLimiter(limit: int, window_s: int, clock=time.monotonic)` avec `hit(key: str) -> bool`.
  - `create_app(settings: Settings | None = None, *, source=None, repo=None, http=None) -> FastAPI`. Les paramètres `source`, `repo` et `http` servent aux tests. L'application monte `SessionMiddleware(secret_key=settings.session_secret, https_only=PUBLIC_URL commence par https)` et les fichiers statiques. L'utilisateur courant est `request.session.get("uid")`.
  - Routes :

| Méthode et chemin | Rôle |
|---|---|
| `GET /` | Si l'utilisateur est connecté : `accueil.html` avec `today_statuses` et la recherche rapide préremplie avec le trajet de son premier train suivi. Sinon : redirection 303 vers `/trajet`. |
| `GET /trajet?de=&vers=&heure=` | Sélecteurs de gares avec un bouton ⇅ (lien qui inverse `de` et `vers`), puis la liste des trains (par défaut, l'heure actuelle). |
| `GET /gare?id=&vers=` | Tableau des départs de la gare. Les filtres rapides par direction sont des liens vers les terminus présents dans la réponse. |
| `GET /suivre` (connexion requise) | Formulaire : départ, arrivée, heure (`<input type=time>`), jours (préréglages et cases à cocher). |
| `POST /suivis` | Appelle `add_follow`. En cas de succès : redirection vers `/` avec un message flash « Enregistré : 08:04 (train 86053), lun-ven ». En cas de `FollowError` : le formulaire est réaffiché avec le message. |
| `POST /suivis/{id}/supprimer` | Supprime le train suivi. |
| `POST /suivis/{id}/remplacer` (champ `heure`) | Remplace l'heure d'un train suivi. |
| `GET /fragments/mes-trains`, `/fragments/trajet?…`, `/fragments/gare?…` | Renvoient le partiel `_trains.html` seul. `app.js` les recharge toutes les 60 s quand `document.visibilityState === "visible"`. |
| `GET /healthz` | Renvoie `{"ok": true}`. |

  - Badges dans `_trains.html` :
    - `À l'heure` (vert) ;
    - `+N min` (orange), avec l'heure prévue barrée ;
    - `Supprimé` (rouge) ;
    - `Remplacé par car` (rouge) ;
    - `Introuvable` (gris) ;
    - `Prévu` et `Parti` (gris) pour les états `upcoming` et `past`.
  - Le motif est affiché sous la ligne. Si `stale`, un bandeau indique « Données d'il y a X min ». Si `dm_ok == 0`, un bandeau indique « Le bot ne peut pas t'envoyer de DM » avec un lien d'installation.
  - **Limite par IP** (R18), deux seaux par fenêtre de 600 s : 30 requêtes sur `/trajet`, `/gare` et `POST /suivis` ; 120 requêtes sur `GET /` et `/fragments/*` (rafraîchissement automatique). La clé est l'en-tête `CF-Connecting-IP`, à défaut `request.client.host`. Au-delà : page `erreur.html` en 429 avec « Trop de recherches, réessaie dans quelques minutes. »
  - Une `SncfError` sans cache donne la page `erreur.html` en 503 avec « Horaires SNCF momentanément indisponibles. » Une `SncfAuthError` donne le même message côté utilisateur, plus un log d'erreur.
  - Mise en page conforme à la maquette C de la spec : en-tête bleu foncé `#0b3d91`, cartes arrondies, barre d'onglets fixe en bas (Accueil / Trajet / Gare), `<meta name="viewport">`, variables CSS clair et sombre, `lang="fr"`.

- [ ] **Step 1 : Écrire les tests (opus)**, avec `TestClient(create_app(settings, source=FakeSource(...), repo=repo))` :
  - `test_home_redirects_when_logged_out` ;
  - `test_home_lists_today_trains_with_badges` (on simule une session connectée en appelant une route de test, ou on signe le cookie avec `itsdangerous`) ;
  - `test_trajet_shows_trains_and_swap_link` ;
  - `test_gare_filters_by_direction` ;
  - `test_follow_post_success_and_error_message` ;
  - `test_delete_only_own_follow` ;
  - `test_rate_limit_31st_request_429_by_cf_ip` ;
  - `test_stale_banner` ;
  - `test_sncf_down_503_french` ;
  - `test_fragment_returns_partial_only` ;
  - `test_healthz` ;
  - `test_limiter_window_expires`.
- [ ] **Step 2 : Lancer `pytest tests/test_web.py tests/test_ratelimit.py -q`, attendu : FAIL.**
- [ ] **Step 3 : Implémenter `ratelimit.py`, `web/app.py`, `web/routes.py`, les templates et les fichiers statiques.**
- [ ] **Step 4 : Lancer `pytest -q`, attendu : PASS.**
- [ ] **Step 5 : Vérification manuelle** : `uvicorn monatrain.web.app:create_app --factory --reload`, ouvrir `http://localhost:8000/trajet?de=stop_area:SNCF:87756403&vers=stop_area:SNCF:87756056` en vue mobile (375 px), puis vérifier les thèmes clair et sombre et l'absence de défilement horizontal.
- [ ] **Step 6 : Commit** `feat: site web mobile`.

---

### Task 8 : Connexion avec Discord (OAuth2)

**Files :**
- Create : `src/monatrain/web/auth.py` ; Modify : `src/monatrain/web/app.py` (inclure le routeur)
- Test : `tests/test_auth.py`

**Interfaces :**
- Consumes : `Settings`, `Repo.upsert_user`, le paramètre `http` de `create_app` (Task 7).
- Produces : les routes suivantes.

| Méthode et chemin | Rôle |
|---|---|
| `GET /login` | Redirige vers `https://discord.com/oauth2/authorize` avec `client_id`, `response_type=code`, `redirect_uri={PUBLIC_URL}/auth/callback`, `scope="identify applications.commands"`, `integration_type=1` (installation de l'app sur le compte utilisateur, pour permettre les DM et les commandes), `prompt=none`, et un `state` aléatoire gardé en session. |
| `GET /auth/callback?code=&state=` | Vérifie le `state`, appelle `POST https://discord.com/api/oauth2/token`, puis `GET https://discord.com/api/users/@me`. Ensuite : `upsert_user`, `set_dm_ok(True)`, `session["uid"]=id`, et redirection vers `/`. Si le `state` est invalide ou que Discord renvoie une erreur : page d'erreur 400 « Connexion Discord impossible, réessaie. » |
| `POST /logout` | Vide la session et redirige vers `/trajet`. |

  - Si `DISCORD_CLIENT_ID` ou `DISCORD_CLIENT_SECRET` est absent, `/login` affiche « Connexion Discord non configurée » : le site reste utilisable en local sans Discord.
  - Lien d'installation affiché dans le bandeau DM : `https://discord.com/oauth2/authorize?client_id={id}&integration_type=1&scope=applications.commands`.

- [ ] **Step 1 : Écrire les tests (opus)**, avec `httpx.MockTransport` qui simule Discord : `test_login_redirect_has_scopes_and_state`, `test_callback_creates_user_and_session`, `test_callback_bad_state_400`, `test_callback_discord_error_400`, `test_logout_clears_session`, `test_login_unconfigured_message`.
- [ ] **Step 2 : Lancer `pytest tests/test_auth.py -q`, attendu : FAIL.**
- [ ] **Step 3 : Implémenter `web/auth.py`.**
- [ ] **Step 4 : Lancer `pytest -q`, attendu : PASS.**
- [ ] **Step 5 : Commit** `feat: connexion Discord`.

---

### Task 9 : App Discord : commandes slash et DM

**Files :**
- Create : `src/monatrain/bot/__init__.py`, `bot/handlers.py`, `bot/notifier.py`, `bot/client.py`, `bot/__main__.py`
- Test : `tests/test_bot_handlers.py`

**Interfaces :**
- Consumes : `follow.*` (Task 5) ; `Watcher`, `Notifier` (Task 6) ; `Repo`, `SncfClient` (Task 3), qui instancie **deux** clients : `"watcher"` pour le watcher et `"web"` pour les commandes, afin que les commandes consomment le budget web et jamais la réserve ; `SlidingWindowLimiter` (Task 7) ; `search_stations`, `parse_days`, `format_days` (Task 1).
- Produces :
  - `bot/handlers.py` : des fonctions pures et testables qui ne dépendent pas de discord.py. Chacune renvoie le texte de la réponse en français.

| Fonction | Rôle |
|---|---|
| `async handle_suivre(repo, source, limiter, user_id, username, depart_id, arrivee_id, heure, jours, jours_perso, now) -> str` | Enregistre un train suivi. Message de succès : « ✅ Je surveille le 08:04 Monaco Monte-Carlo → Nice-Ville (train 86053), lun-ven. » Si `dm_ok` vaut 0, ajoute « ⚠️ Je n'arrive pas à t'envoyer de DM : installe l'app sur ton compte : {lien} ». |
| `async handle_mes_trains(repo, source, limiter, user_id, now) -> str` | Une ligne par train : heure, trajet, jours, badge texte. Si aucun train : « Tu ne suis aucun train. Utilise /suivre. » |
| `handle_retirer(repo, user_id, follow_id) -> str` | Retire un train suivi. |
| `async handle_departs(repo, source, limiter, gare_id, vers_id: str | None, now) -> str` | Les 8 prochains trains. |
| `autocomplete_gare(current: str) -> list[tuple[str, str]]` | Gares correspondant à la saisie. |
| `async autocomplete_heure(source, depart_id, arrivee_id, current: str, now) -> list[tuple[str, str]]` | Libellés de la forme « 08:04 · train 86053 · arr. 08:26 ». Si `current` vaut `"8"` ou `"08"`, la recherche part de 08:00. Si une des deux gares manque, la liste est vide. |
| `autocomplete_mes_trains(repo, user_id) -> list[tuple[str, str]]` | Les trains suivis de l'utilisateur. |

  - Limite par utilisateur : 20 appels par fenêtre de 600 s pour les fonctions qui appellent l'API, avec le message « Doucement 🙂 réessaie dans quelques minutes. »
  - `bot/notifier.py` : `DiscordNotifier(client: discord.Client)` implémente `Notifier`. Il envoie le DM avec `user.send` et, pour une proposition, une `discord.ui.View` persistante avec les boutons « Suivre {HH:MM} » (`custom_id="replace:{follow_id}:{HH:MM}"`) et « Garder l'actuel ». `discord.Forbidden` et `discord.NotFound` donnent `False`.
  - `bot/client.py` : `MonatrainBot(discord.Client)` avec un `app_commands.CommandTree`. Les commandes `/suivre`, `/mes-trains`, `/retirer` et `/departs` sont déclarées avec `allowed_installs(guilds=True, users=True)` et `allowed_contexts(guilds=True, dms=True, private_channels=True)`, et répondent de façon éphémère. `jours` propose les choix `semaine`, `tous`, `week-end`, `perso`. `setup_hook` synchronise les commandes (globalement, ou sur un serveur de dev si `DISCORD_DEV_GUILD_ID` est défini), enregistre la vue persistante et lance `Watcher.run_forever()` en tâche de fond. Le bouton « Suivre » appelle `repo.replace_dep_time`, puis répond « ✅ C'est noté, je surveille maintenant le {HH:MM}. »
  - `python -m monatrain.bot` : lit `Settings`, appelle `init_schema`, et lance le bot. S'il manque `DISCORD_BOT_TOKEN`, il s'arrête avec un message clair.
  - Au premier `/suivre` d'un utilisateur, le bot envoie un DM de bienvenue (« Tes alertes arriveront ici 🚆 ») pour vérifier que les DM passent, et met à jour `dm_ok`.

- [ ] **Step 1 : Écrire les tests (opus)** sur `handlers.py` uniquement : `test_suivre_success_text`, `test_suivre_custom_days`, `test_suivre_error_passthrough`, `test_suivre_dm_warning_when_dm_blocked`, `test_mes_trains_empty_and_listed`, `test_retirer_other_users_train_refused`, `test_autocomplete_gare_accents`, `test_autocomplete_heure_from_partial_hour`, `test_user_rate_limit_message`.
- [ ] **Step 2 : Lancer `pytest tests/test_bot_handlers.py -q`, attendu : FAIL.**
- [ ] **Step 3 : Implémenter `handlers.py`, `notifier.py`, `client.py` et `__main__.py`.**
- [ ] **Step 4 : Lancer `pytest -q` (attendu : PASS), puis `python -c "import monatrain.bot.client"` (attendu : import sans erreur).**
- [ ] **Step 5 : Vérification manuelle (avec Thomas)** : créer l'application dans le portail Discord en suivant le README de la Task 10, lancer `python -m monatrain.bot`, puis taper `/suivre` en DM et contrôler l'autocomplétion et le DM de bienvenue. Vérifier aussi, selon les règles actuelles de Discord, que le DM fonctionne avec une **installation sur le compte utilisateur**, sans serveur commun. Si ce n'est pas le cas, documenter dans le README qu'il faut rejoindre un serveur où le bot est présent.
- [ ] **Step 6 : Commit** `feat: app Discord`.

---

### Task 10 : Docker, Cloudflare Tunnel, README *[haiku pour l'implémentation]*

**Files :**
- Create : `Dockerfile`, `docker-compose.yml`, `README.md` ; Modify : `.env.example` (ajouter `DISCORD_DEV_GUILD_ID`, optionnelle)
- Test : `tests/test_packaging.py`

**Interfaces :**
- Consumes : les points d'entrée `monatrain.web.app:create_app` (Task 7) et `python -m monatrain.bot` (Task 9).
- Produces :
  - **`Dockerfile`** :
    - image `python:3.12-slim`, `ENV TZ=Europe/Paris PYTHONUNBUFFERED=1` ;
    - `pip install --no-cache-dir .` (sans les dépendances de dev) ;
    - exécution en utilisateur non-root `app`, `WORKDIR /app`, dossier `/app/data` ;
    - `CMD` par défaut : `uvicorn monatrain.web.app:create_app --factory --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips="*"`.
  - **`docker-compose.yml`** :
    - service `web` : `build: .`, `env_file: .env`, volume `./data:/app/data`, ports `"127.0.0.1:8000:8000"`, `restart: unless-stopped`, healthcheck `python -c "import urllib.request;urllib.request.urlopen('http://localhost:8000/healthz')"` ;
    - service `bot` : même image, `command: python -m monatrain.bot`, même `env_file` et même volume, `restart: unless-stopped` ;
    - service `cloudflared` : `image: cloudflare/cloudflared:2025.10.0`, `command: tunnel --no-autoupdate run`, `environment: TUNNEL_TOKEN=${CLOUDFLARE_TUNNEL_TOKEN}`, `profiles: ["prod"]`, `depends_on: web`, `restart: unless-stopped`.
  - **`README.md`** en français, avec les commandes **PowerShell** pour le développement sous Windows et **bash** pour le serveur :
    - développement local : `python -m venv .venv`, `.venv\Scripts\pip install -e .[dev]`, `pytest`, `docker compose up --build` ;
    - création de l'application Discord : bot, *Installation* Guild + User, redirect `https://train.sbertacchi.dev/auth/callback` et `http://localhost:8000/auth/callback` ;
    - création du tunnel Cloudflare : Zero Trust → Tunnels → hostname public `train.sbertacchi.dev` → service `http://web:8000` ;
    - déploiement sur le ThinkCentre : `git clone`, copie du `.env`, `docker compose --profile prod up -d --build` ;
    - mise à jour : `git pull && docker compose --profile prod up -d --build` ;
    - sauvegarde : copie de `data/monatrain.db` ;
    - logs : `docker compose logs -f bot`.

- [ ] **Step 1 : Écrire les tests (opus)** : `test_env_ignored_by_git_and_docker` (`.env` figure dans `.gitignore` et dans `.dockerignore`), `test_compose_has_three_services_and_prod_profile` (lecture du YAML : services `web`, `bot` et `cloudflared`, `cloudflared` dans le profil `prod`, volume `./data` sur `web` et `bot`), `test_no_secret_literals_in_repo` (aucun fichier suivi par git ne contient la valeur de `SNCF_TOKEN` quand elle est définie dans l'environnement).
- [ ] **Step 2 : Lancer `pytest tests/test_packaging.py -q`, attendu : FAIL.**
- [ ] **Step 3 : Écrire `Dockerfile`, `docker-compose.yml` et `README.md`.**
- [ ] **Step 4 : Lancer `pytest -q` (attendu : PASS), puis `docker compose up -d --build web` et `curl http://localhost:8000/healthz` (attendu : `{"ok":true}`).**
- [ ] **Step 5 : Commit** `chore: Docker, tunnel Cloudflare, README`.

---

### Task 11 : Vérification finale (opus)

- [ ] **Step 1 :** Lancer `pytest -q`. Attendu : tous les tests passent.
- [ ] **Step 2 :** Lancer `docker compose up -d --build`. Attendu : `web` passe à l'état *healthy* et `bot` se connecte. Vérifier dans les logs le message « Commandes synchronisées » et l'absence de traceback.
- [ ] **Step 3 :** Faire relire l'ensemble de la branche par un agent **`opus`**. Il compare le code à la spec, en particulier aux anti-objectifs (quota, spam, fausses suppressions, secrets) et aux cinq points de la Review Focus. Ses constats repartent vers un agent `sonnet` pour correction, puis Opus revérifie.
- [ ] **Step 4 :** Faire un essai réel avec Thomas : suivre son 8h04 Monaco → Nice, puis contrôler l'accueil sur téléphone et `/mes-trains`.

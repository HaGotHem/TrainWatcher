# Monatrain — document de conception

Date : 2026-11-06 · Auteur : Thomas (avec Claude)

## 1. Intention

Thomas prend les TER ZOU! de la Côte d'Azur (ex. **le 8h04 Monaco → Nice** en semaine) et en a assez d'ouvrir l'app SNCF pour savoir si son train est à l'heure. Il veut :

1. **Un site web mobile first** pour consulter les horaires en temps réel (retards, suppressions) sans l'app SNCF.
2. **Une app / un bot Discord** qui lui envoie **un DM** quand un de ses trains suivis est **en retard ou supprimé**, et qui permet de **configurer ses trains par commandes slash**.
3. **Plusieurs utilisateurs** (amis, collègues) : chacun a ses propres trains et reçoit ses propres DM.
4. Le tout **dans Docker**, déployé sur son serveur maison (ThinkCentre i7), exposé sur **`train.sbertacchi.dev`**.

Ce qui rend le projet utile : on regarde **ses** trains, pas un tableau générique, et on est **prévenu** sans rien ouvrir.

## 2. Objectifs, non-objectifs, anti-objectifs

**Objectifs**
- Voir en un coup d'œil, sur téléphone, l'état du jour de ses trains suivis.
- Chercher les trains directs A → B et le tableau des départs d'une gare de la Côte d'Azur.
- Suivre des **trains récurrents** (gare départ, gare arrivée, heure de départ prévue, jours de circulation), depuis le site **ou** Discord. Plusieurs trains par utilisateur.
- Recevoir un DM Discord en cas de retard ≥ 5 min ou de suppression.
- Se connecter au site avec Discord (pas de mot de passe).

**Non-objectifs (pas maintenant)**
- Gares hors Côte d'Azur, trajets avec correspondance.
- Trains opérés par Transdev (TRSI) absents de l'API SNCF.
- Notifications push navigateur, e-mail, SMS.
- Historique / statistiques de ponctualité, interface d'administration.
- Suivi d'un train ponctuel « pour un jour donné » (uniquement récurrent).

**Anti-objectifs (échec même si « ça marche »)**
- **Épuiser le quota SNCF** (5 000 req/jour, 150 000/mois) → le site et les alertes tombent.
- **Spammer** les utilisateurs de DM (messages répétés pour le même état, messages « tout va bien »).
- **Fausses alertes de suppression** : annoncer un train supprimé alors qu'il roule.
- **Fuite de secrets** : token SNCF, token bot, secret OAuth dans le code, dans git ou dans l'image Docker.

## 3. Contraintes

- **API SNCF (Navitia)** : `https://api.sncf.com/v1/coverage/sncf`, auth HTTP Basic (token en username, mot de passe vide). Token validé le 2026-11-06 (HTTP 200). Gérer 401 (token invalide) et 429 (quota) proprement. Rafraîchissement ≥ 60 s. Dates `YYYYMMDDTHHMMSS` en heure locale Europe/Paris. Structure des réponses : voir annexe A.
- **Secrets** uniquement dans `.env` (racine, déjà présent avec `SNCF_TOKEN`), jamais en dur. `.env` dans `.gitignore` **et** `.dockerignore`.
- **Docker obligatoire** (décision de Thomas) : `docker compose up -d` doit suffire sur le serveur.
- Développement sur **Windows** (PowerShell) ; fournir les commandes Windows dans le README. Prod : Linux sur le ThinkCentre (Docker).
- Domaine `sbertacchi.dev` déjà géré par **Cloudflare**.
- Interface **en français**.
- Règle de travail de Thomas : les tests sont écrits **avant** l'implémentation par un agent Opus ; l'implémenteur (Sonnet/Haiku) ne modifie pas ces tests.

## 4. Ce qui a été décidé

### 4.1 Site web (décidé avec Thomas, maquette « C · Mes trains d'abord »)

- **Accueil connecté** : liste des trains suivis **du jour** (ceux dont les jours de circulation incluent aujourd'hui), chacun avec heure prévue / réelle, trajet, jours, et un badge : `À l'heure` (vert), `+N min` (orange), `Supprimé` (rouge), plus le motif de la perturbation quand l'API le fournit. En dessous : une **recherche rapide A → B** préremplie avec le trajet du premier train suivi, et un bouton **« + Suivre un train »**.
- **Accueil non connecté** : ouvre l'onglet **Trajet** + bouton « Se connecter avec Discord ».
- **Onglet Trajet** : sélecteurs gare de départ / gare d'arrivée (bouton ⇅ pour inverser), « Maintenant » ou heure choisie, liste des prochains **trains directs** avec statut.
- **Onglet Gare** : choix d'une gare, tableau des départs, filtres rapides par direction.
- **Suivre un train** : choisir départ, arrivée, puis un train dans la liste des horaires (ce qui fixe l'heure), puis les jours (lun-ven par défaut ; tous les jours ; week-end ; sélection libre). Possibilité de supprimer un train suivi.
- Barre d'onglets en bas (Accueil / Trajet / Gare).

### 4.2 Discord (décidé avec Thomas)

- **Une seule application Discord** : sert à la fois de bot (DM + commandes) et de fournisseur de connexion OAuth2 pour le site. L'identifiant Discord de l'utilisateur relie le site et le bot : les trains suivis sont les mêmes des deux côtés.
- **Commandes slash** (utilisables en DM avec le bot et sur un serveur) :
  - `/suivre` : départ, arrivée, heure (avec autocomplétion sur les horaires réels), jours.
  - `/mes-trains` : liste de ses trains suivis avec leur état du jour.
  - `/retirer` : arrêter de suivre un train (autocomplétion sur ses trains).
  - `/departs` : prochains départs d'une gare (ou A → B).
- Les réponses aux commandes sont **éphémères** (visibles seulement par l'utilisateur) quand elles sont tapées sur un serveur.

### 4.3 Règles d'alerte (proposées par Claude, validées par Thomas)

- Surveillance d'un train à partir de **1 h avant son départ prévu**, jusqu'à son départ réel.
- **DM** dès que le retard atteint **≥ 5 min**, ou si le train est **supprimé**.
- **Nouveau DM** seulement si l'état s'aggrave nettement (retard qui augmente encore de ≥ 5 min par rapport au dernier DM envoyé) ou si le train passe à supprimé.
- **Aucun message quand tout va bien.**
- Le DM contient : trajet, n° de train, heure prévue → heure estimée, retard, motif si connu.

### 4.4 Gares (décidé avec Thomas : uniquement la Côte d'Azur)

Liste fixe, codée dans l'app (identifiants vérifiés via `/places` le 2026-11-06) :

| Gare | ID |
|---|---|
| Théoule-sur-Mer | `stop_area:SNCF:87757583` |
| Mandelieu-la-Napoule | `stop_area:SNCF:87757591` |
| Cannes La Bocca | `stop_area:SNCF:87757617` |
| Cannes | `stop_area:SNCF:87757625` |
| Le Bosquet | `stop_area:SNCF:87757757` |
| Ranguin | `stop_area:SNCF:87757732` |
| Mouans-Sartoux | `stop_area:SNCF:87401323` |
| Grasse | `stop_area:SNCF:87757724` |
| Golfe-Juan – Vallauris | `stop_area:SNCF:87757641` |
| Juan-les-Pins | `stop_area:SNCF:87757666` |
| Antibes | `stop_area:SNCF:87757674` |
| Biot | `stop_area:SNCF:87757690` |
| Villeneuve-Loubet – Plage | `stop_area:SNCF:87756304` |
| Cagnes-sur-Mer | `stop_area:SNCF:87756320` |
| Cros-de-Cagnes | `stop_area:SNCF:87756338` |
| Saint-Laurent-du-Var | `stop_area:SNCF:87756346` |
| Nice Saint-Augustin | `stop_area:SNCF:87756254` |
| Nice-Ville | `stop_area:SNCF:87756056` |
| Nice Riquier | `stop_area:SNCF:87756353` |
| Villefranche-sur-Mer | `stop_area:SNCF:87756361` |
| Beaulieu-sur-Mer | `stop_area:SNCF:87756379` |
| Èze | `stop_area:SNCF:87756387` |
| Cap-d'Ail | `stop_area:SNCF:87756395` |
| Monaco Monte-Carlo | `stop_area:SNCF:87756403` |
| Carnolès | `stop_area:SNCF:87756478` |
| Roquebrune-Cap-Martin | `stop_area:SNCF:87756460` |
| Menton | `stop_area:SNCF:87756486` |
| Menton Garavan | `stop_area:SNCF:87756494` |
| Vintimille | `stop_area:SNCF:83045013` |

(Nice-Ville et Vintimille viennent du brief de Thomas, déjà vérifiés ; `/places?q=Vintimille` ne renvoie rien.)

### 4.5 Déploiement (décidé avec Thomas)

- Serveur : **ThinkCentre i7** à la maison, Docker.
- Exposition publique via **Cloudflare Tunnel** (conteneur `cloudflared` dans le compose, token de tunnel dans `.env`) : pas de port ouvert sur la box, HTTPS automatique.
- URL publique : **`https://train.sbertacchi.dev`** (sous-domaine choisi par Claude, modifiable via la config).
- En local (dev, Windows) : le même compose tourne sans le tunnel, site sur `http://localhost:8000`.

## 5. Choix techniques faits par Claude (Thomas a délégué, sauf Docker)

- **Python 3.12**, un seul paquet `monatrain` partagé par deux services lancés depuis **la même image Docker** :
  - `web` : **FastAPI** + templates **Jinja2** rendus côté serveur, un peu de JavaScript natif pour rafraîchir les statuts toutes les ~60 s via un endpoint JSON. Pas de framework front ni d'étape de build JS.
  - `bot` : **discord.py** (commandes slash + DM) **et** la boucle de surveillance des trains (watcher).
  - `cloudflared` : uniquement en prod (profil compose `prod`).
- **Base SQLite** (mode WAL) sur un volume `./data`, partagée par `web` et `bot`. Tables minimales : utilisateurs (id Discord, pseudo, avatar), trains suivis (utilisateur, gare départ, gare arrivée, heure prévue `HH:MM`, jours), état de notification par train suivi et par jour (dernier statut, dernier retard notifié), compteur de requêtes SNCF par jour.
- **Client SNCF** unique dans le paquet : n'extrait que les champs utiles, gère 401 / 429 / timeouts, cache court (≥ 60 s) des réponses identiques, et compte chaque requête dans SQLite.
- **Économie de quota** : le watcher regroupe les trains suivis identiques (même départ, arrivée, heure, jour) → **une seule requête pour tous les utilisateurs** qui suivent ce train. Le watcher interroge chaque train « actif » au plus toutes les 2 min. Un **budget journalier** garde une réserve pour les alertes : si le compteur approche la limite, le site affiche les données en cache plutôt que de refaire des appels.
- **Accès : ouvert à tous** (décidé par Thomas). N'importe quel compte Discord peut se connecter et suivre des trains ; la recherche Trajet / Gare est publique. Conséquences (choix de Claude) :
  - le cache des réponses SNCF est **partagé entre tous les visiteurs** : une même recherche (même gare ou même A → B, même minute) ne coûte qu'une requête, quel que soit le nombre de visiteurs ;
  - **limite par IP** sur les endpoints qui appellent l'API (ex. 30 recherches / 10 min) et **limite par utilisateur** sur les commandes Discord ;
  - **10 trains suivis maximum par utilisateur** ;
  - **réserve de quota** : 2 000 requêtes/jour sont réservées au watcher ; au-delà de 3 000 requêtes « site » dans la journée, le site ne sert plus que du cache (avec un message « données d'il y a X min ») ;
  - l'app Discord est **installable par chaque utilisateur** (user install) **et** sur des serveurs ; le constructeur vérifie que l'une ou l'autre permet bien au bot d'envoyer des DM (voir § 6).
- **Token SNCF dédié à Monatrain** (confirmé par Thomas) : le compteur SQLite est la référence du budget journalier (remis à zéro à minuit, heure de Paris) et mensuel. Un 429 reste géré (passage en mode cache seul + log), mais il ne devrait pas arriver en temps normal.
- **Identification d'un train récurrent** : par (gare départ, gare arrivée, heure de départ **prévue**). Le n° de train est enregistré à titre indicatif mais ne sert pas de clé (il peut changer selon les jours).
- **Détection des suppressions** : un train est déclaré supprimé seulement sur un signal explicite de l'API (`journeys[].status == NO_SERVICE`, ou une disruption `NO_SERVICE` / `stop_time_effect == "deleted"` qui le concerne). **Un train simplement absent des résultats ne déclenche pas d'alerte** (anti-objectif : fausses alertes) ; c'est journalisé.
- **Ce qui compte comme « Supprimé » pour un utilisateur** (choix de Claude) : le train est supprimé **pour lui** si `NO_SERVICE` vise ce train, ou si **sa gare de départ ou sa gare d'arrivée** est `deleted` (ex. terminus avancé). Une suppression d'arrêt **ailleurs** sur la ligne n'est pas une suppression pour lui. Si l'API montre un **car de substitution** (mode commercial autocar/bus à la place du train), on envoie un DM distinct « Remplacé par car », pas « Supprimé ».
- **Changements d'horaires** (choix de Claude ; le service annuel change mi-décembre) : correspondance stricte sur l'heure prévue. Si un train suivi est **introuvable un jour où il devrait circuler**, sans perturbation qui l'explique, il est marqué « Introuvable » sur le site et dans `/mes-trains`. Après **2 jours prévus consécutifs** introuvable, **un seul DM** prévient l'utilisateur et propose le train le plus proche (± 15 min) sur le même trajet, à accepter d'un clic (bouton Discord) ou depuis le site.
- **Connexion web** : OAuth2 Discord (scope `identify`), session dans un cookie signé (`SessionMiddleware` de Starlette, secret dans `.env`).
- **Fuseau** : `TZ=Europe/Paris` dans les conteneurs, toutes les heures manipulées en heure locale de Paris.
- **Thème** clair et sombre selon le réglage du téléphone ; **manifest PWA** pour « ajouter à l'écran d'accueil » (pas de mode hors ligne).
- **Configuration** (`.env`, avec un `.env.example` commité sans valeurs) : `SNCF_TOKEN`, `DISCORD_BOT_TOKEN`, `DISCORD_CLIENT_ID`, `DISCORD_CLIENT_SECRET`, `SESSION_SECRET`, `PUBLIC_URL`, `CLOUDFLARE_TUNNEL_TOKEN`.
- **Tests** : pytest, réponses SNCF simulées à partir de vraies réponses allégées (fixtures), aucune requête réseau réelle dans les tests.
- Dépôt git à initialiser dans `monatrain/` avec `.gitignore` (`.env`, `data/`, `.superpowers/`, caches Python).

## 6. Laissé au constructeur

- Arborescence exacte du paquet, noms des modules, schéma SQL précis.
- Mise en page et styles exacts (en suivant la maquette C : cartes arrondies, badges de couleur, barre d'onglets en bas, bleu foncé en en-tête).
- Comment enregistrer les commandes slash (globales ou par serveur en dev).
- Le détail de l'autocomplétion des horaires dans `/suivre`.
- Comment garantir la réception des DM : selon les règles Discord, le bot ne peut écrire en DM qu'à un utilisateur avec qui il partage un serveur ou qui a installé l'app. Le constructeur vérifie la règle actuelle et prévoit le cas d'échec (le site affiche un avertissement avec un lien d'installation ; `/suivre` le signale aussi).
- La documentation pas à pas dans le README : créer l'application Discord (bot + OAuth2, URL de redirection), créer le tunnel Cloudflare et le relier à `train.sbertacchi.dev`, lancer en local et en prod.

## Annexe A — Structure de l'API SNCF (constatée)

- **Départs** : `/stop_areas/{id}/departures?data_freshness=realtime&count=10`. `departures[].stop_date_time.base_departure_date_time` (prévu), `.departure_date_time` (réel), `.data_freshness` (`realtime` | `base_schedule` = pas d'info temps réel, a priori à l'heure). `display_informations` : `label` (ligne, ex. C3), `trip_short_name` (n° de train), `direction`, `network`, `commercial_mode`, `links[]` avec `rel == "disruptions"` → `id` d'un élément de `disruptions[]` à la racine.
- **Trajets** : `/journeys?from={id}&to={id}&datetime=YYYYMMDDTHHMMSS&datetime_represents=departure&data_freshness=realtime&max_nb_transfers=0&direct_path=none&count=8`. `journeys[].status` : `""` (normal), `SIGNIFICANT_DELAYS`, `NO_SERVICE`. Infos du train dans `sections[]` où `type == "public_transport"` : `base_departure_date_time`, `departure_date_time`, `base_arrival_date_time`, `arrival_date_time`, `data_freshness`, `display_informations`. Les sections `crow_fly` sont à ignorer.
- **Perturbations** : `disruptions[]` à la racine : `id`, `severity.effect`, `messages[0].text` (motif), `impacted_objects[].impacted_stops[]` avec `base_departure_time` / `amended_departure_time` (`HHMMSS`) et `stop_time_effect` (`delayed`, `deleted`, …). Endpoint dédié : `/stop_areas/{id}/disruptions`.
- **Point d'attention** : un train supprimé peut ne pas apparaître dans `/departures` → croiser avec les disruptions.

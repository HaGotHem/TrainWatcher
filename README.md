# Monatrain

Suivi des TER de la Côte d'Azur : un site web et un bot Discord.

- **Site** (`train.sbertacchi.dev`) : horaires en direct des gares, recherche de trajets, connexion avec Discord pour suivre ses trains.
- **Bot Discord** : envoie un DM quand un train suivi est en retard (à partir de 5 min) ou supprimé, et se configure par commandes slash.

Le tout tourne dans Docker : un service `web` (site), un service `bot` (commandes Discord et surveillance des trains) et, en production, un service `cloudflared` (tunnel Cloudflare qui expose le site sans ouvrir de port sur la box).

## Prérequis

- Python 3.12 ou plus (développement sous Windows).
- Docker avec le plugin `docker compose` (Docker Desktop sous Windows, Docker Engine sous Linux).
- Un token de l'API SNCF (Navitia).
- Une application Discord (voir [Création de l'application Discord](#création-de-lapplication-discord)).
- Pour la production : un compte Cloudflare qui gère le domaine `sbertacchi.dev`.

## Développement local

Les commandes ci-dessous sont à lancer dans **PowerShell**, à la racine du projet.

```powershell
python -m venv .venv
.venv\Scripts\pip install -e ".[dev]"
.venv\Scripts\python -m pytest
```

Pour lancer le site et le bot dans Docker (sans le tunnel) :

```powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }   # ne jamais écraser un .env existant
# compléter le fichier .env (voir ci-dessous)
docker compose up --build
```

Le site est alors disponible sur `http://localhost:8000`. Le bot exige `DISCORD_BOT_TOKEN` : tant qu'il n'est pas renseigné, le service `bot` s'arrête et redémarre en boucle.

## Configuration du `.env`

Le fichier `.env` contient les secrets. Il n'est jamais versionné (il figure dans `.gitignore` et `.dockerignore`). Partir du modèle commité, sans écraser un `.env` déjà rempli (il contient déjà `SNCF_TOKEN`, par exemple) :

```bash
[ -f .env ] || cp .env.example .env
```

| Variable | Obligatoire | Rôle |
|---|---|---|
| `SNCF_TOKEN` | oui | Token de l'API SNCF (authentification HTTP Basic, mot de passe vide). |
| `DISCORD_BOT_TOKEN` | oui | Token du bot Discord. |
| `DISCORD_CLIENT_ID` | oui | Identifiant de l'application Discord (connexion au site). |
| `DISCORD_CLIENT_SECRET` | oui | Secret OAuth2 de l'application Discord. |
| `SESSION_SECRET` | oui | Clé de signature des sessions du site. |
| `PUBLIC_URL` | oui en production | Adresse publique du site, **sans slash final** : `https://train.sbertacchi.dev`, ou `http://localhost:8000` en local. |
| `CLOUDFLARE_TUNNEL_TOKEN` | oui en production | Token du tunnel Cloudflare. |
| `DISCORD_DEV_GUILD_ID` | non | Identifiant d'un serveur Discord de test (voir ci-dessous). Laissé vide, les commandes slash sont globales et peuvent mettre jusqu'à une heure à apparaître. **À laisser vide en production** : renseigné, les commandes n'existent que dans ce serveur et ne fonctionnent plus en DM ni en installation utilisateur. |

Générer la valeur de `SESSION_SECRET` (PowerShell sous Windows) :

```powershell
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Sur le serveur (bash) :

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

### Serveur de développement Discord (facultatif)

`DISCORD_DEV_GUILD_ID` est optionnel. Son intérêt : les commandes slash y apparaissent immédiatement, alors qu'une synchronisation globale peut prendre jusqu'à une heure. Pour l'utiliser, après avoir créé l'application (voir la section Création de l'application Discord) :

1. Installer d'abord l'application sur ce serveur : dans le Developer Portal, **Installation → Guild Install**, ouvrir le lien généré et choisir le serveur.
2. Activer le mode développeur de Discord : **Paramètres → Avancés → Mode développeur**.
3. Faire un clic droit sur le serveur → **Copier l'identifiant du serveur**, et coller la valeur dans `DISCORD_DEV_GUILD_ID`.

**Attention :** si l'application n'est pas installée sur ce serveur, le bot échoue au démarrage.

**Attention :** `PUBLIC_URL` doit être exactement l'adresse du site, **sans slash final**. Avec `https://train.sbertacchi.dev/`, l'adresse de retour OAuth devient `https://train.sbertacchi.dev//auth/callback`, et la connexion Discord échoue.

## Création de l'application Discord

Dans le [Developer Portal Discord](https://discord.com/developers/applications) :

1. **New Application** : donner un nom à l'application (par exemple « Monatrain »).
2. **Bot** : cliquer sur **Reset Token**, copier le token dans `DISCORD_BOT_TOKEN`.
3. **OAuth2 → General** :
   - copier le **Client ID** dans `DISCORD_CLIENT_ID` ;
   - cliquer sur **Reset Secret**, copier le **Client Secret** dans `DISCORD_CLIENT_SECRET` ;
   - dans **Redirects**, ajouter les deux adresses de retour :
     - `https://train.sbertacchi.dev/auth/callback`
     - `http://localhost:8000/auth/callback`
4. **Installation** :
   - cocher les deux contextes d'installation : **Guild Install** (serveurs) et **User Install** (utilisateurs) ;
   - scopes : `applications.commands`, et `bot` pour l'installation sur un serveur.

Se connecter au site avec Discord, ou ouvrir le lien **User Install**, installe l'application sur ton compte : c'est ce qui active les commandes et les DM.

## Tunnel Cloudflare

Le tunnel relie le domaine public au service `web` sans ouvrir de port sur la box.

1. Ouvrir le tableau de bord **Zero Trust** de Cloudflare, puis **Networks → Tunnels**.
2. Cliquer sur **Create a tunnel**, choisir **Cloudflared**, donner un nom au tunnel.
3. Copier le token affiché dans la commande d'installation (la valeur qui suit `cloudflared service install`) et le placer dans `CLOUDFLARE_TUNNEL_TOKEN`.
4. Dans l'onglet **Public Hostname**, ajouter :
   - sous-domaine : `train` ; domaine : `sbertacchi.dev` (soit `train.sbertacchi.dev`) ;
   - type de service : `HTTP` ;
   - URL : `web:8000` (le nom du service dans le compose, joignable par `cloudflared` sur le réseau Docker).

## Déploiement sur le ThinkCentre

Sur le serveur, sous **bash** :

```bash
git clone <url-du-depot> monatrain
cd monatrain
cp .env.example .env
nano .env                     # renseigner toutes les variables
mkdir -p data                 # base SQLite, montée dans les conteneurs
docker compose --profile prod up -d --build
```

Le conteneur tourne sous l'utilisateur `app` (UID 1000) et doit pouvoir écrire dans `data/`. Si l'UID de ton utilisateur n'est pas 1000, lancer `sudo chown -R 1000:1000 data`.

Vérifier que les trois services tournent :

```bash
docker compose --profile prod ps
```

## Mise à jour

```bash
git pull && docker compose --profile prod up -d --build
```

## Sauvegarde

La base SQLite est `data/monatrain.db`, en mode WAL : le site et le bot y écrivent en continu. Ne pas copier ce fichier directement, car les écritures récentes peuvent se trouver dans le fichier `-wal` et être absentes de la copie.

La sauvegarde utilise l'API de sauvegarde en ligne de SQLite. Elle se lance dans le conteneur `web` pendant qu'il tourne, sans l'arrêter :

```bash
mkdir -p ~/sauvegardes
docker compose exec web python -c "import sqlite3; s=sqlite3.connect('data/monatrain.db'); d=sqlite3.connect('data/sauvegarde.db'); s.backup(d); d.close(); s.close()"
mv data/sauvegarde.db ~/sauvegardes/monatrain-$(date +%F).db
```

Le fichier final est dans `~/sauvegardes`, hors du dépôt. Le fichier temporaire `data/sauvegarde.db` est déplacé immédiatement.

## Logs

```bash
docker compose logs -f bot
```

Pour le site : `docker compose logs -f web`. Pour le tunnel : `docker compose --profile prod logs -f cloudflared`.

## Quota de l'API SNCF

L'API SNCF est limitée à 5 000 requêtes par jour et 150 000 par mois. Le budget est réparti ainsi :

- 2 000 requêtes par jour sont réservées au watcher, qui surveille les trains suivis (alertes) ;
- au-delà de 3 000 requêtes « web » dans la journée, le site ne sert plus que les données en cache (avec la mention de leur âge) ;
- le cache est rafraîchi au plus toutes les 60 secondes et conservé 24 h pour le repli ;
- chaque requête temps réel de trajets ou de départs est doublée d'un recoupement avec l'horaire théorique (`base_schedule`, mis en cache 5 minutes) pour retrouver les trains supprimés, que le temps réel ne renvoie plus.

Un même train suivi par plusieurs utilisateurs ne consomme qu'une seule requête.

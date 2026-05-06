# VintedBot Discord

Bot Discord qui surveille Vinted et envoie une notification en temps quasi-réel dès qu'un nouvel article correspond à vos filtres.

## Fonctionnalités

- **Surveillance multi-filtres** : autant de filtres que nécessaire, chacun dans son propre salon
- **Anti-ban** : délai configurable entre les sondages, jitter aléatoire, rotation des User-Agents, backoff automatique sur rate-limit
- **Initialisation propre** : pas de spam au premier démarrage — les articles existants sont ignorés
- **Multi-pays** : France, Belgique, Allemagne, Espagne, Italie, Pologne, Royaume-Uni, et plus
- **Embeds riches** : photo, prix, taille, marque, état, lieu, score vendeur

## Slash commands

| Commande | Description |
|----------|-------------|
| `/ajouter-filtre` | Crée une surveillance (nécessite la perm *Gérer les salons*) |
| `/liste-filtres` | Affiche tous les filtres du serveur |
| `/supprimer-filtre id` | Supprime un filtre |
| `/tester-filtre id` | Prévisualise 3 articles sans affecter la BDD |
| `/aide` | Affiche l'aide |

## Installation rapide

### 1. Créer un bot Discord

1. Rendez-vous sur [discord.com/developers/applications](https://discord.com/developers/applications)
2. Créez une application → onglet **Bot** → copiez le token
3. Onglet **OAuth2 > URL Generator** : cochez `bot` + `applications.commands`  
   Permissions : `Send Messages`, `Embed Links`, `View Channels`
4. Invitez le bot sur votre serveur avec l'URL générée

### 2. Configuration

```bash
cp .env.example .env
# Éditez .env et renseignez DISCORD_TOKEN
```

### 3. Lancer avec Docker (recommandé)

```bash
docker compose up -d
```

### 3b. Lancer sans Docker

```bash
python -m venv .venv
source .venv/bin/activate   # Windows : .venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

## Variables d'environnement

| Variable | Défaut | Description |
|----------|--------|-------------|
| `DISCORD_TOKEN` | — | Token du bot Discord (**obligatoire**) |
| `POLL_INTERVAL` | `90` | Secondes entre deux sondages par filtre (min recommandé : 60) |
| `MAX_ITEMS_PER_POLL` | `20` | Articles récupérés par sondage |
| `SEEN_ITEMS_RETENTION_DAYS` | `7` | Jours avant de purger les IDs d'articles vus |
| `DATABASE_PATH` | `data/vinted.db` | Chemin vers la base SQLite |

## Architecture

```
src/
├── config.py    — Variables d'environnement
├── database.py  — SQLite async (filtres + articles vus)
├── vinted.py    — Client API Vinted (cookie session, anti-ban)
├── monitor.py   — Boucles de sondage par filtre
├── embeds.py    — Construction des embeds Discord
└── bot.py       — Bot Discord + slash commands
```

## Notes sur les limites Vinted

- L'API publique Vinted est non documentée et peut changer.
- Un intervalle de **90 secondes minimum** est recommandé par filtre.
- Le bot ne publie jamais plus d'articles qu'il n'en reçoit de l'API (max 20 par appel).
- Aucune authentification Vinted n'est requise ni utilisée.

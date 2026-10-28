# Déploiement

Rien n'est en ligne pour l'instant. Ce document décrit comment déployer, le jour où on le décide.

## Principe

- **Une image Docker** contient tout : la carte, l'API, le MCP en HTTP, la collecte et l'OCR en français.
- **Un volume `/data`** contient toutes les données : la base, les PDF bruts, le cache et les transcriptions. C'est la seule chose à sauvegarder.
- **Deux usages de la même image** :
  - `web`, qui tourne en permanence ;
  - `update`, la tâche quotidienne.

## Mise en route

1. Créer un fichier `.env` à côté de `compose.yaml`. Il ne doit jamais être commité. Il contient :
   - `RADAR_API_TOKEN` : un long jeton aléatoire ;
   - `RADAR_PUBLIC_HOST` : le nom de domaine ;
   - en option, `ANTHROPIC_API_KEY` et `RADAR_LLM_MODEL`.
2. Lancer le serveur :

```bash
docker compose up -d --build
```

3. Remplir le volume une première fois : copier `radar.sqlite`, `blobs/`, `transcripts/`, `cache/` et `reference/` dans `/data`, ou lancer une collecte.
4. Placer un proxy HTTPS devant le port 8765, par exemple Caddy ou Traefik. Le port n'est publié que sur la machine elle-même.

## Tâche quotidienne

Elle lance une collecte légère des nouveautés, puis la reconstruction. Il suffit de la planifier une fois par jour (cron ou timer systemd) :

```bash
docker compose run --rm update
```

- Un site qui bloque est sauté jusqu'au lendemain, sans nouvelle tentative.
- Les préfectures ne sont pas collectées par défaut : il faut les ajouter avec `--sources`.
- Chaque exécution complète le journal des nouveautés.

## Surveillance

- `radar update` renvoie `health_alerts` à la fin de chaque exécution.
- `radar health --strict` sort en erreur si une source est bloquée, muette depuis 3 passages, plantée ou sans mise à jour depuis 3 jours. On peut le brancher sur une alerte.

## Sécurité

- Hors localhost, le serveur refuse de démarrer sans `RADAR_API_TOKEN`.
- Le jeton protège `/api/*` et `/mcp`. La page de la carte ne contient aucune donnée : elle demande le jeton au premier appel.
- L'image tourne sous un utilisateur non-root.
- Les noms des personnes physiques ne sont jamais stockés (RGPD).

## Sauvegarde

Il suffit de copier le volume `/data` chaque nuit. Les corrections humaines s'exportent aussi dans `gold/corrections.jsonl` (`radar review export`), qui reste local et doit être sauvegardé dans un espace privé. Une base perdue se reconstruit à partir de `blobs/` avec `radar run`, sans nouvelle collecte.

## Vérifications automatiques

À chaque push, [.github/workflows/ci.yml](../.github/workflows/ci.yml) vérifie :
- le format du code ;
- le lint ;
- les types ;
- les tests ;
- la construction de l'image.

## Restent à décider

- L'hébergeur : un petit VPS en France suffit au départ.
- Le nom de domaine.
- Un jeton par client, ou un vrai système de comptes.
- L'éventuelle facturation à l'usage du MCP.

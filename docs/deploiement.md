# Déploiement

Rien n'est en ligne pour l'instant. Ce document décrit comment déployer, le jour où on le décide.

## Principe

- **Une image Docker** contient tout : la carte, l'API, le MCP en HTTP, la collecte et l'OCR en français.
- **Un volume `/data`** contient base, PDF, transcriptions, références humaines dans `/data/gold`, évaluations et registre durable des travaux IA. Le cache est reconstructible ; les corrections et les demandes payantes ne le sont pas.
- **Un volume `/backups`**, monté sur les tâches, reçoit les snapshots privés. Les copier aussi hors de l'hôte.
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

3. Remplir le volume par une restauration vérifiée ou importer les données existantes. Les nouveaux volumes nommés reçoivent les références de `/data/gold` préparées dans l'image. Un ancien volume ou un bind mount n'est jamais remplacé automatiquement ; initialiser ses références depuis `/app/gold` une fois en préservant les fichiers existants.
4. Placer un proxy HTTPS devant le port 8765, par exemple Caddy ou Traefik. Le port n'est publié que sur la machine elle-même.

Les ressources sont bornées séparément pour web et job (`RADAR_WEB_CPUS`, `RADAR_WEB_MEMORY`, `RADAR_JOB_CPUS`, `RADAR_JOB_MEMORY`). Les valeurs proposées sont des limites initiales, pas une capacité mesurée. Les PID sont plafonnés et le web utilise une racine en lecture seule.

Pour servir un jeu figé : évaluer et approuver la base, puis `radar dataset publish --dir /data/publications`. Définir `RADAR_PUBLISHED_DIR=/data/publications` pour le web. Les activations suivantes sont observées sans redémarrage, avec une version cohérente par requête. Voir [exploitation.md](exploitation.md).

## Tâche quotidienne

Elle lance une collecte légère des nouveautés, puis la reconstruction. Il suffit de la planifier une fois par jour (cron ou timer systemd) :

```bash
docker compose run --rm update
```

- Un site qui bloque est sauté jusqu'au lendemain, sans nouvelle tentative.
- Les préfectures ne sont pas collectées par défaut : il faut les ajouter avec `--sources`.
- Chaque exécution complète le journal des nouveautés.
- Le verrou local empêche les écrivains coopérants de se chevaucher. Ne pas multiplier les collecteurs indépendants sur un même site.
- Les budgets IA journaliers, de campagne et par dossier sont à zéro par défaut. Configurer des plafonds et des tarifs versionnés avant les traitements payants. Les demandes sont conservées dans `llm-jobs.sqlite`, hors du cache.

## Surveillance

- `radar update` renvoie `health_alerts` à la fin de chaque exécution.
- `radar health --strict` sort en erreur si une source est bloquée, muette depuis 3 passages, plantée ou sans mise à jour depuis 3 jours. On peut le brancher sur une alerte.
- Surveiller aussi disponibilité de la base, fraîcheur des publications, échecs PDF, tâches incertaines, consommation et réservations. Une sonde verte ne prouve pas la précision métier.

## Sécurité

- Hors localhost, le serveur refuse de démarrer sans `RADAR_API_TOKEN`.
- Le jeton protège `/api/*` et `/mcp`. La page de la carte ne contient aucune donnée : elle demande le jeton au premier appel.
- L'image tourne sous un utilisateur non-root.
- Les noms des personnes physiques ne sont jamais stockés (RGPD).

## Sauvegarde

Ne pas copier seulement le fichier SQLite pendant qu'il écrit en WAL. Utiliser une sauvegarde cohérente :

```bash
docker compose run --rm update radar backup --out /backups --keep 7
```

La commande conserve transactions validées, registre IA et références ; vérifie checksums et intégrité SQLite ; préserve les dossiers inconnus lors de la rétention. Le cache PDF/OCR n'est pas nécessaire, mais les anciens identifiants de lots qui y subsisteraient sont conservés.

Les corrections s'exportent aussi dans `/data/gold/corrections.jsonl` (`radar review export`) et restent privées. Tester la restauration dans un emplacement vide et garder une copie hors de l'hôte. Reconstruire depuis les PDF ne restaure ni les décisions humaines ni le suivi des demandes payantes. Procédure : [exploitation.md](exploitation.md).

## Vérifications automatiques

À chaque push, [.github/workflows/ci.yml](../.github/workflows/ci.yml) vérifie :
- le format du code ;
- le lint ;
- les types ;
- les tests ;
- la construction de l'image.

Les tests tournent hors ligne ; Tesseract français est installé explicitement en CI. Ils ne remplacent pas des références indépendantes réellement relues. Les commandes de publication de données ne déploient ni le serveur ni l'environnement Codex automatiquement.

## Restent à décider

- L'hébergeur : un petit VPS en France suffit au départ.
- Le nom de domaine.
- Un jeton par client, ou un vrai système de comptes.
- L'éventuelle facturation à l'usage du MCP.

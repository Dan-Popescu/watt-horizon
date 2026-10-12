# Radar Raccordement

Repérer tôt les projets qui vont demander un raccordement au réseau électrique (batteries géantes, data centers), à partir de documents publics, avec une preuve pour chaque information.

Statut : POC limité aux Hauts-de-France, échantillon de données gelé au 2026-10-12. Le déploiement reste à décider.

## Ce que ça fait

1. **Collecte** des dossiers DREAL « cas par cas », des avis MRAe, des installations classées (Géorisques), des créations de sociétés et de la presse. La collecte est lente et polie : elle s'arrête au premier blocage.
2. **Lecture** des PDF : texte, champs de formulaire, puis transcription ou OCR des pages scannées.
3. **Extraction** du porteur, du SIRET, de la commune, du GPS, de la puissance, du poste électrique et des dates. Chaque valeur est accompagnée de sa citation exacte.
4. **Tri** en deux étages : mots-clés (pour ne rien rater), puis LLM ou jeu de vérité (pour la précision).
5. **Contrôles** : SIRET, commune, cohérence du GPS, plausibilité de la puissance, dates, et masquage des personnes physiques (RGPD).
6. **Projets** : regroupement des dossiers et des signaux, puis interrogation en lecture seule par la CLI ou par MCP (pour les agents IA).

## Démarrer

```bash
uv sync --all-extras
```

```bash
uv run pytest
```

```bash
uv run radar run
```

```bash
uv run radar stats
```

Avec `RADAR_OFFLINE=1`, rien n'est envoyé sur le réseau. Les autres variables d'environnement sont décrites dans [AGENTS.md](AGENTS.md).

## Brancher un agent (MCP)

Le serveur est déclaré dans [.mcp.json](.mcp.json) et se lance avec `uv run radar mcp`. Il propose cinq outils en lecture seule :

| Outil | Usage |
| --- | --- |
| `dataset_info` | Couverture, fraîcheur et limites des données. À appeler en premier. |
| `search_projects` | Recherche par catégorie, département, commune, rayon, puissance minimale ou date. |
| `get_project` | Fiche complète : preuves citées, documents, contrôles, signaux liés. |
| `zone_pressure` | Projets et puissance cumulée autour d'un point. |
| `projects_near_substation` | Projets cités autour d'un poste électrique. |

## Pour aller plus loin

- [docs/architecture.md](docs/architecture.md) : architecture et choix techniques.
- [AGENTS.md](AGENTS.md) : règles à respecter et recettes pour faire évoluer le code (en anglais, destiné aux agents).

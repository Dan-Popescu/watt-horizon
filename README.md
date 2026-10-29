# WattHorizon

Repérer tôt les projets qui vont demander un raccordement au réseau électrique (batteries géantes, data centers), à partir de documents publics, avec une preuve pour chaque information.

Statut : prototype limité aux Hauts-de-France, échantillon gelé au 2026-10-12. Non déployé ; la précision métier reste à valider. Voir les [priorités de fiabilisation](docs/roadmap.md).

## Ce que ça fait

1. **Collecte** des dossiers DREAL « cas par cas », des avis MRAe, des installations classées (Géorisques), des créations de sociétés et de la presse. La collecte est lente et polie : elle s'arrête au premier blocage.
2. **Lecture** des PDF : texte, champs de formulaire, puis transcription ou OCR des pages scannées.
3. **Extraction** du porteur, du SIRET, de la commune, du GPS, de la puissance, du poste électrique et des dates. Chaque valeur est accompagnée de sa citation exacte.
4. **Tri** en deux étages : mots-clés (pour ne rien rater), puis LLM ou jeu de vérité (pour la précision).
5. **Contrôles** : SIRET, commune, cohérence du GPS, plausibilité de la puissance, dates, et masquage des personnes physiques (RGPD).
6. **Projets et nouveautés** : regroupement des dossiers et des signaux, puis journal de ce qui change à chaque mise à jour (projet nouveau, puissance modifiée, décision rendue…).
7. **Accès** : une carte web, des exports CSV et GeoJSON, et MCP pour les agents IA. Tout est en lecture seule.

## Développer en local ou dans Codex Cloud

Le dépôt contient le code, les tests et un petit corpus de PDF caviardés. Les données de travail dans `data/` et les secrets restent locaux. **Les tests fonctionnent sans clé API ni nouvelle collecte.**

Installation et reprise : [docs/developpement.md](docs/developpement.md).

## Démarrer

```bash
uv sync --frozen --all-extras
```

```bash
uv run radar run
```

```bash
uv run radar web
```

La carte est alors sur http://127.0.0.1:8765. Avec `RADAR_OFFLINE=1`, rien n'est envoyé sur le réseau. Les autres variables d'environnement sont décrites dans [AGENTS.md](AGENTS.md).

## La carte

- **Carte** : projets colorés par type, taille selon la puissance, filtres par type, département, puissance et texte libre. Un clic ouvre la fiche : chiffres clés, citations exactes, contrôles, documents, signaux liés.
- **Nouveautés** : ce qui a changé depuis une date, avec l'avant et l'après.
- **Exports** : CSV pour Excel, GeoJSON pour les logiciels de cartographie, et la configuration MCP à copier.

## Mesurer et améliorer la qualité

- `radar eval` mesure la qualité et la compare à la mesure précédente :
  - la justesse de chaque champ ;
  - l'OCR ;
  - le tri ;
  - les données personnelles.
- `radar web --review` ouvre la page de **relecture**, en usage interne. Chaque dossier validé ou corrigé devient une référence vérifiée. Les corrections l'emportent sur la machine ; les limites connues de l'évaluation sont détaillées dans la feuille de route.
- `radar review export` sauvegarde les corrections et les décisions sur les liens dans `gold/`. Ces exports internes restent locaux dans ce dépôt public ; sauvegarder aussi la base de travail.
- En relecture, « voir dans le PDF » montre la page d'origine avec la valeur surlignée, et le mode « Liens entre dossiers » fait confirmer ou séparer les projets.

- À chaque construction de dossier, des **signalements** repèrent les cas suspects. La file de relecture (`radar triage`) les présente en premier, avec la raison.
- `radar errors` montre où la machine se trompe le plus. `radar cases` transforme chaque dossier vérifié en test de non-régression.
- L'onglet **Qualité** de `radar web --review` résume le tout.

Méthode : [docs/qualite.md](docs/qualite.md). Guide d'annotation : [docs/annotation.md](docs/annotation.md).

La couverture et les archives sont inspectables avec `radar coverage --years 2024 2025 2026`.
Le [rattrapage historique](docs/couverture-documentaire.md) reprend les listes par pages ; la
[récupération des dossiers](docs/dossiers-documentaires.md) suit les pièces et annexes avec budgets et provenance.
Les [campagnes aveugles](docs/campagnes-annotation.md) préparent des références humaines et comparent des variantes
sur les mêmes entrées. Un [inventaire manuel externe](docs/inventaire-reference-decouverte.md) mesure aussi les
dossiers jamais collectés. Ces outils ne donnent aucun score de précision réel sans références vérifiées.

Les [six chantiers documentaires](docs/six-documentary-workstreams.md) sont raccordés : catalogue de sources,
recherche web bornée, dossiers complets, tableaux/OCR sélectif, interface privée de relecture et import traçable
du consensus de développement. `radar quality-loop` et le tableau de qualité montrent les observations réellement
disponibles. Le [rapport de validation](docs/validation-six-workstreams-20261029.md) distingue les tests, les essais
locaux et les validations métier restant à effectuer.

Les [mesures de capacité hors ligne](docs/capacity-measurements-20261029.md) et la
[comparaison des variantes de lecture](docs/mesure-lecture-20261029.md) permettent de reproduire les coûts locaux.
Le stockage des liens évite désormais une seconde collection complète de JSON en mémoire. Les tableaux et
le second OCR restent optionnels ; leurs gains métier doivent encore être mesurés sur des références humaines.

## Brancher un agent (MCP)

Deux façons de se connecter :
- en local, via [.mcp.json](.mcp.json) (`uv run radar mcp`) ;
- à distance, via l'adresse `http://<hôte>:8765/mcp` servie par `radar web`.

Les six outils sont en lecture seule :

| Outil | Usage |
| --- | --- |
| `dataset_info` | Couverture, fraîcheur et limites des données. À appeler en premier. |
| `search_projects` | Recherche par catégorie, département, commune, rayon, puissance minimale ou date. |
| `get_project` | Fiche complète : preuves citées, documents, contrôles, signaux liés. |
| `zone_pressure` | Projets et puissance cumulée autour d'un point. |
| `projects_near_substation` | Projets cités autour d'un poste électrique. |
| `what_changed` | Nouveautés depuis une date (alertes). |

## Pour aller plus loin

- [docs/developpement.md](docs/developpement.md) : installation reproductible et travail en local ou dans le Cloud.
- [docs/roadmap.md](docs/roadmap.md) : défauts connus et prochaines priorités.

- [docs/architecture.md](docs/architecture.md) : architecture et choix techniques.
- [docs/deploiement.md](docs/deploiement.md) : mise en ligne, tâche quotidienne, sauvegardes.
- [AGENTS.md](AGENTS.md) : règles à respecter et recettes pour faire évoluer le code (en anglais, destiné aux agents).

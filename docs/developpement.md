# Développer WattHorizon

Le même dépôt sert au travail local et à Codex Cloud : https://github.com/Dan-Popescu/watt-horizon.

## Installation

Prérequis : Python 3.12, `uv` et Git. Node permet aussi de vérifier le JavaScript ; Tesseract français sert aux mesures OCR lorsqu'il est installé.

```bash
git clone https://github.com/Dan-Popescu/watt-horizon.git
cd watt-horizon
uv sync --frozen --all-extras
```

Le paquet et la commande restent nommés `radar` pour conserver les commandes existantes.

## Vérifier sans données ni clé

```bash
uv run ruff format --check src tests
uv run ruff check src tests
uv run mypy
uv run pytest
```

Les tests utilisent des dossiers temporaires et les PDF caviardés de `tests/corpus/`. Le test des cas humains reste ignoré tant que `gold/cases/` est vide. Réussir les tests ne démontre pas la précision métier : lire [roadmap.md](roadmap.md).

## Codex Cloud

Créer un environnement depuis les réglages Codex Cloud, sélectionner `Dan-Popescu/watt-horizon`, puis faire installer et tester le projet. Fournir ces réglages :

- Python 3.12 et `uv` ; Node pour les tests JavaScript.
- Installation : `bash .codex/setup.sh`.
- Variable `RADAR_OFFLINE=1`. Aucun secret n'est nécessaire pour développer.
- Lancer les quatre vérifications ci-dessus, puis publier l'environnement quand sa préparation est terminée.

Le script prépare les dépendances ; il ne collecte rien et ne démarre aucun service. Un environnement Cloud ne récupère pas automatiquement la base locale. Pour travailler sur la pipeline, utiliser les tests et le corpus caviardé existants.

Documentation officielle : https://learn.chatgpt.com/docs/environments/cloud-environments.

## Reprendre un changement ailleurs

Une branche par changement, préfixée `feat/`. Enregistrer et pousser le travail avant de changer de machine ; ailleurs, récupérer la même branche. Un nouveau chat doit lire `AGENTS.md`, puis la documentation liée à sa tâche. Les modifications non enregistrées dans Git ne sont pas transférées.

Le code, les tests et les consignes partagés passent par Git. La base `data/`, les secrets et les exports bruts de relecture restent hors du dépôt public. Le changement de nom ne modifie pas le dossier local existant ni ses données.

# Validation des chantiers — 29 octobre 2026

Branche : `feat/scalable-quality-pipeline`. Base de revue : `2f4e71dc44762ecb70456cfed5320a4516e4582e`. Code validé : `71706b5` ; les commits suivants de cette livraison ne modifient que ce compte rendu.

## Contrôles

| Vérification | Résultat |
| --- | --- |
| Format Ruff | 185 fichiers conformes |
| Ruff lint | Aucun problème |
| mypy strict | 122 fichiers source validés |
| Suite hors ligne | **364 réussis, 1 ignoré**, 124,31 s |
| Docker Compose | Configuration valide |
| Image de production | Construction réussie, dépendances verrouillées, TLS conservé |
| Conteneur non-root / système en lecture seule / sans réseau externe | Exécution hors ligne, refus de promotion d'une base vide, exports CSV/GeoJSON, sauvegarde/restauration et lecture HTTP vérifiés |

Le test ignoré dépend des cas humains locaux absents du dépôt public. Aucun test échoué n’a été désactivé. Le défaut de cache OCR découvert par la première suite complète a été corrigé avant le passage final.

## Comparaison synthétique du rapprochement

Mesures hors ligne avec `tracemalloc`, sur des dossiers sans indice partagé et sans lien résultant. L'instrumentation ajoute du temps ; les durées ne sont pas un débit de production.

| Dossiers | Paires possibles, ancienne méthode | Pic Python avant | Pic Python après | Temps avant | Temps après |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 1 000 | 499 500 | 31,17 MiB | 0,52 MiB | 0,934 s | 0,028 s |
| 2 000 | 1 999 000 | 124,15 MiB | 1,04 MiB | 3,719 s | 0,061 s |

Ces nombres de paires sont théoriques ; la nouvelle sélection ne matérialise pas toutes ces paires. Les cas denses sont conservés intégralement et peuvent rester coûteux si le résultat lui-même est dense.

Autres essais comparables : 201 projets de 1 MW donnent désormais **201 projets et 201 MW**, tout en affichant une page de 200 éléments. Un lot de 24 PDF valides et un PDF corrompu met en cache les 24 documents valides au lieu d'interrompre le lot au premier échec.

## Revues indépendantes

Deux revues ont examiné séparément les standards et la conformité à la spécification. Leurs contre-exemples ont conduit à des corrections supplémentaires :

- Observations de calibration, arbitrage et prérelecture comptées par groupes indépendants, y compris les PDF partagés.
- Classement ordinaire mesuré sur les sorties machine ; référence `gold` du test refusée comme preuve de réussite.
- Contrôle de complétude par états durables, y compris après crash, attente IA et relance partielle d'un autre dossier.
- Invalidation des dossiers et rapprochements après modification des documents, corrections et décisions humaines.
- Lecture bornée avant allocation et rendu d'aperçu sous admission, limites et watchdog.
- Migrations atomiques : schéma et version avancent ensemble ; une erreur ne laisse pas une demi-migration.

Les deux revues ont confirmé la clôture de leurs constats. Une comparaison supplémentaire de 100 transitions déterministes a conservé les mêmes liens, projets et journaux entre reconstruction incrémentale et complète.

## Utilisation et limites

Voir [l'exploitation](exploitation.md), [la méthode qualité](qualite.md), [la feuille de route](roadmap.md) et [la spécification](scalable-quality-pipeline.md).

L'ancien endpoint `/api/projects` conserve une liste mais est borné à 200 éléments ; utiliser les en-têtes de continuation ou `/api/projects/page` pour poursuivre. Les exports complets restent disponibles en flux. Les jeux publiés sont distincts du candidat en cours ; les approbations antérieures ne permettent pas de publier un candidat devenu incomplet.

Les références publiques livrées restent non vérifiées. Les seuils par défaut de 0,95 et 30 groupes sont des règles sur les observations, pas une garantie statistique de précision. Il faut des annotations humaines représentatives pour mesurer la qualité métier, choisir les modèles et autoriser une publication réelle.

Aucune collecte nouvelle, aucun appel de modèle payant et aucun déploiement distant n'ont été effectués. Les essais fournisseur utilisent des doublures ; l'intégration payante réelle reste à vérifier avec le modèle, les tarifs et les accès retenus. Aucun coût mensuel ni plafond de capacité de production n'est déduit de ces essais.

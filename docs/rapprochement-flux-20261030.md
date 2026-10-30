# Rapprochement en flux et diagnostics précalculés — 30 octobre 2026

Ce lot prolonge les [mesures de capacité précédentes](capacity-measurements-20261029.md).
`Database.replace_links` était déjà alimenté en flux : cette livraison ne revendique pas sa correction.
Le changement porte sur la construction des liens et les consommateurs qui conservaient encore tout le graphe.

## Chemin réellement raccordé

`link_and_build` consomme `iter_propose_links`, avec conservation de `propose_links` comme interface de liste.
Un index SQLite temporaire sur disque reçoit les liens, conserve le dernier doublon et applique les décisions
humaines par recherche indexée. Les parcours du graphe, la conservation des liens non affectés et les contraintes
de regroupement utilisent des curseurs. La reconstruction ne conserve plus le dictionnaire global des rattachements
entre dossiers et signaux : elle lit les signaux du groupe en cours.

Les contraintes humaines sont conservées dans un index distinct même lorsqu'une paire n'a pas de lien automatique
(par exemple un regroupement sur code INSEE sans libellé de commune). Une décision de séparation reste alors effective
sans créer artificiellement un lien publié.

Le chemin complet de référence et le chemin incrémental publient liens, projets, révisions et journal dans la même
transaction existante. Une erreur de publication restitue les sorties précédentes. Une suppression ou un retrait
de décision déclenche la reconstruction correspondante ; les campagnes appliquent aussi immédiatement les retraits
avant le prochain rapprochement. La lecture des décisions courantes sélectionne la dernière ligne en SQL sans charger
tout l'historique.

Les partitions et les diagnostics ne conservent que les contraintes humaines, sans matérialiser les liens automatiques.
Les vues de projet et de relecture parcourent également les liens en flux. Le contrat actuel de l'API de relecture
retourne toujours toutes ses lignes : la réponse finale reste proportionnelle au nombre de lignes retournées.

La mémoire n'est pas constante : les dossiers, signaux, projets et décisions humaines restent en mémoire à leur échelle
respective. L'index temporaire conserve tous les liens sur disque avec un cache SQLite de 2 Mio. Le nombre de liens
peut rester quadratique ; une saturation du disque doit être traitée comme un échec, jamais comme une complétude.

## Diagnostics et révision exacte

Une migration ajoutée en fin de schéma maintient une révision monotone des tables contribuant aux diagnostics.
Les traitements calculent les agrégats sous un snapshot de lecture ; leur publication vérifie que cette révision
est toujours identique. Une modification concurrente conserve l'ancien snapshot et refuse la publication périmée.

`radar quality-loop` actualise les snapshots documentaire et tableau de bord. La requête web lit le résultat enregistré
et n'effectue plus le regroupement, l'analyse des erreurs ou le parcours complet des liens. Elle expose `snapshot` :
date, périmètre, révision source, révision actuelle, état `current`, `stale` ou `unavailable`, et raisons de péremption.
Les mutations de données, références externes modifiées et une ancienneté supérieure à 24 heures invalident l'actualité.
Les fichiers de cas sont identifiés par leur contenu ; le journal IA externe est suivi avec les métadonnées du fichier
SQLite et de son WAL. Le journal reste une source externe observée, pas une révision atomique partagée avec la base métier.

L'interface affiche explicitement un diagnostic absent ou périmé. Un snapshot absent n'est pas une mesure nulle.
Les calculs ne créent aucune annotation humaine et n'établissent aucune précision métier sur une base sans références.

## Mesure reproductible

Commande : `uv run python tools/benchmark_link_pipeline.py --records 1000`.
Chaque essai utilise un nouvel interpréteur et une base jetable ; il construit et publie le graphe dense entier.
La mesure avant a été réalisée avant les changements de ce lot, depuis la référence `cec4c7c` restaurée.
Les [résultats bruts](measurements/streamed-link-pipeline-20261030.json) conservent une répétition avant et trois après.

| Mesure | Avant | Après |
| --- | ---: | ---: |
| Dossiers synthétiques | 1 000 | 1 000 |
| Liens effectivement stockés | 499 500 | 499 500 |
| Projets | 1 | 1 |
| Pic RSS du processus | 459,09 Mio | environ 41,35 Mio |
| Durée complète | 4,526 s | 5,116 à 5,251 s |

Le SHA-256 du JSON de tous les liens stockés, triés par paire, est identique dans les quatre essais :
`4a18abd6f1d8bfa75b6581a3d297d80367885a5001e90537f98874a5a4f64906`.
La baisse de RSS observée est d'environ 91 %, avec un surcoût de calcul et de stockage temporaire. La référence n'a
qu'une répétition et l'environnement est partagé : ces durées locales ne caractérisent pas la production.
La fonction de compatibilité `propose_links` retourne volontairement encore une liste ; le gain concerne le chemin
de publication et ses consommateurs raccordés au flux.

## Contrôles

Les tests couvrent l'équivalence complète/incrémentale après ajouts, suppressions et retraits de décisions,
la conservation du dernier doublon et des raisons, l'absence d'appel à `Database.links` dans la publication,
le nettoyage de l'index temporaire, la publication atomique et une régression mémoire sur 124 750 liens complets.
Les tests de snapshots contrôlent lecture sans recalcul, absence, date et périmètre, mutation des entrées,
expiration, changement concurrent et erreur de calcul. Les tests web utilisent le snapshot explicitement préparé.

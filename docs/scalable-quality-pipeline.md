# Fiabilité, dépenses et passage à l’échelle

Spécification de livraison issue de la revue du 29 octobre 2026, approuvée pour implémentation.
Point de départ : `2f4e71dc44762ecb70456cfed5320a4516e4582e`.

## Résultats attendus

1. Une référence humaine conserve la valeur effectivement approuvée, même après reconstruction. Les jeux développement, calibration et test restent indépendants et groupés par projet/documents communs. Une référence de comparaison approuvée ne devient pas le dernier essai automatiquement. Mesure obligatoire absente, corpus vide et échec de reconstruction bloquent la promotion.
2. La validation d’extraction distingue valeur, unité, contexte et preuve. Les scores OCR détectent les associations nombre/unité erronées. La calibration distingue les champs et les présences/absences. Une correction partielle laisse visibles les champs à relire.
3. Les tâches IA et leurs dépendances survivent à la suppression des caches. Réservations, tentatives, résultats bruts, états d’envoi incertains, usage et budgets sont durables. Une erreur de résultat ne bloque pas les autres. L’orchestration attend l’état réel des tâches, avec délai borné.
4. Le rapprochement ne construit pas toutes les paires sans indice partagé, et préserve les liens denses et décisions humaines. Les requêtes limitent le travail au stockage ; les agrégats portent sur tout le jeu filtré. Le même PDF conserve plusieurs rattachements. Les PDF défectueux sont isolés ; CPU/mémoire et invalidation sont bornés et explicites.
5. Sauvegarde cohérente SQLite, restauration vérifiée, jeux de résultats versionnés et activation atomique permettent une exploitation sobre. Un seul traitement écrit à la fois sur l’hôte. Disponibilité, fraîcheur, reprises et usage sont observables. La distribution vient avec un besoin mesuré, sans déploiement implicite.

## Interfaces de validation

Les tests portent sur les interfaces existantes ou explicitement demandées : construction de dossier et références, calibration et relecture, commandes d’évaluation/shadow, exécution IA avec faux fournisseur, rapprochement, requêtes web/MCP, lecture/OCR, sauvegarde/restauration et promotion de jeu.

Les cas de régression de la revue doivent être conservés : validation 50→80 MW, confusion MW/MWh, inversion OCR, acceptation apprise depuis un autre champ, métrique disparue, tâches partagées, panne après acceptation fournisseur, changement de modèle, réponse invalide suivie d’une valide, 201 projets à 1 MW et PDF corrompu dans un lot.

## Intégration

Les agents travaillent sur des fichiers distincts dans le checkout isolé existant. Les lots passent des tests ciblés puis sont intégrés par commits cohérents sur `feat/scalable-quality-pipeline`. Une revue de conformité et une revue de cette spécification précèdent les contrôles finaux. Aucun appel payant, nouvelle collecte ou déploiement n’est nécessaire pour développer ces mécanismes.

## Limites de validation

Les références humaines et les effectifs restent à produire sur des documents représentatifs. Une suite de tests verte ou un budget configuré ne prouve pas une précision métier, un coût mensuel ni une capacité cloud mesurée. L’approbation d’un jeu de résultats exige des données réellement vérifiées ; le code ne doit pas inventer cette approbation.

# Validation complémentaire : capacité et exploitation — 29 octobre 2026

Cette livraison poursuit les [six chantiers documentaires](six-documentary-workstreams.md), depuis
`40f12914311b266ed4cab29cebe72691b3a62bc0`. Elle mesure les coûts locaux, corrige une allocation évitable
du stockage et raccorde les options de lecture au déploiement Compose. Elle n'effectue aucune collecte
Internet, annotation humaine, dépense de modèle ou publication distante de l'application.

Code et outils contrôlés : `f1314b1ce96abef62e8661150c37236e89fe7cac` ; les compléments suivants
de cette livraison portent sur la documentation.

## Mesures et correction mémoire

Le [banc de capacité](capacity-measurements-20261029.md) fournit ses mesures brutes et ses limites :
10 000 dossiers isolés, des cas denses jusqu'à 499 500 liens, un parcours complet de 10 000 projets et
le stockage de 100 000 liens reçus en flux. Les contrôles vérifient aussi l'absence d'omissions, de doublons
et de modification des totaux, ainsi que les égalités et valeurs inconnues en pagination.

`Database.replace_links` créait une liste de tous les JSON, puis une autre liste de toutes leurs clés.
Le correctif `fe86819` alimente une table temporaire SQLite en flux, puis remplace les différences dans la
transaction du caller. Le dernier doublon reste prioritaire ; une écriture identique laisse les lignes et
la révision intactes. Une interruption de l'entrée ou une erreur de publication conserve l'état précédent.
La table temporaire est vidée après publication ; le rapprochement lui-même retourne toujours sa liste de liens.

Le test de régression mémoire échouait avant le correctif avec 10 493 680 octets Python alloués pour
12 000 liens. Il passe désormais sous le plafond de 3 Mio. Sur le programme minimal de 100 000 liens,
le pic RSS observé passe de 117,945 Mio avant à 29,711 Mio au maximum des trois essais après. La référence
avant n'a qu'une répétition ; cette baisse ne s'étend pas automatiquement à toute la pipeline.

La [comparaison de lecture](mesure-lecture-20261029.md) porte sur 11 PDF caviardés et 77 pages. Les tableaux
augmentent ici le coût froid d'environ 2,4 fois sans ajouter de champ extrait. Aucun passage OCR n'a été
déclenché sur ce corpus textuel. Les deux options restent désactivées par défaut.

## Docker et configuration

Compose expose maintenant les mêmes 14 paramètres de lecture sur `web` et `update`, avec limites et options
de tableaux/second OCR. Le rendu `docker compose --profile jobs config --format json` est vérifié avec
les options à `0` puis à `1` et des limites personnalisées. Les valeurs arrivent identiquement dans les
deux services ; les budgets applicatifs et les limites du conteneur restent des protections distinctes.

L'image de production construite avec le code applicatif `e927203` est
`sha256:d3816a649acb538fcf17ef5531f3a7c002477bfd019a75f6b1c0f76daf34ff7a`.
Les changements ultérieurs concernent les bancs, leurs tests et la documentation.
La construction conserve la vérification TLS et monte le certificat de session sans l'intégrer dans l'image.

Deux essais locaux de cette image sont exécutés avec utilisateur UID 10001, réseau `none`, racine en
lecture seule, `/tmp` limité à 64 Mio, 1 CPU, 512 Mio et 128 processus :

- Corpus caviardé monté en lecture seule, base et campagne dans un volume jetable : schéma 18,
  7 dossiers, 3 projets attendus sans différence avec le snapshot de régression.
- Interface privée réellement démarrée : HTML, santé, texte source et image HTTP 200 ; image PNG de
  185 198 octets ; 7 fiches aveugles, identité du lecteur masquée, brouillon enregistré sans finalisation.
  Tableaux et second OCR activés pour cet essai uniquement. `quality_measured` reste `false`.
- Scan construit lu par Tesseract français à 400 dpi : une page lue, associations « 30 MW » et « 60 MWh »
  conservées, aucune erreur ni page manquante. Il s'agit d'un contrôle fonctionnel, pas d'une mesure métier.

Ces essais utilisent uniquement des copies jetables ; aucune référence humaine n'est créée.

## Revues et contrôles

La revue Standards indépendante n'a signalé aucun écart matériel. La revue Spec a reproduit une médiane
incorrecte dans le comparateur lorsque le nombre de documents est pair : `[2, 6]` donnait `6` au lieu de `4`.
Le correctif `f1314b1` emploie `statistics.median`, avec régressions paire, impaire et singleton. La revue
confirme sa clôture. Le rapport sur 11 PDF reste inchangé. Les deux axes ne signalent plus de constat restant.

Contrôles finaux :

- **551 tests réussis, 1 ignoré**, suite hors ligne complète en 162,59 secondes. Le test ignoré dépend
  des cas humains privés absents du dépôt public ; aucun échec n'est désactivé.
- Format Ruff conforme sur 227 fichiers, Ruff propre, mypy strict propre sur les 144 modules applicatifs
  et sur les deux bancs. Le diff ne contient pas d'erreur d'espacement.
- `RADAR_OFFLINE=1 radar run` terminé, aucun dossier ni projet dans la base cloud principale et aucun
  échec de traitement. Le corpus de régression est contrôlé séparément par les tests et le conteneur.
- `radar eval` ne relève aucune régression face à la référence disponible. Son contrôle d'approbation
  reste **en échec**, faute de références humaines vérifiées, de classement indépendant et d'effectifs
  suffisants. Les 21 étiquettes et 21 fiches publiques restent non vérifiées ; aucune publication de jeu
  n'est autorisée par cette évaluation vide.

Ces contrôles restent distincts d'une approbation de qualité métier ou d'un déploiement.

## Prérequis externes

La politique cloud active a été relue : destinations personnalisées vides, accès restreint aux presets.
Les 19 domaines nécessaires sont bien enregistrés dans le brouillon, sans activation dans cette instance.
Le [contrat du skill de configuration](skill://plugin_connector_1p_ed5feb9070a08191b08c81c47947bc16/setup/references/onboarding.md)
précise : « Agent draft saves do not execute scripts, apply changes to the running environment, or publish it. »

La collecte administrative réelle, la qualification du moteur et de nouveaux territoires, l'inventaire externe
des dossiers manqués et les références de lecteurs humains restent nécessaires pour mesurer l'exhaustivité et
la précision. Les nouveaux bancs permettent de progresser sans attribuer ces résultats aux seuls tests.

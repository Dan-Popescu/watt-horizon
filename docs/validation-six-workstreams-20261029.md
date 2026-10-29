# Validation des six chantiers — 29 octobre 2026

Complément depuis `b2b887a`, périmètre dans [six-documentary-workstreams.md](six-documentary-workstreams.md).
Code testé : `5f50756d9eaaa8456d318e702b88b0189b985761`.

## Vérifications finales

- **540 tests réussis, 1 ignoré**, suite hors ligne complète, 152,35 secondes.
- Ruff et formatage propres sur 222 fichiers ; mypy strict propre sur 144 modules.
- Deux avertissements Python existants concernent `fork` dans les tests de verrou ; aucun échec.
- Le corpus anonymisé existant conserve ses catégories, champs, avertissements et 3 projets attendus.
- Les tests construits couvrent notamment catalogues, recherches, budgets HTTP physiques, reprises, limites
  de parsing, provenance, OCR, cellules de tableaux, contexte de quantités, interface privée, corrections et métriques.

Les tests HTTP locaux nécessitent le chemin d'exécution approuvé hors confinement : le bac à sable bloque la
communication de TestClient entre threads. Les tests réseau restent exclus ; aucun site public ni moteur de
recherche réel n'a été interrogé pour cette livraison. Aucun modèle payant n'a été appelé.

## Essais locaux du parcours

Le serveur `campaign serve` a été lancé puis arrêté par le test dans une copie privée du pilote, sans modifier
sa campagne de référence. Observations : santé HTTP 200, 7 dossiers attribués à la première lecture, fiche sans
valeur machine ni identité de lecteur exposée, brouillon sauvegardé, texte source HTTP 200, image PDF PNG de
185 198 octets. **Aucune référence humaine n'a été validée.** La migration automatique du schéma conservé a été
ajoutée et vérifiée avant le démarrage du serveur, sans conserver le verrou d'écriture pendant son fonctionnement.

Un scan construit a été lu réellement par Tesseract en français à 400 dpi avec le modèle local conservé :
les associations « 30 MW » et « 60 MWh » sont préservées, une page lue, aucune page manquante ni erreur.
Les tests OCR s'adaptent aux modèles `eng`/`fra` disponibles ; le préfixe cloud contenant seulement `fra` a aussi
été testé. Cela ne mesure pas l'exactitude OCR sur des scans administratifs réels difficiles.

Dans le pilote de 7 dossiers/11 PDF, un plan limité à 20 requêtes a été préparé et enregistré sous la campagne
`six-workstreams-smoke`, sans requête externe. Son état tronqué est explicite. Le tableau de qualité indique
toujours `quality_measured: false`, aucune correction humaine et 20 requêtes en attente. Les nouvelles variantes
de tableaux et de second OCR restent désactivées par défaut jusqu'à comparaison sur des références réelles.

L'import du consensus a été testé sur des annotations construites : il crée une observation humaine explicite
de développement et un reçu durable, refuse les conflits, ignore les réponses réservées, ne réapplique pas un
reçu identique et conserve la prédiction machine initiale. Un cas synthétique incorrect produit effectivement
une erreur dans la boucle d'amélioration. Ces tests ne sont pas des annotations métier.

## Revue Standards

Un défaut P2 a été reproduit : un catalogue de 4 000 candidats pouvait être enregistré au-delà des 5 Mio admis
par son lecteur, rendant le fichier inutilisable. Le correctif vérifie exactement la taille UTF-8 avant remplacement,
borne les imports et conserve les données précédentes. La découverte retient le préfixe de candidats qui tient,
puis signale une couverture partielle. La relecture indépendante a confirmé la résolution sur la reproduction
originale et quatre tests ciblés. Aucun autre écart concret restant n'a été signalé par cet axe.

## Revue Spec

Un défaut P1 a été reproduit : l'aplatissement d'une ligne de tableau pouvait perdre son en-tête « Phase » ou
une unité contradictoire, puis promouvoir une puissance au niveau du projet. Le correctif réutilise l'éligibilité
des observations structurées et empêche le texte aplati de contourner le contrôle. Les phases, scénarios,
actifs, états et unités sont conservés ; les cas ambigus ne deviennent pas des scalaires de projet supplémentaires.
La relecture indépendante confirme l'abstention pour la phase 1 et les unités contradictoires, y compris quand
le texte de page répète la valeur, et l'acceptation d'un stockage explicite non ambigu. 21 régressions ciblées
ont été rejouées. Aucun autre écart concret restant n'a été signalé par cet axe.

## Configuration et limites métier

Un brouillon réseau a été enregistré pour 19 domaines précis des sources configurées, du moteur public et des
API de référence, en préservant les règles existantes et les presets. Il ne modifie pas à lui seul l'environnement
actif. Le [contrat de configuration](skill://plugin_connector_1p_ed5feb9070a08191b08c81c47947bc16/setup/references/onboarding.md)
stipule : « Agent draft saves do not execute scripts, apply changes to the running environment, or publish it. »

La qualification du moteur réel et des nouveaux territoires/périodes reste à effectuer après activation réseau.
Les références humaines, la diversité du corpus, le rappel sur un inventaire externe et les coûts réels restent à
mesurer. Les domaines inconnus, pièces hors périmètre, formats non reconnus et ambiguïtés restent signalés.
Aucun déploiement, taux d'exhaustivité global, coût mensuel ou précision métier n'est certifié par ces tests.

# Validation du correctif de confidentialité 2026.10.31b

Le correctif conserve les identités personnelles complètes connues, y compris leurs tokens
courts, et masque leurs fragments situés hors des noms complets de sociétés. Une exemption
de société ne doit pas laisser réapparaître une identité après aplatissement des retours
à la ligne. Une union ambiguë de plusieurs exemptions est masquée conservativement.

Le patch de code et tests revu porte l'empreinte
`fc1fe3e29329a0aa9cc89a1f548cd70cbf6efefbd6e7bec034d93ee7d8812259`.
Les versions antérieures rejetées sont conservées dans les artefacts privés.

## Vérifications achevées

- Setup verrouillé du dépôt ; Ruff format/check et mypy strict passent.
- Suite complète : **783 tests réussis, un ignoré**, en 155,14 secondes.
- Revue indépendante finale : 115 tests ciblés, 300 contre-épreuves synthétiques et
  neuf scénarios du pipeline avec classificateur factice ; aucun corpus réservé consulté.
- Image réellement construite et vérifiée :
  `sha256:761399956b0f7f83c903d1238c52b2ddaabe0e298b903ff2b589d62e20cbdf53`.
- Rejeu hors réseau dans une copie privée distincte : **553 fichiers, 285 dossiers** ;
  292 lectures complètes et 261 incomplètes ; 95 dossiers complets et 190 en attente.
- Les valeurs métier brutes et classifications machine des 285 dossiers sont identiques
  à celles du V1. Les agrégats de lecture et d'extraction sont également identiques.
- Audit natif des sorties courantes : **quatre emplacements détectés avant, zéro après**,
  avec couverture des 285 dossiers et sans manque de cache requis pour cet audit.
- Contrôle indépendant à déclencheurs figés depuis le V1 : **cinq chaînes détectées avant,
  zéro après**, dans toutes les chaînes et clés du dossier de développement concerné.
  Les quatre anciens emplacements sont inclus. Ce contrôle empêche un faux zéro obtenu
  en supprimant le candidat qui servait à reconstruire les déclencheurs de l'audit.

Les deux audits n'ont pas le même périmètre de comptage. Ils ne constituent pas un détecteur
général de noms ni une garantie d'anonymisation exhaustive. Les originaux et sorties antérieures
restent privés et conservés ; ils ne sont pas déclarés anonymisés rétroactivement.

## Limites de l'évaluation

Les budgets IA applicatifs restent nuls et aucun modèle applicatif n'est activé.
Aucune référence humaine, correction ou décision de liaison humaine n'est créée.
Les trois flags persistés `quote_not_found` restent présents ; aucune nouvelle validation
exhaustive des citations originales n'est revendiquée.

La commande native `radar eval` n'est pas relancée : la lignée historique comporte des unions
de partitions protégées, et 190 extractions restent incomplètes. Le diagnostic utilise l'API
en lecture seule, sans réattribuer les partitions, promouvoir une campagne ou publier de projets.
Ces blocages préexistants restent documentés ; le correctif ne produit pas une réussite de
qualité métier ni une mesure de précision ou de rappel.

Les avis IA de sources restent des propositions séparées de `gold`. Leur consolidation,
les reprises après incidents de chronologie et la qualification des anciennes méthodes
d'affichage restent nécessaires. L'indépendance des projets n'est pas établie.

Les copies et snapshots sont locaux. Leur présence après une reconnexion ne garantit pas
leur conservation après recréation de l'instance ; aucune sauvegarde privée hors site n'est
provisionnée. Le [bilan du corpus étendu](bilan-corpus-etendu-20261031.md) conserve le contexte
de collecte et les limites documentaires.

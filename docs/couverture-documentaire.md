# Couverture des sources et archives

`radar coverage --years 2024 2025 2026` fournit hors ligne l’inventaire par source,
région, période, page et mode de collecte. `unconfigured` signifie qu’aucune URL
observée n’est configurée ; `unvisited` qu’aucune collecte n’a été enregistrée.
Les états `partial`, `blocked`, `failed` et la fraîcheur restent visibles.
Une page vide ou un format HTML inconnu ne valide jamais la couverture.

La mise à jour quotidienne reste limitée aux pages récentes. Le rattrapage DREAL
est explicite : `radar collect dreal --backfill --max-pages 3`. Il inclut toutes
les listes configurées, y compris l’archive avant 2025. Le budget s’applique à
chaque liste. La commande suivante reprend à la première page non terminée ;
un échec ou un budget d’articles atteint ne valide pas une page partielle.
Une modification des liens de la première page déclenche un nouveau parcours.
Les écritures documentaires sont idempotentes. Le contrôle du début de liste
nécessite une requête même si le dernier parcours était terminé.
Cette vérification ne rafraîchit pas la date d’achèvement d’une archive déjà
terminée. `--backfill --restart` recommence depuis le début pour revisiter les
anciennes pages ; la fraîcheur complète repose sur la date d’achèvement réelle.

Les observations récentes et historiques ont des enregistrements distincts :
une surveillance de trois pages ne remplace pas une archive déjà parcourue.
`complete` signifie uniquement que les pages annoncées par cette liste ont été
parcourues et les pièces sélectionnées traitées. Ce n’est pas une mesure du rappel
des projets, ni une garantie sur des pièces non liées ou des pages hors périmètre.
Les déplacements internes d’une liste entre deux passages restent possibles ;
la surveillance récente et les nouveaux parcours sont nécessaires.

Les pages annuelles MRAe configurées sont suivies séparément. Les années sans
configuration ne sont jamais inventées. Les préfectures conservent un périmètre
de navigation restreint : même une visite terminée reste `partial`, avec sa limite
explicite. Elles restent volontaires, séquentielles, espacées d’au moins cinq
secondes, avec arrêt sur blocage. Aucun contournement n’est ajouté.

La couverture est persistée dans la base principale et incluse dans les sauvegardes.
Les tests utilisent exclusivement des transports HTTP simulés. Les données humaines
et la mesure réelle du rappel restent nécessaires avant toute promesse d’exhaustivité.

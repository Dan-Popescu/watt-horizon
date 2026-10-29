# Interface privée de relecture aveugle

L'interface complète les [campagnes d'annotation](campagnes-annotation.md). Elle ne publie aucune donnée et n'ouvre pas les API publiques du projet. Une instance est attribuée à un lecteur et à un rôle fixe : `a` pour la première lecture ; `b` pour les groupes tirés pour une seconde lecture indépendante.

```bash
RADAR_OFFLINE=1 uv run radar campaign serve data/campaigns/pilote --reviewer lecteur-1 --slot a --port 8766
RADAR_OFFLINE=1 uv run radar campaign serve data/campaigns/pilote --reviewer lecteur-2 --slot b --port 8767
```

L'application écoute localement par défaut. Une écoute distante exige `RADAR_API_TOKEN` et `--public-host`, avec un accès HTTPS protégé adapté à chaque lecteur. Le jeton se renseigne dans l'interface ; il reste en mémoire et n'apparaît jamais dans les URL. Utiliser des identifiants de relecture privés et distribuer séparément l'accès à chaque lecteur. Le logiciel empêche de réutiliser le même identifiant normalisé dans les deux rôles ; il ne peut pas prouver qu'ils correspondent physiquement à deux personnes différentes.

## Examiner et saisir

Le lecteur voit uniquement ses dossiers, leurs pièces sources et ses propres saisies. Les valeurs machine, les scores, les partitions et les observations de l'autre lecteur ne sont pas affichés. Le rôle `b` ne donne accès qu'aux groupes prévus pour une double lecture. Les noms des lecteurs restent dans les fichiers privés, sans retour dans les réponses de l'interface.

Choisir une pièce, parcourir les pages, puis déclarer explicitement son examen. Les pages sont rendues avec le mécanisme borné de visualisation existant : les données personnelles textuelles identifiées sont masquées ; l'image d'une page scannée peut encore contenir les informations du document public original. L'accès demeure privé. Le texte et les champs du document permettent de copier une citation ; un texte trop long est signalé comme abrégé. Les pages non lues automatiquement sont signalées, sans devenir des absences.

Pour chaque champ, choisir **présent**, **absent**, **inconnu** ou **non examiné**. Une valeur présente nécessite une citation exacte et sa pièce/page ; le bouton « Utiliser cette page » fixe ce rattachement. Pour un champ AcroForm, utiliser `field:nom_du_champ` plutôt qu'une page inventée. Les puissances sont saisies en MW et les capacités en MWh. La pertinence de chaque pièce est renseignée séparément avec une citation du document concerné.

Lorsque la lecture structurée des tableaux est configurée, le texte source expose aussi les cellules avec leurs localisations `table:1:row:2:column:3` : numéros de tableau, ligne et colonne à partir de 1, la première ligne étant l'en-tête. Renseigner ce repère dans la localisation et conserver la page réelle. La citation décrit la cellule ou sa ligne selon les contrôles de campagne ; l'unité dans l'en-tête reste une preuve distincte du nombre. Aucune reconnaissance supplémentaire de tableaux n'est activée par l'interface. Le texte de copie est borné et ses limites sont signalées.

Les brouillons peuvent rester partiels. Ils s'enregistrent à la demande, ainsi qu'avant un changement de dossier si la saisie a changé. Une fenêtre obsolète reçoit un conflit et ne remplace pas la version plus récente. Pour valider une fiche, tous les champs et toutes les pièces doivent avoir été examinés ; « inconnu » reste possible. Une attestation explicite et les contrôles de citation, de types, d'unités et de hashes précèdent l'enregistrement vérifié. Aucun modèle n'est appelé. Une fiche validée reste figée ; un nouvel arbitrage doit passer par une nouvelle campagne plutôt que modifier cette référence silencieusement.

## Sauvegarde et exploitation

Les événements de saisie sont conservés dans `<campagne>/reviews/journal.sqlite`, sous forme de révisions ajoutées sans suppression. La dernière projection JSONL de chaque lecteur est indiquée dans l'interface sous `reviews/`. Chaque fiche validée est aussi archivée dans un fichier immuable portant son empreinte de contenu ; les brouillons restent dans le journal, sans recopier toute la campagne à chaque état. Les fiches vierges initiales et le manifeste ne sont jamais modifiés. Le journal fait autorité : une projection interrompue est reconstruite au redémarrage ou au prochain chargement de la file.

Les exports s'utilisent directement avec `campaign validate` et `campaign compare --annotations ...`. Une lecture isolée ne remplace pas le consensus requis pour les groupes à double annotation. Les désaccords restent visibles dans le validateur de campagne et aucune correction globale n'est appliquée automatiquement.

`duration_s` conserve une durée déclarée par le navigateur pour les périodes où l'onglet est visible, plafonnée à quatre heures par enregistrement. Elle aide à estimer l'effort de relecture ; elle ne prouve pas une durée de travail humain actif. Les fichiers du journal et les exports restent privés sous `data/` et doivent être sauvegardés avec les références.

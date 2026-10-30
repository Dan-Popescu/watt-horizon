# Tâches persistantes de la pipeline

Les commandes historiques restent disponibles. Le worker `tasks` appelle réellement les lecteurs,
l’extraction, les collecteurs et le rapprochement existants, séparément du service web.
Il ne soumet aucun appel de modèle payant. Les tâches référencent le registre IA durable existant ;
un travail IA en attente, incertain ou bloqué exige sa réconciliation habituelle.

```bash
RADAR_OFFLINE=1 uv run radar tasks plan --generation pilote-20261030
RADAR_OFFLINE=1 uv run radar tasks enqueue --generation pilote-20261030
RADAR_OFFLINE=1 uv run radar tasks run --max-tasks 100
uv run radar tasks status
uv run radar tasks status --key EMPREINTE
```

Chaque commande produit un objet JSON. `plan` décrit les entrées actuelles sans les mettre en file.
`enqueue` fige les groupes d’évaluation avant extraction : les dossiers qui n’ont encore que des pièces
reçoivent une fiche explicitement en attente, sans titre brut, valeurs extraites ni sortie machine.
La date de téléchargement réellement conservée détermine le test prospectif. Une nouvelle relation
entre partitions réservées reste une erreur demandant une nouvelle campagne. Aucune référence humaine
n’est créée. Les clés peuvent évoluer entre un plan initial et la mise en file qui fige ses partitions.

## Identités et reprise

Les clés SHA-256 portent sur le type, l’identité logique, les entrées JSON, les dépendances, les limites
et les références IA. La lecture inclut l’empreinte documentaire, la politique et les transcriptions.
L’extraction inclut les pièces, corrections, partitions, code, configuration et état du registre IA.
Le rapprochement lie son algorithme, les signaux, décisions humaines, révisions du graphe et les sorties
exactes de ses dépendances d’extraction. Les paramètres d’un travail existant ne sont jamais modifiés.
Une nouvelle entrée remplace la tâche de même identité logique ; un ancien worker perd son droit de publier.
Un retour à une politique antérieure réactive sa tâche remplacée avec un nouveau jeton, en conservant ses
anciennes tentatives, durées et requêtes ; cette réactivation ne remet pas les budgets à zéro.

Les états sont `pending`, `running`, `retry_wait`, `blocked`, `failed`, `complete` et `superseded`.
Les tentatives, jetons de fencing, baux, requêtes physiques et temps consommé restent dans SQLite.
Hors ligne, les tâches réseau attendent sans tentative. Une interruption après réservation consomme
la durée réservée lors de l’expiration ; elle ne donne pas un budget gratuit au worker suivant.

Une réservation prend un verrou SQLite court. Les opérations historiques travaillent ensuite sur une
copie SQLite privée ; les différences de lignes sont préparées sur disque. La publication vérifie le bail,
les entrées externes et une révision monotone, puis applique les différences et l’état final dans une seule
transaction. Une modification concurrente fait abandonner le résultat ; remettre en file les entrées actuelles.
Les blobs adressés par contenu et caches peuvent recevoir des résultats sans référence après un abandon.
Les manifestes de partitions partagés ne sont jamais écrits par un calcul privé.

La préparation copie actuellement la base complète pour les opérations historiques d’extraction, collecte
et rapprochement. C’est un coût disque/temps réel, conservateur, qui n’est pas une preuve de débit de production.
Les différences et les lectures de liens restent traitées en flux. La transaction finale dure proportionnellement
aux lignes effectivement publiées ; aucune promesse de latence indépendante du volume n’est faite.

## Réseau borné et générations documentaires

Une URL stable peut produire une nouvelle pièce : choisir une nouvelle génération explicite. Le fichier
privé passé à `--spec` contient un tableau JSON, par exemple pour une URL déjà observée et autorisée :

```json
[
  {
    "kind": "download",
    "identity": "source/region/dossier/url",
    "source": "dreal",
    "inputs": {
      "source": "dreal_case_by_case",
      "region": "hauts-de-france",
      "number": "IDENTIFIANT_REEL",
      "url": "https://SOURCE_AUTORISEE/PIECE_OBSERVEE.pdf",
      "title": "Titre réellement observé",
      "filename": "piece.pdf"
    }
  }
]
```

Ces valeurs illustrent le contrat ; elles ne constituent aucune source qualifiée ni collecte réelle.
Pour `kind: explore`, `source` désigne un collecteur existant (`dreal`, `mrae`, `prefectures`, `georisques`,
`registry`, `press`) et `inputs` reprend explicitement ses options de collecte. Les domaines administratifs
doivent être autorisés par la politique réseau active. La configuration du catalogue et du tri fait partie
de la clé. Une collecte historique peut rester partielle : les pièces et états sont publiés, mais la tâche
est bloquée avec ses raisons de couverture, jamais annoncée complète au seul motif que la fonction est revenue.
Une collecte terminée prépare les tâches locales pour ses nouvelles pièces.

Toutes les requêtes utilisent `PoliteHttp`. Les tâches réseau sont admises une à la fois, même avec plusieurs
workers et clients, pour conserver la sérialisation des sources. Un espacement global conservateur de cinq
secondes est réservé durablement avant chaque tentative ; l’attente reste hors verrou et survit aux redémarrages.
Le budget compte les tentatives physiques, redirections et reprises
comprises, avant l’envoi, dans SQLite. Les plafonds d’octets et de délai du transport sont resserrés au budget
du travail. Un blocage ferme durablement cette source et suspend ses autres tâches, y compris les workers
déjà réservés. Aucun redémarrage automatique ne contourne ce blocage ; le statut conserve la cause.
Le rétablissement d’une source nécessite une décision opérateur après vérification, sans boucle de reprise.

## Ressources et vérification

`--max-attempts`, `--max-requests`, `--lease-seconds`, `--runtime-seconds` et `--max-bytes` sont figés dans
chaque tâche. Les dépassements documentaires et pages utiles non lues restent bloqués avec leurs compteurs.
Le temps générique Python est interrompu par un timer POSIX sur le thread principal ; les autres plateformes
refusent ce worker plutôt que d’annoncer une borne inexistante. Les lecteurs natifs utilisent leurs propres
processus et watchdogs. Un appel C non coopératif exige toujours ces watchdogs ou l’arrêt du conteneur.
Le timer ne borne pas la mémoire : conserver les plafonds CPU/mémoire/processus du service de jobs.
Le service web ne lance aucun de ces traitements lourds.

Les contrôles des fichiers de règles, références, communes, catalogue et code portent sur leurs contenus,
y compris lorsqu’une restauration conserve leur taille et leur date. Les options des collecteurs, dont
les années et dates par défaut, sont matérialisées dès le plan. Une modification entre mise en file et calcul,
ou avant publication, impose une nouvelle entrée explicite.

La publication d’un jeu est refusée tant qu’une tâche courante reste inachevée. Les tâches remplacées ne
deviennent pas des résultats et ne bloquent pas leur remplacement. Les tests couvrent crash après réservation,
expiration, ancien worker, remplacement des entrées, dépendance bloquée, source fermée, reprise des budgets,
interruption temporelle et rollback atomique, ainsi que le chemin réel lecture/extraction/rapprochement.
Ces contrôles techniques n’établissent ni rappel métier, ni coût mensuel, ni annotation indépendante.

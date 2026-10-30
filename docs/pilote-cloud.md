# Pilote ponctuel entièrement cloud

Ce profil prépare des lots reprenables de quelques centaines de pièces, avec leurs dossiers complets.
Il ne lance ni collecte complète, ni service permanent, ni appel payant pendant l'installation.
Le test d'endurance reste un chantier distinct.

## État livré et limites de la plateforme

Les outils `cloud_environment.environment_status`, `environment_diagnostics` et
`read_environment_diagnostic` lisent l'instance courante. La session de validation du 30 octobre 2026
est attachée à la configuration demandée, révision 1, active et connectée. La politique appliquée
est restreinte au preset `package_managers`, sans domaine administratif personnalisé, secret déclaré
ni identité sortante configurée. Le snapshot réseau indique aucun VPN et aucune autorisation TCP privée.

Le catalogue de compétences et les outils exposés ne fournissent **aucune opération d'édition,
d'enregistrement de brouillon, de publication ou d'application de configuration cloud**. Ils ne permettent
pas non plus de provisionner un disque privé durable, un bucket ou une entrée navigateur authentifiée
pour cette instance. Ces fonctions manquantes ne sont pas remplacées par une modification du JSON système.

| État | Constat |
| --- | --- |
| Fichier de demande préparé | `.codex/cloud-pilot.request.json`, versionné ; format documentaire interne, pas un schéma d'import plateforme. Renseigner sa cible depuis un nouvel état plateforme. |
| Brouillon enregistré dans la plateforme | Aucun : l'outil d'enregistrement n'est pas disponible. |
| Configuration plateforme appliquée | Configuration préexistante, révision 1 ; aucune autorisation réseau ajoutée. |
| Changements appliqués à cette session | Dépendances verrouillées, image Docker construite, répertoires privés et profils exécutés pour les essais techniques. |
| Environnement utilisable | Traitement hors réseau vérifié ; pilote réel bloqué par réseau, stockage durable, accès navigateur et classement. |

Une nouvelle session doit vérifier `source_config_id`, `source_config_version_id`, `spec_revision`,
`observed_spec_revision` et `observations_current`, puis l'état `enforced` du réseau et `ready` des
liaisons requises. Un fichier enregistré, une publication de réglages et une instance utilisant ces
réglages sont trois faits distincts. Aucun identifiant d'instance ancien ne doit servir de cible implicite.

## Préparer les dépendances et le stockage de session

Depuis le dépôt, avec l'utilisateur cloud non-root ayant accès au daemon Docker géré :

```bash
bash .codex/setup.sh
python tools/cloud_pilot.py --root /workspace/watt-pilot-private/pilot-20261030 init
python tools/cloud_pilot.py --root /workspace/watt-pilot-private/pilot-20261030 build
python tools/cloud_pilot.py --root /workspace/watt-pilot-private/pilot-20261030 check
```

Le setup impose Python 3.12 avec `uv sync --frozen --all-extras`, vérifie Git et signale l'absence
de Node ou d'OCR français sur l'hôte. Cette session dispose de Python 3.12.14, uv 0.12.23, Git et
Node 24.19.0. L'OCR français est préparé **dans l'image**, pas installé sur l'hôte. Docker 28.4.0 et
Compose 2.40.3 sont disponibles. Le build conserve le proxy, TLS et la CA combinée de la session,
montée temporairement dans BuildKit. Les dépendances Python sont verrouillées ; les paquets Debian
du build restent dépendants de l'état des dépôts. Conserver l'identifiant immuable de l'image testée
et passer `--image sha256:IDENTIFIANT_REEL` au contrôleur pour une reprise exactement identique.
Une image absente n'est jamais téléchargée automatiquement lors de l'exécution.

`init` est idempotent, ne copie ni labels ni données existantes et conserve son secret de relecture.
Le répertoire doit rester hors du dépôt public, appartenir à l'utilisateur et être en mode 0700.
La base et les journaux ne doivent pas être partagés entre machines sur un montage réseau SQLite/WAL.

| Chemin sous la racine privée | Usage |
| --- | --- |
| `data/radar.sqlite` | Dossiers, tâches, tentatives, réservations et blocages durables |
| `data/network-lock.sqlite`, `data/network-admission.sqlite` | Admission et cadence communes aux clients coopérants du même pilote |
| `data/blobs/`, `data/transcripts/`, `data/cache/`, `data/reference/` | Originaux, transcriptions, caches et références |
| `data/llm-jobs.sqlite` | Registre IA existant lorsqu'il est utilisé ; jamais remplacé par le cache |
| `data/gold/`, `data/evaluation/` | Références privées et campagnes ; labels non copiés à l'initialisation |
| `inputs/` | Spécifications, inventaire indépendant et entrées privées, montés en lecture seule |
| `reports/` | Journaux de commandes et état du contrôleur, privés |
| `backups/`, `restores/` | Snapshots vérifiés et essais de restauration |
| `control/review-token` | Secret local du prototype de relecture ; aucun montage dans les collecteurs |

Le daemon géré ne voit que ces montages déclarés, sans montage du PC ni accès au réseau personnel.
**Leur présence sur `/workspace` ne prouve pas leur conservation après recréation cloud.**

## Ressources et isolation effectivement contrôlables

Mesure de la session : quota cgroup de 4 CPU, 16 Gio de RAM, environ 30 Gio de disque disponible
avant construction (environ 25,7 Gio après), `/tmp` hôte d'environ 8,8 Gio ; aucun plafond hôte de
processus déclaré. Ces observations ne constituent pas une réservation permanente de ressources.

Le fichier **autonome** `compose.cloud-pilot.yaml` ne doit pas être fusionné avec `compose.yaml`.
Il retire les capacités, active `no-new-privileges`, utilise l'UID/GID non-root courant, une racine
en lecture seule, 2 CPU, 2 Gio de RAM sans swap, 128 processus et `/tmp` tmpfs de 512 Mio avec
`noexec,nosuid`. Fichiers ouverts : 1024/4096 ; taille d'un fichier : 512 Mio. Un lecteur PDF,
32 Mio par pièce, 300 pages PDF, 50 pages OCR, 20 millions de pixels OCR, temps de lecture 30 s,
temps OCR 60 s et tableaux plafonnés à 20 pages. Un dépassement documentaire reste incomplet.

Le contrôleur surveille toute la racine du pilote, y compris les sauvegardes, avec plafond de 6 Gio
et réserve de 6 Gio libres pour SQLite et les copies temporaires. Il contrôle avant démarrage et
chaque seconde, arrête le conteneur et écrit `state: incomplete`, `reason: storage_budget` au
dépassement. **Ce contrôle peut dépasser le seuil entre deux observations et pendant l'arrêt** ;
il ne remplace pas un quota de volume. Un pilote plus gros exige un volume dimensionné et un quota
effectif. Les copies SQLite par tâche, sauvegardes et restaurations consomment un espace additionnel.

Le collecteur reçoit seulement `data/`, `inputs/` en lecture seule et la CA TLS. Aucun montage de
`.git`, du domicile, des sauvegardes, du secret de relecture ou du socket Docker ; aucune transmission
de `GH_TOKEN`, clé de modèle ou secret de production. Ces propriétés ont été vérifiées dans les
processus réels. Le réseau reste celui du proxy de la session de développement : cela ne prouve pas
l'absence de tout pouvoir associé au proxy ou une séparation entre tenants cloud. Une séparation
forte exige un profil collecteur distinct, sans identité injectée ni accès Git en écriture et avec
ses seuls domaines ; le profil de développement conserve ses accès d'installation. Cette option est
préparée dans la demande, sans nouveau provisionnement facturable.

## Réseau, sources et premier lot

La demande reprend les deux connecteurs administratifs principaux, les cinq préfectures HDF et les
API de validation. Les domaines optionnels Géorisques, annuaire entreprises, ODRE et DuckDuckGo sont
documentés sans les ouvrir pour le premier lot : l'annuaire est une destination de liens, ODRE une
importation optionnelle, les autres correspondent à des connecteurs/recherches hors de ce premier lot.
Les domaines d'installation restent dans le profil de développement.

Le test effectif de la page DREAL configurée `2026-r5508.html`, avec `make_http`/`PoliteHttp` et
admission persistante, a rencontré `ProxyError: 403 Forbidden` après **une tentative**. Aucun PDF
nouveau n'a été téléchargé, aucune reprise ou route alternative n'a été tentée. Les autres domaines
administratifs ne sont pas présentés comme testés : leur absence est établie par la politique active.
Le contrôleur refuse la collecte si les neuf domaines requis manquent au snapshot restreint. Après
modification des réglages, revérifier aussi l'état plateforme courant et recréer la session si le
snapshot local ne correspond pas aux réglages appliqués.

Le premier contrôle réseau préparé dans la session est `inputs/network-smoke.json` : deux tâches
`explore`, DREAL HDF 2026 limité à un article et MRAe HDF 2026 limité à un avis. Il utilise uniquement
la liste et l'année déjà configurées dans le dépôt ; il ne prétend pas les avoir requalifiées aujourd'hui.
Son plan hors réseau a été calculé ; rien n'est mis en file à cette étape.

```bash
python tools/cloud_pilot.py --root /workspace/watt-pilot-private/pilot-20261030 run --mode offline -- radar tasks plan --generation cloud-smoke-20261030 --spec /inputs/network-smoke.json --max-requests 30 --runtime-seconds 300 --max-attempts 1 --max-bytes 67108864
# Après application et contrôle du réseau :
python tools/cloud_pilot.py --root /workspace/watt-pilot-private/pilot-20261030 run --mode offline -- radar tasks enqueue --generation cloud-smoke-20261030 --spec /inputs/network-smoke.json --max-requests 30 --runtime-seconds 300 --max-attempts 1 --max-bytes 67108864
python tools/cloud_pilot.py --root /workspace/watt-pilot-private/pilot-20261030 run --mode collect -- radar tasks run --max-tasks 1
```

Le mode `collect` fixe `RADAR_OFFLINE=0` **et** `RADAR_WORKER_OFFLINE=0`. Le mode `offline` conserve
les deux à 1 et impose `network_mode: none`. Les tâches d'un pilote partagent le même `data/` :
cadence persistante, réservations et suspension de source survivent aux lots. Un 403/429 ferme la
source ; aucune nouvelle génération ne doit servir à contourner sa suspension.

Pour les territoires suivants, observer d'abord des pages officielles de départ, ajouter uniquement
leurs hôtes vérifiés par le workflow plateforme, importer des `RegionConfig` explicites, puis exécuter
`radar catalog discover --region SLUG_OBSERVE --source dreal --years 2025 2026 --max-pages 4` et adopter
les identifiants réellement obtenus avec `radar catalog adopt --id IDENTIFIANT_OBSERVE`. Aucune URL
annuelle n'est déduite d'une année. Cette qualification et les hébergeurs externes d'annexes restent
**non vérifiés**, car les pages ne sont pas accessibles. Le scanner de dossiers reste borné et principalement
de même origine ; une annexe externe refusée ne devient pas une pièce complète simplement en ouvrant son hôte.

Après le petit contrôle, préparer des lots explicites par source/territoire en visant environ 300 pièces,
avec scans de dossiers complets et annexes. Les plafonds par tâche ne constituent pas un compteur global
de 300 téléchargements. Arrêter entre les lots et examiner les statuts ; augmenter un budget exige un
nouveau plan explicite. PDF textuels, scans, tableaux, Office et archives doivent être **rencontrés** et
contrôlés, pas fabriqués pour remplir des strates. `pilot plan` compte tous les membres d'un groupe et
exclut explicitement un groupe trop gros, sans le tronquer. Voir [pilote-qualite.md](pilote-qualite.md).

## Suivre, reprendre, arrêter et sauvegarder

```bash
python tools/cloud_pilot.py --root /workspace/watt-pilot-private/pilot-20261030 run --mode offline -- radar tasks status
python tools/cloud_pilot.py --root /workspace/watt-pilot-private/pilot-20261030 run --mode offline -- radar tasks enqueue --generation local-20261030
python tools/cloud_pilot.py --root /workspace/watt-pilot-private/pilot-20261030 run --mode offline -- radar tasks run --max-tasks 25
python tools/cloud_pilot.py --root /workspace/watt-pilot-private/pilot-20261030 run --mode offline -- radar quality-loop
python tools/cloud_pilot.py --root /workspace/watt-pilot-private/pilot-20261030 run --mode offline -- radar eval
python tools/cloud_pilot.py --root /workspace/watt-pilot-private/pilot-20261030 stop
python tools/cloud_pilot.py --root /workspace/watt-pilot-private/pilot-20261030 run --mode offline -- radar backup --out /backups --keep 3
```

Chaque appel rend un JSON avec chemin du rapport et journal `.log` associé. `process_exited` avec
code 0 signifie que **la commande** est revenue ; lire le JSON `radar` du journal pour les tâches
bloquées, dossiers incomplets et gates qualité. Après une interruption, consulter les baux/tentatives,
puis reprendre les mêmes tâches ; les réservations ne sont pas remises à zéro. Une modification des
entrées exige `enqueue` ; une nouvelle collecte utilise une génération explicite.

Le snapshot rendu sous `/backups/NOM_REEL` se restaure dans un répertoire absent ou vide :

```bash
python tools/cloud_pilot.py --root /workspace/watt-pilot-private/pilot-20261030 run --mode offline -- radar restore /backups/NOM_REEL --to /restores/reprise
python tools/cloud_pilot.py --root /workspace/watt-pilot-private/pilot-20261030 run --mode offline -- env RADAR_DATA_DIR=/restores/reprise RADAR_GOLD_DIR=/restores/reprise/gold radar tasks status
```

La sauvegarde SQLite conserve le WAL validé, les tâches et le registre IA durable ; les caches
reconstructibles restent exclus. Le secret de relecture et les spécifications sous `inputs/` restent
hors du snapshot applicatif : les conserver séparément dans le stockage privé/gestionnaire de secrets.
Le contrôleur et Docker tournent dans le cloud. Aucune garantie de survie des processus après fermeture
du chat, mise en veille ou recréation de session n'est disponible ; les données de reprise ne sont
utiles que si leur stockage survit. Aucun ordonnanceur ni tâche de quatre semaines n'a été activé.

## Sauvegarde hors instance et consultation navigateur à terminer

L'option préparée est un hôte cloud avec disque local persistant chiffré (40 Gio initiaux), un seul
écrivain SQLite, des snapshots privés versionnés hors hôte et une passerelle HTTPS authentifiée
OIDC ou équivalente avec liste d'utilisateurs autorisés. Le proxy côté cloud rejoint uniquement
`127.0.0.1:8766` ; aucun tunnel ne tourne sur le PC. Les documents bruts restent sur le volume privé.
Le collecteur est un profil sans secret ; seul le composant de sauvegarde possède l'identité bucket,
et seul le service de relecture possède le secret applicatif. La restriction egress doit être assurée
par le fournisseur/proxy : Compose seul ne l'applique pas sur un autre hébergeur.

Après choix et provisionnement du stockage privé, une identité cloud à droits limités et le CLI du
fournisseur permettent par exemple une copie S3. Ces commandes sont une **recette non exécutée**,
aucun bucket ni identifiant n'étant configuré :

```bash
: "${WATT_PRIVATE_BUCKET:?bucket privé réellement provisionné}"
: "${WATT_BACKUP_PREFIX:?préfixe privé de ce pilote}"
: "${WATT_SNAPSHOT_NAME:?nom du snapshot validé rendu par radar backup}"
aws s3 sync "/workspace/watt-pilot-private/pilot-20261030/backups/$WATT_SNAPSHOT_NAME/" "s3://$WATT_PRIVATE_BUCKET/$WATT_BACKUP_PREFIX/$WATT_SNAPSHOT_NAME/" --sse AES256 --only-show-errors
# Depuis une NOUVELLE instance cloud autorisée, vers un répertoire privé vide :
aws s3 sync "s3://$WATT_PRIVATE_BUCKET/$WATT_BACKUP_PREFIX/$WATT_SNAPSHOT_NAME/" "/workspace/watt-pilot-private/pilot-20261030/backups/$WATT_SNAPSHOT_NAME/" --only-show-errors
```

Exiger bucket bloquant l'accès public, chiffrement, versionnement, rétention définie et droits limités
à ce préfixe ; sélectionner l'identité via le manifeste non secret de la plateforme lorsqu'il existe,
sans exporter ses valeurs. Vérifier ensuite `radar restore`, les empreintes et les états de tâches sur
la nouvelle instance **avant** d'annoncer une conservation après recréation. Aucun transfert cloud,
politique bucket ou restauration après recréation de cette instance n'a été testé ici.

```bash
python tools/cloud_pilot.py --root /workspace/watt-pilot-private/pilot-20261030 review
```

Cette commande lance uniquement l'origine privée sur la **loopback du cloud**, avec token exigé sur
les API/documents. Ce n'est pas une URL consultable depuis le navigateur de l'utilisateur. La page
statique et la santé sont publiques au niveau applicatif, sans contenu projet ; la future passerelle
devra protéger toutes les routes. Le secret local du prototype doit être remplacé par une liaison au
gestionnaire de secrets pour un service hébergé. Aucun accès navigateur hébergé n'est déclaré actif.

Le fournisseur, l'identité autorisée et le budget infrastructure ne sont pas fournis. Aucun service
facturable supplémentaire n'a été provisionné. Un devis est nécessaire pour calcul, disque, bucket,
requêtes/transferts et authentification avant provisionnement ; le coût de la session actuelle dépend
du forfait plateforme. Une offre nouvelle ne doit pas être annoncée gratuite sans vérification.

## Classement et conditions humaines

Le pilote démarre sans classificateur et sans copie des annotations de référence. De nouveaux dossiers
peuvent être lus et extraits tout en restant sans catégorie ; l'absence de projets détectés ne démontre
pas l'absence de projets dans les sources. Tous les budgets IA restent à zéro, modèles non configurés,
aucun appel soumis. Le registre `llm-jobs.sqlite` est conservé si le chemin IA est activé ultérieurement.

Si un modèle est retenu : fournisseur actuellement raccordé Anthropic, hôte `api.anthropic.com`, secret
`ANTHROPIC_API_KEY` dans un profil modèle séparé, modèle réellement disponible et tarifs fournisseur
datés/versionnés sous `RADAR_LLM_PRICING_PATH`. Aucun tarif n'est inventé ; le fichier préparé vide ne
permet pas de facturer un modèle inconnu. Garder les trois budgets à zéro jusqu'à l'enveloppe explicite.
Le chemin payant est `radar run --llm-batch`, suivi de `radar llm-batch status|collect`, puis d'une
reconstruction ; `tasks run` réutilise les réponses disponibles et ne soumet pas de nouveaux appels.
Ne pas lancer ces commandes payantes dans le profil collecteur ni utiliser gold pour fabriquer un succès.

Restent nécessaires : sources et territoires réellement qualifiés, inventaire indépendant, deux lecteurs
distincts, arbitrage des désaccords, suivi du temps humain et population prospective appropriée. Les tests
techniques et formulaires vierges ne fournissent aucune annotation ni précision métier.

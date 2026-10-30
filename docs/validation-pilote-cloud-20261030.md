# Validation du profil cloud ponctuel — 30 octobre 2026

Ce rapport conserve la validation de préparation sur l'ancienne configuration réseau.
La [reprise sur la nouvelle instance et les résultats réels](resultats-pilote-cloud-20261030.md)
sont consignés séparément, sans réutiliser ses identifiants d'instance ni ses données privées.

Le [guide d'exploitation](pilote-cloud.md) contient les commandes exactes, chemins, limites et
prérequis. La [demande préparée](../.codex/cloud-pilot.request.json) reste un fichier documentaire :
**aucun brouillon plateforme n'a été enregistré, aucune modification réseau n'a été appliquée**.
La copie privée ciblée est sous
`/workspace/watt-pilot-private/pilot-20261030/control/cloud-change-request.json` dans cette session.
Elle conserve l'identité de configuration et la version relues, sans publication de ces identifiants
dans le dépôt. Les [preuves techniques structurées](measurements/pilote-cloud-20261030.json)
ne contiennent aucun document brut, secret ou export humain.

## Configuration et disponibilité réelle

L'outil plateforme confirme que l'instance de cette session utilise bien la configuration demandée,
révision observée 1, observations courantes, phase `running`, connexion `connected`. La politique
`restricted` est `enforced`, mais reste limitée au preset `package_managers`, sans domaine personnalisé.
Aucune opération de configuration save/publish/apply n'est exposée par les outils disponibles.
Le skill runtime consulté décrit ces lectures et renvoie au workflow de configuration, absent ici.

Un accès réel par `make_http` / `PoliteHttp` à la liste DREAL HDF configurée renvoie
`ProxyError: 403 Forbidden` après **une tentative**. Arrêt immédiat ; aucun document nouveau récupéré,
aucune route alternative ni désactivation de TLS. Les neuf hôtes de la demande restent absents de la
politique. La garde du contrôleur refuse de lancer le profil collecteur dans cet état.

Les étapes qui restent inaccessibles sont : application réseau, observation de nouvelles sources et
territoires/annexes, téléchargement réel puis traitement de ce petit lot, stockage durable hors instance
et entrée navigateur privée hébergée. **Le pilote réel complet n'est pas activé.**

## Changements exécutés dans la session

- Setup verrouillé répété avec succès, sans collecte : Python **3.12.14**, uv **0.12.23**, Git,
  Node **24.19.0**. L'hôte n'a pas Tesseract français ; l'image le fournit.
- Docker **28.4.0**, Compose **2.40.3**, image construite avec proxy, TLS et CA temporaire :
  `sha256:42fb1d4c03838bb9e275b055ad254de877cdb5214b4054d3f5fd317df4f61809`.
  Le chemin de build du nouveau contrôleur a également réussi, en réutilisant les couches préparées.
- Initialisation privée du futur pilote sous `/workspace/watt-pilot-private/pilot-20261030` ;
  aucune donnée ni référence préexistante copiée. Plan de **deux** tâches réseau bornées calculé
  hors ligne ; aucune mise en file ni collecte réelle déclenchée.
- Essais sur un espace distinct : `/workspace/watt-pilot-private/validation-20261030`.
  Les journaux détaillés sont dans `reports/`, les snapshots dans `backups/` et la restauration
  dans `restores/smoke/`. Ces chemins restent liés au stockage de cette session.

Le processus collecteur réel, lancé uniquement pour une inspection sans requête externe, possède
UID/GID **1000**, capacités effectives nulles et `NoNewPrivs=1`. Il ne reçoit aucune des variables
GH/GitHub, Anthropic, jeton API ou clés AWS inspectées ; ni socket Docker, ni dépôt hôte, ni secret de
relecture ne sont visibles. Proxy présent, budgets IA tous à zéro. Ces faits concernent les montages
et processus testés ; ils ne prouvent pas une séparation d'identité au niveau du proxy de développement.

Les cgroups vérifient **2 CPU, 2 Gio RAM, zéro swap et 128 processus**. `/tmp` est un tmpfs de
**512 Mio** `noexec,nosuid`, la racine refuse l'écriture ; limites de fichiers ouverts **1024/4096**,
fichier individuel **512 Mio**. Tesseract énumère `eng`, `fra`, `osd`. Une reconnaissance française
hors réseau sur une image **synthétique explicitement technique** a réussi ; ce n'est ni un scan
administratif nouvellement rencontré, ni une mesure de qualité OCR métier.

## Traitement, reprise, preuves et restauration

Le corpus public déjà caviardé, **11 PDF / 7 dossiers**, a été importé sans ses labels de catégorie.
Le worker hors réseau termine **19 tâches** avec zéro requête physique ; sa seconde exécution
n'effectue **aucun travail**. Lecture/extraction conservent **67 preuves localisées** dans ces dossiers,
notamment des champs AcroForm et clauses d'arrêtés. Les nouveaux résultats restent sans classement
indépendant ; zéro projet construit dans ce rejeu n'est pas une conclusion métier.

`radar backup` a produit un snapshot privé cohérent. `radar restore` vers une destination vide
réussit, `integrity_check` retourne `ok`. Les **11 originaux**, lignes documentaires, **19 tâches**,
historique de tentatives et dépendances ont les mêmes empreintes logiques après restauration.
Le cache reconstructible est volontairement exclu. Aucun transfert vers un bucket n'a eu lieu.

La relecture a été réellement démarrée sur `127.0.0.1:8766` **du cloud**. `/api/projects` et
`/api/review` renvoient **401** sans secret et **200** avec secret ; aucune annotation n'a été créée.
L'interface statique seule renvoie 200 sans contenu projet. Après arrêt, suppression et recréation
du conteneur, la relecture répond toujours 200 avec authentification et les **19 tâches terminées**
sont présentes. Les conteneurs de validation ont été arrêtés en fin d'essai.

**La recréation de l'environnement cloud n'a pas été testée.** Une recréation de conteneur ne prouve
pas que `/workspace` survivra à une nouvelle instance. Aucun accès depuis le navigateur utilisateur
via une passerelle privée hébergée n'a été activé. La recette de stockage hors hôte et l'option
d'entrée HTTPS authentifiée sont préparées dans le guide, avec coûts à faire chiffrer avant provisionnement.

Le refus disque a été exercé réellement avec une limite volontairement inférieure à l'état présent :
sortie **2**, rapport `incomplete` / `storage_budget`, aucun conteneur démarré. Le contrôle surveillé
par seconde peut dépasser son seuil ; aucun quota de volume dur n'est revendiqué.

## Contrôles du dépôt et conditions restantes

**649 tests réussis, 1 ignoré en 172,22 s**, format Ruff, Ruff et mypy strict sans erreur
(157 modules applicatifs et le contrôleur). Le contrôle JavaScript existant passe dans la suite.
Les six tests ajoutés couvrent refus disque, réserve libre, absence de suivi de lien symbolique,
dépassement par un lot très court, refus réseau et conservation des entrées privées à la réinitialisation.

`radar run`, `shadow --strict` et `cases check --strict` passent sur la base préexistante vide.
`radar eval` ne relève aucune régression ; son gate reste **failed**, faute de références humaines
indépendantes. Le test ignoré dépend de cas humains privés absents ; aucun échec n'a été désactivé.
Les seuils ne sont pas abaissés. Aucune qualité métier, exhaustivité documentaire ou capacité de
production n'est mesurée par ces contrôles.

Restent nécessaires : outil plateforme de configuration, domaines effectivement appliqués, observation
et qualification de territoires supplémentaires, stockage privé persistant et sauvegarde hors instance,
passerelle navigateur authentifiée, choix d'un classificateur indépendant et éventuel budget de modèle
avec tarifs versionnés, inventaire indépendant et deux lectures humaines. **Budgets IA : 0 / 0 / 0 USD**,
aucun nouveau service facturable provisionné, aucune collecte d'endurance activée.

# Premier pilote réel commencé le 30 octobre 2026

Le pilote ponctuel a collecté **212 fichiers uniques : 110 PDF et 102 pages HTML**, associés à
**91 dossiers**. La campagne de double lecture retient **206 documents, 90 dossiers et 84 groupes
techniques**. Elle contient **109 PDF et 97 HTML** : le nombre de documents inclut les pages de
dossier, et ne désigne donc pas 206 PDF.

**Le gate qualité échoue.** Les lectures incomplètes, les alertes de confidentialité et les preuves
à revoir restent présentes. Aucun jeu de données approuvé ni service public n'a été publié.
La collecte s'est terminée le 30 octobre ; les derniers contrôles et le bilan ont été produits
dans la nuit du 30 au 31 octobre, en heure de Paris. Aucun test d'endurance ou ordonnanceur
permanent n'a été lancé.

Les [mesures agrégées](measurements/pilote-cloud-reel-20261030.json) accompagnent ce rapport.
Les originaux, bases, sorties machine détaillées, journaux, secrets et formulaires restent sous
`/workspace/watt-pilot-private/pilot-20261030`, hors du dépôt public.

## Configuration et accès réellement observés

L'état plateforme relu indique `running`, `connected`, révisions demandée et observée **1 / 1**,
`observations_current: true`, réseau `restricted/enforced`. Les **neuf hôtes demandés** figurent
dans la politique effective et dans `/etc/codex/network-policy.json`. La version de configuration
observée porte l'identifiant `cecfgver_6ae51397501c81a08e405cde4c29c5bc` ; les identifiants complets
de configuration et d'instance sont conservés dans les journaux privés `reports/environment-status-*.json`.
Le numéro de révision seul n'a pas servi de preuve : les accès DREAL/MRAe ont effectivement ramené
des pièces. Aucun 403/429 n'a été observé et aucune suspension de source n'a été enregistrée.
Les autres hôtes ont été vérifiés dans la politique, sans sonde HTTP : les préfectures et les API
de validation n'ont pas été appelées pour enrichir ce traitement hors réseau.

Les tâches ont comptabilisé **274 tentatives HTTP physiques**, avec `PoliteHttp`, admission persistante,
cadence de cinq secondes et budgets bornés. Trois pages supplémentaires ont été lues par les commandes
de qualification ; leurs tentatives physiques n'étaient pas instrumentées dans le compteur des tâches.
Le total réseau global n'est donc pas annoncé comme exactement 274. Aucun contournement du proxy
ou changement de la politique système n'a été effectué.

Le checkout initial, propre sur `work`, était à `f1dc6cc`. `main` a été récupéré à `330c0ed` et le
travail a continué sur une branche dédiée. Les fichiers privés et images de la précédente session
étaient absents. Le setup verrouillé, le contrôleur et le Compose existants ont été réutilisés.

## Couverture et composition du corpus

| Périmètre configuré | Observation finale |
| --- | --- |
| DREAL HDF 2026 | 33 pages de liste, curseur 264, état technique `complete` |
| DREAL HDF 2025 | 18 pages, curseur 144, état `partial/page_budget` |
| MRAe HDF 2026 | Une page, 26 avis reconnus, 13 PDF sélectionnés, état technique `complete` |
| Préfectures et archive DREAL antérieure à 2025 | Non collectées |
| MRAe 2025 et autres régions | Non qualifiées pour ce pilote |

La qualification bornée a observé un lien vers une page Normandie. L'adoption sous le périmètre HDF
a été refusée par le contrôle régional ; aucune URL annuelle n'a été inventée ni ce lien adopté.
Les cinq départements HDF apparaissent dans les champs extraits ; neuf dossiers restent sans
département extrait. Ces champs ne constituent pas une validation géographique indépendante.

Le corpus comprend **199 fichiers DREAL et 13 MRAe**. Les rôles affectés par le logiciel sont
109 formulaires, 90 décisions et 13 avis. Les PDF représentent **1 119 pages** ; 32 PDF comportent
au total **263 pages signalées sans couche textuelle suffisante** à l'ingestion. DOCX, XLSX et ZIP
n'ont pas été rencontrés : leurs branches de lecture ne sont pas évaluées ici.

Les **111 scans de dossiers** sont marqués complets selon le parcours reconnu de même origine.
Cela atteste les liens parcourus, sans établir l'exhaustivité juridique des dossiers, les pièces
non reconnues, les annexes externes ou le rappel sur un inventaire indépendant absent.

Le premier contrôle réel a révélé des liens de navigation générale ajoutés à un dossier. Le correctif
[9417d26](https://github.com/Dan-Popescu/watt-horizon/commit/9417d2681861c79c85b014d52fad2a9ef643d37c)
exclut les zones de navigation de la recherche d'annexes et conserve les liens du contenu, y compris
les pages intermédiaires. Le test reproduit l'erreur avant correction ; le rejeu de la page observée
ne conserve ensuite que son PDF. Les **quatre pages de navigation erronées** restent archivées.
Le dossier affecté entier, soit six fichiers, est exclu de la campagne ; aucun membre n'a été retiré
silencieusement d'un groupe retenu. Les anciens arcs `budget` de cette exploration sont conservés
comme historique. Hors de ce dossier exclu, les arcs observés ne comportent aucun refus de budget,
format non pris en charge ou annexe externe signalée.

## Lecture, extraction et limites constatées

Le premier passage des tâches de lecture conserve ses résultats : **99 complets, 112 incomplets
et un échec**. Les 102 HTML signalent des ressources liées ou images non lues ; leur texte peut
rester exploitable. Dix PDF sont incomplets et un PDF dépasse le délai natif de 30 secondes.

Sur les **109 PDF ayant rendu un résultat** au premier passage :

- 1 091 pages décrites, dont 846 avec couche textuelle ; le PDF en échec de 28 pages reste hors de ce dénominateur ;
- 245 pages soumises à l'OCR local français, **231 avec texte obtenu et 14 restant non lues** ;
- 1 018 pages soumises à la recherche de tableaux, **716 tableaux et 3 835 lignes détectés** ;
- 73 pages exclues de l'analyse de tableaux par le plafond de 20 pages par PDF ;
- 65 PDF avec noms de champs AcroForm, dont 35 avec des champs remplis, pour 3 558 champs remplis lus.

Ce sont des compteurs techniques, sans transcription de référence, WER/CER ou validation des cellules.
Le besoin d'OCR n'est pas assimilé à une erreur humaine démontrée, ni un tableau détecté à un tableau correct.

Le Cerfa `2025-8575`, 28 pages et 5 636 576 octets, a dépassé les 30 secondes. Une reprise explicite
à **60 secondes pour cette pièce seule**, avec les mêmes CPU/RAM et plafonds de tableaux/OCR,
a produit sa sortie partielle. L'échec initial reste dans le registre. Les autres sorties machine
conservent leur politique initiale. Le premier calcul global avait produit 90 sorties, puis le
regroupement a refusé des relations traversant les partitions figées. Le manifeste `cloud-pilot`
a été conservé avec son hash inchangé ; une campagne distincte `cloud-pilot-observed-20261030`
a été ouverte, sans prétendre établir l'indépendance d'un nouveau test.

Les **91 dossiers possèdent maintenant une sortie machine**, avant toute correction humaine.
Le worker initial avait publié onze extractions ; la commande historique `run` a également conservé
les extractions partielles des autres dossiers avec leurs flags. Les tâches initialement bloquées
ou en échec n'ont pas été déclarées réussies. Une seconde exécution du worker a rendu `work: []`,
sans tentative ni requête supplémentaire.

## Preuves, contradictions et qualité métier

Le logiciel a produit **623 objets `Evidence`**, dont 375 repères AcroForm, 131 clauses et
48 paragraphes. Les valeurs non nulles comprennent une puissance MW dans 18 dossiers, une capacité
MWh dans sept, une commune dans 57 et un SIRET dans 33. Ces effectifs mêlent candidats et audits
de documents non ciblés ; ils mesurent la présence de sorties, sans mesurer leur exactitude.
La puissance MW reste inconnue dans 73 dossiers et la capacité MWh dans 84.

Exemples de développement, sans annotation humaine ajoutée :

- `006741` : date extraite `2026-01-13`, document `dd82ae9f4057…`, repère
  `field:Dossiercompletdate`, citation brute `13-01-2026` ;
- `012602` : désaccord signalé entre **0,99 MW dans le formulaire et 40 MW dans la décision**.
  La citation AcroForm parle de production estimée à `0.99 MWc`. Son actif, son unité et son contexte
  doivent être arbitrés avant toute interprétation comme puissance de raccordement ;
- `012538` : treize observations quantitatives conservées, puissance et capacité laissées inconnues
  pour incertitude documentaire ou rattachement à un autre actif. Un désaccord de date d'un jour est aussi signalé.

Au total, trois flags de désaccord et six de contexte quantitatif restent à revoir. Le contrôle
signale **deux citations introuvables**. En outre, **28 codes département n'ont pas d'entrée directe
`Evidence.department`** : leurs dérivations demandent une provenance explicite, sans que ce compteur
constitue 28 erreurs de valeur démontrées.

L'audit de confidentialité à 30 secondes refusait de conclure sur le PDF bloqué. À 60 secondes,
`eval` termine mais détecte **deux alertes sur un dossier**, dans une preuve et le vocabulaire.
Les contenus concernés restent privés. Leur présence interdit d'annoncer un audit sans alerte.
Les méthodes de classement enregistrées sont toutes `rules` : 49 `other`, 41 `unclassified` et
un `needs_review`. **Aucun classificateur indépendant n'est configuré**, aucune référence humaine
n'est disponible et aucun rappel, précision métier ou qualité complète de détection n'est mesuré.
Les zéro projets construits ne permettent pas de conclure à l'absence de projets dans ces sources.

Le gate d'évaluation reste **`failed`**, notamment pour tâches inachevées, preuves invalides,
alertes de confidentialité et échantillons indépendants absents. Aucun seuil n'a été abaissé et
aucune référence approuvée n'a été créée. La stabilité sous perturbations artificielles, fournie
dans le JSON, est une mesure de robustesse et non une précision métier.

Les deux formulaires contiennent chacun **90 fiches et 1 196 observations `unreviewed`**,
zéro lecteur attribué et zéro ligne vérifiée. Les 84 groupes sont tous prévus en double lecture.
L'effort humain reste non mesuré. `campaign` a été utilisé ; `pilot prepare`, qui exige une véritable
déclaration d'inventaire externe manuel, n'a pas reçu de provenance inventée.

## Temps, ressources et caches

Setup : Python hôte 3.12.14, uv 0.12.23, Git 2.52.0, Node 24.19.0 ; Docker 28.4.0 et Compose 2.40.3.
Image : Python 3.12.16, PyMuPDF 1.28.2 et Tesseract **5.5.0 français**. L'image testée est
`sha256:83130f968d6a4c421ff8adb6b4f551998ab34adadffdc619b887948790811355`.
Les hashes des 156 fichiers Python de l'image correspondent au checkout du correctif.

Les conteneurs vérifiés appliquent **2 CPU, 2 Gio RAM sans swap, 128 processus**, racine en lecture
seule, capacités nulles, `no-new-privileges`, `/tmp` tmpfs 512 Mio `noexec,nosuid`, fichiers ouverts
1024/4096 et fichier individuel 512 Mio. L'orchestrateur est dans un cgroup hôte de 4 CPU et 16 Gio.
Les limites de lecture sont 32 Mio par fichier, 300 pages PDF, OCR 50 pages/20 millions de pixels/60 s,
tableaux 20 pages. Le collecteur ne monte ni dépôt Git, socket Docker, secret de relecture ni clé de modèle.
Son egress conserve cependant le preset de développement ; un profil collecteur séparé reste à prévoir
pour une isolation d'identité plus forte.

| Mesure contrôleur | Durée observée |
| --- | --- |
| Quatre lots de collecte après le petit contrôle | 22 min 23 s |
| Trois lots de tâches locales, premier passage | 12 min 41 s |
| `run` initial refusé lors du regroupement | 49,5 s |
| Reprise ciblée à 60 s | 98,0 s |
| Évaluation à 60 s | 175,8 s |
| Sauvegarde / restauration | 9,0 s / 8,0 s |

Le maximum mémoire observé atteint **1,65 Gio** pendant la reprise du PDF, contre environ 230 Mio
au maximum pendant les lots de collecte instrumentés. Les pics cgroup sont échantillonnés toutes
les dix secondes ; l'intervalle final non échantillonné n'est pas certifié. Ces mesures sur cette
instance ne démontrent pas un débit de production.

Les originaux occupent **161,6 Mio**. Le cache opérationnel final occupe environ **3,4 Mio** :
423 entrées du namespace historique `pdf`, qui inclut aussi les HTML et les deux délais de lecture,
et 32 entrées OCR. Ce ne sont pas 423 documents distincts. Le cache était vide avant le traitement.
Les résultats bruts des premier et dernier passages sont archivés séparément ; les échecs OCR
ne sont pas transformés en transcriptions inventées.

## Conservation et reprise réellement testées

La racine privée appartient à l'UID 1000 et est en mode 0700. Avec sauvegarde, restauration et
archives de contexte, elle occupe environ **537 Mio** ; environ **23,1 Gio** restent disponibles.
Le contrôleur surveille 6 Gio de données et une réserve libre de 6 Gio chaque seconde.
Ce dispositif ne constitue pas un quota de volume dur et peut dépasser le seuil entre deux observations.

Le snapshot `backups/watt-snapshot-20261030T230601242299Z-2ed3c0d2877d` a été restauré vers
`restores/recovery`. `integrity_check` rend `ok` ; les **32 tables applicatives**, les **212 originaux**,
les **1 073 définitions de tâche**, **229 tentatives** et **1 575 dépendances** ont les mêmes contenus
logiques ou hashes après restauration. Les états de blocage et budgets consommés sont conservés.

Le snapshot applicatif exclut le cache opérationnel reconstructible. Le cache du premier passage
est figé dans `data/session-analysis/initial-reading-cache.sqlite` et celui du dernier dans
`backups/final-reading-cache.sqlite` ; les 455 entrées de ce dernier ont aussi été vérifiées après copie.
`backups/pilot-session-context-20261030.tar` conserve séparément entrées, journaux, scripts opératoires
et secret local, avec accès privé. Les snapshots machine avant et après reprise sont sous
`data/session-analysis/`; le snapshot de la campagne est `data/campaigns/pilot-20261030/initial.json`.

**Toutes ces copies restent sur cette instance.** Le rafraîchissement des outils a retrouvé les
données et processus en cours ; aucune recréation cloud n'a été testée. Aucun bucket, volume durable,
sauvegarde hors instance ou passerelle de relecture hébergée n'est configuré. La présence du code
dans Git ne sauvegarde pas les pièces privées ni les images Docker.

Restent à traiter avant une publication de données : alertes de confidentialité, citations et
provenances incomplètes, ressources HTML et pages OCR/tableaux non lues, réconciliation des tâches,
inventaire indépendant et double lecture réelle. L'élargissement territorial et aux autres formats
exige de nouvelles sources observées. Un classificateur indépendant sera nécessaire pour mesurer
la détection complète. La persistance et une sauvegarde privée hors instance restent à provisionner
avec une identité et un budget d'infrastructure explicites.

Les budgets IA sont restés **0 / 0 / 0 USD**, sans soumission de nouvel appel de modèle.
Les contrôles du code donnent **650 tests réussis, un ignoré**, Ruff format, Ruff et mypy strict
sans erreur. Le test ignoré exige des cas humains privés absents. Ces résultats techniques
n'approuvent pas le jeu documentaire et aucune infrastructure facturable supplémentaire n'a été créée.

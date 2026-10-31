# Revue IA du pilote et extension du corpus

Décision du 31 octobre 2026 : exploiter d'abord le corpus actuel, puis viser **500 à 1 000 fichiers
pour 200 à 300 groupes candidats**, en conservant les dossiers et annexes entiers. La collecte
progressive est autorisée. Pour cette étape, seules des revues IA sont disponibles : **aucune
annotation humaine vérifiée n'est créée**. Ce protocole complète le
[bilan du premier pilote](resultats-pilote-cloud-20261030.md).

## Unités et limites de l'évaluation

Le pilote initial conserve 212 fichiers et 91 dossiers. La campagne sélectionne 206 fichiers
(109 PDF et 97 HTML), 90 dossiers et 84 groupes techniques. Le groupe initial contaminé par
des liens de navigation reste exclu entier. Deux recomptages indépendants ont vérifié les
effectifs, les empreintes des originaux et les formulaires humains restés vierges.

| Partition figée | Groupes | Dossiers | Usage dans cette étape |
| --- | ---: | ---: | --- |
| Développement | 36 | 36 | Lecture détaillée et diagnostic |
| Calibration | 22 | 23 | Métadonnées et intégrité uniquement |
| Test | 26 | 31 | Métadonnées et intégrité uniquement |

Un fichier, un dossier et un groupe ne sont pas trois observations indépendantes. L'absence
de collision entre groupes par empreinte, URL ou identifiant administratif ne prouve pas
l'indépendance des projets réels. Celle-ci reste à qualifier. Les détails des erreurs réservées
ne sont pas remis aux agents chargés des corrections. Renommer une campagne ne crée pas un
nouveau test indépendant.

Les formulaires humains A/B restent inchangés : 90 fiches chacun, toutes non vérifiées, aucun
lecteur humain attribué et aucun effort humain déclaré. Les avis IA sont dans un registre privé
distinct, avec `source_only: true` et `human_verified: false`. Ils ne sont pas importés dans
`gold`, les corrections ou le consensus humain.

## Trois lectures séparées, sans prédictions

Les lecteurs utilisent le même modèle que le coordinateur, avec raisonnement extra-élevé,
conformément à la demande. Chacun reçoit les sources masquées et leurs limites, sans base de
données, sortie machine, candidats d'extraction, référence attendue ou avis d'un autre lecteur.
Le troisième lecteur lit également les sources avant toute confrontation aux autres avis.

Les paquets de développement contiennent 84 documents et 357 pages, avec les couches texte,
OCR disponible, champs, cellules et segments conservés dans les caches. Chaque document et
paquet porte une empreinte ; chaque valeur affirmée exige une citation exacte, un document et
un repère. La lecture d'une citation prouve sa présence dans le paquet masqué, sans certifier
le PDF d'origine, l'OCR, l'unité, le rattachement au bon actif ou le fait que la citation justifie
la valeur affirmée.

Le premier export a laissé passer des résidus possibles dans des signatures numériques. Les
lectures ont été suspendues ; les paquets et avis V1 ont été conservés, puis les paquets V2
ont été produits sans écrasement. Les groupes modifiés doivent être réellement relus avant
un nouvel avis, jamais simplement rattachés à une nouvelle empreinte.

La V2 occulte entièrement 51 pages de signature ou certificat. Deux contrôles distincts
vérifient les spans de masquage et les indices détectables sur 19 450 composants. Zéro alerte
dans ces contrôles **ne garantit pas une anonymisation exhaustive** : un nom inconnu en prose,
sans champ ou contexte reconnaissable, peut échapper aux heuristiques. Aucune identité résiduelle
n'est recopiée dans les avis ou le dépôt public. Toute nouvelle détection suspend les lectures
concernées et impose une nouvelle version du paquet.

Règles de notation :

- `present` : valeur établie par une pièce et une preuve localisée ; MW, MWc, MWh, actif cible,
  équipement, installation existante et variante restent distingués ;
- `unknown` : masque, lecture partielle, ambiguïté, contradiction ou preuve insuffisante ;
- `absent` : uniquement après lecture complète des pièces pertinentes, sans zone utile omise.

Le validateur privé vérifie les empreintes, les slots séparés, les documents déclarés lus et
les citations au repère exact. Il refuse les formulaires humains natifs et les avis sur une
ancienne version. Il rapporte les accords explicites, doubles inconnus et désaccords séparément.
Les accords sur des inconnus ne deviennent pas des réponses correctes. Les constats critiques
et les désaccords nécessitent une troisième vérification ; un désaccord non résolu reste inconnu.

Trois avis du même modèle sur le même OCR restent corrélés. Leur accord est un indicateur de
reproductibilité des avis, **pas une précision métier ni une vérité indépendante**. Sans
classificateur indépendant configuré, la détection complète n'est pas évaluée. Les revues
permettent un diagnostic de lecture et d'extraction, sans approuver un jeu de production.

## Contrat de la seconde phase

La seconde phase étend les lectures aux groupes candidats du corpus enrichi. Les nombres
ci-dessus décrivent uniquement la première phase. Les nouveaux effectifs ne sont arrêtés
qu'après collecte, lecture locale et vérification des regroupements. Un manifeste distinct
conserve les dossiers et toutes leurs pièces observées, leurs empreintes et les limites de
lecture. Une campagne d'avis IA sur des sources partielles reste explicitement observationnelle ;
elle ne contourne pas les conditions de préparation ou de publication d'une campagne native.

Le groupe est l'unité d'échantillonnage, mais **chaque dossier reçoit sa propre fiche par lecteur**.
Une citation doit appartenir à une pièce attachée à ce dossier. La présence d'une pièce dans
un autre dossier du même groupe ne suffit pas. Un groupe n'a deux revues complètes que lorsque
chacun de ses dossiers possède les deux fiches valides. Les rôles d'une pièce partagée restent
définis par dossier. Toute empreinte partagée entre groupes bloque l'export jusqu'à résolution
du regroupement ; elle ne devient pas une observation supplémentaire.

Les paquets de développement et les paquets réservés sont séparés. Les seconds sont destinés
à des lecteurs IA isolés, qui ne reçoivent que les sources masquées, sans prédictions ni erreurs
du pipeline. Le coordinateur du développement reçoit seulement leurs compteurs agrégés et ne
consulte pas leurs valeurs, citations ou désaccords individuels. Ces annotations de sources ne
sont ni importées comme références humaines ni utilisées pour régler le code. Toute appartenance
historique à la calibration ou au test protège le groupe entier, même après une nouvelle liaison.
Les manifestes anciens restent intacts ; une union entre anciennes partitions impose une
nouvelle lignée observationnelle documentée, sans prétendre réparer l'indépendance historique.

Chaque valeur présente exige deux lectures séparées. Les conclusions critiques présentes
(catégorie, date de dépôt, décision, puissance, capacité et poste de raccordement), ainsi que
les désaccords, nécessitent une troisième lecture des sources avant confrontation aux avis.
Les inconnus ne sont pas transformés en absences ou en succès. Le contrôle automatique des
citations et des empreintes vérifie la structure ; il ne certifie ni la lecture effective ni
la justification sémantique d'une valeur.

Le champ `mw` désigne une puissance du projet explicitement exprimée en MW. Le lecteur déclare
l'actif, son état, la base de mesure et le périmètre. Une puissance installée éolienne ne prouve
pas une puissance de raccordement AC ; cette dernière reste une observation distincte inconnue
dans ce protocole. Les MWc/DC ne sont pas convertis implicitement. Les MWh de stockage restent
distincts d'une production annuelle. Un poste existant cité dans le texte n'est pas un poste
de raccordement établi. Une date isolée ou de signature n'est pas une date de dépôt complet.
Un SIRET mal formé n'est pas corrigé par devinette ; plusieurs communes ne sont pas réduites
arbitrairement à une seule. Le masque ou l'ambiguïté d'identité entraîne une abstention.

Les formulaires humains restent vierges et séparés des avis IA. Les deux outils privés de
cette phase doivent être testés sur des cas synthétiques et relus chacun par un agent distinct
avant utilisation. Les sorties brutes, versions des paquets et éventuels avis invalides sont
conservés ; aucune réécriture silencieuse ne remplace une lecture antérieure.

## Correctif de confidentialité et rejeu séparé

Un cas synthétique reproduit la réexemption des noms d'un porteur personne physique comme
mots de société, avant le masque tardif de validation. Le nom pouvait atteindre un classificateur
et rester dans les preuves, le vocabulaire ou des candidats. Deux revues du code ont reproduit
indépendamment le défaut et recherché des contre-exemples ; une troisième a examiné le correctif.

Le correctif limite les exemptions aux occurrences des noms complets de sociétés, masque les
personnes connues avant classification et les candidats perdants, puis refait le contrôle après
l'extraction de secours. Il couvre aussi les alertes, les contrôles et les propositions tardives
du pré-réviseur. L'audit examine désormais les candidats, les valeurs et la classification
machine conservées, les contrôles et les flags. Les noms composés, variations de casse connues,
homonymies et raisons sociales distinctes ont des tests discriminants. La version d'extraction
passe à `2026.10.31a`.

Cette correction ne constitue pas un détecteur général de noms libres. Le cas sans nom reconnu
avant l'appel reste une limite des entrées modèle ; aucun modèle applicatif n'est activé pendant
ce pilote. Les budgets IA applicatifs restent nuls et `RADAR_OFFLINE=1` reste le défaut.

Le rejeu utilise une copie privée distincte, sous `/workspace/watt-pilot-private/review-20261031`.
Les sorties du pilote initial, caches de première lecture, partitions et formulaires ne sont
pas remplacés. La comparaison note explicitement la politique de lecture : première campagne
principalement à 30 secondes avec une reprise ciblée à 60 secondes ; variante à 60 secondes.
Les CPU, RAM et autres plafonds restent ceux du Compose du pilote.

## Dimensionner l'extension

Le ratio actuel est 206 / 84 = **2,45 fichiers par groupe**. À composition identique, 200 et
300 groupes représenteraient environ 490 et 736 fichiers. C'est une projection conditionnelle,
sans garantie : les HTML de dossier comptent dans les fichiers et la richesse des annexes varie.
1 000 fichiers n'est utile que si des dossiers naturellement plus riches ou des lacunes de
couverture le justifient. Aucun groupe ne sera tronqué pour atteindre un compteur.

La taille statistique est celle des groupes indépendants **effectivement évaluables pour la
mesure**, dans la partition concernée. Elle dépend des positifs, négatifs et champs présents,
pas seulement de la taille du corpus. À titre d'ordre de grandeur, un intervalle de Wilson
à 95 % autour d'une proportion de 50 % a une demi-largeur de 6,86 points avec 200 groupes et
5,62 avec 300, sous hypothèses d'échantillonnage indépendant. Ces effectifs doivent être ceux
du dénominateur évalué, sans compter le développement. Même 30 succès sur 30 ne donnent qu'une
borne basse binomiale exacte bilatérale à 95 % de 88,43 %.

Préparer deux volets distincts avant l'extension :

1. Un échantillon de couverture avec cadre de sélection, critères et probabilités documentés,
   incluant des rejets aléatoires. Il servira à une future mesure avec références indépendantes.
2. Un lot diagnostique enrichi en candidats BESS, centres de données, cas ambigus, contradictions,
   tableaux, scans et annexes difficiles. Ses proportions ne sont pas extrapolées à la population.

Les étiquettes IA de ces volets restent des propositions. Les vrais positifs, vrais négatifs,
faux regroupements et pièces attendues ne sont pas déclarés vérifiés en l'absence de référence
appropriée. Définir les dénominateurs par classe, champ et modalité avant d'annoncer une précision.

La prochaine collecte reprend par défaut DREAL HDF 2025 au curseur réellement observé 144.
Une lacune nécessitant d'autres pièces peut donner priorité aux avis MRAe déjà observés, puis à
la qualification d'une période MRAe ou d'une préfecture depuis des liens effectivement lus.
Aucune URL annuelle ne sera fabriquée. Les neuf hôtes autorisés restent la frontière réseau ;
aucun contournement du proxy n'est permis.

Les titres explicitement liés aux batteries déjà observés correspondent à trois groupes
réservés. Ils ne sont pas réutilisés pour régler le code, ni recollectés afin de les déplacer
vers le développement. Les deux avis initiaux sur le développement donnent 34 catégories
`other` et deux inconnues ; ce constat ne décrit ni la réserve ni la prévalence dans les sources.

Chaque nouvelle branche commence par 1 à 3 dossiers. Les lots suivants visent au plus 20 à
30 groupes complets et 100 fichiers, avec plafonds explicites de tentatives, octets et durée.
Une limite atteinte reste un dossier incomplet signalé et conservé, sans succès de complétude
inventé. `PoliteHttp`, cadence de cinq secondes, admission persistante et arrêt immédiat de la
source sur blocage restent obligatoires. La couverture et les lacunes sont recalculées entre
lots ; l'obtention de 500 fichiers ne déclenche pas une promotion automatique.

## Conservation des preuves

Les originaux, bases, journaux, paquets, avis et scripts opérationnels restent hors dépôt public.
Le snapshot et sa restauration ont été comparés ; ils restent sur le même système de fichiers.
Cela vérifie une reprise locale, sans prouver une sauvegarde hors site ou une persistance dans
une autre instance cloud. Git conserve le code et les rapports agrégés, pas le corpus privé.
Une copie privée durable externe reste à provisionner avant de compter sur une prochaine session.

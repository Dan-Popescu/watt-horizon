# Corpus étendu : bilan arrêté et reprise du 31 octobre 2026

Le corpus atteint **553 fichiers uniques et 275 groupes candidats sélectionnés**.
L'objectif de volume est atteint ; l'objectif de 200 à 300 groupes indépendants annotés
**n'est pas atteint**. L'indépendance des projets n'est pas établie et les lectures IA croisées
restent partielles. Ce document arrête les observations avant la perte de l'environnement
d'exécution ; il ne constitue ni une promotion du jeu ni une mesure de précision métier.

Les revues disponibles sont uniquement des avis IA, conformément à la décision utilisateur.
Aucune annotation humaine, référence métier ou réussite de détection n'est inventée.

## État de livraison et interruption

Base publique vérifiée à distance le 31 octobre : `03dd7ede7ba55210a7d192c630d932c31ee02b12`.
Les correctifs déjà intégrés incluent le masquage `2026.10.31a` et les régions MRAe seules.
Le présent bilan ajoute uniquement de la documentation. Le candidat de correction
`2026.10.31b` reste privé, non intégré et sans validation finale.

L'exécuteur a cessé de répondre : les lecteurs ont rencontré une déconnexion du transport,
puis `No usable execution environment is available`. Les lectures et écritures privées sont
suspendues. Au contrôle de configuration de 11 h UTC, le service de gestion annonçait pourtant
`running`, `connected` et `observations_current: true`. Ce signal ne prouve donc pas que
l'exécuteur ou ses fichiers soient accessibles. La liste des diagnostics conservés est vide ;
aucune cause de panne n'est établie. Aucun contournement du proxy n'a été tenté.

Les chiffres privés ci-dessous proviennent des contrôles effectués avant cette interruption.
Ils ne constituent pas une nouvelle inspection du disque après la panne.

## Configuration et protections réellement observées

- Configuration : `c82359b2-0c44-4c2c-a914-379fa68295fd~cecfg_6ae4d8fc6c4481a0816c9fc036e5efeb`.
- Version : `c82359b2-0c44-4c2c-a914-379fa68295fd~cecfgver_6ae51397501c81a08e405cde4c29c5bc`.
- Révisions demandée et observée : **2 / 2**, observations courantes.
- Politique : réseau restreint, état appliqué `enforced`. Les neuf hôtes demandés sont présents :
  `www.hauts-de-france.developpement-durable.gouv.fr`,
  `www.mrae.developpement-durable.gouv.fr`, `www.nord.gouv.fr`,
  `www.pas-de-calais.gouv.fr`, `www.somme.gouv.fr`, `www.oise.gouv.fr`,
  `www.aisne.gouv.fr`, `geo.api.gouv.fr`, `recherche-entreprises.api.gouv.fr`.
  L'autorisation de ces neuf hôtes n'est pas un test HTTP réussi sur chacun.
- Les accès DREAL/MRAe ont effectivement fonctionné via `PoliteHttp`, avec cadence de cinq
  secondes et admissions persistantes. Aucun 403/429 ou blocage de source n'est enregistré
  dans l'extension arrêtée.
- Profil hors ligne par défaut ; réseau uniquement dans le profil explicite de collecte.
  Aucun modèle applicatif configuré, budgets applicatifs IA nuls, aucun appel payant applicatif.
  Les sous-agents de revue sont distincts des appels de modèle de l'application.

Les jobs utilisent deux CPU, 2 Gio de mémoire sans swap et 128 PID ; système racine en lecture
seule, UID 1000, capacités supprimées, `no-new-privileges`, tmpfs de 512 Mio. Ils n'embarquent
ni dépôt Git, ni socket Docker, ni jeton de revue. Image de lecture utilisée :
`sha256:d3b1769c23bb7165e52d437652babf81a7a0cdaff3e515aa263616505875ac64`,
avec Python 3.12.16, PyMuPDF 1.28.2 et Tesseract 5.5.0, langue française.

Le transport borne chaque réponse HTTP à 32 Mio. `--max-bytes` ne fournit pas de compteur
cumulatif durable des octets réseau ; les octets des blobs ne mesurent pas le trafic.
L'ingestion garde ses limites de 500 pages / 30 secondes ; le lecteur du rejeu utilise
300 pages / 60 secondes. OCR : 50 pages, 20 mégapixels, 60 secondes ; tableaux : plafond de 20 pages
par document. La surveillance du disque n'est pas un quota matériel.

## Inventaire et regroupement

| Unité | Corpus conservé | Sélection observationnelle |
| --- | ---: | ---: |
| Fichiers uniques | 553 | 547 |
| PDF / HTML | 333 / 220 | 332 / 215 |
| Attachements de pièces | 554 | 548 |
| Dossiers administratifs | 285 | 284 |
| Groupes candidats | 276 | 275 |

La sélection retire entier le groupe initial contaminé par des liens de navigation, soit
un dossier et six fichiers. Les HTML de cadre de source sont archivés séparément :
153 fichiers uniques et 164 reçus, hors compteur du corpus documentaire.

| Usage protégé | Groupes candidats | Dossiers | Fichiers uniques | Attachements |
| --- | ---: | ---: | ---: | ---: |
| Développement | 144 | 145 | 282 | 283 |
| Réserve protégée | 131 | 139 | 265 | 265 |
| Total | 275 | 284 | 547 | 548 |

La réserve conserve toute appartenance historique à la calibration ou au test. Les anciennes
partitions et formulaires ne sont pas réécrits. La fermeture par anciennes appartenances,
empreinte, URL exacte et identifiant administratif régional a été vérifiée par un outil
indépendant : 43 cas synthétiques et huit contre-épreuves complémentaires ; contrôle réel
final sans échec. Un premier contrôle en échec reste conservé, avec le diagnostic et la
correction de son contrat : promotions historiques conservatrices et alias d'URL de première
observation ne sont pas assimilés à une corruption. Ce n'est pas une preuve d'indépendance
des projets : sociétés, communes, modèles de documents et projets liés peuvent rester corrélés.

Ce corpus est un échantillon diagnostique enrichi par le filtrage configuré, sans plan
probabiliste établi. Ses proportions ne sont pas extrapolées aux sources. La réserve ne
contient que 131 groupes techniques ; les groupes de développement ne gonflent pas un
éventuel dénominateur de test. Aucun intervalle de précision sur 275 projets indépendants
n'est calculé à partir de ces avis IA.

Empreinte du manifeste sélectionné :
`ad8046a51aab69f6428cd6ead22960bba518661a5479caf06c0ac670ddab2b35`.
Rapport indépendant final :
`d089943229468b11ffccd604e876d7d398b7f108895d2f20546bf0563ff6c0fd`.
Empreinte de corpus du traitement :
`555331c403c9cbb318c24aab50bafc020f304240fd8c4ac10ab9f85630e1e79f`.

## Sources observées et complétude

La collecte ajoute 341 fichiers uniques et 194 dossiers aux 212 fichiers / 91 dossiers
initiaux. Les pièces DREAL représentent 214 PDF et 220 HTML, sur 166 identités de dossier.
Les pièces MRAe représentent 120 attachements PDF, 119 PDF uniques et 119 identités de dossier.

Les trois pages annuelles suivantes ont été qualifiées depuis des liens ou un formulaire
de recherche effectivement observés, sans construction d'URL annuelle :

- [Hauts-de-France 2025](https://www.mrae.developpement-durable.gouv.fr/avis-rendus-sur-projets-de-la-mrae-hauts-de-france-a1442.html) :
  135 liens reconnus, 69 sélectionnés et acquis.
- [Normandie 2025](https://www.mrae.developpement-durable.gouv.fr/avis-rendus-sur-projets-de-la-mrae-normandie-en-a1459.html) :
  58 liens reconnus, 23 sélectionnés et acquis.
- [Île-de-France 2025](https://www.mrae.developpement-durable.gouv.fr/avis-rendus-sur-projets-de-la-mrae-ile-de-france-a1446.html) :
  117 liens reconnus, 15 pièces sélectionnées et acquises, correspondant à 14 dossiers.
  Huit titres du cadre de source contiennent une mention explicite de centre de données :
  ce sont des candidats de sélection, pas huit positifs métier vérifiés.

Ces acquisitions complètent la sélection configurée de ces pages, sans établir le rappel
du filtrage ni la complétude administrative des dossiers. DREAL HDF 2025 reste partiel
(`item_budget`, curseur réellement observé 800) ; MRAe HDF 2026 reste partiel.
Aucune couverture des cinq préfectures ou des deux API ne découle de leur autorisation réseau.

Les 120 racines PDF MRAe ont seulement leur propre arête dans le graphe documentaire.
Acquérir l'avis ne signifie pas avoir acquis l'étude d'impact et ses annexes. Les liens
présents dans les PDF ne sont pas suivis par le collecteur actuel ; les fichiers Office
et ZIP sont des pièces terminales. Aucun Office/ZIP n'est rencontré dans ce corpus.

Un audit hors réseau des 333 PDF a parcouru 3 773 pages : 94 URL uniques ressemblent à des
documents, dont 90 ne correspondent pas aux URL stockées. Ce sont des pistes à qualifier,
**pas 90 pièces manquantes prouvées**. Quarante-trois pistes appartiennent aux hôtes autorisés ;
aucune de ces pistes, autorisée ou non, n'est téléchargée par cet audit. Un PDF contient un
fichier embarqué, conservé dans le
brut mais dont le contenu n'est pas extrait. Son rôle d'annexe pertinente reste inconnu.
Les arêtes historiques du graphe étant cumulatives, elles ne sont pas toutes des incidents
de la dernière collecte.

L'inventaire administratif externe n'est pas validé. Pour cette campagne, chaque avis de
lecteur conserve `administrative_document_inventory_unverified` ; `absent` n'est pas utilisé.
Une pièce ou un fait non établi reste inconnu, avec sa raison.

## Lecture et exécution : résultats V1

| Étape | Résultat observé |
| --- | --- |
| Première lecture des 553 fichiers | 292 PDF complets ; 41 PDF et 220 HTML incomplets ; zéro échec de lecture |
| Worker natif de lecture | 292 terminés, 261 bloqués |
| Worker natif d'extraction | 95 terminés, 190 bloqués |
| Rapprochement natif | Une tâche bloquée par ses dépendances d'extraction |
| Traitement observationnel des 285 dossiers | 95 complets, 190 en attente, zéro échec ; 285 sorties machine brutes conservées |

La file native atteint un état sans tâche courante éligible ; cela ne signifie pas que toutes
les tâches sont terminées. Le traitement observationnel conserve les lectures partielles
sans contourner le verrou de publication natif ni réattribuer les partitions historiques.
Aucun nouveau projet n'est publié.
La commande native `radar eval` n'est pas relancée : elle réattribuerait les partitions et
rencontrerait les unions historiques protégées. Le rapport diagnostic utilise une API en
lecture seule, sans promouvoir cette campagne observationnelle en évaluation native.

Sur les fichiers physiques : 3 773 pages PDF, 3 201 pages avec texte natif, 572 signalées
comme scannées, 553 pages OCR non vides et 19 pages non lues dans 12 documents.
Un texte OCR non vide ne mesure pas son exactitude. L'extraction de tableaux produit
1 716 tableaux et 9 063 lignes ; 237 pages de tableaux sont en erreur, dans 31 PDF.

Les 220 HTML ont des erreurs de format dans ce lecteur. Des contre-épreuves synthétiques
confirment qu'une image HTML déclenche un blocage conservateur ; cela ne prouve pas que les
images réelles soient inutiles. Les raisons d'incomplétude sont non exclusives.
Les sommes pondérées par dossier diffèrent des compteurs physiques lorsqu'une pièce est
partagée : par exemple 3 788 pages dans les fiches, contre 3 773 pages PDF uniques.

## Extraction, preuves et abstentions : diagnostic sans référence métier

Les 285 sorties utilisent la méthode de règles. Les catégories conservées sont :
163 non classés, 121 `other`, une `needs_review`. Il n'existe pas de classificateur indépendant
configuré. La détection complète, sa précision et son rappel ne sont donc pas évalués.

Les champs machine sont renseignés pour 46 dossiers en puissance MW et 11 en énergie MWh.
Ce sont des présences de valeurs, pas des valeurs validées. Les candidats multiples signalent
des contradictions dans cinq dossiers pour le porteur, quatre pour la date, un pour la
puissance et un pour la commune ; ces nombres par champ ne sont pas un nombre de fautes
démontrées ni un total de dossiers distincts.

Aucune preuve ne référence une pièce extérieure à l'appartenance du dossier dans le contrôle
structurel. Les preuves utilisent surtout les champs et segments extraits ; la présence d'un
repère n'établit pas sa justification sémantique. Cent cinquante-quatre valeurs de département
n'ont pas de preuve directe dans le décompte : elles ne sont pas déclarées fausses pour autant.
Trois flags persistés `quote_not_found` demeurent ; ce bilan n'a pas réalisé une nouvelle
validation exhaustive des citations.

Les erreurs concrètes de périmètre déjà examinées en développement dans la première phase
restent pertinentes : MW installés éoliens versus raccordement AC, MWc photovoltaïques versus
MW AC, date de signature versus dépôt complet, SIRET invalide et commune multiple.
Les détails de ces erreurs et leurs arbitrages figurent dans le
[bilan de la première phase](revue-ia-pilote-20261031.md). Le contrat distingue également
production annuelle et capacité MWh, sans présenter cette précaution comme une erreur constatée.
Ces exemples ne sont pas présentés comme
des erreurs vérifiées dans l'ensemble des 275 nouveaux groupes.

## Revues IA : progression, confidentialité et limites

Les lecteurs séparés utilisent le même modèle que le coordinateur avec raisonnement
extra-élevé, des sources masquées et aucune prédiction machine ni opinion d'un autre lecteur.
Le groupe est l'unité d'échantillonnage ; chaque dossier exige une fiche par lecteur et des
preuves issues de ses propres pièces. Le contrôle d'une citation exacte ne remplace pas
la relecture de son sens. Le coordinateur du développement ne reçoit pas les valeurs,
citations ou erreurs individuelles de la réserve.

Points de reprise déclarés persistés avant l'interruption, sans validation finale gelée :

| Lecteur V4 | Groupes avec fiches | Fiches de dossier |
| --- | ---: | ---: |
| Développement A | 24 / 144 | 24 / 145 |
| Développement B | 24 / 144 | 24 / 145 |
| Développement C | Pas commencé en V4 | 0 / 145 |
| Réserve A | 60 / 131 | 68 / 139 |
| Réserve B | 41 / 131 | 47 / 139 |
| Réserve C | 19 / 131 | 21 / 139 |

Les lectures supplémentaires non persistées ne sont pas comptées. Ces lignes ne s'additionnent
pas en groupes distincts ni en groupes ayant deux ou trois revues valides. Les sorties V3
restent séparées. Aucun résultat final d'accord ou d'exactitude n'est produit pour cette phase.

L'export réservé V3 a échoué fermé sur le deuxième contrôle de confidentialité. La V4 garde
trois documents entièrement occultés pour cette raison, avec appartenance et limites explicites ;
aucun contenu partiellement accepté de ces documents n'est publié dans les paquets.
Malgré ces contrôles, le lecteur réservé A a signalé deux paquets supplémentaires contenant
des résidus possibles de signature personnelle. Ils restent suspendus ; leurs seuls repères
opaques sont privés. Les lecteurs sont arrêtés et ne reprendront pas automatiquement.
Ces suspicions nécessitent un traitement de confidentialité avant toute nouvelle lecture.
Zéro alerte automatique ne garantit pas une anonymisation exhaustive.

L'audit des sorties machine V1 trouve encore quatre occurrences détectables, dans un seul
dossier de développement : trois preuves et un candidat. Le correctif isolé `2026.10.31b`
a des tests ciblés verts, mais sa dernière version n'a pas achevé la revue indépendante,
la suite complète, la construction d'image ni le rejeu réel. Il n'est pas intégré.
La vérification devra aussi conserver les déclencheurs de confidentialité du V1 :
un audit qui reconstruit ses noms seulement depuis les candidats corrigés pourrait annoncer
un faux zéro alors que des preuves résiduelles subsistent. Le contrôle figé a un contre-exemple
synthétique ; son utilisation sur les deux bases réelles reste à faire.

Tous ces avis restent `source_only: true`, `human_verified: false`. Trois lecteurs du même
modèle et du même OCR ont des erreurs corrélées ; leur accord ne devient ni vérité humaine,
ni précision métier, ni classificateur indépendant.

## Temps, mémoire, disque et caches

Mesures arrêtées, issues des pilotes de commandes ; elles ne représentent pas un benchmark
de débit de production :

| Opération | Durée pilote | Pic mémoire observé |
| --- | ---: | ---: |
| Douze lots d'extension | 2 632,997 s | 247 578 624 octets |
| Première lecture des 553 fichiers | 1 011,09 s | 529 702 912 octets |
| Quatre lots de traitement observationnel | 108,118 s | 442 007 552 octets |
| Audit des URI PDF hors réseau | 77,431 s | Non échantillonné par le moniteur de jobs |
| Diagnostic final en lecture seule | 11,626 s | 90 116 096 octets |

Les échantillons mémoire peuvent manquer un pic bref. Aucune somme de ces durées ne constitue
le temps de bout en bout, qui inclut attentes réseau, préparation, vérifications et revues IA.
Aucun OOM n'a été observé dans les échantillons de lecture.
Le compteur de requêtes physiques passe de 274 à 789 : +515, dont 510 pour les douze lots
et cinq pour les petits contrôles d'accès. Les phases hors ligne n'ajoutent pas de requête.
Le trafic cumulé en octets n'est pas mesuré.

Au point de contrôle avant diagnostic, le cache conserve 764 entrées de lecture PDF et
104 entrées OCR, avec intégrité SQLite vérifiée. Le dernier suivi disque observe environ
2,79 Go sous la racine active et 17,46 Go disponibles sur son système de fichiers.
Ces observations antérieures à la panne ne prouvent pas leur persistance après celle-ci.

## Données privées, formulaires et sauvegardes

Racine initiale : `/workspace/watt-pilot-private/pilot-20261030`, conservée.
Racine du V1 étendu : `/workspace/watt-pilot-private/review-20261031`.
Originaux, bases, caches, journaux, secrets, exports de lecteurs et scripts opérationnels
restent hors dépôt public.

Les anciens formulaires humains A/B conservent chacun leurs 90 fiches vierges et leur
empreinte d'origine. Deux nouvelles feuilles préparatoires ont chacune 3 672 lignes
vierges, sans lecteur attribué, valeur ni vérification. Elles ne sont ni une campagne native
validée ni des annotations humaines importables en l'état. Aucune correction humaine
ou référence gold n'est créée.

La sauvegarde locale finale V1 :
`watt-snapshot-20261031T104254125714Z-7e830bbef698`.
Une restauration séparée a comparé les comptes de 32 tables et vérifié les empreintes des
553 blobs. Empreinte identique de la base du snapshot et de la restauration :
`892676335900a04b7443e0b08c46d9ba3c3e75927e8532bdb06568de23278c18`.
Les points de reprise et sauvegardes antérieurs sont conservés.

Cette sauvegarde native ne comprend pas le cache ni les avis situés hors du répertoire
de données. Le cache dispose de copies locales séparées ; les avis V4 et scripts de
`/workspace/work` n'ont pas de copie externe garantie. Les snapshots et restaurations sont
sur la même instance. **Aucune sauvegarde privée hors site ni persistance inter-instance
n'est établie.** Le dépôt public sauvegarde le code et ce bilan agrégé, pas le corpus privé.

## Reprise nécessaire

1. Retrouver un exécuteur utilisable ; relire l'état courant de sa propre instance, la politique
   et les fichiers réellement présents. Ne pas réutiliser un ancien identifiant d'instance,
   réinitialiser les données ou relancer une collecte aveugle.
2. Inspecter Git sans écrasement : main comporte localement une petite précision documentaire
   non commitée sur l'inventaire administratif ; le correctif de confidentialité est dans
   le worktree séparé `/workspace/watt-horizon-privacy-20261031b`.
   Ne pas reconstruire ou intégrer ce correctif à partir du seul présent bilan. Les commits
   documentaires créés via GitHub pendant la panne ne mettent pas le checkout local à jour :
   comparer et préserver son diff avant toute récupération non destructive du main distant.
3. Vérifier les blobs, bases, caches, manifestes et checkpoints réels, puis établir une copie
   privée durable externe couvrant aussi les avis et scripts. Une sauvegarde localement
   restaurée ne remplace pas cette copie.
4. Traiter les deux suspicions de confidentialité par leurs seuls repères opaques, hors
   exposition métier à l'équipe de développement. Conserver les paquets et avis précédents.
   Toute modification du contenu d'un paquet masqué exige une vraie relecture liée à sa
   nouvelle empreinte, même si le PDF brut est inchangé. Aucun avis n'est simplement rattaché
   au nouveau hash ; les avis sur des paquets inchangés gardent leurs empreintes d'origine,
   après validation des fichiers persistés.
5. Terminer la revue indépendante du patch candidat
   `9759227b644f2f0ce96c3ebdca69460da01eea339cf1caa432d42ae8c01eb348`,
   ainsi que la revue indépendante du helper de contrôle à déclencheurs figés, puis les
   vérifications du dépôt. Faire le rejeu hors ligne dans une nouvelle racine,
   en gardant le V1 intact ; contrôler les sorties avec l'audit natif et les déclencheurs
   privés figés avant correction. Ne publier aucun contenu nominatif.
6. Reprendre les lecteurs sur leurs sorties réellement persistées, sans importer leurs avis
   dans gold, sans leur montrer les autres avis et sans compter les paquets suspendus comme
   validés. Achever A/B et la troisième lecture des conclusions critiques et désaccords.
7. Qualifier la complétude et les dépendances entre projets, puis documenter les dénominateurs
   réellement évaluables par classe et champ. Étendre la collecte seulement pour combler
   des lacunes identifiées, en gardant les groupes entiers et les frontières réseau.
8. Distinguer le bilan d'exécution, la complétude documentaire et la qualité métier.
   Cette dernière reste non mesurée pour l'extension ; la détection complète requiert
   un classificateur et une référence indépendants appropriés.

Les derniers contrôles complets du code public intégré ont donné 685 tests réussis,
un ignoré, Ruff/format et mypy verts. Ils ne valident pas le candidat `2026.10.31b`,
les annotations encore partielles, ni la qualité du corpus. Aucun test d'exécution n'est
annoncé comme réalisé après la perte de l'exécuteur.

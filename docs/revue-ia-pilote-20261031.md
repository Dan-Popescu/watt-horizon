# Exploitation du corpus initial par revues IA

Le corpus initial a été audité avant son extension. **36 groupes de développement, 84 documents
et 357 pages disponibles** ont fait l'objet de trois lectures IA séparées, sans prédictions.
Les 48 groupes de calibration/test sont restés réservés. Un quatrième contrôle ciblé a examiné
le sens des preuves des champs divergents. **Aucune annotation humaine, correction ou référence
vérifiée n'a été produite.** Le [protocole](protocole-revue-ia-20261031.md) décrit les garanties
et les limites ; les [mesures agrégées](measurements/revue-ia-pilote-20261031.json) permettent
de vérifier les effectifs. Ce rapport porte sur le corpus initial, pas sur le nombre de fichiers
atteint ensuite pendant l'extension.

## Comptages vérifiés

Deux audits indépendants confirment 84 groupes techniques sélectionnés, 90 dossiers et
206 fichiers uniques : 109 PDF et 97 HTML. Les 206 empreintes originales ont été recomputées.
Le corpus privé initial entier conserve 212 fichiers et 91 dossiers ; le groupe initial affecté
par les liens de navigation reste exclu entier de la campagne. Les groupes techniques ne sont
pas déclarés indépendants au sens métier.

Les groupes sont répartis en 36 développement, 22 calibration et 26 test ; les dossiers en
36, 23 et 31. Les deux formulaires humains restent vierges, sans lecteur attribué, et l'effort
humain reste non mesuré. Les sorties machine initiales, partitions et formulaires sont conservés.
Les différences d'URL entre une ancienne ligne physique et son attachement documentaire ont
été vérifiées : aucun désaccord entre le manifeste et l'attachement contextuel n'a été trouvé.

## Résultats des lectures séparées

Les lecteurs A, B et C utilisent le même modèle, avec raisonnement extra-élevé. Leurs fichiers
ont passé les contrôles d'empreintes, de document, de repère et de citation exacte. Les trois
lectures donnent 34 catégories `other` et deux inconnues. Cela décrit leurs avis sur le
développement, sans décrire la réserve, la prévalence réelle ou l'exactitude d'un classificateur.

| Comparaison de 360 positions de champ | Nombre |
| --- | ---: |
| Même valeur explicite chez les trois lecteurs | 133 |
| Inconnu chez les trois lecteurs | 208 |
| Avis différents, y compris valeur présente/inconnue | 19 |
| Absence affirmée par les trois lecteurs | 0 |

L'accord ne prouve pas l'exactitude : les lecteurs partagent le même modèle, les mêmes sources
masquées et le même OCR. Les 208 inconnus ne sont ni des réponses correctes ni des absences
démontrées. Le masquage, les pages non lues et l'ambiguïté y contribuent ; ce compteur ne mesure
pas le rappel de l'extracteur.

Le contrôle ciblé porte sur 19 positions dans 15 groupes : 57 avis et 45 citations, toutes
exactement présentes dans les paquets. Un avis source seul a été figé avant la confrontation
aux lecteurs. Selon le contrat de champ explicité, 13 positions sont supportées, cinq partielles
et une non établie. Ce sont des conclusions IA candidates, sans vote transformé en référence.
Les avis originaux restent inchangés.

Exemples qui empêchent d'assimiler citation exacte et valeur métier validée :

- Un avis MRAe décrit **17 MW éoliens installés**. Cette lecture ne prouve pas une puissance AC
  de raccordement. Les deux périmètres restent distincts.
- Neuf positions conservent le champ explicite `Dossiercompletdate`. Un rattachement reste
  incertain : le formulaire du groupe `010309` porte aussi le numéro `010339`. La date lisible
  ne suffit pas à résoudre l'identité du dossier.
- Une suite OCR de 14 chiffres échoue au Luhn. Aucun chiffre n'est corrigé pour fabriquer un
  SIRET valide ; sans référence visuelle indépendante, la cause source/OCR reste à établir.
- Deux positions décrivent plusieurs communes ; une commune unique n'est pas établie.
- Un poste existant faisant l'objet de travaux ne prouve pas le poste de raccordement d'un
  autre actif. Les MWc photovoltaïques, MWh de production, kVA et programmes futurs restent séparés.

Le dictionnaire des prochaines lectures doit distinguer valeur littérale observée, actif,
état existant/projeté et portée du champ. Un accord sur une puissance installée ne doit pas
devenir une preuve de raccordement. Un identifiant douteux ou une pluralité de communes ne
doivent pas être forcés dans un scalaire réputé vérifié.

## Confidentialité et correction contrôlée

Le premier paquet masqué conservait des résidus possibles dans des signatures. Les lectures
ont été suspendues, la V1 archivée et les groupes réellement relus sur la V2. La V2 occulte
51 pages de signature/certificat ; deux détecteurs distincts ont contrôlé 19 450 composants,
sans alerte détectée. Cela ne certifie pas l'anonymisation exhaustive. Les noms libres non
reconnus restent une limite explicite.

Le défaut applicatif a été reproduit avec des PDF synthétiques : un porteur personne physique
pouvait bénéficier d'une exemption de société avant validation, puis rester dans une preuve,
le vocabulaire ou un candidat. Deux analyses indépendantes et une revue du correctif ont
cherché des contre-exemples : casse, noms composés, homonymie avec une société, candidat perdant,
localisation HTML échouée, détails de contrôle et propositions tardives. Les raisons sociales
distinctes sont préservées dans les cas de contrôle. La correction est versionnée
`2026.10.31a` ; elle ne constitue pas un détecteur général de noms libres.

Sur une copie privée des 91 dossiers, l'audit **élargi**, appliqué avant le recalcul, trouve
14 emplacements signalés dans trois dossiers. Après recalcul, il trouve zéro emplacement.
L'ancien audit n'examinait pas toutes ces surfaces : ses deux alertes ne sont donc pas un
dénominateur comparable. Les sources et sorties initiales ne sont pas remplacées.

## Exécution et qualité restent séparées

La suite finale donne **659 tests réussis, un ignoré** ; formatage, Ruff et mypy passent.
Les tests réseau réels restent exclus de cette suite. Les vérifications PDF/OCR du pilote
utilisent l'image Docker avec Tesseract français.

Le recalcul des 91 dossiers termine avec code zéro en 20,2 secondes, avec les caches hérités
déjà présents. Leurs hits/misses n'ont pas été archivés pour cette exécution.
L'évaluation stricte prend 22,2 secondes et sort avec code 2 : le gate reste `failed`.
Les pics cgroup échantillonnés atteignent respectivement 253,5 et 275,4 Mio. L'intervalle final
non échantillonné n'est pas certifié ; ces mesures ne sont pas un débit de production ni une
comparaison de performance avec le premier passage à froid.

Les deux preuves signalées introuvables restent présentes, les tâches de lecture ne sont pas
toutes terminées, et les échantillons indépendants vérifiés manquent. Aucun seuil ni référence
approuvée n'a été modifié. Aucun classificateur indépendant n'est configuré : la détection complète,
la précision, le rappel et la qualité métier restent **non évalués**. Les budgets de modèles du
pipeline restent nuls ; les revues IA de cette session sont distinctes de ses appels applicatifs.

Les originaux, bases, journaux, paquets et avis restent privés. Les copies locales et restaurations
testées ne prouvent pas une sauvegarde hors site ni une persistance dans une autre instance cloud.

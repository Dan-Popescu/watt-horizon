# Campagnes privées d'annotation et comparaison

Les campagnes complètent les références et la revue existantes. Elles permettent de comparer deux variantes sur les mêmes documents, avec des annotations saisies à l'aveugle. Elles n'approuvent pas automatiquement un déploiement et ne modifient ni les corrections ni les fichiers `gold/`.

## Préparer les documents et les fiches

Après la collecte et le traitement des dossiers :

```bash
RADAR_OFFLINE=1 uv run radar campaign prepare --out data/campaigns/pilote-2026 --limit 100 --double-rate 0.1
RADAR_OFFLINE=1 uv run radar campaign snapshot data/campaigns/pilote-2026 --name extracteur-initial --out data/campaigns/initial.json
```

`--limit` compte des groupes indépendants, pas des dossiers : tous les dossiers d'un groupe sont conservés ensemble. Les projets rapprochés et les PDF partagés restent dans la même partition. Les partitions existantes sont conservées ; un conflit entre groupes déjà figés est refusé. La sélection répartit les groupes entre sources, puis favorise la diversité des territoires, années de première observation, modalités de lecture, audits, rôles et gabarits. Le rapport expose les strates non représentées. Cette sélection diversifiée ne constitue pas un échantillon probabiliste de tous les projets français.

Le répertoire contient un manifeste avec les identités complètes, hashes des PDF, URL, rôles, pages et partitions, ainsi que deux fichiers JSONL vierges. `reviewer-a.jsonl` couvre tous les dossiers ; `reviewer-b.jsonl` couvre les groupes à relire indépendamment. Distribuer chaque fiche à son lecteur sans lui montrer la sortie machine ni la fiche de l'autre. Les documents restent dans le stockage privé : aucun PDF brut n'est ajouté au dépôt. Les chemins recommandés sous `data/` sont ignorés par Git.

Les fichiers et répertoires de campagne existants ne sont jamais écrasés par les commandes. Une campagne porte toujours `independence: unestablished` : changer de répertoire ne rend pas indépendant un ancien jeu déjà utilisé pour ajuster le code. Pour une mesure prospective indépendante, constituer un nouveau périmètre temporel et contrôler son absence de contamination avant toute interprétation.

## Saisir et contrôler les observations

Chaque lecteur renseigne son identifiant privé, `verified: true` lorsqu'il a réellement contrôlé les observations, `blind: true` uniquement si la saisie est aveugle, et les IDs de `reviewed_documents`. Chaque champ a un état :

- `present` : valeur explicite et citation vérifiable dans le document indiqué ; puissance en MW, capacité en MWh, dates au format ISO ;
- `absent` : absence constatée après examen explicite de toutes les pièces du dossier ;
- `unknown` : document ambigu, illisible ou information impossible à conclure ;
- `unreviewed` : champ non encore examiné.

Seul `absent` devient une référence nulle. Les états inconnu et non examiné ne sont pas notés comme des absences. Exemple d'observation :

```json
{"status":"present","value":47.0,"evidence":{"document_id":"<sha256>","page":3,"locator":"text","quote":"une puissance de 47 MW"}}
```

Le validateur contrôle le hash réel des octets, la page et la présence de la citation, puis l'appui de la valeur dans cette citation, avec contrôle des unités numériques. Une citation dans un champ AcroForm utilise `locator: "field:Valeurs"` et `page: null`. La lecture locale utilise les transcriptions/OCR configurés ; aucun modèle payant n'est appelé. Ces contrôles ne remplacent pas le jugement humain sur le sens de la citation.

Pour une cellule de tableau, indiquer la page et un repère `table:1:row:2:column:3` (indices à partir de 1). La citation doit être le contenu complet de cette cellule, sans lui ajouter artificiellement une unité. Par exemple, la cellule `500` dans une colonne intitulée `Puissance (kW)` justifie une valeur saisie de `0.5` MW. Le validateur retrouve la cellule et l'unité explicite de sa colonne dans le PDF réel ; il refuse les unités ambiguës, contradictoires ou incompatibles et les mauvais repères. La première ligne est l'en-tête ; les tableaux à en-têtes multiples ne sont pas interprétés implicitement. Les bornes d'une fourchette de puissance se vérifient séparément ; une fourchette de capacité ne devient pas automatiquement une capacité scalaire. L'actif, la phase et le scénario restent un jugement humain explicite.

La lecture de tableaux configurée est réutilisée. Sinon, une demande explicite de citation tabulaire déclenche uniquement l'analyse de sa page dans un processus isolé, avec délai et limites de lignes/caractères ; elle ne force pas le parcours de toutes les pages précédentes. Ce contrôle est limité à 20 pages distinctes citées par document et par validation. Une preuve impossible à lire ou hors budget reste refusée ; aucune citation synthétique n'est créée dans la référence.

La pertinence de chaque document se renseigne séparément dans `document_relevance` : état `present`, valeur `relevant` ou `irrelevant`, citation de ce document. Les annotations non vérifiées restent exclues. Pour les groupes à double lecture, chaque champ attend deux lecteurs distincts et des valeurs concordantes. Un désaccord reste en attente ; une majorité automatique ne fabrique pas une référence.

```bash
RADAR_OFFLINE=1 uv run radar campaign validate data/campaigns/pilote-2026 --annotations data/campaigns/pilote-2026/reviewer-a.jsonl data/campaigns/pilote-2026/reviewer-b.jsonl
```

## Comparer deux extractions

Après une modification de l'extracteur et un nouveau traitement sur les mêmes pièces, exporter la variante :

```bash
RADAR_OFFLINE=1 uv run radar campaign snapshot data/campaigns/pilote-2026 --name extracteur-candidat --out data/campaigns/candidat.json
RADAR_OFFLINE=1 uv run radar campaign compare data/campaigns/pilote-2026 --baseline data/campaigns/initial.json --candidate data/campaigns/candidat.json --annotations data/campaigns/pilote-2026/reviewer-a.jsonl data/campaigns/pilote-2026/reviewer-b.jsonl
```

Les snapshots contiennent uniquement `Filing.machine`, jamais les valeurs corrigées. Les versions de chaque variante sont conservées. Un dossier sans sortie machine brute, un changement de pièces ou de leurs métadonnées et un snapshot tronqué sont refusés. Les méthodes de classification `gold`, humaines ou de référence ne comptent pas comme prédictions indépendantes de catégorie.

Les résultats séparent l'exactitude normalisée des valeurs et la tolérance métier historique. Une différence de 0,05 MW peut donc être signalée dans la première mesure alors que la seconde la tolère. Les effectifs, intervalles de Wilson et améliorations/régressions appariées sont calculés par groupes indépendants. Une erreur dans un membre rend le groupe incorrect pour ce champ. Les erreurs individuelles ne sont exposées que pour la partition de développement ; calibration et test restent agrégés.

Sans référence humaine utilisable, la comparaison reste `unmeasured`. Une comparaison partielle porte seulement sur les champs et groupes effectivement annotés ; elle ne mesure pas les dossiers jamais collectés. `--strict` échoue s'il reste des champs/pièces à examiner, des désaccords, aucune mesure ou une régression exacte par groupe. Une comparaison positive ne certifie pas l'exhaustivité d'Internet et ne remplace pas le contrôle d'approbation des jeux publiés.

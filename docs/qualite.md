# Amélioration continue

Objectif : quand les données arrivent en volume, repérer tout seul les cas mal traités, les faire relire en priorité, et transformer chaque erreur corrigée en amélioration durable, mesurée honnêtement.

## La boucle

```
collecte ─► dossiers + signalements ─► file de relecture ─► corrections (cause, à l'aveugle)
   ▲                                                              │
   │      règles améliorées ◄── erreurs fréquentes + cas de non-régression ◄┘
   └──── radar eval : mesures indépendantes, comparées à la référence approuvée
```

| Étape | Outil | Ce que ça apporte |
| --- | --- | --- |
| 1. Repérer | signalements, calculés à chaque construction de dossier | Une raison de douter, sans référence : citation introuvable, formulaire et décision qui divergent, mise en page inconnue, OCR bruité, valeur mentionnée mais non extraite, contrôle en alerte, catégorie non décidée… |
| 2. Ne rien rater | échantillon d'audit à la collecte | Les dossiers « presque retenus » sont relus sur leur texte complet. 3 % des rejets sont tirés au hasard, pour estimer ce que le tri rate. |
| 3. Prioriser | `radar triage`, onglet Relecture | Les dossiers à relire d'abord, avec la raison. Un dossier relu revient dans la file si la machine change d'avis. |
| 4. Corriger | onglet Relecture | Chaque correction note sa **cause** (OCR, mise en page, règle, classement…). Le mode **à l'aveugle** fait saisir sans voir la machine. |
| 5. Apprendre | `radar errors` | Erreurs regroupées par champ, cause et type de document : la règle à améliorer en premier. Les erreurs qu'**aucun signalement** n'avait vues indiquent le prochain détecteur à écrire. |
| 6. Ne jamais régresser | `radar cases export` puis `check` | Chaque dossier vérifié devient un test rejoué sans PDF. Une erreur connue reste « ouverte » jusqu'à sa correction. Un champ corrigé qui casse de nouveau bloque les tests. |
| 7. Mesurer | `radar eval`, onglet Qualité | Scores par champ et groupe indépendant, par type de document, sur un test gelé. La comparaison utilise une référence approuvée, jamais automatiquement le dernier essai. |

## Ce qui s'améliore tout seul, et ce qui reste humain

| Automatique (à chaque `radar run`) | Humain (rapide) |
| --- | --- |
| Fiabilité de chaque source réapprise à partir des relectures (arbitrage) | Relire le haut de la file, par dossier ou **par champ** au clavier |
| Confiance par champ recalibrée ; acceptation automatique seulement quand c'est prouvé | Appliquer en un clic une correction **propagée** aux dossiers qui ont la même erreur |
| Variantes d'écriture apprises appliquées partout (`radar aliases export`) | Choisir la cause de l'erreur |
| Changements de la machine enregistrés et mis en relecture | Donner un ticket `radar improve` à un agent, puis vérifier `radar shadow --strict` |
| Modèles de documents inconnus et dérives signalés | Accepter un nouveau modèle (`radar templates export`) une fois ses règles vérifiées |
| Règles adoptées sans code écartées si les relectures les prouvent peu fiables | Adopter une règle proposée (`radar rules adopt`) |
| Termes proposés à partir des projets que le tri a ratés, avec leur coût | Adopter un terme (`radar screen-terms adopt`) |
| Second avis d'un classifieur local, actif dès 30 étiquettes fiables et une justesse prouvée | Trancher quand il contredit la catégorie |
| Dossiers inchangés non reconstruits (empreinte des entrées) | — |
| 10 % des dossiers relus remis en file pour une 2e relecture | La faire à l'aveugle, par un autre relecteur |

## Outils de relecture

- **Voir dans le PDF** : chaque valeur s'ouvre sur la page du document où elle a été lue, passage surligné ou champ encadré. Les noms de personnes sont noircis ; l'image n'est jamais enregistrée. Une page scannée s'affiche sans surlignage.
- **Liens entre dossiers** (mode de la page Relecture) : pour chaque paire rapprochée par la machine, dire « même projet », « liés, projets distincts » ou « sans rapport ». La décision s'applique aussitôt aux projets et sert de référence pour mesurer les regroupements. Les fusions faites par la machine passent en premier : une fusion fausse cache un projet.
- **Corpus de PDF réels caviardés** (`tests/corpus/`) : toute la chaîne est rejouée à chaque test. Une mise à jour de PyMuPDF ou de la lecture qui change un résultat fait échouer les tests.

## Règles de méthode

- **Populations gelées et groupées.** Les dossiers partageant un document ou un même projet restent ensemble dans un manifeste versionné développement/calibration/test. Apprendre les règles et l'arbitrage sur le développement, les seuils sur la calibration, et mesurer sur le test. Les détails du test ne deviennent pas des cas de développement. Les données utilisées avant le 2026-10-12 pour régler les règles ne deviennent pas indépendantes par un nouveau nom de campagne.
- **Une part à l'aveugle.** Relire environ 1 dossier sur 5 en mode « à l'aveugle ». Sans cela, le relecteur valide ce que la machine propose, et la justesse paraît meilleure qu'elle n'est.
- **Les audits aléatoires d'abord.** Ils sont en tête de file. Les laisser de côté fausse l'estimation du rappel.
- **Effectifs indépendants.** Trente dossiers d'un même projet ne font qu'un groupe. Les rapports distinguent effectifs bruts et indépendants. Comparer les versions sur les mêmes groupes et garder les régressions individuelles de développement ; le chevauchement des intervalles de deux taux ne constitue pas à lui seul un test de différence. Les seuils de promotion sont des critères sur les observations, pas une garantie de précision en production.
- **La machine est mesurée seule.** Les corrections humaines ne comptent jamais comme des réussites de la machine.
- **Double relecture.** Un échantillon revient à l'aveugle pour un autre relecteur. Les désaccords demandent une consigne plus précise et un arbitrage documenté avant de conclure à une précision élevée. Une correction d'un champ ne valide pas tous les autres.
- **Référence immuable.** Une validation conserve l'instantané effectivement approuvé. Si la machine change ensuite, la référence reste identique et le champ revient en relecture. Un ancien événement sans instantané n'autorise pas à inventer ce qu'un humain aurait validé.
- **Promotion explicite.** `radar eval --approve-baseline` doit satisfaire le profil choisi ; `radar dataset publish` vérifie l'approbation du jeu exact avant activation. Les essais dégradés restent des essais. Voir [exploitation.md](exploitation.md).

## Routine conseillée (hebdomadaire)

1. `radar update` (ou la tâche planifiée), puis l'onglet Relecture : traiter le haut de la file, dont les audits aléatoires.
2. `radar errors` : prendre le groupe d'erreurs le plus gros, corriger la règle et ajouter un test.
3. `radar cases export`, puis `radar cases check`, puis `radar eval --strict` : aucune régression, des cas ouverts qui passent à « corrigés ».
4. `radar screen-terms suggest` et `radar rules list` : adopter ce qui est proposé et sûr, écarter le reste.
5. `radar review export` : les corrections et les décisions sur les liens sont sauvegardées localement dans `gold/`. Conserver ces exports et la base dans une sauvegarde privée ; seuls les exemples caviardés vont dans le dépôt public.

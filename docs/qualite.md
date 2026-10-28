# Amélioration continue

Objectif : quand les données arrivent en volume, repérer tout seul les cas mal traités, les faire relire en priorité, et transformer chaque erreur corrigée en amélioration durable, mesurée honnêtement.

## La boucle

```
collecte ─► dossiers + signalements ─► file de relecture ─► corrections (cause, à l'aveugle)
   ▲                                                              │
   │      règles améliorées ◄── erreurs fréquentes + cas de non-régression ◄┘
   └──── radar eval : mesures avec marge d'erreur, comparées à la fois précédente
```

| Étape | Outil | Ce que ça apporte |
| --- | --- | --- |
| 1. Repérer | signalements, calculés à chaque construction de dossier | Une raison de douter, sans référence : citation introuvable, formulaire et décision qui divergent, mise en page inconnue, OCR bruité, valeur mentionnée mais non extraite, contrôle en alerte, catégorie non décidée… |
| 2. Ne rien rater | échantillon d'audit à la collecte | Les dossiers « presque retenus » sont relus sur leur texte complet. 3 % des rejets sont tirés au hasard, pour estimer ce que le tri rate. |
| 3. Prioriser | `radar triage`, onglet Relecture | Les dossiers à relire d'abord, avec la raison. Un dossier relu revient dans la file si la machine change d'avis. |
| 4. Corriger | onglet Relecture | Chaque correction note sa **cause** (OCR, mise en page, règle, classement…). Le mode **à l'aveugle** fait saisir sans voir la machine. |
| 5. Apprendre | `radar errors` | Erreurs regroupées par champ, cause et type de document : la règle à améliorer en premier. Les erreurs qu'**aucun signalement** n'avait vues indiquent le prochain détecteur à écrire. |
| 6. Ne jamais régresser | `radar cases export` puis `check` | Chaque dossier vérifié devient un test rejoué sans PDF. Une erreur connue reste « ouverte » jusqu'à sa correction. Un champ corrigé qui casse de nouveau bloque les tests. |
| 7. Mesurer | `radar eval`, onglet Qualité | Scores avec marge d'erreur, par type de document, sur un jeu de test gelé. Plus : calibration de la confiance, justesse à l'aveugle, rappel estimé du tri, intégrité des preuves, robustesse, précision des liens et des regroupements, accord entre relecteurs, régressions. |

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

- **Jeu de test gelé.** Environ 30 % des dossiers sont tirés une fois pour toutes (de façon stable, par numéro). On voit leurs scores, jamais le détail de leurs erreurs, et ils ne deviennent pas des cas de test : sinon on règle les règles sur eux et leurs scores mentent. Les dossiers traités avant le 2026-10-12 ont déjà servi au réglage : le jeu de test n'est fiable que pour les dossiers ajoutés depuis.
- **Une part à l'aveugle.** Relire environ 1 dossier sur 5 en mode « à l'aveugle ». Sans cela, le relecteur valide ce que la machine propose, et la justesse paraît meilleure qu'elle n'est.
- **Les audits aléatoires d'abord.** Ils sont en tête de file. Les laisser de côté fausse l'estimation du rappel.
- **Marges d'erreur.** 8 sur 10, c'est « entre 49 et 94 % ». Ne comparer deux versions que si leurs marges ne se chevauchent pas, ou s'appuyer sur les cas de non-régression.
- **La machine est mesurée seule.** Les corrections humaines ne comptent jamais comme des réussites de la machine.
- **Double relecture.** Environ 1 dossier relu sur 10 revient, à l'aveugle, pour un autre relecteur. Si deux relecteurs ne s'accordent qu'à 85 % sur un champ, une justesse mesurée de 95 % sur ce champ ne veut rien dire : il faut d'abord préciser la consigne.

## Routine conseillée (hebdomadaire)

1. `radar update` (ou la tâche planifiée), puis l'onglet Relecture : traiter le haut de la file, dont les audits aléatoires.
2. `radar errors` : prendre le groupe d'erreurs le plus gros, corriger la règle et ajouter un test.
3. `radar cases export`, puis `radar cases check`, puis `radar eval --strict` : aucune régression, des cas ouverts qui passent à « corrigés ».
4. `radar screen-terms suggest` et `radar rules list` : adopter ce qui est proposé et sûr, écarter le reste.
5. `radar review export` : les corrections et les décisions sur les liens sont sauvegardées localement dans `gold/`. Conserver ces exports et la base dans une sauvegarde privée ; seuls les exemples caviardés vont dans le dépôt public.

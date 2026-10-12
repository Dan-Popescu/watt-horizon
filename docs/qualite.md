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
| 7. Mesurer | `radar eval`, onglet Qualité | Scores avec marge d'erreur, par type de document, sur un jeu de test gelé. Plus : calibration de la confiance, justesse à l'aveugle, rappel estimé du tri, intégrité des preuves, régressions. |

## Règles de méthode

- **Jeu de test gelé.** Environ 30 % des dossiers sont tirés une fois pour toutes (de façon stable, par numéro). On voit leurs scores, jamais le détail de leurs erreurs, et ils ne deviennent pas des cas de test : sinon on règle les règles sur eux et leurs scores mentent. Les dossiers traités avant le 2026-10-12 ont déjà servi au réglage : le jeu de test n'est fiable que pour les dossiers ajoutés depuis.
- **Une part à l'aveugle.** Relire environ 1 dossier sur 5 en mode « à l'aveugle ». Sans cela, le relecteur valide ce que la machine propose, et la justesse paraît meilleure qu'elle n'est.
- **Les audits aléatoires d'abord.** Ils sont en tête de file. Les laisser de côté fausse l'estimation du rappel.
- **Marges d'erreur.** 8 sur 10, c'est « entre 49 et 94 % ». Ne comparer deux versions que si leurs marges ne se chevauchent pas, ou s'appuyer sur les cas de non-régression.
- **La machine est mesurée seule.** Les corrections humaines ne comptent jamais comme des réussites de la machine.

## Routine conseillée (hebdomadaire)

1. `radar update` (ou la tâche planifiée), puis l'onglet Relecture : traiter le haut de la file, dont les audits aléatoires.
2. `radar errors` : prendre le groupe d'erreurs le plus gros, corriger la règle et ajouter un test.
3. `radar cases export`, puis `radar cases check`, puis `radar eval --strict` : aucune régression, des cas ouverts qui passent à « corrigés ».
4. `radar review export` : les corrections sont sauvegardées dans `gold/`.

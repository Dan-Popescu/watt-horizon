# Architecture

## Flux

```
sources ──► store (PDF bruts + SQLite) ──► lecture ──► extraction ──► tri ──► contrôles ──► dossiers
                                                                                              │
signaux (sociétés, presse, ICPE) ──────────────────────────────────────────► liens ──► projets
                                                                                              │
                                                                       API lecture ──► CLI / MCP
```

Chaque couche ne dépend que des couches situées en amont. Le domaine (`domain/`) est pur : il ne fait aucune entrée-sortie. Tous les assemblages concrets sont faits dans `wiring.py`.

## Choix et raisons

| Choix | Raison |
| --- | --- |
| Une preuve (document, emplacement, citation) pour chaque valeur | Le client paie pour une information vérifiable. C'est aussi une garde contre les hallucinations du LLM. |
| PDF bruts stockés par empreinte SHA-256 | C'est la source de vérité. Tout le reste se reconstruit (`radar run`) sans nouvelle collecte. |
| SQLite (WAL, migrations versionnées) | Un seul fichier, pas de serveur. Suffisant pour quelques dizaines de milliers de dossiers. Passer à PostgreSQL ne toucherait que `store/`. |
| Tri en deux étages | Les mots-clés gardent tout ce qui pourrait compter (rappel). Le LLM tranche ensuite (précision). Un dossier écarté au premier étage est perdu, d'où les tests sur de vrais titres. |
| Classifieurs en chaîne : LLM puis jeu de vérité | On peut changer de modèle (moins cher, local) sans toucher au reste. `radar eval` mesure l'effet. |
| Champs de formulaire lus avant le texte | Plusieurs formulaires n'indiquent la puissance que dans leurs champs. |
| Transcriptions mot pour mot, puis Tesseract | Les décisions scannées sont rares : une relecture humaine ou par LLM vaut mieux qu'un OCR approximatif. |
| Puissance « estimée » toujours séparée de la « déclarée » | L'estimation (nombre de conteneurs, surface) est calibrée sur trois dossiers réels. Elle est donnée en fourchette, avec sa base de calcul. |
| Masquage des personnes physiques avant stockage | Minimisation RGPD : la base ne contient jamais leurs noms. |
| Client HTTP poli | Une requête par seconde et par site, au plus. Un site qui bloque est abandonné pour toute l'exécution. Les préfectures en ont déjà bloqué une (octobre 2026). |
| MCP en lecture seule, branché sur `api/queries.py` | Une seule logique de requête pour la CLI, MCP et une future API web. |

## État mesuré (2026-10-12, Hauts-de-France, hors ligne)

- 35 documents, 22 dossiers, 9 signaux, 10 projets.
- Rappel du premier tri sur le jeu de vérité : 10/10.
- Champs remplis sur les 10 dossiers cibles :

| Champ | Remplis |
| --- | --- |
| Porteur | 10/10 |
| Commune | 10/10 |
| Date | 10/10 |
| SIRET | 7/10 |
| Puissance | 7/10 |
| GPS exact | 5/10 |
| Poste | 4/10 |

- L'accord du LLM avec le jeu de vérité n'est pas encore mesuré : il faut une clé d'API et `RADAR_LLM_MODEL`.

## Questions ouvertes

- Hébergement et fréquence de mise à jour (tâche quotidienne, base en lecture partagée).
- MCP distant (transport HTTP et authentification) en plus de stdio.
- Jeu de vérité à agrandir (environ 200 dossiers) avant de comparer des modèles moins chers.
- Extension à l'Île-de-France : les data centers y relèvent surtout de la MRAe et des préfectures.

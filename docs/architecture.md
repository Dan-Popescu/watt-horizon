# Architecture

## Flux

```
sources ──► store (PDF bruts + SQLite) ──► lecture + OCR ──► extraction (règles, puis LLM vérifié) ──► tri ──► contrôles
                                                                                                                  │
                                                       corrections humaines ──► dossiers (sortie machine conservée)
                                                                                                                  │
signaux (sociétés, presse, registre national) ──► liens + regroupement d'un même projet ──► projets ──► journal
                                                                                                                  │
                                                                  API lecture ──► CLI / MCP / carte web / alerte e-mail
```

Chaque couche ne dépend que des couches situées en amont. Le domaine (`domain/`) est pur : il ne fait aucune entrée-sortie. Tous les assemblages concrets sont faits dans `wiring.py`.

## Boucle qualité

```
modifier un extracteur, un prompt ou un moteur OCR  ──►  radar run  ──►  radar eval  ──►  régressions ?
        ▲                                                                                   │
        └──────────── corriger le code, ou la référence si c'est elle qui avait tort ◄───────┘
relecture humaine (radar web --review)  ──►  références vérifiées  ──►  mesures fiables
```

- **Ce qui est mesuré** :
  - la justesse de chaque champ par rapport aux références ;
  - la qualité de l'OCR, comparée aux transcriptions ;
  - le rappel du premier tri ;
  - l'accord du LLM avec les étiquettes vérifiées ;
  - l'absence de données personnelles.
- **Comparaison automatique** : chaque mesure est comparée à la précédente, et `--strict` bloque en cas de régression.
- **Ce que mesure l'évaluation** : toujours la **sortie de la machine**, jamais les corrections humaines. Sinon, la mesure serait faussée à 100 %.
- **Traçabilité** : chaque dossier garde les versions de l'extracteur, du modèle et du prompt utilisés, ainsi que le moteur qui a lu chaque document.
- **Caches** : les résultats de l'OCR et du LLM sont mis en cache, avec ces versions dans la clé. La mise à jour quotidienne ne repaie pas les mêmes documents.

## Choix et raisons

| Choix | Raison |
| --- | --- |
| Une preuve (document, emplacement, citation) pour chaque valeur | Le client paie pour une information vérifiable. C'est aussi une garde contre les hallucinations du LLM. |
| LLM seulement en secours, et vérifié | Les règles sont gratuites et testées. Le LLM n'est appelé que pour les champs manquants des dossiers cibles. Sa réponse est rejetée si la citation n'est pas dans le document, ou si la valeur n'est pas dans la citation. |
| PDF bruts stockés par empreinte SHA-256 | C'est la source de vérité. Tout le reste se reconstruit (`radar run`) sans nouvelle collecte. |
| SQLite (WAL, migrations versionnées) | Un seul fichier, pas de serveur. Suffisant pour quelques dizaines de milliers de dossiers. Passer à PostgreSQL ne toucherait que `store/`. |
| Tri en deux étages | Les mots-clés gardent tout ce qui pourrait compter (rappel). Le LLM tranche ensuite (précision). |
| Transcriptions mot pour mot, puis Tesseract | Tesseract fait 4,6 % de mots manquants sur nos scans, surtout dans les titres décoratifs. Une relecture humaine reste la référence. |
| Masquage des personnes physiques partout | Citations, descriptions, titres et texte envoyé au LLM. Un audit automatique le vérifie à chaque évaluation. |
| Corrections humaines en ajout seul | Rien n'est écrasé : chaque correction garde son auteur et sa date, et peut être annulée. La plus récente l'emporte. |
| Regroupement prudent d'un même projet | Même commune ET (même SIRET ou même porteur). Un SIRET seul ne suffit pas : une même société dépose plusieurs projets. |
| « En service » seulement par correspondance stricte avec le registre national | Même commune, mise en service après le dépôt, puissance à ±30 %. Les petites installations anciennes de la même commune ne comptent pas. |
| Client HTTP poli et surveillance des collecteurs | Une requête par seconde et par site, au plus. `radar health` signale une source bloquée, muette depuis plusieurs passages ou arrêtée. |
| MCP, API JSON et carte branchés sur `api/queries.py` | Une seule logique de requête pour tout le monde. Le MCP fonctionne en local (stdio) et à distance (HTTP). |
| Journal des changements | Ce que paie un client, c'est l'alerte : « nouveau projet », « puissance modifiée », « décision rendue ». |
| Références dans `gold/`, versionnées dans git | Le travail de relecture humaine ne doit jamais se perdre avec une base reconstruite. |

## État mesuré (2026-10-12, Hauts-de-France, hors ligne)

- **Volume** : 35 documents, 22 dossiers, 10 projets. 754 signaux, dont 745 installations du registre national.
- **Premier tri** : rappel de 10/10 sur le jeu de vérité.
- **OCR Tesseract** : 4,6 % de mots manquants, mesurés dans l'image Docker sur 4 décisions scannées.
- **Confidentialité** : aucune fuite.
- **Champs remplis** sur les 10 dossiers cibles :

| Champ | Remplis |
| --- | --- |
| Porteur | 10/10 |
| Commune | 10/10 |
| Date | 10/10 |
| SIRET | 7/10 |
| Puissance | 7/10 |
| Poste | 6/10 |
| GPS exact | 5/10 |

Les manques restants ont été vérifiés un par un : l'information est absente des documents.

- **Références** :
  - 21 étiquettes et 21 fiches par champ, toutes **non vérifiées** (brouillons).
  - Les mesures de justesse ne valent donc que comme détection de régression, tant qu'un humain ne les a pas validées dans la page de relecture.
- **Accord du LLM** : pas encore mesuré. Il faut une clé d'API ; la commande `radar eval --classifier` est payante.

## Questions ouvertes

- Hébergement et nom de domaine. Tout est prêt côté code : voir [deploiement.md](deploiement.md).
- Comptes clients : un jeton unique pour l'instant.
- Faire valider les références par un humain, puis les agrandir à environ 200 dossiers avant de comparer des modèles moins chers.
- Extension à l'Île-de-France : les data centers y relèvent surtout de la MRAe et des préfectures.

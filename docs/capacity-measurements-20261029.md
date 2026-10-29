# Mesures de capacité hors ligne — 29 octobre 2026

Le [banc reproductible](../tools/benchmark_capacity.py) complète les essais du
[rapprochement indexé](validation-20261029.md). Il utilise des dossiers et projets
explicitement synthétiques, dans des processus et bases temporaires distincts.
Aucun document réel, donnée de production, accès Internet ou modèle payant n'intervient.

## Reproduire

Depuis la racine du dépôt, avec les dépendances installées :

```bash
UV_CACHE_DIR=/tmp/watt-uv-cache uv run python tools/benchmark_capacity.py \
  --repeats 3 > /tmp/watt-capacity.json
```

Une commande plus courte permet de cibler les scénarios :

```bash
uv run python tools/benchmark_capacity.py sparse:10000 dense:1000 pages:10000 --repeats 3
```

La sortie est un objet JSON unique. Un échec ou délai dépassé reste dans le rapport,
et le processus principal termine avec le code 2. Chaque répétition lance un nouvel
interpréteur avec un délai de 60 secondes, configurable entre 1 et 60 secondes.
Les tailles sont plafonnées : 100 000 dossiers isolés ou projets, 1 500 dossiers denses,
12 scénarios et 5 répétitions au maximum. Ces bornes de travail ne sont pas une
garantie de consommation mémoire ; le cas dense matérialise tous ses liens.

Les [résultats bruts](measurements/capacity-20261029.json) contiennent les 27 répétitions,
la version Python, la plateforme, la révision du code applicatif et le SHA-256 du banc.
Code applicatif : `40f12914311b266ed4cab29cebe72691b3a62bc0`, Python 3.12.14,
Linux x86-64. Aucun chronométrage n'utilise `tracemalloc`.

## Rapprochement et regroupement

Le cas isolé donne à chaque dossier une commune et un nom de société distincts.
Le cas dense leur donne la même commune et le même nom. Le banc vérifie exactement
le nombre de paires, de groupes et de dossiers conservés, puis refait le rapprochement
pour un seul dossier modifié. Les durées sont les médianes de trois répétitions.
Le pic RSS est le maximum du processus entier observé parmi les trois répétitions.

| Scénario | Dossiers | Candidats = liens | Rapprochement | Regroupement | Groupes | Pic RSS |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Isolé | 1 000 | 0 | 0,0104 s | 0,0057 s | 1 000 | 30,32 Mio |
| Isolé | 5 000 | 0 | 0,0583 s | 0,0291 s | 5 000 | 47,35 Mio |
| Isolé | 10 000 | 0 | 0,1457 s | 0,0569 s | 10 000 | 68,86 Mio |
| Dense | 100 | 4 950 | 0,0145 s | 0,0006 s | 1 | 29,94 Mio |
| Dense | 300 | 44 850 | 0,1741 s | 0,0017 s | 1 | 59,83 Mio |
| Dense | 1 000 | 499 500 | 2,3758 s | 0,0053 s | 1 | 395,79 Mio |

L'incrémental sur un seul dossier du cas dense de 1 000 dossiers compare **999 paires**
en **0,0144 seconde** médiane. Il reconstruit encore les métadonnées des dossiers :
ce résultat ne décrit pas un coût constant indépendant du volume.

Le cas dense montre une limite concrète : environ 396 Mio pour 1 000 dossiers laissent
peu de marge à un processus limité à 512 Mio. L'exploitation sépare le service de lecture
et le job de traitement doté de 2 Gio ; il ne faut pas exécuter ce rapprochement dans le
service de lecture. Ce banc n'établit ni le volume maximal du job de 2 Gio, ni son coût
mensuel. Le nombre de liens demandé reste quadratique ; aucun lien n'est tronqué pour
améliorer artificiellement les mesures.

## Pagination SQL et sérialisation

Les projets ont des puissances égales par groupes et une puissance inconnue tous les
13 projets. Chaque appel ouvre une connexion courte en lecture seule, effectue la
requête et les agrégats, valide les modèles, puis encode le JSON. Le banc parcourt
**toutes** les pages et refuse les doublons, les omissions, les totaux incomplets ou
une page dépassant 200 projets. Il ne conserve que les identifiants pour contrôler
la complétude ; les réponses précédentes sont libérées.

| Projets | Pages de 200 | Parcours complet | p50 par page | p95 par page | JSON total | Pic RSS |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 000 | 5 | 0,0259 s | 5,13 ms | 6,26 ms | 376 018 octets | 32,03 Mio |
| 5 000 | 25 | 0,3349 s | 12,44 ms | 18,55 ms | 1 880 438 octets | 46,09 Mio |
| 10 000 | 50 | 1,1754 s | 21,80 ms | 32,48 ms | 3 761 100 octets | 63,16 Mio |

Les p50/p95 du tableau sont les médianes des quantiles calculés séparément pour chaque
répétition. Le p95 utilise le rang supérieur. À 1 000 projets, cinq pages ne suffisent
pas à estimer une latence de queue représentative. Les totaux complets sont recalculés
à chaque page ; le temps du parcours augmente donc plus vite que son seul nombre de
projets. Les fichiers SQLite font respectivement environ 0,96, 3,66 et 7,04 Mio.

## Portée des résultats

Le RSS inclut l'interpréteur, les imports (environ 26 Mio), les données construites,
les structures des algorithmes et les allocations natives. C'est une mesure de pic
du processus, pas une soustraction fiable de mémoire attribuable à une fonction.
Les durées sont locales, séquentielles, sur une machine partagée sans contrôle de charge.
Le stockage et les caches de l'OS ne sont pas remis à froid entre répétitions.

La pagination exclut le transport HTTP, les utilisateurs concurrents, le navigateur
et le rendu de carte. Le rapprochement n'inclut pas les signaux, les décisions humaines
de séparation, la persistance des liens ou toute la pipeline. Le banc n'évalue pas
la collecte, les PDF/OCR, les modèles, la qualité métier ou la facturation cloud.
Une mesure sur des dossiers représentatifs et un test de charge du service restent
nécessaires avant de choisir le dimensionnement de production.

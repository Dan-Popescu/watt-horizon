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

Ce rapport a été produit avec la première version du banc (`69a8105`), avant l'ajout
du scénario `storage:100000`. La commande sans scénarios du banc actuel ajoute ses
trois répétitions ; les neuf scénarios des tableaux restent identiques.

## Stockage de liens reçus en flux

```bash
uv run python tools/benchmark_capacity.py storage:100000 --repeats 3
```

Ce scénario génère les objets `Link` un par un, sans liste préalable. Il mesure le
premier stockage puis un remplacement identique, et vérifie le nombre exact de liens,
le SHA-256 des JSON ordonnés par leurs clés et la conservation de la révision lors du
remplacement sans changement. Le contrôle des données utilise un curseur SQL en flux,
sans appeler `db.links()`, qui matérialiserait une seconde liste complète. La raison
synthétique de chaque lien est la chaîne `Synthetic storage measurement ` répétée
20 fois ; ses caractéristiques ne décrivent pas des liens réels.

`initial_storage_peak_process_rss_mib` est pris juste après le premier stockage ;
le pic global inclut aussi les contrôles et le second passage. Ce scénario isole la
persistance d'une entrée déjà générable en flux. Il ne réduit pas les allocations de
`propose_links`, dont l'interface retourne encore une liste complète.

Après le correctif `fe86819`, les [trois mesures du banc](measurements/link-storage-20261029.json)
stockent 100 000 liens en **0,612 seconde médiane**, avec un pic initial maximal de
**31,46 Mio**. Le second stockage identique prend 0,581 seconde médiane. Les trois
contrôles de contenu donnent le même SHA-256 et conservent la révision du jeu.

Un essai minimal, limité aux imports `Link`/`Database` et au premier stockage, avait
montré le coût des listes JSON avant le correctif. Le même programme a été repris
après le correctif, séparément du banc enrichi :

| Code du stockage | Répétitions | Durée | Pic RSS |
| --- | ---: | ---: | ---: |
| Avant (`40f1291`) | 1 | 1,314 s | 117,945 Mio |
| Après (`fe86819`) | 3 | 0,661 s médiane | 29,711 Mio maximum |

Les durées après sont 0,660, 0,742 et 0,661 seconde ; leurs pics sont 29,539,
29,480 et 29,711 Mio. La référence avant ne comporte qu'une répétition : elle ne
caractérise pas sa variance. La baisse observée concerne la sérialisation et
l'écriture de liens générés en flux. Elle ne prouve pas une baisse similaire de
toute la pipeline, et ne supprime pas le cas dense de rapprochement à environ 396 Mio.

Le programme minimal se reproduit ainsi sur la révision à examiner :

```bash
uv run python - <<'PY'
import json, resource, tempfile, time
from pathlib import Path
from radar.domain.models import Link
from radar.store.db import Database

started = time.perf_counter()
with tempfile.TemporaryDirectory(prefix="watt-link-storage-") as directory:
    with Database(Path(directory) / "radar.sqlite") as db:
        with db.transaction():
            db.replace_links(
                Link(a=f"manual:test:{i:06d}", b="signal:test",
                     reasons=("Synthetic storage measurement " * 20,))
                for i in range(100_000)
            )
        assert db.conn.execute("SELECT count(*) FROM links").fetchone()[0] == 100_000
        print(json.dumps({"elapsed_s": time.perf_counter() - started,
                          "peak_rss_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024}))
PY
```

Un essai de délai dépassé a aussi été exécuté : scénario de stockage de 500 000 liens,
délai d'une seconde, code de sortie 2, statut `timeout`, aucun fichier de travail restant.
Le processus parent possède le répertoire temporaire pour le nettoyer même si le worker
doit être interrompu avant de pouvoir exécuter ses propres nettoyages.

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

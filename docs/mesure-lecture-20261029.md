# Coût local des variantes de lecture — 29 octobre 2026

Sur le petit corpus DREAL caviardé existant, la reconnaissance de tableaux multiplie le temps de lecture
à cache froid par environ **2,4**, sans changer les champs extraits par les règles. Le cache persistant évite
la reprise du parsing : les trois variantes retrouvent les mêmes résultats à chaud. Ce résultat soutient le
maintien des tableaux en option ; il ne mesure pas leur utilité sur les annexes complexes encore absentes du corpus.

Le script [compare_reading.py](../tools/compare_reading.py) reproduit cette comparaison sans réseau ni modèle.
Il utilise les lecteurs et les règles existants, sans modifier leurs options par défaut, sans écrire dans `data/`
et sans créer de référence humaine. Les caches temporaires sont séparés par variante et supprimés à la fin.

## Population et protocole

- 7 dossiers, 11 PDF réels caviardés, **77 pages**, 2 642 296 octets ; une seule région et une seule famille de sources.
- Empreinte des noms de fichiers et SHA-256 PDF : `a46c1f2da1cebc070698c389ee49d59aca69edcb5eb8fb7e8a4d69470e5e0731`.
- Python 3.12.14, PyMuPDF 1.28.2, Tesseract 5.5.0 ; modèle français local, core du dépôt au commit `40f1291`.
- Une passe séquentielle avec un nouveau cache SQLite, puis une passe recréant les lecteurs sur ce même cache.
  Le cache disque du système n'est pas vidé ; « froid » désigne ici le cache applicatif uniquement.
- Le chronomètre inclut la lecture, l'OCR éventuellement déclenché, les diagnostics et l'extraction par règles.
  Le chargement initial des octets PDF et la création des lecteurs ne sont pas inclus. Aucun classement,
  contrôle métier, regroupement de projets ou envoi au navigateur n'est chronométré.

## Résultats de cette exécution

| Variante | Cache applicatif froid | Cache persistant réutilisé | Tables / lignes reconnues | Champs de règles modifiés face à la base |
| --- | ---: | ---: | ---: | ---: |
| Texte et champs, OCR primaire disponible | 3,372 s | 0,039 s | 0 / 0 | 0 |
| Même lecture avec tableaux | 8,082 s | 0,060 s | 65 / 374 | 0 |
| Même lecture avec second OCR sélectif | 3,819 s | 0,042 s | 0 / 0 | 0 |

Chaque passe froide compte 11 échecs de recherche dans le cache, puis chaque passe chaude 11 lectures depuis
ce cache et aucun échec de recherche. Les empreintes du texte, des champs de formulaire et les associations
numériques restent identiques entre les deux passes d'une même variante. Aucune erreur de lecture, page
illisible, limite de tableau ou extraction de dossier en échec n'est observée sur ces fichiers.

Les tableaux ajoutent une représentation structurée et changent le texte concaténé de 8 PDF. Ils n'ajoutent
ni ne retirent d'association littérale unique nombre/unité par rapport à la lecture de base ; leurs répétitions
ne sont pas comptées comme des informations supplémentaires. Les valeurs de règles sont comparées après
arbitrage entre les pièces d'un même dossier. Les preuves, le classement et les valeurs validées ne sont pas
évalués par cette comparaison.

**Aucun passage OCR n'a été déclenché** : les 77 pages disposent d'une couche texte suffisante. La différence
de durée entre base et second OCR n'est donc pas un coût mesuré du second moteur. Le corpus exclut les scans
pour pouvoir être caviardé de façon fiable ; il ne peut pas démontrer un gain de précision OCR. Les essais sur
scans construits restent des tests de fonctionnement distincts, pas des annotations administratives réelles.

Les compteurs de tableaux sont des métadonnées de contenu conservées dans le cache : les retrouver à chaud
ne signifie pas que 77 pages ont été analysées à nouveau. Les compteurs de cache décrivent cette réutilisation.
Une seule exécution ne suffit pas à estimer un débit, une latence p95 stable, une capacité cloud ou un coût mensuel.

## Relancer

```bash
UV_CACHE_DIR=/tmp/watt-uv-cache \
TESSDATA_PREFIX=/workspace/.local/share/tessdata \
uv run python tools/compare_reading.py > /tmp/watt-reading-comparison.json
```

`TESSDATA_PREFIX` doit désigner le répertoire réellement installé sur la machine ; l'omettre lorsque Tesseract
utilise son installation standard. `--no-ocr` désactive explicitement les deux passages locaux, et `--corpus`
permet de choisir un autre corpus au même format. Le script borne le manifeste, les fichiers, les pages et
les travaux natifs ; il accepte au plus 100 dossiers, 100 PDF et 128 Mio d'octets PDF au total.

Stdout contient un seul objet JSON, les diagnostics natifs vont sur stderr. Le code de sortie vaut 2 si une
lecture ou extraction manque, ou si une erreur de tableau/OCR ou une page illisible subsiste. Les empreintes
et listes de champs modifiés facilitent le diagnostic ; **aucun score d'exactitude ou de rappel humain n'est calculé**.

Pour décider d'une activation plus large, il reste à comparer les variantes sur une campagne figée contenant
de vraies annexes, des tableaux à en-têtes multiples et des scans difficiles, avec références et arbitrage humains.

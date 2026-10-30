# Lecture adaptative — politique et validation du 30 octobre 2026

La lecture adaptative est optionnelle. `RADAR_READING_MODE=economical` conserve le chemin existant ;
le drapeau historique des tableaux reste respecté. Le mode `adaptive` commence par les textes et champs,
puis classe les pages par indices littéraux de quantités/projets et de contexte incertain. Le mode `full`
demande les tableaux sur toutes les pages, dans les bornes existantes. Ces indices servent au routage,
pas à une décision métier ni à une preuve de pertinence des pages écartées.

La politique `adaptive-v1` fixe deux budgets documentaires : `RADAR_READING_MAX_DEEP_PAGES` pour les
tableaux et `RADAR_READING_MAX_OCR_PAGES` pour l'OCR. Les limites natives de lignes, caractères,
pixels et durée continuent de s'appliquer. Les scans sans indice restent des candidats dont l'utilité
est inconnue. Le dépassement conserve les pages non lues et les raisons ; il ne vaut jamais complétude.

Le cache inclut politique, budgets, version des formats/PyMuPDF, tailles, pages, caractères, durée et
options des tableaux. La première couche texte/champs reste dans le cache de lecture ; les textes OCR
primaires restent dans leur cache moteur ou les transcriptions privées. Le second OCR conserve en plus
leurs empreintes et moteurs avant amélioration. `Filing.reading_diagnostics` enregistre les empreintes
du premier passage, la clé de reconstruction, les motifs d'approfondissement, les compteurs et les pages
utiles non approfondies. Aucun texte brut supplémentaire n'est publié dans ces diagnostics.

Les observations conservent document, preuve, équipement, état, phase, scénario, projet explicitement nommé,
unité/base et localisation native. Les cellules Office n'ont pas de page PDF. Une unité portée uniquement
par l'en-tête reste une observation distincte, sans promotion automatique ; une formule sans résultat fiable
reste incertaine. Les désaccords sur les valeurs d'un même contexte, plusieurs projets nommés, ou une preuve
de puissance rattachée à une phase, un scénario, un équipement différent, une installation existante ou une
base incertaine imposent l'abstention et la relecture. Les observations et les candidats initiaux sont
conservés ; les corrections humaines restent appliquées après la sortie machine brute.

## Mesure reproductible

Commande hors réseau et sans modèle :

```bash
uv run python tools/compare_reading.py --no-ocr
```

Le [rapport brut](measurements/adaptive-reading-20261030.json) porte sur les mêmes 7 dossiers,
11 PDF caviardés et 77 pages dans les quatre variantes. Le manifeste et les octets ont leurs empreintes
dans le rapport. Chaque variante utilise un cache neuf, puis un lecteur neuf réutilisant ce cache persistant.

| Variante | Temps froid | Cache persistant | Pages de tableaux | Tableaux |
| --- | ---: | ---: | ---: | ---: |
| Économique | 2,216 s | 0,0236 s | 0 | 0 |
| Tableaux complets | 4,870 s | 0,0320 s | 77 | 65 |
| Second OCR, désactivé pour cet essai | 2,185 s | 0,0230 s | 0 | 0 |
| Adaptative, budget 8 pages/document | 3,854 s | 0,0287 s | 46 | 37 |

L'adaptative traite moins de pages structurées et coûte ici environ 21 % de moins que les tableaux complets,
tout en coûtant davantage que la lecture économique. Aucun champ extrait ni association littérale
nombre/unité n'est ajouté ou perdu sur ce petit corpus. Six pages à indices utiles dépassent le budget
adaptatif : elles restent explicitement non approfondies, `readers_complete=false` et le programme sort avec
le code 2. Ce résultat incomplet est conservé, sans suppression de pages du dénominateur.

Ces temps correspondent à une seule répétition sur cet hôte, avec d'autres travaux locaux en cours.
Ils ne mesurent ni débit de production ni précision métier. Le corpus est textuel : aucun OCR n'est exécuté
et sa représentativité pour les scans difficiles reste absente. Le comparateur mesure lecture et extraction
par règles ; les contrôles de contexte de la pipeline sont vérifiés séparément par les tests.

Pour une comparaison métier, conserver une campagne figée et deux snapshots bruts via les commandes
`campaign snapshot` et `campaign compare`. Les partitions et références de la campagne doivent rester
identiques ; les références de développement servent aux réglages, calibration et test restent réservés.
Aucune annotation indépendante supplémentaire n'a été reçue ici et aucun score métier n'est calculé.
L'option adaptative reste désactivée par défaut.

# Lecture des tableaux, contextes et scans difficiles

La lecture structurée des tableaux et le second passage OCR local sont **optionnels** : leur présence ne prouve pas une meilleure précision métier. Ils doivent être comparés sur une campagne humaine vérifiée avant généralisation.

## Tableaux PDF

`PdfReader(read_tables=True)` reconnaît les tableaux dans le processus PDF isolé déjà soumis à un délai maximal. Les budgets par défaut sont 20 pages tentées, 2 000 lignes et 200 000 caractères de cellules par document ; ils sont configurables. Les pages non traitées ou tronquées restent explicitement signalées dans `table_errors`. Les caches distinguent les politiques et limites de lecture.

Chaque `PdfTable` conserve la page, l'indice du tableau, son rectangle et les cellules par ligne. Les textes des cellules sont conservés dans leur ordre ; aucune unité ni valeur n'est inventée dans le texte rendu. Une mesure dont l'unité est seulement dans l'en-tête peut devenir une **observation contextualisée**, avec cet en-tête comme provenance, mais pas une valeur projet automatiquement acceptée.

Les avis et annexes peuvent combler une déclaration de quantité entièrement manquante avec une ligne explicite portant son unité et identifiant le projet. Les formulaires et décisions conservent leur priorité. Les phases et scénarios ne sont ni additionnés ni réduits à une valeur scalaire.

## Observations de quantités

`Filing.quantity_observations` conserve les bornes normalisées MW/MWh, l'unité AC/DC/crête, la citation, sa page et, le cas échéant, tableau/ligne/colonne. Le type d'actif (stockage, centre de données, solaire, raccordement, équipement…), son état proposé/existant/exemple/inconnu, la phase et le scénario restent séparés. Leur identification est une heuristique destinée à la relecture, pas une conclusion métier certifiée. Des contextes multiples ou incomplets restent explicites.

La limite de 200 observations par dossier est visible dans `strata.quantity_observation_limit_reached`. Les observations demandant une contextualisation produisent un avertissement sur les dossiers cibles. Leurs citations sont anonymisées au même endroit que les autres preuves avant stockage dans le dossier dérivé.

## Second passage OCR sélectif

`make_ocr(secondary_pass=True, secondary_max_pages=8)` conserve le premier moteur et réessaie seulement les pages scannées non lues ou de faible qualité. Il privilégie les pages proches d'indices techniques, puis utilise un rendu local Tesseract à 400 dpi, dans les limites de pixels, octets, concurrence et délai existantes. Le moteur natif est utilisé directement ; aucun service payant ni processus OCR détaché n'est lancé.

Une transcription humaine n'est jamais remplacée. Un résultat secondaire n'est retenu que s'il améliore la lisibilité heuristique et conserve les associations numériques déjà reconnues. Une divergence de quantité garde le premier texte et une erreur explicite pour relecture. Les tentatives sans texte ne marquent pas une page comme lue. Les caches réutilisent les lectures réussies ; les erreurs restent visibles et peuvent être réessayées.

`PdfContent.coverage_summary()` distingue pages, lectures OCR, pages encore non lues, pages demandées, processus natifs effectivement démarrés, pages servies du cache, tableaux et erreurs. Les compteurs `reading_*` des dossiers rendent ce travail observable. Ils décrivent le travail local ; ils ne constituent ni un prix cloud ni une estimation de précision. Un test OCR réel porte sur un scan **construit en anglais**, seule langue installée sur l'hôte de validation ; la qualité française doit être mesurée dans l'image disposant de `fra`.

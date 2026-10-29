# Qualifier les sources, périodes et nouvelles régions

Le catalogue privé complète la configuration intégrée des Hauts-de-France. Il permet d'ajouter des régions à
partir d'URL explicitement fournies, puis de découvrir les pages annuelles réellement liées par ces sources.
Aucune adresse n'est générée à partir d'un nom de région ou d'une année.

Le catalogue utilise un fichier JSON versionné, conseillé sous `data/reference/source-catalog.json`. La
configuration par région contient `slug`, `departments`, `dreal_base`, `dreal_listings`, `dreal_periods`,
`mrae_pages`, `prefectures` et les éventuelles `seeds` de découverte (`dreal` et `mrae`). Les codes des départements
et les URL HTTPS gouvernementales sont contrôlés. Les URL DREAL restent sous leur base configurée. Les nouvelles
configurations sont des déclarations de l'opérateur, pas une preuve que les sites répondent ni qu'ils couvrent
tous les projets. Un conflit ne remplace jamais silencieusement une région existante.
Un nouvel import peut ajouter des URL de départ à `seeds` lorsque la configuration active de la région est
identique ; il ne supprime pas les anciennes URL ni les sources déjà adoptées.

La découverte parcourt séquentiellement au maximum le nombre de pages demandé (1 à 100, 8 par défaut), 500 liens
par page, une file de 500 liens et un catalogue de 10 000 candidats. Le transport conserve ses limites HTTP ;
chaque page doit également tenir dans 4 Mio pour l'analyse. Les liens vers des PDF ou autres pièces ne servent
pas de pages de découverte. Les pages d'activité génériques, les liens externes, les années futures et les
liens portant plusieurs années ne deviennent pas des sources annuelles candidates. Une limite atteinte reste
visible sous `partial`, avec les années demandées qui n'ont pas été observées. Un blocage arrête la découverte
et conserve les candidats déjà observés.

Un candidat contient l'URL observée, l'URL parente, le texte du lien, l'année, la région, la date d'observation et
le hash de la page parente. Il reste `pending` et ne modifie pas les collecteurs. Son adoption est explicite :
la page cible est téléchargée à nouveau, les redirections hors origine sont refusées, et sa compatibilité avec
le connecteur est contrôlée. Une page MRAe doit annoncer la région dans son en-tête et fournir des avis de
projets reconnus ; une page DREAL doit contenir des articles d'examen au cas par cas. L'année doit apparaître
dans l'URL ou l'en-tête de la page cible. Le contrôle reste structurel et ne vaut pas validation humaine du contenu.

Une adoption refusée garde un état `rejected`, `blocked` ou `failed` et sa raison. Une adoption réussie conserve
le hash de la cible et sa date. Une URL déjà configurée n'est pas présentée artificiellement comme un nouveau
candidat en attente. Les anciennes configurations restent intactes lors d'une nouvelle tentative échouée.

Les listes DREAL adoptées sont ordonnées par année réelle pour préserver la collecte récente des deux dernières
années. `dreal_periods` conserve la période explicite même si le nom du fichier ne commence pas par l'année ; les
archives `before-AAAA` restent distinctes. L'inventaire de couverture et les reprises de collecte utilisent cette
métadonnée. Une page configurée ou adoptée reste « jamais parcourue » tant qu'aucune collecte n'a établi son état.

Les tests de ce module utilisent des pages construites et un transport HTTP simulé. Ils ne qualifient aucun
nouveau site en conditions réelles. La configuration réseau de l'environnement doit autoriser les domaines
concernés avant toute découverte ou adoption réelle.

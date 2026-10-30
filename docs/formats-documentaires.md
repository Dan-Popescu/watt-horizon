# Pièces HTML, DOCX, XLSX et archives

La récupération de dossier reconnaît maintenant les liens PDF, DOCX, XLSX et ZIP, y compris avec paramètres.
Une page HTML complète peut aussi être conservée comme pièce ; un fragment contenant seulement des liens reste
une page de navigation. Le format est identifié par les octets, sans faire confiance à l'extension. Les sources
existantes passent par cette ingestion commune : leurs commandes restent compatibles.

Les originaux restent adressés par SHA-256 dans le stockage privé. Une même URL produisant de nouveaux octets
ajoute une version documentaire. Les archives sont conservées avec leurs membres pris en charge, leur chemin
interne et le hash du conteneur. Les relations privées d'archive incluent le hash du conteneur : deux versions
successives ne réécrivent pas leur provenance. Les membres non pris en charge restent dans l'archive originale
et portent une relation `unsupported` ; le parcours est alors partiel. Aucun membre n'est extrait vers un chemin
du système de fichiers et aucun fichier exécutable n'est lancé.

## Lecture et incertitudes

- HTML : texte des paragraphes et cellules ; scripts, styles et ressources intégrées ne sont pas exécutés.
- DOCX : paragraphes du corps et cellules des tableaux. Les images, en-têtes et notes non lus restent signalés.
- XLSX : feuilles, coordonnées de cellules, chaînes partagées, en-têtes et contexte de ligne conservés.
  Les formules ne sont jamais calculées ; même un résultat en cache reste explicitement non vérifié et exclu
  du texte fiable. Une unité seulement présente en en-tête reste une observation avec provenance, sans promotion
  scalaire automatique. Les graphiques et images exigent une relecture.
- ZIP : original et pièces reconnues, y compris archives imbriquées dans les bornes ci-dessous.

Ces formats ont `pages=0`. Les citations utilisent `paragraph:N`, `table:N:row:N:cell:N` ou
`sheet:1:cell:A1` (position réelle de la feuille), jamais des numéros de page PDF fabriqués. Les noms de feuilles
ne deviennent pas des identifiants publics, car ils peuvent contenir des noms de personnes. L'extraction commune reçoit les segments fiables ;
une preuve nécessitant une concaténation ambiguë entre cellules ne justifie pas une valeur scalaire. Les cellules
et en-têtes servent aussi aux observations contextualisées. Les sorties brutes restent distinctes des corrections.

L'interface privée de campagne affiche les segments par blocs bornés et leurs localisations exactes.
Le validateur vérifie la citation dans le paragraphe ou la cellule correspondants, refuse une page inventée
et les résultats de formule non vérifiés, et contrôle l'unité littérale ou celle de l'en-tête d'une cellule.
Les contrôles d'accès, le masquage des personnes et `Cache-Control: no-store` s'appliquent aussi à ces routes.
Les originaux et exports de relecture ne sont pas ajoutés à l'API publique.

## Bornes et états

Les limites structurelles par défaut sont : 32 Mio compressés/par pièce, 64 Mio décompressés cumulés,
100 membres cumulés d'archives, ratio maximal 200, trois niveaux d'archives, 8 Mio de XML par partie,
200 000 nœuds XML, profondeur XML/HTML de 64, 20 000 lignes, 100 000 cellules/segments et 2 millions de
caractères, contexte conservé compris. Le répertoire central ZIP est borné avant son allocation par le lecteur.
ZIP64, archives chiffrées, chemins absolus, traversées `..`, liens symboliques et chemins Windows sont refusés.
Les déclarations et entités XML sont refusées ; aucune relation Office externe n'est ouverte.

Lecture et décompression natives utilisent des processus possédés et arrêtables avec un délai de 30 secondes
par défaut. Le lecteur commun réutilise son délai et ses limites de taille/texte configurés. Sous POSIX,
ces travailleurs appliquent aussi une limite d'espace d'adressage de 512 Mio ; un dépassement devient une erreur
explicite du travail. Les plateformes sans `RLIMIT_AS` conservent les bornes structurelles et le watchdog,
mais ne disposent pas de ce plafond mémoire natif : elles doivent imposer une limite de processus/conteneur.
Les contraintes de l'hébergement restent distinctes de ces limites applicatives.

Une limite ou une erreur ne produit pas une lecture « complète ». La complétude de récupération des liens,
la lisibilité des pièces et l'achèvement de l'extraction sont des états différents. Les erreurs de format,
ressources intégrées non lues et formules incertaines restent accessibles au pilote et à la relecture.
Les archives dont les membres sont limités ou non pris en charge rendent leur parcours partiel.

## Validation et limites de couverture

Les tests construisent des documents et des parcours HTTP simulés : lecture jusqu'à l'extraction, conservation
des originaux et versions, membres ignorés visibles, citations humaines de protocole, routes privées,
traversées de chemins, liens symboliques, compression, XML, cellules et plafond mémoire natif.
Ces fiches de test ne sont pas des annotations métier et ne sont jamais importées dans les références réelles.

Aucune source administrative supplémentaire n'a été qualifiée ou activée pendant cette livraison. La politique
réseau active ne permet pas les destinations administratives. Il reste à observer les pages réelles, qualifier
leurs périodes et pièces, collecter un pilote borné puis obtenir de véritables doubles lectures pour mesurer
les omissions et la précision sur ces formats. Aucun gain métier n'est déduit des tests construits.

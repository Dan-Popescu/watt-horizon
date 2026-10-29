# Découverte web et pistes à corroborer

Le connecteur de recherche complète les listes administratives et les flux RSS. Il enregistre des **pistes**, jamais des faits extraits ni des projets validés. Il ne crée aucune référence humaine et n'appelle aucun modèle.

## Planifier puis rechercher

`radar discover plan` prépare des requêtes depuis les sociétés connues, leurs alias adoptés, SIRET, communes, postes et numéros de dossier, ainsi que les termes explicitement fournis. Une rotation entre ces familles évite qu'une longue liste de sociétés monopolise les premières requêtes. Les expressions contenant des coordonnées ou noms précédés d'une civilité sont refusées. Les requêtes ne déduisent pas de nouveaux noms ou lieux.

Chaque plan appartient à une campagne de recherche datée en UTC par défaut. Replanifier dans la même campagne déduplique les requêtes ; une nouvelle date permet de rechercher de nouvelles publications sans effacer l'historique. La bibliothèque accepte également un identifiant de campagne explicite. Chaque requête conserve ses dossiers d'origine et son périmètre de domaines.

`radar discover search` interroge l'interface HTML publique de DuckDuckGo par défaut, exclusivement via `PoliteHttp`. Un autre endpoint HTTPS compatible peut être configuré explicitement. Le parseur, les limites et les reprises ont été testés avec des réponses construites ; la disponibilité réelle du moteur reste à qualifier. Aucun CAPTCHA ou blocage n'est contourné. Une réponse inconnue est un échec, pas une recherche vide réussie.

Les budgets distinguent requêtes, pages, résultats et tentatives HTTP physiques. Le compteur physique inclut redirections et reprises HTTP, et est enregistré avant l'envoi ; un crash ne donne pas une reprise gratuite. Un plafond cumulatif existe pour chaque requête de campagne. Les réponses de recherche sont limitées à 2 Mio, les résultats du parseur à 1 000 et les textes de titre/résumé à 4 000 caractères avant minimisation. Un plafond atteint laisse l'état partiel visible.

Les URL de suivi du moteur sont décodées et les domaines sont comparés à des limites de noms DNS, afin de refuser par exemple `gouv.fr.example.org`. Les paramètres fonctionnels des téléchargements restent conservés. Les titres et résumés enregistrés sont minimisés ; les personnes reconnues, téléphones et courriels sont masqués. Un résumé de moteur ne sert jamais de preuve d'une puissance ou d'une localisation.

## Corroborer des pièces officielles

`radar discover resolve` examine une sélection bornée des pistes. Seuls les domaines administratifs admis sont téléchargés. Le choix explicite d'un adaptateur ne prouve pas la région : un hôte DREAL/préfectoral régional configuré, ou un contexte de région/département dans la pièce officielle, est nécessaire avant ingestion. Un résultat national MRAe concernant une autre région reste non confirmé, même si le résumé du moteur affirme le contraire.

Les titres de `DocumentRecord` viennent du titre de la page officielle ; pour un PDF direct, le titre de recherche n'est pas recopié. Le téléchargement initial est transmis au parcours de dossier pour éviter une seconde requête. Le parcours conserve ses propres budgets, preuves de rattachement et états partiels. Les préfectures gardent une cadence globale d'au moins cinq secondes entre requêtes, même en changeant de département.

Les états des pistes sont `pending`, `running`, `unconfirmed`, `partial`, `resolved`, `blocked`, `failed` ou `budget`. **Résolu** signifie que des pièces officielles ont été récupérées ; la classification et l'extraction du projet restent dans la pipeline normale. Les pistes RSS pertinentes sans commune reconnue restent dans cette file et ne deviennent pas des signaux géolocalisés.

Les recherches interrompues reprennent leur curseur et conservent leur compteur d'envoi ; une page coupée par le budget de résultats est revisitée pour ne pas perdre sa fin, avec déduplication des pistes. Les états partiels peuvent être repris ; les échecs demandent une relance explicite. Les blocages ne sont pas automatiquement remis dans la file. Le nombre de tentatives par piste est également borné.

`radar discover status` expose les compteurs et une liste limitée de pistes récentes. Les tables résident dans la base principale et entrent dans les sauvegardes SQLite existantes. Les tentatives et leurs états sont conservés séparément des caches jetables. L'accès au moteur public par défaut n'a pas de facturation API configurée ; les frais d'un endpoint personnalisé sont inconnus. Les coûts cloud et le temps humain ne sont pas estimés à zéro.

Les commandes réseau nécessitent des domaines autorisés dans l'environnement et `RADAR_OFFLINE=0`. Les tests restent entièrement hors ligne. La couverture du moteur, des sources et du corpus humain doit être mesurée avant toute promesse d'exhaustivité.

# Fiabilisation de WattHorizon

État au 29 octobre 2026, branche `feat/scalable-quality-pipeline`. Les constats ci-dessous proviennent de la revue du 28 octobre ; ils ont été transformés en correctifs et tests hors ligne. Une suite verte ne constitue ni une certification de précision métier ni une mesure de capacité de production.

La base historique examinée comptait 35 documents, 22 dossiers, 10 projets et aucune correction humaine. Les 21 références de classement et les 21 fiches de champs livrées restent non vérifiées. Ces volumes historiques ne décrivent pas la base vide de l’environnement cloud.

## Mécanismes implémentés

- Références humaines figées, identités complètes source/région/dossier et refus des numéros ambigus.
- Partitions groupées développement/calibration/test, effectifs par groupes indépendants, référence approuvée distincte des essais et profils de promotion explicites.
- Validation nombre/unité/contexte/preuve, calibration par champ et présence, relecture partielle et audits des acceptations automatiques.
- Métriques OCR sensibles aux associations nombre/unité, WER/CER pour transcriptions complètes déclarées et erreurs visibles.
- Registre IA durable distinct du cache, demandes partagées, réservations, états incertains, réponses brutes, reprises et budgets fermés par défaut.
- Téléchargements et exports en flux ; limites avant lecture des blobs ; processus PDF/OCR isolés avec watchdog et cache mémoire borné.
- Index de rapprochement, agrégats complets, pagination SQL, rattachements multiples et écritures différentielles.
- Sauvegarde cohérente WAL, restauration vérifiée, verrou d’écriture et publication de jeux approuvés avec bascule atomique et rollback.

Voir [la spécification de livraison](scalable-quality-pipeline.md) et [l’exploitation](exploitation.md). Les cas denses peuvent produire eux-mêmes un nombre quadratique de liens ; aucun résultat n’est tronqué silencieusement. Le rapprochement conserve des métadonnées proportionnelles aux dossiers et aux liens, et sa capacité réelle reste à mesurer.

## Validation métier et exploitation restant à établir

Les [trois lots de couverture documentaire](documentary-quality-plan.md) disposent désormais de leurs outils :
inventaire et rattrapage reprenable, parcours borné des pièces avec provenance, extraction prudente dans les
annexes, campagnes aveugles et comparaison des sorties brutes. L'[inventaire externe](inventaire-reference-decouverte.md)
permet d'inclure les dossiers jamais collectés dans la mesure du rappel. Les défauts relevés pendant les deux
revues de livraison ont été corrigés et couverts par des tests. La collecte réelle, l'annotation représentative et
la qualification des nouvelles régions/périodes restent à effectuer ; l'existence de ces outils ne vaut pas validation métier.

1. Annoter des groupes représentatifs des sources, régions, périodes et modalités, avec une part à l’aveugle et une double annotation. Ne pas déduire une validation humaine de la sortie machine.
2. Mesurer erreurs publiées, faux absents, informations et projets manqués, faux regroupements, coût humain et coût par projet exploitable. Les seuils par défaut de 0,95 et 30 groupes sont des règles sur des observations, pas des garanties statistiques.
3. Réserver un nouveau test prospectif à des groupes réellement nouveaux, avec un manifeste distinct et une date de première observation réelle. Un ancien test déjà exploité pour régler les règles ne devient pas indépendant par renommage.
4. Mesurer mémoire, durée, taux de cache, tokens et latence p95 sur des volumes croissants ; choisir l’hébergement, les budgets et la disponibilité avant déploiement. Aucun coût mensuel ni débit de production n’est garanti ici.
5. Les seconds moteurs OCR, modèles moins chers et nouvelles architectures distribuées restent des décisions à mesurer sur ces références, pas des améliorations supposées.

## Ordre retenu pour les correctifs

| Priorité | Travail | Résultat attendu |
| --- | --- | --- |
| 1 | Références humaines immuables, séparation apprentissage/évaluation et contrôles de régression | Une mesure qui ne valide pas ses propres erreurs |
| 2 | Unités, contexte et confiance mesurée séparément pour chaque champ | Moins de valeurs fausses acceptées silencieusement |
| 3 | Reprise durable des traitements IA, erreurs isolées et dépenses bornées | Des lots reprenables sans perte ni répétition incontrôlée |
| 4 | Évaluation OCR jusqu'au nombre, à l'unité, au champ et à sa preuve | Repérer les erreurs importantes dans chaque modalité |
| 5 | Rapprochement indexé, mémoire bornée, pagination et totaux complets | Un coût de traitement maîtrisé avec le volume |
| 6 | État de relecture par champ et correction groupée des erreurs répétées | Moins de temps humain par projet exploitable |

## Historique des défauts ayant motivé les tests de régression

- `pipeline/corrections.reviewed_references` et `quality/arbiter.verified_values` utilisent les valeurs courantes après une validation ancienne. Une valeur validée à 50 peut devenir une référence à 80 sans nouvelle relecture. Réutiliser la valeur effectivement approuvée.
- `pipeline/run.process_filings` apprend aussi sur les corrections du jeu de test. `quality/errors.analyse` masque les détails avant certains compteurs. Séparer apprentissage et notation ; calculer les compteurs avant de masquer les exemples.
- `evaluation/report.compare` ignore les mesures disparues ; `cli.cmd_eval` remplace la référence par le dernier essai, même dégradé. `shadow --strict` ne bloque pas tous les échecs de reconstruction. Conserver une référence approuvée et traiter explicitement les mesures impossibles.
- `quality/confidence.calibrate` mélange les champs et les absences. Des absences correctes sur un champ peuvent suffire à faire accepter une puissance fausse. Évaluer chaque décision sur des observations indépendantes et maintenir un contrôle aléatoire des acceptations automatiques.
- `extraction/llm.value_in_quote` peut accepter 50 MW à partir d'une citation de 50 MWh. Vérifier unité, objet et contexte, pas seulement la présence du nombre.
- `pipeline/llm_batch` ne conserve pas toutes les dépendances dossier/requête partagée. Une réponse disponible peut laisser un dossier non classé. Un changement de configuration peut aussi faire perdre le suivi d'une ancienne réponse ; une réponse mal formée peut bloquer les suivantes.
- L'envoi des lots n'est pas réservé de manière exclusive. Une interruption ou deux envois concurrents peuvent répéter une demande. Une relance directe ignore les demandes encore en cours en lot. Conserver les réponses brutes, versions, tentatives et envois incertains ; réconcilier avant de réessayer.
- `cli.cmd_process --wait` peut sortir avant d'intégrer le dernier étage ou sans attendre un lot déjà en cours. Terminer selon l'état réel des tâches, pas un nombre fixe de tours.
- `reading/pdf.PdfReader.prefetch` est appelé avant la protection des erreurs par dossier. Un PDF corrompu dans une préparation parallèle peut arrêter tout le lot.
- `api/queries.zone_pressure` calcule ses totaux sur une liste plafonnée à 200. Calculer les totaux complets séparément de la liste affichée.
- `store/db.upsert_document` déplace un PDF partagé vers le dernier dossier. Séparer le fichier unique de ses rattachements administratifs.
- `quality/triage` peut retirer un dossier après une seule correction partielle. Suivre les champs restant à vérifier, y compris dans la double annotation.

## Historique des limites de méthode et de volume

- `linking/linker.propose_links` construit toutes les paires en mémoire : 1 000 dossiers sans aucun lien produisent déjà 499 500 comparaisons. Indexer les indices partagés avant de comparer.
- La mesure OCR par mots manquants ne détecte pas l'inversion des nombres de « 50 MW / 100 MWh » vers « 100 MW / 50 MWh ». Mesurer aussi les valeurs exactes, unités et associations. Signaler les pages utiles non lues dans les documents mixtes ; prévoir un second moteur pour une lecture dégradée.
- Le tirage des rejets proches des critères et le dénominateur de l'estimation du rappel ne représentent pas exactement la même population. Séparer la recherche d'erreurs de la mesure statistique.
- Mesurer coût par projet exploitable, temps humain, erreurs parmi les valeurs publiées, informations laissées inconnues et projets manqués. Ajouter des plafonds de dépense et de tentatives avant les gros volumes.

Pour chaque correction : reproduire le défaut, ajouter le test, corriger, puis rejouer le corpus. Tous ces travaux peuvent commencer sans clé ni nouvelle collecte. Une future mesure sur des documents représentatifs réellement vérifiés restera nécessaire avant de promettre un taux d'erreur très faible.

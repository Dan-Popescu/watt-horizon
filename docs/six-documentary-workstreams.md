# Les six chantiers documentaires

Le périmètre est celui des six recommandations initiales. Les trois premiers lots techniques livrés dans
`b2b887a` n'en constituent qu'une première étape. L'ordre de priorité reste couverture, dossiers complets,
références humaines, puis approfondissement de la découverte et de la lecture et raccordement de la boucle.

| Chantier initial | Première livraison | Complément implémenté dans cette livraison |
| --- | --- | --- |
| 1. Sources, territoires et périodes | Inventaire honnête, archives DREAL reprenables, états de fraîcheur | Catalogue extensible validé, découverte de pages annuelles observées, qualification distincte de l'adoption |
| 2. Dossiers complets | Parcours borné, annexes neutres, liens indirects, provenance et erreurs | Relier les nouvelles pistes officielles au même parcours, conserver les limites et les pièces non accessibles |
| 3. Découverte au-delà des titres/RSS | Audits DREAL/MRAe/préfectures, inventaire externe des dossiers manqués | Requêtes par sociétés/alias/SIRET/communes/postes, moteur public borné, pistes non localisées, corroboration officielle |
| 4. Documents longs et complexes | Sélection tardive de passages, quantités prudentes dans les annexes | Tableaux structurés, contexte équipement/phase/scénario, seconde lecture OCR locale sélective et comptabilité de lecture |
| 5. Références humaines | Campagnes aveugles, double lecture, citations et groupes figés | Interface privée utilisable, brouillons et finalisation contrôlée, séparation des lecteurs et historique des décisions |
| 6. Mesure et amélioration continue | Comparaisons exactes/tolérées, rappel sur inventaire externe | Import traçable du consensus de développement, propositions d'amélioration sans fuite du test, compteurs par source, coût et effort mesurés |

Les fonctions sont testées avant intégration. Les téléchargements réels respectent la configuration réseau,
les budgets et l'arrêt sur blocage. Les sorties machine, les citations contrôlées automatiquement et les données
synthétiques ne deviennent jamais des validations humaines. Les scores sans référence utilisable restent non mesurés.
Le catalogue ne déclare pas une couverture nationale à partir d'URL supposées ; les pages doivent être observées
et qualifiées. Les variantes de lecture coûteuses restent explicites jusqu'à leur comparaison sur les références.

## Exécuter le parcours complet

1. Inspecter `radar coverage --years 2024 2025 2026` et `radar catalog status`. Le catalogue externe par défaut
   est `data/sources/catalog.json` ; `RADAR_SOURCE_CATALOG` permet d'en choisir un autre. Les graines de nouvelles
   régions s'importent explicitement, sans inventer d'URL ni de vérification humaine.
2. Lorsque les domaines nécessaires sont autorisés, utiliser
   `RADAR_OFFLINE=0 radar catalog discover --source mrae --years 2024 2025`,
   qualifier les candidats avec `catalog adopt --id ...`, puis collecter les sources adoptées.
   `collect dreal --backfill --max-pages 3` reprend les archives ; les préfectures restent explicites et séquentielles.
3. Prévisualiser `radar discover plan --term 'stockage batteries'`, ajouter `--enqueue` pour enregistrer les requêtes,
   puis exécuter `RADAR_OFFLINE=0 radar discover search --max-requests 10` et `discover resolve --max-leads 5`.
   Le second accès réseau nécessite lui aussi `RADAR_OFFLINE=0`. Une campagne de requêtes est identifiée par la date
   UTC, ou par `--campaign`. Les pistes RSS non localisées alimentent également la planification. Les extraits du
   moteur ne deviennent jamais des faits ; les dossiers annuels ou indices régionaux sont laissés à leur collecteur.
4. Traiter avec `RADAR_OFFLINE=1 radar run`. Les options `RADAR_PDF_TABLES=1` et `RADAR_OCR_SECONDARY=1` activent
   respectivement les tableaux et le second passage local ; leurs budgets sont configurables. Les observations
   conservent les actifs, phases, scénarios et unités sans fabriquer de total. Voir [lecture-complexe.md](lecture-complexe.md).
5. Préparer une nouvelle campagne, exporter son snapshot initial et ouvrir [l'interface privée](interface-annotation-aveugle.md)
   avec `campaign serve --reviewer ... --slot a`, puis un autre lecteur pour le rôle `b`. Les citations de cellules
   peuvent référencer l'unité de la même colonne. Les brouillons ne valent pas validation et les désaccords restent ouverts.
6. Valider les projections courantes de chaque lecteur et comparer les variantes sur la campagne figée. Utiliser
   `radar campaign apply <campagne> --annotations <projection-a.jsonl> <projection-b.jsonl>` pour intégrer seulement
   le consensus de développement. Prendre exactement les projections indiquées par l'interface, pas un glob incluant
   leurs archives immuables. Les réponses de calibration et de test restent dans l'évaluation de campagne ; elles
   n'alimentent pas cet import d'apprentissage. Un conflit avec une décision humaine antérieure exige un arbitrage.

Après application : `radar run`, `radar errors`, `radar cases export`, puis `radar shadow --strict` et `radar eval --strict`.
Le reçu d'import, les preuves, les corrections ajoutées et la sortie machine initiale sont conservés séparément.
Une répétition identique de l'import est sans effet. Aucun appel modèle n'est déclenché par l'import.

`radar quality-loop` et le tableau de qualité affichent les audits par source, les pièces incomplètes, la fraîcheur,
la lecture, les recherches, les coûts au tarif enregistré et le temps de relecture déclaré lorsqu'ils existent.
Les coûts inconnus restent inconnus ; les compteurs de pages décrivent la dernière lecture, pas une facture cumulée.
L'[inventaire externe](inventaire-reference-decouverte.md) demeure nécessaire pour compter les dossiers jamais collectés.

## Prérequis externes restant distincts du code

La configuration cloud active limite actuellement les destinations aux gestionnaires de paquets et à GitHub.
Un brouillon a été enregistré avec les 19 domaines précis nécessaires aux sources configurées, au moteur public et
aux API de référence ; les destinations existantes sont conservées et aucun secret n'est ajouté. Il ne remplace pas
l'activation dans les paramètres de l'environnement. Le [contrat du skill de configuration](skill://plugin_connector_1p_ed5feb9070a08191b08c81c47947bc16/setup/references/onboarding.md)
précise : « Agent draft saves do not execute scripts, apply changes to the running environment, or publish it. »

La collecte réelle, la qualification du moteur dans ses conditions réelles et les annotations de véritables lecteurs
conditionnent encore la mesure du gain métier. Aucun de ces résultats ne se déduit des exemples construits, des
tests automatisés ou du petit corpus de régression existant. Les six chantiers sont outillés ; leur exploitation
reste soumise à ces faits vérifiables.

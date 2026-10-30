# Cinq nouveaux chantiers — 30 octobre 2026

Cette spécification complète les [six chantiers déjà livrés](six-documentary-workstreams.md)
depuis `cec4c7c51fcbe19004ab4890c82232b90f2275a0`. Elle reprend la demande de
transfert du 30 octobre. Le package et les commandes restent `radar` / `RADAR_*`.

1. **Pilote qualité réel.** Composer inventaire indépendant, campagnes figées,
   double lecture et comparaison avec `pilot plan|prepare|status`. La provenance
   externe est obligatoire ; les groupes restent complets au-delà des filtres.
   Conserver les sorties machine et les formulaires vierges, séparer développement,
   calibration et test prospectif, rendre visibles les manques et l'effort humain.
   Seul un consensus humain de développement alimente l'apprentissage existant.
2. **Couverture documentaire.** Raccorder HTML, DOCX, XLSX et archives à l'ingestion,
   l'extraction et la relecture privée. Conserver originaux, versions, pièces et
   rattachements. Borner téléchargements, archives, XML, cellules et calcul natif ;
   refuser les chemins dangereux et tout réseau documentaire implicite. Employer
   des localisations paragraphe/tableau/cellule/feuille/membre ; conserver les
   formules sans résultat fiable et les unités ambiguës comme incertaines.
   Qualifier une nouvelle source avant activation depuis des pages observées.
3. **Tâches durables.** Fournir `tasks plan|enqueue|run|status` avec entrées immuables,
   clés déterministes, générations, dépendances, budgets persistants, historique et
   baux avec fencing. Réserver brièvement, calculer hors verrou, publier atomiquement
   après validation du bail et des entrées. Tester crash, expiration, ancien worker,
   déduplication, entrée modifiée, dépendance bloquée et budgets après reprise.
   Tout HTTP passe par PoliteHttp ; suspendre la source après blocage. Hors ligne,
   le réseau attend sans tentative. Référencer le registre IA existant sans répéter
   un appel payant incertain. Les travaux lourds s'exécutent hors du service web.
4. **Mémoire et diagnostics.** Parcourir les liens en flux dans la pipeline et ses
   consommateurs sans troncature ; préserver raisons, dernier doublon, décisions,
   révisions et journal atomique. Garder le chemin complet de référence et prouver
   l'équivalence après ajouts, suppressions et retraits de décisions. Précalculer
   les diagnostics lourds par révision exacte ; exposer date, périmètre et statut
   périmé/indisponible. Mesurer avant/après dans des processus isolés identiques.
   `Database.replace_links` est déjà corrigé et ne fait pas partie de cette livraison.
5. **Lecture adaptative.** Versionner une politique économique puis approfondir les
   pages utiles ou incertaines dans des budgets explicites. Inclure toutes les options
   dans les caches, conserver premier passage et preuves, publier provenance,
   compteurs, contradictions et pages utiles non lues. Relier les quantités au projet,
   équipement, état, phase, scénario, unité et base ; abstention/relecture en cas
   de contradiction. Respecter les couches et les sorties machine brutes. Comparer
   les variantes sur les mêmes groupes figés, sans exploiter le test réservé.

## Validation et limites

Les tests ciblés couvrent les contrats risqués ; format Ruff, Ruff, mypy strict,
suite hors ligne, `radar run` et `radar eval` sont les contrôles finaux. Les données
brutes et exports humains restent privés. Aucun modèle payant n'est autorisé.

Au démarrage, le dépôt est propre au commit attendu. La politique réseau appliquée
n'autorise aucun domaine administratif : les observations de nouvelles pages,
qualifications territoriales, collecte réelle et inventaire externe réel sont
bloqués. L'implémentation doit signaler ces opérations restantes précisément.
Les références publiques non vérifiées ne remplacent aucune annotation humaine.

La livraison distingue mécanismes, collecte réalisée, annotations reçues, qualité
mesurée, configuration enregistrée et activation/publication. Réussir des tests
synthétiques ne démontre pas le rappel, la précision, le coût ou le débit métier.

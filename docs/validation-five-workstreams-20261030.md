# Validation des cinq chantiers — 30 octobre 2026

Code contrôlé : `d3de7a34fc1226870b5becf06135de46ee07983f`, depuis
`cec4c7c51fcbe19004ab4890c82232b90f2275a0`. La [spécification](five-workstreams-20261030.md)
reste distincte des résultats métier. Cette livraison n'active aucun domaine réseau, ne collecte
aucun nouveau document administratif et ne crée aucune annotation humaine.

## Résultats par catégorie

| Catégorie | État constaté |
| --- | --- |
| Mécanismes implémentés | Les cinq lots sont raccordés au CLI, à la pipeline et à la relecture privée. |
| Collecte réellement effectuée | Aucune collecte Internet. Rejeu local des 11 PDF caviardés existants ; formats Office/HTML/ZIP contrôlés par des fixtures fonctionnelles explicites. |
| Annotations humaines reçues | Zéro. Les formulaires produits restent vierges ; aucun consensus ni inventaire indépendant réel n'a été finalisé. |
| Qualité effectivement mesurée | Équivalence technique et coûts locaux mesurés. Aucun rappel global, précision métier, débit de production ou coût mensuel établi. Le gate métier reste en échec. |
| Configuration enregistrée | Options de lecture versionnées et service Compose `worker` ajoutés au dépôt. Aucune modification des réglages réseau cloud. Lecture adaptative optionnelle, worker Compose hors ligne par défaut. |
| Activation/publication | Livraison du code par Git. Aucune application déployée, aucun jeu approuvé ou publié, aucune nouvelle source activée. |

## Mécanismes raccordés

1. [Pilote qualité](pilote-qualite.md) : `pilot plan|prepare|status`, provenance externe obligatoire,
   groupes complets malgré les filtres, inventaire et sorties machine figés, double lecture vierge,
   partitions prospectives, effort déclaré et lacunes visibles. Les anciens manifestes PDF restent valides.
2. [Formats documentaires](formats-documentaires.md) : HTML, DOCX, XLSX et ZIP dans l'ingestion,
   l'extraction et la relecture privée. Originaux et rattachements d'archives conservés, localisations
   paragraphe/tableau/cellule/feuille/membre, formules incertaines, limites structurelles, mémoire POSIX
   et watchdogs. Les parties Office non lues ne deviennent pas silencieusement complètes.
3. [Tâches persistantes](taches-persistantes.md) : `tasks plan|enqueue|run|status`, entrées immuables,
   dépendances, historique, budgets, baux et fencing, calcul sur snapshot privé puis publication atomique.
   Les décisions IA durables sont référencées sans nouvelle soumission payante. Les clients historiques
   et les workers partagent une admission HTTP physique, une cadence durable et l'exclusion jusqu'à la
   fin du corps de réponse ; aucun verrou d'écriture applicatif n'est conservé pendant le réseau.
4. [Rapprochement en flux](rapprochement-flux-20261030.md) : proposition, décisions, conservation
   incrémentale et consommateurs raccordés à un index SQLite temporaire, avec chemin complet de référence.
   Diagnostics calculés hors requêtes web, datés et liés à une révision exacte ; états périmé/indisponible
   exposés. La correction antérieure de `Database.replace_links` n'est pas revendiquée à nouveau.
5. [Lecture adaptative](lecture-adaptative.md) : premier passage conservé, approfondissements sélectifs,
   budgets et clés de cache versionnés, provenance et pages utiles non lues, quantités contextualisées,
   abstention en cas de contradiction. Une valeur structurée conserve une preuve littérale localisée
   ou est retirée. Les sorties machine brutes et les références réservées sont préservées.

## Contrôles automatisés

- État initial restauré : **551 tests réussis, 1 ignoré** sur une archive isolée du commit de référence.
- État final : **643 tests réussis, 1 ignoré**, suite hors ligne complète en **106,81 secondes**.
  Le test ignoré dépend toujours des cas humains privés absents ; aucun échec n'a été désactivé.
- Format Ruff : **248 fichiers conformes** ; Ruff sans erreur ; mypy strict sur **157 modules** et les
  trois bancs `benchmark_capacity.py`, `compare_reading.py`, `benchmark_link_pipeline.py` sans erreur.
- `radar run` hors ligne termine sans échec ; base cloud principale vide. `radar eval` ne détecte aucune
  régression face à la référence disponible, mais le gate reste **failed** faute d'observations humaines
  indépendantes et d'effectifs suffisants. Les seuils restent inchangés. `quality-loop` produit un
  snapshot courant explicitement daté ; l'API ne lance pas son recalcul.

Les tests couvrent notamment crash/réservation, expiration, ancien worker, remplacement et retour
A→B→A des entrées sans remise à zéro des budgets, dépendance bloquée, suspension d'une source,
budgets après reprise, changements de fichiers malgré une date conservée et rollback atomique.
Les contrôles HTTP reproduisent une réponse historique encore ouverte pendant qu'un worker tente
un envoi, une suspension pendant l'attente, les redirections/reprises et les réservations conservées
après échec. Aucun de ces tests n'appelle un site administratif ni un modèle payant.

## Rejeux et mesures

Le chemin CLI réel a été exercé sur les **11 PDF caviardés / 7 dossiers** déjà livrés : 19 tâches
terminées, zéro travail à la seconde exécution, sorties machine identiques à `radar run`, partitions
présentes et traitements complets. Les références de classement ne sont pas copiées dans ces bases
jetables : les catégories y restent inconnues et les deux chemins produisent zéro projet. Ce contrôle
prouve l'équivalence technique ; le corpus de régression classé est contrôlé séparément par la suite.

Les [mesures brutes du rapprochement](measurements/streamed-link-pipeline-20261030.json) conservent
la référence et tous les essais : 1 000 dossiers synthétiques, **499 500 liens et 1 projet**, même SHA-256
avant/après. RSS de **459,09 Mio à environ 41,35 Mio**, soit environ **−91 %**. Trois vérifications finales
sur le schéma 21 donnent 41,344–41,355 Mio et 5,210–5,619 s, contre 4,526 s pour l'unique référence.
Le gain mémoire s'accompagne de calcul et de stockage temporaire supplémentaires ; il ne caractérise
pas un débit de production. Les listes de compatibilité et réponses finales complètes restent
proportionnelles à leur contenu.

La [comparaison de lecture](measurements/adaptive-reading-20261030.json) porte sur les mêmes 11 PDF / 77 pages :
lecture économique froide 2,216 s, tableaux complets 4,870 s / 77 pages, mode adaptatif 3,854 s / 46 pages.
Aucun champ extrait ni association littérale supplémentaire ou perdu dans cet essai. Six pages utiles
dépassent le budget adaptatif et restent signalées ; le comparateur sort donc avec le statut incomplet
attendu. Sans références humaines représentatives, aucune amélioration métier n'est déduite de ce résultat.

## Image et exploitation

Image finale : `sha256:943097a4db08fe42cf88950ba93557bb77aa55f474836650d698a2336fbb07fd`.
Construction avec vérification TLS et certificat de session monté temporairement, sans l'intégrer à l'image.

Un conteneur jetable a exécuté les contrôles avec UID **10001**, réseau **none**, racine en lecture seule,
**1 CPU**, **512 Mio**, **128 processus**, `/tmp` et `/data` en tmpfs de 64 Mio :

- schéma 21, 11 documents / 7 dossiers, 19 tâches terminées, seconde exécution sans travail ;
- DOCX construit avec contrôle de contenu lu avec une localisation sans page fictive ;
- valeur HTML conservée avec sa preuve de paragraphe, campagne privée créée sans annotation ;
- HTML et segments HTTP 200 via l'application, puis serveur privé réellement démarré sur loopback,
  santé et segments HTTP 200 avec authentification ;
- diagnostic courant, zéro annotation humaine et zéro appel payant.

Le rendu Compose conserve les limites séparées : web 512 Mio, jobs/worker 2 Gio ; modes de lecture
identiques, worker hors ligne par défaut. Le snapshot privé d'une opération historique copie actuellement
la base complète : prévoir l'espace disque et mesurer le volume réel avant exploitation. Le timer Python
POSIX et les watchdogs natifs ont des périmètres distincts des limites du conteneur.

## Standards

La revue indépendante a reproduit une valeur HTML sans preuve propre. Le correctif ajoute une citation
de segment littéral ou retire la valeur. Reproduction et régressions vérifiées ; **zéro constat restant**.
La revue de la coordination réseau commune ne relève pas de nouvelle violation matérielle.

## Spec

La revue indépendante a reproduit les défauts de fencing externe, de retour A→B→A, de contrôles DOCX
omis et de concurrence réseau. Les correctifs sont couverts par des régressions ; le dernier essai
vérifie que la réponse d'un client est fermée avant l'envoi du second. **Zéro constat restant** sur le
périmètre de code revu. Cette clôture ne remplace pas une campagne métier réelle.

## Opérations réelles restant bloquées

La politique réseau appliquée reste restreinte aux presets ; aucun domaine personnalisé administratif
n'est autorisé. Les opérations non effectuées sont : découverte et qualification de nouvelles pages,
rattrapage de périodes, collecte des dossiers et annexes, qualification de territoires supplémentaires
et recherche publique réelle. Les destinations déjà configurées comprennent notamment
`www.hauts-de-france.developpement-durable.gouv.fr`, `www.mrae.developpement-durable.gouv.fr`,
les cinq préfectures des Hauts-de-France et `html.duckduckgo.com` ; aucune autorisation n'a été contournée.

Il reste également à constituer un inventaire indépendant réel, recevoir deux lectures de véritables
lecteurs et réserver une population prospective réellement nouvelle. Les 21 labels et 21 fiches publics
restent non vérifiés. Aucun rappel global, précision métier, coût mensuel ou capacité de production
n'est établi par cette livraison.

Commits d'implémentation : `c1ae16e` (spécification), `04dab54` (intégration), `1678e1e`
(correctifs de revue), `d3de7a3` (admission HTTP commune). Le présent rapport complète leur livraison.

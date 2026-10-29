# Validation des trois lots documentaires — 29 octobre 2026

Périmètre : modifications depuis `2f4b8e9`, critères dans [documentary-quality-plan.md](documentary-quality-plan.md).
La livraison comporte des mécanismes vérifiés hors ligne. Aucun nouveau site public n'a été collecté, aucun modèle
payant appelé, aucune infrastructure déployée et aucune référence déclarée humainement vérifiée.

## Résultats observés

- Suite complète sur le code `1bcbf53` : **427 tests réussis, 1 ignoré**, 135,22 secondes. Deux avertissements
  de dépréciation Python concernent les tests existants de verrou après `fork`, sans échec.
- Formatage et Ruff : 198 fichiers vérifiés ; mypy strict : 129 modules sans erreur.
- Couverture : rattrapage historique reprenable, pages partielles non validées, blocages et erreurs persistants,
  absence de configuration visible, fraîcheur de l'achèvement préservée lors d'un simple contrôle de tête de liste.
- Dossiers : PDF avec paramètres, téléchargements indirects, annexes neutres, cycles, budgets, identités stables,
  liens externes signalés sans téléchargement, erreurs de parcours et provenance ; transport HTTP simulé.
- Préfectures : cadence globale entre départements et arrêt sur blocage ; aucun test réel des sites ajouté.
- Extraction : unités, contexte de projet, contradictions et citations à la page ; aucune attribution d'une citation
  concaténant artificiellement deux documents. Une déclaration contradictoire impose l'abstention.
- Campagnes : fiches vierges, strates non représentées visibles, double lecture partielle, inconnus distincts des
  absences, hashes réels contrôlés même pour les références d'absence, métadonnées et groupes figés, sorties
  corrigées exclues, erreurs individuelles du test masquées et refus des comparaisons devenues dépendantes.
- Inventaire externe : les dossiers jamais observés et ceux listés sans téléchargement restent au dénominateur ;
  rappel documentaire séparé, regroupement des dossiers d'un projet et absence de score pour les brouillons.

L'exemple construit de document long contient 152 932 caractères. La capacité tardive « 60 MWh » est absente
des 20 000 premiers caractères et présente dans les 3 983 caractères sélectionnés. Le sélecteur signale
148 999 caractères omis et 25 indices couverts sur 305. Ce résultat vérifie la recherche de passages ; il ne mesure
ni le rappel d'extraction réel ni les économies d'un modèle.

## Pilote privé et mesure métier

Le corpus anonymisé existant a été rejoué dans `data/pilots/documentary-20261029/` : 7 dossiers, 11 PDF et
3 projets de régression. La campagne `campaign-ready/` contient 7 groupes (4 développement, 2 calibration,
1 test), dont 2 groupes proposés pour une seconde lecture indépendante. Elle garde `independence: unestablished`.
Elle couvre uniquement la population DREAL livrée, sans scans ni préfectures ni avis MRAe représentatifs.

`campaign prepare`, `snapshot` et `validate` fonctionnent sur ce pilote. Il reste 0 référence humaine vérifiée,
77 champs et 11 pertinences documentaires en attente. `campaign compare --strict` retourne correctement le code 2
avec `unmeasured`. Les catégories issues du rejeu avec labels de régression ne comptent pas comme prédictions
indépendantes. Les fichiers du pilote restent privés et ignorés par Git.

`RADAR_OFFLINE=1 radar run` et `radar eval` ont également été exécutés sur la base principale vide : aucun dossier
en échec ni régression détectée ; le contrôle d'approbation refuse toute certification faute de références vérifiées.

## Revue Standards

Quatre défauts ont été confirmés par des reproductions, puis corrigés : groupes modifiés après préparation ;
acceptation des absences sans lecture des fichiers ; contradiction ignorée avant fusion ; comparaison d'anciens
snapshots après découverte d'un rapprochement. Les relectures ont confirmé la résolution des quatre points.

## Revue Spec

Deux défauts ont été confirmés et corrigés : rattachement d'une annexe d'avis MRAe sans numéro au dossier précédent ;
acceptation de métadonnées modifiées et sorties périmées lors d'un snapshot. Les tests indépendants de relecture
confirment les corrections. Aucun autre écart concret restant n'a été signalé sur ces points.

## Limites pratiques

L'environnement autorise actuellement les domaines des gestionnaires de paquets et GitHub, pas les sites
administratifs de collecte. Leur qualification et l'exécution d'une campagne de collecte réelle nécessitent donc
une configuration réseau adaptée. La suite utilise des sources simulées. TestClient se bloque sous le confinement
de l'exécuteur ; le même test ciblé passe immédiatement hors confinement. La suite complète est donc exécutée
hors confinement, tout en conservant l'exclusion des tests réseau.

Les parseurs restent spécialisés dans les documents administratifs PDF et les pages qui les relient ; les pièces
externes non autorisées, formats non reconnus, tableaux complexes et scénarios ambigus restent explicitement
incomplets ou à qualifier. Les années et régions non configurées ne sont pas inventées. La sélection de passages
reste optionnelle avant comparaison sur des données réellement vérifiées.

# Mesurer les dossiers et pièces manqués

Une campagne sur les seuls dossiers déjà collectés ne peut pas compter les projets jamais découverts. `radar campaign discovery --reference data/annotation/inventaire.jsonl` compare donc la collecte à un inventaire manuel externe, sans accès réseau ni appel modèle.

Chaque ligne décrit un dossier d'un groupe de projet et ses pièces pertinentes attendues :

```json
{"project_group":"projet-a","source":"dreal","region":"hauts-de-france","dossier_url":"https://example.org/dossier","document_urls":["https://example.org/annexe.pdf"],"relevant":true,"basis":"manual_inventory","reviewer":"","reviewed_on":"","verified":false}
```

L'exemple est fictif et non vérifié. Une personne renseigne son identifiant et la date de relecture ISO avant de déclarer la référence vérifiée. Sources prises en charge : `dreal`, `mrae`, `prefectures`. Les URL doivent correspondre aux pages et téléchargements effectivement identifiés ; les fragments sont ignorés, les autres paramètres sont conservés. Les alias et changements d'URL nécessitent une vérification de l'inventaire.

Le rapport distingue les dossiers pertinents téléchargés, listés sans téléchargement et jamais observés sous cette URL. Il mesure séparément le rappel des pièces attendues. Plusieurs dossiers d'un même projet restent un seul groupe indépendant ; un groupe partiellement retrouvé reste incomplet pour ce score conservateur. Les cas connus non pertinents retrouvés sont comptés, sans extrapoler une précision globale à partir de cet échantillon.

Constituer l'inventaire depuis des sources indépendantes de la sortie du collecteur, documenter ses critères d'inclusion et ses périodes, puis le figer avant l'essai. Une liste composée seulement des dossiers déjà trouvés surestime le rappel. Le logiciel contrôle le format, l'attribution de la vérification et les doublons ; il ne peut pas prouver qu'une personne a réellement relu les sources. Un inventaire vide ou non vérifié reste « non mesuré ». Les scores décrivent exclusivement cet inventaire, jamais l'exhaustivité d'Internet.

Conserver ce fichier privé dans `data/annotation/` et le sauvegarder avec les autres références humaines.

# Récupération des pièces d'un dossier

Les collecteurs documentaires utilisent maintenant un parcours commun des pièces administratives d'un dossier sélectionné. Il reconnaît les PDF avec paramètres, fragments et extensions en majuscules, les téléchargements administratifs sans extension et les liens nommés « Annexe », « Pièce », « Dossier », « Étude » ou « Document ». Un téléchargement doit contenir un PDF valide avant ingestion ; une page HTML intermédiaire peut être suivie.

Le parcours reste sur l'origine du dossier et ne suit pas les liens de navigation générale identifiés. Il n'ouvre pas les archives ou l'ensemble d'une page annuelle au nom d'un dossier. Les fichiers externes, les archives ZIP et les documents bureautiques ne sont pas extraits par ce mécanisme. Une récupération complète signifie que les liens reconnus dans ce périmètre ont été parcourus ; elle ne garantit pas que toutes les pièces existantes sont publiées ou reconnues.

Les liens de pièces reconnus sur une autre origine restent enregistrés avec l'état `outside_scope`, sans requête réseau. Leur présence laisse le dossier partiel et demande une vérification ou une configuration explicite de la source concernée ; elle n'est pas assimilée à une absence de document.

Les liens externes et les cycles consomment le même budget global de relations découvertes. Une erreur ou un blocage conserve son type, son motif et l'état du lien concerné dans le dernier parcours ; l'opérateur peut donc distinguer une pièce non reconnue, un refus du site et une limite de parcours.

## Budgets et reprise

Les options `collect` et `update` acceptent :

```bash
uv run radar collect dreal --max-items 2 --max-pages 1 --backfill \
  --dossier-pages 8 --dossier-documents 24 --dossier-depth 2
```

Cette commande réalise une collecte réseau ; elle doit être lancée explicitement. Les budgets de pages du collecteur portent sur les listes DREAL ou la navigation d'une préfecture. Les budgets de dossier s'ajoutent **pour chaque dossier sélectionné** : par défaut 8 pages HTML, 24 PDF et deux niveaux de liens, avec 500 liens maximum et 64 Mio de réponses cumulées par dossier. Chaque réponse reste soumise aux limites HTTP. Le volume total dépend donc du nombre de dossiers sélectionnés ; `--max-items` permet de limiter les sélections DREAL, et limite le total de documents MRAe. Le parcours préfectoral reste borné par le nombre de pages de navigation et les budgets individuels des dossiers.

Un budget atteint laisse le dossier partiel et empêche la liste DREAL ou MRAe d'être déclarée complète. Une nouvelle collecte revisite le dossier ; les fichiers sont dédupliqués par hash. Le rattrapage DREAL ne valide sa page qu'après le traitement de tous ses dossiers. Pour revisiter une archive DREAL déjà terminée, utiliser `--backfill --restart`.

Les préfectures restent optionnelles et leur couverture reste partielle, car la navigation est volontairement restreinte. Les requêtes de toutes les préfectures sont séquentielles et espacées globalement d'au moins cinq secondes, y compris lors du passage à un autre département. Le premier blocage met fin à toute la collecte préfectorale.

## Identité et traçabilité

Un dossier DREAL fixe son identité avant de parcourir ses pièces : numéro du titre, numéro unique des fichiers directement liés, ou identifiant stable dérivé de l'URL de sa page. Ses annexes neutres gardent cette identité ; une nouvelle dénomination de fichier ne change pas l'identité déjà enregistrée. Les dossiers MRAe conservent leur référence ; seule une annexe explicitement adjacente au même avis peut hériter de cette référence. Deux projets ayant un titre identique ne sont pas fusionnés sur ce seul indice.

Les tables privées `dossier_scans` et `dossier_edges` conservent l'état du dernier parcours et les relations parent → pièce, avec le hash des PDF téléchargés. Les échecs et limites restent visibles. Les rejets MRAe et préfectoraux sont enregistrés dans `listing_items` et soumis au même tirage stable de contrôle aléatoire que DREAL, avant les contrôles ciblés des cas proches. Une sélection de contrôle ne devient jamais une validation humaine.

`radar coverage` affiche également les comptes de dossiers par état et les vingt premiers dossiers incomplets de la région demandée, avec une indication explicite lorsqu'il y en a davantage. Les tables privées permettent d'inspecter les graphes détaillés sans exposer ces documents dans l'API publique.

Les tests utilisent des mises en page synthétiques et des PDF générés. Aucun nouveau site public ni service IA payant n'a été appelé pour vérifier ce lot.

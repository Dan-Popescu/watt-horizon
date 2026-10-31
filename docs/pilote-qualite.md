# Préparer et suivre un pilote documentaire

`radar pilot plan|prepare|status` compose les [campagnes aveugles](campagnes-annotation.md),
l’[inventaire indépendant](inventaire-reference-decouverte.md) et les comparaisons existantes.
Ces commandes sont locales, sans collecte ni appel de modèle. Les pièces, références et identités
des lecteurs restent dans le stockage privé sous `data/`.

## Déclarer le périmètre et l’origine de l’inventaire

Créer un JSON privé avec `inventory`, chemin relatif au JSON, et une `provenance` explicite :

```json
{
  "inventory": "inventaire.jsonl",
  "provenance": {
    "basis": "external_manual_inventory",
    "assembled_by": "identifiant-prive-du-responsable",
    "assembled_on": "2026-10-30",
    "references": ["https://example.org/index-a-remplacer"],
    "inclusion_protocol": "Décrire les sources, périodes et critères vérifiés indépendamment.",
    "independence_rationale": "Décrire comment éviter une liste issue des résultats du collecteur."
  },
  "sources": ["dreal_case_by_case", "mrae_opinion", "prefecture"],
  "regions": ["hauts-de-france"],
  "observed_from": "2025-01-01",
  "observed_through": "2026-10-30",
  "formats": ["pdf", "html", "docx", "xlsx", "zip"],
  "roles": ["form", "decision", "other"],
  "required_reads": ["text", "ocr", "unread"],
  "max_groups": 30,
  "max_documents": 300,
  "max_bytes": 268435456,
  "prospective_after": "2026-10-30"
}
```

L’URL `example.org` est un emplacement fictif à remplacer par une page réellement observée ;
ce JSON n’atteste aucune collecte. L’inventaire utilise les lignes décrites dans la documentation
existante : provenance, protocole et références doivent être fournis même si ses lignes restent
non vérifiées. Seules des lignes vérifiées par une personne alimentent la mesure de découverte.
Le logiciel vérifie ces déclarations et leur intégrité ; il ne peut pas prouver leur vérité.

La [revue IA du corpus initial du 31 octobre](revue-ia-pilote-20261031.md) conserve trois lectures
séparées et un contrôle du sens des preuves, sans annotation humaine ni score métier fabriqué.
Le [protocole d'extension](protocole-revue-ia-20261031.md) distingue les fichiers, dossiers et groupes,
ainsi que les partitions réservées et les limites statistiques de la cible 500–1 000 fichiers.

Les filtres de date portent sur la **date de téléchargement enregistrée pour chaque pièce dans cette base**,
pas sur une année juridique supposée. Les formats reprennent le type détecté à l’ingestion ;
les anciennes pièces sans type utilisent leur extension. La lecture et sa validation contrôlent
séparément les octets. Les filtres d’une même dimension
sont alternatifs et les dimensions se combinent. Un dossier sélectionné apporte toujours tous
les membres de son groupe, y compris ceux hors périmètre. Les plafonds comptent les groupes et
les documents complets ; un groupe trop grand est exclu avec un motif, jamais tronqué.

```bash
RADAR_OFFLINE=1 uv run radar pilot plan --spec data/pilots/perimetre.json
RADAR_OFFLINE=1 uv run radar pilot prepare --spec data/pilots/perimetre.json --dir data/pilots/pilote-20261030
RADAR_OFFLINE=1 uv run radar pilot status data/pilots/pilote-20261030
```

Le plan affiche les dimensions représentées et manquantes, les groupes exclus par budget,
les membres inclus hors filtre, les effectifs et les partitions. Il ne modifie pas la base et
ne télécharge rien. Un inventaire vide, une base vide ou des modalités absentes restent visibles.
Les titres courts et les modèles de document participent à la sélection diversifiée ; ils ne
garantissent pas la présence de cas difficiles ou de projets similaires. Examiner ces lacunes
avant de retenir le corpus.

## Figer le pilote et distribuer la double lecture

`prepare` vérifie les empreintes des octets originaux, exige des sorties de traitement complètes
et conserve les contrôles existants d’appartenance aux groupes. Il prépare dans un répertoire
temporaire privé, puis publie l’ensemble par renommage :

- `manifest.json` et les deux formulaires humains **vierges**, avec double lecture de tous les groupes ;
- `initial.json`, les sorties machine brutes avant correction, et leurs versions ;
- `inventory.jsonl`, copie exacte de l’inventaire externe, liée par SHA-256 ;
- `pilot.json`, périmètre, provenance, budgets, plan et empreinte du manifeste ;
- `assignments.jsonl`, slots `a` et `b` sans lecteur attribué ; `effort.jsonl`, vide.

Une destination existante est refusée. Un échec de préparation ne laisse pas de campagne
partielle visible. Une modification du périmètre ou des groupes exige une nouvelle campagne.
Le seuil prospectif reprend les garanties des partitions : la date de première observation
du groupe doit dépasser le seuil pour être test ; un membre historique garde tout le groupe
hors du nouveau test. Des partitions préexistantes ne sont jamais réaffectées silencieusement.
La déclaration temporelle ne suffit pas à prouver l’absence de contamination.

Attribuer les lecteurs dans `assignments.jsonl` et distribuer séparément les deux paquets.
Les lecteurs d’un dossier doivent être distincts. Le champ `assigned` compte les slots
attribués ; `annotated` compte les lignes réellement déclarées vérifiées et validées. Le logiciel
ne remplit aucun formulaire à leur place. La saisie, la vérification, la comparaison et l’import
restent ceux des commandes `campaign`. Seul `campaign apply` peut importer un consensus de
développement ; calibration et test restent réservés.

## Lire les résultats sans masquer les inconnus

`status` distingue les dossiers attendus, découverts, téléchargés, lisibles et traités, ainsi que
les pièces attendues à chaque étape. Une pièce téléchargée ne prouve pas que le dossier est
complet : examiner aussi les comptes de pièces et les motifs des scans incomplets. La lisibilité
consomme au maximum les budgets du pilote ; un dépassement reste un blocage explicite.

Le résultat reprend le rappel sur l’inventaire vérifié, les erreurs de valeurs de la sortie
initiale contre le consensus, les désaccords, les champs en attente et les lacunes de représentation.
Les erreurs détaillées de valeurs restent limitées au développement. Les faux regroupements et
séparations sont comptés sur les paires de dossiers que l’inventaire externe vérifié permet de
rattacher aux projets actuels ; sans paires comparables, la mesure reste indisponible. Les décisions
humaines déjà appliquées aux projets font partie de cet état courant, distinct de la sortie
initiale figée. Il ne s’agit pas d’une précision globale extrapolée à Internet.

Les formules sans résultat vérifié, les parties Office omises, les segments incertains, les
erreurs OCR ou de tableau et les pages utiles non lues empêchent d’annoncer une lecture complète ;
leurs motifs restent visibles même si une partie du texte est exploitable.

Si les entrées courantes divergent du manifeste, `status` affiche `stale` et son motif ; aucune
qualité actuelle n’est déclarée mesurée. Le snapshot initial reste une preuve de l’ancien état.
Un corpus non annoté reste `unmeasured` pour les valeurs ; un inventaire non vérifié ne produit
aucun rappel. Aucun compteur nul ne devient une note parfaite.

L’effort n’est pas déduit du nombre de fichiers. Chaque session déclarée dans `effort.jsonl`
doit donner `campaign_fingerprint`, un `session_id` unique, un `reviewer`, une `phase`
(`inventory`, `review` ou `adjudication`), des `seconds` finis positifs ou nuls et, facultativement,
un `filing_key` de la campagne. Les durées sont agrégées sans exposer les identités. L’absence de
session reste `unmeasured`, avec durée totale inconnue. Un fichier différent se passe par `--effort`.

## État réel au 30 octobre 2026

Le paragraphe historique suivant décrit la première session. La [reprise avec collecte réelle](resultats-pilote-cloud-20261030.md)
donne l'état de la nouvelle configuration et du corpus ; elle n'apporte aucune annotation humaine indépendante.

L’environnement actif n’autorise que GitHub et les gestionnaires de paquets. Les destinations
administratives et le moteur public restent absents de la politique appliquée. Aucune collecte
réelle ni qualification de territoire n’a donc été exécutée pour ce pilote. Aucun inventaire
indépendant nouveau et aucune double annotation humaine n’ont été reçus. Les essais automatisés
vérifient les mécanismes sur des données de protocole construites et ne fournissent aucune mesure
de qualité métier. Restent à effectuer : activer les destinations autorisées par le workflow
cloud, observer les pages réelles, constituer l’inventaire indépendant, collecter un lot borné,
le traiter, préparer sa campagne, recevoir les deux lectures et examiner les mesures.

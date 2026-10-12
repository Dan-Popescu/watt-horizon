# Kit de démo

Données du prototype au 12/10/2026, Hauts-de-France.

| Fichier | Usage |
| --- | --- |
| [presentation.html](presentation.html) | Une page à montrer ou à imprimer en PDF (Ctrl+P). |
| [alerte-exemple.html](alerte-exemple.html) | L'e-mail hebdomadaire qu'un client recevrait, généré par `radar digest`. |
| La carte | Se lance avec `uv run radar web`, puis s'ouvre sur http://127.0.0.1:8765. |

## Avant la démo

1. Lancer la carte (`uv run radar web`) et vérifier que le fond de carte s'affiche.
2. Ouvrir l'alerte d'exemple dans un autre onglet.
3. Pour la régénérer :

```bash
uv run radar digest --since 2026-01-01 --map-url http://127.0.0.1:8765/ --out demo/alerte-exemple.html
```

## Déroulé (5 minutes)

1. **Le problème** (30 s)
   « Pour brancher une batterie géante ou un data center, il faut une place sur un poste électrique. Ces places sont rares. Savoir qui vise quel poste, et avant les autres, vaut de l'argent. »

2. **La carte** (1 min)
   Montrer les 10 projets, colorés par type et dimensionnés selon la puissance.
   « 457 MW déclarés. Aucun de ces projets n'est encore dans le registre officiel. »

3. **Un cas concret** (1 min 30)
   Cliquer sur Aubenton, puis sur Bonneuil-les-Eaux.
   « Le même développeur, APAL MW, se place près de deux postes, dont "Margot", qui est encore en construction. Chaque chiffre a sa citation et le lien vers le PDF officiel. »
   Montrer ensuite le contrôle GPS en alerte : « Le dossier se trompe de commune, et l'outil le voit. »

4. **L'alerte** (1 min)
   Montrer l'e-mail : « Chaque lundi, le client reçoit les nouveaux dossiers de ses zones. Un clic, et il est sur la fiche. »

5. **Les agents IA et les exports** (30 s)
   Montrer l'onglet Exports : « Leur Excel, leur logiciel de cartographie, ou directement leur agent IA. »

6. **La question** (30 s)
   « Qu'est-ce que ça vous éviterait de rater ? Combien ça vaut pour vous ? »

## Questions à poser aux clients potentiels

- Comment suivez-vous aujourd'hui les projets concurrents près de vos postes ? Combien de temps y passez-vous ?
- Quel projet avez-vous découvert trop tard ? Qu'est-ce que ça vous a coûté ?
- Quelles régions et quels types de projets vous intéressent ?
- Le format qui vous sert le plus : la carte, l'alerte, Excel ou l'accès pour vos outils ?
- Quelle information manque ici et vous ferait signer ?
- Quel budget serait évident pour vous ? À partir de quel prix faudrait-il réfléchir ?
- Accepteriez-vous un pilote gratuit d'un mois, en échange de vos retours ?

Noter les réponses telles quelles, sans les interpréter, avec le nom de l'entreprise et la date.

## Objections probables

| Objection | Réponse |
| --- | --- |
| « Ces données sont publiques. » | Oui, mais éparpillées dans des centaines de PDF, parfois scannés. Le radar les lit chaque jour et cite sa source à chaque fois. |
| « On a déjà une veille. » | Laquelle, et avec quel délai ? Le registre officiel a environ un an de retard. |
| « Et si l'IA se trompe ? » | Chaque valeur est accompagnée de la citation exacte et du lien vers le document. Ce qui n'est pas encore vérifié est marqué « à vérifier ». |
| « Seulement les Hauts-de-France ? » | C'est le prototype. Ajouter une région demande surtout de la configuration. La couverture nationale est la prochaine étape si l'intérêt se confirme. |
| « Et les données personnelles ? » | Les personnes physiques sont masquées avant tout stockage : seules les sociétés apparaissent. |

## À ne pas dire

- Ne pas présenter comme des faits les chiffres de file d'attente (15 GW / 18 GW) ni le prix : ce sont des hypothèses à sourcer ou à tester.
- Ne pas promettre la couverture nationale ni de date de mise en ligne.

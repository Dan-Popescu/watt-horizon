# Corpus de test (PDF réels caviardés)

Documents publics de la DREAL Hauts-de-France (examens au cas par cas), réutilisés sous Licence Ouverte (Etalab).
Caviardés avant d'entrer dans le dépôt : champs de personnes vidés, noms de personnes, civilités, courriels,
téléphones et blocs de signature effacés de la couche texte, toutes les images retirées ; documents scannés exclus.

- `manifest.json` : dossiers, documents, catégorie de référence, communes utiles aux contrôles.
- `expected.json` : résultat attendu de toute la chaîne, vérifié par `tests/test_corpus.py`.

Mettre à jour après un changement voulu : `uv run radar corpus snapshot`, puis relire le diff de `expected.json`.

# Vocal LAN — consignes pour tout assistant IA

**Avant d'agir, lis ce fichier, `LISEZ-MOI.txt`, puis `HANDOFF.md` (état du travail).**

Application Windows en Python de discussion vocale sur réseau local, sans serveur.
Dépôt : https://github.com/Poustie/Vocal-lan (branche `main` ; Yann contribue par PR depuis `develop`).

## Règles

- Répondre en français, simplement.
- **Le dépôt GitHub est public** : n'y mettre ni donnée personnelle, ni adresse e-mail, ni réglage local.
- Ne jamais versionner `vocal_config.json`, `settings.json`, `vocal.log`, `build/` ou `dist/`.
- Aucun push ni publication de Release sans accord explicite.
- Tout le monde doit utiliser la même version : signaler tout changement qui casse la compatibilité réseau.
- Les tests vont dans `tests/`, jamais à la racine. Ils ne doivent jamais écrire dans le vrai `%APPDATA%\Vocal LAN`.
- Callback audio (`engine.py`, `dsp.py`, `room_echo.py`, `denoise.py`) : budget de 5 ms par trame, éviter d'allouer des tableaux (utiliser `out=`).

## Commandes

- Lancer : `lancer.bat`
- Tests : `python -m pytest tests -q` (environ 3 minutes)
- Construire l'exe : `build.bat` (résultat : `dist\Vocal LAN.zip`)

## Publier une version

1. Changer `VERSION` dans `settings.py` (et le test qui la vérifie dans `tests/test_engine.py`).
2. Lancer les tests, puis `build.bat`.
3. Après accord : pousser, puis créer une Release GitHub `vX.Y.Z` avec `Vocal LAN.zip`.
   Lien à donner aux utilisateurs : https://github.com/Poustie/Vocal-lan/releases/latest

Changement d'outil : mettre à jour `HANDOFF.md`, puis faire un commit.

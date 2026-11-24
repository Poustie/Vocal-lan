# Passation — Vocal LAN

Dernière mise à jour : 24/11/2026, par Claude Code.

## Où on en est

- Version **2.2.0** : ajoute « Dans ma pièce » (contribution de Yann, PR #1, module `room_echo.py`). Un bouton sur la carte d'un ami assis dans la même pièce retire ma voix de son flux, sans délai ajouté. Protocole réseau inchangé : compatible avec la 2.1 et la 2.0.
- Après la PR #1 :
  - les tests de Yann, posés par erreur à la racine, sont dans `tests/` ;
  - `room_echo.py` n'alloue presque plus rien dans le callback audio (environ 10 Ko par trame au lieu de 220 Ko pour 3 amis), avec un résultat identique au bit près ;
  - le test de budget du callback refait jusqu'à 3 mesures quand la machine est chargée.
- L'exe de `dist\` est encore la 2.1 : le reconstruire avec `build.bat` avant de le distribuer.
- L'exe n'est pas publié sur GitHub.

## Points d'attention

- Tests : toujours dans `tests/`. Lancer `python -m pytest tests -q` (environ 3 minutes).
- Aucune vérification automatique des PR sur GitHub : lancer les tests avant de fusionner.
- « Dans ma pièce » n'a été testé qu'avec un écho simulé, jamais avec deux PC dans une vraie pièce.

## Décisions à prendre

- **Licence** : aucune pour l'instant, donc personne n'a le droit de réutiliser le code. Une licence MIT a été proposée. RNNoise garde sa licence BSD.
- **Release GitHub** : publier `Vocal LAN.zip` (environ 47 Mo) pour un téléchargement en un clic, après avoir reconstruit l'exe.
- **Vérification automatique** : ajouter GitHub Actions pour lancer les tests sur chaque PR.

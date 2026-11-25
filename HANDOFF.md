# Passation — Vocal LAN

Dernière mise à jour : 25/11/2026, par Claude Code.

## Où on en est

- Version **2.2.0**, publiée en Release GitHub `v2.2.0` (`Vocal LAN.zip`, 47 Mo) : https://github.com/Poustie/Vocal-lan/releases/latest
- Nouveauté de la 2.2.0 : « Dans ma pièce » (contribution de Yann, PR #1, module `room_echo.py`). Un bouton sur la carte d'un ami assis dans la même pièce retire ma voix de son flux, sans délai ajouté. Protocole réseau inchangé : compatible avec la 2.1 et la 2.0.
- Corrections après la PR #1 :
  - les tests de Yann, posés par erreur à la racine, sont dans `tests/` ;
  - `room_echo.py` n'alloue presque plus rien dans le callback audio (environ 10 Ko par trame au lieu de 220 Ko pour 3 amis), avec un résultat identique au bit près ;
  - le test de budget du callback refait jusqu'à 3 mesures quand la machine est chargée.
- 250 tests passent sur `main`.

## Points d'attention

- Aucune vérification automatique des PR sur GitHub : lancer les tests avant de fusionner.
- « Dans ma pièce » n'a été testé qu'avec un écho simulé, jamais avec deux PC dans une vraie pièce.
- Le 25/11/2026, `vocal.py` et `lancer.bat` ont été écrasés sur le disque par une très ancienne version, d'origine inconnue. Ils ont été remis depuis git. Si ça se reproduit : `git checkout -- vocal.py lancer.bat`.
- `gh` n'est pas installé : la Release v2.2.0 a été créée à la main sur le site GitHub.

## Décisions à prendre

- **Licence** : aucune pour l'instant, donc personne n'a le droit de réutiliser le code. Une licence MIT a été proposée. RNNoise garde sa licence BSD.
- **Vérification automatique** : ajouter GitHub Actions pour lancer les tests sur chaque PR.

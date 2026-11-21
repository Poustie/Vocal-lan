"""Icône près de l'horloge, la « zone de notification » de Windows (pystray).

L'icône vit dans son propre thread. Elle ne touche jamais à Tk : les clics du menu
posent des commandes dans ``self.commands`` (queue.SimpleQueue) que l'interface vide
dans son tick (« open », « mic », « deaf », « denoise », « quit »).
"""
from __future__ import annotations

import logging
import queue

log = logging.getLogger("vocal.tray")

# Couleurs d'état : vert = connecté et micro ouvert, rouge = micro ou son coupé,
# gris = seul ou hors ligne.
STATE_COLORS = {
    "green": (46, 184, 114),
    "red": (226, 74, 70),
    "grey": (112, 120, 140),
}

try:  # pystray peut manquer ou ne pas avoir de zone de notification (session sans bureau)
    import pystray
    from pystray import Menu, MenuItem
except Exception as exc:  # noqa: BLE001
    pystray = None
    log.warning("pystray indisponible : %s", exc)


def _image(state: str):
    """Image 64x64 de l'état (même motif que l'icône de l'appli, couleur unie)."""
    from PIL import Image, ImageDraw
    size, ss = 64, 4
    s = size * ss
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    col = STATE_COLORS.get(state, STATE_COLORS["grey"])
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, s - 1, s - 1), radius=int(s * 0.23), fill=col + (255,))
    bw, gap = s * 0.10, s * 0.065
    x = (s - (5 * bw + 4 * gap)) / 2
    for h in (0.30, 0.55, 0.85, 0.55, 0.30):
        bh = s * 0.58 * h
        y0 = (s - bh) / 2
        d.rounded_rectangle((x, y0, x + bw, y0 + bh), radius=bw / 2, fill=(255, 255, 255, 255))
        x += bw + gap
    return img.resize((size, size), Image.LANCZOS)


class Tray:
    """Icône de la zone de notification. ``available`` est faux si pystray ne marche pas."""

    def __init__(self):
        self.commands: queue.SimpleQueue = queue.SimpleQueue()
        self.available = False
        self._icon = None
        self._state = "grey"
        self._title = "Vocal LAN"
        self._mic_on = True
        self._sound_on = True
        self._denoise_on: bool | None = None       # None : le moteur n'a pas de suppression du bruit (menu masqué)
        self._images: dict = {}

    # --- cycle de vie -----------------------------------------------------
    def start(self) -> bool:
        if pystray is None:
            return False
        try:
            self._images = {k: _image(k) for k in STATE_COLORS}
            menu = Menu(
                MenuItem("Ouvrir Vocal LAN", lambda *_: self.commands.put("open"), default=True),
                Menu.SEPARATOR,
                MenuItem(lambda _i: "Couper le micro" if self._mic_on else "Réactiver le micro",
                         lambda *_: self.commands.put("mic")),
                MenuItem(lambda _i: "Couper le casque" if self._sound_on else "Réactiver le casque",
                         lambda *_: self.commands.put("deaf")),
                MenuItem("Bruit réduit", lambda *_: self.commands.put("denoise"),
                         checked=lambda _i: bool(self._denoise_on),
                         visible=lambda _i: self._denoise_on is not None),
                Menu.SEPARATOR,
                MenuItem("Quitter", lambda *_: self.commands.put("quit")),
            )
            self._icon = pystray.Icon("vocal-lan", self._images[self._state], self._title, menu)
            self._icon.run_detached()
            self.available = True
        except Exception as exc:  # noqa: BLE001
            log.warning("Zone de notification indisponible : %s", exc)
            self._icon = None
            self.available = False
        return self.available

    def stop(self) -> None:
        icon, self._icon = self._icon, None
        self.available = False
        if icon is not None:
            try:
                icon.stop()
            except Exception:  # noqa: BLE001
                pass

    # --- mises à jour (appelées depuis le thread Tk) ---------------------------
    def update(self, state: str, title: str, mic_on: bool, sound_on: bool, denoise: bool | None = None) -> None:
        self._mic_on, self._sound_on, self._denoise_on = mic_on, sound_on, denoise
        if self._icon is None:
            self._state, self._title = state, title
            return
        try:
            if state != self._state and state in self._images:
                self._icon.icon = self._images[state]
            if title != self._title:
                self._icon.title = title[:127]          # limite Windows
            if (mic_on, sound_on, denoise) != (getattr(self, "_menu_sig", None)):
                self._menu_sig = (mic_on, sound_on, denoise)
                self._icon.update_menu()
        except Exception as exc:  # noqa: BLE001
            log.debug("tray.update : %s", exc)
        self._state, self._title = state, title

    def notify(self, message: str, title: str = "Vocal LAN") -> None:
        if self._icon is None:
            return
        try:
            self._icon.notify(message, title)
        except Exception as exc:  # noqa: BLE001
            log.debug("tray.notify : %s", exc)

"""Raccourcis globaux (push-to-talk, couper le micro, couper le son) par interrogation de GetAsyncKeyState.

Un thread dédié interroge l'état des touches toutes les 5 ms et appelle directement le moteur
(sans passer par Tk). Pas de hook : rien n'est intercepté, le jeu reçoit aussi la touche.
Limite : un jeu lancé en administrateur peut masquer ses touches à un programme normal.
"""
from __future__ import annotations

import ctypes
import logging
import sys
import threading
import time

log = logging.getLogger("vocal.hotkeys")
POLL_S = 0.005
CAPTURE_TIMEOUT_S = 16.0   # un peu plus que le délai de l'interface (15 s) : elle abandonne la première

# nom -> code de touche virtuelle Windows
_VK: dict[str, int] = {
    "mouse_middle": 0x04, "mouse4": 0x05, "mouse5": 0x06,
    "backspace": 0x08, "tab": 0x09, "enter": 0x0D, "pause": 0x13, "capslock": 0x14, "space": 0x20,
    "pageup": 0x21, "pagedown": 0x22, "end": 0x23, "home": 0x24,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28, "insert": 0x2D, "delete": 0x2E,
    "shift_left": 0xA0, "shift_right": 0xA1, "ctrl_left": 0xA2, "ctrl_right": 0xA3,
    "alt_left": 0xA4, "alt_right": 0xA5,
    "semicolon": 0xBA, "equals": 0xBB, "comma": 0xBC, "minus": 0xBD, "period": 0xBE, "slash": 0xBF,
    "backquote": 0xC0, "bracket_left": 0xDB, "backslash": 0xDC, "bracket_right": 0xDD, "quote": 0xDE,
}
_VK.update({chr(c).lower(): c for c in range(0x41, 0x5B)})            # lettres : VK = code ASCII majuscule
_VK.update({chr(c): c for c in range(0x30, 0x3A)})                    # chiffres
_VK.update({f"f{i}": 0x6F + i for i in range(1, 25)})
_VK.update({f"num{i}": 0x60 + i for i in range(10)})
_VK.update({"num_multiply": 0x6A, "num_add": 0x6B, "num_subtract": 0x6D, "num_decimal": 0x6E, "num_divide": 0x6F})
# modificateurs « génériques » (gauche ou droite) pour les combinaisons comme ctrl+m
_MODS = {"ctrl": (0xA2, 0xA3), "shift": (0xA0, 0xA1), "alt": (0xA4, 0xA5)}
_MOD_KEYS = {"shift_left", "shift_right", "ctrl_left", "ctrl_right", "alt_left", "alt_right"}

_LABELS = {"mouse_middle": "Souris molette (clic)", "mouse4": "Souris 4", "mouse5": "Souris 5",
           "backspace": "Retour arrière", "tab": "Tab", "enter": "Entrée", "pause": "Pause",
           "capslock": "Verr. maj", "space": "Espace", "pageup": "Page haut", "pagedown": "Page bas",
           "end": "Fin", "home": "Début", "left": "Flèche gauche", "up": "Flèche haut",
           "right": "Flèche droite", "down": "Flèche bas", "insert": "Inser", "delete": "Suppr",
           "shift_left": "Maj gauche", "shift_right": "Maj droite", "ctrl_left": "Ctrl gauche",
           "ctrl_right": "Ctrl droit", "alt_left": "Alt gauche", "alt_right": "Alt droit",
           "ctrl": "Ctrl", "shift": "Maj", "alt": "Alt", "semicolon": ";", "equals": "=", "comma": ",",
           "minus": "-", "period": ".", "slash": "/", "backquote": "`", "bracket_left": "[",
           "bracket_right": "]", "backslash": "\\", "quote": "'", "num_multiply": "Pavé *",
           "num_add": "Pavé +", "num_subtract": "Pavé -", "num_decimal": "Pavé .", "num_divide": "Pavé /"}


def key_label(key: str | None) -> str:
    """Nom affichable en français d'un nom de touche (« f9 » -> « F9 », « mouse4 » -> « Souris 4 »)."""
    if not key:
        return "Aucune"
    parts = []
    for k in key.split("+"):
        if k in _LABELS:
            parts.append(_LABELS[k])
        elif k.startswith("num") and k[3:].isdigit():
            parts.append(f"Pavé {k[3:]}")
        else:
            parts.append(k.upper())
    return " + ".join(parts)


def _real_state(vk: int) -> bool:
    return bool(ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000)


class Hotkeys:
    def __init__(self, engine, settings, key_state=None):
        """`key_state(vk) -> bool` peut être injecté (tests)."""
        self.engine, self.settings = engine, settings
        self._state = key_state or (_real_state if sys.platform == "win32" else (lambda vk: False))
        self._keys: dict[str, str | None] = {}
        self._down = {"ptt": False, "mute": False, "deafen": False}
        self._capturing = False
        self._cap_mods: set[str] = set()
        self._cap_armed = False  # on attend que tout soit relâché avant de capturer
        self._cap_deadline = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.apply()

    # ---------- configuration ----------
    def apply(self) -> None:
        s = self.settings
        self._keys = {"ptt": s.ptt_key if s.ptt_mode else None, "mute": s.mute_key, "deafen": s.deafen_key}
        for name in self._down:
            if not self._keys[name]:
                if name == "ptt" and self._down[name]:
                    self.engine.ptt_up()  # touche retirée pendant qu'elle était enfoncée
                self._down[name] = False

    def start(self) -> None:
        if self._thread:
            return
        self._stop.clear()
        if sys.platform == "win32":
            try:
                ctypes.windll.winmm.timeBeginPeriod(1)  # pour que sleep(5 ms) dure vraiment ~5 ms
            except Exception:
                pass
        self._thread = threading.Thread(target=self._run, name="vocal-hotkeys", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        t, self._thread = self._thread, None
        if t:
            t.join(timeout=1)
        if self._down["ptt"]:
            self.engine.ptt_up()
            self._down["ptt"] = False

    def capture_next(self) -> None:
        """La prochaine touche ou bouton pressé est envoyé en Event("key_captured", code=nom). Échap : annule."""
        self._cap_mods = set()
        self._cap_armed = False
        self._cap_deadline = time.monotonic() + CAPTURE_TIMEOUT_S
        self._capturing = True

    # ---------- interrogation ----------
    def is_pressed(self, key: str) -> bool:
        """Vrai si toutes les parties de « ctrl+m » sont enfoncées."""
        for part in key.split("+"):
            if part in _MODS:
                if not any(self._state(vk) for vk in _MODS[part]):
                    return False
            else:
                vk = _VK.get(part)
                if vk is None or not self._state(vk):
                    return False
        return True

    def _run(self) -> None:
        while not self._stop.wait(POLL_S):
            try:
                if self._capturing:
                    self._capture_step()
                else:
                    self._step()
            except Exception:
                log.exception("raccourcis")
                self._stop.wait(0.5)

    def _step(self) -> None:
        for name, key in self._keys.items():
            if not key:
                continue
            now = self.is_pressed(key)
            if now == self._down[name]:
                continue
            self._down[name] = now
            if name == "ptt":
                (self.engine.ptt_down if now else self.engine.ptt_up)()
            elif now and name == "mute":  # bascule à l'appui
                self.engine.set_muted(not self.engine.muted)
            elif now and name == "deafen":
                self.engine.set_deafened(not self.engine.deafened)

    def _capture_step(self) -> None:
        if time.monotonic() > self._cap_deadline:  # personne n'écoute plus : on rend les raccourcis
            self._capturing = False
            return
        pressed = [n for n, vk in _VK.items() if self._state(vk)]
        if not self._cap_armed:  # ignore ce qui était déjà enfoncé (ex. le clic de souris du bouton « Définir »)
            if not pressed:
                self._cap_armed = True
            return
        if self._state(0x1B):  # Échap
            self._finish(None)
            return
        others = [n for n in pressed if n not in _MOD_KEYS]
        mods = [n for n in pressed if n in _MOD_KEYS]
        self._cap_mods |= set(mods)
        if others:
            prefix = []
            for g, (l, r) in (("ctrl", ("ctrl_left", "ctrl_right")), ("shift", ("shift_left", "shift_right")),
                              ("alt", ("alt_left", "alt_right"))):
                if l in mods or r in mods:
                    prefix.append(g)
            self._finish("+".join(prefix + [others[0]]))
        elif not mods and self._cap_mods:  # un modificateur seul a été pressé puis relâché
            self._finish(sorted(self._cap_mods)[0])

    def _finish(self, code: str | None) -> None:
        from engine import Event
        self._capturing = False
        self.engine.events.put(Event("key_captured", code=code))

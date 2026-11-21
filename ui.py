"""Interface Vocal LAN (customtkinter) : fenêtre principale, réglages, aide.

Règles :
  - Ce module ne touche ni aux sockets ni à sounddevice : tout passe par l'objet ``engine``
    (Engine ou FakeEngine) et par des ``Snapshot`` immuables.
  - Un seul thread touche à Tk : le thread principal. Le moteur, les raccourcis et l'icône
    de la zone de notification communiquent par files (``engine.events``, ``tray.commands``).
  - Tick à 20 Hz, mises à jour différentielles : les widgets ne sont créés qu'à l'arrivée
    d'un participant et détruits à son départ.
"""
from __future__ import annotations

import logging
import math
import os
import queue
import re
import sys
import threading
import time
import tkinter as tk
from pathlib import Path

import customtkinter as ctk
from PIL import Image, ImageDraw

try:
    from settings import VERSION, app_dir, resource_path  # type: ignore
except Exception:  # noqa: BLE001 - settings.py (Dev A) pas encore là
    from fake_engine import VERSION, app_dir, resource_path

try:
    from engine import EngineError  # type: ignore
except Exception:  # noqa: BLE001
    from fake_engine import EngineError

log = logging.getLogger("vocal.ui")

# ===========================================================================
# Palette (toutes les couleurs de l'appli sont ici)
# ===========================================================================
BG = "#0f1218"
SURFACE = "#171b24"
SURFACE_2 = "#1f2431"
SURFACE_3 = "#2a3040"
BORDER = "#2b3244"
TEXT = "#f2f4f9"
TEXT_MUTED = "#a8b0c2"       # 7:1 sur BG
TEXT_DIM = "#8b94a9"         # 5:1 sur SURFACE
ACCENT = "#5b8cff"
ACCENT_HOVER = "#7aa2ff"
GREEN = "#3ddc84"
GREEN_BG = "#143626"
GREEN_BG_HOVER = "#1a4631"
RED = "#ff6b66"
RED_BG = "#4a1e21"
RED_BG_HOVER = "#5c262a"
ORANGE = "#f7b13d"
ORANGE_BG = "#3b2e14"
BLUE_BG = "#18283f"
METER_TRACK = "#262c3b"
METER_IDLE = "#5d6a85"

AVATAR_COLORS = ("#4f7cf0", "#8b5cf6", "#d6489a", "#d9692a", "#1fa971", "#0f9fb8", "#b7791f",
                 "#d9514e")

DEFAULT_DEVICE = "Par défaut de Windows"
FONT_FAMILY = "Segoe UI"
TICK_MS = 50                 # 20 Hz
SAVE_EVERY_S = 1.0
ALONE_HELP_AFTER_S = 20.0
NOBODY_HEARS_AFTER_S = 8.0   # des pairs sont là mais aucun ne nous reçoit depuis ce délai : bandeau pare-feu
DEVICE_ADVICE = "Choisis un autre {what} dans la liste ou rebranche-le, puis clique Actualiser."
MAX_NAME = 24

# --- 2.1 : suppression du bruit (4 crans), style de voix ------------------------------------------
DENOISE_LEVELS = (("off", "Aucune"), ("light", "Léger"), ("normal", "Normal"), ("strong", "Fort"))
DENOISE_LABEL = dict(DENOISE_LEVELS)
DENOISE_FROM_LABEL = {v: k for k, v in DENOISE_LEVELS}
# Délais annoncés par cran (contrat) ; si le moteur sait mieux (``denoise_delays_ms``), c'est lui qui fait foi.
DENOISE_DELAY_MS = {"off": 0, "light": 5, "normal": 25, "strong": 25}
WHISPER_NOTE = "Si tu chuchotes, choisis Léger ou Aucune."
DENOISE_TEXT = {
    "off": "Ta voix part telle quelle, sans délai ajouté.",
    "light": "Enlève le ventilateur et le souffle. Ta voix reste naturelle, avec le moins de délai.",
    "normal": "Enlève aussi le clavier et la souris entre tes mots, en gardant ta voix la plus naturelle possible. " + WHISPER_NOTE,
    "strong": "Nettoie au maximum, pour une pièce très bruyante. Ta voix peut sonner un peu moins naturelle. " + WHISPER_NOTE,
}
# Normal et Fort ont le même délai (+25 ms) : on le dit franchement, sans l'alarmer.
LAG_REASSURE = "À peine perceptible ; coupe-le si tu veux le son le plus direct possible."
LAG_EXPLAIN = ("Ce délai peut se sentir dans une conversation très rapide. Le réglage Léger n'ajoute que {light}, "
               "et « Aucune » n'ajoute rien.")
MON_MUTED = "Ton micro est coupé : personne ne t'entend"
MON_PTT_UP = "Touche relâchée : personne ne t'entend"
STYLE_LABELS = (("natural", "Naturelle"), ("clear", "Plus claire"))
STYLE_TEXT = {
    "natural": "Ta voix telle qu'elle est (les grondements graves sont coupés).",
    "clear": "Volume plus régulier, voix plus nette, « s » adoucis. Aucun délai en plus.",
}
STYLE_TEXT_NO_HP = "Ta voix telle qu'elle est, sans aucun filtre sur les graves."
SLOW_AFTER_MS = 30           # au-delà de 30 ms de délai ajouté, il s'affiche en orange (et s'explique)
CLEAN_PEER_MS = 25           # « Nettoyer sa voix » : délai sur la voix de cet ami seulement
CLEAN_MAX = 3
NOISY_FLOOR = 0.17           # bruit de fond ambiant au-dessus de cette valeur du vumètre (≈ -50 dB) : « bruyant »


def denoise_delay(level: str, snap=None) -> int:
    """Délai nominal d'un niveau (ms), tel que le moteur le mesure (denoise.level_latency_samples) ;
    repli sur la table du contrat si ce module est absent."""
    table = getattr(snap, "denoise_delays_ms", None)
    if isinstance(table, dict) and level in table:
        return int(round(table[level]))
    try:
        from denoise import level_latency_samples  # type: ignore
        return int(round(level_latency_samples(level) * 1000 / 48000))
    except Exception:  # noqa: BLE001
        return DENOISE_DELAY_MS.get(level, 0)


def manual_gate_idle(snap) -> bool:
    """Le filtre de bruit manuel ne sert à rien quand Normal / Fort sont en marche (une seule porte à la fois)."""
    return bool(getattr(snap, "denoise_active", False)) and getattr(snap, "denoise_level", "off") in ("normal", "strong")


def delay_label(ms: float) -> str:
    return f"+{round(ms):d} ms" if round(ms) > 0 else "aucun"


def total_delay_ms(snap) -> int:
    """Estimation honnête de ta voix jusqu'à l'oreille d'un ami : micro + casque + marge réseau + réglages."""
    try:
        lat = snap.audio.latency_ms
        dev = float(sum(lat)) if lat else 25.0
    except Exception:  # noqa: BLE001
        dev = 25.0
    bufs = sorted(p.buffer_ms for p in snap.peers)
    buf = bufs[len(bufs) // 2] if bufs else 10          # l'ami « médian » : pas le pire cas
    return int(round(dev + buf + float(getattr(snap, "latency_added_ms", 0.0) or 0.0)))


def make_seg(master, values, command, height: int = 34):
    return ctk.CTkSegmentedButton(master, values=list(values), height=height, font=font(12),
                                  selected_color=ACCENT, selected_hover_color=ACCENT_HOVER,
                                  unselected_color=SURFACE_3, unselected_hover_color=BORDER,
                                  fg_color=SURFACE_3, text_color=TEXT, command=command)


_SCALE = 1.0                # facteur DPI, fixé à la création de la fenêtre


def px(n: float) -> int:
    """Pixels réels pour les widgets Tk bruts (Canvas), qui ne sont pas mis à l'échelle par ctk."""
    return max(1, int(round(n * _SCALE)))


_fonts: dict = {}


def font(size: int = 13, weight: str = "normal") -> ctk.CTkFont:
    key = (size, weight)
    if key not in _fonts:
        _fonts[key] = ctk.CTkFont(family=FONT_FAMILY, size=size, weight=weight)
    return _fonts[key]


# ===========================================================================
# Utilitaires
# ===========================================================================
_key_label_fn = None


def key_label(code) -> str:
    """Nom affichable d'une touche (hotkeys.key_label si dispo)."""
    global _key_label_fn
    if not code:
        return ""
    if _key_label_fn is None:
        try:
            from hotkeys import key_label as kl  # type: ignore
            _key_label_fn = kl
        except Exception:  # noqa: BLE001
            _key_label_fn = lambda c: str(c)  # noqa: E731
    try:
        return _key_label_fn(code)
    except Exception:  # noqa: BLE001
        return str(code)


def initial_of(name: str) -> str:
    for ch in name or "":
        if ch.isalnum():
            return ch.upper()
    return "?"


def color_for(ident: str) -> str:
    h = sum(ord(c) * (i + 7) for i, c in enumerate(ident or "?"))
    return AVATAR_COLORS[h % len(AVATAR_COLORS)]


def clip(text: str, n: int = 22) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def group_names(names: list[str], one: str, many: str) -> str:
    """« Marie a rejoint » / « Marie et Tom ont rejoint » / « Marie, Tom et 2 autres ont rejoint »."""
    names = [clip(n, 18) for n in names]
    if len(names) == 1:
        return f"{names[0]} {one}"
    if len(names) == 2:
        return f"{names[0]} et {names[1]} {many}"
    rest = len(names) - 2
    return f"{names[0]}, {names[1]} et {rest} autre{'s' if rest > 1 else ''} {many}"


def with_advice(msg: str, what: str = "appareil") -> str:
    """Ajoute après l'erreur du moteur ce que l'utilisateur peut faire (une seule fois)."""
    msg = (msg or "Cet appareil ne marche pas.").strip()
    if "Actualiser" in msg:
        return msg
    return f"{msg} {DEVICE_ADVICE.format(what=what)}"


def count_label(n_people: int) -> str:
    return f"{n_people} personne" + ("s" if n_people > 1 else "")


def pct(v: float) -> str:
    return f"{round(v * 100):d} %"


def open_folder(path: Path) -> None:
    try:
        os.startfile(str(path))  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001
        log.warning("Ouverture du dossier impossible : %s", exc)


class Diff:
    """Mélange : ``self.upd(widget, texte=..)`` ne reconfigure un widget que si une valeur change."""

    def _init_diff(self) -> None:
        self._dcache: dict = {}

    def upd(self, widget, **kw) -> None:
        changed = {}
        for k, v in kw.items():
            if self._dcache.get((widget, k), _MISSING) != v:
                self._dcache[(widget, k)] = v
                changed[k] = v
        if changed:
            widget.configure(**changed)


_MISSING = object()


# ===========================================================================
# Icônes dessinées avec PIL (nettes à toutes les tailles, sans fichiers)
# ===========================================================================
_icon_cache: dict = {}


def _draw_icon(kind: str, color: str) -> Image.Image:
    S = 128
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    c = color
    clear = (0, 0, 0, 0)

    def slash():
        d.line((20, 18, 108, 112), fill=clear, width=26)
        d.line((20, 18, 108, 112), fill=c, width=10)

    if kind in ("mic", "mic_off"):
        d.rounded_rectangle((44, 8, 84, 72), radius=20, fill=c)
        d.arc((26, 36, 102, 100), 0, 180, fill=c, width=9)
        d.line((64, 100, 64, 118), fill=c, width=9)
        d.line((44, 119, 84, 119), fill=c, width=9)
        if kind == "mic_off":
            slash()
    elif kind in ("head", "head_off"):
        d.arc((20, 14, 108, 106), 180, 360, fill=c, width=9)
        d.line((24, 60, 24, 70), fill=c, width=9)
        d.line((104, 60, 104, 70), fill=c, width=9)
        d.rounded_rectangle((12, 60, 42, 106), radius=11, fill=c)
        d.rounded_rectangle((86, 60, 116, 106), radius=11, fill=c)
        if kind == "head_off":
            slash()
    elif kind == "gear":
        cx = cy = 64
        for i in range(8):
            a = i * math.pi / 4
            ca, sa = math.cos(a), math.sin(a)
            pts = []
            for r, w in ((30, -9), (30, 9), (54, 9), (54, -9)):
                pts.append((cx + r * ca - w * sa, cy + r * sa + w * ca))
            d.polygon(pts, fill=c)
        d.ellipse((cx - 40, cy - 40, cx + 40, cy + 40), fill=c)
        d.ellipse((cx - 17, cy - 17, cx + 17, cy + 17), fill=clear)
    elif kind == "copy":
        d.rounded_rectangle((40, 40, 110, 112), radius=12, outline=c, width=9)
        d.rounded_rectangle((16, 14, 86, 86), radius=12, fill=clear)
        d.rounded_rectangle((16, 14, 86, 86), radius=12, outline=c, width=9)
    elif kind == "plus":
        d.line((64, 24, 64, 104), fill=c, width=12)
        d.line((24, 64, 104, 64), fill=c, width=12)
    elif kind == "check":
        d.line((22, 68, 52, 96), fill=c, width=13)
        d.line((52, 96, 106, 34), fill=c, width=13)
    elif kind == "alert":
        d.polygon([(64, 12), (120, 108), (8, 108)], outline=c, width=9)
        d.line((64, 46, 64, 78), fill=c, width=10)
        d.ellipse((58, 88, 70, 100), fill=c)
    return img


def icon(kind: str, color: str = TEXT, size: int = 20) -> ctk.CTkImage:
    key = (kind, color, size)
    if key not in _icon_cache:
        im = _draw_icon(kind, color)
        _icon_cache[key] = ctk.CTkImage(light_image=im, dark_image=im, size=(size, size))
    return _icon_cache[key]


def logo_image(size: int) -> ctk.CTkImage | None:
    try:
        im = Image.open(resource_path("assets/icon.png")).convert("RGBA")
        return ctk.CTkImage(light_image=im, dark_image=im, size=(size, size))
    except Exception:  # noqa: BLE001
        return None


def apply_window_icon(win) -> None:
    """customtkinter écrase l'icône 200 ms après la création : on la repose ensuite."""
    def go():
        try:
            ico = resource_path("assets/icon.ico")
            if Path(ico).exists():
                win.iconbitmap(str(ico))
        except Exception:  # noqa: BLE001
            pass
    try:
        win.after(300, go)
    except Exception:  # noqa: BLE001
        pass


# ===========================================================================
# Petits widgets
# ===========================================================================
class Label(ctk.CTkLabel):
    """CTkLabel dont ``wraplength`` suit la largeur du conteneur (fenêtres redimensionnables, 480 px mini).

    ``pad`` : place retirée à la largeur du conteneur (marges, icône voisine...). Sans effet si le
    conteneur est une fenêtre à taille fixe : le ``wraplength`` donné est alors conservé.
    """

    def __init__(self, master, *args, pad: int = 40, **kw):
        super().__init__(master, *args, **kw)
        self._pad, self._wl = pad, None
        if kw.get("wraplength") and not isinstance(master, (ctk.CTkToplevel, ctk.CTk, tk.Toplevel)):
            master.bind("<Configure>", self._fit, add="+")

    def _fit(self, e):
        wl = max(120, int((e.width - self._pad) / _SCALE))
        if wl != self._wl:
            self._wl = wl
            try:
                self.configure(wraplength=wl)
            except Exception:  # noqa: BLE001
                pass


class Tooltip:
    """Infobulle au survol. ``text`` peut être une chaîne ou une fonction."""

    def __init__(self, widget, text):
        self.widget, self.text = widget, text
        self._tip = None
        self._job = None
        for ev, fn in (("<Enter>", self._enter), ("<Leave>", self._leave),
                       ("<ButtonPress>", self._leave)):
            widget.bind(ev, fn, add="+")

    def _enter(self, _e=None):
        self._cancel()
        self._job = self.widget.after(450, self._show)

    def _cancel(self):
        if self._job:
            try:
                self.widget.after_cancel(self._job)
            except Exception:  # noqa: BLE001
                pass
            self._job = None

    def _leave(self, _e=None):
        self._cancel()
        if self._tip is not None:
            try:
                self._tip.destroy()
            except Exception:  # noqa: BLE001
                pass
            self._tip = None

    def _show(self):
        self._job = None
        txt = self.text() if callable(self.text) else self.text
        if not txt:
            return
        try:
            x = self.widget.winfo_pointerx() + 14
            y = self.widget.winfo_pointery() + 18
            tip = tk.Toplevel(self.widget)
            tip.wm_overrideredirect(True)
            tip.wm_attributes("-topmost", True)
            tip.wm_geometry(f"+{x}+{y}")
            tk.Label(tip, text=txt, justify="left", bg=SURFACE_3, fg=TEXT, padx=10, pady=6,
                     font=(FONT_FAMILY, 10), bd=1, relief="solid", highlightthickness=0).pack()
            self._tip = tip
        except Exception:  # noqa: BLE001
            self._tip = None


_avatar_cache: dict = {}
_pil_font_cache: dict = {}


def _pil_font(size: int):
    from PIL import ImageFont
    if size not in _pil_font_cache:
        f = None
        for name in ("segoeuib.ttf", "seguisb.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf"):
            try:
                f = ImageFont.truetype(name, size)
                break
            except Exception:  # noqa: BLE001
                continue
        _pil_font_cache[size] = f or ImageFont.load_default()
    return _pil_font_cache[size]


def _avatar_image(size: int, letter: str, color: str, speaking: bool, dim: bool, bg: str):
    """Pastille lissée (rendue à 4x puis réduite), mise en cache."""
    from PIL import ImageTk
    key = (size, letter, color, speaking, dim, bg)
    img = _avatar_cache.get(key)
    if img is None:
        S = size * 4
        im = Image.new("RGBA", (S, S), bg)
        d = ImageDraw.Draw(im)
        m = int(S * 0.12)
        if speaking:
            d.ellipse((0, 0, S - 1, S - 1), outline=GREEN, width=int(S * 0.065))
        d.ellipse((m, m, S - m - 1, S - m - 1), fill=SURFACE_3 if dim else color)
        f = _pil_font(int(S * 0.40))
        d.text((S / 2, S / 2), letter, fill=TEXT_DIM if dim else "white", font=f, anchor="mm")
        im = im.resize((size, size), Image.LANCZOS)
        img = _avatar_cache[key] = ImageTk.PhotoImage(im)
    return img


class Avatar(tk.Label):
    """Pastille ronde : initiale sur fond coloré, anneau vert quand la personne parle."""

    def __init__(self, master, size: int = 48, bg: str = SURFACE):
        self._px = px(size)
        super().__init__(master, bg=bg, bd=0, highlightthickness=0, padx=0, pady=0)
        self._bg_color = bg
        self._speaking = False
        self._ident = ("?", SURFACE_3, False)
        self._render()

    def _render(self):
        letter, color, dim = self._ident
        self.configure(image=_avatar_image(self._px, letter, color, self._speaking, dim, self._bg_color))

    def set_identity(self, letter: str, color: str, dim: bool = False):
        key = (letter, color, dim)
        if key != self._ident:
            self._ident = key
            self._render()

    def set_speaking(self, on: bool):
        if on != self._speaking:
            self._speaking = on
            self._render()


class Meter(tk.Canvas):
    """Vumètre en forme de pilule, avec le seuil du filtre dessiné dessus."""

    def __init__(self, master, height: int = 16, bg: str = SURFACE):
        super().__init__(master, height=px(height), bg=bg, highlightthickness=0, bd=0)
        self._ht = px(height)
        self._wd = 1
        self.level = 0.0
        self.gate = 0.0
        self.active = False
        self._last = None
        self.track = self.create_line(0, 0, 0, 0, width=self._ht, capstyle="round", fill=METER_TRACK)
        self.fill = self.create_line(0, 0, 0, 0, width=self._ht, capstyle="round", fill=METER_IDLE)
        self.mark = self.create_line(0, 0, 0, 0, width=max(2, px(2)), fill=TEXT)
        self.mark_top = self.create_polygon(0, 0, 0, 0, 0, 0, fill=TEXT, outline="")
        self.bind("<Configure>", self._on_resize)

    def _on_resize(self, e):
        self._wd = max(e.width, 2 * self._ht)
        self._last = None
        self._draw()

    def set(self, level: float, active: bool, gate: float = 0.0):
        level = round(max(0.0, min(1.0, level)), 2)
        gate = round(max(0.0, min(1.0, gate)), 3)
        key = (level, active, gate)
        if key == self._last:
            return
        self.level, self.active, self.gate = level, active, gate
        self._draw()

    def _draw(self):
        self._last = (self.level, self.active, self.gate)
        h, w = self._ht, self._wd
        y = h // 2
        x0, x1 = h // 2, w - h // 2
        self.coords(self.track, x0, y, x1, y)
        if self.level < 0.03:
            self.coords(self.fill, 0, 0, 0, 0)
        else:
            self.coords(self.fill, x0, y, x0 + self.level * (x1 - x0), y)
        color = METER_IDLE
        if self.active:
            color = ORANGE if self.level > 0.9 else GREEN
        self.itemconfigure(self.fill, fill=color)
        if self.gate > 0:
            gx = x0 + self.gate * (x1 - x0)
            self.coords(self.mark, gx, 0, gx, h)
            t = max(3, h // 4)
            self.coords(self.mark_top, gx - t, 0, gx + t, 0, gx, t * 1.4)
        else:
            self.coords(self.mark, 0, 0, 0, 0)
            self.coords(self.mark_top, 0, 0, 0, 0, 0, 0)


class QualityBars(tk.Canvas):
    """Trois barres : 3 vertes = excellente, 2 orange = correcte, 1 rouge = mauvaise."""

    def __init__(self, master, bg: str = SURFACE):
        super().__init__(master, width=px(24), height=px(18), bg=bg, highlightthickness=0, bd=0)
        self._q = None
        self.bars = []
        for i in range(3):
            x = px(2) + i * px(8)
            h = px(7) + i * px(5)
            self.bars.append(self.create_rectangle(x, px(17) - h, x + px(5), px(17),
                                                   fill=METER_TRACK, outline=""))

    def set(self, quality: str):
        if quality == self._q:
            return
        self._q = quality
        n, col = {"good": (3, GREEN), "ok": (2, ORANGE), "bad": (1, RED)}.get(quality, (0, METER_IDLE))
        for i, b in enumerate(self.bars):
            self.itemconfigure(b, fill=col if i < n else SURFACE_3)


class SliderRow(ctk.CTkFrame):
    """Curseur + valeur. ``set()`` n'écrase pas la valeur pendant que l'utilisateur la glisse."""

    def __init__(self, master, from_, to, steps, fmt, command, value=None, slider_width=None):
        super().__init__(master, fg_color="transparent")
        self.fmt, self.command = fmt, command
        self.dragging = False
        kw = {"width": slider_width} if slider_width else {}
        self.slider = ctk.CTkSlider(self, from_=from_, to=to, number_of_steps=steps, height=18,
                                    command=self._moved, button_color="#e8edff",
                                    button_hover_color="#ffffff", progress_color=ACCENT,
                                    fg_color=SURFACE_3, **kw)
        self.slider.pack(side="left", fill="x", expand=True)
        self.value_lbl = Label(self, text="", width=52, anchor="e", font=font(12),
                                      text_color=TEXT_MUTED)
        self.value_lbl.pack(side="right", padx=(8, 0))
        self.slider.bind("<ButtonPress-1>", self._press, add="+")
        self.slider.bind("<ButtonRelease-1>", self._release, add="+")
        self._shown = None
        if value is not None:
            self.set(value, force=True)

    def _press(self, _e=None):
        self.dragging = True

    def _release(self, _e=None):
        self.dragging = False

    def _moved(self, v):
        self._show(v)
        self.command(v)

    def _show(self, v):
        txt = self.fmt(v)
        if txt != self._shown:
            self._shown = txt
            self.value_lbl.configure(text=txt)

    def set(self, v, force=False):
        if self.dragging and not force:
            return
        if abs(self.slider.get() - v) > 1e-6:
            self.slider.set(v)
        self._show(v)

    def get(self):
        return self.slider.get()

    def set_enabled(self, on: bool):
        """Grise (ou rétablit) le curseur ; la valeur reste affichée."""
        if getattr(self, "_enabled", True) == on:
            return
        self._enabled = on
        self.slider.configure(state="normal" if on else "disabled",
                              progress_color=ACCENT if on else TEXT_DIM,
                              button_color="#e8edff" if on else TEXT_DIM)
        self.value_lbl.configure(text_color=TEXT_MUTED if on else TEXT_DIM)


class BigToggle(ctk.CTkFrame, Diff):
    """Gros bouton d'état : icône + titre (l'état) + indication (l'action et le raccourci)."""

    def __init__(self, master, command):
        super().__init__(master, corner_radius=16, fg_color=SURFACE_2, height=68)
        self._init_diff()
        self.command = command
        self._colors = (SURFACE_2, SURFACE_3)
        self._hover = False
        self.icon_lbl = Label(self, text="", width=44)
        self.icon_lbl.pack(side="left", padx=(14, 4), pady=10)
        box = ctk.CTkFrame(self, fg_color="transparent")
        box.pack(side="left", fill="both", expand=True, padx=(2, 8), pady=10)
        self.title_lbl = Label(box, text="", anchor="w", font=font(15, "bold"))
        self.title_lbl.pack(fill="x", pady=(0, 0))
        # l'indication passe à la ligne au lieu d'être coupée (raccourcis longs, fenêtre de 480 px)
        self.hint_lbl = Label(box, text="", anchor="w", justify="left", font=font(11),
                              text_color=TEXT_MUTED, wraplength=90, pad=6)
        self.hint_lbl.pack(fill="x")
        for w in (self, self.icon_lbl, box, self.title_lbl, self.hint_lbl):
            w.bind("<Button-1>", lambda _e: self.command(), add="+")
            w.bind("<Enter>", self._enter, add="+")
            w.bind("<Leave>", self._leave, add="+")
            try:
                w.configure(cursor="hand2")
            except Exception:  # noqa: BLE001
                pass

    def _enter(self, _e=None):
        self._hover = True
        self.configure(fg_color=self._colors[1])

    def _leave(self, _e=None):
        try:
            under = self.winfo_containing(*self.winfo_pointerxy())
        except Exception:  # noqa: BLE001
            under = None
        w = under
        while w is not None:
            if w is self:
                return
            w = getattr(w, "master", None)
        self._hover = False
        self.configure(fg_color=self._colors[0])

    def set_state(self, title, hint, icon_img, bg, bg_hover, title_color, hint_color=TEXT_MUTED):
        self._colors = (bg, bg_hover)
        self.upd(self, fg_color=bg_hover if self._hover else bg)
        self.upd(self.title_lbl, text=title, text_color=title_color)
        self.upd(self.hint_lbl, text=hint, text_color=hint_color)
        self.upd(self.icon_lbl, image=icon_img)


class Banner(ctk.CTkFrame):
    """Bandeau d'information (erreur, avertissement, info, succès)."""

    STYLES = {
        "error": (RED_BG, "#ffc2bf", RED),
        "warn": (ORANGE_BG, "#ffe0a8", ORANGE),
        "info": (BLUE_BG, "#c3d6ff", ACCENT),
        "ok": (GREEN_BG, "#b4f2d0", GREEN),
    }

    def __init__(self, master, on_close=None):
        super().__init__(master, corner_radius=12, border_width=1)
        self.on_close = on_close
        self._sig = None
        self.label = ctk.CTkLabel(self, text="", anchor="w", justify="left", font=font(12),
                                  wraplength=360)
        self.label.pack(side="left", fill="x", expand=True, padx=(12, 6), pady=8)
        self.button = ctk.CTkButton(self, text="", width=10, height=28, corner_radius=8,
                                    font=font(12, "bold"))
        self.close_btn = ctk.CTkButton(self, text="✕", width=28, height=28, corner_radius=8,
                                       fg_color="transparent", hover_color=SURFACE_3, font=font(12),
                                       command=self._close)
        self.bind("<Configure>", self._resized)

    def _resized(self, e):
        wl = max(160, e.width - (190 if self.button.winfo_manager() else 70))
        if getattr(self, "_wl", None) != wl:
            self._wl = wl
            self.label.configure(wraplength=wl)

    def _close(self):
        if self.on_close:
            self.on_close()

    def show(self, text, kind="info", action_text=None, action_cmd=None, closable=True):
        sig = (text, kind, action_text, closable)
        if sig == self._sig:
            return
        self._sig = sig
        bg, fg, edge = self.STYLES[kind]
        self.configure(fg_color=bg, border_color=edge)
        self.label.configure(text=text, text_color=fg)
        self.button.pack_forget()
        self.close_btn.pack_forget()
        if action_text:
            self.button.configure(text=action_text, command=action_cmd, fg_color=edge,
                                  hover_color=edge, text_color="#10131a")
            self.button.pack(side="right", padx=(0, 8), pady=6)
        if closable:
            self.close_btn.configure(text_color=fg)
            self.close_btn.pack(side="right", padx=(0, 4))


class BannerStack:
    """Pile de bandeaux identifiés par une clé ; aucun widget recréé à l'affichage."""

    def __init__(self, master, side_pady=(0, 6)):
        self.frame = ctk.CTkFrame(master, fg_color="transparent", width=0, height=0)
        self._banners: dict[str, Banner] = {}
        self.pady = side_pady

    def show(self, key, text, kind="info", action_text=None, action_cmd=None, closable=True):
        b = self._banners.get(key)
        if b is None:
            b = self._banners[key] = Banner(self.frame, on_close=lambda k=key: self.hide(k))
        b.show(text, kind, action_text, action_cmd, closable)
        if not b.winfo_manager():
            b.pack(fill="x", pady=self.pady)

    def hide(self, key):
        b = self._banners.get(key)
        if b is not None and b.winfo_manager():
            b.pack_forget()

    def visible(self, key) -> bool:
        b = self._banners.get(key)
        return bool(b and b.winfo_manager())

    def any_visible(self) -> bool:
        return any(b.winfo_manager() for b in self._banners.values())


# ===========================================================================
# Fenêtres de dialogue
# ===========================================================================
class Dialog(ctk.CTkToplevel):
    def __init__(self, app, title: str, width: int, height: int, modal: bool = False,
                 minsize: tuple | None = None):
        super().__init__(app, fg_color=BG)
        parent = app
        app = getattr(parent, "app", parent)          # le parent peut être une autre fenêtre (aide)
        self.app = app
        self.title(title)
        if minsize is None:
            self.resizable(False, False)
        else:   # (customtkinter masque la fenêtre si on change la taille avant son premier affichage)
            self.after(120, lambda: self.winfo_exists() and self.minsize(*minsize))
        try:
            app.update_idletasks()
            # CTkToplevel.geometry ne met à l'échelle que la taille ; x et y sont déjà des pixels réels
            x = app.winfo_rootx() + max(0, (app.winfo_width() - px(width)) // 2)
            y = app.winfo_rooty() + max(0, (app.winfo_height() - px(height)) // 3)
            self.geometry(f"{width}x{height}+{max(0, int(x))}+{max(0, int(y))}")
        except Exception:  # noqa: BLE001
            self.geometry(f"{width}x{height}")
        self.transient(parent)
        apply_window_icon(self)
        if modal:
            self.after(80, self._grab)
        self.bind("<Escape>", lambda _e: self.destroy())

    def _grab(self):
        try:
            if not self.winfo_exists():
                return
            self.grab_set()
            self.focus_force()
        except Exception:  # noqa: BLE001
            pass


class FirewallDialog(Dialog):
    """Explication AVANT la demande d'autorisation Windows (UAC)."""

    def __init__(self, app, on_confirm):
        super().__init__(app, "Autoriser Vocal LAN dans le pare-feu", 460, 330, modal=True)
        self.on_confirm = on_confirm
        Label(self, text="Une autorisation va être demandée", font=font(18, "bold"),
                     anchor="w").pack(fill="x", padx=24, pady=(22, 8))
        Label(
            self, anchor="w", justify="left", wraplength=410, font=font(13), text_color=TEXT_MUTED,
            text=("Le pare-feu de Windows empêche parfois tes amis de te trouver.\n\n"
                  "Windows va afficher une fenêtre « Contrôle de compte d'utilisateur » pour te "
                  "demander ton accord. C'est normal : clique sur « Oui ».\n\n"
                  "Vocal LAN ajoute seulement une règle qui l'autorise à communiquer sur ton "
                  "réseau local. Rien d'autre n'est modifié et tu peux la retirer quand tu veux.")
        ).pack(fill="x", padx=24)
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(side="bottom", fill="x", padx=24, pady=20)
        ctk.CTkButton(row, text="Annuler", width=110, height=36, fg_color=SURFACE_3,
                      hover_color=BORDER, command=self.destroy).pack(side="right", padx=(8, 0))
        ctk.CTkButton(row, text="Continuer", width=130, height=36, fg_color=ACCENT,
                      hover_color=ACCENT_HOVER, text_color="#0b1020", font=font(13, "bold"),
                      command=self._go).pack(side="right")

    def _go(self):
        self.destroy()
        self.on_confirm()


class AddIpRow(ctk.CTkFrame):
    """Champ « adresse IP » + bouton Ajouter, avec retour d'erreur lisible."""

    def __init__(self, master, app, on_done=None):
        super().__init__(master, fg_color="transparent")
        self.app, self.on_done = app, on_done
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(fill="x")
        self.entry = ctk.CTkEntry(row, placeholder_text="Exemple : 192.168.1.25", height=34,
                                  fg_color=SURFACE_2, border_color=BORDER, font=font(13))
        self.entry.pack(side="left", fill="x", expand=True)
        self.entry.bind("<Return>", lambda _e: self.submit())
        self._busy = False
        self.add_btn = ctk.CTkButton(row, text="Ajouter", width=90, height=34, fg_color=ACCENT,
                                     hover_color=ACCENT_HOVER, text_color="#0b1020",
                                     font=font(13, "bold"), command=self.submit)
        self.add_btn.pack(side="right", padx=(8, 0))
        self.msg = Label(self, text="", anchor="w", justify="left", wraplength=380,
                                font=font(12))

    def _say(self, text, color):
        self.msg.configure(text=text, text_color=color)
        self.msg.pack(fill="x", pady=(4, 0))

    def _alive(self) -> bool:
        try:
            return bool(self.winfo_exists())
        except Exception:  # noqa: BLE001
            return False

    def submit(self):
        text = self.entry.get().strip()
        if not text or self._busy:
            return
        # « http://1.2.3.4/ » : on enlève le préfixe que les gens collent depuis un navigateur
        text = re.sub(r"^[A-Za-z][A-Za-z0-9+.-]*://", "", text).rstrip("/ ")
        self._busy = True
        self.add_btn.configure(state="disabled")
        self._say("Recherche…", TEXT_MUTED)       # la résolution d'un nom peut durer 2 s : hors thread Tk
        self.app.run_bg(lambda: self.app.engine.add_peer_ip(text),
                        lambda _res, exc: self._added(text, exc))

    def _added(self, text, exc):
        self._busy = False
        if not self._alive():
            return
        self.add_btn.configure(state="normal")
        if isinstance(exc, EngineError):
            self._say(getattr(exc, "message_fr", str(exc)), RED)
            return
        if exc is not None:
            log.warning("add_peer_ip : %s", exc)
            self._say(f"Impossible d'ajouter cette adresse ({exc}).", RED)
            return
        self.entry.delete(0, "end")
        self._say("Ajouté. Si la personne est là, elle apparaîtra dans quelques secondes.", GREEN)
        self.app.toast("Recherche de " + text + "…")
        if self.on_done:
            self.on_done()


class AddIpDialog(Dialog):
    def __init__(self, app):
        super().__init__(app, "Ajouter par IP", 440, 240, modal=True)
        Label(self, text="Ajouter quelqu'un par son adresse", font=font(17, "bold"),
                     anchor="w").pack(fill="x", padx=24, pady=(22, 4))
        Label(self, anchor="w", justify="left", wraplength=390, font=font(12),
                     text_color=TEXT_MUTED,
                     text="Ton ami la trouve dans Vocal LAN avec le bouton « Copier mon IP ».").pack(
            fill="x", padx=24, pady=(0, 12))
        AddIpRow(self, app).pack(fill="x", padx=24)
        ctk.CTkButton(self, text="Fermer", width=100, height=34, fg_color=SURFACE_3,
                      hover_color=BORDER, command=self.destroy).pack(side="bottom", anchor="e",
                                                                    padx=24, pady=18)


class HelpWindow(Dialog):
    """« Ça ne marche pas ? » : checklist menant au bouton du pare-feu."""

    def __init__(self, app):
        try:
            sh = app.winfo_screenheight()
        except Exception:  # noqa: BLE001
            sh = 900
        super().__init__(app, "Ça ne marche pas ?", 520, max(520, min(780, int(sh / _SCALE) - 110)),
                         minsize=(460, 520))
        body = self.body_scroll = ctk.CTkScrollableFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=10, pady=10)
        Label(body, text="Ça ne marche pas ?", font=font(22, "bold"), anchor="w").pack(
            fill="x", padx=12, pady=(8, 0))
        Label(body, text="Vérifie ces points, dans l'ordre.", font=font(13),
                     text_color=TEXT_MUTED, anchor="w").pack(fill="x", padx=12, pady=(0, 8))

        def card(num, title, text):
            c = ctk.CTkFrame(body, fg_color=SURFACE, corner_radius=14)
            c.pack(fill="x", padx=8, pady=6)
            head = ctk.CTkFrame(c, fg_color="transparent")
            head.pack(fill="x", padx=14, pady=(12, 2))
            Label(head, text=str(num), width=26, height=26, corner_radius=13, fg_color=ACCENT,
                         text_color="#0b1020", font=font(13, "bold")).pack(side="left")
            Label(head, text=title, font=font(14, "bold"), anchor="w", justify="left",
                         wraplength=380, pad=60).pack(side="left", padx=10)
            Label(c, text=text, anchor="w", justify="left", wraplength=420, font=font(12),
                         text_color=TEXT_MUTED).pack(fill="x", padx=14, pady=(0, 12))
            return c

        card(1, "Tes amis sont sur le même réseau ?",
             "Toi et tes amis devez être sur le même Wi-Fi, ou branchés sur la même box. Un réseau "
             "« invité » sépare souvent les appareils les uns des autres. Tes amis doivent aussi avoir "
             "lancé Vocal LAN.")
        c2 = card(2, "Le pare-feu Windows bloque peut-être Vocal LAN",
                  "C'est la cause la plus fréquente. Si tu as refusé la demande de Windows au premier "
                  "lancement, plus personne ne peut te trouver. Ce bouton règle ça en une fois.")
        ctk.CTkButton(c2, text="Autoriser Vocal LAN dans le pare-feu", height=38, fg_color=ACCENT,
                      hover_color=ACCENT_HOVER, text_color="#0b1020", font=font(13, "bold"),
                      command=app.ask_firewall).pack(anchor="w", padx=14, pady=(0, 6))
        self.fw_status = Label(c2, text="", anchor="w", justify="left", wraplength=420,
                                      font=font(12))
        ctk.CTkFrame(c2, height=6, fg_color="transparent").pack()
        card(3, "Ton casque et ton micro",
             "Vérifie que ton casque est branché et que les boutons Micro et Casque ne sont pas "
             "rouges. Dans les réglages, « Je m'entends » te permet de tester ton micro.")
        c4 = card(4, "Toujours rien ? Ajoute ton ami par son adresse",
                  "Demande-lui de cliquer sur « Copier mon IP » dans Vocal LAN, puis écris-la ici.")
        AddIpRow(c4, app).pack(fill="x", padx=14, pady=(0, 6))
        mine = ctk.CTkFrame(c4, fg_color="transparent")
        mine.pack(fill="x", padx=14, pady=(2, 14))
        self.ip_lbl = Label(mine, text="", anchor="w", font=font(12), text_color=TEXT_MUTED)
        self.ip_lbl.pack(side="left")
        ctk.CTkButton(mine, text="Copier mon IP", width=110, height=28, fg_color=SURFACE_3,
                      hover_color=BORDER, font=font(12), command=app.copy_ip).pack(side="right")
        self.set_ip(app.last_snap.local_ip if app.last_snap else "")
        ctk.CTkFrame(body, height=24, fg_color="transparent").pack()      # le bas n'est jamais collé

    def set_ip(self, ip: str):
        self.ip_lbl.configure(text=f"Ton adresse : {ip}" if ip else "")

    def set_fw_status(self, text: str, color: str = TEXT_MUTED):
        self.fw_status.configure(text=text, text_color=color)
        self.fw_status.pack(fill="x", padx=14, pady=(0, 12))


# ===========================================================================
# Sélecteurs partagés (réglages + assistant)
# ===========================================================================
class DevicePicker:
    """Menus Micro / Casque (+ bouton Actualiser) reliés à engine.list_devices / set_devices.

    Rien ne bloque l'interface : la liste est lue sur un thread, ``set_devices`` (0,2 à 0,5 s)
    aussi, et « Actualiser » attend l'événement ``devices_changed`` du moteur.
    """

    def __init__(self, app, parent, label_width=70, with_refresh=True, menu_width=210, on_error=None):
        self.app = app
        self.on_error = on_error      # callable(texte | None) : l'assistant affiche l'erreur chez lui
        self.menus: dict = {}
        self._maps: dict = {}
        self.busy = False
        self.refreshing = False
        self._refresh_job = None
        self._load_id = 0
        for key, label in (("in", "Micro"), ("out", "Casque")):
            row = ctk.CTkFrame(parent, fg_color="transparent")
            row.pack(fill="x", pady=3)
            Label(row, text=label, width=label_width, anchor="w", font=font(13)).pack(side="left")
            m = ctk.CTkOptionMenu(row, values=[DEFAULT_DEVICE], width=menu_width, height=34,
                                  dynamic_resizing=False, fg_color=SURFACE_3, button_color=BORDER,
                                  button_hover_color=SURFACE_3, dropdown_fg_color=SURFACE_2,
                                  dropdown_hover_color=SURFACE_3, text_color=TEXT,
                                  dropdown_text_color=TEXT, font=font(12), dropdown_font=font(12),
                                  anchor="w", command=lambda _v: self._changed())
            m.pack(side="left", fill="x", expand=True)
            self.menus[key] = m
            if key == "in" and with_refresh:
                self.refresh_btn = ctk.CTkButton(row, text="Actualiser", width=84, height=34,
                                                 fg_color=SURFACE_3, hover_color=BORDER, font=font(12),
                                                 command=self.refresh)
                self.refresh_btn.pack(side="right", padx=(8, 0))
            elif key == "out" and with_refresh:
                ctk.CTkFrame(row, width=84, height=1, fg_color="transparent").pack(side="right",
                                                                                   padx=(8, 0))
        self.status = Label(parent, text="", anchor="w", font=font(12), text_color=ACCENT_HOVER)
        self._show_current()
        self.reload()

    # -- affichage ---------------------------------------------------------------
    def alive(self) -> bool:
        try:
            return bool(self.menus["in"].winfo_exists())
        except Exception:  # noqa: BLE001
            return False

    def _show_current(self):
        """Affichage immédiat (sans interroger le moteur) : le choix enregistré, ou « par défaut »."""
        s = self.app.settings
        for key, cur in (("in", s.dev_in), ("out", s.dev_out)):
            self._maps[key] = {DEFAULT_DEVICE: None, **({cur: cur} if cur else {})}
            self.menus[key].configure(values=list(self._maps[key]))
            self.menus[key].set(DEFAULT_DEVICE if cur is None else cur)

    def _choices(self, names, current):
        mp = {DEFAULT_DEVICE: None}
        for n in names:
            mp[n] = n
        if current and current not in names:
            mp[f"{current} (absent)"] = current
        return mp

    def _set_busy(self, text: str = ""):
        self.busy = bool(text)
        for m in self.menus.values():
            m.configure(state="disabled" if text else "normal")
        if hasattr(self, "refresh_btn"):
            self.refresh_btn.configure(state="disabled" if text else "normal")
        if text:
            self.status.configure(text=text)
            self.status.pack(fill="x", pady=(2, 0))
        else:
            self.status.pack_forget()

    # -- liste des appareils -------------------------------------------------------
    def reload(self):
        """Relit la liste sur un thread (le moteur peut être occupé à rouvrir le son)."""
        self._load_id += 1
        mine = self._load_id
        self.app.run_bg(self.app.engine.list_devices,
                        lambda res, exc: self._loaded(mine, res, exc))

    def _loaded(self, load_id, res, exc):
        if load_id != self._load_id or not self.alive():
            return
        if exc is not None or not res:
            if exc is not None:
                log.warning("list_devices : %s", exc)
            res = ([], [])
        ins, outs = res
        s = self.app.settings
        for key, names, cur in (("in", ins, s.dev_in), ("out", outs, s.dev_out)):
            mp = self._maps[key] = self._choices(names, cur)
            m = self.menus[key]
            m.configure(values=list(mp))
            m.set(DEFAULT_DEVICE if cur is None else (cur if cur in names else f"{cur} (absent)"))
        if self.refreshing:
            self._end_refresh()

    def refresh(self):
        """« Actualiser » : le moteur relit la liste en arrière-plan puis envoie ``devices_changed``."""
        if self.busy or self.refreshing:
            return
        try:
            self.app.engine.refresh_devices()
        except Exception:  # noqa: BLE001
            log.exception("refresh_devices")
            self.app.toast("Impossible d'actualiser la liste pour l'instant")
            return
        self.refreshing = True
        self._set_busy("Actualisation de la liste…")
        self._refresh_job = self.app.after(6000, self._end_refresh)      # filet de sécurité

    def on_devices_changed(self):
        """Appelé par l'application à la réception de l'événement ``devices_changed``."""
        self.reload()

    def _end_refresh(self):
        if self._refresh_job is not None:
            try:
                self.app.after_cancel(self._refresh_job)
            except Exception:  # noqa: BLE001
                pass
            self._refresh_job = None
        was = self.refreshing
        self.refreshing = False
        if self.alive() and self.busy:
            self._set_busy("")
            if was:
                self.reload()

    # -- changement de périphérique --------------------------------------------------
    def _changed(self):
        if self.busy:
            return
        din = self._maps["in"].get(self.menus["in"].get())
        dout = self._maps["out"].get(self.menus["out"].get())
        s = self.app.settings
        prev = (s.dev_in, s.dev_out)
        if (din, dout) == prev:
            return
        self._set_busy("Changement en cours…")
        what = "micro" if din != prev[0] else "casque"
        self.app.run_bg(lambda: self.app.engine.set_devices(din, dout),
                        lambda res, exc: self._switched(prev, res, exc, what))

    def _switched(self, prev, info, exc, what="appareil"):
        ok = exc is None and (info is None or info.ok)
        if ok:
            self._error(None)
            self._done_switch()
            return
        msg = (getattr(info, "error_fr", None) if info is not None else None) or "Cet appareil ne marche pas."
        if exc is not None:
            log.warning("set_devices : %s", exc)
        # on remet l'ancien choix pour ne pas laisser l'utilisateur sans son
        if self.alive():
            self.status.configure(text="Retour à l'ancien appareil…")
        self.app.run_bg(lambda: self.app.engine.set_devices(*prev),
                        lambda res, e2: self._reverted(msg, res, e2, what))

    def _reverted(self, msg, info, exc, what="appareil"):
        back = exc is None and (info is None or info.ok)
        text = f"{msg} L'ancien appareil a été remis." if back else msg
        self._error(with_advice(text, what))
        self._done_switch()

    def _error(self, text):
        """Erreur de périphérique : dans l'assistant (qui recouvre les bandeaux) ou dans la fenêtre."""
        if self.on_error is not None:
            self.on_error(text)
        elif text:
            self.app.banners.show("audio_pick", text, "error")
        else:
            self.app.banners.hide("audio_pick")

    def _done_switch(self):
        if self.alive():
            self._set_busy("")
            self.reload()


class KeyPicker(ctk.CTkFrame):
    """Bouton « touche » : un clic puis on appuie sur la touche voulue (capture globale)."""

    def __init__(self, master, app, field: str, empty="Aucune"):
        super().__init__(master, fg_color="transparent")
        self.app, self.field, self.empty = app, field, empty
        self.btn = ctk.CTkButton(self, text="", width=130, height=32, fg_color=SURFACE_3,
                                 hover_color=BORDER, font=font(13, "bold"), command=self._click)
        self.btn.pack(side="left")
        self.clear = ctk.CTkButton(self, text="✕", width=30, height=32, fg_color="transparent",
                                   hover_color=SURFACE_3, text_color=TEXT_MUTED, font=font(12),
                                   command=self._clear)
        self.clear.pack(side="left", padx=(4, 0))
        Tooltip(self.clear, "Retirer cette touche")
        self.refresh()

    def refresh(self):
        code = getattr(self.app.settings, self.field, None)
        self.btn.configure(text=key_label(code) or self.empty, fg_color=SURFACE_3,
                           text_color=TEXT if code else TEXT_MUTED)
        if code:
            self.clear.pack(side="left", padx=(4, 0))
        else:
            self.clear.pack_forget()

    def _click(self):
        self.btn.configure(text="Appuie sur une touche…", fg_color=ACCENT, text_color="#0b1020")
        self.app.capture_key(self._got)

    _FIELD_NAMES = {"ptt_key": "appuyer pour parler", "mute_key": "couper le micro",
                    "deafen_key": "couper le casque"}
    _RISKY = {"space", "enter", "tab", "backspace"}

    def destroy(self):
        # une capture en cours viserait un widget détruit (fermeture des réglages, fin de l'assistant)
        try:
            if getattr(self.app, "_capture_cb", None) == self._got:
                self.app.cancel_capture()
        except Exception:  # noqa: BLE001
            pass
        super().destroy()

    def _got(self, code):
        try:
            if not self.winfo_exists():
                return
        except Exception:  # noqa: BLE001
            return
        if code:
            s = self.app.settings
            other = next((n for f, n in self._FIELD_NAMES.items()
                          if f != self.field and getattr(s, f, None) == code), None)
            if other:
                self.app.toast(f"Cette touche sert déjà à « {other} ». Choisis-en une autre.", 5)
            else:
                self._set(code)
                if len(code) == 1 or code in self._RISKY:
                    self.app.toast("Attention : cette touche agira aussi quand tu écris. "
                                   "Une touche F ou un bouton de souris est plus sûr.", 6)
        self.refresh()

    def _clear(self):
        self._set(None)
        self.refresh()

    def _set(self, code):
        setattr(self.app.settings, self.field, code)
        self.app.settings.dirty = True
        try:
            self.app.hotkeys.apply()
        except Exception:  # noqa: BLE001
            log.exception("hotkeys.apply")


# ===========================================================================
# Fenêtre de réglages
# ===========================================================================
class SettingsWindow(Dialog, Diff):
    def __init__(self, app):
        Dialog.__init__(self, app, "Réglages · Vocal LAN", 580, 780, minsize=(520, 560))
        self._init_diff()
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<Escape>", lambda _e: self.close())
        s, eng = app.settings, app.engine
        snap = app.last_snap or eng.snapshot()
        body = self.body = ctk.CTkScrollableFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=10, pady=(10, 0))
        head = ctk.CTkFrame(body, fg_color="transparent")
        head.pack(fill="x", padx=8, pady=(4, 0))
        Label(head, text="Réglages", font=font(22, "bold")).pack(side="left", padx=4)

        # ---- Profil ----
        card = self._section("Profil")
        row = self._row(card, "Pseudo")
        self.name_entry = ctk.CTkEntry(row, height=34, fg_color=SURFACE_2, border_color=BORDER,
                                       font=font(13))
        self.name_entry.pack(side="left", fill="x", expand=True)
        self.name_entry.insert(0, s.name or snap.name)
        self.name_entry.bind("<Return>", lambda _e: self._commit_name(True))
        self.name_entry.bind("<FocusOut>", lambda _e: self._commit_name(False))
        self.name_msg = Label(card, text="Validé avec Entrée ou en cliquant ailleurs.",
                                     anchor="w", font=font(11), text_color=TEXT_DIM)
        self.name_msg.pack(fill="x", padx=16, pady=(0, 12))

        # ---- Audio ----
        card = self._section("Audio")
        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.pack(fill="x", padx=16, pady=(12, 4))
        self.devices = DevicePicker(app, inner)
        Label(card, text="Volume du casque", anchor="w", font=font(13, "bold")).pack(
            fill="x", padx=16, pady=(12, 2))
        self.master_slider = SliderRow(card, 0, 2, 40, pct, lambda v: eng.set_master(v), value=s.master)
        self.master_slider.pack(fill="x", padx=16, pady=(0, 14))

        # ---- Ma voix (2.1) ----
        card = self._section("Ma voix")
        self._build_voice(card)

        # ---- Touches ----
        card = self._section("Touches")
        self.ptt_var = ctk.IntVar(value=1 if s.ptt_mode else 0)
        for txt, val in (("Micro toujours ouvert", 0), ("Appuyer pour parler", 1)):
            ctk.CTkRadioButton(card, text=txt, variable=self.ptt_var, value=val, font=font(13),
                               fg_color=ACCENT, hover_color=ACCENT_HOVER, border_color=TEXT_DIM,
                               command=self._ptt_changed).pack(anchor="w", padx=16, pady=(12 if val == 0 else 6, 0))
        self.key_pickers = []
        for label, field, empty in (("Touche pour parler", "ptt_key", "Choisir…"),
                                    ("Couper / rétablir le micro", "mute_key", "Aucune"),
                                    ("Couper / rétablir le casque", "deafen_key", "Aucune")):
            row = self._row(card, label, label_width=210)
            kp = KeyPicker(row, app, field, empty)
            kp.pack(side="left")
            self.key_pickers.append(kp)
        Label(card, anchor="w", justify="left", wraplength=480, font=font(11), text_color=TEXT_DIM,
                     text="Ces touches marchent aussi quand tu es dans un jeu. Dans la fenêtre, "
                          "Ctrl+M coupe ou rétablit le micro.").pack(fill="x", padx=16, pady=(2, 14))

        # ---- Fenêtre ----
        card = self._section("Fenêtre")
        self.switches = {}
        ui = s.ui
        for key, txt in (("close_to_tray", "Réduire près de l'horloge à la fermeture"),
                         ("always_on_top", "Toujours au premier plan"),
                         ("start_with_windows", "Démarrer avec Windows"),
                         ("sounds", "Sons quand quelqu'un arrive ou part"),
                         ("notifications", "Notification quand quelqu'un arrive ou part")):
            value = bool(getattr(ui, key, False))
            if key == "start_with_windows":      # l'état réel (registre) fait foi
                try:
                    import netutil  # type: ignore
                    value = bool(netutil.is_autostart())
                except Exception:  # noqa: BLE001
                    pass
            var = ctk.BooleanVar(value=value)
            sw = ctk.CTkSwitch(card, text=txt, variable=var, font=font(13), progress_color=ACCENT,
                               button_color="#e8edff", fg_color=SURFACE_3,
                               command=lambda k=key, v=var: self._switch(k, v))
            sw.pack(anchor="w", padx=16, pady=(12 if key == "close_to_tray" else 6, 0))
            self.switches[key] = var
        ctk.CTkFrame(card, height=10, fg_color="transparent").pack()

        # ---- Avancé (replié) ----
        self.adv_open = bool(getattr(ui, "show_advanced", False))
        self.adv_btn = ctk.CTkButton(body, text="", anchor="w", height=36, fg_color=SURFACE,
                                     hover_color=SURFACE_2, font=font(13, "bold"), corner_radius=12,
                                     command=self._toggle_adv)
        self.adv_btn.pack(fill="x", padx=2, pady=(16, 0))
        self.adv = ctk.CTkFrame(body, fg_color=SURFACE, corner_radius=14)
        self._build_voice_advanced(self.adv)
        Label(self.adv, text="Gain du micro", anchor="w", font=font(13, "bold")).pack(
            fill="x", padx=16, pady=(14, 2))
        self.gain = SliderRow(self.adv, 0, 2, 40, pct, lambda v: eng.set_mic_gain(v), value=s.mic_gain)
        self.gain.pack(fill="x", padx=16)
        Label(self.adv, text="À augmenter seulement si on t'entend trop faiblement.",
                     anchor="w", font=font(11), text_color=TEXT_DIM).pack(fill="x", padx=16)
        stab = Label(self.adv, text="Stabilité du son", anchor="w", font=font(13, "bold"))
        stab.pack(fill="x", padx=16, pady=(14, 4))
        Tooltip(stab, "Si le son hache ou se coupe, choisis Stable.")
        self._modes = {"Auto": "auto", "Réactif": "reactive", "Stable": "stable"}
        self.mode_seg = ctk.CTkSegmentedButton(self.adv, values=list(self._modes), height=34,
                                               font=font(12), selected_color=ACCENT,
                                               selected_hover_color=ACCENT_HOVER,
                                               unselected_color=SURFACE_3,
                                               unselected_hover_color=BORDER, fg_color=SURFACE_3,
                                               text_color=TEXT, command=self._mode_changed)
        self.mode_seg.pack(anchor="w", padx=16)
        Label(self.adv, anchor="w", justify="left", wraplength=480, font=font(11),
                     text_color=TEXT_DIM,
                     text="Si le son hache, choisis Stable. Auto convient presque toujours ; "
                          "Réactif réduit le délai.").pack(fill="x", padx=16, pady=(4, 0))
        Label(self.adv, text="Ajouter quelqu'un par son adresse IP", anchor="w",
                     font=font(13, "bold")).pack(fill="x", padx=16, pady=(16, 4))
        AddIpRow(self.adv, app).pack(fill="x", padx=16)
        mine = ctk.CTkFrame(self.adv, fg_color="transparent")
        mine.pack(fill="x", padx=16, pady=(4, 0))
        self.ip_lbl = Label(mine, text="", anchor="w", font=font(12), text_color=TEXT_MUTED)
        self.ip_lbl.pack(side="left")
        ctk.CTkButton(mine, text="Copier mon IP", width=110, height=28, fg_color=SURFACE_3,
                      hover_color=BORDER, font=font(12), command=app.copy_ip).pack(side="right")
        jbtn = ctk.CTkButton(self.adv, text="Ouvrir le dossier du journal", height=34,
                             fg_color=SURFACE_3, hover_color=BORDER, font=font(12),
                             command=lambda: open_folder(app_dir()))
        jbtn.pack(anchor="w", padx=16, pady=(14, 16))
        Tooltip(jbtn, "Le journal est un fichier texte qui aide à comprendre un problème.")
        self._apply_adv()

        # ---- À propos / Aide ----
        card = self._section("À propos et aide")
        top = ctk.CTkFrame(card, fg_color="transparent")
        top.pack(fill="x", padx=16, pady=(14, 4))
        img = logo_image(44)
        if img:
            Label(top, text="", image=img).pack(side="left")
        col = ctk.CTkFrame(top, fg_color="transparent")
        col.pack(side="left", padx=12)
        Label(col, text="Vocal LAN", font=font(16, "bold"), anchor="w").pack(fill="x")
        Label(col, text=f"Version {VERSION}", font=font(12), text_color=TEXT_MUTED,
                     anchor="w").pack(fill="x")
        Label(card, anchor="w", justify="left", wraplength=480, font=font(12),
                     text_color=TEXT_MUTED,
                     text="Chat vocal pour réseau local : pas de serveur, pas de compte, et un délai "
                          "le plus faible possible.").pack(fill="x", padx=16, pady=(4, 8))
        ctk.CTkButton(card, text="Ça ne marche pas ?", height=36, fg_color=SURFACE_3, hover_color=BORDER,
                      font=font(13), command=app.open_help).pack(anchor="w", padx=16, pady=(0, 16))
        ctk.CTkFrame(body, height=12, fg_color="transparent").pack()

        foot = ctk.CTkFrame(self, fg_color=BG)
        foot.pack(fill="x", padx=16, pady=12)
        ctk.CTkButton(foot, text="Fermer", width=110, height=36, fg_color=ACCENT, hover_color=ACCENT_HOVER,
                      text_color="#0b1020", font=font(13, "bold"), command=self.close).pack(side="right")
        self.update_from(snap)

    # -- Ma voix ---------------------------------------------------------------
    def _build_voice(self, card):
        app = self.app
        Label(card, text="Suppression du bruit", anchor="w", font=font(13, "bold")).pack(
            fill="x", padx=16, pady=(14, 6))
        self.nr_seg = make_seg(card, [lbl for _, lbl in DENOISE_LEVELS], self._nr_changed)
        self.nr_seg.pack(anchor="w", padx=16)
        self.nr_desc = Label(card, anchor="w", justify="left", wraplength=480, font=font(12),
                             text_color=TEXT_MUTED, text="")
        self.nr_desc.pack(fill="x", padx=16, pady=(6, 0))
        self.nr_note = Label(card, anchor="w", justify="left", wraplength=480, font=font(11),
                             text_color=ORANGE, text="")
        Label(card, text="Style de voix", anchor="w", font=font(13, "bold")).pack(
            fill="x", padx=16, pady=(16, 6))
        self.style_seg = make_seg(card, [lbl for _, lbl in STYLE_LABELS], self._style_changed)
        self.style_seg.pack(anchor="w", padx=16)
        self.style_desc = Label(card, anchor="w", justify="left", wraplength=480, font=font(12),
                                text_color=TEXT_MUTED, text="")
        self.style_desc.pack(fill="x", padx=16, pady=(6, 0))
        # --- délai honnête ---
        box = ctk.CTkFrame(card, fg_color=SURFACE_2, corner_radius=12)
        box.pack(fill="x", padx=16, pady=(14, 0))
        self.delay_lbl = Label(box, anchor="w", justify="left", wraplength=450, font=font(13, "bold"),
                               text="", pad=70)
        self.delay_lbl.pack(fill="x", padx=12, pady=(10, 0))
        self.delay_sub = Label(box, anchor="w", justify="left", wraplength=450, font=font(11),
                               text_color=TEXT_DIM, text="", pad=70)
        self.delay_sub.pack(fill="x", padx=12, pady=(0, 10))
        # --- je m'entends + A/B + double barre ---
        Label(card, text="Écoute-toi", anchor="w", font=font(13, "bold")).pack(
            fill="x", padx=16, pady=(16, 6))
        line = ctk.CTkFrame(card, fg_color="transparent")
        line.pack(fill="x", padx=16)
        self.monitor_btn = ctk.CTkButton(line, text="Je m'entends (15 s)", height=36, fg_color=SURFACE_3,
                                         hover_color=BORDER, font=font(13), command=app.toggle_monitor)
        self.monitor_btn.pack(side="left")
        self.ab_seg = make_seg(line, ["Sans filtre", "Avec filtre"], self._ab_changed, height=36)
        self.ab_seg.pack(side="left", padx=(12, 0))
        self.ab_seg.set("Avec filtre")
        self.ab_seg.configure(state="disabled")
        for attr, text in (("meter_in", "Avant"), ("meter_out", "Après")):
            r = ctk.CTkFrame(card, fg_color="transparent")
            r.pack(fill="x", padx=16, pady=(8 if attr == "meter_in" else 4, 0))
            Label(r, text=text, width=50, anchor="w", font=font(12), text_color=TEXT_MUTED).pack(side="left")
            m = Meter(r, height=14, bg=SURFACE)
            m.pack(side="left", fill="x", expand=True)
            setattr(self, attr, m)
        self.mon_warn = Label(card, anchor="w", justify="left", wraplength=480, font=font(12, "bold"),
                              text_color=ORANGE, text="")           # affichée seulement si personne ne t'entend
        self.ab_hint = Label(card, anchor="w", justify="left", wraplength=480, font=font(11),
                             text_color=TEXT_DIM, text="")
        self.ab_hint.pack(fill="x", padx=16, pady=(6, 0))
        Label(card, anchor="w", justify="left", wraplength=480, font=font(11), text_color=ORANGE,
              text="Mets ton casque avant : sans casque, tu auras de l'écho.").pack(
            fill="x", padx=16, pady=(2, 14))

    def _build_voice_advanced(self, adv):
        app, eng = self.app, self.app.engine
        s = app.settings
        Label(adv, text="Ma voix, réglages fins", anchor="w", font=font(14, "bold")).pack(
            fill="x", padx=16, pady=(14, 2))
        self.fx_vars = {}

        def switch(key, text, hint, cmd):
            var = ctk.BooleanVar(value=bool(getattr(s, key, True)))
            sw = ctk.CTkSwitch(adv, text=text, variable=var, font=font(13), progress_color=ACCENT,
                               button_color="#e8edff", fg_color=SURFACE_3, command=lambda: cmd(var))
            sw.pack(anchor="w", padx=16, pady=(10, 0))
            Label(adv, text=hint, anchor="w", justify="left", wraplength=480, font=font(11),
                  text_color=TEXT_DIM).pack(fill="x", padx=16, pady=(2, 0))
            self.fx_vars[key] = var
            return sw

        switch("fx_highpass", "Couper les grondements graves",
               "Enlève le « boum » du bureau, des pas ou du souffle dans le micro.",
               lambda v: app.eng_call("set_fx", highpass=bool(v.get())))
        row = ctk.CTkFrame(adv, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(6, 0))
        Label(row, text="Force", width=50, anchor="w", font=font(12), text_color=TEXT_MUTED).pack(side="left")
        self.hp_seg = make_seg(row, ["Douce", "Forte"], self._hp_changed, height=30)
        self.hp_seg.pack(side="left")
        self.hp_seg.set("Douce")
        Label(adv, text="Filtre de bruit manuel", anchor="w", font=font(13, "bold")).pack(
            fill="x", padx=16, pady=(16, 4))
        self.meter = Meter(adv, height=16, bg=SURFACE)
        self.meter.pack(fill="x", padx=16, pady=(0, 6))
        self.gate = SliderRow(adv, -70, -20, 50, self._gate_txt, lambda v: eng.set_gate_db(v),
                              value=s.gate_db)
        self.gate.pack(fill="x", padx=16)
        self.gate.value_lbl.configure(width=84)
        self.gate_hint = Label(adv, anchor="w", justify="left", wraplength=480, font=font(11),
                               text_color=TEXT_DIM, text="")
        self.gate_hint.pack(fill="x", padx=16, pady=(2, 0))
        self._gate_idle = None
        switch("fx_limiter", "Éviter que ta voix sature",
               "Retient les éclats de voix pour qu'ils ne grésillent pas chez tes amis.",
               lambda v: app.eng_call("set_fx", limiter=bool(v.get())))
        switch("recv_leveler", "Égaliser le volume des amis",
               "Remonte un peu ceux qu'on entend mal, baisse ceux qui crient. Un ami bruyant n'est pas "
               "remonté.",
               lambda v: app.eng_call("set_fx", recv_leveler=bool(v.get())))
        self.reset_btn = ctk.CTkButton(adv, text="Tout désactiver", height=32, fg_color=SURFACE_3,
                                       hover_color=BORDER, font=font(12), command=self._disable_all)
        self.reset_btn.pack(anchor="w", padx=16, pady=(14, 0))
        Label(adv, text="Ta voix part brute, comme dans la version 2.0 : aucun traitement, aucun délai. "
                        "Le volume des amis n'est pas touché.",
              anchor="w", justify="left", wraplength=480, font=font(11), text_color=TEXT_DIM).pack(
            fill="x", padx=16, pady=(2, 4))

    def _nr_changed(self, label):
        level = DENOISE_FROM_LABEL.get(label, "off")
        self.app.eng_call("set_denoise_level", level)
        if level != "off":
            self.app._last_denoise = level

    def _style_changed(self, label):
        style = next((k for k, v in STYLE_LABELS if v == label), "natural")
        self.app.eng_call("set_voice_style", style)

    def _ab_changed(self, label):
        self.app.eng_call("set_monitor_processed", label == "Avec filtre")

    def _hp_changed(self, label):
        self.app.eng_call("set_fx", highpass_hz=120 if label == "Forte" else 80)

    def _disable_all(self):
        self.app.eng_call("disable_all_processing")
        self.app.eng_call("set_gate_db", -70.0)        # le filtre manuel aussi : « tout » veut dire tout
        self.app.toast("Tous les traitements de ta voix sont coupés.")

    # -- structure -----------------------------------------------------------
    def _section(self, title):
        Label(self.body, text=title.upper(), font=font(11, "bold"), text_color=TEXT_DIM,
                     anchor="w").pack(fill="x", padx=8, pady=(16, 4))
        card = ctk.CTkFrame(self.body, fg_color=SURFACE, corner_radius=14)
        card.pack(fill="x", padx=2)
        return card

    def _row(self, card, label, label_width=70):
        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(12, 2) if label == "Pseudo" else 4)
        Label(row, text=label, width=label_width, anchor="w", font=font(13)).pack(side="left")
        return row

    @staticmethod
    def _gate_txt(v):
        return "Aucun filtre" if v <= -69.5 else f"Filtre {round((v + 70) / 50 * 100)} %"

    # -- actions ---------------------------------------------------------------
    def close(self):
        self._commit_name(False)
        self.app.cancel_capture()
        self.app.settings_win = None
        self.destroy()

    def _commit_name(self, from_enter: bool):
        app = self.app
        new = self.name_entry.get().strip()[:MAX_NAME]
        cur = (app.last_snap.name if app.last_snap else app.settings.name) or ""
        if not new:
            self.name_entry.delete(0, "end")
            self.name_entry.insert(0, cur)
            return
        if new != cur:
            app.engine.set_name(new)
            self.name_msg.configure(text="Pseudo enregistré.", text_color=GREEN)
        if from_enter:
            self.focus_set()

    def _ptt_changed(self):
        on = bool(self.ptt_var.get())
        self.app.engine.set_ptt(on)
        try:
            self.app.hotkeys.apply()
        except Exception:  # noqa: BLE001
            log.exception("hotkeys.apply")
        if on and not self.app.settings.ptt_key:
            self.app.toast("Choisis la touche à maintenir pour parler")

    def _switch(self, key, var):
        val = bool(var.get())
        self.app.set_ui_pref(key, val, var)

    def _mode_changed(self, label):
        self.app.engine.set_buffer_mode(self._modes[label])

    def _toggle_adv(self):
        self.adv_open = not self.adv_open
        self.app.settings.ui.show_advanced = self.adv_open
        self.app.settings.dirty = True
        self._apply_adv()

    def _apply_adv(self):
        self.adv_btn.configure(text=("▾  " if self.adv_open else "▸  ") + "Réglages avancés")
        if self.adv_open:
            self.adv.pack(fill="x", padx=2, pady=(4, 0), after=self.adv_btn)
        else:
            self.adv.pack_forget()

    # -- synchronisation -----------------------------------------------------
    def reload_devices(self):
        self.devices.on_devices_changed()

    def refresh_keys(self):
        for kp in self.key_pickers:
            kp.refresh()

    def update_from(self, snap):
        a = self.app
        active = snap.transmitting and not snap.muted
        idle = manual_gate_idle(snap)
        self.meter.set(snap.mic_level, active, 0.0 if idle else snap.gate_level)
        self.gate.set(snap.gate_db)
        if idle != self._gate_idle:                     # une seule porte à la fois : grisé avec Normal / Fort
            self._gate_idle = idle
            self.gate.set_enabled(not idle)
            self.gate_hint.configure(
                text=("Inutile avec Normal ou Fort : la suppression du bruit fait déjà ce travail. "
                      "Ton réglage est gardé pour les réglages Aucune et Léger." if idle else
                      "À gauche, rien n'est filtré. À droite, les petits bruits (clavier, souffle) sont "
                      "baissés quand tu ne parles pas : ta voix doit dépasser le trait blanc."))
        self.master_slider.set(snap.master)
        self.gain.set(snap.mic_gain)
        self.upd(self.monitor_btn, text="Arrêter l'écoute" if snap.monitor else "Je m'entends (15 s)",
                 fg_color=ACCENT if snap.monitor else SURFACE_3,
                 text_color="#0b1020" if snap.monitor else TEXT)
        self._update_voice(snap)
        want = 1 if snap.ptt_mode else 0
        if self.ptt_var.get() != want:
            self.ptt_var.set(want)
        label = next((k for k, v in self._modes.items() if v == snap.buffer_mode), "Auto")
        if self.mode_seg.get() != label:
            self.mode_seg.set(label)
        self.upd(self.ip_lbl, text=f"Ton adresse : {snap.local_ip}" if snap.local_ip else "")
        if self.focus_get() is not getattr(self.name_entry, "_entry", None) and not self._name_has_focus():
            cur = snap.name if snap.name else ""
            if self.name_entry.get() != cur and cur:
                self.name_entry.delete(0, "end")
                self.name_entry.insert(0, cur)

    def _update_voice(self, snap):
        """Carte « Ma voix » : reflète l'état du moteur (jamais l'inverse, sauf pendant un clic)."""
        level = getattr(snap, "denoise_level", "off")
        style = getattr(snap, "voice_style", "natural")
        label = DENOISE_LABEL.get(level, "Aucune")
        if self.nr_seg.get() != label:
            self.nr_seg.set(label)
        slabel = dict(STYLE_LABELS).get(style, "Naturelle")
        if self.style_seg.get() != slabel:
            self.style_seg.set(slabel)
        d = denoise_delay(level, snap)
        self.upd(self.nr_desc, text=f"{DENOISE_TEXT.get(level, '')}" + (f"  ({delay_label(d)})" if d else ""))
        self.upd(self.style_desc, text=(STYLE_TEXT_NO_HP if style == "natural" and not getattr(snap, "fx_highpass", True)
                                        else STYLE_TEXT.get(style, "")))
        # note : suppresseur indisponible ou en panne
        note = ""
        err = getattr(snap, "denoise_error_fr", None)
        avail = getattr(self.app.engine, "denoise_available", None)
        if err and level != "off":
            note = err
        elif callable(avail) and not avail() and level in ("normal", "strong"):
            note = "Ce niveau n'est pas disponible sur cet ordinateur : ta voix part sans suppression du bruit."
        elif callable(avail) and not avail():
            note = "Les niveaux Normal et Fort ne sont pas disponibles sur cet ordinateur."
        self.upd(self.nr_note, text=note)
        if note and not self.nr_note.winfo_manager():
            self.nr_note.pack(fill="x", padx=16, pady=(4, 0), after=self.nr_desc)
        elif not note and self.nr_note.winfo_manager():
            self.nr_note.pack_forget()
        # délai honnête
        added = float(getattr(snap, "latency_added_ms", 0.0) or 0.0)
        slow = added > SLOW_AFTER_MS
        total = total_delay_ms(snap)
        calm = level in ("normal", "strong") and round(added) > 0 and not slow
        head = "Délai ajouté par tes réglages : " + (f"+{round(added)} ms" if round(added) > 0 else "aucun")
        self.upd(self.delay_lbl, text=f"{head}   (total ≈ {total} ms)",
                 text_color=ORANGE if slow else TEXT)
        self.upd(self.delay_sub,
                 text=(LAG_EXPLAIN.format(light=delay_label(denoise_delay('light', snap))) if slow else
                       (LAG_REASSURE + " Le total est une estimation : micro, casque et réseau compris.") if calm
                       else "Le total est une estimation : micro, casque et réseau compris."),
                 text_color=ORANGE if slow else TEXT_DIM)
        # écoute + A/B + double barre
        mon = snap.monitor
        self.upd(self.ab_seg, state="normal" if mon else "disabled")
        want = "Avec filtre" if getattr(snap, "monitor_processed", True) else "Sans filtre"
        if self.ab_seg.get() != want:
            self.ab_seg.set(want)
        before = snap.mic_level
        after = getattr(snap, "mic_level_out", before)
        act = snap.transmitting and not snap.muted
        denoise_on = level in ("normal", "strong") and not err
        self.meter_in.set(before, act, 0.0)
        self.meter_out.set(after, act, 0.0)
        warn = ""
        if mon and getattr(snap, "monitor_processed", True):
            if snap.muted:
                warn = MON_MUTED
            elif snap.ptt_mode and not snap.ptt_active:
                warn = MON_PTT_UP
        self.upd(self.mon_warn, text=warn)
        if warn and not self.mon_warn.winfo_manager():
            self.mon_warn.pack(fill="x", padx=16, pady=(6, 0), before=self.ab_hint)
        elif not warn and self.mon_warn.winfo_manager():
            self.mon_warn.pack_forget()
        self.upd(self.ab_hint,
                 text=(("Tape sur ton clavier : la barre « Après » doit nettement baisser par rapport à « Avant ». "
                        if denoise_on else
                        "Tape sur ton clavier et bascule Sans filtre / Avec filtre pour comparer. ")
                       + "Avec filtre : tu entends exactement ce que reçoivent tes amis "
                         "(rien si ton micro est coupé ou ta touche relâchée)."
                       if mon else "Clique sur « Je m'entends », puis tape sur ton clavier pour tester."))
        # avancé
        for key, var in self.fx_vars.items():
            v = bool(getattr(snap, key, var.get()))
            if bool(var.get()) != v:
                var.set(v)
        hz = "Forte" if getattr(snap, "fx_highpass_hz", 80) == 120 else "Douce"
        if self.hp_seg.get() != hz:
            self.hp_seg.set(hz)
        hp_on = bool(getattr(snap, "fx_highpass", True))
        self.upd(self.hp_seg, state="normal" if hp_on else "disabled")

    def _name_has_focus(self):
        try:
            w = self.focus_get()
            return w is not None and str(w).startswith(str(self.name_entry))
        except Exception:  # noqa: BLE001
            return False


# ===========================================================================
# Lignes de participants
# ===========================================================================
class PeerRow(ctk.CTkFrame, Diff):
    """Une ligne = une personne (ou moi). Créée une fois, mise à jour par petites touches."""

    def __init__(self, app, master, peer_id: str | None):
        super().__init__(master, fg_color=SURFACE, corner_radius=16,
                         border_width=1 if peer_id is None else 0, border_color=BORDER)
        self._init_diff()
        self.app = app
        self.peer_id = peer_id
        self.me = peer_id is None
        self.grid_columnconfigure(1, weight=1)
        self.avatar = Avatar(self, 48, SURFACE)
        self.avatar.grid(row=0, column=0, rowspan=2 if not self.me else 1, padx=(14, 12),
                         pady=(12, 12), sticky="n" if not self.me else "")
        info = ctk.CTkFrame(self, fg_color="transparent")
        info.grid(row=0, column=1, sticky="ew", pady=(12, 0 if not self.me else 12))
        self.name_lbl = Label(info, text="", anchor="w", font=font(15, "bold"))
        self.name_lbl.pack(fill="x")
        subrow = ctk.CTkFrame(info, fg_color="transparent")
        subrow.pack(fill="x")
        self.sub_lbl = Label(subrow, text="", anchor="w", font=font(12), text_color=TEXT_MUTED)
        self.sub_lbl.pack(side="left")
        self.sub2_lbl = Label(subrow, text="", anchor="w", font=font(12), text_color=TEXT_MUTED)
        self.sub2_lbl.pack(side="left")
        # 2e ligne (avertissement) : seulement quand il y en a un, pour ne pas allonger toutes les lignes
        self.subrow, self.subrow2 = subrow, ctk.CTkFrame(info, fg_color="transparent")
        self._line2 = False
        self.sub3_lbl = Label(self.subrow2, text="", anchor="w", font=font(12, "bold"),
                              text_color=ORANGE)
        self.sub3_lbl.pack(side="left")
        # « T'entend » (vert, discret) : sur la 1re ligne si elle est courte, sinon sur la 2e
        self.sub4_lbl = Label(info, text="", anchor="w", font=font(12), text_color=GREEN)
        self._hears_in = None
        self._name_tip = Tooltip(self.name_lbl, "")
        if not self.me:
            self.quality = QualityBars(self, SURFACE)
            self.quality.grid(row=0, column=2, padx=(6, 8), pady=(14, 0), sticky="n")
            self._q_text = ""
            Tooltip(self.quality, lambda: self._q_text)
            self.mute_btn = ctk.CTkButton(self, text="Couper pour moi", width=118, height=30,
                                          corner_radius=9, fg_color=SURFACE_3, hover_color=BORDER,
                                          font=font(12), command=self._toggle_mute)
            self.mute_btn.grid(row=0, column=3, padx=(0, 14), pady=(12, 0), sticky="n")
            self.vol = SliderRow(self, 0, 2, 40, pct, self._volume_changed, value=1.0)
            self.vol.grid(row=1, column=1, columnspan=2, sticky="ew", padx=(0, 8), pady=(2, 12))
            self.vol.value_lbl.bind("<Double-Button-1>", lambda _e: self._reset_volume(), add="+")
            Tooltip(self.vol.value_lbl, "Double-clic : remettre à 100 %")
            self.clean_btn = ctk.CTkButton(self, text="Nettoyer sa voix", width=118, height=30,
                                           corner_radius=9, fg_color=SURFACE_3, hover_color=BORDER,
                                           font=font(12), command=self._toggle_clean)
            self.clean_btn.grid(row=1, column=3, padx=(0, 14), pady=(2, 12), sticky="e")
            self._clean_tip = ""
            Tooltip(self.clean_btn, lambda: self._clean_tip)
            self._muted = False
            self._clean = False
            self._clean_idle = False
            self._clean_shown = None
            self._clean_msg = ("", 0.0, "")

    # -- actions utilisateur ---------------------------------------------------
    def _volume_changed(self, v):
        self.app.engine.set_peer_volume(self.peer_id, v)

    def _reset_volume(self):
        self.vol.set(1.0, force=True)
        self.app.engine.set_peer_volume(self.peer_id, 1.0)

    def _toggle_mute(self):
        self.app.engine.set_peer_muted(self.peer_id, not self._muted)
        self._muted = not self._muted      # retour visuel immédiat
        self._paint_mute()

    def _toggle_clean(self):
        """Bascule « Nettoyer sa voix ». Le moteur refuse au-delà de 3 voix (CLEAN_LIMIT) : message clair."""
        want = not self._clean
        try:
            fn = getattr(self.app.engine, "set_peer_clean", None)
            if fn is not None:
                fn(self.peer_id, want)
        except Exception as exc:  # noqa: BLE001 - EngineError("CLEAN_LIMIT", ...) ou autre
            msg = getattr(exc, "message_fr", None) or "Impossible de nettoyer cette voix pour l'instant."
            code = getattr(exc, "code", "")
            if code == "CLEAN_LIMIT" and not getattr(exc, "message_fr", None):
                msg = "Tu nettoies déjà 3 voix : ton PC ne suivrait pas avec une de plus."
            elif code == "DENOISE_UNAVAILABLE":      # le texte du moteur parle de « ta voix » : ici c'est celle d'un ami
                msg = ("Le nettoyage des voix n'est pas disponible sur cet ordinateur "
                       "(fichier manquant ou modifié).")
            self._clean_msg = (msg, time.monotonic() + 6.0, str(code or ""))
            self.app.toast(msg, 5)
            return
        self._clean = want                       # retour visuel immédiat (le moteur confirme au tick suivant)
        self._clean_msg = ("", 0.0, "")
        self._paint_clean()

    def _paint_clean(self):
        if self._clean and self._clean_idle:
            self.upd(self.clean_btn, text="En pause", fg_color=ORANGE_BG, hover_color="#4d3b19",
                     text_color=ORANGE)
        elif self._clean:
            self.upd(self.clean_btn, text="Voix nettoyée", fg_color=GREEN_BG, hover_color=GREEN_BG_HOVER,
                     text_color=GREEN)
        else:
            self.upd(self.clean_btn, text="Nettoyer sa voix", fg_color=SURFACE_3, hover_color=BORDER,
                     text_color=TEXT)

    def _paint_mute(self):
        if self._muted:
            self.upd(self.mute_btn, text="Rétablir", fg_color=RED_BG, hover_color=RED_BG_HOVER,
                     text_color=RED)
        else:
            self.upd(self.mute_btn, text="Couper pour moi", fg_color=SURFACE_3, hover_color=BORDER,
                     text_color=TEXT)

    # -- mise à jour -------------------------------------------------------------
    def update_me(self, snap):
        name = snap.name or "Moi"
        shown = clip(name, 20) + " (moi)"
        self.upd(self.name_lbl, text=shown)
        self._name_tip.text = name
        speaking = snap.speaking and snap.transmitting and not snap.muted
        self.avatar.set_identity(initial_of(name), color_for(self.app.settings.install_id or name),
                                 dim=snap.muted)
        self.avatar.set_speaking(speaking)
        if snap.deafened and snap.muted:
            txt, col = "Casque coupé · micro coupé aussi", RED
        elif snap.muted:
            txt, col = "Micro coupé", RED
        elif snap.deafened:
            txt, col = "Casque coupé", ORANGE
        elif snap.ptt_mode and not snap.ptt_active:
            key = key_label(self.app.settings.ptt_key)
            txt, col = (f"Maintiens {key} pour parler" if key else "Choisis une touche dans les réglages"), TEXT_MUTED
        elif speaking:
            txt, col = "Tu parles", GREEN
        else:
            txt, col = "Micro ouvert", TEXT_MUTED
        self.upd(self.sub_lbl, text=txt, text_color=col)

    def update_peer(self, pv, dup: bool = False):
        name = pv.name or "?"
        shown = clip(name, 16) + f" (.{pv.ip.rsplit('.', 1)[-1]})" if dup else clip(name, 22)
        self.upd(self.name_lbl, text=shown, text_color=TEXT_DIM if pv.muted else TEXT)
        self._name_tip.text = f"{name}  ·  {pv.ip}"
        self.avatar.set_identity(initial_of(name), color_for(pv.id or name), dim=pv.muted)
        self.avatar.set_speaking(pv.speaking and not pv.muted)
        speak = "Parle" if (pv.speaking and not pv.muted) else ""
        states = []
        if pv.remote_muted:
            states.append("Micro coupé")
        if pv.remote_deafened:
            states.append("Casque coupé")
        warn, wcol = "", ORANGE
        if not pv.hears_me:
            warn = "Ne te reçoit pas"
        if pv.muted:
            warn, wcol = "Coupé pour toi", RED
        clean = getattr(pv, "clean", None)            # None : moteur sans « Nettoyer sa voix » (2.0)
        if not warn:
            if time.monotonic() < self._clean_msg[1]:
                code = self._clean_msg[2]
                warn, wcol = (("Maximum : 3 voix nettoyées" if code == "CLEAN_LIMIT" else
                               "Nettoyage indisponible" if code == "DENOISE_UNAVAILABLE" else
                               "Nettoyage impossible"), ORANGE)
            elif clean and not getattr(pv, "clean_active", True):
                warn, wcol = "Nettoyage en pause", ORANGE
            elif clean is False and getattr(pv, "noisy", False):
                warn, wcol = "Micro bruyant : essaie « Nettoyer »", TEXT_DIM
        if clean is None:
            if self._clean_shown is not False:
                self._clean_shown = False
                self.clean_btn.grid_remove()
        else:
            if self._clean_shown is not True:
                self._clean_shown = True
                self.clean_btn.grid()
            self._clean = bool(clean)
            self._clean_idle = bool(clean) and not getattr(pv, "clean_active", True)
            self._paint_clean()
            who = clip(name, 16)
            reason = getattr(pv, "clean_reason", None)
            self._clean_tip = (((reason + "\nClique pour l'arrêter.") if reason else
                                (f"Le nettoyage de la voix de {who} est demandé mais ne tourne pas pour l'instant "
                                 "(3 voix au maximum, ou un problème). Clique pour l'arrêter."))
                               if self._clean_idle else
                               f"La voix de {who} est nettoyée dans ton casque.\n"
                               f"+{CLEAN_PEER_MS} ms sur sa voix seulement. Clique pour arrêter."
                               if clean else
                               f"Réduit le bruit de {who} dans TON casque.\n"
                               f"+{CLEAN_PEER_MS} ms sur sa voix seulement (rien ne change pour lui).")
        stxt = " · ".join(states)
        if stxt and speak:
            stxt = "  ·  " + stxt
        if not (speak or stxt):
            speak = "Connecté"
        # « Ne te reçoit pas » (orange, 2e ligne) ou « T'entend » (vert, discret)
        inline = len(speak) + len(stxt) <= 14
        hears = "T'entend" if pv.hears_me else ""
        if hears and (inline or warn):
            hears = "  ·  " + hears
        self.upd(self.sub_lbl, text=speak, text_color=GREEN if speak == "Parle" else TEXT_MUTED)
        self.upd(self.sub2_lbl, text=stxt)
        self.upd(self.sub3_lbl, text=warn, text_color=wcol)
        self.upd(self.sub4_lbl, text=hears)
        want_line2 = bool(warn or (hears and not inline))
        if want_line2 != self._line2:
            self._line2 = want_line2
            if want_line2:
                self.subrow2.pack(fill="x")
            else:
                self.subrow2.pack_forget()
        where = (self.subrow if inline else self.subrow2) if hears else None
        if where is not self._hears_in:
            self._hears_in = where
            self.sub4_lbl.pack_forget()
            if where is not None:
                self.sub4_lbl.pack(in_=where, side="left")
        self.quality.set(pv.quality)
        label = {"good": "excellente", "ok": "correcte", "bad": "mauvaise"}.get(pv.quality)
        if label:
            ping = "?" if pv.ping_ms is None else (f"{pv.ping_ms:.0f} ms" if pv.ping_ms >= 1 else "< 1 ms")
            self._q_text = (f"Connexion {label}\nDélai : {ping}\nPaquets perdus : {pv.loss_pct:.0f} %\n"
                            f"Délai ajouté pour un son fluide : {pv.buffer_ms} ms")
        else:
            self._q_text = "Qualité de la connexion en cours de mesure…"
        self._muted = pv.muted
        self._paint_mute()
        self.vol.set(pv.volume)


# ===========================================================================
# Application
# ===========================================================================
_GEO_RE = re.compile(r"^(\d+)x(\d+)(?:([+-]-?\d+)([+-]-?\d+))?$")


class _NullHotkeys:
    """Raccourcis absents (vocal.py peut passer None) : même API, ne fait rien."""

    def start(self): pass
    def stop(self): pass
    def apply(self): pass
    def capture_next(self): pass


class App(ctk.CTk, Diff):
    def __init__(self, engine, settings, hotkeys, use_tray: bool = True):
        super().__init__(fg_color=BG)
        global _SCALE
        ctk.set_appearance_mode("dark")
        try:
            _SCALE = float(self._get_window_scaling())
        except Exception:  # noqa: BLE001
            _SCALE = 1.0
        self._init_diff()
        # images et polices dépendent de la racine Tk : on repart de zéro (utile aux tests)
        _fonts.clear()
        _icon_cache.clear()
        _avatar_cache.clear()
        self.engine, self.settings = engine, settings
        self.hotkeys = hotkeys if hotkeys is not None else _NullHotkeys()
        self.last_snap = None
        self.rows: dict[str, PeerRow] = {}
        self._order: list = []
        self.settings_win: SettingsWindow | None = None
        self.help_win: HelpWindow | None = None
        self.add_ip_win: AddIpDialog | None = None
        self._copy_job = None
        self.wizard = None
        self._closing = False
        self._finalized = False
        self._last_save = 0.0
        self._geo_dirty_at: float | None = None
        self._capture_cb = None
        self._capture_deadline = 0.0
        self._expire: dict[str, float] = {}
        self._toast_job = None
        self._twm_until = 0.0
        self._err_count = 0
        self._empty_shown = None
        self._alone_hint_shown = None
        self._topmost = None
        self.tray = None
        self._bg: queue.SimpleQueue = queue.SimpleQueue()    # résultats des tâches de fond
        self._prev_state: tuple | None = None                # (micro coupé, casque coupé) au tick précédent
        self._nobody_since: float | None = None              # depuis quand aucun pair ne nous reçoit
        self._last_denoise: str | None = None                # dernier niveau choisi (pour « rétablir » en un clic)
        self._clip_until = 0.0                               # alerte « ton micro sature » maintenue 2 s
        self._nr_tip = ""

        self.title("Vocal LAN")
        self.minsize(480, 560)
        self._restore_geometry()
        self.protocol("WM_DELETE_WINDOW", self._on_close_request)
        self.bind_all("<Control-m>", self._on_ctrl_m)
        self.bind("<Configure>", self._on_configure)
        apply_window_icon(self)

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)
        self._build_header()
        self.banners = BannerStack(self)
        self.banners.frame.grid(row=1, column=0, sticky="ew", padx=16)
        self._build_list()
        self._build_bottom()
        self._build_toast()

        if use_tray:
            self.tray = _make_tray()

        if not getattr(settings.ui, "first_run_done", False):
            self._start_wizard()
        elif os.environ.get("VOCAL_LAN_MINIMIZED"):      # démarrage avec Windows : discret
            self.after(50, self._start_hidden)
        self._tick_job = self.after(TICK_MS, self._tick)

    # ------------------------------------------------------------ construction
    def _build_header(self):
        head = ctk.CTkFrame(self, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=16, pady=(14, 8))
        head.grid_columnconfigure(1, weight=1)
        logo = logo_image(40)
        Label(head, text="", image=logo, width=44).grid(row=0, column=0, rowspan=2, padx=(0, 10))
        Label(head, text="Vocal LAN", font=font(21, "bold"), anchor="w").grid(
            row=0, column=1, sticky="w")
        st = ctk.CTkFrame(head, fg_color="transparent")
        st.grid(row=1, column=1, sticky="w")
        self.dot_lbl = Label(st, text="●", font=font(11), text_color=TEXT_DIM, width=14)
        self.dot_lbl.pack(side="left")
        self.status_lbl = Label(st, text="Démarrage…", font=font(13), text_color=TEXT_MUTED,
                                       anchor="w")
        self.status_lbl.pack(side="left", padx=(2, 0))
        self._status_tip = Tooltip(st, "")
        Tooltip(self.status_lbl, lambda: self._status_tip.text)
        btns = ctk.CTkFrame(head, fg_color="transparent")
        btns.grid(row=0, column=2, rowspan=2, sticky="e")
        self.copy_btn = ctk.CTkButton(btns, text=" Copier mon IP", image=icon("copy", TEXT, 16),
                                      compound="left", height=34, width=130, corner_radius=10,
                                      fg_color=SURFACE_2, hover_color=SURFACE_3, font=font(12),
                                      command=self.copy_ip)
        self.copy_btn.pack(side="left", padx=(0, 6))
        Tooltip(self.copy_btn, "Copie ton adresse pour la donner à un ami")
        help_btn = ctk.CTkButton(btns, text="?", width=34, height=34, corner_radius=10,
                                 fg_color=SURFACE_2, hover_color=SURFACE_3, font=font(15, "bold"),
                                 command=self.open_help)
        help_btn.pack(side="left", padx=(0, 6))
        Tooltip(help_btn, "Ça ne marche pas ?")
        gear = ctk.CTkButton(btns, text="", image=icon("gear", TEXT, 18), width=34, height=34,
                             corner_radius=10, fg_color=SURFACE_2, hover_color=SURFACE_3,
                             command=self.open_settings)
        gear.pack(side="left")
        Tooltip(gear, "Réglages")

    def _build_list(self):
        self.list_holder = ctk.CTkFrame(self, fg_color="transparent")
        self.list_holder.grid(row=2, column=0, sticky="nsew", padx=10)
        self.list_holder.grid_rowconfigure(0, weight=1)
        self.list_holder.grid_columnconfigure(0, weight=1)
        self.plist = ctk.CTkScrollableFrame(self.list_holder, fg_color="transparent", corner_radius=0)
        self.plist.grid(row=0, column=0, sticky="nsew")
        self.me_row = PeerRow(self, self.plist, None)

        e = self.empty = ctk.CTkFrame(self.plist, fg_color="transparent")
        Label(e, text="", image=icon("head", TEXT_DIM, 44)).pack(pady=(34, 8))
        self.empty_title = Label(e, text="Personne d'autre pour l'instant", font=font(18, "bold"))
        self.empty_title.pack()
        self.empty_text = Label(
            e, text="Tes amis doivent lancer Vocal LAN sur le même Wi-Fi ou le même réseau que toi.",
            font=font(13), text_color=TEXT_MUTED, wraplength=380, justify="center")
        self.empty_text.pack(pady=(6, 14))
        row = ctk.CTkFrame(e, fg_color="transparent")
        row.pack()
        ctk.CTkButton(row, text="Ça ne marche pas ?", height=38, fg_color=SURFACE_3, hover_color=BORDER,
                      font=font(13), command=self.open_help).pack(side="left", padx=4)
        ctk.CTkButton(row, text="Ajouter par IP", height=38, fg_color=SURFACE_3, hover_color=BORDER,
                      font=font(13), command=self.open_add_ip).pack(side="left", padx=4)
        self.alone_hint = ctk.CTkFrame(e, fg_color=ORANGE_BG, corner_radius=14, border_width=1,
                                       border_color=ORANGE)
        Label(self.alone_hint, anchor="w", justify="left", wraplength=360, font=font(12),
                     text_color="#ffe0a8",
                     text="Toujours personne ? Le pare-feu de Windows bloque peut-être Vocal LAN.").pack(
            fill="x", padx=14, pady=(12, 6))
        ctk.CTkButton(self.alone_hint, text="Voir l'aide du pare-feu", height=34, fg_color=ORANGE,
                      hover_color="#ffc562", text_color="#10131a", font=font(12, "bold"),
                      command=self.open_help).pack(anchor="w", padx=14, pady=(0, 12))

    def _build_bottom(self):
        wrap = ctk.CTkFrame(self, fg_color="transparent")
        wrap.grid(row=3, column=0, sticky="ew", padx=16, pady=(6, 14))
        wrap.grid_columnconfigure(0, weight=1)
        self.alert = BannerStack(wrap, side_pady=(0, 8))
        self.alert.frame.grid(row=0, column=0, sticky="ew")
        bar = self.bar = ctk.CTkFrame(wrap, fg_color=SURFACE, corner_radius=18)
        bar.grid(row=1, column=0, sticky="ew")
        bar.grid_columnconfigure((0, 1), weight=1, uniform="tg")
        top = ctk.CTkFrame(bar, fg_color="transparent")
        top.grid(row=0, column=0, columnspan=2, sticky="ew", padx=16, pady=(14, 0))
        Label(top, text="Ton micro", font=font(12, "bold"), text_color=TEXT_MUTED).pack(side="left")
        self.hint_lbl = Label(top, text="", font=font(12), text_color=TEXT_MUTED, anchor="e")
        self.hint_lbl.pack(side="right")
        # puce « Bruit réduit » : un clic coupe ou rétablit la suppression du bruit, même en pleine partie
        self.nr_chip = ctk.CTkButton(top, text="Réduire le bruit", width=10, height=24, corner_radius=12,
                                     font=font(11, "bold"), fg_color=SURFACE_3, hover_color=BORDER,
                                     text_color=TEXT_MUTED, command=self.toggle_denoise)
        Tooltip(self.nr_chip, lambda: self._nr_tip)
        self._nr_chip_shown = False
        self.meter = Meter(bar, height=16, bg=SURFACE)
        self.meter.grid(row=1, column=0, columnspan=2, sticky="ew", padx=16, pady=(6, 12))
        Tooltip(self.meter, "Le trait blanc est le seuil du filtre : tant que la barre reste en dessous, "
                            "on ne t'entend pas.")
        self.mic_btn = BigToggle(bar, self.toggle_mic)
        self.mic_btn.grid(row=2, column=0, sticky="nsew", padx=(14, 6), pady=(0, 14))
        self.deaf_btn = BigToggle(bar, self.toggle_deaf)
        self.deaf_btn.grid(row=2, column=1, sticky="nsew", padx=(6, 14), pady=(0, 14))
        self._mic_tip = self._deaf_tip = ""
        for btn, attr in ((self.mic_btn, "_mic_tip"), (self.deaf_btn, "_deaf_tip")):
            for w in (btn, btn.icon_lbl, btn.title_lbl, btn.hint_lbl):
                Tooltip(w, lambda a=attr: getattr(self, a))

    def _build_toast(self):
        # Le toast s'affiche sur la ligne « Ton micro » de la barre du bas (rien d'important dessous) :
        # il ne recouvre ni les participants ni les boutons.
        self.toast_lbl = Label(self.bar, text="", corner_radius=10, fg_color=SURFACE_3,
                               text_color=TEXT, font=font(12), padx=12, pady=3, wraplength=380,
                               pad=70)

    def _start_wizard(self):
        from wizard import Wizard
        self.wizard = Wizard(self, self.engine, self.settings, self.hotkeys, self._wizard_done)
        self.wizard.place(x=0, y=0, relwidth=1, relheight=1)
        self.wizard.lift()

    def _start_hidden(self):
        try:
            if self.tray and self.tray.available:
                self.withdraw()
            else:
                self.iconify()
        except Exception:  # noqa: BLE001
            pass

    def go_online(self):
        """Ouvre le réseau (fin de l'assistant ou « Réessayer »). Une erreur (port pris...) reste visible."""
        try:
            self.engine.go_online()
        except Exception as exc:  # noqa: BLE001
            log.warning("go_online : %s", exc)
            msg = getattr(exc, "message_fr", None) or "Le réseau n'a pas pu être ouvert."
            self.banners.show("net_open", msg, "error", "Réessayer", self.go_online, closable=True)
            return False
        self.banners.hide("net_open")
        return True

    def _wizard_done(self):
        w, self.wizard = self.wizard, None
        if w is not None:
            w.destroy()
        self.toast("Tout est prêt !")

    # ------------------------------------------------------------ géométrie
    def _restore_geometry(self):
        geo = getattr(self.settings.ui, "geometry", None)
        w, h, pos = 540, 720, ""
        m = _GEO_RE.match(geo or "")
        if m:
            w, h = max(480, int(m.group(1))), max(560, int(m.group(2)))
            if m.group(3) is not None:
                x, y = int(m.group(3)), int(m.group(4))
                sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
                if -40 <= x <= sw - 120 and 0 <= y <= sh - 120:
                    pos = f"+{x}+{y}"
        w, h = min(w, 1600), min(h, 1400)
        self.geometry(f"{w}x{h}{pos}")

    def _on_configure(self, e):
        if e.widget is self:
            self._geo_dirty_at = time.monotonic()

    def _save_geometry_if_settled(self, now):
        if self._geo_dirty_at is None or now - self._geo_dirty_at < 0.6:
            return
        self._geo_dirty_at = None
        try:
            if self.state() == "normal":
                geo = self.geometry()
                if geo != self.settings.ui.geometry:
                    self.settings.ui.geometry = geo
                    self.settings.dirty = True
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------ actions
    def toast(self, text: str, seconds: float = 3.2):
        self.toast_lbl.configure(text=text)
        self.toast_lbl.place(in_=self.bar, relx=0.5, y=px(8), anchor="n")
        self.toast_lbl.lift()
        if self._toast_job:
            try:
                self.after_cancel(self._toast_job)
            except Exception:  # noqa: BLE001
                pass
        self._toast_job = self.after(int(seconds * 1000), self._hide_toast)

    def _hide_toast(self):
        self._toast_job = None
        try:
            self.toast_lbl.place_forget()
        except Exception:  # noqa: BLE001
            pass

    def _cue(self, kind):
        if getattr(self.settings.ui, "sounds", True):
            try:
                self.engine.play_cue(kind)
            except Exception:  # noqa: BLE001
                log.exception("play_cue")

    # -- tâches de fond : le résultat revient dans le thread Tk, via le tick -----------------
    def run_bg(self, fn, done=None):
        """Exécute ``fn()`` sur un thread ; ``done(résultat, exception)`` est appelé par le tick Tk."""
        def work():
            try:
                res, exc = fn(), None
            except Exception as e:  # noqa: BLE001
                res, exc = None, e
            self._bg.put((done, res, exc))
        threading.Thread(target=work, name="vocal-ui-bg", daemon=True).start()

    def _drain_bg(self):
        for _ in range(20):
            try:
                done, res, exc = self._bg.get_nowait()
            except Exception:  # noqa: BLE001
                return
            if done is not None:
                try:
                    done(res, exc)
                except Exception:  # noqa: BLE001
                    log.exception("tâche de fond")

    # -- micro / casque ---------------------------------------------------------------------
    # La règle « Discord » (couper le casque coupe aussi le micro et le rétablit ensuite ; rétablir le micro
    # pendant que le casque est coupé réactive le casque) est appliquée par le MOTEUR : l'interface appelle seulement
    # set_muted / set_deafened et affiche l'état.
    def toggle_mic(self):
        snap = self.last_snap
        self.engine.set_muted(not (snap.muted if snap else False))

    def toggle_deaf(self):
        snap = self.last_snap
        self.engine.set_deafened(not (snap.deafened if snap else False))

    def _track_mute_state(self, snap):
        """Sons de confirmation sur les CHANGEMENTS d'état (la règle sourdine / micro est dans le moteur)."""
        m, d = snap.muted, snap.deafened
        prev, self._prev_state = self._prev_state, (m, d)
        if prev is None:
            return
        pm, pd = prev
        if d != pd:
            self._cue("mute" if d else "unmute")
        elif m != pm:
            self._cue("mute" if m else "unmute")

    def _on_ctrl_m(self, _e=None):
        if self.wizard is None:
            self.toggle_mic()
        return "break"

    def toggle_monitor(self):
        snap = self.last_snap or self.engine.snapshot()
        self.engine.set_monitor(not snap.monitor, 15)

    def eng_call(self, name, *args, **kw):
        """Appelle une méthode 2.1 du moteur si elle existe (un moteur 2.0 ne l'a pas : on ne plante pas)."""
        fn = getattr(self.engine, name, None)
        if fn is None:
            return None
        try:
            return fn(*args, **kw)
        except Exception:  # noqa: BLE001
            log.exception("moteur.%s", name)
            return None

    def toggle_denoise(self):
        """Puce « Bruit réduit » et menu de l'icône : coupe, rétablit, ou relance si le suppresseur est tombé."""
        snap = self.last_snap or self.engine.snapshot()
        level = getattr(snap, "denoise_level", "off")
        if level != "off" and getattr(snap, "denoise_error_fr", None):
            self._last_denoise = level
            self.eng_call("set_denoise_level", level)            # « Réessayer »
            self.toast("Suppression du bruit relancée")
        elif level != "off":
            self._last_denoise = level
            self.eng_call("set_denoise_level", "off")
            self.toast("Bruit réduit coupé : ta voix part sans filtre")
        else:
            back = self._last_denoise or "normal"
            avail = getattr(self.engine, "denoise_available", None)
            if callable(avail) and not avail() and back in ("normal", "strong"):
                back = "light"                       # DLL absente : Léger (numpy) marche toujours
            self.eng_call("set_denoise_level", back)
            self.toast(f"Bruit réduit rétabli : {DENOISE_LABEL[back]}, {delay_label(denoise_delay(back, snap))}"
                       if denoise_delay(back, snap) else f"Bruit réduit rétabli : {DENOISE_LABEL[back]}")

    def _retry_denoise(self):
        snap = self.last_snap or self.engine.snapshot()
        level = getattr(snap, "denoise_level", "off")
        self.eng_call("set_denoise_level", level if level != "off" else (self._last_denoise or "normal"))

    def copy_ip(self):
        snap = self.last_snap or self.engine.snapshot()
        ip = snap.local_ip
        if not ip:
            self.toast("Adresse introuvable : es-tu connecté à un réseau ?")
            return
        try:
            self.clipboard_clear()
            self.clipboard_append(ip)
            self.update_idletasks()
        except Exception:  # noqa: BLE001
            pass
        self.toast(f"Adresse copiée : {ip}")
        self.copy_btn.configure(text=" Copié !")
        if self._copy_job:
            try:
                self.after_cancel(self._copy_job)
            except Exception:  # noqa: BLE001
                pass
        self._copy_job = self.after(1500, self._copy_reset)

    def _copy_reset(self):
        self._copy_job = None
        if not self._closing:
            self.copy_btn.configure(text=" Copier mon IP")

    def set_ui_pref(self, key, val, var=None):
        ui = self.settings.ui
        if key == "start_with_windows":
            try:
                import netutil  # type: ignore
                netutil.set_autostart(val)
            except Exception as exc:  # noqa: BLE001
                log.warning("set_autostart : %s", exc)
                if var is not None:
                    var.set(not val)
                self.toast("Impossible de changer le démarrage avec Windows")
                return
        setattr(ui, key, val)
        self.settings.dirty = True
        if key == "always_on_top":
            self._apply_topmost()

    def _apply_topmost(self):
        want = bool(getattr(self.settings.ui, "always_on_top", False))
        if want != self._topmost:
            self._topmost = want
            try:
                self.attributes("-topmost", want)
            except Exception:  # noqa: BLE001
                pass

    def open_settings(self):
        if self.settings_win is not None and self.settings_win.winfo_exists():
            self.settings_win.lift()
            self.settings_win.focus_force()
            return
        self.settings_win = SettingsWindow(self)

    def open_help(self):
        if self.help_win is not None and self.help_win.winfo_exists():
            self.help_win.lift()
            self.help_win.focus_force()
            return
        self.help_win = HelpWindow(self)

    def open_add_ip(self):
        if self.add_ip_win is not None and self.add_ip_win.winfo_exists():
            self.add_ip_win.lift()
            self.add_ip_win.focus_force()
            return
        self.add_ip_win = AddIpDialog(self)

    def ask_firewall(self):
        parent = self.help_win if self.help_win is not None and self.help_win.winfo_exists() else self
        FirewallDialog(parent, self._do_firewall)

    def _do_firewall(self):
        try:
            self.engine.repair_firewall()
        except Exception as exc:  # noqa: BLE001
            log.exception("repair_firewall")
            self.banners.show("fw", f"Impossible de lancer la réparation ({exc}).", "error")
            return
        self.toast("Réponds « Oui » à la fenêtre de Windows…", 5)
        self._help_status("En attente de ta réponse à la fenêtre de Windows…")

    def _help_status(self, text, color=TEXT_MUTED):
        if self.help_win is not None and self.help_win.winfo_exists():
            self.help_win.set_fw_status(text, color)

    def capture_key(self, callback):
        """Demande la prochaine touche pressée (globale). callback(code | None)."""
        if self._capture_cb is not None:
            old, self._capture_cb = self._capture_cb, None
            old(None)
        self._capture_cb = callback
        self._capture_deadline = time.monotonic() + 12
        try:
            self.hotkeys.capture_next()
        except Exception:  # noqa: BLE001
            log.exception("capture_next")
            self._capture_cb = None
            callback(None)

    def cancel_capture(self):
        """Abandonne une capture en cours (fenêtre fermée, délai dépassé). Tant que ``Hotkeys`` attend
        une touche, ses raccourcis sont suspendus : on lui demande d'annuler si elle sait le faire
        (``cancel_capture``, demandé à Dev A dans la revue croisée)."""
        cb, self._capture_cb = self._capture_cb, None
        fn = getattr(self.hotkeys, "cancel_capture", None)
        if fn is not None:
            try:
                fn()
            except Exception:  # noqa: BLE001
                log.exception("cancel_capture")
        if cb is not None:
            try:
                cb(None)
            except Exception:  # noqa: BLE001
                pass

    def show_window(self):
        try:
            self.deiconify()
            self.lift()
            self.attributes("-topmost", True)
            self.after(200, self._restore_topmost)
            self.focus_force()
        except Exception:  # noqa: BLE001
            pass

    def _restore_topmost(self):
        if not self._closing:
            try:
                self.attributes("-topmost", bool(self._topmost))
            except Exception:  # noqa: BLE001
                pass

    def _is_hidden(self) -> bool:
        try:
            return self.state() in ("withdrawn", "iconic")
        except Exception:  # noqa: BLE001
            return False

    def _notify_tray(self, msg):
        if self.tray and self.tray.available and self._is_hidden() and \
                getattr(self.settings.ui, "notifications", True):
            self.tray.notify(msg)

    # ------------------------------------------------------------ fermeture
    def _on_close_request(self):
        ui = self.settings.ui
        if self.wizard is None and getattr(ui, "close_to_tray", True) and self.tray and self.tray.available:
            self.withdraw()
            if not getattr(ui, "tray_notice_shown", False):
                ui.tray_notice_shown = True
                self.settings.dirty = True
                self.tray.notify("Vocal LAN continue de tourner ici, à côté de l'horloge. "
                                 "Clic droit sur l'icône pour quitter.")
            return
        self.quit_app()

    def quit_app(self):
        """Contrat de fermeture : save, hotkeys.stop, engine.shutdown, puis on détruit Tk."""
        if self._finalized:
            return
        self._finalized = True
        self._closing = True
        for job in (self._tick_job, self._copy_job, self._toast_job):
            if job:
                try:
                    self.after_cancel(job)
                except Exception:  # noqa: BLE001
                    pass
        try:
            if self.state() == "normal":
                self.settings.ui.geometry = self.geometry()
                self.settings.dirty = True
        except Exception:  # noqa: BLE001
            pass
        if self.tray:                       # d'abord : l'icône ne doit pas rester si l'arrêt traîne
            try:
                self.tray.stop()
            except Exception:  # noqa: BLE001
                log.exception("tray.stop")
        for step in (self.settings.save, self.hotkeys.stop, self.engine.shutdown):
            try:
                step()
            except Exception:  # noqa: BLE001
                log.exception("fermeture")
        try:
            self.quit()
            self.destroy()
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------ tick
    def _tick(self):
        if self._closing:
            return
        t0 = time.monotonic()
        try:
            snap = self.engine.snapshot()
            self.last_snap = snap
            self._drain_tray()
            if self._closing:
                return
            self._drain_events(snap)     # vidé dès le premier tick (avertissements de démarrage)
            self._drain_bg()
            if self._closing:
                return
            self._track_mute_state(snap)
            self._render(snap, t0)
        except Exception:  # noqa: BLE001
            self._err_count += 1
            if self._err_count <= 5 or self._err_count % 200 == 0:
                log.exception("Erreur dans le tick de l'interface (%d)", self._err_count)
        if not self._closing:
            self._tick_job = self.after(max(1, TICK_MS - int((time.monotonic() - t0) * 1000)),
                                        self._tick)

    def _drain_tray(self):
        if not self.tray:
            return
        while True:
            try:
                cmd = self.tray.commands.get_nowait()
            except Exception:  # noqa: BLE001
                return
            if cmd == "open":
                self.show_window()
            elif cmd == "mic":
                self.toggle_mic()
            elif cmd == "deaf":
                self.toggle_deaf()
            elif cmd == "denoise":
                self.toggle_denoise()
            elif cmd == "quit":
                self.quit_app()
                return

    def _drain_events(self, snap):
        ev_q = self.engine.events
        joined: list[str] = []
        left: list[str] = []
        for _ in range(200):
            try:
                ev = ev_q.get_nowait()
            except Exception:  # noqa: BLE001
                break
            if ev.kind == "peer_joined":
                joined.append(ev.peer_name or "Quelqu'un")
            elif ev.kind == "peer_left":
                left.append(ev.peer_name or "Quelqu'un")
            else:
                self._handle_event(ev)
        # arrivées / départs groupés : un seul son et une seule notification même si 8 personnes
        # arrivent en même temps (démarrage, retour du réseau)
        for names, kind, verb in ((joined, "join", "a rejoint|ont rejoint"),
                                  (left, "leave", "est parti|sont partis")):
            if not names:
                continue
            self._cue(kind)
            if getattr(self.settings.ui, "notifications", True):
                msg = group_names(names, *verb.split("|"))
                self.toast(msg)
                self._notify_tray(msg)

    def _handle_event(self, ev):
        kind = ev.kind
        if kind == "audio_error":
            self.banners.show("audio_ev", ev.message_fr or "Problème avec le son.", "error",
                              "Réglages", self.open_settings, closable=True)
        elif kind == "audio_recovered":
            self.banners.hide("audio_ev")
            self.banners.hide("audio_pick")
            self.banners.show("recovered", ev.message_fr or "Le son est revenu.", "ok")
            self._expire["recovered"] = time.monotonic() + 5
        elif kind == "denoise_failed" and ev.code == "peer":
            # le nettoyage de la voix d'UN ami s'est arrêté : pas de bandeau (ce n'est pas ta voix) ; la carte de
            # l'ami dit « Nettoyage en pause » et son bouton permet de réessayer
            self.toast(ev.message_fr or "Le nettoyage de la voix d'un ami s'est arrêté : sa voix passe "
                                        "sans traitement.", 6)
        elif kind == "denoise_failed":
            # repli sur la voix non traitée, sans coupure : on le dit clairement (le bandeau suit l'état du moteur)
            msg = ev.message_fr or ("La suppression du bruit s'est arrêtée. Ta voix passe sans filtre, "
                                    "sans coupure.")
            self.banners.show("denoise", msg, "warn", "Réessayer", self._retry_denoise, closable=False)
            self.toast("Suppression du bruit arrêtée : ta voix passe sans filtre", 5)
        elif kind == "settings_warning":
            self.banners.show("settings", ev.message_fr or "Tes réglages n'ont pas pu être relus.", "warn")
        elif kind == "network_warning":
            self._network_warning(ev.message_fr or "")
        elif kind == "devices_changed":
            if self.settings_win is not None and self.settings_win.winfo_exists():
                self.settings_win.reload_devices()
            if self.wizard is not None:
                self.wizard.reload_devices()
            if ev.message_fr:
                if self.wizard is not None:         # l'assistant recouvre les toasts
                    self.wizard.say(ev.message_fr)
                else:
                    self.toast(ev.message_fr, 5)
        elif kind == "firewall_result":
            if ev.code == "ok":
                self.toast("Pare-feu configuré. Tes amis devraient apparaître.", 5)
                self._help_status("C'est fait : Vocal LAN est autorisé. Patiente quelques secondes.", GREEN)
                self.banners.hide("fw")
            elif ev.code == "cancelled":
                self.toast("Autorisation annulée. Tu pourras réessayer depuis l'aide.", 5)
                self._help_status("Tu as annulé la demande de Windows. Tu peux réessayer.", ORANGE)
            else:
                msg = ev.message_fr or "Le pare-feu n'a pas pu être configuré."
                self.banners.show("fw", msg, "error")
                self._help_status(msg, RED)
        elif kind == "show_window":                 # 2e instance lancée : on se remet devant
            self.show_window()
        elif kind == "key_captured":
            cb, self._capture_cb = self._capture_cb, None
            if cb:
                cb(ev.code)

    def _network_warning(self, msg: str):
        """Avertissement réseau du moteur. Une autre version de Vocal LAN reste affichée (jusqu'à
        fermeture) : c'est la vraie raison pour laquelle un ami n'apparaît pas."""
        if "autre version" in msg:
            m = re.search(r"ordinateur\s+(\S+)", msg)
            who = f" (ordinateur {m.group(1)})" if m else ""
            text = (f"Un ami n'a pas la même version de Vocal LAN{who} : il ne peut pas parler avec toi. "
                    "Installez la même version partout.")
            self.banners.show("net_version:" + (m.group(1) if m else ""), text, "warn")
            return
        self.banners.show("net", msg, "warn")
        self._expire["net"] = time.monotonic() + 12

    # ------------------------------------------------------------ rendu
    def _render(self, snap, now):
        now = time.monotonic()
        # expiration des bandeaux temporaires et de la capture de touche
        for key, due in list(self._expire.items()):
            if now >= due:
                self._expire.pop(key)
                self.banners.hide(key)
        if self._capture_cb is not None and now >= self._capture_deadline:
            self.cancel_capture()

        n_people = len(snap.peers) + 1
        safe = self._safe
        safe("en-tête", self._render_header, snap, n_people)
        safe("liste", self._render_list, snap)
        safe("bas", self._render_bottom, snap, now)
        safe("bandeaux", self._render_banners, snap)
        safe("fenêtres", self._render_windows, snap)
        safe("topmost", self._apply_topmost)
        safe("zone de notification", self._update_tray, snap, n_people)
        safe("géométrie", self._save_geometry_if_settled, now)
        if now - self._last_save >= SAVE_EVERY_S:
            self._last_save = now
            safe("sauvegarde", self.settings.save_if_dirty)

    def _safe(self, what, fn, *args):
        """Une étape de rendu qui échoue ne doit pas empêcher les suivantes (ni la sauvegarde)."""
        try:
            fn(*args)
        except Exception:  # noqa: BLE001
            self._err_count += 1
            if self._err_count <= 5 or self._err_count % 200 == 0:
                log.exception("Rendu « %s » (%d)", what, self._err_count)

    def _render_windows(self, snap):
        if self.settings_win is not None:
            if self.settings_win.winfo_exists():
                self.settings_win.update_from(snap)
            else:
                self.settings_win = None
        if self.help_win is not None:
            if self.help_win.winfo_exists():
                self.help_win.set_ip(snap.local_ip)
            else:
                self.help_win = None
        if self.add_ip_win is not None and not self.add_ip_win.winfo_exists():
            self.add_ip_win = None
        if self.wizard is not None:
            self.wizard.update_from(snap)

    def _render_header(self, snap, n_people):
        if snap.network == "no_network":
            dot, txt, tip = RED, "Pas de réseau", "Aucune connexion réseau détectée."
        elif not snap.online:
            dot, txt, tip = TEXT_DIM, "Hors ligne", ""
        elif snap.peers:
            dot, txt = GREEN, f"Connecté · {count_label(n_people)}"
            tip = f"Ton adresse : {snap.local_ip}"
        else:
            dot, txt = ORANGE if snap.seconds_alone > ALONE_HELP_AFTER_S else TEXT_DIM, \
                "Seul pour l'instant"
            tip = f"Ton adresse : {snap.local_ip}"
        self.upd(self.dot_lbl, text_color=dot)
        self.upd(self.status_lbl, text=txt)
        self._status_tip.text = tip
        title = f"Vocal LAN ({n_people})" if snap.peers else "Vocal LAN"
        if title != getattr(self, "_title", None):
            self._title = title
            self.title(title)

    def _render_list(self, snap):
        ids = [p.id for p in snap.peers]
        structure_changed = False
        for pid in [i for i in self.rows if i not in ids]:
            self.rows.pop(pid).destroy()
            structure_changed = True
        for pv in snap.peers:
            if pv.id not in self.rows:
                self.rows[pv.id] = PeerRow(self, self.plist, pv.id)
                structure_changed = True
        want_empty = not snap.peers
        if structure_changed or ids != self._order or want_empty != self._empty_shown:
            self._order = ids
            self._empty_shown = want_empty
            self.me_row.pack_forget()
            self.empty.pack_forget()
            for r in self.rows.values():
                r.pack_forget()
            self.me_row.pack(fill="x", pady=(0, 8), padx=4)
            for i in ids:
                self.rows[i].pack(fill="x", pady=4, padx=4)
            if want_empty:
                self.empty.pack(fill="x", pady=(8, 0))
        self.me_row.update_me(snap)
        seen: dict[str, int] = {snap.name.casefold(): 1}
        for pv in snap.peers:
            k = (pv.name or "?").casefold()
            seen[k] = seen.get(k, 0) + 1
        for pv in snap.peers:
            self.rows[pv.id].update_peer(pv, seen[(pv.name or "?").casefold()] > 1)
        if want_empty:
            if snap.network == "no_network":
                self.upd(self.empty_title, text="Pas de connexion réseau")
                self.upd(self.empty_text, text="Vocal LAN ne trouve aucun réseau. Branche-toi au Wi-Fi "
                                               "ou au câble de ta box, puis réessaie.")
            else:
                self.upd(self.empty_title, text="Personne d'autre pour l'instant")
                self.upd(self.empty_text, text="Tes amis doivent lancer Vocal LAN sur le même Wi-Fi "
                                               "ou le même réseau que toi.")
            hint = snap.network == "alone" and snap.seconds_alone >= ALONE_HELP_AFTER_S
            if hint != self._alone_hint_shown:
                self._alone_hint_shown = hint
                if hint:
                    self.alone_hint.pack(pady=(16, 0))
                else:
                    self.alone_hint.pack_forget()

    def _render_bottom(self, snap, now):
        ui = self.settings.ui
        ptt_key = key_label(self.settings.ptt_key)
        mute_key = key_label(self.settings.mute_key)
        deaf_key = key_label(self.settings.deafen_key)
        # --- micro (indications courtes : la fenêtre peut faire 480 px de large) ---
        sc_m = "Clic ou Ctrl+M" + (f"\n{mute_key}" if mute_key else "")
        if snap.muted:
            self._mic_tip = "Réactiver le micro et le casque" if snap.deafened else "Réactiver le micro"
            self.mic_btn.set_state("Micro coupé", sc_m,
                                   icon("mic_off", RED, 26), RED_BG, RED_BG_HOVER, RED, "#e9b8b6")
        elif snap.ptt_mode:
            self._mic_tip = "Maintiens la touche pour parler. Clique ici pour couper le micro."
            if snap.ptt_active:
                self.mic_btn.set_state("Tu parles", f"{ptt_key} maintenue" if ptt_key else "Micro ouvert",
                                       icon("mic", GREEN, 26), GREEN_BG, GREEN_BG_HOVER, GREEN, "#a7e6c3")
            else:
                self.mic_btn.set_state("Appuyer pour parler",
                                       f"Maintiens {ptt_key}" if ptt_key else "Choisis une touche",
                                       icon("mic", TEXT_MUTED, 26), SURFACE_2, SURFACE_3, TEXT)
        else:
            self._mic_tip = "Couper le micro"
            self.mic_btn.set_state("Micro actif", sc_m,
                                   icon("mic", GREEN, 26), GREEN_BG, GREEN_BG_HOVER, GREEN, "#a7e6c3")
        # --- casque ---
        sc_d = "Clic" + (f" ou {deaf_key}" if deaf_key else "")
        if snap.deafened:
            self._deaf_tip = "Réactiver le casque"
            self.deaf_btn.set_state("Casque coupé", sc_d,
                                    icon("head_off", RED, 26), RED_BG, RED_BG_HOVER, RED, "#e9b8b6")
        else:
            self._deaf_tip = "Couper le casque : tu n'entends plus personne et ton micro est coupé aussi."
            self.deaf_btn.set_state("Casque actif", sc_d,
                                    icon("head", GREEN, 26), GREEN_BG, GREEN_BG_HOVER, GREEN, "#a7e6c3")
        # --- vumètre ---
        # (avec la suppression du bruit, la barre montre ce qui part vraiment : le clavier la fait nettement moins monter)
        shown = getattr(snap, "mic_level_out", snap.mic_level) if getattr(snap, "denoise_active", False) \
            else snap.mic_level
        active = snap.transmitting and not snap.muted
        self._render_denoise_chip(snap)
        self.meter.set(shown if not snap.muted else shown * 0.6, active,
                       0.0 if manual_gate_idle(snap) else snap.gate_level)
        if snap.muted:
            hint = "Micro coupé"
        elif snap.monitor:
            hint = "Tu t'entends (test en cours)"
        elif snap.ptt_mode:
            hint = f"Maintiens {ptt_key} pour parler" if ptt_key else "Appuyer pour parler"
        else:
            hint = "Micro toujours ouvert"
        self.upd(self.hint_lbl, text=hint, text_color=ORANGE if snap.monitor else TEXT_MUTED)
        # --- alerte « micro coupé » (maintenue 1,5 s pour ne pas clignoter) ---
        if snap.talking_while_muted:
            self._twm_until = now + 1.5
        if now < self._twm_until and snap.muted:
            if snap.deafened:
                self.alert.show("twm", "Casque coupé : tu n'entends personne et ton micro est coupé aussi.",
                                "warn", "Tout réactiver", self.toggle_mic, closable=False)
            else:
                self.alert.show("twm", "Ton micro est coupé : tes amis ne t'entendent pas.", "warn",
                                "Réactiver le micro", self.toggle_mic, closable=False)
        else:
            self.alert.hide("twm")
        # --- alerte « ton micro sature » (maintenue 2 s : pas de clignotement) ---
        if getattr(snap, "clipping", False) and not snap.muted:
            self._clip_until = now + 2.0
        if now < self._clip_until and not snap.muted:
            self.alert.show("clip", "Ton micro sature : ta voix grésille chez tes amis. Baisse le gain "
                                    "du micro ou éloigne-toi un peu.", "warn", "Réglages",
                            self.open_settings, closable=False)
        else:
            self.alert.hide("clip")

    def _render_denoise_chip(self, snap):
        """Puce « Bruit réduit » : verte si active, grise si coupée (un clic rétablit), orange si en panne."""
        has = hasattr(self.engine, "set_denoise_level") and hasattr(snap, "denoise_level")
        if has != self._nr_chip_shown:
            self._nr_chip_shown = has
            if has:
                self.nr_chip.pack(side="left", padx=(10, 0), before=self.hint_lbl)
            else:
                self.nr_chip.pack_forget()
        if not has:
            return
        level = snap.denoise_level
        err = getattr(snap, "denoise_error_fr", None)
        if level != "off":
            self._last_denoise = level
        d = denoise_delay(level, snap)
        name = DENOISE_LABEL.get(level, "")
        if level != "off" and err:
            self.upd(self.nr_chip, text="Bruit réduit : arrêté", fg_color=ORANGE_BG, hover_color="#4d3b19",
                     text_color=ORANGE)
            self._nr_tip = f"{err}\nClique pour la relancer."
        elif level != "off":
            self.upd(self.nr_chip, text="Bruit réduit", fg_color=GREEN_BG, hover_color=GREEN_BG_HOVER,
                     text_color=GREEN)
            lag = f"{name}, {delay_label(d)}." if d else f"{name}."
            if level in ("normal", "strong") and d:
                lag += f"\n{LAG_REASSURE}"
            self._nr_tip = f"{lag}\nClique pour couper."
        else:
            back = self._last_denoise or "normal"
            db = denoise_delay(back, snap)
            self.upd(self.nr_chip, text="Réduire le bruit", fg_color=SURFACE_3, hover_color=BORDER,
                     text_color=TEXT_MUTED)
            self._nr_tip = (f"Bruit non réduit. Clique pour activer ({DENOISE_LABEL[back]}, {delay_label(db)})."
                            if db else "Bruit non réduit. Clique pour activer.")

    def _render_banners(self, snap):
        err = getattr(snap, "denoise_error_fr", None)
        if err and getattr(snap, "denoise_level", "off") != "off":
            self.banners.show("denoise", err, "warn", "Réessayer", self._retry_denoise, closable=False)
        else:
            self.banners.hide("denoise")
        # des amis sont là mais aucun ne te reçoit depuis un moment : presque toujours le pare-feu
        now = time.monotonic()
        if snap.peers and snap.online and not any(p.hears_me for p in snap.peers):
            if self._nobody_since is None:
                self._nobody_since = now
            if now - self._nobody_since >= NOBODY_HEARS_AFTER_S:
                self.alert.show("nobody", "Personne ne te reçoit : ton pare-feu bloque peut-être Vocal LAN.",
                                "warn", "Aide pare-feu", self.open_help, closable=False)
        else:
            self._nobody_since = None
            self.alert.hide("nobody")
        a = snap.audio
        if not a.ok:
            self.banners.hide("audio_ev")       # le bandeau d'état dit déjà la même chose
            self.banners.show("audio_state", a.error_fr or "Le son ne fonctionne pas pour l'instant.",
                              "error", "Réglages", self.open_settings, closable=False)
        else:
            self.banners.hide("audio_state")
        if a.degraded:
            self.banners.show("degraded", "Mode compatibilité : le son peut avoir un peu plus de délai "
                                          "que d'habitude.", "info")
        # le bandeau « degraded » est fermable ; il revient seulement si le mode change
        elif self.banners.visible("degraded"):
            self.banners.hide("degraded")

    def _update_tray(self, snap, n_people):
        if not self.tray or not self.tray.available:
            return
        if snap.muted or snap.deafened:
            state = "red"
        elif not snap.online or not snap.peers or snap.network != "ok":
            state = "grey"
        else:
            state = "green"
        people = f"{count_label(n_people)}" if snap.peers else "seul"
        extra = (" · casque et micro coupés" if snap.muted and snap.deafened
                 else " · micro coupé" if snap.muted else " · casque coupé" if snap.deafened else "")
        nr = (getattr(snap, "denoise_level", "off") != "off") if hasattr(snap, "denoise_level") else None
        self.tray.update(state, f"Vocal LAN · {people}{extra}", not snap.muted, not snap.deafened, denoise=nr)


def _make_tray():
    try:
        from tray import Tray
        t = Tray()
        return t if t.start() else None
    except Exception:  # noqa: BLE001
        log.exception("Zone de notification indisponible")
        return None


# ===========================================================================
# Point d'entrée
# ===========================================================================
def run(engine, settings, hotkeys) -> None:
    """Bloque jusqu'à la fermeture. Affiche l'assistant au premier lancement."""
    app = App(engine, settings, hotkeys, use_tray=True)
    try:
        app.mainloop()
    finally:
        app.quit_app()

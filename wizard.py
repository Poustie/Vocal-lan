"""Assistant de premier lancement : 3 étapes courtes, affichées dans la fenêtre principale.

1. Pseudo.
2. « Mets ton casque » : avertissement d'écho, choix micro / casque, vumètre en direct, « Je m'entends ».
3. Mode de parole (micro ouvert / appuyer pour parler) et explication du pare-feu.

À la fin : réglages enregistrés, ``first_run_done = True`` puis ``engine.go_online()``.
"""
from __future__ import annotations

import logging
import os
import re

import customtkinter as ctk

import time
from collections import deque

from ui import (ACCENT, ACCENT_HOVER, BG, BORDER, DENOISE_FROM_LABEL, DENOISE_LABEL, DENOISE_LEVELS,
                DENOISE_TEXT, LAG_EXPLAIN, LAG_REASSURE, DevicePicker, KeyPicker, MAX_NAME, NOISY_FLOOR, ORANGE, ORANGE_BG, Banner,
                SURFACE, SURFACE_2, SURFACE_3, TEXT, TEXT_DIM, TEXT_MUTED, GREEN, Label, Meter,
                SLOW_AFTER_MS, denoise_delay, delay_label, font, icon, logo_image, make_seg, with_advice)

log = logging.getLogger("vocal.wizard")

STEPS = 3


def default_name() -> str:
    """Pseudo proposé : le nom de session Windows, nettoyé."""
    raw = os.environ.get("USERNAME") or os.environ.get("USER") or ""
    raw = re.sub(r"[^\w\- ]", "", raw).strip()
    return raw[:1].upper() + raw[1:] if raw else ""


class Wizard(ctk.CTkFrame):
    def __init__(self, app, engine, settings, hotkeys, on_done):
        super().__init__(app, fg_color=BG, corner_radius=0)
        self.app, self.engine, self.settings, self.hotkeys = app, engine, settings, hotkeys
        self.on_done = on_done
        self.step = 0
        self._monitor = False
        self._built: dict[int, ctk.CTkFrame] = {}
        self.picker: DevicePicker | None = None

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)
        # points de progression
        self.dots = ctk.CTkFrame(self, fg_color="transparent")
        self.dots.grid(row=0, column=0, pady=(26, 0))
        self._dot_widgets = []
        for _ in range(STEPS):
            d = ctk.CTkFrame(self.dots, width=34, height=6, corner_radius=3, fg_color=SURFACE_3)
            d.pack(side="left", padx=4)
            self._dot_widgets.append(d)
        self.stage = ctk.CTkFrame(self, fg_color="transparent")
        self.stage.grid(row=1, column=0, sticky="nsew", padx=40, pady=10)
        # navigation
        nav = ctk.CTkFrame(self, fg_color="transparent")
        nav.grid(row=2, column=0, sticky="ew", padx=40, pady=(0, 6))
        self.back_btn = ctk.CTkButton(nav, text="Retour", width=100, height=42, fg_color=SURFACE_3,
                                      hover_color=BORDER, font=font(14), command=self.back)
        self.back_btn.pack(side="left")
        self.next_btn = ctk.CTkButton(nav, text="Continuer", width=160, height=42, fg_color=ACCENT,
                                      hover_color=ACCENT_HOVER, text_color="#0b1020",
                                      font=font(14, "bold"), command=self.next)
        self.next_btn.pack(side="right")
        self.nav_hint = Label(self, text="", font=font(12), text_color=ORANGE, anchor="e",
                              justify="right", wraplength=420, pad=80)
        self.grid_rowconfigure(3, minsize=32)       # marge du bas, qui accueille aussi le texte d'aide
        self.show(0)

    # ------------------------------------------------------------- navigation
    def show(self, n: int):
        if self._monitor:
            self.engine.set_monitor(False)
            self._monitor = False
        self.step = n
        for f in self._built.values():
            f.pack_forget()
        if n not in self._built:
            self._built[n] = getattr(self, f"_build{n + 1}")()
        self._built[n].pack(fill="both", expand=True)
        for i, d in enumerate(self._dot_widgets):
            d.configure(fg_color=ACCENT if i <= n else SURFACE_3)
        if n == 0:
            self.back_btn.pack_forget()
        else:
            self.back_btn.pack(side="left")
        self.next_btn.configure(text="Terminer" if n == STEPS - 1 else "Continuer")
        self._validate()

    def back(self):
        if self.step > 0:
            self.show(self.step - 1)

    def next(self):
        if self.step == 0:
            name = self.name_entry.get().strip()[:MAX_NAME]
            if not name:
                return
            self.engine.set_name(name)
        if self.step < STEPS - 1:
            self.show(self.step + 1)
        else:
            self.finish()

    def finish(self):
        s = self.settings
        if self._monitor:
            self.engine.set_monitor(False)
            self._monitor = False
        ptt = bool(self.mode_var.get())
        self.engine.set_ptt(ptt)
        s.ui.first_run_done = True
        s.dirty = True
        try:
            s.save()
        except Exception:  # noqa: BLE001
            log.exception("save")
        try:
            self.hotkeys.apply()
        except Exception:  # noqa: BLE001
            log.exception("hotkeys.apply")
        # le moteur n'ouvre le réseau qu'ici : si le port est pris, un bandeau « Réessayer » reste
        # affiché dans la fenêtre principale (l'assistant se ferme quand même)
        self.app.go_online()
        self.on_done()

    def _validate(self):
        ok = True
        if self.step == 0:
            ok = bool(self.name_entry.get().strip())
        elif self.step == 2:
            ok = not (self.mode_var.get() == 1 and not self.settings.ptt_key)
        state = "normal" if ok else "disabled"
        if state != getattr(self, "_next_state", None):
            self._next_state = state
            self.next_btn.configure(state=state, fg_color=ACCENT if ok else SURFACE_3,
                                    text_color_disabled=TEXT_DIM)
        hint = "" if (ok or self.step != 2) else \
            "Choisis une touche, ou reviens à « Micro toujours ouvert »"
        if hint != getattr(self, "_hint_shown", None):
            self._hint_shown = hint
            self.nav_hint.configure(text=hint)
            if hint:
                self.nav_hint.grid(row=3, column=0, sticky="ne", padx=40)
            else:
                self.nav_hint.grid_forget()


    # ------------------------------------------------------------------ pages
    def _title(self, parent, text, sub=None):
        Label(parent, text=text, font=font(26, "bold"), anchor="w", justify="left").pack(
            fill="x", pady=(10, 4))
        if sub:
            Label(parent, text=sub, font=font(14), text_color=TEXT_MUTED, anchor="w",
                         justify="left", wraplength=440).pack(fill="x", pady=(0, 14))

    def _build1(self):
        outer = ctk.CTkFrame(self.stage, fg_color="transparent")
        f = ctk.CTkFrame(outer, fg_color="transparent")
        f.pack(expand=True, fill="x")          # contenu centré verticalement entre les points et le bouton
        img = logo_image(96)
        if img:
            Label(f, text="", image=img).pack(pady=(0, 14))
        Label(f, text="Bienvenue sur Vocal LAN", font=font(28, "bold")).pack()
        Label(f, text="Parle avec tes amis sur le même réseau, sans compte ni serveur.",
                     font=font(14), text_color=TEXT_MUTED, wraplength=440).pack(pady=(8, 34))
        Label(f, text="Comment veux-tu t'appeler ?", font=font(14, "bold"), anchor="w").pack(
            fill="x")
        self.name_entry = ctk.CTkEntry(f, height=44, font=font(16), fg_color=SURFACE_2,
                                       border_color=BORDER, placeholder_text="Ton pseudo")
        self.name_entry.pack(fill="x", pady=(6, 4))
        self.name_entry.insert(0, self.settings.name or default_name())
        self.name_entry.bind("<KeyRelease>", lambda _e: self._validate())
        self.name_entry.bind("<Return>", lambda _e: self.next())
        Label(f, text="C'est le nom que tes amis verront. Tu pourras le changer plus tard.",
                     font=font(12), text_color=TEXT_DIM, anchor="w").pack(fill="x")
        self._focus_job = self.after(150, self._focus_name)
        return outer

    def _focus_name(self):
        self._focus_job = None
        try:
            if self.winfo_exists():
                self.name_entry.focus_set()
        except Exception:  # noqa: BLE001
            pass

    def destroy(self):
        job, self._focus_job = getattr(self, "_focus_job", None), None
        if job:
            try:
                self.after_cancel(job)
            except Exception:  # noqa: BLE001
                pass
        super().destroy()

    def _build2(self):
        f = ctk.CTkFrame(self.stage, fg_color="transparent")
        self._title(f, "Mets ton casque")
        warn = ctk.CTkFrame(f, fg_color=ORANGE_BG, corner_radius=14, border_width=1, border_color=ORANGE)
        warn.pack(fill="x", pady=(0, 16))
        self._warn = warn
        Label(warn, text="", image=icon("head", ORANGE, 30), width=44).pack(side="left", padx=(12, 0),
                                                                                    pady=12)
        Label(warn, anchor="w", justify="left", wraplength=380, pad=110, font=font(13), text_color="#ffe0a8",
                     text="Avec des haut-parleurs, ton micro reprend le son et tout le monde entend un "
                          "écho strident. Un casque règle le problème.").pack(
            side="left", padx=12, pady=12, fill="x", expand=True)
        box = ctk.CTkFrame(f, fg_color="transparent")
        box.pack(fill="x")
        self.err = Banner(f)                    # erreurs de périphérique : visibles ici, pas sous l'assistant
        self._err_pick = None
        self._err_shown = None
        self.picker = DevicePicker(self.app, box, label_width=64, with_refresh=True, menu_width=280,
                                   on_error=self._pick_error)
        Label(f, text="Parle : la barre doit bouger", font=font(14, "bold"), anchor="w").pack(
            fill="x", pady=(18, 6))
        self.meter = Meter(f, height=18, bg=BG)
        self.meter.pack(fill="x")
        line = ctk.CTkFrame(f, fg_color="transparent")
        line.pack(fill="x", pady=(14, 4))
        self.mon_btn = ctk.CTkButton(line, text="Je m'entends (15 s)", height=40, fg_color=SURFACE_3,
                                     hover_color=BORDER, font=font(13), command=self._toggle_monitor)
        self.mon_btn.pack(side="left")
        self._ab_state = None
        self.ab_seg = make_seg(line, ["Sans filtre", "Avec filtre"], self._ab_changed, height=40)
        self.ab_seg.pack(side="left", padx=(10, 0))
        self.ab_seg.set("Avec filtre")
        self.ab_seg.configure(state="disabled")
        self.mon_hint = Label(f, text="Pour vérifier que ton micro marche, écoute ta propre voix quelques secondes.",
                     font=font(12), text_color=TEXT_DIM, anchor="w", wraplength=440,
                     justify="left")
        self.mon_hint.pack(fill="x")
        self._build_denoise(f)
        return f

    def _build_denoise(self, f):
        """Suppression du bruit : Normal présélectionné pour une nouvelle installation, délai affiché."""
        self.nr_box = None
        if not hasattr(self.engine, "set_denoise_level"):
            return
        box = self.nr_box = ctk.CTkFrame(f, fg_color=SURFACE, corner_radius=14)
        box.pack(fill="x", pady=(14, 0))
        top = ctk.CTkFrame(box, fg_color="transparent")
        top.pack(fill="x", padx=14, pady=(12, 0))
        Label(top, text="Suppression du bruit", font=font(14, "bold"), anchor="w").pack(side="left")
        self.nr_delay = Label(top, text="", font=font(12, "bold"), text_color=TEXT_MUTED, anchor="e")
        self.nr_delay.pack(side="right")
        self.nr_seg = make_seg(box, [lbl for _, lbl in DENOISE_LEVELS], self._nr_changed)
        self.nr_seg.pack(anchor="w", padx=14, pady=(8, 0))
        self.nr_info = Label(box, text="", font=font(12), text_color=TEXT_MUTED, anchor="w", justify="left",
                             wraplength=400, pad=110)
        self.nr_info.pack(fill="x", padx=14, pady=(6, 12))
        self.reco = ctk.CTkFrame(f, fg_color=ORANGE_BG, corner_radius=12, border_width=1, border_color=ORANGE)
        Label(self.reco, text="On entend beaucoup de bruit de fond. Nous te conseillons « Fort » avec la "
                              "voix « Plus claire ».", font=font(12), text_color="#ffe0a8", anchor="w",
              justify="left", wraplength=290, pad=170).pack(side="left", fill="x", expand=True, padx=12, pady=8)
        ctk.CTkButton(self.reco, text="Appliquer", width=90, height=30, fg_color=ORANGE, hover_color="#ffc562",
                      text_color="#10131a", font=font(12, "bold"), command=self._apply_reco).pack(
            side="right", padx=10, pady=8)
        self._reco_state = "wait"                  # wait -> shown -> done
        self._floor = deque()
        self._since = time.monotonic()
        self._last_noise_check = 0.0
        self._preselect_denoise()

    def _preselect_denoise(self):
        """Nouvelle installation : Normal (ou Léger si le fichier de suppression est absent)."""
        try:
            snap = self.engine.snapshot()
            if getattr(snap, "denoise_level", "off") != "off":
                return
            avail = getattr(self.engine, "denoise_available", None)
            self.engine.set_denoise_level("normal" if (avail is None or avail()) else "light")
        except Exception:  # noqa: BLE001
            log.exception("présélection de la suppression du bruit")

    def _nr_changed(self, label):
        self.engine.set_denoise_level(DENOISE_FROM_LABEL.get(label, "off"))

    def _ab_changed(self, label):
        fn = getattr(self.engine, "set_monitor_processed", None)
        if fn:
            fn(label == "Avec filtre")

    def _apply_reco(self):
        self.engine.set_denoise_level("strong")
        fn = getattr(self.engine, "set_voice_style", None)
        if fn:
            fn("clear")
        self._reco_state = "done"
        self.reco.pack_forget()

    # Mesure du bruit de fond de l'assistant : jamais sur un seul échantillon, jamais pendant qu'on parle.
    NOISE_WINDOW_S = 12.0          # mémoire des mesures
    NOISE_MIN_QUIET_S = 2.0        # non-parole cumulée exigée avant de recommander quoi que ce soit
    NOISE_RUN = 10                 # une pause = au moins 10 mesures (0,5 s) presque constantes
    NOISE_RUN_SPREAD = 0.06        # écart max / min toléré dans une pause (la parole varie bien plus)
    NOISE_PERCENTILE = 75          # plancher = percentile élevé des mesures de pause (prudent)

    @classmethod
    def noise_floor_estimate(cls, samples):
        """samples = [(t, niveau)] -> (plancher de bruit ou None, secondes de non-parole). Les « pauses »
        sont les passages d'au moins 0,5 s où le niveau reste presque constant (la parole, elle, ondule à
        chaque syllabe). Sans 2 s de pauses cumulées : pas d'estimation, donc pas de recommandation."""
        lv = [v for _, v in samples]
        n = len(lv)
        quiet = []
        k = 0
        while k + cls.NOISE_RUN <= n:
            w = lv[k:k + cls.NOISE_RUN]
            if max(w) - min(w) <= cls.NOISE_RUN_SPREAD:
                quiet.extend(w)
                k += cls.NOISE_RUN
            else:
                k += 1
        if len(quiet) * 0.05 < cls.NOISE_MIN_QUIET_S:
            return None, len(quiet) * 0.05
        quiet.sort()
        idx = min(len(quiet) - 1, int(len(quiet) * cls.NOISE_PERCENTILE / 100))
        return quiet[idx], len(quiet) * 0.05

    def _watch_noise(self, snap, now=None):
        """Recommande « Fort » si le bruit de fond, mesuré sur les pauses, est élevé (voir noise_floor_estimate)."""
        if self.nr_box is None or self._reco_state == "done":
            return
        now = time.monotonic() if now is None else now
        self._floor.append((now, snap.mic_level))
        while self._floor and now - self._floor[0][0] > self.NOISE_WINDOW_S:
            self._floor.popleft()
        if self._reco_state != "wait" or now - self._last_noise_check < 1.0:
            return
        self._last_noise_check = now
        floor, _quiet = self.noise_floor_estimate(self._floor)
        if floor is not None and floor > NOISY_FLOOR and getattr(snap, "denoise_level", "off") != "strong":
            self._reco_state = "shown"
            self.reco.pack(fill="x", pady=(10, 0))

    def _build3(self):
        f = ctk.CTkFrame(self.stage, fg_color="transparent")
        self._title(f, "Comment veux-tu parler ?")
        self.mode_var = ctk.IntVar(value=1 if self.settings.ptt_mode else 0)
        for val, title, sub in (
                (0, "Micro toujours ouvert", "On t'entend dès que tu parles. Le plus simple."),
                (1, "Appuyer pour parler", "On t'entend seulement quand tu maintiens une touche, "
                                           "même dans un jeu.")):
            card = ctk.CTkFrame(f, fg_color=SURFACE, corner_radius=14)
            card.pack(fill="x", pady=5)
            ctk.CTkRadioButton(card, text=title, variable=self.mode_var, value=val, font=font(14, "bold"),
                               fg_color=ACCENT, hover_color=ACCENT_HOVER, border_color=TEXT_DIM,
                               command=self._mode_changed).pack(anchor="w", padx=16, pady=(12, 0))
            Label(card, text=sub, font=font(12), text_color=TEXT_MUTED, anchor="w", justify="left",
                         wraplength=350, pad=70).pack(fill="x", padx=46, pady=(2, 12))
        self.key_row = ctk.CTkFrame(f, fg_color="transparent")
        line = ctk.CTkFrame(self.key_row, fg_color="transparent")
        line.pack(fill="x")
        Label(line, text="Touche à maintenir :", font=font(13), anchor="w").pack(side="left")
        self.key_picker = KeyPicker(line, self.app, "ptt_key", "Choisir…")
        self.key_picker.pack(side="left", padx=10)
        fw = ctk.CTkFrame(f, fg_color=SURFACE, corner_radius=14)
        fw.pack(fill="x", pady=(14, 0), side="bottom")
        Label(fw, text="Une dernière chose : le pare-feu", font=font(13, "bold"), anchor="w").pack(
            fill="x", padx=16, pady=(12, 2))
        Label(fw, anchor="w", justify="left", wraplength=410, font=font(12), text_color=TEXT_MUTED,
                     text="Windows peut te demander d'autoriser Vocal LAN sur le réseau. Réponds "
                          "« Autoriser » : sans cela, tes amis ne te verront pas. Si ça ne marche pas, le "
                          "bouton « Ça ne marche pas ? » t'aidera.").pack(fill="x", padx=16, pady=(0, 12))
        self._mode_changed()
        return f

    # -------------------------------------------------------------- actions
    def _mode_changed(self):
        if self.mode_var.get() == 1:
            self.key_row.pack(fill="x", pady=(10, 0))
        else:
            self.key_row.pack_forget()
        self._validate()

    def _toggle_monitor(self):
        self._monitor = not self._monitor
        self.engine.set_monitor(self._monitor, 15)

    def _pick_error(self, text):
        self._err_pick = text
        self._show_error(text or self._snap_error)

    _snap_error = None

    def _show_error(self, text):
        if text == self._err_shown:
            return
        self._err_shown = text
        if text:
            self.err.show(text, "error", closable=False)
            if not self.err.winfo_manager():
                self.err.pack(fill="x", pady=(0, 10), before=self._warn)
        else:
            self.err.pack_forget()

    def say(self, text):
        """Message court (liste des appareils modifiée...) : l'assistant recouvre les toasts."""
        if self.step == 1 and not self._err_shown:
            self.mon_hint.configure(text=text)

    def _update_denoise(self, snap):
        st = "normal" if snap.monitor else "disabled"
        if self._ab_state != st:
            self._ab_state = st
            self.ab_seg.configure(state=st)
        want = "Avec filtre" if getattr(snap, "monitor_processed", True) else "Sans filtre"
        if self.ab_seg.get() != want:
            self.ab_seg.set(want)
        if self.nr_box is None:
            return
        level = getattr(snap, "denoise_level", "off")
        lab = DENOISE_LABEL.get(level, "Aucune")
        if self.nr_seg.get() != lab:
            self.nr_seg.set(lab)
        added = getattr(snap, "latency_added_ms", 0.0) or 0.0
        d = denoise_delay(level, snap)
        shown = round(added) if added else d
        self.nr_delay.configure(text=f"Délai ajouté : {delay_label(shown)}",
                                text_color=ORANGE if shown > SLOW_AFTER_MS else TEXT_MUTED)
        txt = DENOISE_TEXT.get(level, "")
        if shown > SLOW_AFTER_MS:
            txt += " " + LAG_EXPLAIN.format(light=delay_label(denoise_delay("light", snap)))
        elif level in ("normal", "strong") and shown > 0:
            txt += " " + LAG_REASSURE
        err = getattr(snap, "denoise_error_fr", None)
        self.nr_info.configure(text=err if (err and level != "off") else txt)
        self._watch_noise(snap)

    def reload_devices(self):
        if self.picker is not None:
            self.picker.on_devices_changed()

    def update_from(self, snap):
        if self.step == 1 and hasattr(self, "meter"):
            a = snap.audio
            self._snap_error = None if a.ok else with_advice(
                a.error_fr or "Le son ne fonctionne pas pour l'instant.", "appareil")
            self._show_error(self._err_pick or self._snap_error)
            self.meter.set(snap.mic_level, snap.speaking, 0.0)
            if self._monitor and not snap.monitor:      # arrêt automatique côté moteur
                self._monitor = False
            self._update_denoise(snap)
            on = snap.monitor
            if on != getattr(self, "_mon_shown", None):
                self._mon_shown = on
                self.mon_btn.configure(text="Arrêter l'écoute" if on else "Je m'entends (15 s)",
                                       fg_color=ACCENT if on else SURFACE_3,
                                       text_color="#0b1020" if on else TEXT)
        elif self.step == 2:
            self._validate()

"""Réglages de Vocal LAN : dataclass, fichier JSON dans %APPDATA%\\Vocal LAN, écriture atomique.

Le moteur modifie l'objet Settings et pose `dirty = True` ; l'interface appelle
`save_if_dirty()` (au plus une fois par seconde) et `save()` à la fermeture.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import math
import os
import pathlib
import re
import sys
import threading
from dataclasses import dataclass, field

VERSION = "2.1.0"
SCHEMA = 1
log = logging.getLogger("vocal.settings")

_ID_RE = re.compile(r"^[0-9a-f]{8}$")
BUFFER_MODES = ("auto", "reactive", "stable")
DENOISE_LEVELS = ("off", "light", "normal", "strong")
VOICE_STYLES = ("natural", "clear")
HIGHPASS_HZ = (80, 120)


def app_dir() -> pathlib.Path:
    """Dossier des données utilisateur (créé si besoin). VOCAL_LAN_DATA le remplace (tests)."""
    env = os.environ.get("VOCAL_LAN_DATA")
    base = pathlib.Path(env) if env else pathlib.Path(os.environ.get("APPDATA") or pathlib.Path.home()) / "Vocal LAN"
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return base


def resource_path(rel: str) -> pathlib.Path:
    """Chemin d'une ressource embarquée (assets/...), en développement comme dans l'exe PyInstaller."""
    base = pathlib.Path(getattr(sys, "_MEIPASS", None) or pathlib.Path(__file__).resolve().parent)
    return base / rel


NEW_IN_21 = ("denoise_level", "voice_style", "fx_highpass", "fx_highpass_hz", "fx_limiter", "recv_leveler")


@dataclass
class UiPrefs:
    """Préférences d'interface (champs décidés par Dev B, valeurs par défaut obligatoires)."""
    geometry: str | None = None
    first_run_done: bool = False
    sounds: bool = True
    notifications: bool = True
    close_to_tray: bool = True
    tray_notice_shown: bool = False
    always_on_top: bool = False
    start_with_windows: bool = False
    show_advanced: bool = False


@dataclass
class Settings:
    name: str = ""
    mic_gain: float = 1.0
    master: float = 1.0
    gate_db: float = -70.0
    dev_in: str | None = None
    dev_out: str | None = None
    buffer_mode: str = "auto"
    ptt_mode: bool = False
    ptt_key: str | None = None
    mute_key: str | None = None
    deafen_key: str | None = None
    manual_peers: list[str] = field(default_factory=list)
    peer_prefs: dict[str, dict] = field(default_factory=dict)
    install_id: str = ""
    port: int = 50555
    # --- 2.1 : traitement de la voix. Valeurs par défaut = comportement de la 2.0 (sauf le passe-haut, 0 ms)
    denoise_level: str = "off"        # "off" | "light" | "normal" | "strong"
    voice_style: str = "natural"      # "natural" | "clear"
    fx_highpass: bool = True
    fx_highpass_hz: int = 80          # 80 | 120
    fx_limiter: bool = True
    recv_leveler: bool = True
    ui: UiPrefs = field(default_factory=UiPrefs)
    dirty: bool = field(default=False, repr=False, compare=False)

    def __post_init__(self):
        # attributs hors sauvegarde (pas des champs du dataclass)
        self.path: pathlib.Path | None = None
        self.warnings: list[str] = []
        self.legacy_volumes: dict[str, float] = {}  # anciens volumes par pseudo (migration)
        self._lock = threading.Lock()

    # ---------- chargement ----------
    @classmethod
    def load(cls, directory: pathlib.Path | None = None, legacy: pathlib.Path | None = None) -> "Settings":
        """Charge settings.json. Fichier absent : migration de l'ancien vocal_config.json si présent.
        Fichier illisible : copie en .bak, valeurs par défaut et avertissement (jamais d'exception)."""
        folder = pathlib.Path(directory) if directory else app_dir()
        path = folder / "settings.json"
        s = cls()
        s.path = path
        try:
            if path.exists():
                try:
                    data = json.loads(path.read_text(encoding="utf-8-sig"))  # BOM du Bloc-notes toléré
                    if not isinstance(data, dict):
                        raise ValueError("racine inattendue")
                    s._apply(data)
                    if any(k not in data for k in NEW_IN_21):  # fichier 2.0 : on le réécrit complet (rien n'est perdu)
                        s.dirty = True
                except (OSError, ValueError, RecursionError) as e:  # RecursionError : JSON trop imbriqué
                    log.warning("settings.json illisible : %s", e)
                    bak = path.with_suffix(".json.bak")
                    try:
                        os.replace(path, bak)
                    except OSError:
                        pass
                    s = cls()
                    s.path = path
                    s.warnings.append("Tes réglages étaient illisibles : les réglages par défaut ont été remis "
                                      "(une copie de l'ancien fichier est gardée en .bak).")
                    s.dirty = True
            else:
                s._migrate(legacy)
        except Exception:  # sécurité ultime : on démarre quand même
            log.exception("chargement des réglages")
            s.dirty = True
        if not _ID_RE.match(s.install_id):
            s.install_id = os.urandom(4).hex()
            s.dirty = True
        s._clean()
        if s.dirty:
            s.save()
        return s

    def _apply(self, data: dict) -> None:
        for f in dataclasses.fields(self):
            if f.name in ("dirty", "ui") or f.name not in data:
                continue
            ok, val = _coerce(data[f.name], getattr(self, f.name))
            if ok:
                setattr(self, f.name, val)
        ui = data.get("ui")
        if isinstance(ui, dict):
            for f in dataclasses.fields(UiPrefs):
                if f.name in ui:
                    ok, val = _coerce(ui[f.name], getattr(self.ui, f.name))
                    if ok:
                        setattr(self.ui, f.name, val)

    def _migrate(self, legacy: pathlib.Path | None) -> None:
        """Ancien vocal_config.json (à côté du script) : pseudo, volumes, périphériques."""
        candidates = [pathlib.Path(legacy)] if legacy else [
            pathlib.Path(__file__).resolve().parent / "vocal_config.json",
            pathlib.Path(sys.executable).resolve().parent / "vocal_config.json"]
        for c in candidates:
            try:
                old = json.loads(c.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError, RecursionError):
                continue
            if not isinstance(old, dict):
                continue
            if isinstance(old.get("name"), str):
                self.name = old["name"]
            for key in ("mic_gain", "master"):
                setattr(self, key, _finite(old.get(key), getattr(self, key), 0.0, 2.0))
            self.gate_db = _finite(old.get("gate_db"), self.gate_db, -70.0, -20.0)
            for key in ("dev_in", "dev_out"):
                v = old.get(key)
                setattr(self, key, v if isinstance(v, str) and v != "Par défaut" else None)
            pv = old.get("peer_volumes")
            if isinstance(pv, dict):
                self.legacy_volumes = {str(k): _finite(v, 1.0, 0.0, 2.0) for k, v in pv.items()
                                       if isinstance(v, (int, float)) and not isinstance(v, bool)}
            log.info("migration de %s", c)
            self.dirty = True
            return

    def _clean(self) -> None:
        """Borne et nettoie toutes les valeurs (fichier modifié à la main, ancienne version...)."""
        self.name = _clean_text(self.name)
        self.mic_gain = _finite(self.mic_gain, 1.0, 0.0, 2.0)
        self.master = _finite(self.master, 1.0, 0.0, 2.0)
        self.gate_db = _finite(self.gate_db, -70.0, -70.0, -20.0)
        if self.buffer_mode not in BUFFER_MODES:
            self.buffer_mode = "auto"
        if isinstance(self.port, bool) or not isinstance(self.port, int) or not (1024 <= self.port <= 65535):
            self.port = 50555
        if self.denoise_level not in DENOISE_LEVELS:
            self.denoise_level = "off"
        if self.voice_style not in VOICE_STYLES:
            self.voice_style = "natural"
        if self.fx_highpass_hz not in HIGHPASS_HZ:
            self.fx_highpass_hz = 80
        self.manual_peers = [p for p in self.manual_peers if isinstance(p, str) and 0 < len(p) <= 100]
        prefs = {}
        for pid, v in self.peer_prefs.items():
            if isinstance(pid, str) and isinstance(v, dict):
                vol = _finite(v.get("volume", 1.0), 1.0, 0.0, 2.0)      # NaN / infini / énorme : 1.0 ou borné
                prefs[pid] = {"volume": vol, "muted": v.get("muted") is True, "clean": v.get("clean") is True,
                              "name": _clean_text(v.get("name", "")) if isinstance(v.get("name", ""), str) else ""}
        self.peer_prefs = prefs

    # ---------- sauvegarde ----------
    def to_dict(self) -> dict:
        d = dataclasses.asdict(self)
        d.pop("dirty", None)
        d["schema"] = SCHEMA
        return d

    def save(self) -> None:
        """Écriture atomique (fichier temporaire puis remplacement). Ne lève jamais."""
        if self.path is None:
            self.path = app_dir() / "settings.json"
        with self._lock:
            self.dirty = False  # remis avant l'écriture : un changement concurrent re-posera le drapeau
            try:
                try:
                    data = self.to_dict()
                except RuntimeError:  # un thread réseau a modifié peer_prefs pendant la copie : on réessaie
                    data = self.to_dict()
                payload = json.dumps(data, ensure_ascii=False, indent=1)
                tmp = self.path.with_suffix(".json.tmp")
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write(payload)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self.path)
            except (OSError, TypeError, ValueError, RuntimeError) as e:
                self.dirty = True
                log.warning("sauvegarde des réglages impossible : %s", e)

    def save_if_dirty(self) -> None:
        if self.dirty:
            self.save()


def _finite(v, default: float, lo: float, hi: float) -> float:
    """Nombre fini borné à [lo, hi] ; `default` si `v` n'est pas un nombre, est NaN, infini ou trop grand pour un flottant."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return default
    try:
        f = float(v)
    except (ValueError, OverflowError):
        return default
    if not math.isfinite(f):
        return default
    return min(max(f, lo), hi)


def _clean_text(text: str, limit: int = 32) -> str:
    """Retire les caractères de contrôle, espaces superflus, limite en caractères (pas en octets).
    U+200D (liant des emojis composés) et les sélecteurs de variante U+FE00..FE0F sont invisibles donc « non
    imprimables » : on les garde, sinon un emoji composé est décomposé ; on évite seulement qu'ils restent en
    tête ou en queue (après la coupe à `limit`)."""
    text = "".join(ch for ch in str(text) if ch.isprintable() or ch == "\u200d" or "\ufe00" <= ch <= "\ufe0f")
    return text.strip().strip("\u200d")[:limit].rstrip("\u200d")


def _coerce(value, default):
    """(ok, valeur convertie) si `value` a le même type que `default`, sinon (False, None)."""
    if isinstance(default, bool):
        return (True, value) if isinstance(value, bool) else (False, None)
    if isinstance(default, (int, float)):
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            try:
                if isinstance(value, float) and not math.isfinite(value):   # NaN, Infinity, -Infinity (JSON Python les accepte)
                    return False, None
                return True, type(default)(value)       # int(1e30) est permis, float(10**400) lève OverflowError
            except (ValueError, OverflowError):
                return False, None
        return False, None
    if isinstance(default, str):
        return (True, value) if isinstance(value, str) else (False, None)
    if isinstance(default, list):
        return (True, list(value)) if isinstance(value, list) else (False, None)
    if isinstance(default, dict):
        return (True, dict(value)) if isinstance(value, dict) else (False, None)
    if default is None:  # champ « str | None »
        return (True, value) if value is None or isinstance(value, str) else (False, None)
    return False, None

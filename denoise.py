"""Suppression du bruit de Vocal LAN 2.1.

    available() -> bool         DLL présente ET hachage SHA-256 correct
    make(level) -> Processor    "off" | "light" | "normal" | "strong"
    Slot                        porte-processeur du moteur : échange à chaud avec fondu enchaîné

Principes
- Format du moteur : float32[240] (5 ms à 48 kHz), échelle int16, traité EN PLACE, `process` ne lève jamais.
- Repli sûr : en cas d'erreur (DLL absente ou modifiée, exception, valeur non finie, PC trop lent) la sortie est
  l'entrée, sans coupure ; après des erreurs répétées le processeur se désactive et `error` contient une phrase
  française prête à afficher.
- `latency_samples` : retard ajouté, exact (mesuré par corrélation, voir tests/test_denoise.py) ; 0 si inactif.
- `vad` : probabilité de voix (0..1) de la dernière trame, ou None si inconnue.
- La DLL est RNNoise (BSD-3, xiph.org). On ne l'appelle que par ctypes ; le paquet Python `pyrnnoise` n'est
  jamais importé. Son SHA-256 est vérifié avant tout chargement.
"""
from __future__ import annotations

import ctypes
import hashlib
import logging
import math
import os
import sys
import threading
import time

import numpy as np

log = logging.getLogger("vocal.denoise")

RATE = 48000
FRAME = 240
EXPECTED_SHA256 = "06770aeb8855ef942b5dcdb436f84dfdd5867b716ba4b4c8a2c66ade03197f31"
DLL_NAME = "rnnoise.dll"
LEVELS = ("off", "light", "normal", "strong")

# retards exacts (échantillons à 48 kHz)
RNNOISE_LATENCY = 1199          # 959 (algorithme) + 240 (tampon 240 -> 480)
WIENER_LATENCY = FRAME          # 5 ms
HYBRID_LATENCY = 2 * FRAME      # 10 ms

FADE_FRAMES = 4                 # fondu de 20 ms
SLOW_FRAME_S = 0.004            # une trame traitée en plus de 4 ms (budget 5 ms) est « lente »
SLOW_WINDOW = 400               # on juge sur 2 s : un trou isolé (antivirus, mise en veille...) ne coupe rien
SLOW_MAX = 280                  # plus de 70 % de trames lentes sur la fenêtre = le PC ne suit vraiment pas
MAX_ERRORS = 4

MSG_UNAVAILABLE = ("La suppression du bruit n'est pas disponible sur cet ordinateur (fichier manquant ou "
                   "modifié). Ta voix passe sans traitement.")
MSG_FAILED = ("La suppression du bruit a rencontré un problème et s'est arrêtée. "
              "Ta voix passe sans traitement.")
MSG_TOO_SLOW = ("Ton ordinateur n'arrive pas à suivre la suppression du bruit : elle est coupée. "
                "Ta voix passe sans traitement.")


# ============================================================ chargement de la DLL
def _candidates() -> list[str]:
    here = os.path.dirname(os.path.abspath(__file__))
    out = []
    mei = getattr(sys, "_MEIPASS", None)
    if mei:
        out.append(os.path.join(mei, "third_party", "rnnoise", DLL_NAME))
        out.append(os.path.join(mei, DLL_NAME))
    out.append(os.path.join(here, "third_party", "rnnoise", DLL_NAME))
    return out


class _Lib:
    """DLL vérifiée et chargée (une seule fois par processus, jamais déchargée : une DLL native utilisée par un
    thread audio ne se décharge pas sans risque, et son poids en mémoire est de quelques Mo)."""

    def __init__(self, path: str):
        self.path = path
        self._dirs = []
        try:        # VCRUNTIME140.dll est cherchée à côté de l'exe (PyInstaller) : on l'ajoute explicitement
            if hasattr(os, "add_dll_directory"):
                for d in {os.path.dirname(path), getattr(sys, "_MEIPASS", None) or ""}:
                    if d and os.path.isdir(d):
                        self._dirs.append(os.add_dll_directory(d))
        except OSError:
            pass
        lib = ctypes.CDLL(path)         # (PyDLL, qui garde le GIL pendant l'appel natif, a été essayé : p99,9 et max pires)
        lib.rnnoise_create.argtypes = [ctypes.c_void_p]
        lib.rnnoise_create.restype = ctypes.c_void_p
        lib.rnnoise_destroy.argtypes = [ctypes.c_void_p]
        lib.rnnoise_destroy.restype = None
        lib.rnnoise_init.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        lib.rnnoise_init.restype = ctypes.c_int
        lib.rnnoise_process_frame.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        lib.rnnoise_process_frame.restype = ctypes.c_float
        lib.rnnoise_get_frame_size.argtypes = []
        lib.rnnoise_get_frame_size.restype = ctypes.c_int
        if lib.rnnoise_get_frame_size() != 2 * FRAME:
            raise OSError("taille de trame RNNoise inattendue")
        self.lib = lib


RETRY_S = 2.0       # un échec de chargement (antivirus qui verrouille le fichier...) n'est pas définitif
_cache: dict = {"lib": None, "tried": False, "why": "", "t": 0.0}
_load_lock = threading.Lock()


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _load() -> "_Lib | None":
    """Cherche la DLL, vérifie son SHA-256 AVANT de la charger, la charge (jamais d'exception).
    Succès mémorisé pour toute la durée du processus. Échec mémorisé RETRY_S secondes seulement : un fichier
    momentanément verrouillé (antivirus, première lecture) ne doit pas désactiver la fonction jusqu'au
    redémarrage, et « Réessayer » doit pouvoir réussir. Verrou : deux threads ne chargent jamais en même temps
    (sinon l'un verrait « indisponible » pendant que l'autre charge)."""
    c = _cache
    lib = c["lib"]
    if lib is not None:
        return lib
    if c["tried"] and time.monotonic() - c["t"] < RETRY_S:
        return None
    with _load_lock:
        if c["lib"] is not None:
            return c["lib"]
        if c["tried"] and time.monotonic() - c["t"] < RETRY_S:
            return None
        why = "introuvable"
        for path in _candidates():
            try:
                if not os.path.isfile(path):
                    continue
                digest = _sha256(path)
                if digest != EXPECTED_SHA256:
                    why = f"empreinte incorrecte ({path})"
                    log.warning("rnnoise.dll refusée : %s", why)
                    continue
                c["lib"] = _Lib(path)
                c["why"] = ""
                c["tried"] = True
                log.info("rnnoise.dll chargée : %s", path)
                return c["lib"]
            except Exception as e:  # noqa: BLE001 - tout échec = indisponible
                why = f"échec de chargement : {e}"
                log.warning("rnnoise.dll : %s", why)
        c["why"] = why
        c["tried"] = True
        c["t"] = time.monotonic()
        return None


def reset_cache() -> None:
    """Oublie le résultat de la recherche de la DLL (tests)."""
    _cache.update(lib=None, tried=False, why="", t=0.0)


def available() -> bool:
    """DLL présente, hachage correct et chargeable."""
    return _load() is not None


def unavailable_reason() -> str:
    _load()
    return _cache["why"]


def dll_path() -> str | None:
    """Chemin de la DLL chargée (diagnostic), None si elle ne l'est pas."""
    lib = _cache["lib"]
    return None if lib is None else lib.path


# ============================================================ noyaux (traitent 240 échantillons en place)
class _Delay:
    """Ligne à retard de `delay` échantillons, par trames de 240, sans allocation (anneau de 8 trames)."""
    SIZE = 8 * FRAME

    def __init__(self, delay: int):
        self.delay = delay
        self.buf = np.zeros(self.SIZE, np.float32)
        self.wp = 0

    def reset(self):
        self.buf.fill(0)
        self.wp = 0

    def push(self, x, out):
        """Écrit `x` (240) et place dans `out` la trame retardée de `delay` échantillons."""
        wp = self.wp
        self.buf[wp:wp + FRAME] = x
        s = (wp - self.delay) % self.SIZE              # la sortie i est l'entrée i - delay
        n1 = min(FRAME, self.SIZE - s)
        out[:n1] = self.buf[s:s + n1]
        if n1 < FRAME:
            out[n1:] = self.buf[:FRAME - n1]
        self.wp = (wp + FRAME) % self.SIZE


_PRIME: np.ndarray | None = None


def _prime_signal() -> np.ndarray:
    """Une voyelle synthétique (0,25 s, -32 dBFS, f0 130 Hz avec vibrato, trois formants : source glottale en dent de
    scie filtrée par trois résonateurs) puis 0,3 s de souffle à -58 dBFS. Calculée une fois (~25 ms), déterministe.
    Sert à « chauffer » le réseau de RNNoise : un réseau tout neuf, qui n'a encore jamais entendu de voix, prend la
    première consonne faible pour du bruit (banc de Dev B : premier mot à -15 dB, alors que les suivants sont à
    -2,5 dB ; après cet amorçage tous sont à -2 dB). Le choix du signal compte : une somme d'harmoniques pures, sans
    vibrato ni souffle, n'a PAS l'effet voulu (essayée : -5 dB sur tous les mots)."""
    global _PRIME
    if _PRIME is None:
        n_v, n_g = int(0.25 * RATE), int(0.30 * RATE)
        n = n_v + n_g
        n -= n % (2 * FRAME)
        rng = np.random.default_rng(3)
        t = np.arange(n_v) / RATE
        f = 130.0 * (1 + 0.03 * np.sin(2 * np.pi * 3.0 * t)) * (1 + 0.1 * np.sin(2 * np.pi * 0.7 * t + 3))
        ph = np.cumsum(f) / RATE
        glot = 2 * (ph % 1.0) - 1
        glot = np.diff(glot, prepend=0) * 0.5 + glot * 0.5
        glot += 0.02 * rng.standard_normal(n_v)
        v = np.zeros(n_v)
        for fc, bw in ((700, 90), (1220, 110), (2600, 160)):
            w0 = 2 * math.pi * fc / RATE
            al = math.sin(w0) / (2 * fc / bw)
            a0 = 1 + al
            b0, a1, a2 = al / a0, -2 * math.cos(w0) / a0, (1 - al) / a0       # passe-bande RBJ (b1 = 0, b2 = -b0)
            z1 = z2 = 0.0
            out = np.empty(n_v)
            for i, xi in enumerate(glot.tolist()):
                o = b0 * xi + z1
                z1 = -a1 * o + z2
                z2 = -b0 * xi - a2 * o
                out[i] = o
            v += out
        na, nr = int(0.01 * RATE), int(0.03 * RATE)
        env = np.ones(n_v)
        env[:na] = np.linspace(0, 1, na)
        env[-nr:] = np.linspace(1, 0, nr)
        v *= env
        v *= 32768 * 10 ** (-32 / 20) / math.sqrt(float(np.mean(v ** 2)))
        sig = np.zeros(n)
        sig[:n_v] = v
        rng = np.random.default_rng(11)
        nz = rng.standard_normal(n)
        sig += nz * (32768 * 10 ** (-58 / 20) / float(np.std(nz)))
        _PRIME = np.clip(sig, -32768, 32767).astype(np.float32)
    return _PRIME


class _RNNoiseCore:
    """RNNoise complet. Tampon 240 -> 480 -> 240 (retard 240) + retard interne 959 : 1199 échantillons.
    La sortie reçoit `dry_db` du signal d'origine retardé de la même durée (évite le filtre en peigne et laisse
    passer un peu de voix si RNNoise se trompe).

    Durée de vie de l'état natif : il est créé ici et détruit UNIQUEMENT par `__del__`, c'est-à-dire quand plus rien
    ne référence l'objet. Un thread qui exécute `process` (le callback audio) tient une référence à `self`
    pendant tout l'appel natif (ctypes libère le GIL, pas la référence), donc `rnnoise_destroy` ne peut pas
    s'exécuter pendant un appel en cours. `close()` n'existe que pour les tests et ne doit jamais être appelé sur un
    objet que le callback peut encore utiliser."""

    latency_samples = RNNOISE_LATENCY

    def __init__(self, lib: _Lib, dry_db: float = -30.0, prime: bool = True):
        self._lib = lib
        self.st = lib.lib.rnnoise_create(None)
        if not self.st:
            raise OSError("rnnoise_create a échoué")
        self.inb = np.zeros(2 * FRAME, np.float32)
        self.outb = np.zeros(2 * FRAME, np.float32)
        self._tmp = np.zeros(2 * FRAME, np.float32)
        self._tp = self._tmp.ctypes.data
        self._fn = lib.lib.rnnoise_process_frame
        self.dry_gain = np.float32(10 ** (dry_db / 20)) if dry_db is not None else np.float32(0.0)
        self._dly = _Delay(RNNOISE_LATENCY)
        self._dd = np.zeros(FRAME, np.float32)
        self.phase = 0
        self.vad: float | None = 0.0
        if prime:
            self._prime()

    def _prime(self):
        """Fait entendre une voix synthétique au réseau (hors callback : ~20 ms), puis vide les tampons audio."""
        sig = _prime_signal()
        tmp, fn, st = self._tmp, self._fn, self.st
        if not st:
            return
        for i in range(0, len(sig), 2 * FRAME):
            tmp[:] = sig[i:i + 2 * FRAME]
            fn(st, self._tp, self._tp)
        self.flush()

    def reset(self):
        """Tampons audio de l'enveloppe remis à zéro (ligne à retard, demi-trames) : quelques microsecondes, utilisable
        dans le callback. Le réseau RNNoise garde sa « chaleur » ET sa mémoire audio native (~30 ms) : ni l'un ni
        l'autre n'est touché. À utiliser pour un ami qui reprend la parole après une pause (son flux s'est arrêté
        sur ses 300 ms de silence d'envoi) ; un réseau remis à neuf à chaque reprise atténuerait chaque premier mot."""
        self.inb.fill(0)
        self.outb.fill(0)
        self._dly.reset()
        self.phase = 0
        self.vad = 0.0

    def sync(self, tick: int, par: int) -> None:
        """Décale la phase des blocs : le calcul natif (un par bloc de 480 = 2 trames) aura lieu sur les trames dont
        le numéro `tick` a la parité `par`. À n'appeler que sur un flux VIDE (juste après make / flush / reset) : la
        trame suivante est la trame numéro `tick`. Le retard ne change pas (240 de tampon + 959 internes, quelle que
        soit la parité) ; ça sert à ne jamais empiler plusieurs calculs RNNoise dans la même trame de 5 ms."""
        self.phase = 1 if (tick & 1) == (par & 1) else 0

    def flush(self):
        """reset() + oubli de la mémoire audio native de RNNoise (6 trames de silence : ~2 ms), SANS toucher à
        l'apprentissage du réseau. Pour un NOUVEAU flux sans rapport avec l'ancien (périphérique rouvert). Les
        trames de silence sont nécessaires : RNNoise garde 30 ms d'historique pour sa recherche de hauteur, et deux
        trames ne suffisaient pas (une voix fantôme ressortait à -30 dBFS)."""
        self.reset()
        if not self.st:
            return
        for _ in range(6):
            self._tmp.fill(0)                  # RNNoise travaille sur place : la sortie écrase l'entrée
            self._fn(self.st, self._tp, self._tp)
        self._tmp.fill(0)
        self.vad = 0.0

    def reinit(self):
        """Remise à neuf complète du réseau (jamais depuis le callback : ~20 ms)."""
        if not self.st:
            return
        self._lib.lib.rnnoise_init(self.st, None)
        self._prime()

    def process(self, x):
        dd = self._dd
        self._dly.push(x, dd)
        if self.phase == 0:
            self.inb[:FRAME] = x
            x[:] = self.outb[FRAME:]
            self.phase = 1
        else:
            self.inb[FRAME:] = x
            np.clip(self.inb, -32768.0, 32767.0, out=self._tmp)
            st = self.st
            if not st:                         # fermé : jamais d'appel natif avec un pointeur nul (plantage sans appel)
                raise RuntimeError("suppresseur fermé")
            self.vad = float(self._fn(st, self._tp, self._tp))
            self.outb[:] = self._tmp
            x[:] = self.outb[:FRAME]
            self.phase = 0
        if self.dry_gain:
            dd *= self.dry_gain
            x += dd

    def close(self):
        st, self.st = self.st, None
        if st:
            try:
                self._lib.lib.rnnoise_destroy(st)
            except Exception:  # noqa: BLE001
                pass

    def __del__(self):
        try:
            self.close()
        except Exception:  # noqa: BLE001
            pass


class _VadProbe:
    """RNNoise utilisé comme simple détecteur de voix (sa sortie audio est jetée). Une probabilité par bloc de
    10 ms, connue 20 ms avant que l'audio traité correspondant ne sorte d'un suppresseur complet."""

    def __init__(self, lib: _Lib):
        self._lib = lib
        self.st = lib.lib.rnnoise_create(None)
        if not self.st:
            raise OSError("rnnoise_create a échoué")
        self._fn = lib.lib.rnnoise_process_frame
        self.blk = np.zeros(2 * FRAME, np.float32)
        self._tp = self.blk.ctypes.data
        self.phase = 0
        self.vad = 0.0

    def reset(self):
        if self.st:
            self._lib.lib.rnnoise_init(self.st, None)
        self.blk.fill(0)
        self.phase = 0
        self.vad = 0.0

    def feed(self, x) -> float:
        if self.phase == 0:
            self.blk[:FRAME] = x
            self.phase = 1
        else:
            np.clip(x, -32768.0, 32767.0, out=self.blk[FRAME:])
            st = self.st
            if not st:
                raise RuntimeError("détecteur fermé")
            self.vad = float(self._fn(st, self._tp, self._tp))
            self.phase = 0
        return self.vad

    def close(self):
        st, self.st = self.st, None
        if st:
            try:
                self._lib.lib.rnnoise_destroy(st)
            except Exception:  # noqa: BLE001
                pass

    def __del__(self):
        try:
            self.close()
        except Exception:  # noqa: BLE001
            pass


class _WienerCore:
    """Filtre de Wiener à STFT (fenêtre 480, pas 240, sqrt-Hann), gain à décision dirigée, bruit suivi par
    statistiques de minimum. Retard 240 (5 ms). Pur numpy ; presque sans allocation (tampons préalloués).
    Le plancher d'atténuation limite le « bruit musical » (-12 dB : kurtosis du résidu ~ celle du bruit)."""

    latency_samples = WIENER_LATENCY
    vad = None

    def __init__(self, floor_db: float = -12.0, over: float = 1.5):
        n = 2 * FRAME
        nb = n // 2 + 1
        self.win = np.sqrt(np.hanning(n + 1)[:-1]).astype(np.float32)
        self.floor = np.float32(10 ** (floor_db / 20))
        self.over = np.float32(over)
        f = np.float32
        self.buf = np.zeros(n, f)
        self.wb = np.zeros(n, f)
        self.yb = np.zeros(n, f)
        self.X = np.zeros(nb, np.complex64)
        self.P = np.zeros(nb, f)
        self.t1 = np.zeros(nb, f)
        self.t2 = np.zeros(nb, f)
        self.t3 = np.zeros(nb, f)
        self.inv = np.zeros(nb, f)
        self.nz = np.zeros(nb, f)
        self.m1 = np.zeros(nb, f)
        self.m2 = np.zeros(nb, f)
        self.ps = np.zeros(nb, f)
        self.g = np.ones(nb, f)
        self.gprev = np.ones(nb, f)
        self.pprev = np.zeros(nb, f)
        self.noise = np.full(nb, 1e5, f)
        self.prev = np.zeros(FRAME, f)
        self.ola = np.zeros(FRAME, f)
        self.subs = [np.full(nb, 1e30, f) for _ in range(4)]
        self.cnt = 0
        self.warm = 0
        self.first = True

    def reset(self):
        for a in (self.buf, self.ps, self.pprev, self.prev, self.ola, self.P):
            a.fill(0)
        self.gprev.fill(1)
        self.g.fill(1)
        self.noise.fill(1e5)
        for s in self.subs:
            s.fill(1e30)
        self.cnt = self.warm = 0
        self.first = True

    def process(self, x):
        buf = self.buf
        buf[:FRAME] = self.prev
        buf[FRAME:] = x
        self.prev[:] = x
        np.multiply(buf, self.win, out=self.wb)
        X = np.fft.rfft(self.wb, out=self.X)
        P, t1, t2 = self.P, self.t1, self.t2
        np.multiply(X.real, X.real, out=P)
        np.multiply(X.imag, X.imag, out=t1)
        P += t1
        P += 1e-3                           # plancher : évite les dénormaux (très lents) sur du silence numérique
        ps = self.ps
        if self.first:
            ps[:] = P
            self.first = False
        else:
            ps *= 0.85
            np.multiply(P, 0.15, out=t1)
            ps += t1
        subs = self.subs
        np.minimum(subs[3], ps, out=subs[3])
        self.cnt += 1
        if self.cnt >= 40:                  # ~200 ms : on fait tourner les sous-fenêtres de minimum
            old = subs.pop(0)
            old.fill(1e30)
            subs.append(old)
            self.cnt = 0
        if self.warm < 10:
            self.warm += 1
            self.noise[:] = ps
        else:
            np.minimum(subs[0], subs[1], out=self.m1)
            np.minimum(subs[2], subs[3], out=self.m2)
            np.minimum(self.m1, self.m2, out=self.m1)
            np.multiply(self.m1, 1.8, out=self.noise)
        nz, inv = self.nz, self.inv
        np.multiply(self.noise, self.over, out=nz)
        nz += 1e-9
        np.divide(1.0, nz, out=inv)
        np.multiply(P, inv, out=t2)                        # post
        t2 -= 1.0
        np.maximum(t2, 0.0, out=t2)
        t2 *= 0.05
        prio = self.t3
        np.multiply(self.gprev, self.gprev, out=prio)
        prio *= self.pprev
        prio *= inv
        prio *= 0.95
        prio += t2
        np.add(prio, 1.0, out=t1)
        g = self.g
        np.divide(prio, t1, out=g)
        np.maximum(g, self.floor, out=g)
        self.gprev, self.g = g, self.gprev
        self.pprev[:] = P
        np.multiply(X, g, out=X)
        y = np.fft.irfft(X, 2 * FRAME, out=self.yb)
        y *= self.win
        np.add(self.ola, y[:FRAME], out=x)
        self.ola[:] = y[FRAME:]


class _HybridCore:
    """Candidat (a) « Normal » : audio = Wiener (+5 ms) retardé de 5 ms de plus ; RNNoise ne sert que de détecteur
    de voix. La porte (profondeur <= 12 dB, jamais un silence) S'OUVRE si le VAD OU le niveau dit « voix » et ne se
    FERME que si le VAD se tait pendant 300 ms (maintien). Retard total 10 ms."""

    latency_samples = HYBRID_LATENCY

    def __init__(self, lib: _Lib, depth_db: float = 12.0, thr: float = 0.30, hold_ms: int = 300,
                 release_ms: int = 120, open_margin_db: float = 10.0):
        self.wi = _WienerCore(floor_db=-12.0)
        self.probe = _VadProbe(lib)
        self.dl = _Delay(FRAME)
        self._d = np.zeros(FRAME, np.float32)
        self.floor = 10 ** (-depth_db / 20)
        self.thr, self.hold_n = thr, hold_ms // 5
        self.rel = (1.0 - self.floor) / (release_ms / 5.0)
        self.margin = open_margin_db
        self._ramp = np.arange(1, FRAME + 1, dtype=np.float32) / FRAME
        self._rbuf = np.zeros(FRAME, np.float32)
        self.vad: float | None = 0.0
        self.reset()

    def reset(self):
        self.wi.reset()
        self.probe.reset()
        self.dl.reset()
        self.g = 1.0
        self.hold = 0
        self.fl = None            # plancher de bruit estimé (dBFS), pour l'ouverture « par niveau »
        self.vad = 0.0

    def process(self, x):
        v = self.probe.feed(x)
        self.vad = v
        d0 = float(np.dot(x, x)) / FRAME
        lvl = 10.0 * math.log10(d0 + 1e-3) - 90.31
        fl = self.fl
        if fl is None or lvl < fl:
            fl = lvl                                         # le plancher descend aussitôt...
        else:
            fl += 0.04                                       # ...et remonte lentement (~8 dB/s)
        self.fl = fl = max(fl, -90.0)
        by_level = lvl > fl + self.margin
        self.wi.process(x)
        out = self._d
        self.dl.push(x, out)
        if v > self.thr or by_level:
            self.hold = self.hold_n
            tgt = 1.0
        elif self.hold > 0:
            self.hold -= 1
            tgt = 1.0
        else:
            tgt = self.floor
        g0 = self.g
        g1 = tgt if tgt > g0 else max(tgt, g0 - self.rel)
        if g0 == 1.0 and g1 == 1.0:
            x[:] = out
        else:
            np.multiply(self._ramp, np.float32(g1 - g0), out=self._rbuf)
            self._rbuf += np.float32(g0)
            np.multiply(out, self._rbuf, out=x)
        self.g = g1

    def close(self):
        self.probe.close()


# ============================================================ enveloppe : fondu, repli, surveillance
class Passthrough:
    """Aucun traitement (niveau « off », ou repli). Ne touche pas au signal."""

    def __init__(self, error: str | None = None):
        self.latency_samples = 0
        self.vad: float | None = None
        self.error = error
        self.enabled = True
        self.level = "off"

    def process(self, x) -> None:
        pass

    def reset(self) -> None:
        pass

    def wake(self) -> None:
        pass


class Denoiser:
    """Processeur du contrat. Enveloppe d'un noyau : fondu sec/traité de 20 ms à l'activation (`enabled` True)
    et à la désactivation (`enabled` False), repli sec sur erreur, détection des valeurs non finies et du PC
    qui ne suit pas. `process` ne lève jamais."""

    def __init__(self, core, level: str):
        self.core = core
        self.level = level
        self.enabled = True
        self.error: str | None = None
        self.vad: float | None = getattr(core, "vad", None)
        self._lat = core.latency_samples
        self.latency_samples = self._lat
        self._f = 0.0
        self._dry = np.zeros(FRAME, np.float32)
        self._ramp = np.arange(1, FRAME + 1, dtype=np.float32) / FRAME
        self._fr = np.zeros(FRAME, np.float32)
        self._errors = 0
        self._slow = 0
        self._nwin = 0
        self._dead = False
        # signal « non traité mais retardé comme le traité » : sert de repli fondu quand le noyau tombe en panne
        self._fb = _Delay(self._lat)
        self._pr = np.zeros(FRAME, np.float32)
        self._nfb = 0                   # trames déjà passées dans `_fb` depuis le dernier reset
        self._last = 0.0                # dernier échantillon sorti (raccord quand le noyau lâche)
        self._src_pr = True             # la sortie ne vient pas du noyau (repli, ou rien de sorti encore) : pas de raccord à faire
        self._gl = np.zeros(FRAME, np.float32)
        self._gramp = np.maximum(0.0, 1.0 - np.arange(FRAME) / 64.0).astype(np.float32)   # raccord de 64 échantillons
        self._nneed = -(-self._lat // FRAME)
        self.par: int | None = None     # parité des trames où le noyau calcule (None = non calée)

    @property
    def finished(self) -> bool:
        """Plus rien à faire : arrêté (enabled False ou en panne) ET fondu de sortie terminé. Le propriétaire peut
        alors le libérer sans que le son saute."""
        return (self._dead or not self.enabled) and self._f == 0.0

    @property
    def fading(self) -> bool:
        return self._dead and self._f > 0.0

    def sync(self, tick: int, par: int) -> None:
        """Cale la phase du noyau (voir _RNNoiseCore.sync). Sans effet pour les noyaux qui calculent à chaque trame."""
        s = getattr(self.core, "sync", None)
        if s is not None:
            self.par = par & 1
            s(tick, par)

    # -- API du contrat
    def reset(self) -> None:
        try:
            self.core.reset()
            self._fb.reset()
            self._nfb = 0
            self._src_pr = True
        except Exception:  # noqa: BLE001
            self._fail(MSG_FAILED)

    def wake(self) -> None:
        self.reset()

    def flush(self) -> None:
        """Nouveau flux audio sans rapport avec l'ancien (périphérique rouvert) : oublie aussi la mémoire audio native."""
        try:
            f = getattr(self.core, "flush", None)
            if f is None:
                self.core.reset()
            else:
                f()
            self._fb.reset()
            self._nfb = 0
            self._src_pr = True
        except Exception:  # noqa: BLE001
            self._fail(MSG_FAILED)

    def process(self, x) -> None:
        try:
            self._process(x)
        except Exception:  # noqa: BLE001 - jamais d'exception vers l'appelant
            try:
                self._errors += 1
                if self._errors >= MAX_ERRORS:
                    self._fail(MSG_FAILED)
            except Exception:  # noqa: BLE001
                pass

    # -- interne
    def _fail(self, msg: str) -> None:
        """Panne : `error` est visible tout de suite (interface, porte manuelle), mais le son redescend vers le sec
        en fondu de 20 ms (voir `_process`), sans saut. `latency_samples` tombe à 0 quand le fondu est fini."""
        self._dead = True
        self.error = msg
        log.warning("suppression du bruit désactivée : %s", msg)

    def _process(self, x) -> None:
        dead = self._dead
        f0 = self._f
        if f0 == 0.0 and (dead or not self.enabled):
            return                                              # inactif : rien du tout, 0 ms
        dry = self._dry
        dry[:] = x
        pr = self._pr
        self._fb.push(dry, pr)                                  # le sec, retardé comme le traité (repli sans saut)
        if self._nfb <= self._nneed:
            self._nfb += 1
        ready = self._nfb > self._nneed                         # `pr` contient déjà du vrai son (sinon : du silence)
        if dead:
            x[:] = pr if ready else dry                         # le noyau ne tourne plus : on descend vers le sec
            self._glue(x)
            target = 0.0
        else:
            target = 1.0 if self.enabled else 0.0
            t0 = time.perf_counter()
            try:
                s = float(np.dot(x, x))
                if not math.isfinite(s):                        # NaN / inf à l'entrée : on assainit, état intact
                    np.nan_to_num(x, copy=False, nan=0.0, posinf=32767.0, neginf=-32768.0)
                    np.clip(x, -32768.0, 32767.0, out=x)
                    dry[:] = x
                self.core.process(x)
                if not math.isfinite(float(np.dot(x, x))):
                    raise FloatingPointError("sortie non finie")
            except Exception:  # noqa: BLE001
                self._errors += 1
                x[:] = pr if ready else dry                     # sortie = entrée (retardée comme le traité), pas de coupure
                self._glue(x)
                if self._errors >= MAX_ERRORS:
                    self._fail(MSG_FAILED)                      # on part en fondu vers le sec, pas d'un coup
                    target = 0.0
                else:
                    try:
                        self.core.reset()
                    except Exception:  # noqa: BLE001
                        self._fail(MSG_FAILED)
                    self._last = float(x[-1])
                    return
            else:
                self._src_pr = False
                self.vad = getattr(self.core, "vad", None)
                dt = time.perf_counter() - t0
                if dt > SLOW_FRAME_S:
                    self._slow += 1
                self._nwin += 1
                if self._nwin >= SLOW_WINDOW:
                    bad = self._slow >= SLOW_MAX
                    self._nwin = self._slow = 0
                    if bad:                                     # le PC ne suit pas : on garde cette trame (bonne) et on
                        self._fail(MSG_TOO_SLOW)                # redescend vers le sec en fondu
                        target = 0.0
        step = 1.0 / FADE_FRAMES
        f1 = f0 + max(-step, min(step, target - f0))
        if f0 == 1.0 and f1 == 1.0:
            self.latency_samples = self._lat
            self._last = float(x[-1])
            return
        fr = self._fr                                           # fondu linéaire f0 -> f1 sur la trame
        np.multiply(self._ramp, np.float32(f1 - f0), out=fr)
        fr += np.float32(f0)
        x -= dry
        x *= fr
        x += dry
        self._f = f1
        self.latency_samples = self._lat if f1 > 0.0 else 0
        self._last = float(x[-1])

    def _glue(self, x) -> None:
        """Première trame sortie du repli au lieu du noyau : on raccorde au dernier échantillon réellement sorti par une
        rampe de 64 échantillons (1,3 ms). Sans cela, passer du traité au sec retardé (qui n'est jamais identique,
        surtout si le bruit était fortement réduit) faisait une marche audible (« pop »)."""
        if self._src_pr:
            return
        self._src_pr = True
        d = self._last - float(x[0])
        if d:
            np.multiply(self._gramp, np.float32(d), out=self._gl)
            x += self._gl

    def close(self) -> None:
        c = getattr(self.core, "close", None)
        if c:
            c()


# ============================================================ fabrique
def make(level: str):
    """Fabrique le processeur d'un niveau. Ne lève jamais : si le niveau a besoin de la DLL et qu'elle manque ou est
    modifiée, renvoie un `Passthrough` dont `error` explique pourquoi (le son n'est pas touché)."""
    try:
        level = level if level in LEVELS else "off"
        if level == "off":
            return Passthrough()
        if level == "light":
            return Denoiser(_WienerCore(floor_db=-12.0), "light")
        lib = _load()
        if lib is None:
            log.warning("niveau %s demandé mais RNNoise est indisponible : %s", level, _cache["why"])
            return Passthrough(MSG_UNAVAILABLE)
        if level == "strong":
            return Denoiser(_RNNoiseCore(lib, dry_db=-30.0), "strong")
        return Denoiser(_normal_core(lib), "normal")
    except Exception:  # noqa: BLE001
        log.exception("make(%s)", level)
        return Passthrough(MSG_FAILED)


def _normal_core(lib: _Lib):
    """Implémentation retenue pour « Normal » (voir l'épreuve : equipe/v21/devA/epreuve_normal.py)."""
    if NORMAL_IMPL == "rnnoise18":
        return _RNNoiseCore(lib, dry_db=-18.0)
    return _HybridCore(lib)


NORMAL_IMPL = "rnnoise18"   # "hybrid" (a, +10 ms) | "rnnoise18" (b, +25 ms) : (b) retenu, aucun candidat ne réussit l'épreuve


def level_latency_samples(level: str) -> int:
    """Retard nominal d'un niveau (pour l'affichage avant activation)."""
    if level == "light":
        return WIENER_LATENCY
    if level == "normal":
        return HYBRID_LATENCY if NORMAL_IMPL == "hybrid" else RNNOISE_LATENCY
    if level == "strong":
        return RNNOISE_LATENCY
    return 0


# ============================================================ porte-processeur du moteur
class Slot:
    """Tient le suppresseur actif. `set()` (thread UI) pose un nouveau processeur ; le callback l'adopte à la
    trame suivante avec un fondu enchaîné de 20 ms (ancien -> nouveau), sans clic même entre deux retards
    différents. Aucun verrou, aucune allocation dans `process`. None = pas de suppresseur (chemin 2.0)."""

    def __init__(self, proc=None):
        self.cur = proc if (proc is not None and not isinstance(proc, Passthrough)) else None
        self.shown = proc                      # ce que l'interface regarde (error, level)
        self._req = (proc,)                    # dernière demande : un NOUVEAU tuple à chaque set()
        self._seen = self._req                 # dernière demande déjà adoptée par le callback
        self._old = None
        self._old_left = 0
        self._dry = np.zeros(FRAME, np.float32)
        self._new = np.zeros(FRAME, np.float32)
        self.tick = 0                          # numéro de la trame en cours (posé par le moteur à chaque callback)
        self.pick = None                       # f(proc) -> parité (0/1) de calcul du nouveau suppresseur (moteur)

    def set(self, proc) -> None:
        """Thread UI. `proc` : sortie de make() (ou None). Le callback compare la RÉFÉRENCE du tuple de demande à celle
        qu'il a déjà vue : il ne remet jamais rien à zéro, donc deux set() rapprochés ne peuvent pas se perdre (avec un
        drapeau « demande en attente » effacé par le callback, la 2e demande pouvait être effacée avec la 1re et le
        suppresseur disparaissait alors que l'interface le croyait en marche)."""
        self.shown = proc                      # l'interface voit tout de suite le nouvel état (niveau, erreur)
        self._req = (proc,)

    @property
    def active(self) -> bool:
        return self.cur is not None and self.cur.error is None and getattr(self.cur, "enabled", True)

    @property
    def error(self) -> str | None:
        p = self.shown
        return None if p is None else p.error

    @property
    def active_shown(self) -> bool:
        """Un vrai suppresseur est posé (ou en cours de pose) et ne signale aucune panne."""
        p = self.shown
        return p is not None and not isinstance(p, Passthrough) and p.error is None

    @property
    def latency_shown(self) -> int:
        """Retard du suppresseur posé (échantillons), 0 si aucun / en panne."""
        p = self.shown
        return 0 if (p is None or p.error is not None) else p.latency_samples

    @property
    def latency_samples(self) -> int:
        c = self.cur
        return 0 if c is None else c.latency_samples

    @property
    def vad(self):
        c = self.cur
        return None if c is None else c.vad

    def process(self, x) -> None:
        req = self._req
        if req is not self._seen:
            self._seen = req
            p = req[0]
            new = None if (p is None or isinstance(p, Passthrough)) else p
            if self.cur is not None and self.cur is not new:
                self._old, self._old_left = self.cur, FADE_FRAMES + 2
                self._old.enabled = False
            self.cur = new
            sync = getattr(new, "sync", None)
            if sync is not None:                                # flux neuf : on cale sa phase de calcul
                pk = self.pick
                sync(self.tick, 0 if pk is None else pk(new))
        cur = self.cur
        if cur is not None and cur.error is not None and getattr(cur, "finished", True):
            self.cur = cur = None                               # panne, et le fondu vers le sec est terminé
        old = self._old
        if old is None:
            if cur is not None:
                cur.process(x)
            return
        dry, new = self._dry, self._new
        dry[:] = x
        old.process(x)                                          # x = ancienne sortie (qui s'estompe vers le sec)
        if cur is not None:
            new[:] = dry
            cur.process(new)                                    # nouvelle sortie (qui monte depuis le sec)
            x += new
            x -= dry
        self._old_left -= 1
        if self._old_left <= 0 or old.error is not None:
            self._old = None

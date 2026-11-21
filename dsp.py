"""Chaine de traitement de la voix de Vocal LAN 2.1 (numpy pur, sans scipy), 0 ms de latence.

Convention : x est un tableau float32 de 240 echantillons (5 ms a 48 kHz), echelle int16 (+-32768), traite
EN PLACE. Tout est causal et sans anticipation : aucun etage n'ajoute de latence.

API publique (figee par CONTRAT_v21.md) :
  VoiceChain(rate=48000, frame=240)
      .configure(style, highpass, highpass_hz, limiter, gate_db)   # thread interface, sans verrou
      .process_pre(x)      # AVANT le suppresseur de bruit : passe-haut (+ detection de saturation)
      .process_post(x)     # APRES le suppresseur : porte manuelle, volume auto, presence, de-esser,
                           #   compresseur, limiteur
      .gate_open, .clipping, .latency_samples (= 0)
  Leveler()                # un par ami, a la reception : .process(x), .enabled

Principes (voir aussi patch_devB_pour_engine.md) :
  * filtres IIR en "forme bloc" exacte : un produit matrice-vecteur float32 par trame, etat conserve ;
  * gains : un niveau par trame, interpole lineairement sur la trame (jamais de marche d'escalier) ;
  * chaque etage est derriere un interrupteur a fondu (15 ms) : activer / desactiver ne fait jamais de clic ;
  * aucun tableau numpy n'est alloue dans process* (tout est preallocate, operandes scalaires en tableaux 0-d) ;
    seuls restent de petits objets CPython (flottants) : mesure < 1 Ko par trame (test tracemalloc) ;
  * `configure` est appele depuis le thread interface : il n'ecrit que des attributs simples (flottants,
    booleens) ou echange la reference d'un filtre deja construit ; le callback les lit a la trame suivante.
"""
from __future__ import annotations

import math

import numpy as np

RATE = 48000
FRAME = 240
FS = 32768.0
DB0 = 20 * math.log10(FS)          # 90.31 : 0 dBFS en echelle int16
F32 = np.float32
RAMP = np.arange(1, FRAME + 1, dtype=F32) / F32(FRAME)       # 1/240 .. 1
GATE_OFF_DB = -70.0                # gate_db <= -70 : porte manuelle desactivee (comme settings.py)
CLIP_LEVEL = 32000.0               # crete a partir de laquelle on dit que le micro sature
CLIP_HOLD_FRAMES = 150             # l'indicateur reste vrai 0,75 s apres la derniere crete
LIMIT_CEIL = 29000.0
BAD_PEAK = 1.0e6                   # au-dela (ou NaN / Inf) : trame invalide, jamais une vraie voix int16
BAD_ENERGY = 1.0e15                # idem pour la somme des carres d'une trame (NaN / Inf / debordement)
_AD = np.full((), 1e-18, np.float32)   # anti-denormal : -380 dBFS, inaudible, tableau 0-d (aucune allocation)


def db2lin(d: float) -> float:
    return 10.0 ** (d / 20.0)


def lvl_db(ms: float) -> float:
    """Puissance moyenne (echelle int16^2) -> dBFS rms."""
    return 10.0 * math.log10(ms + 1e-3) - DB0


class _Scalars:
    """Deux operandes scalaires preallocables : un ufunc avec un scalaire Python cree ~120-200 octets a chaque
    appel, avec un tableau 0-d : 0 octet. Un jeu par instance (pas de partage entre threads)."""
    __slots__ = ("a", "b", "d")

    def __init__(self):
        self.a = np.zeros((), F32)
        self.b = np.zeros((), F32)
        self.d = np.zeros((), F32)


def _dot_self(a, sc: _Scalars) -> float:
    np.dot(a, a, out=sc.d)
    return sc.d.item()


# ------------------------------------------------------------------ biquads (RBJ)
def biquad(kind: str, f0: float, q: float = 0.7071, gain_db: float = 0.0, fs: int = RATE):
    w0 = 2 * math.pi * f0 / fs
    cw, sw = math.cos(w0), math.sin(w0)
    al = sw / (2 * q)
    A = 10 ** (gain_db / 40)
    if kind == "hp":
        b = [(1 + cw) / 2, -(1 + cw), (1 + cw) / 2]
        a = [1 + al, -2 * cw, 1 - al]
    elif kind == "lp":
        b = [(1 - cw) / 2, 1 - cw, (1 - cw) / 2]
        a = [1 + al, -2 * cw, 1 - al]
    elif kind == "ap":
        b = [1 - al, -2 * cw, 1 + al]
        a = [1 + al, -2 * cw, 1 - al]
    elif kind == "peak":
        b = [1 + al * A, -2 * cw, 1 - al * A]
        a = [1 + al / A, -2 * cw, 1 - al / A]
    elif kind == "hshelf":
        sq = 2 * math.sqrt(A) * al
        b = [A * ((A + 1) + (A - 1) * cw + sq), -2 * A * ((A - 1) + (A + 1) * cw),
             A * ((A + 1) + (A - 1) * cw - sq)]
        a = [(A + 1) - (A - 1) * cw + sq, 2 * ((A - 1) - (A + 1) * cw), (A + 1) - (A - 1) * cw - sq]
    else:
        raise ValueError(kind)
    return [x / a[0] for x in b], [x / a[0] for x in a]


def butter_hp4(f0: float):
    """Passe-haut Butterworth d'ordre 4 = 2 sections."""
    return [biquad("hp", f0, 0.5412), biquad("hp", f0, 1.3066)]


def sim_biquads(sections, x, z):
    """Reference : cascade de biquads (forme transposee II), boucle Python. Sert a la conception et aux tests."""
    z = [list(s) for s in z]
    y, hist = [], []
    for v in x:
        for i, (b, a) in enumerate(sections):
            zz = z[i]
            o = b[0] * v + zz[0]
            zz[0] = b[1] * v - a[1] * o + zz[1]
            zz[1] = b[2] * v - a[2] * o
            v = o
        y.append(v)
        hist.append([q for zz in z for q in zz])
    return y, hist


class BlockFilter:
    """Cascade de biquads, forme bloc exacte. Etat conserve ; un changement de coefficients se fait par un
    fondu d'une trame ancien -> nouveau. `set_sections` (thread interface) construit le nouveau filtre ;
    le callback l'adopte a la trame suivante (comparaison de reference, aucune perte possible)."""

    def __init__(self, sections):
        self._build(sections)
        self._pend = None
        self._seen = None
        self._t = np.zeros(FRAME, F32)
        self._t2 = np.zeros(FRAME, F32)

    def _build(self, sections):
        n = FRAME
        order = 2 * len(sections)
        self.sections = sections
        imp = [1.0] + [0.0] * (n - 1)
        h, hist = sim_biquads(sections, imp, [[0.0, 0.0]] * len(sections))
        h = np.asarray(h)
        i, j = np.indices((n, n))
        d = i - j
        H = np.where(d >= 0, h[np.clip(d, 0, None)], 0.0)
        D = np.asarray([hist[n - 1 - jj] for jj in range(n)]).T          # order x n
        C = np.zeros((n, order))
        E = np.zeros((order, order))
        zero = [0.0] * n
        for m in range(order):
            z0 = [[0.0, 0.0] for _ in sections]
            z0[m // 2][m % 2] = 1.0
            y, hs = sim_biquads(sections, zero, z0)
            C[:, m] = y
            E[:, m] = hs[n - 1]
        self.H, self.C, self.D, self.E = (M.astype(F32) for M in (H, C, D, E))
        self.z = np.zeros(order, F32)
        self.y = np.zeros(n, F32)
        self.zn = np.zeros(order, F32)
        self.ztmp = np.zeros(order, F32)
        self.tc = np.zeros(n, F32)

    def reset(self):
        self.z[:] = 0

    def _run(self, x, out):
        np.dot(self.H, x, out=out)
        np.dot(self.C, self.z, out=self.tc)
        out += self.tc
        np.dot(self.D, x, out=self.zn)
        np.dot(self.E, self.z, out=self.ztmp)
        self.zn += self.ztmp
        self.zn += _AD          # anti-denormal : l'etat ne reste jamais coince dans le domaine denormal (x18 en cout)
        self.z, self.zn = self.zn, self.z

    def set_sections(self, sections):
        self._pend = BlockFilter(sections)

    def swap_in(self, other: "BlockFilter"):
        """Thread interface : adopte un filtre deja construit (cout nul)."""
        self._pend = other

    def process(self, x):
        p = self._pend
        if p is not self._seen:                         # nouveau reglage : fondu d'une trame ancien -> nouveau
            self._seen = p
            if p is not None and len(p.z) == len(self.z):
                p.z[:] = self.z
                np.copyto(self._t2, x)
                self._run(x, self.y)                    # ancien
                p._run(self._t2, p.y)                   # nouveau (etat repris de l'ancien)
                np.subtract(p.y, self.y, out=self._t)
                self._t *= RAMP
                self._t += self.y
                x[:] = self._t
                for k in ("sections", "H", "C", "D", "E", "z", "y", "zn", "ztmp", "tc"):
                    setattr(self, k, getattr(p, k))
                return
        self._run(x, self.y)
        x[:] = self.y


# ------------------------------------------------------------------ outils
class GainRamp:
    """Applique un gain qui varie lineairement entre deux valeurs sur la trame."""

    def __init__(self):
        self.g = 1.0
        self.buf = np.zeros(FRAME, F32)
        self.sc = _Scalars()

    def apply(self, x, g1: float):
        g0 = self.g
        sc = self.sc
        if g0 == g1:
            if g1 != 1.0:
                sc.a[...] = g1
                x *= sc.a
        else:
            sc.a[...] = g1 - g0
            sc.b[...] = g0
            np.multiply(RAMP, sc.a, out=self.buf)
            self.buf += sc.b
            x *= self.buf
        self.g = g1


class NoiseFloor:
    """Plancher de bruit par statistiques de minimum (fenetre ~3 s en 6 sous-fenetres), montee limitee."""

    def __init__(self, init_db=-55.0, win=100, nwin=6, lo=-80.0, hi=-40.0, rise_db_s=8.0):
        self.init, self.win, self.nwin, self.lo, self.hi = init_db, win, nwin, lo, hi
        self.rise = rise_db_s * FRAME / RATE
        self.reset()

    def reset(self):
        self.ring = [math.inf] * self.nwin
        self.pos = 0
        self.cur = math.inf
        self.cnt = 0
        self.db = self.init
        self.frames = 0

    def update(self, L: float) -> float:
        if L < self.cur:
            self.cur = L
        self.cnt += 1
        self.frames += 1
        if self.cnt >= self.win:
            self.ring[self.pos] = self.cur
            self.pos = (self.pos + 1) % self.nwin
            self.cur = math.inf
            self.cnt = 0
        m = min(min(self.ring), self.cur)
        if m == math.inf:
            m = self.init
        m = min(max(m + 1.0, self.lo), self.hi)       # +1 dB : le minimum est biaise vers le bas
        self.db = m if m < self.db else min(m, self.db + self.rise)
        return self.db


class Stage:
    """Etage de traitement : process(x), reset(), wake() (reprise apres contournement)."""

    def process(self, x) -> None:  # pragma: no cover
        raise NotImplementedError

    def reset(self) -> None:
        pass

    def wake(self) -> None:
        self.reset()


# ------------------------------------------------------------------ 1. passe-haut
class HighPass(Stage):
    """Butterworth 4 poles. Les deux coupures (80 / 120 Hz) sont construites une fois pour toutes."""

    def __init__(self, fc: float = 80.0):
        self.f = BlockFilter(butter_hp4(fc))
        self.fc = fc

    def set_cutoff(self, fc: float):
        """Thread interface : change la coupure a chaud (nouveau filtre construit ici, fondu d'une trame)."""
        if fc == self.fc:
            return
        self.fc = fc
        self.f.set_sections(butter_hp4(fc))

    def process(self, x):
        self.f.process(x)

    def reset(self):
        self.f.reset()


# ------------------------------------------------------------------ 2. porte manuelle
class ManualGate(Stage):
    """Filtre de bruit manuel : seuil `threshold_db` (dBFS rms de la trame), maintien 300 ms, relachement
    150 ms, attenuation limitee a `depth_db` (12 dB) au lieu d'un silence. L'attaque est anticipee : le
    sous-bloc de 1,25 ms qui contient le debut du mot ouvre la porte AVANT lui (gratuit, la trame est deja
    entiere). Hors des defauts de la 2.1 (reglage avance)."""

    def __init__(self, threshold_db: float = -50.0, depth_db: float = 12.0, hold_ms: float = 300.0,
                 release_ms: float = 150.0):
        self.threshold_db = threshold_db
        self.depth = depth_db
        self.hold_frames = int(hold_ms / 5)
        self.rel_step = depth_db / (release_ms / 5)
        self.W = _interp_matrix(4)
        self.knots = np.ones(5, F32)
        self.gbuf = np.zeros(FRAME, F32)
        self.msb = np.zeros(4, F32)
        self.sq = np.zeros(FRAME, F32)
        self._sqv = self.sq.reshape(4, 60)
        self._ones = np.ones(60, F32)
        self.reset()

    def reset(self):
        self.open = False
        self.sending = True
        self.hold = 0
        self.g_db = 0.0          # demarre ouvert : pas de coupure au branchement
        self.last = 1.0

    @property
    def is_open(self) -> bool:
        return self.sending

    def process(self, x):
        ms = self.msb
        np.multiply(x, x, out=self.sq)
        np.dot(self._sqv, self._ones, out=ms)
        m0, m1, m2, m3 = ms.item(0), ms.item(1), ms.item(2), ms.item(3)
        lf = lvl_db((m0 + m1 + m2 + m3) * (1 / 240))
        t_open = self.threshold_db
        t_close = t_open - 3.0
        if lf >= t_open or (self.open and lf >= t_close):
            self.hold = self.hold_frames
            self.open = True
        elif self.hold > 0:
            self.hold -= 1
        else:
            self.open = False
        k = self.knots
        g0 = self.g_db
        if self.open:
            k0 = 0
            if g0 < -0.5:                        # ouverture : le sous-bloc de l'attaque
                lim = t_open - 4.0
                for j, mj in enumerate((m0, m1, m2, m3)):
                    if lvl_db(mj * (1 / 60)) >= lim:
                        k0 = j
                        break
            g1 = 0.0
            k[0] = self.last
            for j in range(1, 5):
                k[j] = 1.0 if j >= max(k0, 1) else self.last
        else:
            g1 = max(-self.depth, g0 - self.rel_step)
            k[0] = self.last
            k[4] = 10 ** (g1 / 20)
            last = self.last
            dk = (k.item(4) - last) * 0.25
            for j in range(1, 4):
                k[j] = last + dk * j
        self.g_db = g1
        self.last = k.item(4)
        # vrai tant que le fondu n'est pas fini : pas de coupure seche de l'envoi
        self.sending = self.open or g1 > -self.depth + 0.5
        np.dot(self.W, k, out=self.gbuf)
        x *= self.gbuf


def _interp_matrix(nk: int) -> np.ndarray:
    """(FRAME x nk+1) : poids de l'interpolation lineaire entre nk+1 noeuds equidistants (0 .. FRAME)."""
    pos = np.arange(1, FRAME + 1) / FRAME * nk
    W = np.zeros((FRAME, nk + 1), F32)
    i0 = np.minimum(pos.astype(int), nk - 1)
    f = pos - i0
    W[np.arange(FRAME), i0] = 1 - f
    W[np.arange(FRAME), i0 + 1] = f
    return W


# ------------------------------------------------------------------ 3. volume automatique
class AGC(Stage):
    """Amene le niveau de parole vers `target_db` (rms, dBFS). Le gain monte lentement, descend vite, est
    BORNE (`max_gain` / `min_gain`) et GELE hors parole (aucun retour vers 0 dB si `relax_db_s` = 0).
    Garde-fou : le gain ne monte jamais au point que le bruit de fond depasse `cible - snr_guard_db`."""

    def __init__(self, target_db=-23.0, max_gain=12.0, min_gain=-12.0, up_db_s=20.0, down_db_s=60.0,
                 peak_over=6.0, abs_gate_db=-55.0, relax_db_s=0.0, snr_guard_db=None, regular=False):
        self.target, self.gmax, self.gmin = target_db, max_gain, min_gain
        self.up = up_db_s * FRAME / RATE
        self.down = down_db_s * FRAME / RATE
        self.relax = relax_db_s * FRAME / RATE
        self.peak_over = peak_over
        self.nf = NoiseFloor()
        self.abs_gate = abs_gate_db
        self.snr_guard = snr_guard_db
        self.regular = regular
        self.ramp = GainRamp()
        self.sc = _Scalars()
        self.reset()

    def reset(self):
        self.lm = -60.0
        self.lq = self.target                   # niveau des mots faibles (suivi lent), sert a `regular`
        self.g_db = 0.0
        self.ramp.g = 1.0
        self.nf.reset()

    def wake(self):
        """Reprise apres contournement : on garde le plancher de bruit appris."""
        self.lm = -60.0
        self.g_db = 0.0
        self.ramp.g = 1.0

    @property
    def floor_db(self) -> float:
        return self.nf.db

    def process(self, x):
        e = _dot_self(x, self.sc)
        if not (e < BAD_ENERGY):                    # NaN, Inf ou debordement : trame jetee, etat remis a zero
            x[:] = 0.0
            self.reset()
            return
        L = lvl_db(e * (1 / FRAME))
        fl = self.nf.update(L)
        voiced = L > max(fl + 9.0, self.abs_gate)
        g = self.g_db
        if voiced:
            self.lm += (L - self.lm) * (0.25 if L > self.lm else 0.006)    # niveau de parole : montee vive
            tgt = self.target
            if self.snr_guard is not None:
                gcap = max(0.0, tgt - self.snr_guard - fl)           # gain maxi qui laisse le bruit sous cible - garde
                if self.regular:
                    # Quand le garde-fou bride le gain, on ne peut pas relever les mots faibles : on ramene plutot
                    # les mots forts vers le niveau que les faibles peuvent atteindre (un gain ne change pas le
                    # rapport signal/bruit ; il baisse meme le bruit des pauses, qui garde le gain du dernier mot).
                    lm = self.lm
                    self.lq += (lm - self.lq) * (0.02 if lm < self.lq else 0.0005)
                    tgt = min(tgt, self.lq + gcap)
            want = min(max(tgt - self.lm, self.gmin), self.gmax)
            if self.snr_guard is not None:
                want = min(want, gcap)
            if want > g:
                g = min(want, g + self.up)
            else:
                g = max(want, g - self.down)
            cap = self.target + self.peak_over - L                          # jamais plus de +6 dB au-dessus de la cible
            if g > cap:
                g = max(cap, self.gmin)
        elif self.relax > 0.0:
            g = g - min(self.relax, g) if g > 0 else g + min(self.relax, -g)
        self.g_db = g
        self.ramp.apply(x, 10 ** (g / 20))


# ------------------------------------------------------------------ 4. egaliseur de presence
class PresenceEQ(Stage):
    """Voix plus claire : creux 'boite' 250 Hz, bosse de presence 3 kHz, leger air 9 kHz."""

    def __init__(self, amount=1.0):
        self.f = BlockFilter([biquad("peak", 250, 0.9, -2.0 * amount), biquad("peak", 3200, 0.9, 3.0 * amount),
                              biquad("hshelf", 9000, 0.7, 2.0 * amount)])

    def process(self, x):
        self.f.process(x)

    def reset(self):
        self.f.reset()


# ------------------------------------------------------------------ 5. de-esser
class DeEsser(Stage):
    """Bande de sifflantes = (x - allpass(x))/2 ; si sa part d'energie depasse `thr_db` (relatif au total),
    on en retire. Independant du niveau de la voix. Au maximum `max_red_db` de reduction sur la bande."""

    def __init__(self, thr_db=-9.0, max_red_db=8.0, fc=6500.0, q=0.5):
        self.ap = BlockFilter([biquad("ap", fc, q)])
        self.thr, self.maxred = thr_db, max_red_db
        self.s = np.zeros(FRAME, F32)
        self.red = np.zeros(FRAME, F32)
        self.sc = _Scalars()
        self.gr = 0.0
        self.g = 1.0

    def reset(self):
        self.ap.reset()
        self.gr = 0.0
        self.g = 1.0

    def process(self, x):
        np.copyto(self.s, x)
        self.ap.process(self.s)                 # s = allpass(x)
        np.subtract(x, self.s, out=self.s)
        self.sc.a[...] = 0.5
        self.s *= self.sc.a                     # s = bande
        et = _dot_self(x, self.sc) + 1.0
        eb = _dot_self(self.s, self.sc) + 1e-3
        want = 0.0
        if et > 2.6e5:                                          # ignore les niveaux < ~ -60 dBFS rms
            ratio_db = 10 * math.log10(eb / et)
            want = min(max((ratio_db - self.thr) * 1.5, 0.0), self.maxred)
        gr = self.gr
        gr = want if want > gr else gr + (want - gr) * 0.3       # attaque immediate, relachement ~15 ms
        self.gr = gr
        g1 = 10 ** (-gr / 20)
        g0 = self.g
        if g0 == 1.0 and g1 == 1.0:
            return
        sc = self.sc
        sc.a[...] = g1 - g0
        sc.b[...] = g0
        np.multiply(RAMP, sc.a, out=self.red)
        self.red += sc.b
        self.g = g1
        np.subtract(1.0, self.red, out=self.red)                 # 1-g
        self.s *= self.red
        x -= self.s


# ------------------------------------------------------------------ 6. compresseur
class Compressor(Stage):
    def __init__(self, thr_db=-26.0, ratio=2.5, knee_db=8.0, att_ms=8.0, rel_ms=120.0):
        self.thr, self.ratio, self.knee = thr_db, ratio, knee_db
        self.ca = 1 - math.exp(-5.0 / att_ms)
        self.cr = 1 - math.exp(-5.0 / rel_ms)
        self.makeup = 0.4 * (1 - 1 / ratio) * abs(thr_db + 12)
        self.ramp = GainRamp()
        self.sc = _Scalars()
        self.env = -80.0

    def reset(self):
        self.env = -80.0
        self.ramp.g = 1.0

    def process(self, x):
        L = lvl_db(_dot_self(x, self.sc) * (1 / FRAME))
        env = self.env
        env += (L - env) * (self.ca if L > env else self.cr)
        self.env = env
        over = env - self.thr
        k = self.knee
        s = 1 - 1 / self.ratio
        if over <= -k / 2:
            red = 0.0
            mk = 0.0                                # ne comprime pas : pas de rattrapage (il remonterait le bruit)
        elif over >= k / 2:
            red = over * s
            mk = self.makeup
        else:
            red = s * (over + k / 2) ** 2 / (2 * k)
            mk = self.makeup * (over + k / 2) / k   # rattrapage progressif dans le genou
        self.ramp.apply(x, 10 ** ((mk - red) / 20))


# ------------------------------------------------------------------ 7. limiteur de sortie
class Limiter(Stage):
    """Limiteur sans anticipation : gain <= seuil/crete de la trame, rampe de 24 echantillons au debut de la
    trame, relachement ~100 ms, filet de securite (ecretage a 32000) pour les rares depassements residuels."""

    def __init__(self, ceil=LIMIT_CEIL):
        self.ceil = ceil
        self.g = 1.0
        self.rel = 1 - math.exp(-5.0 / 100.0)
        self.r24 = np.arange(1, 25, dtype=F32) / F32(24)
        self.t = np.zeros(FRAME, F32)
        self.t24 = np.zeros(24, F32)
        self.sc = _Scalars()

    def reset(self):
        self.g = 1.0

    def process(self, x):
        t = self.t
        np.abs(x, out=t)
        pk = t.item(t.argmax())
        g0 = self.g
        need = self.ceil / pk if pk > self.ceil else 1.0
        g1 = need if need < g0 else g0 + (1.0 - g0) * self.rel
        sc = self.sc
        if g1 != 1.0 or g0 != 1.0:
            if need < g0:       # attaque : rampe courte puis gain constant
                sc.a[...] = g1 - g0
                sc.b[...] = g0
                np.multiply(self.r24, sc.a, out=self.t24)
                self.t24 += sc.b
                x[:24] *= self.t24
                sc.a[...] = g1
                x[24:] *= sc.a
            else:
                sc.a[...] = g1 - g0
                sc.b[...] = g0
                np.multiply(RAMP, sc.a, out=t)
                t += sc.b
                x *= t
        self.g = g1
        np.abs(x, out=t)                           # filet de securite
        if t.item(t.argmax()) > 32000.0:
            np.clip(x, -32000.0, 32000.0, out=x)


# ------------------------------------------------------------------ interrupteur a fondu
class Bypassable:
    """Active / desactive un etage sans clic : fondu sec / traite sur `ramp_frames` trames (15 ms), etat
    remis a zero a l'activation. Desactive et stabilise : une comparaison, rien d'autre (bit a bit identique)."""

    def __init__(self, proc: Stage, enabled=False, ramp_frames=3):
        self.proc = proc
        self.target = self.w = 1.0 if enabled else 0.0
        self.step = 1.0 / ramp_frames
        self.dry = np.zeros(FRAME, F32)
        self.buf = np.zeros(FRAME, F32)
        self.sc = _Scalars()

    def set_enabled(self, on: bool):
        self.target = 1.0 if on else 0.0

    def process(self, x):
        t, w = self.target, self.w
        if w == t:
            if t:
                self.proc.process(x)
            return
        if w == 0.0:
            self.proc.wake()
        np.copyto(self.dry, x)
        self.proc.process(x)
        w1 = min(1.0, w + self.step) if t > w else max(0.0, w - self.step)
        sc = self.sc
        sc.a[...] = w1 - w
        sc.b[...] = w
        np.multiply(RAMP, sc.a, out=self.buf)
        self.buf += sc.b
        x -= self.dry
        x *= self.buf
        x += self.dry
        self.w = w1

    def settle(self):
        self.w = self.target


# ------------------------------------------------------------------ chaine d'emission
class VoiceChain:
    """Chaine d'emission (sans le suppresseur de bruit, qui est chez Dev A et s'insere entre les deux appels) :

        micro -> process_pre : [passe-haut] -> (suppresseur) -> process_post : [porte manuelle] -> [volume auto]
              -> [presence] -> [de-esser] -> [compresseur] -> [limiteur] -> envoi

    "Naturelle" = passe-haut + limiteur. "Plus claire" ajoute volume auto, presence, de-esser, compresseur.
    """

    def __init__(self, rate: int = RATE, frame: int = FRAME):
        if rate != RATE or frame != FRAME:
            raise ValueError("dsp.py est fige a 48 kHz, trames de 240 echantillons")
        self.latency_samples = 0
        self._hp = HighPass(80.0)
        self._gate = ManualGate()
        self._agc = AGC(snr_guard_db=25.0, regular=True)
        self._eq = PresenceEQ(1.0)
        self._de = DeEsser()
        self._comp = Compressor()
        self._lim = Limiter()
        B = Bypassable
        self._b_hp = B(self._hp, enabled=True)
        self._b_gate, self._b_agc = B(self._gate), B(self._agc)
        self._b_eq, self._b_de, self._b_comp = B(self._eq), B(self._de), B(self._comp)
        self._b_lim = B(self._lim, enabled=True)
        self._post = (self._b_gate, self._b_agc, self._b_eq, self._b_de, self._b_comp, self._b_lim)
        self._all = (self._b_hp,) + self._post
        self._tabs = np.zeros(FRAME, F32)
        self._clip_hold = 0
        self._gate_on = False
        self._cfg = ("natural", True, 80, True, GATE_OFF_DB)
        self.clipping = False

    # ---- thread interface
    def configure(self, *, style: str = "natural", highpass: bool = True, highpass_hz: int = 80,
                  limiter: bool = True, gate_db: float = GATE_OFF_DB) -> None:
        """Ne leve jamais, ne prend aucun verrou. Les changements sont pris a la trame suivante, avec fondu."""
        try:
            clear = style == "clear"
            hz = 120 if highpass_hz == 120 else 80
            gdb = float(gate_db)
            if gdb != gdb:
                gdb = GATE_OFF_DB
            self._cfg = ("clear" if clear else "natural", bool(highpass), hz, bool(limiter), gdb)
            self._hp.set_cutoff(float(hz))
            self._b_hp.set_enabled(bool(highpass))
            self._b_lim.set_enabled(bool(limiter))
            for b in (self._b_agc, self._b_eq, self._b_de, self._b_comp):
                b.set_enabled(clear)
            on = gdb > GATE_OFF_DB
            self._gate.threshold_db = min(gdb, -20.0)
            self._gate_on = on
            self._b_gate.set_enabled(on)
        except Exception:
            pass

    @property
    def config(self):
        return self._cfg

    # ---- thread audio
    def process_pre(self, x) -> None:
        """Avant le suppresseur : detection de saturation (lecture seule) puis passe-haut."""
        t = self._tabs
        np.abs(x, out=t)
        pk = t.item(t.argmax())
        if not (pk <= BAD_PEAK):                    # NaN, Inf ou valeur absurde : on n'empoisonne pas l'etat
            x[:] = 0.0
            self.reset()
            return
        if pk >= CLIP_LEVEL:
            self._clip_hold = CLIP_HOLD_FRAMES
            self.clipping = True
        elif self._clip_hold > 0:
            self._clip_hold -= 1
            if self._clip_hold == 0:
                self.clipping = False
        self._b_hp.process(x)

    def process_post(self, x) -> None:
        """Apres le suppresseur : porte manuelle, volume auto, presence, de-esser, compresseur, limiteur."""
        t = self._tabs
        np.abs(x, out=t)
        if not (t.item(t.argmax()) <= BAD_PEAK):    # la sortie du suppresseur peut etre invalide aussi
            x[:] = 0.0
            self.reset()
            return
        for b in self._post:
            b.process(x)

    @property
    def gate_open(self) -> bool:
        """Etat de la porte manuelle ; True si elle est desactivee. Reste vrai pendant le maintien (300 ms)
        et le fondu de fermeture."""
        if not self._gate_on and self._b_gate.w == 0.0:
            return True
        return self._gate.sending

    def reset(self) -> None:
        for b in self._all:
            b.proc.reset()
            b.settle()
        self._clip_hold = 0
        self.clipping = False

    def wake(self) -> None:
        """Apres une pause (muet, PTT) : repart propre sans oublier le plancher de bruit."""
        for b in self._all:
            b.proc.wake()
        self._clip_hold = 0
        self.clipping = False


# ------------------------------------------------------------------ reception
class Leveler:
    """Egalise le volume d'un ami (un exemplaire par ami). Cible -24 dBFS, gain BORNE a +6 / -12 dB, monte
    lentement (10 dB/s), descend vite. Garde-fou rapport signal/bruit (`snr_guard_db` = 30) : jamais de gain
    qui ferait passer le bruit de fond de l'ami au-dessus de (cible - 30 dB), donc un ami au micro bruyant
    n'est pas remonte. A appeler avec chaque trame REELLE de cet ami (pas pour une trame manquante).
    `enabled` = False : le gain revient a 0 dB par une rampe d'une trame, puis rien n'est touche."""

    def __init__(self, snr_guard_db: float = 30.0):
        self.enabled = True
        self._agc = AGC(target_db=-24.0, max_gain=6.0, min_gain=-12.0, up_db_s=10.0, down_db_s=60.0,
                        peak_over=6.0, abs_gate_db=-50.0, relax_db_s=0.0, snr_guard_db=snr_guard_db)

    @property
    def gain_db(self) -> float:
        return self._agc.g_db

    @property
    def floor_db(self) -> float:
        """Plancher de bruit estime de cet ami (dBFS)."""
        return self._agc.nf.db

    @property
    def noisy(self) -> bool:
        """Bruit de fond de l'ami > -50 dBFS, apres au moins 6 s de mesure."""
        nf = self._agc.nf
        return nf.frames >= 1200 and nf.db > -50.0

    def reset(self) -> None:
        self._agc.reset()

    def process(self, x) -> None:
        a = self._agc
        if self.enabled:
            a.process(x)
        elif a.ramp.g != 1.0:
            a.ramp.apply(x, 1.0)
            a.g_db = 0.0
            a.lm = -60.0
        else:                                # volume des amis coupé : on continue de mesurer son bruit de fond
            a.nf.update(lvl_db(_dot_self(x, a.sc) * (1 / FRAME)))   # (pour la suggestion « Nettoyer sa voix »)

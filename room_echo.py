"""Annulation de ma propre voix dans le flux des amis qui sont dans MA pièce (Vocal LAN).

Le problème
-----------
Quand un ami est dans la même pièce que moi, son micro capte aussi ma voix (par l'air). Son flux
m'arrive donc avec, en plus de SA voix, une copie de MA voix, en retard (capture + réseau + tampon anti-
saccades : typiquement 30 à 150 ms). Je m'entends en écho dans mon casque. Le suppresseur de bruit
(RNNoise) ne peut rien y faire : une voix reste une voix.

La solution
-----------
C'est un annuleur d'écho acoustique, mais « à l'envers » : la référence n'est pas ce que je joue dans mes
haut-parleurs, c'est ce que mon propre micro capte. À la réception, pour chaque ami marqué « dans ma pièce »,
on estime le trajet (air + micro de l'ami + réseau + tampon) qui transforme MON micro en la fraction de ma
voix présente dans SON flux, puis on la soustrait avant le mixage. Le filtre est adaptatif (il suit les
déplacements, le tampon réseau, etc.) et ne s'adapte que quand c'est sûr (voir « Garde-fous »).

C'est causal : mon micro est capté AVANT que la copie arrive par le réseau. Rien n'est retardé, aucune
latence n'est ajoutée à la voix de l'ami (contrairement à un traitement côté ami, non causal).

Convention (la même que dsp.py) : trames de 240 échantillons (5 ms à 48 kHz), float32, échelle int16,
traitement EN PLACE, numpy pur. Aucun thread, aucun verrou.

API
---
  bank = RefBank(frame=240, parts=64)           # un seul par Engine
  bank.push(mic)                                 # UNE fois par callback, avec mon micro (après le gain, AVANT
                                                 #   tout traitement : passe-haut, RNNoise, porte…)
  rc = RoomCanceller(bank)                       # un par ami « dans ma pièce »
  rc.process(frame)                              # sur la trame de l'ami, avant nettoyage/volume/mixage
  rc.reset(); rc.stats() -> dict                 # remise à zéro ; diagnostic (ERLE, adaptation…)

Garde-fous (ce qui évite de manger la voix de l'ami)
----------------------------------------------------
  1. Adaptation seulement quand JE parle (mon micro au-dessus de son plancher) ET que le micro de l'ami
     est plus faible que le mien : quand l'ami parle seul ou en même temps que moi, le filtre est gelé
     (il continue de soustraire ce qu'il sait, sans rien apprendre de faux).
  2. Le gain total du filtre est borné (un trajet acoustique ne peut pas amplifier ma voix).
  3. Si la sortie devient plus forte que l'entrée (filtre mal calé), la soustraction est fondue vers 0.
  4. Option « suppression résiduelle » : tant que je parle et que le flux de l'ami ne contient que mon
     écho et son bruit de fond, on baisse ce flux de quelques dB (jamais quand l'ami parle vraiment).
"""
from __future__ import annotations

import math

import numpy as np

F32 = np.float32
C64 = np.complex64
FS = 32768

DEFAULT_FRAME = 240
BOOST = 2.0                   # pas d'adaptation x3 au tout début, redescend à x1
BOOST_FRAMES = 500            # ... après 500 trames adaptées (~2,5 s de ma voix)
DEFAULT_PARTS = 64            # 64 x 5 ms = 320 ms de décalage couvert (réseau + tampon + salle)
ABS_REF_RMS_DB = -50          # en dessous, mon micro est considéré silencieux (dBFS)
ABS_REF_POW = (FS * 10 ** (ABS_REF_RMS_DB / 20)) ** 2


def _pow(a):
    return float(np.dot(a, a)) / len(a)


class RefBank:
    """Historique partagé de mon micro, en spectres (recouvrement-sauvegarde, fenêtre de 2 trames).

    Les spectres des `parts` dernières trames sont gardés dans un tampon circulaire DOUBLÉ : les `parts`
    dernières trames, de la plus ancienne à la plus récente, sont toujours une tranche contiguë (`view()`),
    donc aucun `np.roll`, aucune copie."""

    def __init__(self, frame: int = DEFAULT_FRAME, parts: int = DEFAULT_PARTS):
        self.B = int(frame)
        self.K = self.B + 1
        self.P = int(parts)
        self.win = np.zeros(2 * self.B, F32)
        self.H = np.zeros((2 * self.P, self.K), C64)
        self.HP = np.zeros((self.P, self.K), F32)  # |X|^2 de chaque trame gardée (évite de le recalculer)
        self._X = np.zeros(self.K, C64)            # tampons préalloués : rien n'est alloué dans le callback
        self._xp = np.zeros(self.K, F32)
        self._dp = np.zeros(self.K, F32)
        self.pos = 0
        self.pw = np.zeros(self.K, F32)          # somme sur les P trames de |X|^2, par raie
        self._pw_tick = 0
        self.pow_now = 0.0                        # puissance de la trame courante
        self.pow_s = 0.0                          # lissée (~10 ms)
        self.floor = ABS_REF_POW                  # plancher (statistique de minimum)
        self.active = False
        self.ticks = 0

    def reset(self):
        self.win.fill(0)
        self.H.fill(0)
        self.HP.fill(0)
        self.pw.fill(0)
        self.pos = 0
        self.pow_now = self.pow_s = 0.0
        self.floor = ABS_REF_POW
        self.active = False

    def push(self, x):
        B, P = self.B, self.P
        self.win[:B] = self.win[B:]
        self.win[B:] = x
        X = np.fft.rfft(self.win, out=self._X)
        pos = self.pos + 1
        if pos == P:
            pos = 0
        xp, dp = self._xp, self._dp               # |X|^2 = re^2 + im^2 (mêmes arrondis que le calcul direct)
        np.multiply(X.real, X.real, out=xp)
        np.multiply(X.imag, X.imag, out=dp)
        xp += dp
        hp = self.HP[pos]                         # |X|^2 de la trame qui sort de l'historique
        np.subtract(xp, hp, out=dp)
        self.pw += dp                             # une seule addition : la somme incrémentale ne dérive pas autrement
        hp[:] = xp
        self.H[pos] = X
        self.H[pos + P] = X
        self.pos = pos
        self._pw_tick += 1
        if self._pw_tick >= 512:                  # recalage : évite toute dérive des sommes incrémentales
            self._pw_tick = 0
            np.sum(self.HP, axis=0, out=self.pw)
        p = _pow(x)
        self.pow_now = p
        self.pow_s = 0.5 * self.pow_s + 0.5 * p
        # plancher : suit les minima, remonte lentement (~ +0,6 dB/s)
        self.floor = min(max(self.pow_s, 1.0), self.floor * 1.0007)
        self.active = self.pow_s > max(ABS_REF_POW, 8.0 * self.floor)
        self.ticks += 1

    def view(self):
        """(P, K) : spectres, du plus ancien au plus récent."""
        return self.H[self.pos + 1: self.pos + 1 + self.P]


class RoomCanceller:
    """Retire de la trame d'un ami la part de MA voix que son micro a captée."""

    def __init__(self, bank: RefBank, mu: float = 0.6, kappa: float = 0.05, gain_max: float = 1.5,
                 suppress_db: float = -12.0, constrain_per_frame: int = 8, lookahead_frames: int = 0):
        self.bank = bank
        B, K, P = bank.B, bank.K, bank.P
        self.B, self.K, self.P = B, K, P
        self.mu = float(mu)
        self.kappa = float(kappa)
        self.gain_max2 = float(gain_max) ** 2 * (2 * B) / 2.0     # borne sur sum|W|^2 (Parseval, demi-spectre)
        self.suppress = suppress_db < 0
        self.g_min = 10 ** (suppress_db / 20) if self.suppress else 1.0
        self.n_con = max(1, int(constrain_per_frame))
        self.W = np.zeros((P, K), C64)
        self._t1 = np.zeros((P, K), C64)
        self._t2 = np.zeros((P, K), C64)
        self._zp = np.zeros(2 * B, F32)      # [zéros | erreur] : moitié basse toujours nulle
        self._Y = np.zeros(K, C64)           # tampons préalloués : rien n'est alloué dans le callback
        self._y2 = np.zeros(2 * B, F32)
        self._e = np.zeros(B, F32)
        self._E = np.zeros(K, C64)
        self._den = np.zeros(K, F32)
        self._gk = np.zeros(K, C64)
        self._G = np.zeros((P, K), C64)
        self._w = np.zeros((max(1, int(constrain_per_frame)), 2 * B), F32)
        self._la = max(0, int(lookahead_frames))
        self._lbuf = np.zeros((self._la + 1, B), F32) if self._la else None
        self._con = 0
        self.alpha = 1.0                  # part de l'estimation réellement soustraite (garde-fou 3)
        self.g = 1.0                      # gain de suppression résiduelle
        self.d_s = self.e_s = self.y_s = 0.0
        self.d_floor = 1e12               # plancher du flux de l'ami (son bruit de fond)
        self.adapting = False
        self.n_adapt = 0
        self.n_frames = 0

    def reset(self):
        self.W.fill(0)
        self.alpha = 1.0
        self.g = 1.0
        self.d_s = self.e_s = self.y_s = 0.0
        self.d_floor = 1e12
        self.adapting = False
        self.n_adapt = self.n_frames = 0
        if self._lbuf is not None:
            self._lbuf.fill(0)

    # -- cœur ---------------------------------------------------------------------------------
    def process(self, d):
        """d : trame de l'ami (float32, 240), modifiée en place. Appeler APRÈS bank.push(mon micro)."""
        bank = self.bank
        B, K, P = self.B, self.K, self.P
        if self._lbuf is not None:                       # retard fixe de l'ami (cas où la copie de ma voix
            self._lbuf[:-1] = self._lbuf[1:]             # arriverait AVANT mon micro : micro Bluetooth, etc.)
            self._lbuf[-1] = d
            d[:] = self._lbuf[0]
        X = bank.view()
        np.multiply(self.W, X, out=self._t1)
        Y = np.einsum("pk->k", self._t1, out=self._Y)    # = somme sur les partitions (np.sum alloue ~66 Ko)
        y = np.fft.irfft(Y, 2 * B, out=self._y2)[B:]
        e = np.subtract(d, y, out=self._e)               # erreur « pleine » (sert à adapter)

        d_p = _pow(d)
        e_p = _pow(e)
        y_p = _pow(y)
        self.d_s = 0.7 * self.d_s + 0.3 * d_p
        self.e_s = 0.7 * self.e_s + 0.3 * e_p
        self.y_s = 0.7 * self.y_s + 0.3 * y_p
        self.n_frames += 1
        active = bank.active
        if not active:                                   # mon micro est calme : le flux de l'ami = son bruit
            self.d_floor = max(min(self.d_s, self.d_floor * 1.002), 1.0)

        # -- adaptation (garde-fou 1) --------------------------------------------------------
        adapt = active and d_p < self.kappa * bank.pow_now
        self.adapting = adapt
        if adapt:
            self.n_adapt += 1
            self._zp[B:] = e
            E = np.fft.rfft(self._zp, out=self._E)
            boost = 1.0 + BOOST * max(0.0, 1.0 - self.n_adapt / BOOST_FRAMES)   # filtre neuf : on apprend vite
            den = np.add(bank.pw, F32(1e3 * P), out=self._den)
            np.divide(F32(self.mu * boost), den, out=den)
            gk = np.multiply(E, den, out=self._gk)                      # (K,)
            np.conjugate(X, out=self._t2)
            self._G[:] = gk                              # gk recopié sur chaque partition : la diffusion (K,) -> (P, K)
            np.multiply(self._t2, self._G, out=self._t1)  # passerait par un tampon interne de ~66 Ko
            self.W += self._t1
            # contrainte temporelle (le filtre ne doit pas « s'enrouler ») sur quelques partitions par trame
            n = self.n_con
            j0 = self._con
            self._con = (j0 + n) % P
            if j0 + n <= P:                              # cas courant : partitions contiguës, sans allocation
                Wn = self.W[j0:j0 + n]
                w = np.fft.irfft(Wn, 2 * B, out=self._w[:n])
                w[:, B:] = 0
                for i in range(n):                       # ligne par ligne : la FFT groupée alloue ~200 Ko
                    np.fft.rfft(w[i], out=Wn[i])
            else:
                idx = [(j0 + i) % P for i in range(n)]
                w = np.fft.irfft(self.W[idx], 2 * B)
                w[:, B:] = 0
                self.W[idx] = np.fft.rfft(w).astype(C64, copy=False)
            # garde-fou 2 : le gain total du trajet reste raisonnable
            en = float(np.vdot(self.W, self.W).real)
            if en > self.gain_max2:
                self.W *= F32(math.sqrt(self.gain_max2 / en))

        # -- garde-fou 3 : ne jamais faire pire que l'entrée ---------------------------------
        # (jugé seulement quand l'estimation de l'écho est nettement au-dessus du bruit de l'ami)
        if active and self.y_s > 4.0 * self.d_floor:
            r = self.e_s / (self.d_s + 1.0)
            target = 1.0 if r <= 1.3 else max(0.0, 1.0 - (r - 1.3) * 2.0)
            self.alpha += (target - self.alpha) * (0.3 if target < self.alpha else 0.05)
        elif not active:
            self.alpha += (1.0 - self.alpha) * 0.01
        ay = np.multiply(y, F32(self.alpha), out=self._e)          # l'erreur ne sert plus : tampon réutilisé
        np.subtract(d, ay, out=d)                                    # sortie, en place

        # -- suppression résiduelle (garde-fou 4) -------------------------------------------
        if self.suppress:
            ysig = self.y_s > 2.0 * self.d_floor                  # un écho est attendu maintenant (selon le filtre)
            only_leak = ysig and _pow(d) < 3.0 * (self.d_floor + 0.5 * self.y_s)
            tg = self.g_min if only_leak else 1.0
            self.g += (tg - self.g) * (0.7 if tg < self.g else 0.1)
            if self.g < 0.999:
                d *= F32(self.g)

    def stats(self):
        erle = 10 * math.log10((self.d_s + 1.0) / (self.e_s + 1.0))
        return {
            "erle_db": erle,
            "adapt_pct": 100.0 * self.n_adapt / max(1, self.n_frames),
            "alpha": self.alpha,
            "gain": self.g,
            "leak_db": 10 * math.log10(self.y_s + 1.0) - 20 * math.log10(FS),
        }

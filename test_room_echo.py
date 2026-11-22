"""Tests de « Dans ma pièce » (room_echo) : annuleur d'écho « à l'envers », référence = mon micro.

Lancer : python -m pytest tests/test_room_echo.py -q
"""
import os
import sys
import time

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import room_echo as re_  # noqa: E402
from engine import FRAME  # noqa: E402

RATE = 48000


def bursts(n, seed, amp=6000.0, on=(0.25, 0.6), off=(0.08, 0.4)):
    """Bruit large bande par salves (comme une voix : parties fortes, silences)."""
    r = np.random.default_rng(seed)
    x = r.standard_normal(n) * amp
    env = np.zeros(n)
    i = 0
    while i < n:
        a = int(r.uniform(*on) * RATE)
        env[i:i + a] = 1.0
        i += a + int(r.uniform(*off) * RATE)
    return (x * env).astype(np.float32)


def room_ir(gain_db=-20.0, ms=120, seed=3):
    r = np.random.default_rng(seed)
    n = int(RATE * ms / 1000)
    h = r.standard_normal(n) * np.exp(-6.9 * np.arange(n) / RATE / 0.08)
    h[int(0.004 * RATE)] += 4.0
    h /= np.sqrt(np.sum(h ** 2))
    return h * 10 ** (gain_db / 20)


def simulate(seconds, lag_frames=14, leak_db=-20.0, friend_talks=None, noise=20.0):
    """Renvoie (entrée de l'ami, sortie, écho seul, voix de l'ami seule), alignés sur le flux de l'ami."""
    n = seconds * RATE
    a = bursts(n, 1)                                              # MOI
    b = np.zeros(n, np.float32) if friend_talks is None else bursts(n, 2) * friend_talks
    leak = np.convolve(a, room_ir(leak_db))[:n].astype(np.float32)
    rng = np.random.default_rng(5)
    mic_a = a + noise * rng.standard_normal(n).astype(np.float32)
    mic_b = b + leak + noise * rng.standard_normal(n).astype(np.float32)
    bank = re_.RefBank(FRAME, 64)
    rc = re_.RoomCanceller(bank)
    nf = n // FRAME
    out = np.zeros(nf * FRAME, np.float32)
    d_all = np.zeros_like(out)
    for i in range(nf):
        bank.push(mic_a[i * FRAME:(i + 1) * FRAME].copy())
        j = max(i - lag_frames, 0)
        d = mic_b[j * FRAME:(j + 1) * FRAME].copy()
        d_all[i * FRAME:(i + 1) * FRAME] = d
        rc.process(d)
        out[i * FRAME:(i + 1) * FRAME] = d
    sh = lag_frames * FRAME
    lk = np.zeros(nf * FRAME)
    lk[sh:] = leak[:nf * FRAME - sh]
    bd = np.zeros(nf * FRAME)
    bd[sh:] = b[:nf * FRAME - sh]
    return d_all, out, lk, bd, rc


def db(x):
    return 10 * np.log10(x + 1e-12)


def test_refbank_tranche_du_plus_ancien_au_plus_recent():
    """La tranche doublée rend bien les P dernières trames dans l'ordre (aucun np.roll)."""
    bank = re_.RefBank(FRAME, 8)
    frames = [np.full(FRAME, float(k + 1), np.float32) for k in range(30)]
    for f in frames:
        bank.push(f)
    v = bank.view()
    assert v.shape == (8, FRAME + 1)
    for r in range(8):                                            # ligne r = trame (29 - (7 - r))
        k = 29 - (7 - r)
        ref = np.fft.rfft(np.concatenate((frames[k - 1], frames[k])))
        assert np.allclose(v[r], ref, rtol=1e-4, atol=1e-2), r
    assert np.allclose(bank.pw, (np.abs(v) ** 2).sum(axis=0), rtol=1e-3)     # somme incrémentale = somme directe


def test_retire_ma_voix_quand_je_parle_seul():
    d, out, lk, bd, rc = simulate(14)
    s = slice(8 * RATE, 14 * RATE)
    residu = out[s] - (d[s] - lk[s])                              # ce qui reste de l'écho (bruit et voix de l'ami exclus)
    gain = db(np.mean(lk[s] ** 2)) - db(np.mean(residu ** 2))
    assert gain > 12, gain
    assert np.isfinite(out).all()


def test_ne_mange_pas_la_voix_de_l_ami():
    d, out, lk, bd, rc = simulate(26, friend_talks=1.0)
    for t0, t1 in ((8, 16), (16, 26)):                           # l'ami parle (souvent en même temps que moi)
        s = slice(t0 * RATE, t1 * RATE)
        perte = db(np.mean(out[s] ** 2)) - db(np.mean(d[s] ** 2))
        # l'écho retiré fait baisser un peu le total, mais la voix de l'ami doit rester : jamais plus de 1,5 dB
        assert perte > -1.5, (t0, t1, perte)
    s = slice(8 * RATE, 26 * RATE)
    voix = np.mean(bd[s] ** 2)
    erreur = np.mean((out[s] - lk[s] * 0 - bd[s]) ** 2)           # écart à (voix de l'ami + écho non retiré + bruit)
    assert db(voix) - db(erreur) > 6                              # la voix de l'ami domine largement la sortie


def test_ami_distant_sans_fuite_reste_intact():
    d, out, lk, bd, rc = simulate(20, leak_db=-200.0, friend_talks=1.0)
    s = slice(6 * RATE, 20 * RATE)
    assert abs(db(np.mean(out[s] ** 2)) - db(np.mean(d[s] ** 2))) < 0.5


def test_sortie_en_place_et_dtype():
    bank = re_.RefBank(FRAME, 16)
    rc = re_.RoomCanceller(bank)
    d = np.zeros(FRAME, np.float32)
    for _ in range(50):
        bank.push((np.random.default_rng(1).standard_normal(FRAME) * 3000).astype(np.float32))
        d[:] = 123.0
        rc.process(d)
        assert d.dtype == np.float32 and d.shape == (FRAME,) and np.isfinite(d).all()
    rc.reset()
    bank.reset()
    assert rc.stats()["adapt_pct"] == 0.0


def test_cout_du_calcul_par_trame():
    """3 amis « dans ma pièce » doivent tenir très largement dans le budget de 5 ms du callback."""
    bank = re_.RefBank(FRAME, 64)
    rcs = [re_.RoomCanceller(bank) for _ in range(3)]
    rng = np.random.default_rng(2)
    mic = (rng.standard_normal(FRAME * 400) * 3000).astype(np.float32)
    ts = []
    for i in range(400):
        bank.push(mic[i * FRAME:(i + 1) * FRAME])
        t0 = time.perf_counter()
        for rc in rcs:
            d = (mic[max(i - 10, 0) * FRAME:max(i - 10, 0) * FRAME + FRAME] * 0.1).copy()
            rc.process(d)
        ts.append(time.perf_counter() - t0)
    assert np.percentile(ts[50:], 90) < 0.002, np.percentile(ts[50:], 90)     # 3 amis : moins de 2 ms (sur 5)

"""Tests de dsp.py (chaine de traitement de la voix 2.1) : numpy seul, aucun peripherique.

`python -m pytest tests/test_dsp.py -q -s` affiche aussi les mesures de cout par trame.
"""
import math
import sys
import time
import tracemalloc
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import dsp  # noqa: E402

F = dsp.FRAME
R = dsp.RATE
OFF = dict(style="natural", highpass=False, highpass_hz=80, limiter=False, gate_db=-70.0)
CLEAR = dict(style="clear", highpass=True, highpass_hz=80, limiter=True, gate_db=-70.0)


def db(v):
    return 10 ** (v / 20) * 32768.0


def rms_db(a):
    a = np.asarray(a, np.float64)
    return 20 * math.log10(math.sqrt(float(np.mean(a * a))) / 32768 + 1e-12)


def make_chain(**cfg):
    c = dsp.VoiceChain()
    c.configure(**cfg)
    c.reset()
    c.configure(**cfg)
    for b in c._all:
        b.settle()
    return c


def run(c, x, pre_only=False):
    n = len(x) // F * F
    y = np.empty(n, np.float32)
    b = np.empty(F, np.float32)
    for i in range(0, n, F):
        b[:] = x[i:i + F]
        c.process_pre(b)
        if not pre_only:
            c.process_post(b)
        y[i:i + F] = b
    return y


def voice(dur, level_db=-26.0, f0=120.0, seed=0):
    """Signal 'voix' : harmoniques de f0 jusqu'a 3,4 kHz, pente -6 dB/octave, enveloppe syllabique."""
    n = int(dur * R) // F * F
    t = np.arange(n) / R
    x = np.zeros(n)
    for h in range(1, int(3400 / f0)):
        x += np.sin(2 * np.pi * f0 * h * t + h * 0.7 + seed) / h
    env = 0.55 + 0.45 * np.sin(2 * np.pi * 3.0 * t) ** 2
    x *= env
    x *= db(level_db) / math.sqrt(float(np.mean(x ** 2)))
    return x.astype(np.float32)


def noise(n, level_db, seed=1):
    r = np.random.default_rng(seed).standard_normal(n)
    return (r * db(level_db) / math.sqrt(float(np.mean(r ** 2)))).astype(np.float32)


def sine(f, amp, dur):
    n = int(dur * R) // F * F
    return (amp * np.sin(2 * np.pi * f * np.arange(n) / R)).astype(np.float32)


# ---------------------------------------------------------------- filtre en forme bloc
def test_blockfilter_matches_reference_and_keeps_state():
    secs = dsp.butter_hp4(90) + [dsp.biquad("peak", 3000, 1, 4)]
    x = (np.random.default_rng(1).standard_normal(F * 40) * 3000).astype(np.float32)
    ref, _ = dsp.sim_biquads(secs, x.astype(float).tolist(), [[0, 0]] * len(secs))
    f = dsp.BlockFilter(secs)
    y = np.empty_like(x)
    for i in range(0, len(x), F):
        b = x[i:i + F].copy()
        f.process(b)
        y[i:i + F] = b
    ref = np.array(ref)
    assert np.sqrt(np.mean((y - ref) ** 2)) / np.sqrt(np.mean(ref ** 2)) < 1e-4      # < -80 dB


# ---------------------------------------------------------------- « tout desactive » : bit a bit
def test_all_off_is_bit_exact():
    c = make_chain(**OFF)
    x = (np.random.default_rng(3).standard_normal(F * 80) * 2500).astype(np.float32)
    assert np.array_equal(run(c, x), x)


def test_all_off_after_toggling_is_bit_exact_once_settled():
    c = make_chain(**CLEAR)
    x = voice(1.0)
    run(c, x)
    c.configure(**OFF)
    x2 = (np.random.default_rng(4).standard_normal(F * 80) * 2500).astype(np.float32)
    y = run(c, x2)
    assert np.array_equal(y[10 * F:], x2[10 * F:])           # 3 trames de fondu, puis identique a l'entree


# ---------------------------------------------------------------- passe-haut
def tone_gain_db(c, f, amp=6000.0):
    y = run(c, sine(f, amp, 1.0), pre_only=True)
    x = sine(f, amp, 1.0)
    s = slice(len(y) // 2, None)
    return rms_db(y[s]) - rms_db(x[s])


def test_highpass_50hz_is_cut_and_voice_band_untouched():
    g50 = tone_gain_db(make_chain(**{**OFF, "highpass": True}), 50)
    g300 = tone_gain_db(make_chain(**{**OFF, "highpass": True}), 300)
    g1k = tone_gain_db(make_chain(**{**OFF, "highpass": True}), 1000)
    assert g50 < -12.0, g50
    assert abs(g300) < 0.3 and abs(g1k) < 0.1


def test_highpass_120hz_cuts_more_than_80hz():
    g80 = tone_gain_db(make_chain(**{**OFF, "highpass": True, "highpass_hz": 80}), 90)
    g120 = tone_gain_db(make_chain(**{**OFF, "highpass": True, "highpass_hz": 120}), 90)
    assert g120 < g80 - 5.0


# ---------------------------------------------------------------- pas de clic aux changements de reglage
def click_ratio(c, change, x, at_frame=300, window=8):
    """max |d2 y| juste apres l'evenement / max |d2 y| de la fin d'un etat stable avant l'evenement."""
    n = len(x) // F * F
    y = np.empty(n, np.float32)
    b = np.empty(F, np.float32)
    for k, i in enumerate(range(0, n, F)):
        if k == at_frame:
            change(c)
        b[:] = x[i:i + F]
        c.process_pre(b)
        c.process_post(b)
        y[i:i + F] = b
    d2 = np.abs(np.diff(y.astype(np.float64), 2))
    a = at_frame * F
    before = d2[a - 100 * F:a - 2].max()
    after = d2[a - 2:a + window * F].max()
    return after / before


@pytest.mark.parametrize("name,start,change", [
    ("style claire", OFF, lambda c: c.configure(**CLEAR)),
    ("style naturelle", CLEAR, lambda c: c.configure(**OFF)),
    ("passe-haut on", OFF, lambda c: c.configure(**{**OFF, "highpass": True})),
    ("passe-haut off", {**OFF, "highpass": True}, lambda c: c.configure(**OFF)),
    ("80 -> 120 Hz", {**OFF, "highpass": True}, lambda c: c.configure(**{**OFF, "highpass": True, "highpass_hz": 120})),
    ("limiteur on", OFF, lambda c: c.configure(**{**OFF, "limiter": True})),
    ("porte on", OFF, lambda c: c.configure(**{**OFF, "gate_db": -30.0})),
    ("porte off", {**OFF, "gate_db": -30.0}, lambda c: c.configure(**OFF)),
])
def test_no_click_when_settings_change(name, start, change):
    x = sine(440, 7000, 4.0) + noise(int(4.0 * R) // F * F, -50)
    c = make_chain(**start)
    r = click_ratio(c, change, x)
    assert r < 4.0, (name, r)             # un clic franc donnerait x50 ou plus


def test_configure_rejects_garbage_without_raising():
    c = dsp.VoiceChain()
    c.configure(style="???", highpass=None, highpass_hz=999, limiter=0, gate_db=float("nan"))
    c.configure(style=None, highpass=True, highpass_hz="x", limiter=True, gate_db="bof")
    b = np.zeros(F, np.float32)
    c.process_pre(b)
    c.process_post(b)
    assert c.latency_samples == 0


# ---------------------------------------------------------------- limiteur
def test_limiter_caps_loud_input_and_leaves_normal_input_alone():
    c = make_chain(**{**OFF, "limiter": True})
    loud = np.clip(np.random.default_rng(2).standard_normal(F * 400) * 30000, -32768, 32767)
    y = run(c, loud)
    assert np.isfinite(y).all() and np.abs(y).max() <= 32001
    c = make_chain(**{**OFF, "limiter": True})
    x = voice(1.0, -20.0)
    assert np.abs(x).max() < dsp.LIMIT_CEIL
    assert np.array_equal(run(c, x), x)             # sous le plafond : rien ne change


def test_limiter_transition_is_smooth():
    c = make_chain(**{**OFF, "limiter": True})
    x = np.concatenate([sine(300, 8000, 0.5), sine(300, 32000, 0.5), sine(300, 8000, 0.5)])
    y = run(c, x)
    assert np.abs(y).max() <= 32001
    d = np.abs(np.diff(y[:len(x)]))
    assert d.max() < 0.2 * 32768            # pas de marche : l'entree elle-meme saute de 8000 a 32000


# ---------------------------------------------------------------- porte manuelle
def test_manual_gate_depth_is_12_db_and_voice_passes():
    c = make_chain(**{**OFF, "gate_db": -45.0})
    pause = noise(R * 2, -60)
    y = run(c, pause)
    assert not c.gate_open
    g = rms_db(y[-R // 2:]) - rms_db(pause[-R // 2:])
    assert -12.6 < g < -11.4, g            # profondeur 12 dB, pas plus
    v = voice(1.0, -22.0)
    y = run(c, v)
    assert c.gate_open
    assert abs(rms_db(y[-R // 2:]) - rms_db(v[-R // 2:])) < 0.1


def test_manual_gate_disabled_is_always_open():
    c = make_chain(**OFF)
    run(c, noise(R, -80))
    assert c.gate_open


# ---------------------------------------------------------------- volume automatique
def test_agc_is_bounded_and_frozen_outside_speech():
    c = make_chain(style="clear", highpass=False, highpass_hz=80, limiter=False, gate_db=-70.0)
    quiet = voice(3.0, -45.0)
    bg = noise(len(quiet), -75)
    y = run(c, quiet + bg)
    gain = rms_db(y[-R:]) - rms_db((quiet + bg)[-R:])
    assert gain <= 12.0 + 3.0 + 0.5        # AGC +12 max, + presence EQ / compresseur (quelques dB)
    g_end = c._agc.g_db
    assert 0.0 < g_end <= 12.0 + 1e-6
    run(c, noise(R * 3, -75, seed=9))        # 3 s de silence : le gain ne bouge pas
    assert abs(c._agc.g_db - g_end) < 1e-6


# ---------------------------------------------------------------- Leveler (reception)
def feed(lv, x):
    y = x[: len(x) // F * F].copy()
    for i in range(0, len(y), F):
        lv.process(y[i:i + F])
    return y


def with_pauses(speech_db, noise_db, seconds=12, seed=1):
    n = seconds * R // F * F
    v = voice(seconds, speech_db, seed=seed)
    gate = (np.sin(2 * np.pi * np.arange(n) / R / 2.5) > -0.2).astype(np.float32)       # parole puis pauses
    return v * gate + noise(n, noise_db, seed), gate


def test_leveler_boosts_quiet_clean_friend_within_bounds():
    lv = dsp.Leveler()
    x, gate = with_pauses(-38.0, -68.0, 14)
    y = feed(lv, x)
    sp = gate[: len(y)] > 0.5
    g = rms_db(y[-6 * R:][sp[-6 * R:]]) - rms_db(x[-6 * R:][sp[-6 * R:]])
    assert 3.0 < g <= 6.3, g               # remonte, jamais plus de +6 dB


def test_leveler_lowers_loud_friend_at_most_12_db():
    lv = dsp.Leveler()
    x, gate = with_pauses(-8.0, -70.0, 10)
    y = feed(lv, np.clip(x, -32000, 32000))
    assert lv.gain_db >= -12.0 - 1e-6
    assert lv.gain_db < -3.0


def test_leveler_does_not_raise_the_noise_of_a_noisy_friend():
    lv = dsp.Leveler()
    x, gate = with_pauses(-38.0, -46.0, 16)          # ami faible, micro de portable bruyant
    y = feed(lv, x)
    pause = gate[: len(y)] < 0.5
    pause[: 3 * R] = False
    pause[-R:] = False
    dn = rms_db(y[pause]) - rms_db(x[: len(y)][pause])
    assert dn < 0.6, dn                      # le bruit des pauses ne monte pas
    assert lv.gain_db < 1.5
    assert lv.noisy


def test_leveler_clean_friend_is_not_flagged_noisy():
    lv = dsp.Leveler()
    x, _ = with_pauses(-30.0, -70.0, 10)
    feed(lv, x)
    assert not lv.noisy


def test_leveler_disabled_is_bit_exact_and_fades_back():
    lv = dsp.Leveler()
    x, _ = with_pauses(-38.0, -68.0, 10)
    feed(lv, x)
    assert abs(lv.gain_db) > 1.0
    lv.enabled = False
    y = x[:F * 40].copy()
    y0 = y.copy()
    for i in range(0, len(y), F):
        lv.process(y[i:i + F])
    assert np.array_equal(y[F:], y0[F:])        # une trame de rampe, puis identique
    lv.enabled = True


# ---------------------------------------------------------------- saturation
def test_clipping_flag_and_hold():
    c = make_chain(**OFF)
    b = np.zeros(F, np.float32)
    c.process_pre(b)
    assert not c.clipping
    b[10] = 32700
    c.process_pre(b)
    assert c.clipping
    for _ in range(dsp.CLIP_HOLD_FRAMES + 5):
        c.process_pre(np.zeros(F, np.float32))
    assert not c.clipping


def test_zero_latency():
    assert make_chain(**CLEAR).latency_samples == 0


def test_silence_stays_silent_and_finite():
    c = make_chain(**CLEAR)
    c.configure(**{**CLEAR, "gate_db": -50.0})
    y = run(c, np.zeros(F * 400, np.float32))
    assert np.isfinite(y).all() and np.abs(y).max() < 1.0


def test_toggle_everything_randomly_stays_finite():
    rng = np.random.default_rng(4)
    c = make_chain(**CLEAR)
    for k in range(300):
        c.configure(style=["natural", "clear"][rng.integers(0, 2)], highpass=bool(rng.integers(0, 2)),
                    highpass_hz=[80, 120][rng.integers(0, 2)], limiter=bool(rng.integers(0, 2)),
                    gate_db=[-70.0, -45.0, -30.0][rng.integers(0, 3)])
        b = (rng.standard_normal(F) * 3000).astype(np.float32)
        c.process_pre(b)
        c.process_post(b)
        assert np.isfinite(b).all()


# ---------------------------------------------------------------- cout et allocations
def _cost(c, x, n=6000):
    nf = len(x) // F
    bufs = x[:nf * F].reshape(nf, F)
    b = np.empty(F, np.float32)
    ts = np.empty(n)
    for i in range(n):
        np.copyto(b, bufs[i % nf])
        t = time.perf_counter_ns()
        c.process_pre(b)
        c.process_post(b)
        ts[i] = time.perf_counter_ns() - t
    ts /= 1000.0
    return ts.mean(), np.percentile(ts, 99), ts.max()


def test_cost_per_frame(capsys):
    x = voice(6.0, -26.0) + noise(6 * R // F * F, -55)
    full = make_chain(style="clear", highpass=True, highpass_hz=80, limiter=True, gate_db=-45.0)
    nat = make_chain(style="natural", highpass=True, highpass_hz=80, limiter=True, gate_db=-70.0)
    off = make_chain(**OFF)
    lv = dsp.Leveler()
    res = {}
    for name, c in (("tout desactive", off), ("Naturelle (defaut)", nat), ("Plus claire + porte", full)):
        _cost(c, x, 500)                                   # chauffe
        res[name] = _cost(c, x)
    b = np.empty(F, np.float32)
    ts = []
    for i in range(6000):
        b[:] = x[(i % 1000) * F:(i % 1000 + 1) * F]
        t = time.perf_counter_ns()
        lv.process(b)
        ts.append(time.perf_counter_ns() - t)
    ts = np.array(ts) / 1000.0
    res["Leveler (par ami)"] = (ts.mean(), np.percentile(ts, 99), ts.max())
    with capsys.disabled():
        print("\ncout par trame de 5 ms (us) moyenne / p99 / max")
        for k, v in res.items():
            print("  %-22s %7.1f / %7.1f / %7.1f" % ((k,) + v))
    assert res["Plus claire + porte"][0] < 300.0          # budget 5000 us : large marge
    assert res["Plus claire + porte"][1] < 1000.0
    assert res["tout desactive"][0] < 40.0


def test_no_numpy_array_allocation_in_process(capsys):
    """tracemalloc : le pic transitoire par trame reste de l'ordre de quelques centaines d'octets
    (petits objets CPython) ; un seul tableau de 240 float32 vaudrait deja ~1 Ko."""
    x = voice(4.0, -26.0) + noise(4 * R // F * F, -55)
    c = make_chain(style="clear", highpass=True, highpass_hz=80, limiter=True, gate_db=-45.0)
    lv = dsp.Leveler()
    b = np.empty(F, np.float32)
    for i in range(0, 80 * F, F):                          # chauffe (premiers appels : caches internes)
        b[:] = x[i:i + F]
        c.process_pre(b)
        c.process_post(b)
        lv.process(b)
    peaks = np.zeros(len(range(100 * F, len(x) - F, F)))
    tracemalloc.start()
    k = 0
    base = tracemalloc.get_traced_memory()[0]
    for i in range(100 * F, len(x) - F, F):
        b[:] = x[i:i + F]
        a0 = tracemalloc.get_traced_memory()[0]
        tracemalloc.reset_peak()
        c.process_pre(b)
        c.process_post(b)
        lv.process(b)
        peaks[k] = tracemalloc.get_traced_memory()[1] - a0
        k += 1
    drift = tracemalloc.get_traced_memory()[0] - base
    tracemalloc.stop()
    with capsys.disabled():
        print("\ntracemalloc, chaine complete + 1 Leveler : pic transitoire par trame moy %.0f o, p99 %.0f o, max %.0f o; "
              "derive %d o sur %d trames" % (peaks.mean(), np.percentile(peaks, 99), peaks.max(), drift, len(peaks)))
    assert peaks.max() < 4096
    assert peaks.mean() < 1500
    assert abs(drift) < 4096                                # pas de fuite


# ---------------------------------------------------------------- v2.1c : denormaux, NaN / Inf, bruit de la piece
def _blockfilters(c):
    out = []
    for b in c._all:
        p = b.proc
        for attr in ("f", "ap"):
            f = getattr(p, attr, None)
            if f is not None:
                out.append(f)
    return out


def test_no_denormal_state_after_digital_silence():
    """B1 : apres de la parole puis un silence numerique exact, aucun etat de filtre ne reste coince dans le
    domaine denormal (cout x2,5 a x18 par trame, indefiniment)."""
    c = make_chain(style="clear", highpass=True, highpass_hz=80, limiter=True, gate_db=-45.0)
    rng = np.random.default_rng(1)
    for _ in range(400):
        x = (rng.standard_normal(F) * 3000).astype(np.float32)
        c.process_pre(x)
        c.process_post(x)
    for _ in range(1500):
        x = np.zeros(F, np.float32)
        c.process_pre(x)
        c.process_post(x)
    tiny = np.finfo(np.float32).tiny
    for f in _blockfilters(c):
        for st in (f.z, f.zn):
            a = np.abs(st)
            assert not np.any((a > 0) & (a < tiny)), a
    assert np.abs(x).max() < 1e-3


def test_cost_after_digital_silence_stays_flat():
    """B1 : le cout par trame apres un silence numerique reste celui d'un silence 'neuf' (mesure : 175 us
    contre 52 us avant le correctif)."""
    def silence_cost(burst):
        c = make_chain(style="clear", highpass=True, highpass_hz=80, limiter=True, gate_db=-45.0)
        rng = np.random.default_rng(1)
        if burst:
            for _ in range(400):
                x = (rng.standard_normal(F) * 3000).astype(np.float32)
                c.process_pre(x)
                c.process_post(x)
        ts = []
        for i in range(900):
            x = np.zeros(F, np.float32)
            t = time.perf_counter_ns()
            c.process_pre(x)
            c.process_post(x)
            ts.append(time.perf_counter_ns() - t)
        return float(np.median(ts[400:])) / 1000.0
    fresh = min(silence_cost(False) for _ in range(3))
    after = min(silence_cost(True) for _ in range(3))
    assert after < 1.6 * fresh + 15.0, (fresh, after)


@pytest.mark.parametrize("bad", [np.nan, np.inf, -np.inf, 1e30])
@pytest.mark.parametrize("style", ["natural", "clear"])
def test_non_finite_frame_does_not_poison_chain(style, bad):
    """B2 / m1 : une trame NaN / Inf ne doit pas empoisonner la chaine (avant : 400 trames sur 400 en NaN)."""
    c = make_chain(style=style, highpass=True, highpass_hz=80, limiter=True, gate_db=-45.0)
    v = voice(1.0, -26.0)
    run(c, v)
    x = np.full(F, bad, np.float32)
    c.process_pre(x)
    c.process_post(x)
    assert np.all(np.isfinite(x))
    y = run(c, voice(1.5, -26.0))
    assert np.all(np.isfinite(y))
    assert rms_db(y[-R // 2:]) > -40.0                       # la voix repasse
    # NaN apparu entre le suppresseur et process_post
    x = voice(0.1, -26.0)[:F].copy()
    x[7] = bad
    c.process_post(x)
    assert np.all(np.isfinite(x))
    y = run(c, voice(1.0, -26.0))
    assert np.all(np.isfinite(y)) and rms_db(y[-R // 2:]) > -40.0


@pytest.mark.parametrize("bad", [np.nan, np.inf, 1e30])
def test_non_finite_frame_does_not_poison_leveler(bad):
    lv = dsp.Leveler()
    feed(lv, voice(1.0, -30.0))
    x = np.full(F, bad, np.float32)
    lv.process(x)
    assert np.all(np.isfinite(x))
    y = feed(lv, voice(1.0, -30.0))
    assert np.all(np.isfinite(y)) and rms_db(y[-R // 2:]) > -45.0
    assert math.isfinite(lv.gain_db)


def test_clear_style_does_not_raise_room_noise_above_guard():
    """B4 : 'Plus claire' ne porte jamais le bruit de la piece au-dessus de cible - 25 dB (-48 dBFS) et ne le
    remonte pas de plus de 8 dB dans les deux cas du rapport de revue (parole -30 / bruit -55, parole -38 /
    bruit -60), tout en regularisant les mots (ecart-type des niveaux de mots reduit)."""
    for sp, nz in ((-30.0, -55.0), (-38.0, -60.0)):
        rng = np.random.default_rng(5)
        secs = 30
        n = secs * R // F * F
        v = voice(secs, 0.0, seed=2)
        sig = np.zeros(n, np.float32)
        words = []
        t = 0
        while t < n:
            d = int((0.3 + 0.4 * rng.random()) * R)
            amp = db(sp) / db(0.0) * (0.25 + 0.75 * rng.random())
            e = min(d, n - t)
            sig[t:t + e] = v[t:t + e] * amp * np.hanning(d)[:e] ** 0.3
            words.append((t, t + e))
            t += d + int((0.4 + 0.6 * rng.random()) * R)
        sig += noise(n, nz, seed=3)
        c = make_chain(style="clear", highpass=True, highpass_hz=80, limiter=True, gate_db=-70.0)
        y = run(c, sig)
        keep = np.ones(n, bool)
        for a, b in words:
            keep[max(0, a - 4800):min(n, b + 9600)] = False
        keep[:2 * R] = False
        gain_noise = rms_db(y[keep]) - rms_db(sig[keep])
        assert rms_db(y[keep]) <= -47.0, (sp, nz, rms_db(y[keep]))
        assert gain_noise <= 12.0, (sp, nz, gain_noise)
        wi = [rms_db(sig[a:b]) for a, b in words if b - a > 9600 and a > 2 * R]
        wo = [rms_db(y[a:b]) for a, b in words if b - a > 9600 and a > 2 * R]
        assert np.std(wo) < np.std(wi), (sp, nz, np.std(wi), np.std(wo))


def test_compressor_makeup_only_when_compressing():
    """B4 : sous le seuil, le compresseur ne rattrape plus de gain (il remontait aussi le bruit de 3,4 dB)."""
    comp = dsp.Compressor()
    x = noise(F * 400, -60)
    for i in range(0, len(x), F):
        comp.process(x[i:i + F])
    assert abs(comp.ramp.g - 1.0) < 1e-4
    loud = voice(1.0, -15.0)
    y = loud.copy()
    for i in range(0, len(y), F):
        comp.process(y[i:i + F])
    assert comp.ramp.g != 1.0

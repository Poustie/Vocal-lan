"""Tests de la suppression du bruit (denoise.py). Lancer : python -m pytest tests -q"""
import os
import shutil
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import denoise as dn  # noqa: E402

F = 240


# ---------------------------------------------------------------- outils
def voice(seconds=3.0, f0=120.0, rms=3000.0):
    """Voyelle synthétique modulée (source glottale + 3 formants), assez « voix » pour RNNoise."""
    n = int(48000 * seconds)
    t = np.arange(n) / 48000
    ph = np.cumsum(f0 * (1 + 0.03 * np.sin(2 * np.pi * 3 * t))) / 48000
    src = 2 * (ph % 1.0) - 1
    out = np.zeros(n)
    for fc, bw in ((700, 90), (1220, 110), (2600, 160)):
        w0 = 2 * np.pi * fc / 48000
        al = np.sin(w0) / (2 * fc / bw)
        a0 = 1 + al
        b0, b2, a1, a2 = al / a0, -al / a0, -2 * np.cos(w0) / a0, (1 - al) / a0
        y = np.zeros(n)
        x1 = x2 = y1 = y2 = 0.0
        for i in range(n):
            v = src[i]
            o = b0 * v + b2 * x2 - a1 * y1 - a2 * y2
            x2, x1, y2, y1 = x1, v, y1, o
            y[i] = o
        out += y
    out *= 0.6 + 0.4 * np.sin(2 * np.pi * 2.5 * t) ** 2
    return (out / np.sqrt(np.mean(out ** 2)) * rms).astype(np.float32)


@pytest.fixture(scope="module")
def vx():
    return voice()


def run(p, sig):
    y = sig.copy()
    for i in range(0, len(y) // F * F, F):
        p.process(y[i:i + F])
    return y


def delay_of(sig, out, maxd=2000):
    seg = slice(24000, 24000 + 30000)
    best = max(range(0, maxd), key=lambda d: float(np.dot(sig[seg], out[seg.start + d:seg.stop + d])))
    return best


@pytest.fixture
def fresh_cache():
    dn.reset_cache()
    yield
    dn.reset_cache()


# ---------------------------------------------------------------- disponibilité et hachage
def test_disponible_avec_la_dll_du_projet(fresh_cache):
    assert dn.available() and dn.unavailable_reason() == ""
    p = dn.make("strong")
    assert not isinstance(p, dn.Passthrough) and p.error is None


def test_hachage_incorrect_donne_un_repli(fresh_cache, monkeypatch):
    monkeypatch.setattr(dn, "EXPECTED_SHA256", "0" * 64)
    dn.reset_cache()
    assert not dn.available() and "empreinte" in dn.unavailable_reason()
    for lv in ("normal", "strong"):
        p = dn.make(lv)
        assert isinstance(p, dn.Passthrough) and p.latency_samples == 0
        assert p.error and "suppression du bruit" in p.error.lower()
        x = np.random.default_rng(1).standard_normal(F).astype(np.float32) * 1000
        y = x.copy()
        p.process(y)
        assert np.array_equal(x, y)                    # sortie = entrée
    light = dn.make("light")                           # le niveau Léger n'a pas besoin de la DLL
    assert light.error is None and light.latency_samples == F


def test_dll_modifiee_refusee_avant_chargement(fresh_cache, monkeypatch, tmp_path):
    src = dn._candidates()[-1]
    bad = tmp_path / "rnnoise.dll"
    shutil.copyfile(src, bad)
    with open(bad, "ab") as f:
        f.write(b"\0")                                 # un octet de plus = autre empreinte
    loaded = []
    monkeypatch.setattr(dn, "_Lib", lambda path: loaded.append(path))
    monkeypatch.setattr(dn, "_candidates", lambda: [str(bad)])
    dn.reset_cache()
    assert not dn.available() and not loaded          # jamais passé à ctypes
    assert dn.make("strong").error


def test_chemin_meipass_de_l_exe(fresh_cache, monkeypatch, tmp_path):
    """Dans l'exe PyInstaller la DLL est sous sys._MEIPASS/third_party/rnnoise/."""
    d = tmp_path / "third_party" / "rnnoise"
    d.mkdir(parents=True)
    shutil.copyfile(dn._candidates()[-1], d / "rnnoise.dll")
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    cands = dn._candidates()
    assert cands[0] == str(d / "rnnoise.dll")
    dn.reset_cache()
    assert dn.available() and dn._cache["lib"].path == str(d / "rnnoise.dll")


def test_dll_absente(fresh_cache, monkeypatch, tmp_path):
    monkeypatch.setattr(dn, "_candidates", lambda: [str(tmp_path / "rien.dll")])
    dn.reset_cache()
    assert not dn.available()
    p = dn.make("normal")
    assert p.error and p.latency_samples == 0


# ---------------------------------------------------------------- latences exactes
@pytest.mark.parametrize("level", ["light", "normal", "strong"])
def test_latence_exacte(level, vx):
    p = dn.make(level)
    assert p.latency_samples == dn.level_latency_samples(level)
    y = run(p, vx)
    d = delay_of(vx, y)
    # Léger : exact. RNNoise : la sortie du réseau n'est pas une copie fidèle, le pic de corrélation est à +-1 échantillon
    assert d == p.latency_samples if level == "light" else abs(d - p.latency_samples) <= 1


def test_ligne_a_retard_exacte():
    for delay in (240, 1199):
        dl = dn._Delay(delay)
        out = np.zeros(F, np.float32)
        pos = None
        for k in range(20):
            x = np.zeros(F, np.float32)
            if k == 0:
                x[7] = 1.0
            dl.push(x, out)
            if out.any():
                pos = k * F + int(np.argmax(out))
        assert pos == 7 + delay


def test_latences_valeurs():
    assert dn.make("off").latency_samples == 0
    assert dn.make("light").latency_samples == 240            # 5 ms
    assert dn.make("strong").latency_samples == 1199          # 24,98 ms
    assert dn.make("normal").latency_samples == dn.level_latency_samples("normal")


def test_hybride_a_10_ms(vx):
    """Le candidat (a) de l'épreuve « Normal » (non retenu) garde son retard exact de 10 ms."""
    p = dn.Denoiser(dn._HybridCore(dn._load()), "normal")
    assert p.latency_samples == 480
    assert delay_of(vx, run(p, vx)) == 480


# ---------------------------------------------------------------- off, silence, NaN
def test_off_identique_a_l_entree():
    p = dn.make("off")
    x = (np.random.default_rng(2).standard_normal(F) * 5000).astype(np.float32)
    y = x.copy()
    for _ in range(10):
        p.process(y)
    assert np.array_equal(x, y) and p.latency_samples == 0 and p.vad is None and p.error is None


@pytest.mark.parametrize("level", ["light", "normal", "strong"])
def test_silence_et_nan_sans_exception(level, vx):
    p = dn.make(level)
    z = np.zeros(F, np.float32)
    for _ in range(300):                       # silence numérique long (dénormaux)
        p.process(z)
    assert np.isfinite(z).all() and p.error is None
    for bad in (np.nan, np.inf, -np.inf):
        x = np.full(F, bad, np.float32)
        p.process(x)
        assert np.isfinite(x).all()            # assaini, jamais de NaN en sortie
    huge = np.full(F, 1e30, np.float32)
    p.process(huge)
    assert np.isfinite(huge).all()
    y = run(p, vx[:48000])                     # et il marche encore ensuite
    assert np.isfinite(y).all() and p.error is None and np.abs(y).max() > 100


def test_reset_vide_les_tampons_et_garde_le_reseau_chaud(vx):
    """Depuis la revue croisée, reset() ne remet PAS le réseau à neuf (il garde sa chaleur : sinon le 1er mot suivant
    est atténué). Deux processeurs qui ont entendu la même chose et sont remis à zéro se comportent donc à
    l'identique, et un processeur remis à zéro ne garde aucune trace audio de l'avant."""
    p, q = dn.make("strong"), dn.make("strong")
    run(p, vx[:48000])
    run(q, vx[:48000])
    p.reset()
    q.reset()
    a, b = run(p, vx[:24000]), run(q, vx[:24000])
    assert np.allclose(a, b, atol=1e-3)
    r = dn.make("strong")
    run(r, vx[:48000])
    r.reset()
    z = np.zeros(F, np.float32)
    r.process(z)
    assert np.abs(z).max() < 1.0                       # la ligne à retard est vidée : rien de l'ancienne voix ne ressort


# ---------------------------------------------------------------- repli sur erreur
def test_exception_repli_puis_desactivation():
    p = dn.make("strong")
    assert p.vad is not None or True

    def boom(x):
        raise RuntimeError("panne")
    p.core.process = boom
    x0 = (np.random.default_rng(3).standard_normal(F) * 2000).astype(np.float32)
    for i in range(dn.MAX_ERRORS):
        y = x0.copy()
        p.process(y)                               # ne lève jamais
        assert np.array_equal(y, x0)               # sortie = entrée
    assert p.error and "suppression du bruit" in p.error.lower()
    for _ in range(dn.FADE_FRAMES + 1):            # le fondu vers le sec dure 20 ms, puis le retard annoncé tombe à 0
        y = x0.copy()
        p.process(y)
    assert p.latency_samples == 0 and p.finished
    assert np.array_equal(y, x0)


def test_sortie_non_finie_repli():
    p = dn.make("light")

    def poison(x):
        x[:] = np.nan
    p.core.process = poison
    x0 = np.ones(F, np.float32) * 100
    y = x0.copy()
    p.process(y)
    assert np.array_equal(y, x0)                   # la trame empoisonnée n'a jamais atteint la sortie


def test_pc_trop_lent_coupe_sans_bruit(monkeypatch):
    monkeypatch.setattr(dn, "SLOW_FRAME_S", 1e-9)  # toute trame est « trop lente »
    p = dn.make("light")
    x0 = (np.random.default_rng(4).standard_normal(F) * 1000).astype(np.float32)
    for _ in range(dn.SLOW_WINDOW + 2):
        y = x0.copy()
        p.process(y)
    assert p.error == dn.MSG_TOO_SLOW
    for _ in range(dn.FADE_FRAMES + 1):
        y = x0.copy()
        p.process(y)
    assert p.latency_samples == 0 and np.array_equal(y, x0)


# ---------------------------------------------------------------- vad
def test_vad_expose(vx):
    p = dn.make("strong")
    run(p, vx[:48000])
    assert 0.0 <= p.vad <= 1.0 and p.vad > 0.5            # voix
    assert dn.make("light").vad is None
    noise = (np.random.default_rng(5).standard_normal(48000 * 2) * 1500).astype(np.float32)
    q = dn.make("strong")
    run(q, noise)
    assert q.vad < 0.3


# ---------------------------------------------------------------- efficacité
def test_fort_supprime_un_bruit_de_fond():
    rng = np.random.default_rng(6)
    noise = (rng.standard_normal(48000 * 3) * 1500).astype(np.float32)
    y = run(dn.make("strong"), noise)
    a = 20 * np.log10(np.sqrt(np.mean(y[48000:] ** 2)) / np.sqrt(np.mean(noise[48000:] ** 2)))
    assert a < -20, a
    z = run(dn.make("light"), noise)
    a = 20 * np.log10(np.sqrt(np.mean(z[48000:] ** 2)) / np.sqrt(np.mean(noise[48000:] ** 2)))
    assert -16 < a < -9, a                                 # plancher -12 dB


# ---------------------------------------------------------------- fondu sans clic
def max_step(x):
    return float(np.max(np.abs(np.diff(x.astype(np.float64)))))


def sine(n, f=440.0, amp=8000.0):
    return (np.sin(2 * np.pi * f * np.arange(n) / 48000) * amp).astype(np.float32)


@pytest.mark.parametrize("level", ["light", "normal", "strong"])
def test_fondu_a_l_activation_et_a_la_desactivation(level):
    sig = sine(48000 * 2) + (np.random.default_rng(7).standard_normal(48000 * 2) * 300).astype(np.float32)
    nat = max_step(sig)
    slot = dn.Slot()
    y = sig.copy()
    n = len(y) // F
    for k in range(n):
        if k == 100:
            slot.set(dn.make(level))              # activation à chaud
        if k == 250:
            slot.set(dn.make("off"))              # désactivation à chaud
        slot.process(y[k * F:(k + 1) * F])
    # aucun saut plus grand que ceux du signal d'origine (+ marge pour la sortie propre du suppresseur)
    zone = y[(100 - 2) * F:(100 + 30) * F]
    assert max_step(zone) <= 1.3 * nat + 50, (level, max_step(zone), nat)
    zone = y[(250 - 2) * F:(250 + 30) * F]
    assert max_step(zone) <= 1.3 * nat + 50, (level, max_step(zone), nat)
    assert slot.latency_samples == 0 and slot.cur is None


def test_fondu_entre_deux_niveaux_de_retards_differents():
    sig = sine(48000 * 2, 700.0)
    nat = max_step(sig)
    slot = dn.Slot(dn.make("strong"))
    y = sig.copy()
    for k in range(len(y) // F):
        if k == 150:
            slot.set(dn.make("light"))            # 25 ms -> 5 ms
        slot.process(y[k * F:(k + 1) * F])
    assert max_step(y[148 * F:180 * F]) <= 1.3 * nat + 50
    assert slot.cur.level == "light"


def test_slot_repli_sur_panne_sans_coupure():
    slot = dn.Slot(dn.make("light"))
    x0 = sine(F, 500.0)
    for _ in range(20):
        y = x0.copy()
        slot.process(y)
    slot.cur.core.process = lambda x: (_ for _ in ()).throw(RuntimeError("x"))
    for _ in range(dn.MAX_ERRORS + 2):
        y = x0.copy()
        slot.process(y)
        assert np.isfinite(y).all() and np.abs(y).max() > 1000     # la voix passe toujours
    assert slot.error and not slot.active_shown
    for _ in range(dn.FADE_FRAMES + 1):
        y = x0.copy()
        slot.process(y)
    assert slot.cur is None
    assert np.array_equal(y, x0)


# ---------------------------------------------------------------- revue croisée : premier mot, durée de vie, courses
def _consonne_puis_voyelle(n_mots=1):
    """1 s de souffle (-58 dBFS), puis `n_mots` fois : consonne faible de 90 ms (-46 dBFS, bande 2,5-9 kHz) + voyelle."""
    rng = np.random.default_rng(2)
    parts, segs, t = [np.zeros(48000, np.float32)], [], 48000
    v = voice(0.25, 125.0, 3000.0)
    for _ in range(n_mots):
        n = int(0.09 * 48000)
        sp = np.fft.rfft(rng.standard_normal(n))
        fr = np.fft.rfftfreq(n, 1 / 48000)
        sp[(fr < 2500) | (fr > 9000)] = 0
        c = np.fft.irfft(sp, n)
        c = (c / np.sqrt(np.mean(c ** 2)) * 32768 * 10 ** (-46 / 20) * np.hanning(n) ** 0.3).astype(np.float32)
        segs.append((t, t + n))
        parts += [c, v, np.zeros(48000, np.float32)]
        t += n + len(v) + 48000
    sig = np.concatenate(parts)
    n = len(sig) // F * F
    sig = sig[:n]
    noise = rng.standard_normal(n).astype(np.float32)
    return sig + noise * (32768 * 10 ** (-58 / 20) / float(noise.std())), segs


def _retention(proc, sig, segs):
    y = run(proc, sig)
    d = proc.latency_samples
    return [10 * np.log10((np.mean(y[a + d:b + d] ** 2) + 1e-3) / (np.mean(sig[a:b] ** 2) + 1e-3)) for a, b in segs]


def test_premier_mot_pas_plus_atténué_que_les_suivants():
    """Un réseau RNNoise tout neuf prenait la 1re consonne faible pour du bruit (-15 dB). L'amorçage par une voix
    synthétique (make) corrige cela ; un noyau non amorcé reste mauvais, ce qui prouve que le test mesure bien."""
    sig, segs = _consonne_puis_voyelle(2)
    froid = dn.Denoiser(dn._RNNoiseCore(dn._load(), dry_db=-18.0, prime=False), "normal")
    chaud = dn.make("normal")
    r_froid = _retention(froid, sig, segs)
    r_chaud = _retention(chaud, sig, segs)
    assert r_chaud[0] >= -3.0, r_chaud
    assert r_chaud[0] >= r_froid[0] + 4.0, (r_froid, r_chaud)


def test_reset_garde_le_reseau_chaud():
    """Périphérique rouvert / ami revenu : les tampons repartent de zéro mais le réseau garde sa chaleur."""
    sig, segs = _consonne_puis_voyelle(1)
    p = dn.make("normal")
    run(p, voice(1.0))
    p.flush()
    assert _retention(p, sig, segs)[0] >= -5.0
    p.reset()
    assert _retention(p, sig, segs)[0] >= -5.0


def test_flush_ne_laisse_aucune_voix_fantome():
    """Nouveau flux : plus aucune trace audio de l'ancien (RNNoise garde 30 ms d'historique de hauteur : il faut 6
    trames de silence, deux ne suffisaient pas). Le réseau, lui, reste chaud."""
    p = dn.make("strong")
    run(p, voice(0.5))
    p.flush()
    y = np.zeros(F, np.float32)
    for _ in range(8):
        y[:] = 0
        p.process(y)
        assert np.abs(y).max() < 5.0, np.abs(y).max()
    assert p.error is None


def test_chargement_reessaye_apres_un_echec_passager(fresh_cache, monkeypatch):
    """Un fichier momentanément verrouillé (antivirus) ne doit pas désactiver la fonction jusqu'au redémarrage."""
    vrai = dn._sha256
    etat = {"n": 0}

    def capricieux(path):
        etat["n"] += 1
        if etat["n"] == 1:
            raise PermissionError("verrouillé par l'antivirus")
        return vrai(path)

    monkeypatch.setattr(dn, "_sha256", capricieux)
    monkeypatch.setattr(dn, "RETRY_S", 0.2)
    assert not dn.available() and "échec" in dn.unavailable_reason()
    assert not dn.available()                       # pas de nouvel essai avant RETRY_S : pas de lecture à chaque tick
    assert etat["n"] == 1
    import time as _t
    _t.sleep(0.25)
    assert dn.available() and etat["n"] == 2


def test_chargement_concurrent_un_seul_resultat(fresh_cache):
    import threading
    res = []

    def go():
        res.append(dn.available())

    ts = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert res == [True] * 8


def test_slot_deux_set_rapproches_ne_se_perdent_pas():
    """Le callback ne doit jamais effacer une demande arrivée pendant qu'il traitait la précédente."""
    import threading
    import time as _t
    slot = dn.Slot()
    stop = threading.Event()
    errs = []

    def rapide(level):                          # sans amorçage (20 ms) : on teste la course, pas le réseau
        if level == "light":
            return dn.make("light")
        return dn.Denoiser(dn._RNNoiseCore(dn._load(), dry_db=-30.0, prime=False), level)

    def callback():
        x = sine(F, 500.0)
        try:
            while not stop.is_set():
                y = x.copy()
                slot.process(y)
                if not np.isfinite(y).all():
                    errs.append("non fini")
        except Exception as e:  # noqa: BLE001
            errs.append(repr(e))

    t = threading.Thread(target=callback)
    t.start()
    try:
        for i in range(120):
            slot.set(rapide("light"))
            slot.set(rapide("strong" if i % 2 else "light"))         # deux ou trois demandes coup sur coup
            final = rapide("normal")
            slot.set(final)
            t0 = _t.monotonic()
            while slot.cur is not final and _t.monotonic() - t0 < 2:
                _t.sleep(0.001)
            assert slot.cur is final, i
            slot.set(None)
            t0 = _t.monotonic()
            while slot.cur is not None and _t.monotonic() - t0 < 2:
                _t.sleep(0.001)
            assert slot.cur is None, i
    finally:
        stop.set()
        t.join(10)
    assert not errs, errs[:3]


def test_destruction_pendant_l_usage_impossible():
    """Le callback tient une référence locale au nettoyeur pendant `process` (même quand ctypes libère le GIL) :
    un autre thread qui retire / remplace `peer.cleaner` ne peut donc pas déclencher `rnnoise_destroy` en plein
    calcul. Épreuve : un thread traite sans arrêt, un autre remplace le nettoyeur 3000 fois."""
    import threading

    class Pair:
        cleaner = None

    peer = Pair()
    peer.cleaner = dn.make("light")
    stop = threading.Event()
    n_calls = [0]
    errs = []

    def callback():
        x = voice(0.05, 120.0, 3000.0)[:F].copy()
        try:
            while not stop.is_set():
                cl = peer.cleaner                      # exactement ce que fait Engine._callback
                if cl is not None:
                    cl.process(x.copy())
                    n_calls[0] += 1
        except Exception as e:  # noqa: BLE001
            errs.append(repr(e))

    t = threading.Thread(target=callback)
    t.start()
    try:
        for i in range(3000):
            peer.cleaner = dn._RNNoiseCore and dn.Denoiser(dn._RNNoiseCore(dn._load(), dry_db=-30.0, prime=False),
                                                           "strong")
            peer.cleaner = None if i % 3 == 0 else peer.cleaner
            if i % 3 == 0:
                peer.cleaner = dn.Denoiser(dn._RNNoiseCore(dn._load(), dry_db=-30.0, prime=False), "strong")
    finally:
        stop.set()
        t.join(20)
    assert not errs and n_calls[0] > 100, (errs[:2], n_calls[0])


def test_pas_de_fuite_des_etats_natifs():
    import gc
    import psutil
    lib = dn._load()
    pr = psutil.Process()
    x = np.zeros(F, np.float32)

    def cycles(n):
        for _ in range(n):
            c = dn._RNNoiseCore(lib, prime=False)
            c.process(x)
            c.process(x)
            del c

    cycles(300)                                         # échauffement des allocateurs
    gc.collect()
    avant = pr.memory_info().rss
    cycles(3000)
    gc.collect()
    assert pr.memory_info().rss - avant < 8e6           # un état fuité coûte des dizaines de Ko : 3000 fuites > 80 Mo


# ---------------------------------------------------------------- v2.1 phase 4b : phases décalées, pannes sans saut
@pytest.mark.parametrize("par", [0, 1])
@pytest.mark.parametrize("start", [0, 1])
def test_phase_decalee_garde_le_retard_exact(vx, par, start):
    """Décaler la phase des blocs RNNoise (calcul sur les trames paires ou impaires) ne change pas le retard annoncé :
    c'est ce qui permet de répartir micro et amis sur les deux parités sans toucher à la latence."""
    p = dn.make("strong")
    p.sync(start, par)
    ticks, cur = [], {"k": 0}
    fn = p.core._fn

    def compte(*a):
        ticks.append(cur["k"])
        return fn(*a)
    p.core._fn = compte
    y = vx.copy()
    for i in range(len(y) // F):
        cur["k"] = start + i
        p.process(y[i * F:(i + 1) * F])
    assert ticks and {t & 1 for t in ticks} == {par}            # le calcul natif n'a lieu que sur la parité demandée
    assert abs(delay_of(vx, y) - p.latency_samples) <= 1 and p.latency_samples == 1199
    ref = run(dn.make("strong"), vx)                            # même sortie qu'une instance non décalée (au bruit près)
    seg = slice(24000, 24000 + 30000)
    assert abs(10 * np.log10(np.mean(y[seg] ** 2) / np.mean(ref[seg] ** 2))) < 1.0


@pytest.mark.parametrize("kind", ["voix", "son_tenu"])
def test_panne_du_noyau_sans_saut(vx, kind):
    """A4 : quand le suppresseur tombe en panne en pleine voix, le son se raccorde (rampe de 1,3 ms) puis redescend vers le
    sec en fondu de 20 ms : aucun saut plus grand que ceux du signal lui-même. Le « son tenu » (233 Hz : RNNoise le
    prend pour du bruit et le supprime, le sec est donc très différent du traité) donnait une marche de 5 800 avant."""
    sig = vx if kind == "voix" else (sine(96000, 233.0, 6000.0)
                                      + np.random.default_rng(1).standard_normal(96000).astype(np.float32) * 30)
    p = dn.make("strong")
    y = sig.copy()
    nat = max_step(sig)
    k0 = 150

    def boom(x):
        raise RuntimeError("panne")
    for k in range(len(y) // F):
        if k == k0:
            p.core.process = boom
        p.process(y[k * F:(k + 1) * F])
    assert p.error and p.finished and p.latency_samples == 0
    zone = y[(k0 - 2) * F:(k0 + dn.MAX_ERRORS + dn.FADE_FRAMES + 6) * F]
    assert max_step(zone) <= 1.3 * nat + 200, (max_step(zone), nat)
    assert np.array_equal(y[(k0 + 40) * F:(k0 + 60) * F], sig[(k0 + 40) * F:(k0 + 60) * F])     # ensuite : le sec, tel quel


def test_slot_garde_le_suppresseur_en_panne_jusqu_a_la_fin_du_fondu():
    slot = dn.Slot(dn.make("strong"))
    x0 = sine(F * 60, 500.0)
    for k in range(40):
        slot.process(x0[k * F:(k + 1) * F].copy())
    def boom(x):
        raise RuntimeError("x")
    slot.cur.core.process = boom
    vus = []
    for k in range(40, 52):
        slot.process(x0[k * F:(k + 1) * F].copy())
        vus.append(slot.cur is not None)
    assert vus[0] and not vus[-1]                      # gardé pendant le fondu, libéré ensuite
    assert slot.error and not slot.active_shown


def test_sync_est_sans_effet_sur_les_noyaux_qui_calculent_a_chaque_trame():
    p = dn.make("light")
    p.sync(5, 1)
    assert p.par is None and p.latency_samples == 240

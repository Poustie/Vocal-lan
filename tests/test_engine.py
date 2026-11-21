"""Tests du moteur : sans carte son (FakeAudio), sur localhost, avec des ports explicites.

Lancer : python -m pytest tests -q
"""
import json
import os
import random
import socket
import struct
import sys
import time

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import engine as eng  # noqa: E402
import netutil  # noqa: E402
from audiodev import FakeAudio  # noqa: E402
from engine import AUDIO_SIZE, FRAME, HEADER, HS, MAGIC, PROTO, Engine, EngineError  # noqa: E402
from hotkeys import Hotkeys, key_label  # noqa: E402
from settings import Settings  # noqa: E402

if sys.platform == "win32":  # sleep(5 ms) réellement proche de 5 ms
    import ctypes
    ctypes.windll.winmm.timeBeginPeriod(1)


# ---------------------------------------------------------------- outils
def free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait_for(cond, timeout=5.0, step=0.02):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return True
        time.sleep(step)
    return False


def sine(k, freq=1000.0, amp=8000.0):
    n = k * FRAME + np.arange(FRAME)
    return (np.sin(2 * np.pi * freq * n / 48000) * amp).astype(np.int16)


# Réglages « comme la 2.0 » : aucun traitement ajouté par la 2.1 (les tests historiques mesurent des amplitudes exactes)
V20 = dict(denoise_level="off", voice_style="natural", fx_highpass=False, fx_limiter=False, recv_leveler=False)


def mk_settings(tmp_path, name, port, peers=(), **kw):
    s = Settings.load(directory=tmp_path / name, legacy=tmp_path / "pas_de_legacy.json")
    s.name, s.port, s.manual_peers = name, port, list(peers)
    for k, v in {**V20, **kw}.items():
        setattr(s, k, v)
    return s


def events_of(e, kind=None):
    out = []
    while not e.events.empty():
        out.append(e.events.get())
    return [x for x in out if kind is None or x.kind == kind]


@pytest.fixture
def rig(tmp_path):
    """Fabrique de moteurs de test (localhost, sans broadcast, FakeAudio), arrêtés en fin de test."""
    made = []

    def make(name, port=None, peers=(), online=True, audio=None, **kw):
        port = port or free_port()
        s = mk_settings(tmp_path, name, port, peers, **kw)
        a = audio or FakeAudio()
        e = Engine(s, audio=a, bind_host="127.0.0.1", broadcast=False)
        e.start(online=online)
        made.append(e)
        return e, a

    make.tmp = tmp_path
    yield make
    for e in made:
        e.shutdown()


class RawPeer:
    """Faux participant en socket brut, pour fabriquer des paquets précis."""

    def __init__(self, target_port, sid=b"\x0a\x0b\x0c\x0d", auto_pong=False):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.05)
        self.target = ("127.0.0.1", target_port)
        self.sid, self.auto_pong = sid, auto_pong
        self.seq = self.sent = 0

    def send(self, kind, payload=b"", seq=0, ver=PROTO, magic=MAGIC):
        self.sock.sendto(HEADER.pack(magic, ver, kind, bytes(4), self.sid, seq) + payload, self.target)

    def beacon(self, name="Raw", flags=0):
        self.send(b"B", bytes([flags]) + name.encode())

    def audio(self, pcm, seq=None, sent=None):
        seq = self.seq if seq is None else seq
        sent = self.sent if sent is None else sent
        self.send(b"A", struct.pack("!I", sent) + np.asarray(pcm, np.int16).tobytes(), seq=seq)
        self.seq, self.sent = seq + 1, sent + 1

    def recv_all(self, timeout=0.3):
        out, t0 = [], time.time()
        while time.time() - t0 < timeout:
            try:
                data, _ = self.sock.recvfrom(4096)
            except OSError:
                continue
            kind = data[3:4]
            out.append((kind, data))
            if kind == b"P" and self.auto_pong:
                self.sock.sendto(HEADER.pack(MAGIC, PROTO, b"Q", bytes(4), self.sid, 0) + data[HS:], self.target)
        return out

    def close(self):
        self.sock.close()


# ---------------------------------------------------------------- découverte et audio
def test_decouverte_mutuelle(rig):
    pb = free_port()
    a, _ = rig("Alice", peers=[f"127.0.0.1:{pb}"])
    b, _ = rig("Bob", port=pb)
    assert wait_for(lambda: len(a.snapshot().peers) == 1 and len(b.snapshot().peers) == 1)
    pa = a.snapshot().peers[0]
    assert pa.name == "Bob" and pa.ip == "127.0.0.1" and len(pa.id) == 8
    assert b.snapshot().peers[0].name == "Alice"
    assert wait_for(lambda: a.snapshot().peers[0].ping_ms is not None)
    assert a.snapshot().peers[0].hears_me
    assert any(ev.peer_name == "Bob" for ev in events_of(a, "peer_joined"))
    assert a.snapshot().network == "ok"


def test_sinusoide_transmise(rig):
    pb = free_port()
    a, aa = rig("Alice", peers=[f"127.0.0.1:{pb}"])
    b, ba = rig("Bob", port=pb)
    assert wait_for(lambda: len(a.snapshot().peers) == 1 and len(b.snapshot().peers) == 1)
    out = []
    for k in range(260):
        aa.tick(sine(k))
        time.sleep(0.005)
        out.append(ba.tick())
    out = np.concatenate(out)
    tail = out[-100 * FRAME:].astype(np.float64)
    assert 5000 < np.sqrt(np.mean(tail ** 2)) < 6200  # 8000/sqrt(2) = 5657
    spec = np.abs(np.fft.rfft(tail * np.hanning(len(tail))))
    assert abs(np.argmax(spec) * 48000 / len(tail) - 1000) < 30
    # latence en trames : la première trame non nulle arrive vite (quelques trames de tampon)
    first = int(np.argmax(np.abs(out) > 100)) // FRAME
    assert first < 40
    pv = b.snapshot().peers[0]
    assert pv.speaking and pv.level > 0.5 and pv.loss_pct == 0.0
    assert pv.buffer_ms >= 10


def test_filtre_bruit_et_maintien(rig):
    a, aa = rig("Alice", gate_db=-40.0)
    rp = RawPeer(a.snapshot().port, auto_pong=True)
    rp.beacon()
    assert wait_for(lambda: len(a.snapshot().peers) == 1)
    for k in range(150):  # la porte démarre ouverte (pas de coupure au branchement) puis se ferme
        aa.tick(np.zeros(FRAME, np.int16) + (k % 3))  # bruit très faible
    before = a._sent
    for k in range(50):
        aa.tick(np.zeros(FRAME, np.int16) + (k % 3))
    assert a._sent == before and not a.snapshot().transmitting
    for k in range(20):
        aa.tick(sine(k, amp=9000))
    assert a._sent == before + 20 and a.snapshot().transmitting
    before = a._sent
    for k in range(200):
        aa.tick(np.zeros(FRAME, np.int16))
    held = a._sent - before
    assert 58 <= held <= 100  # maintien de 300 ms + fondu de fermeture (150 ms) : jamais de coupure sèche
    assert not a.snapshot().transmitting
    rp.close()


def test_ptt_mute_etats_distants(rig):
    pb = free_port()
    a, aa = rig("Alice", peers=[f"127.0.0.1:{pb}"], ptt_mode=True)
    b, _ = rig("Bob", port=pb)
    assert wait_for(lambda: len(a.snapshot().peers) == 1 and len(b.snapshot().peers) == 1)
    loud = sine(0, amp=9000)
    for _ in range(10):
        aa.tick(loud)
    assert a._sent == 0 and a.snapshot().ptt_mode and not a.snapshot().ptt_active
    a.ptt_down()
    for _ in range(10):
        aa.tick(loud)
    assert a._sent == 10 and a.snapshot().ptt_active and a.snapshot().transmitting
    a.ptt_up()
    for _ in range(10):
        aa.tick(loud)
    assert a._sent == 10
    # mute : rien n'est envoyé, "je parle en étant muet" est détecté, l'état est annoncé
    a.set_ptt(False)
    a.set_muted(True)
    for _ in range(10):
        aa.tick(loud)
    s = a.snapshot()
    assert a._sent == 10 and s.muted and s.talking_while_muted and not s.transmitting
    assert wait_for(lambda: b.snapshot().peers[0].remote_muted)
    a.set_deafened(True)
    assert wait_for(lambda: b.snapshot().peers[0].remote_deafened)
    a.set_muted(False)
    a.set_deafened(False)
    assert wait_for(lambda: not b.snapshot().peers[0].remote_muted and not b.snapshot().peers[0].remote_deafened)


def test_qui_parle_hysteresis(rig):
    a, aa = rig("Alice")
    assert not a.snapshot().speaking
    for k in range(10):
        aa.tick(sine(k, amp=6000))
    assert a.snapshot().speaking and a.snapshot().mic_level > 0.5
    for _ in range(10):  # 50 ms de silence : l'indicateur ne clignote pas
        aa.tick(np.zeros(FRAME, np.int16))
    assert a.snapshot().speaking
    for _ in range(60):
        aa.tick(np.zeros(FRAME, np.int16))
    assert not a.snapshot().speaking


def test_pertes_et_reordonnancement(rig):
    b, ba = rig("Bob")
    rp = RawPeer(b.snapshot().port, auto_pong=True)
    rp.beacon("Raw")
    assert wait_for(lambda: len(b.snapshot().peers) == 1)
    outs = []
    for k in range(120):
        pkt = sine(k, amp=7000)
        if k in (30, 31, 70):  # trois paquets perdus
            rp.seq += 1
            rp.sent += 1
        elif k == 50:  # 51 arrive avant 50
            rp.audio(sine(k + 1, amp=7000), seq=51, sent=51 - 3)
            rp.audio(pkt, seq=50, sent=50 - 3)
            rp.seq, rp.sent = 52, 52 - 3
        else:
            rp.audio(pkt)
        time.sleep(0.006)
        outs.append(ba.tick())
    assert b.stats()["callback_errors"] == 0
    pv = b.snapshot().peers[0]
    assert pv.loss_pct > 0 and pv.buffer_ms <= eng.MAX_BUFFER * 5
    assert np.max(np.abs(np.concatenate(outs))) <= 32767
    rp.close()


def test_au_revoir_et_timeout(rig, monkeypatch):
    pb = free_port()
    a, _ = rig("Alice", peers=[f"127.0.0.1:{pb}"])
    b, _ = rig("Bob", port=pb)
    assert wait_for(lambda: len(a.snapshot().peers) == 1 and len(b.snapshot().peers) == 1)
    events_of(a)
    b.shutdown()  # le message « au revoir » retire Bob tout de suite (bien avant 5 s)
    assert wait_for(lambda: len(a.snapshot().peers) == 0, timeout=1.0)
    assert any(ev.kind == "peer_left" and ev.peer_name == "Bob" for ev in events_of(a))
    assert a.snapshot().seconds_alone >= 0
    # timeout : un pair qui disparaît sans prévenir
    monkeypatch.setattr(eng, "TIMEOUT", 1.5)
    rp = RawPeer(a.snapshot().port)
    rp.beacon("Fantome")
    assert wait_for(lambda: len(a.snapshot().peers) == 1)
    assert wait_for(lambda: len(a.snapshot().peers) == 0, timeout=5.0)
    rp.close()


def test_hears_me_faux_si_pas_de_pong(rig):
    a, _ = rig("Alice")
    rp = RawPeer(a.snapshot().port, auto_pong=False)  # reçoit nos pings mais ne répond jamais
    rp.beacon()
    assert wait_for(lambda: len(a.snapshot().peers) == 1)
    assert a.snapshot().peers[0].hears_me  # délai de grâce
    assert wait_for(lambda: not a.snapshot().peers[0].hears_me, timeout=6.0)
    assert a.snapshot().peers[0].quality == "bad"
    rp.close()


def test_paquets_malformes(rig):
    a, aa = rig("Alice")
    rp = RawPeer(a.snapshot().port, auto_pong=True)
    rng = random.Random(1)
    junk = [b"", b"x", b"VL", b"\x00" * 15, b"\xff" * 2000, b"hello world, not a packet at all!!"]
    for d in junk:
        rp.sock.sendto(d, rp.target)
    rp.send(b"B", b"\x00" + "é".encode() * 3000)               # balise géante
    rp.send(b"A", b"\x00" * 10)                                 # audio de mauvaise taille
    rp.send(b"Z", b"inconnu")
    rp.send(b"P", b"court")
    rp.send(b"Q", b"court")
    rp.send(b"B", b"\x00A", ver=1)                              # version incompatible
    rp.send(b"B", b"\x00A", magic=b"XX")
    for _ in range(300):
        rp.sock.sendto(bytes(rng.randrange(256) for _ in range(rng.randrange(1, 600))), rp.target)
    time.sleep(0.3)
    assert len(a.snapshot().peers) == 0
    assert any(ev.kind == "network_warning" for ev in events_of(a))
    # le moteur vit toujours : un vrai pair est découvert ensuite
    rp.beacon("Vrai")
    assert wait_for(lambda: len(a.snapshot().peers) == 1)
    assert all(t.is_alive() for t in a._threads)
    rp.beacon("é" * 40)  # nom trop long : tronqué en caractères
    assert wait_for(lambda: a.snapshot().peers[0].name == "é" * 32)
    rp.close()


def test_balise_contenu_et_hors_ligne(rig):
    a, _ = rig("Zoé", online=False)
    rp = RawPeer(a.snapshot().port)
    time.sleep(1.4)
    assert not [k for k, _ in rp.recv_all(0.1) if k == b"B"]  # rien n'est annoncé avant go_online
    rp.beacon()
    time.sleep(0.2)
    assert len(a.snapshot().peers) == 0 and not a.snapshot().online
    a.add_peer_ip(f"127.0.0.1:{rp.sock.getsockname()[1]}")
    a.go_online()
    got = rp.recv_all(1.0)
    beacons = [d for k, d in got if k == b"B"]
    assert beacons
    magic, ver, kind, room, sid, _ = HEADER.unpack_from(beacons[0])
    assert (magic, ver, room) == (MAGIC, PROTO, bytes(4)) and sid.hex() == a.settings.install_id
    assert beacons[0][HS + 1:].decode() == "Zoé"
    rp.close()


def test_port_deja_pris(tmp_path):
    p = free_port()
    blocker = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    blocker.bind(("127.0.0.1", p))
    try:
        e = Engine(mk_settings(tmp_path, "X", p), audio=FakeAudio(), bind_host="127.0.0.1", broadcast=False)
        with pytest.raises(EngineError) as ei:
            e.start()
        low = ei.value.message_fr.lower()
        assert ei.value.code == "PORT_USED" and "déjà ouvert" in low and "exclusif" not in low and " port" not in low
    finally:
        blocker.close()


def test_deux_moteurs_meme_port_exclusif(rig):
    a, _ = rig("Alice")
    with pytest.raises(EngineError) as ei:
        rig("Bob", port=a.snapshot().port)
    assert ei.value.code == "PORT_USED"


def test_exception_dans_le_callback(rig):
    a, aa = rig("Alice")
    rp = RawPeer(a.snapshot().port, auto_pong=True)
    rp.beacon()
    assert wait_for(lambda: len(a.snapshot().peers) == 1)
    peer = next(iter(a._peers.values()))
    peer.q.extend([np.zeros(7, np.int16)] * 5)  # trames de mauvaise longueur : le mixage lèvera une exception
    peer.playing = True
    out = aa.tick(sine(0))
    assert not out.any() and a.stats()["callback_errors"] >= 1
    peer.q.clear()
    assert aa.tick(sine(1)).shape == (FRAME,)  # le flux continue
    # le pilote donne un nombre de trames inattendu
    outdata = np.ones((100, 1), np.int16)
    a._callback(np.zeros((100, 1), np.int16), outdata, 100, None, None)
    assert not outdata.any()
    rp.close()


def test_limiteur_doux_et_volume_max(rig):
    a, aa = rig("Alice")
    rp = RawPeer(a.snapshot().port, auto_pong=True)
    rp.beacon("Fort")
    assert wait_for(lambda: len(a.snapshot().peers) == 1)
    a.set_peer_volume(a.snapshot().peers[0].id, 5.0)  # borné à 2.0
    assert a.snapshot().peers[0].volume == 2.0
    a.set_master(1.0)
    outs = []
    for k in range(80):
        rp.audio(sine(k, amp=20000))  # x2 volume : 40000 crête avant limiteur
        time.sleep(0.006)
        outs.append(aa.tick())
    out = np.concatenate(outs[40:]).astype(np.int64)
    assert np.max(np.abs(out)) <= 32767
    assert np.max(np.abs(out)) > 30000            # fort mais...
    assert np.mean(np.abs(out) >= 32767) < 0.01   # ...pas écrêté à plat
    # un signal faible n'est pas touché par le limiteur
    a.set_master(1.0)
    a.set_peer_volume(a.snapshot().peers[0].id, 1.0)
    outs = []
    for k in range(80, 140):
        rp.audio(sine(k, amp=5000))
        time.sleep(0.006)
        outs.append(aa.tick())
    assert 4900 <= np.max(np.abs(np.concatenate(outs[30:]))) <= 5000
    rp.close()


def test_sons_et_monitor(rig):
    a, aa = rig("Alice")
    assert not aa.tick().any()
    a.play_cue("join")
    out = np.concatenate([aa.tick() for _ in range(40)])
    assert np.abs(out).max() > 2000
    a.set_deafened(True)
    a.play_cue("mute")
    assert np.abs(np.concatenate([aa.tick() for _ in range(40)])).max() > 1000  # audible même en sourd
    a.set_deafened(False)
    a.play_cue("inconnu")  # ignoré
    # monitor : on s'entend, puis arrêt automatique
    loud = sine(0, amp=8000)
    a.set_monitor(True, timeout_s=0.8)
    assert max(np.abs(aa.tick(loud)).max() for _ in range(4)) > 7000     # (l'envoi vient de rouvrir : fondu de 3 trames)
    assert wait_for(lambda: not a.snapshot().monitor, timeout=3.0)
    assert not aa.tick(loud).any()


def test_surveillance_audio_reconnexion(rig):
    class Mortel(FakeAudio):
        realtime = True
        mort = False

        def healthy(self):
            return not self.mort and self.callback is not None

        def open(self, *a):
            self.mort = False
            return super().open(*a)

    audio = Mortel()
    e, _ = rig("Alice", audio=audio)
    events_of(e)
    audio.mort = True  # casque débranché
    assert wait_for(lambda: audio.opened >= 2, timeout=5)
    evs = [x.kind for x in events_of(e)]
    assert "audio_error" in evs
    assert wait_for(lambda: e.snapshot().audio.ok)
    # plus de périphérique du tout : erreur claire, puis retour quand il revient
    audio.fail_open = True
    audio.mort = True
    assert wait_for(lambda: not e.snapshot().audio.ok, timeout=5)
    assert "micro" in e.snapshot().audio.error_fr.lower()
    audio.fail_open = False
    assert wait_for(lambda: e.snapshot().audio.ok, timeout=8)
    assert any(x.kind == "audio_recovered" for x in events_of(e))


def test_set_devices_et_refresh(rig):
    audio = FakeAudio(ins=["Micro A", "Micro B"], outs=["Sortie A"])
    e, _ = rig("Alice", audio=audio)
    assert e.list_devices() == (["Micro A", "Micro B"], ["Sortie A"])
    info = e.set_devices("Micro B", None)
    assert info.ok and info.input_name == "Micro B" and e.settings.dev_in == "Micro B" and e.settings.dirty
    audio.fail_open = True
    info = e.set_devices("Micro A", None)  # ne lève pas
    assert not info.ok and info.error_fr
    audio.fail_open = False
    events_of(e)
    e.refresh_devices()
    assert wait_for(lambda: any(x.kind == "devices_changed" for x in events_of(e)))


def test_mesures_du_callback_latence(rig):
    """Durée d'un callback avec 3 pairs qui parlent et 3 destinataires (le budget est 5 ms)."""
    a, aa = rig("Alice")
    raws = [RawPeer(a.snapshot().port, sid=bytes([1, 1, 1, i]), auto_pong=True) for i in range(1, 4)]
    socks = []
    for r in raws:
        r.beacon(f"P{r.sid[3]}")
        s = r.sock
        socks.append(s)
    assert wait_for(lambda: len(a.snapshot().peers) == 3)
    durations = []
    for k in range(3000):
        for r in raws:
            r.audio(sine(k, 200 + 100 * r.sid[3], 6000))
        if k % 4 == 0:
            time.sleep(0.001)
        mic = sine(k)
        indata = mic.reshape(-1, 1)
        outdata = np.zeros((FRAME, 1), np.int16)
        t0 = time.perf_counter()
        a._callback(indata, outdata, FRAME, None, None)
        durations.append(time.perf_counter() - t0)
    d = np.array(durations[500:]) * 1000
    print(f"\ncallback (3 pairs, 3 destinataires): moyenne {d.mean():.3f} ms, p99 {np.percentile(d, 99):.3f} ms, "
          f"max {d.max():.3f} ms, budget 5 ms")
    assert a.stats()["callback_errors"] == 0
    assert d.mean() < 1.0
    for r in raws:
        r.close()


# ---------------------------------------------------------------- réglages, pairs manuels
def test_pairs_manuels(rig):
    a, _ = rig("Alice")
    with pytest.raises(EngineError) as ei:
        a.add_peer_ip("pas une adresse !!")
    assert ei.value.code == "BAD_ADDRESS" and ei.value.message_fr
    for bad in ("", "0.0.0.0", "255.255.255.255", "1.2.3.4:99999", "1.2.3.4:abc", "224.0.0.1"):
        with pytest.raises(EngineError):
            a.add_peer_ip(bad)
    with pytest.raises(EngineError):
        a.add_peer_ip(f"127.0.0.1:{a.snapshot().port}")  # soi-même
    a.add_peer_ip("192.168.1.25")
    a.add_peer_ip("192.168.1.26:50600")
    a.add_peer_ip("192.168.1.25")
    assert a.settings.manual_peers == ["192.168.1.25", "192.168.1.26:50600"]
    assert a.settings.dirty
    a.remove_peer_ip("192.168.1.25")
    assert a.settings.manual_peers == ["192.168.1.26:50600"]


def test_reglages_bornes_et_prefs(rig):
    a, _ = rig("Alice")
    a.set_mic_gain(9)
    a.set_master(-3)
    a.set_gate_db(-5)
    assert (a.settings.mic_gain, a.settings.master, a.settings.gate_db) == (2.0, 0.0, -20.0)
    a.set_gate_db(-200)
    assert a.snapshot().gate_db == -70.0 and a.snapshot().gate_level == 0.0
    a.set_buffer_mode("stable")
    a.set_buffer_mode("n'importe quoi")
    assert a.settings.buffer_mode == "stable" and a.snapshot().buffer_mode == "stable"
    a.set_name("  Zoé\x00\n  ")
    assert a.settings.name == "Zoé"
    rp = RawPeer(a.snapshot().port, sid=bytes.fromhex("deadbeef"), auto_pong=True)
    rp.beacon("Copain")
    assert wait_for(lambda: len(a.snapshot().peers) == 1)
    pid = a.snapshot().peers[0].id
    assert pid == "deadbeef"
    a.set_peer_volume(pid, 1.5)
    a.set_peer_muted(pid, True)
    assert a.settings.peer_prefs[pid] == {"volume": 1.5, "muted": True, "clean": False, "name": "Copain"}
    # il part puis revient (même pseudo ou autre) : volume et mute mémorisés par identité
    rp.send(b"X")
    assert wait_for(lambda: len(a.snapshot().peers) == 0)
    rp.beacon("Autre pseudo")
    assert wait_for(lambda: len(a.snapshot().peers) == 1)
    pv = a.snapshot().peers[0]
    assert pv.volume == 1.5 and pv.muted
    rp.close()


# ---------------------------------------------------------------- settings
def test_settings_aller_retour_et_atomique(tmp_path):
    s = Settings.load(directory=tmp_path, legacy=tmp_path / "rien.json")
    assert len(s.install_id) == 8 and (tmp_path / "settings.json").exists()
    s.name, s.ptt_key, s.ui.sounds = "Zoé", "mouse4", False
    s.dirty = True
    s.save_if_dirty()
    assert not s.dirty and not list(tmp_path.glob("*.tmp"))
    t = Settings.load(directory=tmp_path, legacy=tmp_path / "rien.json")
    assert (t.name, t.ptt_key, t.ui.sounds, t.install_id) == ("Zoé", "mouse4", False, s.install_id)
    assert t.warnings == []


@pytest.mark.parametrize("contenu", [b"{pas du json", b'{"name": "Zo', b"", b"[1,2,3]", b"\xff\xfe\x00garbage"])
def test_settings_corrompus(tmp_path, contenu):
    (tmp_path / "settings.json").write_bytes(contenu)
    s = Settings.load(directory=tmp_path, legacy=tmp_path / "rien.json")
    assert s.name == "" and s.mic_gain == 1.0 and len(s.install_id) == 8
    assert s.warnings and (tmp_path / "settings.json.bak").exists()
    json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))  # un fichier sain a été réécrit
    e = Engine(s, audio=FakeAudio(), bind_host="127.0.0.1", broadcast=False)
    s.port = free_port()
    e._port = s.port
    e.start()
    try:
        assert any(ev.kind == "settings_warning" for ev in events_of(e))
    finally:
        e.shutdown()


def test_settings_valeurs_invalides(tmp_path):
    bad = {"name": 12, "mic_gain": "fort", "master": 99, "gate_db": -300, "buffer_mode": "x", "port": 3,
           "install_id": "zz", "manual_peers": "pas une liste", "ptt_key": 5, "inconnu": True,
           "peer_prefs": {"a": {"volume": 50, "muted": "oui"}, "b": 3}, "ui": {"sounds": "non", "geometry": 4,
                                                                                "show_advanced": True}}
    (tmp_path / "settings.json").write_text(json.dumps(bad), encoding="utf-8")
    s = Settings.load(directory=tmp_path, legacy=tmp_path / "rien.json")
    assert s.name == "" and s.mic_gain == 1.0 and s.master == 2.0 and s.gate_db == -70.0
    assert s.buffer_mode == "auto" and s.port == 50555 and len(s.install_id) == 8
    assert s.manual_peers == [] and s.ptt_key is None
    assert s.peer_prefs == {"a": {"volume": 2.0, "muted": False, "clean": False, "name": ""}}
    assert s.ui.sounds is True and s.ui.geometry is None and s.ui.show_advanced is True


def test_migration_ancien_fichier(rig, tmp_path):
    old = {"name": "CQCQBOUFIN", "mic_gain": 2.5, "master": 1.0, "gate_db": -60.0,
           "peer_volumes": {"Gwanda": 2.0}, "dev_in": "Par défaut", "dev_out": "Haut-parleurs (Realtek)"}
    legacy = tmp_path / "vocal_config.json"
    legacy.write_text(json.dumps(old), encoding="utf-8")
    d = tmp_path / "data"
    s = Settings.load(directory=d, legacy=legacy)
    assert (s.name, s.mic_gain, s.gate_db, s.dev_in, s.dev_out) == ("CQCQBOUFIN", 2.0, -60.0, None,
                                                                    "Haut-parleurs (Realtek)")
    assert (d / "settings.json").exists() and legacy.exists()  # l'ancien fichier n'est pas supprimé
    assert Settings.load(directory=d, legacy=legacy).name == "CQCQBOUFIN"
    # le volume par pseudo est repris quand « Gwanda » arrive
    s.port = free_port()
    e = Engine(s, audio=FakeAudio(), bind_host="127.0.0.1", broadcast=False)
    e.start()
    try:
        rp = RawPeer(s.port, sid=bytes.fromhex("11223344"), auto_pong=True)
        rp.beacon("Gwanda")
        assert wait_for(lambda: len(e.snapshot().peers) == 1)
        assert e.snapshot().peers[0].volume == 2.0
        assert s.peer_prefs["11223344"]["volume"] == 2.0
        rp.close()
    finally:
        e.shutdown()


def test_resource_path_et_app_dir(tmp_path, monkeypatch):
    import settings as st
    monkeypatch.setenv("VOCAL_LAN_DATA", str(tmp_path / "x"))
    assert st.app_dir() == tmp_path / "x" and (tmp_path / "x").is_dir()
    assert st.resource_path("assets").parent == st.resource_path("settings.py").parent
    assert st.VERSION == "2.1.0"


# ---------------------------------------------------------------- réseau, pare-feu, raccourcis
def test_parse_address_et_pare_feu():
    assert netutil.parse_address("192.168.1.5", 50555) == ("192.168.1.5", 50555)
    assert netutil.parse_address(" 10.0.0.2:50600 ", 50555) == ("10.0.0.2", 50600)
    with pytest.raises(ValueError):
        netutil.parse_address("a.b.c.d.invalide.", 50555)
    cmd = netutil.build_firewall_command(r"C:\Program Files\Vocal LAN\Vocal LAN.exe", 50555)
    assert 'name="Vocal LAN"' in cmd and "protocol=UDP" in cmd and "localport=50555" in cmd
    assert "dir=in" in cmd and "action=allow" in cmd and "profile=private,domain" in cmd
    assert 'program="C:\\Program Files\\Vocal LAN\\Vocal LAN.exe"' in cmd and cmd.startswith("/c netsh")
    assert "public" in netutil.build_firewall_command("x.exe", 1234, "private,domain,public")
    with pytest.raises(ValueError):
        netutil.build_firewall_command('a&b".exe', 50555)
    assert isinstance(netutil.get_interfaces(), list)


def test_repair_firewall_ne_lance_pas_uac(rig, monkeypatch):
    called = {}
    monkeypatch.setattr(netutil, "add_firewall_rule", lambda port: (called.setdefault("p", port), ("ok", "fait"))[1])
    a, _ = rig("Alice")
    a.repair_firewall()
    assert wait_for(lambda: any(ev.kind == "firewall_result" and ev.code == "ok" for ev in events_of(a)))
    assert called["p"] == a.snapshot().port


def test_hotkeys_ptt_mute_capture(rig):
    a, aa = rig("Alice", ptt_mode=True, ptt_key="mouse4", mute_key="f9", deafen_key="ctrl+f10")
    down = set()
    hk = Hotkeys(a, a.settings, key_state=lambda vk: vk in down)
    hk.start()
    try:
        down.add(0x05)  # souris 4
        assert wait_for(lambda: a.snapshot().ptt_active, timeout=1)
        down.discard(0x05)
        assert wait_for(lambda: not a.snapshot().ptt_active, timeout=1)
        down.add(0x78)  # F9 : bascule à l'appui
        assert wait_for(lambda: a.muted, timeout=1)
        down.discard(0x78)
        time.sleep(0.05)
        down.add(0x78)
        assert wait_for(lambda: not a.muted, timeout=1)
        down.discard(0x78)
        down.add(0x79)  # F10 seul : pas de combinaison Ctrl
        time.sleep(0.05)
        assert not a.deafened
        down.add(0xA2)  # Ctrl gauche + F10
        assert wait_for(lambda: a.deafened, timeout=1)
        down.clear()
        # capture de la prochaine touche
        hk.capture_next()
        time.sleep(0.05)
        down.add(0x77)  # F8
        assert wait_for(lambda: any(ev.kind == "key_captured" and ev.code == "f8" for ev in events_of(a)), timeout=1)
        down.clear()
        hk.capture_next()
        time.sleep(0.05)
        down.add(0x1B)  # Échap annule
        assert wait_for(lambda: any(ev.kind == "key_captured" and ev.code is None for ev in events_of(a)), timeout=1)
        down.clear()
        # configuration changée : plus de PTT
        down.add(0x05)
        assert wait_for(lambda: a.snapshot().ptt_active, timeout=1)
        a.settings.ptt_key = None
        hk.apply()
        assert wait_for(lambda: not a.snapshot().ptt_active, timeout=1)
    finally:
        hk.stop()


def test_key_label():
    assert key_label("f9") == "F9" and key_label("mouse4") == "Souris 4" and key_label("ctrl_right") == "Ctrl droit"
    assert key_label("ctrl+m") == "Ctrl + M" and key_label(None) == "Aucune" and key_label("num5") == "Pavé 5"


def test_arret_idempotent(rig):
    a, _ = rig("Alice")
    a.shutdown()
    a.shutdown()
    assert not a.snapshot().online


# ---------------------------------------------------------------- revue croisée : régressions
def test_pare_feu_purge_les_regles_bloquer_de_notre_exe():
    exe = r"C:\Apps\Vocal LAN\Vocal LAN.exe"
    assert "delete rule name=all" in netutil.build_firewall_command(exe, 50555, purge_program=True)
    # avec python.exe on ne touche qu'à notre propre règle
    assert 'delete rule name="Vocal LAN"' in netutil.build_firewall_command(exe, 50555)


def test_capture_de_touche_expire_et_rend_les_raccourcis(rig):
    a, _ = rig("Alice")
    hk = Hotkeys(a, a.settings, key_state=lambda vk: False)
    hk.capture_next()
    assert hk._capturing
    hk._cap_deadline = time.monotonic() - 1
    hk._capture_step()
    assert not hk._capturing and not [e for e in events_of(a) if e.kind == "key_captured"]


def test_reglages_volume_nan_et_sauvegarde_concurrente(tmp_path):
    (tmp_path / "settings.json").write_text(
        '{"peer_prefs": {"aa": {"volume": NaN, "muted": true, "name": "x"}}}', encoding="utf-8")
    s = Settings.load(tmp_path)
    assert s.peer_prefs["aa"]["volume"] == 1.0 and s.peer_prefs["aa"]["muted"] is True
    real = s.to_dict
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("dictionary changed size during iteration")
        return real()

    s.to_dict = flaky
    s.save()
    assert not s.dirty and json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))["peer_prefs"]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows seulement")
def test_instance_unique_et_activation(monkeypatch):
    monkeypatch.setenv("VOCAL_LAN_INSTANCE", "_pytest%d" % os.getpid())
    saved = (netutil._mutex, netutil._pid_map, netutil._event, netutil._listener)
    try:
        netutil._listener = None
        netutil._listener_stop.clear()
        assert netutil.acquire_single_instance() is True
        assert netutil.acquire_single_instance() is False       # même nom : déjà pris (même processus)
        assert netutil._primary_pid() == os.getpid()
        got = []
        netutil.start_activation_listener(lambda: got.append(1))
        assert netutil.signal_existing() is True
        assert wait_for(lambda: got, timeout=3)
        assert netutil.find_main_windows("Vocal LAN introuvable ici") == []
    finally:
        netutil.stop_activation_listener()
        if netutil._listener is not None:
            netutil._listener.join(timeout=2)
        netutil._listener_stop.clear()
        netutil._mutex, netutil._pid_map, netutil._event, netutil._listener = saved


# ================================================================ ronde de correctifs (phase 4b)
def test_hors_ligne_n_ouvre_aucun_socket_et_l_audio_marche(tmp_path):
    """A-I2 : pas de bind avant go_online (sinon le pare-feu Windows s'affiche avant l'assistant)."""
    p = free_port()
    blocker = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    blocker.bind(("127.0.0.1", p))  # le port est pris : un bind le trahirait
    audio = FakeAudio()
    e = Engine(mk_settings(tmp_path, "Hors", p), audio=audio, bind_host="127.0.0.1", broadcast=False)
    try:
        e.start(online=False)  # ne lève pas : aucun bind
        assert e._sock is None and not [t for t in e._threads if t.name == "vocal-recv"]
        assert e.snapshot().audio.ok and not e.snapshot().online
        e.set_monitor(True)
        for k in range(40):  # l'audio tourne (vumètre, « Je m'entends »)
            out = audio.tick(sine(k))
        assert np.abs(out).max() > 1000 and e.snapshot().mic_level > 0.3
        with pytest.raises(EngineError) as ei:
            e.go_online()  # c'est ici que le port est demandé
        assert ei.value.code == "PORT_USED" and not e.snapshot().online
        blocker.close()
        e.go_online()  # réessai possible une fois le port libéré
        assert e.snapshot().online and e._sock is not None
        assert any(t.name == "vocal-recv" for t in e._threads)
    finally:
        blocker.close()
        e.shutdown()


def test_sourdine_coupe_le_micro_comme_discord(rig):
    """Sourdine -> micro coupé ; fin de sourdine -> micro comme avant ; micro rétabli -> fin de sourdine."""
    e, _ = rig("Alice")
    e.set_deafened(True)
    s = e.snapshot()
    assert s.deafened and s.muted
    e.set_deafened(False)
    s = e.snapshot()
    assert not s.deafened and not s.muted  # le micro était ouvert avant
    e.set_muted(True)
    e.set_deafened(True)
    e.set_deafened(False)
    s = e.snapshot()
    assert not s.deafened and s.muted  # le micro était déjà coupé : il le reste
    e.set_muted(False)
    e.set_deafened(True)
    e.set_muted(False)  # rétablir le micro pendant la sourdine lève la sourdine
    s = e.snapshot()
    assert not s.deafened and not s.muted
    e.set_deafened(True)
    e.set_deafened(True)  # idempotent : ne mémorise pas « muet » par erreur
    e.set_deafened(False)
    assert not e.snapshot().muted
    e.set_deafened(True)  # les autres sont avertis (balise : drapeaux muet=1, sourd=2)
    assert e._beacon()[HS] == 3


def test_sourdine_tait_les_pairs_mais_pas_je_m_entends(rig):
    a, aa = rig("Alice")
    rp = RawPeer(a.snapshot().port, auto_pong=True)
    rp.beacon("Bob")
    assert wait_for(lambda: len(a.snapshot().peers) == 1)
    for k in range(40):
        rp.audio(sine(k, 500, 9000))
    time.sleep(0.1)
    a.set_deafened(True)
    outs = [aa.tick(None) for _ in range(30)]
    assert max(int(np.abs(o).max()) for o in outs) == 0
    a.set_monitor(True, processed=False)                  # « Sans filtre » : le micro brut reste audible
    out = aa.tick(sine(0, 700, 9000))
    assert np.abs(out).max() > 3000
    a.set_monitor_processed(True)                         # « Avec filtre » : ce que les autres entendent, donc rien (micro coupé)
    outs = [aa.tick(sine(k, 700, 9000)) for k in range(12)]
    assert int(np.abs(outs[-1]).max()) == 0
    rp.close()


def test_set_devices_echec_restaure_l_ancien(rig):
    """A-I4 : échec -> anciens périphériques, ancien flux rouvert, mauvais nom jamais gardé."""
    class Capricieux(FakeAudio):
        def open(self, dev_in, dev_out, callback, frame):
            if dev_in == "Casque Bluetooth":
                self.opened += 1
                self.callback = None
                return eng.AudioInfo(False, None, None, None, False, "Impossible d'ouvrir ce périphérique.")
            return super().open(dev_in, dev_out, callback, frame)

    audio = Capricieux(ins=["Micro A", "Micro B"], outs=["Sortie A"])
    e, _ = rig("Alice", audio=audio)
    assert e.set_devices("Micro B", None).ok
    e.settings.dirty = False
    events_of(e)
    info = e.set_devices("Casque Bluetooth", "Sortie A")
    assert not info.ok and info.error_fr
    assert (e.settings.dev_in, e.settings.dev_out) == ("Micro B", None)
    assert not e.settings.dirty
    s = e.snapshot()
    assert s.audio.ok and s.audio.input_name == "Micro B" and audio.callback is not None
    audio.tick(None)  # l'ancien flux tourne vraiment


def test_list_devices_ne_bloque_jamais():
    """A-I5 : verrou d'ouverture tenu par un autre thread -> la liste en cache revient aussitôt."""
    import threading

    import audiodev
    b = audiodev.AudioBackend()
    b._last_lists = (["Micro cache"], ["Sortie cache"])
    held, release = threading.Event(), threading.Event()

    def hold():
        with b._lock:
            held.set()
            release.wait(5)

    t = threading.Thread(target=hold, daemon=True)
    t.start()
    assert held.wait(2)
    t0 = time.perf_counter()
    assert b.list_devices() == (["Micro cache"], ["Sortie cache"])
    assert time.perf_counter() - t0 < 0.1
    release.set()
    t.join(2)


def test_parle_et_niveau_retombent_si_le_flux_est_mort(rig):
    """A-I6 : plus de callback depuis > 0,5 s (ou flux fermé) -> speaking False, niveaux 0."""
    a, aa = rig("Alice")
    rp = RawPeer(a.snapshot().port, auto_pong=True)
    rp.beacon("Bob")
    assert wait_for(lambda: len(a.snapshot().peers) == 1)
    for k in range(120):
        rp.audio(sine(k, 600, 9000))
        if k % 8 == 0:
            time.sleep(0.002)
        aa.tick(sine(k, 400, 12000))
    s = a.snapshot()
    assert s.speaking and s.mic_level > 0.3 and s.peers[0].speaking and s.peers[0].level > 0.3
    time.sleep(0.8)  # le flux ne tourne plus : aucun callback
    s = a.snapshot()
    assert not s.speaking and s.mic_level == 0 and not s.transmitting
    assert not s.peers[0].speaking and s.peers[0].level == 0
    for k in range(60):  # le flux repart
        aa.tick(sine(k, 400, 12000))
    assert a.snapshot().speaking
    a._audio_info = eng.AudioInfo(False, None, None, None, False, "mort")  # flux fermé : même chose
    s = a.snapshot()
    assert not s.speaking and s.mic_level == 0
    rp.close()


def test_changer_de_peripherique_ne_declenche_pas_de_fausse_alerte(rig):
    """M6 : le watchdog (qui voit le flux fermé pendant la bascule) ne doit rien dire."""
    class Lent(FakeAudio):
        realtime = True

        def open(self, *a):
            time.sleep(0.15)  # fenêtre pendant laquelle le flux est fermé
            return super().open(*a)

        def healthy(self):
            return self.callback is not None

    audio = Lent(ins=["A", "B"], outs=["S"])
    e, _ = rig("Alice", audio=audio)
    events_of(e)
    for k in range(6):
        e.set_devices("B" if k % 2 == 0 else "A", None)
        time.sleep(0.3)
    kinds = [x.kind for x in events_of(e)]
    assert "audio_error" not in kinds and "audio_recovered" not in kinds


def test_watchdog_ne_rouvre_jamais_apres_shutdown(rig):
    """A-M8."""
    class Mortel(FakeAudio):
        realtime = True

        def healthy(self):
            return False

    audio = Mortel()
    e, _ = rig("Alice", audio=audio)
    assert wait_for(lambda: audio.opened >= 2, timeout=6)
    e.shutdown()
    n = audio.opened
    e._next_audio_try = 0.0
    e._watchdog(time.monotonic() + 100)
    e._open_audio()  # même une ouverture tardive se referme
    time.sleep(0.6)
    assert audio.opened == n + 1 and audio.callback is None


def test_port_de_session_n_est_pas_persiste(tmp_path, monkeypatch):
    """M5 / A-M2 : --port vaut pour la session, settings.port et settings.json restent intacts."""
    import types

    import hotkeys as hk_mod
    import netutil as nu
    import vocal
    data = tmp_path / "data"
    monkeypatch.setenv("VOCAL_LAN_DATA", str(data))
    monkeypatch.setattr(vocal, "setup_logging", lambda: None)
    monkeypatch.setattr(nu, "boost_process", lambda: None)
    monkeypatch.setattr(nu, "acquire_single_instance", lambda: True)
    monkeypatch.setattr(nu, "start_activation_listener", lambda cb: None)
    monkeypatch.setattr(nu, "stop_activation_listener", lambda: None)
    seen = {}
    port = free_port()

    class Dummy:
        def __init__(self, *a, **k):
            pass

        def start(self):
            pass

        def stop(self):
            pass
    monkeypatch.setattr(hk_mod, "Hotkeys", Dummy)
    real = eng.Engine
    monkeypatch.setattr(eng, "Engine", lambda s, port=None: real(s, audio=FakeAudio(), bind_host="127.0.0.1",
                                                                 broadcast=False, port=port))

    def fake_run(engine, settings, hotkeys):
        seen["port"] = engine.snapshot().port
        seen["settings_port"] = settings.port
    monkeypatch.setitem(sys.modules, "ui", types.SimpleNamespace(run=fake_run))
    assert vocal.main(["--port", str(port), "--option-inconnue"]) == 0
    assert seen == {"port": port, "settings_port": 50555}
    assert json.loads((data / "settings.json").read_text(encoding="utf-8"))["port"] == 50555


def test_arguments_invalides_ne_plantent_pas_sans_console(monkeypatch):
    """A-M10 : sys.stderr vaut None dans l'exe sans console."""
    import vocal
    monkeypatch.setattr(sys, "stderr", None)
    args, notes = vocal.parse_args(["--port", "abc", "--zzz"])
    assert args.port is None and notes
    args, notes = vocal.parse_args(["--inconnue", "10.0.0.5", "--minimized"])
    assert args.minimized and notes
    vocal.parse_args(["-h"])  # ni aide imprimée ni sortie


def test_seconds_alone_repart_quand_le_reseau_revient(rig):
    """A-M1."""
    e, _ = rig("Alice")
    e._alone_since = time.monotonic() - 300
    e._no_net, e._broadcast = True, True
    e._ifaces = [("x", "10.0.0.2", "10.0.0.255")]  # le réseau est revenu
    e._every_second(time.monotonic(), 1)
    e._broadcast = False
    assert e.snapshot().seconds_alone < 5 and not e._no_net


def test_table_des_pairs_limitee(rig):
    """M1 : au-delà de MAX_PEERS, les nouveaux sont ignorés (et on prévient une fois)."""
    a, _ = rig("Alice")
    events_of(a)
    raws = []
    for i in range(eng.MAX_PEERS + 8):
        r = RawPeer(a.snapshot().port, sid=bytes([9, 9, i // 256, i % 256]))
        r.beacon(f"P{i}")
        raws.append(r)
    assert wait_for(lambda: len(a.snapshot().peers) == eng.MAX_PEERS, timeout=5)
    time.sleep(0.3)
    assert len(a.snapshot().peers) == eng.MAX_PEERS and len(a._targets) == eng.MAX_PEERS
    assert len(events_of(a, "network_warning")) == 1
    raws[0].beacon("Renomme")  # un pair déjà connu continue de fonctionner
    assert wait_for(lambda: any(p.name == "Renomme" for p in a.snapshot().peers))
    for r in raws:
        r.close()


def test_pas_d_audio_envoye_sans_pair(rig):
    """A-M4 : seul, rien n'est émis et transmitting est faux."""
    a, aa = rig("Alice")
    for k in range(40):
        aa.tick(sine(k))
    s = a.snapshot()
    assert not s.peers and not s.transmitting and a._sent == 0


def test_pseudo_garde_zwj_et_selecteurs_de_variante():
    from settings import _clean_text
    famille = "\U0001F468‍\U0001F469‍\U0001F467"
    assert _clean_text(famille) == famille
    assert _clean_text("❤️ Seb") == "❤️ Seb"
    assert _clean_text("‍Seb‍") == "Seb"
    assert _clean_text("a\x00b\x07") == "ab"
    assert not _clean_text(famille * 20).endswith("‍")


def test_settings_bom_et_json_trop_profond(tmp_path):
    """M3 : BOM UTF-8 toléré ; JSON ultra-imbriqué = corrompu (.bak + avertissement), jamais d'exception."""
    d1 = tmp_path / "bom"
    d1.mkdir()
    (d1 / "settings.json").write_bytes(b"\xef\xbb\xbf" + json.dumps({"name": "Zoé", "master": 1.5}).encode())
    s = Settings.load(directory=d1, legacy=d1 / "rien.json")
    assert s.name == "Zoé" and s.master == 1.5 and not s.warnings
    d2 = tmp_path / "profond"
    d2.mkdir()
    (d2 / "settings.json").write_text("[" * 200000 + "]" * 200000, encoding="utf-8")
    s = Settings.load(directory=d2, legacy=d2 / "rien.json")
    assert s.warnings and (d2 / "settings.json.bak").exists() and s.name == "" and len(s.install_id) == 8


def test_set_devices_introuvable_ne_garde_pas_le_nom(rig):
    """A-I4 (repli silencieux sur le défaut) : le nom introuvable n'est pas mémorisé, une erreur est renvoyée."""
    class Repli(FakeAudio):
        def open(self, dev_in, dev_out, callback, frame):
            info = super().open(dev_in, dev_out, callback, frame)
            self.notice = f"Le micro « {dev_in} » est introuvable : le micro par défaut est utilisé." if dev_in == "Fantôme" else None
            return info

    e, _ = rig("Alice", audio=Repli())
    info = e.set_devices("Fantôme", None)
    assert not info.ok and "introuvable" in info.error_fr and e.settings.dev_in is None
    assert e.snapshot().audio.ok


# ================================================================ Vocal LAN 2.1 : suppression du bruit et traitement de la voix
import tracemalloc  # noqa: E402

import denoise  # noqa: E402
from test_denoise import voice as synth_voice  # noqa: E402

_VOICE_CACHE = []


def vframe(k, amp=1.0):
    """Trame k d'une voix synthétique (3 s en boucle), int16."""
    if not _VOICE_CACHE:
        _VOICE_CACHE.append(synth_voice(3.0, rms=3000.0))
    v = _VOICE_CACHE[0]
    n = len(v) // FRAME
    return (v[(k % n) * FRAME:(k % n + 1) * FRAME] * amp).astype(np.int16)


@pytest.fixture
def denoise_cache():
    denoise.reset_cache()
    yield
    denoise.reset_cache()


def rms(a):
    return float(np.sqrt(np.mean(np.asarray(a, np.float64) ** 2)))


def make_peers(a, n):
    raws = []
    for i in range(1, n + 1):
        r = RawPeer(a.snapshot().port, sid=bytes([7, 7, 7, i]), auto_pong=True)
        r.beacon(f"Ami{i}")
        raws.append(r)
    assert wait_for(lambda: len(a.snapshot().peers) == n)
    return raws


def test_deux_moteurs_21_fort_s_entendent(rig):
    kw = dict(denoise_level="strong", fx_highpass=True, fx_limiter=True)
    pb = free_port()
    a, aa = rig("Alice", peers=[f"127.0.0.1:{pb}"], **kw)
    b, ba = rig("Bob", port=pb, **kw)
    assert wait_for(lambda: len(a.snapshot().peers) == 1 and len(b.snapshot().peers) == 1)
    sa = a.snapshot()
    assert sa.denoise_level == "strong" and sa.denoise_active and sa.denoise_error_fr is None
    assert 24.9 < sa.latency_added_ms < 25.1
    out = []
    for k in range(500):
        aa.tick(vframe(k))
        time.sleep(0.005)
        out.append(ba.tick())
    tail = np.concatenate(out)[-200 * FRAME:]
    assert 1200 < rms(tail) < 4500, rms(tail)          # la voix de Alice arrive chez Bob, quasi intacte
    assert b.snapshot().peers[0].speaking
    s = a.snapshot()
    assert s.mic_level_out > 0.3 and s.transmitting and not events_of(a, "denoise_failed")


def test_moteur_21_et_moteur_configure_comme_la_20_s_entendent(rig):
    """Compatibilité du protocole : même en-tête, même trame ; Bob n'a aucun traitement (tout désactivé)."""
    pb = free_port()
    a, aa = rig("Alice", peers=[f"127.0.0.1:{pb}"], denoise_level="strong", fx_highpass=True, fx_limiter=True)
    b, ba = rig("Bob", port=pb)                        # V20 : tout désactivé
    assert wait_for(lambda: len(a.snapshot().peers) == 1 and len(b.snapshot().peers) == 1)
    rp = RawPeer(pb, sid=bytes([5, 5, 5, 5]), auto_pong=True)   # témoin du format sur le fil
    rp.beacon("Temoin")
    assert wait_for(lambda: len(b.snapshot().peers) == 2)
    out_a, out_b = [], []
    for k in range(500):
        out_a.append(aa.tick(vframe(k)))
        out_b.append(ba.tick(sine(k, 600, 8000)))
        time.sleep(0.005)
    assert 1200 < rms(np.concatenate(out_b)[-200 * FRAME:]) < 4500      # Bob entend la voix de Alice
    assert 4500 < rms(np.concatenate(out_a)[-200 * FRAME:]) < 6200      # Alice entend le son de Bob (8000/sqrt2)
    pkts = [d for k, d in rp.recv_all(0.3) if k == b"A"]
    assert pkts, "Bob envoie de l'audio au témoin"
    assert all(len(d) == AUDIO_SIZE for d in pkts)
    magic, ver, kind, room, sid, seq = HEADER.unpack_from(pkts[0])
    assert (magic, ver, kind) == (MAGIC, PROTO, b"A") and PROTO == 2
    assert a.snapshot().peers[0].hears_me and b.snapshot().peers[0].hears_me
    rp.close()


def test_limite_de_3_amis_nettoyes(rig):
    a, aa = rig("Alice")
    raws = make_peers(a, 4)
    ids = [p.id for p in a.snapshot().peers]
    for pid in ids[:3]:
        a.set_peer_clean(pid, True)
    with pytest.raises(EngineError) as ei:
        a.set_peer_clean(ids[3], True)
    assert ei.value.code == "CLEAN_LIMIT" and "3" in ei.value.message_fr
    assert "RNNoise" not in ei.value.message_fr
    s = a.snapshot()
    assert sum(p.clean_active for p in s.peers) == 3 and sum(p.clean for p in s.peers) == 3
    assert a.settings.peer_prefs.get(ids[3], {}).get("clean", False) is False     # refusé : rien de mémorisé
    assert a.settings.peer_prefs[ids[0]]["clean"] is True and a.settings.dirty
    a.set_peer_clean(ids[0], False)                     # on en libère un : le 4e peut passer
    a.set_peer_clean(ids[3], True)
    assert sum(p.clean_active for p in a.snapshot().peers) == 3
    a.set_peer_clean(ids[3], True)                      # idempotent : pas d'erreur sur un ami déjà nettoyé
    # mémorisé par ami (survit au rechargement des réglages et à un départ / retour)
    a.settings.save()
    s2 = Settings.load(directory=a.settings.path.parent, legacy=a.settings.path.parent / "rien.json")
    assert s2.peer_prefs[ids[1]]["clean"] is True and s2.peer_prefs[ids[0]]["clean"] is False
    raws[1].send(b"X")
    assert wait_for(lambda: len(a.snapshot().peers) == 3)
    raws[1].beacon("Ami2")
    assert wait_for(lambda: len(a.snapshot().peers) == 4)
    assert wait_for(lambda: next(p for p in a.snapshot().peers if p.id == ids[1]).clean_active)
    for r in raws:
        r.close()


def test_nettoyer_sa_voix_supprime_le_bruit_d_un_ami(rig):
    a, aa = rig("Alice")
    (rp,) = make_peers(a, 1)
    pid = a.snapshot().peers[0].id
    noise = (np.random.default_rng(9).standard_normal(FRAME * 700) * 1500).astype(np.int16)

    def level(n0, n1):
        outs = []
        for k in range(n0, n1):
            rp.audio(noise[k * FRAME:(k + 1) * FRAME])
            time.sleep(0.005)
            outs.append(aa.tick())
        return rms(np.concatenate(outs[-100:]))

    sans = level(0, 300)
    a.set_peer_clean(pid, True)
    avec = level(300, 600)
    assert sans > 1000 and avec < sans / 10, (sans, avec)           # plus de 20 dB de moins
    assert a.snapshot().peers[0].clean_active
    rp.close()


def test_tout_desactiver_est_identique_a_l_entree_bit_a_bit(rig):
    a, aa = rig("Alice", denoise_level="strong", voice_style="clear", fx_highpass=True, fx_limiter=True)
    (rp,) = make_peers(a, 1)
    rp.recv_all(0.1)
    for k in range(40):                                  # traitement actif : le son est modifié
        aa.tick(vframe(k, 6.0))
    a.disable_all_processing()
    s = a.snapshot()
    assert (s.denoise_level, s.voice_style, s.fx_highpass, s.fx_limiter) == ("off", "natural", False, False)
    assert s.latency_added_ms == 0 and not s.denoise_active
    st = a.settings
    assert (st.denoise_level, st.voice_style, st.fx_highpass, st.fx_limiter) == ("off", "natural", False, False)
    for k in range(80):                                  # fondus terminés
        aa.tick(vframe(k, 6.0))
    rp.recv_all(0.2)
    sent = {}
    mine = {}
    for k in range(80):
        frame = vframe(k, 6.0)                           # voix forte (pointes > 15000), sans écrêtage
        mine[a._seq] = frame.copy()
        aa.tick(frame)
    for kind, d in rp.recv_all(0.4):
        if kind == b"A":
            sent[HEADER.unpack_from(d)[5]] = np.frombuffer(d, np.int16, count=FRAME, offset=HS + 4)
    assert len(sent) >= 70
    for seq, pcm in sent.items():
        assert np.array_equal(pcm, mine[seq]), seq        # bit à bit
    assert np.abs(np.concatenate(list(mine.values()))).max() > 15000
    rp.close()


def test_moniteur_ab_sans_filtre_avec_filtre(rig):
    a, aa = rig("Alice", denoise_level="strong")          # Fort : +25 ms, donc A et B se distinguent nettement
    a.set_monitor(True, timeout_s=None, processed=False)
    assert a.snapshot().monitor and not a.snapshot().monitor_processed
    sig = [vframe(k) for k in range(400)]
    outs = []
    for k in range(120):
        outs.append(aa.tick(sig[k]))
    assert np.array_equal(outs[-1], sig[119])             # Sans filtre : le micro tel quel, non retardé
    a.set_monitor_processed(True)                          # bascule A -> B pendant l'écoute
    assert a.snapshot().monitor_processed
    for k in range(120, 300):
        outs.append(aa.tick(sig[k]))
    flat = np.concatenate(outs).astype(np.float64)
    raw = np.concatenate(sig[:300]).astype(np.float64)
    nat = np.abs(np.diff(raw)).max()
    assert np.abs(np.diff(flat[115 * FRAME:140 * FRAME])).max() <= 1.5 * nat + 50    # fondu : pas de clic
    assert not np.array_equal(outs[-1], sig[299])          # Avec filtre : le son traité, retardé de 25 ms
    sl = slice(200 * FRAME, 300 * FRAME)
    d = denoise.RNNOISE_LATENCY
    c_delayed = np.corrcoef(flat[sl], raw[200 * FRAME - d:300 * FRAME - d])[0, 1]
    c_direct = np.corrcoef(flat[sl], raw[sl])[0, 1]
    assert c_delayed > 0.9 and c_delayed > c_direct + 0.2, (c_delayed, c_direct)
    a.set_monitor_processed(False)                         # et retour
    for k in range(300, 330):
        outs.append(aa.tick(sig[k]))
    assert np.array_equal(outs[-1], sig[329])
    assert np.abs(np.diff(np.concatenate(outs[296:330]).astype(np.float64))).max() <= 1.5 * nat + 50


def test_tracemalloc_du_callback(rig):
    """Pas de tableau numpy alloué dans le callback ; au plus 4 Ko de temporaires CPython par trame."""
    a, aa = rig("Alice", denoise_level="strong", voice_style="clear", fx_highpass=True, fx_limiter=True,
                recv_leveler=True)
    raws = make_peers(a, 3)
    for p in a.snapshot().peers:
        a.set_peer_clean(p.id, True)
    peers = list(a._peers.values())
    mic = np.zeros((FRAME, 1), np.int16)
    out = np.zeros((FRAME, 1), np.int16)
    for k in range(600):                                   # chauffe (états, fondus, première trame de chaque ami)
        for i, p in enumerate(peers):
            p.q.append(vframe(k + 40 * i))
        mic[:, 0] = vframe(k)
        a._callback(mic, out, FRAME, None, None)
    tracemalloc.start()
    peaks = []
    try:
        for k in range(600, 1000):
            for i, p in enumerate(peers):
                p.q.append(vframe(k + 40 * i))
            mic[:, 0] = vframe(k)
            base = tracemalloc.get_traced_memory()[0]
            tracemalloc.reset_peak()
            a._callback(mic, out, FRAME, None, None)
            peaks.append(tracemalloc.get_traced_memory()[1] - base)
    finally:
        tracemalloc.stop()
    peaks = np.array(peaks)
    print(f"\ntracemalloc callback (chaîne + Fort + 3 amis nettoyés): médiane {np.median(peaks):.0f} o, "
          f"p99 {np.percentile(peaks, 99):.0f} o, max {peaks.max()} o")
    assert a.stats()["callback_errors"] == 0
    assert np.percentile(peaks, 99) <= 4096 and np.median(peaks) <= 4096
    for r in raws:
        r.close()


def test_budget_du_callback_chaine_fort_et_3_amis_nettoyes(rig):
    """Mesure du budget (5 ms par trame) : chaîne « Plus claire » + Fort + 3 amis nettoyés, p99 < 2,5 ms."""
    a, aa = rig("Alice", denoise_level="strong", voice_style="clear", fx_highpass=True, fx_limiter=True,
                recv_leveler=True)
    raws = make_peers(a, 3)
    for p in a.snapshot().peers:
        a.set_peer_clean(p.id, True)
    peers = list(a._peers.values())
    mic = np.zeros((FRAME, 1), np.int16)
    out = np.zeros((FRAME, 1), np.int16)
    ts = []
    for k in range(4000):
        for i, p in enumerate(peers):
            p.q.append(vframe(k + 40 * i))
        mic[:, 0] = vframe(k)
        t0 = time.perf_counter()
        a._callback(mic, out, FRAME, None, None)
        ts.append(time.perf_counter() - t0)
    d = np.array(ts[500:]) * 1000
    print(f"\nBUDGET chaîne+Fort+3 amis nettoyés: moyenne {d.mean():.3f} ms, p50 {np.median(d):.3f}, "
          f"p99 {np.percentile(d, 99):.3f}, p99,9 {np.percentile(d, 99.9):.3f}, max {d.max():.3f} ms (budget 5 ms, "
          f"charge moyenne {100 * d.mean() / 5:.1f} %)")
    assert a.stats()["callback_errors"] == 0
    assert a.snapshot().denoise_active and sum(p.clean_active for p in a.snapshot().peers) == 3
    assert np.percentile(d, 99) < 2.5
    for r in raws:
        r.close()


def test_envoi_prudent_avec_le_suppresseur(rig):
    """Fort : le silence n'est pas envoyé (après 300 ms), la voix l'est toujours, sans coupure en début de mot."""
    a, aa = rig("Alice", denoise_level="strong")
    (rp,) = make_peers(a, 1)
    for k in range(200):
        aa.tick(vframe(k))
    n0 = a._sent
    assert a.snapshot().transmitting
    for k in range(200):                                   # silence : maintien de 300 ms puis plus rien
        aa.tick(np.zeros(FRAME, np.int16))
    held = a._sent - n0
    assert 55 <= held <= 90, held
    assert not a.snapshot().transmitting
    n1 = a._sent
    for k in range(60):                                    # la parole reprend : tout est envoyé dès le début du mot
        aa.tick(vframe(k + 200))
    assert a._sent - n1 >= 55
    # un bruit de clavier (impulsions) n'ouvre pas l'envoi : RNNoise le supprime
    rng = np.random.default_rng(3)
    for k in range(200):
        aa.tick(np.zeros(FRAME, np.int16))
    n3 = a._sent
    for k in range(400):
        x = np.zeros(FRAME, np.int16)
        if k % 40 == 0:
            x[:60] = (rng.standard_normal(60) * 2000).astype(np.int16)
        aa.tick(x)
    assert a._sent - n3 < 80, a._sent - n3
    rp.close()


def test_le_micro_reste_ouvert_sur_aucune_et_leger(rig):
    """Comportement 2.0 conservé : sans suppresseur (ou Léger), tout est envoyé, même le silence."""
    for level in ("off", "light"):
        a, aa = rig(f"A{level}", denoise_level=level)
        (rp,) = make_peers(a, 1)
        n0 = a._sent
        for k in range(100):
            aa.tick(np.zeros(FRAME, np.int16))
        assert a._sent - n0 == 100, level
        rp.close()


def test_panne_du_suppresseur_repli_sans_coupure(rig, denoise_cache, monkeypatch):
    monkeypatch.setattr(denoise, "_candidates", lambda: [])
    denoise.reset_cache()
    a, aa = rig("Alice", denoise_level="strong")
    assert not a.denoise_available()
    s = a.snapshot()
    assert not s.denoise_active and s.denoise_error_fr and "suppression du bruit" in s.denoise_error_fr.lower()
    assert s.denoise_level == "strong" and s.latency_added_ms == 0
    evs = events_of(a, "denoise_failed")
    assert len(evs) == 1 and evs[0].message_fr == s.denoise_error_fr
    (rp,) = make_peers(a, 1)
    n0 = a._sent
    for k in range(50):
        aa.tick(vframe(k))
    assert a._sent - n0 == 50                              # le son passe (micro toujours ouvert), sans coupure
    with pytest.raises(EngineError) as ei:
        a.set_peer_clean(a.snapshot().peers[0].id, True)
    assert ei.value.code == "DENOISE_UNAVAILABLE"
    a.set_denoise_level("light")                           # le niveau Léger fonctionne sans la DLL
    s = a.snapshot()
    assert s.denoise_active and s.denoise_error_fr is None and s.latency_added_ms == 5.0
    rp.close()


def test_panne_en_cours_de_route(rig):
    a, aa = rig("Alice", denoise_level="strong")
    for k in range(60):
        aa.tick(vframe(k))
    assert a.snapshot().denoise_active

    def boom(x):
        raise RuntimeError("panne")
    a._slot.cur.core.process = boom
    for k in range(60, 80):
        aa.tick(vframe(k))
        assert np.abs(a._x).max() > 100                      # la voix envoyée ne tombe jamais à zéro
    assert wait_for(lambda: events_of(a, "denoise_failed"), timeout=2)
    s = a.snapshot()
    assert not s.denoise_active and s.denoise_error_fr and s.latency_added_ms == 0
    a.set_denoise_level("off")
    assert a.snapshot().denoise_error_fr is None


def test_reglages_21_valeurs_et_snapshot(rig):
    a, aa = rig("Alice")
    s = a.snapshot()
    assert (s.denoise_level, s.voice_style, s.recv_leveler, s.fx_highpass_hz) == ("off", "natural", False, 80)
    a.set_denoise_level("n'importe quoi")
    a.set_voice_style("rose")
    assert a.snapshot().denoise_level == "off" and a.snapshot().voice_style == "natural"
    a.set_denoise_level("normal")
    a.set_voice_style("clear")
    a.set_fx(highpass=True, highpass_hz=120, limiter=True, recv_leveler=True)
    a.set_fx(highpass_hz=99)                               # valeur invalide ignorée
    s = a.snapshot()
    assert (s.denoise_level, s.voice_style, s.fx_highpass, s.fx_highpass_hz, s.fx_limiter, s.recv_leveler) == \
        ("normal", "clear", True, 120, True, True)
    assert a.settings.dirty and a.settings.denoise_level == "normal" and a.settings.fx_highpass_hz == 120
    assert abs(s.latency_added_ms - denoise.level_latency_samples("normal") / 48.0) < 0.01
    for k in range(5):                                     # micro qui sature
        aa.tick(np.full(FRAME, 32000, np.int16))
    s = a.snapshot()
    assert s.clipping and s.mic_level > 0.9 and 0.0 <= s.mic_level_out <= 1.0


def test_filtre_manuel_et_une_seule_porte(rig):
    """Avec Normal / Fort la porte manuelle est neutralisée (jamais deux portes en série)."""
    a, aa = rig("Alice", gate_db=-40.0, denoise_level="off")
    assert a._chain.config[4] == -40.0
    a.set_denoise_level("strong")
    assert a._chain.config[4] == -70.0
    a.set_denoise_level("light")
    assert a._chain.config[4] == -40.0
    a.set_gate_db(-30.0)
    assert a._chain.config[4] == -30.0


def test_settings_migration_20_vers_21_sans_perte(tmp_path):
    old = {"name": "Zoé", "mic_gain": 1.4, "master": 0.8, "gate_db": -55.0, "buffer_mode": "stable",
           "ptt_mode": True, "ptt_key": "mouse4", "manual_peers": ["192.168.1.9"], "install_id": "0a1b2c3d",
           "port": 50600, "schema": 1, "peer_prefs": {"deadbeef": {"volume": 1.3, "muted": True, "name": "Gwanda"}},
           "ui": {"first_run_done": True, "show_advanced": True}}
    (tmp_path / "settings.json").write_text(json.dumps(old), encoding="utf-8")
    s = Settings.load(directory=tmp_path, legacy=tmp_path / "rien.json")
    # rien n'est perdu
    assert (s.name, s.mic_gain, s.master, s.gate_db, s.buffer_mode, s.ptt_key, s.install_id, s.port) == \
        ("Zoé", 1.4, 0.8, -55.0, "stable", "mouse4", "0a1b2c3d", 50600)
    assert s.manual_peers == ["192.168.1.9"] and s.ui.first_run_done and s.ui.show_advanced
    assert s.peer_prefs["deadbeef"] == {"volume": 1.3, "muted": True, "clean": False, "name": "Gwanda"}
    # défauts 2.1 = chemin 2.0
    assert (s.denoise_level, s.voice_style, s.fx_highpass, s.fx_highpass_hz, s.fx_limiter, s.recv_leveler) == \
        ("off", "natural", True, 80, True, True)
    data = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))      # réécrit, complet
    assert data["denoise_level"] == "off" and data["peer_prefs"]["deadbeef"]["clean"] is False
    assert data["name"] == "Zoé" and data["ptt_key"] == "mouse4"


def test_settings_21_valeurs_invalides_et_clean_conserve(tmp_path):
    bad = {"denoise_level": "turbo", "voice_style": 3, "fx_highpass": "oui", "fx_highpass_hz": 95,
           "fx_limiter": None, "recv_leveler": 1,
           "peer_prefs": {"a": {"volume": 1.0, "clean": True}, "b": {"clean": "oui"}, "c": {"clean": False}}}
    (tmp_path / "settings.json").write_text(json.dumps(bad), encoding="utf-8")
    s = Settings.load(directory=tmp_path, legacy=tmp_path / "rien.json")
    assert (s.denoise_level, s.voice_style, s.fx_highpass, s.fx_highpass_hz, s.fx_limiter, s.recv_leveler) == \
        ("off", "natural", True, 80, True, True)
    assert s.peer_prefs["a"]["clean"] is True and s.peer_prefs["b"]["clean"] is False
    assert s.peer_prefs["c"]["clean"] is False
    s.denoise_level, s.voice_style, s.fx_highpass_hz = "strong", "clear", 120
    s.save()
    t = Settings.load(directory=tmp_path, legacy=tmp_path / "rien.json")
    assert (t.denoise_level, t.voice_style, t.fx_highpass_hz) == ("strong", "clear", 120)
    assert t.peer_prefs["a"]["clean"] is True


# ---------------------------------------------------------------- revue croisée (Dev A relit son code)
def test_tout_desactiver_coupe_aussi_le_filtre_de_bruit_manuel(rig):
    """Le filtre manuel (même à 12 dB) change le son : « bit à bit identique » exige qu'il soit coupé aussi."""
    a, aa = rig("Alice", gate_db=-40.0)
    (rp,) = make_peers(a, 1)
    a.disable_all_processing()
    assert a.snapshot().gate_db == -70.0 and a.settings.gate_db == -70.0
    for k in range(80):
        aa.tick(vframe(k, 0.01))                          # très faible : un filtre manuel le fermerait
    rp.recv_all(0.2)
    for k in range(80):
        aa.tick(vframe(k, 0.01))
    got = [d for kind, d in rp.recv_all(0.4) if kind == b"A"]
    assert len(got) >= 70                                 # le micro reste ouvert : rien n'est filtré
    rp.close()


def test_valeur_non_finie_dans_la_chaine_se_reprend(rig):
    """Un état de filtre NaN resterait NaN pour toute la session : le moteur remet la chaîne à zéro."""
    a, aa = rig("Alice", fx_highpass=True, fx_limiter=True)
    for k in range(40):
        aa.tick(vframe(k))
    a._chain._hp.f.z[:] = np.nan                           # empoisonnement
    for k in range(40):
        aa.tick(vframe(k))
    s = a.snapshot()
    assert a.stats()["callback_errors"] >= 1
    assert np.isfinite(a._chain._hp.f.z).all()
    assert 0.3 < s.mic_level_out <= 1.0                    # la voix repasse, pas de silence définitif


def test_clean_limit_et_indisponible_ont_un_message(rig, denoise_cache, monkeypatch):
    a, aa = rig("Alice")
    raws = make_peers(a, 4)
    ids = [p.id for p in a.snapshot().peers]
    for pid in ids[:3]:
        a.set_peer_clean(pid, True)
    with pytest.raises(EngineError) as e:
        a.set_peer_clean(ids[3], True)
    assert e.value.code == "CLEAN_LIMIT" and "3" in e.value.message_fr
    a.set_peer_clean(ids[0], False)
    monkeypatch.setattr(denoise, "_candidates", lambda: [])
    denoise.reset_cache()
    with pytest.raises(EngineError) as e2:
        a.set_peer_clean(ids[3], True)
    assert e2.value.code == "DENOISE_UNAVAILABLE" and e2.value.message_fr
    assert not a.snapshot().peers[3].clean              # rien de mémorisé après un refus
    for r in raws:
        r.close()


# ---------------------------------------------------------------- v2.1 phase 4b : budget CPU, seuil d'envoi, A/B, pannes, réglages
def _compteur_rnnoise(cores):
    """Remplace l'appel natif de chaque noyau par un compteur (un seul entier modifiable partagé)."""
    n = [0]
    for core in cores:
        fn = getattr(core, "_fn_orig", None) or core._fn
        core._fn_orig = fn

        def w(*args, _fn=fn):
            n[0] += 1
            return _fn(*args)
        core._fn = w
    return n


def _noyaux(a):
    c = [a._slot.cur.core] if a._slot.cur is not None else []
    return c + [p.cleaner.core for p in a._peers.values() if p.cleaner is not None]


def test_pas_plus_de_2_calculs_rnnoise_par_callback(rig):
    """M1 : micro sur une parité de trames, amis nettoyés répartis (2 + 2) : jamais plus de 2 calculs RNNoise dans un même
    callback de 5 ms (avant : les 4 en phase, donc 4 calculs une trame sur deux, p99 de 2,6 à 3,3 ms en vrai processus).
    Vrai aussi après un changement de cran et quand les amis reprennent la parole après une pause."""
    a, aa = rig("Alice", denoise_level="strong", voice_style="clear")
    raws = make_peers(a, 3)
    for p in a.snapshot().peers:
        a.set_peer_clean(p.id, True)
    peers = list(a._peers.values())
    mic = np.zeros((FRAME, 1), np.int16)
    out = np.zeros((FRAME, 1), np.int16)
    k = [0]
    cnt = [[0]]

    def avance(n, parle=(True, True, True)):
        comptes = []
        for _ in range(n):
            for i, p in enumerate(peers):
                if parle[i]:
                    p.q.append(vframe(k[0] + 40 * i))
            mic[:, 0] = vframe(k[0])
            c0 = cnt[0][0]
            a._callback(mic, out, FRAME, None, None)
            comptes.append(cnt[0][0] - c0)
            k[0] += 1
        return comptes
    avance(40)                                               # adoption du suppresseur d'émission par le callback
    cnt[0] = _compteur_rnnoise(_noyaux(a))
    c = avance(400)
    assert max(c) == 2 and min(c) == 2, sorted(set(c))       # 4 instances, 2 calculs par trame, toujours
    pars = [a._slot.cur.par] + [p.cleaner.par for p in peers]
    assert pars.count(0) == 2 and pars.count(1) == 2, pars
    # changement de cran : l'ancien et le nouveau suppresseur se croisent ~30 ms, puis plus que 2 par trame
    a.set_denoise_level("normal")
    avance(12)
    cnt[0] = _compteur_rnnoise(_noyaux(a))
    c = avance(200)
    assert max(c) == 2, sorted(set(c))
    # les amis se taisent plus d'une seconde (flux arrêté), puis reprennent : leurs phases sont recalées
    avance(eng.CLEAN_GAP + 30, parle=(False, False, False))
    avance(5)
    c = avance(300)
    assert max(c) == 2, sorted(set(c))
    assert a.stats()["callback_errors"] == 0
    for r in raws:
        r.close()


def test_seuil_d_envoi_vad_et_signal_a_moins_62_dbfs(rig):
    """Le VAD n'ouvre jamais seul : il faut aussi un signal d'au moins -62 dBFS à l'entrée (avant : -70, sous tout bruit de
    micro réel donc VAD seul en pratique). Le VAD est forcé à 1 et la voix traitée à ~0 pour isoler cette condition."""
    assert eng.SEND_VAD_IN_DB == -62.0
    a, aa = rig("Alice", denoise_level="strong")
    for k in range(30):
        aa.tick(vframe(k))
    core = a._slot.cur.core

    def vad_toujours_vrai(x):
        x *= 0.001                                           # voix traitée presque nulle : seule la condition VAD compte
        core.vad = 1.0
    core.process = vad_toujours_vrai
    for db, ouvert in ((-66.0, False), (-63.0, False), (-60.0, True), (-45.0, True), (-80.0, False)):
        a._tx_hold = 0
        amp = 32768 * 10 ** (db / 20) * 2 ** 0.5             # sinusoïde de niveau efficace `db` dBFS
        for k in range(150):
            aa.tick(sine(k, 1000.0, amp))
        assert a._tx is ouvert, (db, a._tx)
    assert a.stats()["callback_errors"] == 0


def test_chuchotement_part_sur_aucune_et_leger(rig):
    """A9 : sur Aucune / Léger le micro reste ouvert : un chuchotement à -50 dBFS part. (Sur Normal / Fort, c'est le niveau
    traité qui décide, pas le seuil VAD : voir correctifs_devA.md.)"""
    rng = np.random.default_rng(5)
    chuchote = (rng.standard_normal(FRAME * 400) * 32768 * 10 ** (-50 / 20)).astype(np.int16)
    for niveau in ("off", "light"):
        a, aa = rig(f"Alice_{niveau}", denoise_level=niveau)
        for k in range(200):
            aa.tick(chuchote[k * FRAME:(k + 1) * FRAME])
        assert a._tx is True


def test_ab_avec_filtre_silencieux_quand_l_envoi_est_ferme(rig):
    """A7 : « Avec filtre » = ce que les autres entendent. Envoi fermé (appuyer-pour-parler relâché, micro coupé, filtre
    de bruit manuel fermé) : silencieux, en fondu ; « Sans filtre » reste audible ; rouvert : audible de nouveau."""
    a, aa = rig("Alice")
    a.set_monitor(True, timeout_s=120, processed=True)

    def ecoute(n, amp=8000):
        o = [aa.tick(sine(k, 700, amp)) for k in range(n)]
        return int(np.abs(o[-1]).max())
    assert ecoute(8) > 6000                                  # envoi ouvert (micro toujours ouvert) : on s'entend
    a.set_ptt(True)                                          # appuyer-pour-parler, relâché
    assert ecoute(8) == 0
    a.ptt_down()
    assert ecoute(8) > 6000
    a.ptt_up()
    assert ecoute(8) == 0
    a.set_monitor_processed(False)                           # « Sans filtre » : le micro brut, quoi qu'il arrive
    assert ecoute(8) > 6000
    a.set_monitor_processed(True)
    a.set_ptt(False)
    assert ecoute(8) > 6000
    a.set_muted(True)                                        # micro coupé
    assert ecoute(8) == 0
    a.set_muted(False)
    assert ecoute(8) > 6000
    # fondu : la descente se fait en 3 trames, sans marche
    a.set_muted(True)
    y = np.concatenate([aa.tick(sine(k, 300, 8000)) for k in range(6)]).astype(np.float64)
    assert np.abs(np.diff(y)).max() < 8000 * 2 * np.pi * 300 / 48000 * 1.5
    a.set_muted(False)
    # filtre de bruit manuel : un signal sous le seuil ferme l'envoi, le retour « Avec filtre » se tait
    a.set_gate_db(-40.0)
    for k in range(120):
        aa.tick(sine(k, 700, 120))                           # ~ -62 dBFS, sous le seuil de -40
    assert not a._tx
    assert int(np.abs(aa.tick(sine(0, 700, 120))).max()) == 0
    for k in range(10):
        o = aa.tick(sine(k, 700, 9000))
    assert a._tx and int(np.abs(o).max()) > 6000


def test_settings_infinity_nan_et_nombres_enormes(tmp_path):
    """A11 : `Infinity`, `-Infinity`, `NaN`, `1e999` et les entiers plus grands qu'un flottant dans settings.json ne lèvent
    jamais : la valeur est bornée ou remplacée par la valeur par défaut, et le reste du fichier est lu normalement."""
    gros = "1" + "0" * 400
    texte = ('{"name": "Zoé", "mic_gain": Infinity, "master": -Infinity, "gate_db": NaN, "port": Infinity, '
             '"buffer_mode": "stable", "fx_highpass_hz": Infinity, "denoise_level": "strong", '
             '"peer_prefs": {"aabbccdd": {"volume": Infinity, "muted": true, "clean": true}, '
             '"11223344": {"volume": 1e999}, "55667788": {"volume": ' + gros + '}, "99aabbcc": {"volume": NaN}}, '
             '"ui": {"sounds": false}}')
    d = tmp_path / "inf"
    d.mkdir()
    (d / "settings.json").write_text(texte, encoding="utf-8")
    s = Settings.load(directory=d, legacy=d / "rien.json")
    assert s.name == "Zoé" and s.buffer_mode == "stable" and s.denoise_level == "strong" and s.ui.sounds is False
    assert s.mic_gain == 1.0 and s.master == 1.0 and s.gate_db == -70.0 and s.port == 50555 and s.fx_highpass_hz == 80
    for pid in ("aabbccdd", "11223344", "55667788", "99aabbcc"):
        v = s.peer_prefs[pid]["volume"]
        assert 0.0 <= v <= 2.0 and v == v
    assert s.peer_prefs["aabbccdd"]["clean"] is True and s.peer_prefs["aabbccdd"]["muted"] is True
    for champ in ("mic_gain", "master", "gate_db", "port"):             # entiers énormes sur chaque champ numérique
        d2 = tmp_path / ("gros_" + champ)
        d2.mkdir()
        (d2 / "settings.json").write_text('{"%s": %s, "name": "Ok"}' % (champ, gros), encoding="utf-8")
        s2 = Settings.load(directory=d2, legacy=d2 / "rien.json")
        assert s2.name == "Ok"
        assert np.isfinite(float(getattr(s2, champ))) and 0 <= abs(float(getattr(s2, champ))) <= 65535
    # ancien fichier (migration 1.x) avec les mêmes valeurs
    (tmp_path / "vieux.json").write_text('{"name": "V", "mic_gain": 1e999, "master": %s, "gate_db": -Infinity, '
                                         '"peer_volumes": {"Bob": Infinity, "Eve": 0.5}}' % gros, encoding="utf-8")
    s3 = Settings.load(directory=tmp_path / "mig", legacy=tmp_path / "vieux.json")
    assert s3.name == "V" and 0.0 <= s3.mic_gain <= 2.0 and 0.0 <= s3.master <= 2.0 and s3.gate_db == -70.0
    assert s3.legacy_volumes["Eve"] == 0.5 and 0.0 <= s3.legacy_volumes["Bob"] <= 2.0


def test_reglages_du_moteur_avec_infini_et_nan(rig):
    a, aa = rig("Alice")
    a.set_mic_gain(float("inf"))
    assert a.snapshot().mic_gain == 2.0
    a.set_master(float("nan"))
    assert a.snapshot().master == 0.0
    a.set_mic_gain(10 ** 400)
    assert a.snapshot().mic_gain == 2.0
    a.set_gate_db(float("-inf"))
    assert a.snapshot().gate_db == -70.0
    a.set_gate_db(float("inf"))
    assert a.snapshot().gate_db == -20.0
    a.set_peer_volume("aabbccdd", float("inf"))
    assert a.settings.peer_prefs["aabbccdd"]["volume"] == 2.0
    assert aa.tick(sine(0)).dtype == np.int16


def _tick_ami(a, p, f0=233.0):
    mic = np.zeros((FRAME, 1), np.int16)
    out = np.zeros((FRAME, 1), np.int16)

    def tick(k):
        p.q.append(sine(k, f0, 6000))
        a._callback(mic, out, FRAME, None, None)
        return out[:, 0].astype(np.float64).copy()
    return tick, 6000 * 2 * np.pi * f0 / 48000


def test_retrait_du_nettoyage_d_un_ami_sans_saut(rig):
    """A5 : « Nettoyer » coupé en pleine voix : fondu de 20 ms vers le sec, le nettoyeur n'est libéré qu'ensuite (avant :
    retiré d'un coup, le son sautait de 25 ms)."""
    a, aa = rig("Alice")
    (rp,) = make_peers(a, 1)
    pid = a.snapshot().peers[0].id
    a.set_peer_clean(pid, True)
    p = next(iter(a._peers.values()))
    tick, nat = _tick_ami(a, p)
    ys = [tick(k) for k in range(300)]
    assert p.cleaner is not None and p.cleaner.enabled
    a.set_peer_clean(pid, False)
    assert p.cleaner is not None and not p.cleaner.enabled and not a.snapshot().peers[0].clean_active
    ys += [tick(k) for k in range(300, 330)]
    y = np.concatenate(ys[290:330])
    assert np.abs(np.diff(y)).max() < nat * 1.6 + 60, (np.abs(np.diff(y)).max(), nat)     # aucun saut pendant la transition
    a._check_denoise()
    assert p.cleaner is None                                 # libéré une fois le fondu fini
    assert a.snapshot().peers[0].clean is False
    a.set_peer_clean(pid, True)                              # on peut le remettre
    assert a.snapshot().peers[0].clean_active
    rp.close()


def test_panne_du_nettoyage_d_un_ami_sans_saut_et_signalee(rig):
    """A4 côté amis : le nettoyeur en panne descend vers le sec en fondu ; événement `denoise_failed` (code peer)."""
    a, aa = rig("Alice")
    (rp,) = make_peers(a, 1)
    pid = a.snapshot().peers[0].id
    a.set_peer_clean(pid, True)
    p = next(iter(a._peers.values()))
    tick, nat = _tick_ami(a, p)
    ys = [tick(k) for k in range(300)]

    def boom(x):
        raise RuntimeError("panne")
    p.cleaner.core.process = boom
    ys += [tick(k) for k in range(300, 330)]
    y = np.concatenate(ys[290:330])
    assert np.abs(np.diff(y)).max() < nat * 1.6 + 60, (np.abs(np.diff(y)).max(), nat)
    a._check_denoise()
    evs = [e for e in events_of(a, "denoise_failed") if e.code == "peer"]
    assert len(evs) == 1 and evs[0].peer_id == pid and "s'est arrêté" in evs[0].message_fr
    v = a.snapshot().peers[0]
    assert v.clean and not v.clean_active and v.clean_reason and "s'est arrêté" in v.clean_reason
    assert p.cleaner is None                                 # libéré (fondu fini)
    rp.close()


def test_clean_demande_mais_impossible_est_explique(rig, denoise_cache, monkeypatch):
    """A10 : un ami marqué « nettoyer » au-delà de 3, ou sans DLL, démarre sans nettoyeur : `clean_active` faux, une
    raison lisible dans `PeerView.clean_reason` ET un événement `denoise_failed` (code peer) ; quand une place se libère,
    le nettoyage en pause démarre tout seul."""
    a, aa = rig("Alice")
    raws = make_peers(a, 4)
    ids = [p.id for p in a.snapshot().peers]
    for pid in ids[:3]:
        a.set_peer_clean(pid, True)
    # le 4e ami était déjà mémorisé « nettoyer » et revient : plus de place
    a.settings.peer_prefs[ids[3]] = {"volume": 1.0, "muted": False, "clean": True, "name": "Ami4"}
    raws[3].send(b"X")
    assert wait_for(lambda: len(a.snapshot().peers) == 3)
    events_of(a)
    raws[3].beacon("Ami4")
    assert wait_for(lambda: len(a.snapshot().peers) == 4)
    v = next(p for p in a.snapshot().peers if p.id == ids[3])
    assert v.clean and not v.clean_active
    assert v.clean_reason and "3" in v.clean_reason and "maximum" in v.clean_reason
    evs = [e for e in events_of(a, "denoise_failed") if e.code == "peer"]
    assert len(evs) == 1 and evs[0].peer_id == ids[3] and evs[0].message_fr
    assert all(p.clean_reason is None for p in a.snapshot().peers if p.clean_active)
    a.set_peer_clean(ids[0], False)                          # une place se libère : le 4e démarre seul (entretien)
    assert wait_for(lambda: next(p for p in a.snapshot().peers if p.id == ids[3]).clean_active, timeout=4)
    assert next(p for p in a.snapshot().peers if p.id == ids[3]).clean_reason is None
    for r in raws:
        r.close()
    # sans DLL
    monkeypatch.setattr(denoise, "_candidates", lambda: [])
    denoise.reset_cache()
    b, bb = rig("Bob")
    (r1,) = make_peers(b, 1)
    pid = b.snapshot().peers[0].id
    r1.send(b"X")
    assert wait_for(lambda: len(b.snapshot().peers) == 0)
    b.settings.peer_prefs[pid] = {"volume": 1.0, "muted": False, "clean": True, "name": "Ami1"}
    events_of(b)
    r1.beacon("Ami1")
    assert wait_for(lambda: len(b.snapshot().peers) == 1)
    v = b.snapshot().peers[0]
    assert v.clean and not v.clean_active and "pas disponible" in (v.clean_reason or "")
    evs = [e for e in events_of(b, "denoise_failed") if e.code == "peer"]
    assert len(evs) == 1 and "pas disponible" in evs[0].message_fr
    r1.close()

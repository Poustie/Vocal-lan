"""Moteur factice pour développer et tester l'interface sans carte son ni réseau.

Même API que ``engine.Engine`` (voir le contrat). Aucune dépendance à sounddevice
ni aux sockets : tout est simulé à partir de l'horloge (``time.monotonic``),
sans thread. Les pairs fictifs arrivent, partent, parlent, ont des qualités
différentes ; le micro simulé produit des salves de voix.

Scénarios (paramètre ``scenario``) :
  - "demo"  : Marie arrive, puis Tom, puis Léa ; Tom part puis revient (boucle).
  - "full"  : quatre pairs déjà présents (idéal pour une capture d'écran).
  - "alone" : personne d'autre sur le réseau (``seconds_alone`` augmente).
  - "nonet" : pas de réseau.
  - "nobody": deux pairs présents mais aucun ne te reçoit (pare-feu).
2.1 :
  - "denoise_fail" : suppression du bruit sur Normal, qui tombe en panne après ~2 s (bandeau).
  - "noisy_friend" : un ami au micro bruyant (``noisy``), suggestion « Nettoyer sa voix ».
  - "clean3"       : 3 amis déjà nettoyés + un 4e que le moteur refuse (CLEAN_LIMIT).
"""
from __future__ import annotations

import ipaddress
import math
import queue
import random
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

# --------------------------------------------------------------------------
# Dataclasses du contrat : importées de engine.py quand elles ont les champs 2.1, sinon définies à
# l'identique ici (le moteur de la 2.0 ne les a pas ; l'interface lit les champs 2.1 par getattr).
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class _Event:
    kind: str
    message_fr: str = ""
    peer_id: str | None = None
    peer_name: str | None = None
    code: str | None = None


class _EngineError(Exception):
    def __init__(self, code: str, message_fr: str = ""):
        super().__init__(message_fr or code)
        self.code = code
        self.message_fr = message_fr or code


@dataclass(frozen=True, slots=True)
class _AudioInfo:
    ok: bool
    input_name: str | None
    output_name: str | None
    latency_ms: tuple[float, float] | None
    degraded: bool
    error_fr: str | None


@dataclass(frozen=True, slots=True)
class _PeerView:
    id: str
    name: str
    ip: str
    speaking: bool
    level: float
    volume: float
    muted: bool
    remote_muted: bool
    remote_deafened: bool
    quality: str
    ping_ms: float | None
    loss_pct: float
    buffer_ms: int
    hears_me: bool
    clean: bool = False
    clean_active: bool = False
    clean_reason: str | None = None
    noisy: bool = False


@dataclass(frozen=True, slots=True)
class _Snapshot:
    online: bool
    name: str
    local_ip: str
    port: int
    muted: bool
    deafened: bool
    monitor: bool
    ptt_mode: bool
    ptt_active: bool
    transmitting: bool
    speaking: bool
    talking_while_muted: bool
    mic_level: float
    gate_level: float
    mic_gain: float
    master: float
    gate_db: float
    buffer_mode: str
    audio: _AudioInfo
    network: str
    seconds_alone: float
    peers: tuple
    denoise_level: str = "off"
    denoise_active: bool = False
    denoise_error_fr: str | None = None
    voice_style: str = "natural"
    latency_added_ms: float = 0.0
    mic_level_out: float = 0.0
    clipping: bool = False
    monitor_processed: bool = True
    fx_highpass: bool = True
    fx_highpass_hz: int = 80
    fx_limiter: bool = True
    recv_leveler: bool = True


try:  # pragma: no cover - dépend de l'avancement de Dev A
    import engine as _engine  # type: ignore
    if "denoise_level" in _engine.Snapshot.__dataclass_fields__ and "clean" in _engine.PeerView.__dataclass_fields__:
        AudioInfo, EngineError, Event = _engine.AudioInfo, _engine.EngineError, _engine.Event
        PeerView, Snapshot = _engine.PeerView, _engine.Snapshot
    else:
        raise ImportError("moteur 2.0")
except Exception:  # noqa: BLE001
    AudioInfo, EngineError, Event, PeerView, Snapshot = _AudioInfo, _EngineError, _Event, _PeerView, _Snapshot


# Latence ajoutée par chaque cran (ms), valeurs du contrat ; le vrai moteur donne la sienne dans
# ``Snapshot.latency_added_ms``.
DENOISE_DELAY_MS = {"off": 0.0, "light": 5.0, "normal": 25.0, "strong": 25.0}
try:        # les vrais délais nominaux (Normal = 25 ms si le candidat « RNNoise + sec » est retenu)
    import denoise as _dn  # type: ignore
    DENOISE_DELAY_MS = {k: _dn.level_latency_samples(k) / 48.0 for k in DENOISE_DELAY_MS}
except Exception:  # noqa: BLE001
    pass
CLEAN_MAX = 3


# --------------------------------------------------------------------------
# Réglages : on utilise settings.py (Dev A) s'il existe, sinon un stub local
# qui ne touche jamais au disque.
# --------------------------------------------------------------------------
try:
    from settings import Settings, UiPrefs, VERSION, app_dir, resource_path  # type: ignore
    REAL_SETTINGS = True
except Exception:  # noqa: BLE001
    REAL_SETTINGS = False
    VERSION = "2.0.0"

    def app_dir() -> Path:
        p = Path(tempfile.gettempdir()) / "vocal-lan-fake"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def resource_path(rel: str) -> Path:
        return Path(__file__).resolve().parent / rel

    @dataclass
    class UiPrefs:
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
        manual_peers: list = field(default_factory=list)
        peer_prefs: dict = field(default_factory=dict)
        install_id: str = "a1b2c3d4"
        port: int = 50555
        ui: UiPrefs = field(default_factory=UiPrefs)
        dirty: bool = field(default=False, repr=False, compare=False)
        save_count: int = field(default=0, repr=False, compare=False)

        @classmethod
        def load(cls) -> "Settings":
            return cls()

        def save(self) -> None:  # en mémoire seulement
            self.dirty = False
            self.save_count += 1

        def save_if_dirty(self) -> None:
            if self.dirty:
                self.save()


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(v)))


def db_to_level(db: float) -> float:
    """Échelle du vumètre (comme engine.to_meter) : -60 dB -> 0, 0 dB -> 1."""
    return _clamp((db + 60.0) / 60.0, 0.0, 1.0)


_DEVICES_IN = ["Microphone (Realtek High Definition Audio)",
               "Micro casque (USB Gaming Headset)",
               "Webcam HD (Micro intégré)"]
_DEVICES_OUT = ["Haut-parleurs (Realtek High Definition Audio)",
                "Casque (USB Gaming Headset)",
                "Écran (HDMI)"]


class FakeEngine:
    """Même API que ``engine.Engine``."""

    current: "FakeEngine | None" = None   # pour FakeHotkeys

    def __init__(self, settings, scenario: str = "demo", alone_offset: float = 0.0):
        self.settings = settings
        self.scenario = scenario
        self.events: queue.SimpleQueue = queue.SimpleQueue()
        self.cues: list[str] = []
        self.calls: list[tuple] = []            # journal des appels (tests)
        self._rng = random.Random(7)
        self._t0 = time.monotonic()
        self._started = False
        self._online = False
        self._closed = False
        self._muted = False
        self._deafened = False
        self._muted_before_deaf = False
        self.go_online_error: "EngineError | None" = None   # tests : go_online() échoue
        self.add_ip_delay = 0.0                              # tests : add_peer_ip lent (DNS)
        self._monitor_until: float | None = None
        self._ptt_active = False
        self._speaking = False
        self._mic_smooth = 0.0
        self._peers: dict[str, dict] = {}
        self._script: list[tuple[float, callable]] = []
        self._fw_due: float | None = None
        self._alone_since: float | None = None
        self._alone_offset = alone_offset
        self._extra_devices = False
        self._devices_due: float | None = None
        self.device_delay = 0.0                 # simule la lenteur de set_devices (0,2 à 0,5 s en vrai)
        self.fail_devices: set = set()          # noms de périphériques qui « ne s'ouvrent pas »
        self._audio_broken_until: float | None = None
        self.local_ip = "192.168.1.42"
        # --- 2.1 : traitement de la voix (valeurs par défaut du contrat = comportement 2.0)
        for name, default in (("denoise_level", "off"), ("voice_style", "natural"), ("fx_highpass", True),
                              ("fx_highpass_hz", 80), ("fx_limiter", True), ("recv_leveler", True)):
            if not hasattr(settings, name):
                setattr(settings, name, default)
        self.denoise_ok = True                  # tests : denoise_available()
        self._denoise_error: str | None = None
        self._denoise_fail_at: float | None = None
        self._monitor_processed = True
        self.force_clean_reason: str | None = None
        self._clip_until = 0.0
        self.force_clipping = False
        self.force_noise_floor: float | None = None      # tests : bruit de fond ambiant (0..1, échelle du vumètre)
        FakeEngine.current = self
        self._init_scenario(scenario)

    # ------------------------------------------------------------ scénarios
    def _now(self) -> float:
        return time.monotonic() - self._t0

    def _spawn(self, name: str, **kw) -> str:
        pid = kw.pop("pid", None) or "%08x" % self._rng.getrandbits(32)
        base = dict(
            name=name, ip="192.168.1.%d" % self._rng.randint(10, 99),
            period=self._rng.uniform(5.0, 9.0), duty=self._rng.uniform(0.3, 0.55),
            phase=self._rng.uniform(0, 8), ping=self._rng.uniform(1.0, 6.0), loss=0.0,
            buffer=10, quality="good", remote_muted=False, remote_deafened=False,
            hears_me=True, speak_always=False, noisy=False)
        base.update(kw)
        self._peers[pid] = base
        return pid

    def _init_scenario(self, scenario: str) -> None:
        if scenario == "full":
            self._spawn("Marie", pid="m1", period=6, duty=0.6, phase=0, ping=2.0)
            self._spawn("Tom", pid="t2", quality="ok", ping=24.0, loss=1.5, buffer=20,
                        remote_muted=True)
            self._spawn("Léa", pid="l3", quality="bad", ping=95.0, loss=6.0, buffer=40,
                        hears_me=False, remote_deafened=True)
            self._spawn("Kévin", pid="k4", period=9, duty=0.4, phase=2, ping=4.0)
        elif scenario == "nobody":      # des amis sont là mais aucun ne reçoit nos paquets (pare-feu)
            self._spawn("Marie", pid="m1", period=6, duty=0.6, phase=0, ping=2.0, hears_me=False)
            self._spawn("Tom", pid="t2", ping=5.0, hears_me=False)
        elif scenario == "demo":
            def join(name, **kw):
                return lambda: self._join(name, **kw)
            self._script = [
                (1.0, join("Marie", pid="m1", ping=2.0)),
                (3.0, join("Tom", pid="t2", quality="ok", ping=26.0, loss=1.0, buffer=20)),
                (9.0, join("Léa", pid="l3", quality="bad", ping=90.0, loss=5.0, buffer=40,
                           hears_me=False)),
                (25.0, lambda: self._leave("t2")),
                (31.0, join("Tom", pid="t2", quality="ok", ping=26.0, loss=1.0, buffer=20)),
                (40.0, lambda: self._leave("l3")),
            ]
        # "alone" et "nonet" : aucun pair
        elif scenario == "denoise_fail":
            self._spawn("Marie", pid="m1", period=6, duty=0.6, phase=0, ping=2.0)
            self.settings.denoise_level = "normal"
            self._denoise_fail_at = 2.0
        elif scenario == "noisy_friend":
            self._spawn("Marie", pid="m1", period=6, duty=0.6, phase=0, ping=2.0)
            self._spawn("Tom", pid="t2", ping=5.0, noisy=True, period=7, duty=0.5, phase=1)
            self._spawn("Léa", pid="l3", ping=6.0)
        elif scenario == "clean3":
            for n, pid in (("Marie", "m1"), ("Tom", "t2"), ("Léa", "l3")):
                self._spawn(n, pid=pid, ping=3.0, noisy=(pid == "t2"))
                self.settings.peer_prefs.setdefault(pid, {})["clean"] = True
            self._spawn("Kévin", pid="k4", ping=4.0, period=9, duty=0.4, phase=2)
        if scenario in ("alone", "nonet", "demo") or not self._peers:
            self._alone_since = -self._alone_offset

    def _join(self, name: str, **kw) -> None:
        pid = self._spawn(name, **kw)
        self._alone_since = None
        self.events.put(Event("peer_joined", f"{name} a rejoint", pid, name))

    def _leave(self, pid: str) -> None:
        p = self._peers.pop(pid, None)
        if p:
            self.events.put(Event("peer_left", f"{p['name']} est parti", pid, p["name"]))
            if not self._peers:
                self._alone_since = self._now()

    # ------------------------------------------------------------- cycle
    def start(self, online: bool = True) -> None:
        self._started = True
        self._online = online
        self._t0 = time.monotonic()

    def go_online(self) -> None:
        if self.go_online_error is not None:
            raise self.go_online_error
        self._online = True
        self.calls.append(("go_online",))

    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._online = False
        self.calls.append(("shutdown",))

    # --------------------------------------------------------- simulation
    def _advance(self) -> None:
        now = self._now()
        while self._script and self._script[0][0] <= now:
            _, fn = self._script.pop(0)
            fn()
        if self._monitor_until is not None and time.monotonic() >= self._monitor_until:
            self._monitor_until = None
        if self._denoise_fail_at is not None and now >= self._denoise_fail_at:
            self._denoise_fail_at = None
            self.fail_denoise()
        if self._fw_due is not None and time.monotonic() >= self._fw_due:
            self._fw_due = None
            self.events.put(Event("firewall_result",
                                  "La règle de pare-feu a été ajoutée.", code="ok"))
        if self._devices_due is not None and time.monotonic() >= self._devices_due:
            self._devices_due = None
            self._extra_devices = not self._extra_devices      # simule un branchement
            self.events.put(Event("devices_changed", "La liste des périphériques a été actualisée."))
        if self._audio_broken_until is not None and time.monotonic() >= self._audio_broken_until:
            self._audio_broken_until = None
            self.events.put(Event("audio_recovered", "Le son est revenu."))

    def _voice(self, now: float) -> float:
        """Niveau brut de ma voix (0..1) : salves de parole de 2,5 s toutes les 7 s."""
        on = (now % 7.0) < 2.5
        if on:
            v = 0.45 + 0.4 * abs(math.sin(now * 7.3)) * (0.6 + 0.4 * math.sin(now * 2.1))
        else:
            v = 0.04 + 0.03 * self._rng.random()
        return _clamp(v, 0.0, 1.0)

    def _peer_view(self, pid: str, p: dict, now: float) -> PeerView:
        pref = self.settings.peer_prefs.get(pid, {})
        cyc = (now + p["phase"]) % p["period"]
        speaking = (cyc < p["period"] * p["duty"]) and not p["remote_muted"]
        if speaking:
            lvl = _clamp(0.35 + 0.5 * abs(math.sin(now * 5.1 + p["phase"])), 0, 1)
        else:
            lvl = 0.02 * self._rng.random()
        ping = None if p["quality"] == "unknown" else p["ping"] + 0.8 * math.sin(now + p["phase"])
        clean = bool(pref.get("clean", False))
        return PeerView(
            id=pid, name=p["name"], ip=p["ip"], speaking=speaking, level=lvl,
            volume=_clamp(pref.get("volume", 1.0), 0, 2), muted=bool(pref.get("muted", False)),
            remote_muted=p["remote_muted"], remote_deafened=p["remote_deafened"],
            quality=p["quality"], ping_ms=ping, loss_pct=p["loss"], buffer_ms=p["buffer"],
            hears_me=p["hears_me"], clean=clean, clean_active=clean and self._denoise_error is None and not self.force_clean_reason,
            clean_reason=(self.force_clean_reason if clean else None),
            noisy=bool(p.get("noisy", False)) and not clean)

    def snapshot(self) -> Snapshot:
        self._advance()
        s = self.settings
        now = self._now()
        gate_level = db_to_level(s.gate_db) if s.gate_db > -69.5 else 0.0
        raw = self._voice(now)
        voice_on = raw > 0.3
        self._mic_smooth = max(raw, self._mic_smooth * 0.8)
        level = self._mic_smooth if not self._deafened else 0.0
        lvl_s = s.denoise_level
        active = lvl_s != "off" and self._denoise_error is None
        if self.force_noise_floor is not None and not voice_on:
            level = max(level, self.force_noise_floor)
        if active:      # la barre « après » : le bruit (clavier, souffle) disparaît, la voix reste
            out = level if voice_on else level * (0.12 if lvl_s != "light" else 0.5)
        else:
            out = level
        speaking = level > max(gate_level, 0.12)
        gate_ok = level >= gate_level
        allowed = (self._ptt_active if s.ptt_mode else True)
        transmitting = (not self._muted) and allowed and gate_ok and level > 0.1
        twm = self._muted and voice_on
        peers = tuple(sorted((self._peer_view(i, p, now) for i, p in self._peers.items()),
                             key=lambda v: v.name.lower()))
        if self.scenario == "nonet":
            network, alone = "no_network", 0.0
        elif not peers:
            network = "alone"
            alone = max(0.0, now - (self._alone_since if self._alone_since is not None else 0.0))
        else:
            network, alone = "ok", 0.0
        broken = self._audio_broken_until is not None
        audio = AudioInfo(
            ok=not broken, input_name=s.dev_in or _DEVICES_IN[0],
            output_name=s.dev_out or _DEVICES_OUT[0], latency_ms=(11.0, 14.0), degraded=False,
            error_fr="Le casque a été débranché." if broken else None)
        return Snapshot(
            online=self._online, name=s.name or "Moi", local_ip=self.local_ip, port=s.port,
            muted=self._muted, deafened=self._deafened, monitor=self._monitor_until is not None,
            ptt_mode=s.ptt_mode, ptt_active=self._ptt_active, transmitting=transmitting,
            speaking=speaking and not self._muted and allowed, talking_while_muted=twm,
            mic_level=level, gate_level=gate_level, mic_gain=s.mic_gain, master=s.master,
            gate_db=s.gate_db, buffer_mode=s.buffer_mode, audio=audio, network=network,
            seconds_alone=alone, peers=peers,
            denoise_level=lvl_s, denoise_active=active, denoise_error_fr=self._denoise_error,
            voice_style=s.voice_style, latency_added_ms=self._added_ms(),
            mic_level_out=out if not self._deafened else 0.0,
            clipping=self.force_clipping or time.monotonic() < self._clip_until or level > 0.97,
            monitor_processed=self._monitor_processed, fx_highpass=s.fx_highpass,
            fx_highpass_hz=s.fx_highpass_hz, fx_limiter=s.fx_limiter, recv_leveler=s.recv_leveler)

    def _added_ms(self) -> float:
        if self._denoise_error is not None:
            return 0.0
        return DENOISE_DELAY_MS.get(self.settings.denoise_level, 0.0)

    # ----------------------------------------------------------- réglages
    def _dirty(self) -> None:
        self.settings.dirty = True

    def set_name(self, name: str) -> None:
        self.settings.name = (name or "").strip()[:24]
        self._dirty()
        self.calls.append(("set_name", self.settings.name))

    @property
    def muted(self) -> bool:
        return self._muted

    @property
    def deafened(self) -> bool:
        return self._deafened

    # Règle « Discord » (identique au vrai moteur) : la sourdine mémorise puis coupe le micro,
    # la fin de sourdine le restaure, et rétablir le micro pendant la sourdine lève la sourdine.
    def set_muted(self, on: bool) -> None:
        on = bool(on)
        if not on and self._deafened:
            self._deafened = False
        self._muted = on
        self.calls.append(("set_muted", on))

    def set_deafened(self, on: bool) -> None:
        on = bool(on)
        if on and not self._deafened:
            self._muted_before_deaf = self._muted
            self._muted = True
        elif not on and self._deafened:
            self._muted = self._muted_before_deaf
        self._deafened = on
        self.calls.append(("set_deafened", on))

    def set_mic_gain(self, v: float) -> None:
        self.settings.mic_gain = _clamp(v, 0, 2)
        self._dirty()

    def set_master(self, v: float) -> None:
        self.settings.master = _clamp(v, 0, 2)
        self._dirty()

    def set_gate_db(self, v: float) -> None:
        self.settings.gate_db = _clamp(v, -70, -20)
        self._dirty()

    def set_monitor(self, on: bool, timeout_s: float | None = 15, processed: bool = True) -> None:
        self._monitor_until = (time.monotonic() + (timeout_s or 1e9)) if on else None
        if on:
            self._monitor_processed = bool(processed)
        self.calls.append(("set_monitor", bool(on), timeout_s))

    def set_monitor_processed(self, processed: bool) -> None:
        self._monitor_processed = bool(processed)
        self.calls.append(("set_monitor_processed", bool(processed)))

    # ------------------------------------------------- 2.1 : voix et suppression du bruit
    def denoise_available(self) -> bool:
        return self.denoise_ok

    def set_denoise_level(self, level: str) -> None:
        if level not in DENOISE_DELAY_MS:
            level = "off"
        self.settings.denoise_level = level
        self._denoise_error = None          # un nouveau choix relance le suppresseur
        self._dirty()
        self.calls.append(("set_denoise_level", level))

    def set_voice_style(self, style: str) -> None:
        self.settings.voice_style = "clear" if style == "clear" else "natural"
        self._dirty()
        self.calls.append(("set_voice_style", self.settings.voice_style))

    def set_fx(self, *, highpass: bool | None = None, highpass_hz: int | None = None,
               limiter: bool | None = None, recv_leveler: bool | None = None) -> None:
        s = self.settings
        if highpass is not None:
            s.fx_highpass = bool(highpass)
        if highpass_hz is not None:
            s.fx_highpass_hz = 120 if int(highpass_hz) == 120 else 80
        if limiter is not None:
            s.fx_limiter = bool(limiter)
        if recv_leveler is not None:
            s.recv_leveler = bool(recv_leveler)
        self._dirty()
        self.calls.append(("set_fx", highpass, highpass_hz, limiter, recv_leveler))

    def disable_all_processing(self) -> None:
        s = self.settings
        s.denoise_level, s.voice_style = "off", "natural"
        s.fx_highpass, s.fx_limiter = False, False
        s.gate_db = -70.0
        self._denoise_error = None
        self._dirty()
        self.calls.append(("disable_all_processing",))

    def set_peer_clean(self, peer_id: str, on: bool) -> None:
        pref = self.settings.peer_prefs.setdefault(peer_id, {})
        on = bool(on)
        if on and not pref.get("clean"):
            n = sum(1 for pid in self._peers if self.settings.peer_prefs.get(pid, {}).get("clean"))
            if n >= CLEAN_MAX:
                self.calls.append(("set_peer_clean", peer_id, on, "CLEAN_LIMIT"))
                raise EngineError("CLEAN_LIMIT", "Tu nettoies déjà 3 voix : ton PC ne suivrait pas avec une "
                                                 "de plus. Retire le nettoyage d'un ami d'abord.")
        pref["clean"] = on
        self._dirty()
        self.calls.append(("set_peer_clean", peer_id, on))

    def set_buffer_mode(self, mode: str) -> None:
        if mode in ("auto", "reactive", "stable"):
            self.settings.buffer_mode = mode
            self._dirty()

    def set_peer_volume(self, peer_id: str, v: float) -> None:
        pref = self.settings.peer_prefs.setdefault(peer_id, {})
        pref["volume"] = _clamp(v, 0, 2)
        self._dirty()

    def set_peer_muted(self, peer_id: str, on: bool) -> None:
        pref = self.settings.peer_prefs.setdefault(peer_id, {})
        pref["muted"] = bool(on)
        self._dirty()

    def set_ptt(self, mode: bool) -> None:
        self.settings.ptt_mode = bool(mode)
        self._dirty()

    def ptt_down(self) -> None:
        self._ptt_active = True

    def ptt_up(self) -> None:
        self._ptt_active = False

    # -------------------------------------------------------------- audio
    def list_devices(self) -> tuple[list[str], list[str]]:
        ins, outs = list(_DEVICES_IN), list(_DEVICES_OUT)
        if self._extra_devices:
            ins.append("Casque Bluetooth (Hands-Free)")
            outs.append("Casque Bluetooth (Stereo)")
        return ins, outs

    def refresh_devices(self) -> None:
        """Asynchrone comme le vrai moteur : l'événement ``devices_changed`` arrive ~0,3 s plus tard."""
        self.calls.append(("refresh_devices",))
        self._devices_due = time.monotonic() + 0.3

    def set_devices(self, dev_in, dev_out) -> AudioInfo:
        """Bloque ``device_delay`` secondes (comme le vrai) ; ne lève jamais."""
        if self.device_delay:
            time.sleep(self.device_delay)
        self.settings.dev_in, self.settings.dev_out = dev_in, dev_out
        self._dirty()
        self.calls.append(("set_devices", dev_in, dev_out))
        if dev_in in self.fail_devices or dev_out in self.fail_devices:
            return AudioInfo(False, None, None, None, False,
                             "Impossible d'ouvrir la carte son (le périphérique est peut-être débranché "
                             "ou occupé).")
        return AudioInfo(True, dev_in or _DEVICES_IN[0], dev_out or _DEVICES_OUT[0],
                         (11.0, 14.0), False, None)

    def play_cue(self, kind: str) -> None:
        self.cues.append(kind)

    # ------------------------------------------------------------- réseau
    def add_peer_ip(self, text: str) -> None:
        if self.add_ip_delay:
            time.sleep(self.add_ip_delay)
        raw = (text or "").strip()
        host = raw.rsplit(":", 1)[0] if raw.count(":") == 1 else raw
        try:
            ipaddress.ip_address(host)
        except ValueError:
            raise EngineError("BAD_ADDRESS",
                              f"« {raw} » n'est pas une adresse valide. Exemple : 192.168.1.25")
        if raw not in self.settings.manual_peers:
            self.settings.manual_peers.append(raw)
        self._dirty()
        self.calls.append(("add_peer_ip", raw))

    def repair_firewall(self) -> None:
        """Simulé : ne touche JAMAIS au vrai pare-feu."""
        self.calls.append(("repair_firewall",))
        self._fw_due = time.monotonic() + 1.5

    # ------------------------------------------------- aides pour les tests
    def push_event(self, kind: str, message_fr: str = "", **kw) -> None:
        self.events.put(Event(kind, message_fr, **kw))

    def break_audio(self, seconds: float = 4.0) -> None:
        self._audio_broken_until = time.monotonic() + seconds
        self.events.put(Event("audio_error", "Le casque a été débranché. Branche-le à nouveau ou "
                              "choisis un autre appareil dans les réglages."))

    def add_fake_peer(self, name: str, **kw) -> str:
        pid = self._spawn(name, **kw)
        self._alone_since = None
        self.events.put(Event("peer_joined", f"{name} a rejoint", pid, name))
        return pid

    def set_peer_hears(self, pid: str, hears: bool) -> None:
        """Tests : le pair reçoit (ou non) nos paquets."""
        if pid in self._peers:
            self._peers[pid]["hears_me"] = hears

    def remove_fake_peer(self, pid: str) -> None:
        self._leave(pid)

    def fail_denoise(self, message: str | None = None) -> None:
        """Tests : le suppresseur tombe en panne (repli sans coupure + événement ``denoise_failed``)."""
        msg = message or ("La suppression du bruit s'est arrêtée : ton PC n'arrivait pas à suivre. "
                          "Ta voix passe sans filtre, sans coupure.")
        self._denoise_error = msg
        self.events.put(Event("denoise_failed", msg))


class FakeHotkeys:
    """Même API que ``hotkeys.Hotkeys`` ; la capture répond après un court délai."""

    def __init__(self, engine=None, settings=None, capture_result: str | None = "f9",
                 delay: float = 1.2):
        self.engine = engine
        self.settings = settings
        self.capture_result = capture_result
        self.delay = delay
        self.started = False
        self.apply_count = 0
        self.capturing = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.started = False

    def apply(self) -> None:
        self.apply_count += 1

    def cancel_capture(self) -> None:
        self.capturing = False
        self.cancelled = getattr(self, "cancelled", 0) + 1

    def capture_next(self) -> None:
        self.capturing = True

        def fire():
            if not self.capturing:       # annulée entre-temps
                return
            self.capturing = False
            eng = self.engine or FakeEngine.current
            if eng is not None:
                eng.events.put(Event("key_captured", code=self.capture_result))

        if self.delay <= 0:
            fire()
        else:
            t = threading.Timer(self.delay, fire)
            t.daemon = True
            t.start()

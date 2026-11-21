"""Moteur Vocal LAN : audio temps réel + réseau UDP + tampon anti-saccades. Aucune dépendance à tkinter.

PCM brut 16 bits 48 kHz, trames de 5 ms, un tampon adaptatif par participant (principes du code d'origine).
Chemin chaud (callback audio) : aucun verrou, aucune allocation notable, aucun appel bloquant.
Les threads réseau ne font qu'ajouter des trames dans des deque (atomique) ; la table des pairs est
remplacée en bloc (copy-on-write) quand quelqu'un arrive ou part.
"""
from __future__ import annotations

import logging
import math
import queue
import socket
import struct
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass

import numpy as np

import audiodev
import denoise
import dsp
import netutil
from settings import DENOISE_LEVELS, VOICE_STYLES, Settings, _clean_text

log = logging.getLogger("vocal.engine")

RATE = 48000
FRAME_MS = 5
FRAME = RATE * FRAME_MS // 1000          # 240 échantillons
# En-tête : magie, version du protocole, type, salon (réservé, zéros), id émetteur, séquence
MAGIC = b"VL"
PROTO = 2
HEADER = struct.Struct("!2sBc4s4sI")
HS = HEADER.size                          # 16
AUDIO_EXTRA = 4                           # compteur de trames réellement envoyées (mesure des pertes)
AUDIO_SIZE = HS + AUDIO_EXTRA + FRAME * 2
STAMP = struct.Struct("!d")
ROOM = bytes(4)

TIMEOUT = 5.0            # secondes sans paquet avant de considérer quelqu'un parti
MAX_BUFFER = 12          # trames max dans le tampon anti-saccades
SHRINK_AFTER = 30000 // FRAME_MS   # 30 s sans saccade -> on retente un tampon plus petit
GATE_OFF = -70.0
GATE_HOLD = 300 // FRAME_MS        # le filtre reste ouvert 300 ms après la voix
VOICE_ON = 32768 * 10 ** (-48 / 20)   # seuil « je parle » (rms) quand le filtre est désactivé
SPEAK_HOLD_ME, SPEAK_HOLD_PEER = 40, 20   # maintien de l'indicateur (trames) : évite le clignotement
METER_DECAY = 0.97                 # décroissance des vumètres par trame
LIMIT_T = 24000.0                  # au-delà, le limiteur doux compresse
MIN_BUFFER = {"auto": 2, "reactive": 1, "stable": 4}
BEACON_NAME_MAX = 32
MAX_PEERS = 32              # au-delà, les nouveaux venus sont ignorés (balises forgées, réseau public)
AUDIO_STALE = 0.5           # s sans callback : l'indicateur « parle » et les vumètres retombent à zéro
CLEAN_LIMIT = 3             # amis dont on nettoie la voix en même temps (RNNoise par ami, dans le callback)
SEND_ON = 32768 * 10 ** (-58 / 20)       # suppresseur actif : niveau (traité) qui ouvre l'envoi
SEND_VAD_IN_DB = -62.0                   # ... ou VAD vrai ET un vrai signal à l'entrée (le VAD seul n'ouvre jamais)
SEND_VAD_IN = 32768 * 10 ** (SEND_VAD_IN_DB / 20)
VAD_ON = 0.5
MON_FADE = 1.0 / 3         # bascule A/B de « Je m'entends » : fondu de 3 trames
MSG_CLEAN_UNAVAILABLE = ("Le nettoyage des voix n'est pas disponible sur cet ordinateur (fichier manquant ou "
                         "modifié).")
MSG_CLEAN_LIMIT = ("Tu nettoies déjà {n} voix, c'est le maximum. Arrête le nettoyage d'un ami pour reprendre "
                   "celui-ci.")
NET_REFRESH_S = 30.0       # énumération complète des interfaces (psutil : GIL gardé ~7-10 ms) au plus toutes les 30 s
CLEAN_GAP = 200            # trames (1 s) sans audio d'un ami : on remet son nettoyeur à zéro


class EngineError(Exception):
    """Erreur prévisible, avec un code stable (tests) et une phrase affichable telle quelle."""

    def __init__(self, code: str, message_fr: str):
        super().__init__(f"{code}: {message_fr}")
        self.code, self.message_fr = code, message_fr


@dataclass(frozen=True, slots=True)
class Event:
    kind: str
    message_fr: str = ""
    peer_id: str | None = None
    peer_name: str | None = None
    code: str | None = None


@dataclass(frozen=True, slots=True)
class AudioInfo:
    ok: bool
    input_name: str | None
    output_name: str | None
    latency_ms: tuple[float, float] | None
    degraded: bool
    error_fr: str | None


@dataclass(frozen=True, slots=True)
class PeerView:
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
    clean: bool = False         # « Nettoyer sa voix » demandé (mémorisé)
    clean_active: bool = False  # nettoyage réellement en marche (limite de 3, DLL, panne)
    noisy: bool = False         # bruit de fond élevé chez cet ami (suggestion)
    clean_reason: str | None = None   # « Nettoyer » demandé mais pas en marche : pourquoi (phrase française prête)


@dataclass(frozen=True, slots=True)
class Snapshot:
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
    audio: AudioInfo
    network: str
    seconds_alone: float
    peers: tuple[PeerView, ...]
    # --- 2.1
    denoise_level: str = "off"
    denoise_active: bool = False
    denoise_error_fr: str | None = None
    voice_style: str = "natural"
    latency_added_ms: float = 0.0      # délai ajouté par MES réglages d'émission
    mic_level_out: float = 0.0         # 0..1 après traitement (barre « après »)
    clipping: bool = False
    monitor_processed: bool = True
    fx_highpass: bool = True
    fx_highpass_hz: int = 80
    fx_limiter: bool = True
    recv_leveler: bool = True


def to_meter(level: float) -> float:
    """rms (échelle int16) -> 0..1 sur 60 dB, comme le vumètre d'origine."""
    db = 20 * math.log10(max(level, 1.0) / 32768)
    return min(max((db + 60) / 60, 0.0), 1.0)


def _clamp(v, lo, hi) -> float:
    try:
        v = float(v)
    except OverflowError:                       # entier plus grand qu'un flottant
        return hi if v > 0 else lo
    except (TypeError, ValueError):
        return lo
    return lo if v != v else min(max(v, lo), hi)


class _Peer:
    """État d'un participant. Les champs « réseau » sont écrits par le thread de réception,
    les champs « lecture » par le callback audio ; ce sont des attributs simples (atomiques)."""
    __slots__ = ("sid", "id", "addr", "name", "r_muted", "r_deaf", "seq", "sent", "playing", "starved",
                 "target", "stable", "q", "volume", "muted", "level", "speaking", "hold", "ping",
                 "last_pong", "seen", "joined", "recv", "lost", "cleaner", "leveler", "clean", "gap",
                 "clean_err")

    def __init__(self, sid: bytes, addr, name: str, min_buffer: int, now: float):
        self.sid, self.id, self.addr, self.name = sid, sid.hex(), addr, name
        self.r_muted = self.r_deaf = False
        self.seq, self.sent = -1, -1
        self.playing = self.starved = False
        self.target, self.stable = min_buffer, 0
        self.q: deque = deque(maxlen=MAX_BUFFER + 4)
        self.volume, self.muted = 1.0, False
        self.level, self.speaking, self.hold = 0.0, False, 0
        self.ping, self.last_pong = None, 0.0
        self.seen = self.joined = now
        self.recv = self.lost = 0
        self.cleaner = None            # denoise.Denoiser (« Nettoyer sa voix »), posé hors du callback
        self.leveler = None            # dsp.Leveler
        self.clean = False
        self.gap = 0                   # trames consécutives sans audio de cet ami
        self.clean_err = False


def _make_cues() -> dict[str, np.ndarray]:
    """Petits sons de notification synthétisés une fois pour toutes (float32, échelle int16)."""
    def tone(freq, dur, vol=0.22):
        n = int(RATE * dur)
        t = np.arange(n) / RATE
        fade = np.minimum(1.0, np.minimum(np.arange(n), n - np.arange(n)) / (RATE * 0.008))
        return (np.sin(2 * math.pi * freq * t) * fade * vol * 32768).astype(np.float32)

    gap = np.zeros(int(RATE * 0.02), np.float32)
    chord = tone(440, 0.45, 0.12) + tone(554, 0.45, 0.12) + tone(659, 0.45, 0.12)
    return {"join": np.concatenate([tone(523, 0.09), gap, tone(784, 0.12)]),
            "leave": np.concatenate([tone(784, 0.09), gap, tone(523, 0.12)]),
            "mute": tone(330, 0.09), "unmute": tone(660, 0.09), "test": chord}


class Engine:
    def __init__(self, settings: Settings, audio=None, bind_host: str = "0.0.0.0", broadcast: bool = True,
                 port: int | None = None):
        """`audio` : backend audio (None = vraie carte son). `bind_host`/`broadcast` : pour les tests.
        `port` : port pour cette session seulement (option --port) ; `settings.port` n'est pas modifié."""
        self.settings = settings
        self.events: queue.SimpleQueue = queue.SimpleQueue()
        self._backend = audio if audio is not None else audiodev.AudioBackend()
        self._bind_host, self._broadcast = bind_host, broadcast
        self._port = int(port if port else settings.port)
        self._my_id = bytes.fromhex(settings.install_id)
        self._room = ROOM
        # état lu par le callback (attributs simples)
        self._muted = self._deaf = self._monitor = False
        self._muted_before_deaf = False
        self._state_lock = threading.Lock()   # mute/sourdine (jamais pris par le callback audio)
        self._monitor_until: float | None = None
        self._ptt_mode = bool(settings.ptt_mode)
        self._ptt_active = False
        self._online = False
        self._min_buffer = MIN_BUFFER.get(settings.buffer_mode, 2)
        self._gain = np.float32(settings.mic_gain)
        self._master = np.float32(settings.master)
        self._gate_db = settings.gate_db
        self._set_gate_levels()
        # traitement de la voix (2.1) : objets créés ici / dans le thread UI, jamais dans le callback
        self._chain = dsp.VoiceChain()
        self._slot = denoise.Slot()
        self._slot.pick = lambda proc: self._pick_par(proc, 0)    # le micro préfère les trames paires
        self._dn_error_sent: str | None = None
        self._reset_req = False
        self._mon_processed = True
        self._mon_w = 1.0
        self._out_level = 0.0
        self._tx_hold = 0
        # état écrit par le callback
        self._mic_level = 0.0
        self._gate_hold = 0
        self._speaking = False
        self._sp_hold = 0
        self._tx = False
        self._twm = False
        self._seq = 0
        self._sent = 0
        self._cb_count = 0
        self._cb_errors = 0
        self._status_errors = 0
        self._send_errors = 0
        self._bad_packets = 0
        self._last_err_log = 0.0
        # tampons préalloués
        self._x = np.zeros(FRAME, np.float32)
        self._mix = np.zeros(FRAME, np.float32)
        self._tmp = np.zeros(FRAME, np.float32)
        self._raw = np.zeros(FRAME, np.float32)      # micro juste après le gain (copie « sans filtre » du moniteur)
        self._mbuf = np.zeros(FRAME, np.float32)
        self._pbuf = np.zeros(FRAME, np.float32)     # copie « Avec filtre » du moniteur, atténuée si l'envoi est fermé
        self._mon_tx = 1.0
        self._ramp = np.arange(1, FRAME + 1, dtype=np.float32) / FRAME
        self._pkt = bytearray(AUDIO_SIZE)
        self._pkt_pcm = np.frombuffer(self._pkt, np.int16, count=FRAME, offset=HS + AUDIO_EXTRA)
        HEADER.pack_into(self._pkt, 0, MAGIC, PROTO, b"A", self._room, self._my_id, 0)
        self._cues = _make_cues()
        self._cue: np.ndarray | None = None
        self._cue_pos = 0
        # pairs
        self._lock = threading.Lock()       # protège seulement les modifications de la table des pairs
        self._clean_lock = threading.Lock()  # « Nettoyer sa voix » : limite de 3 et pose/retrait des nettoyeurs (jamais pris par le callback)
        self._peers: dict[bytes, _Peer] = {}
        self._targets: tuple = ()
        self._manual: set[tuple[str, int]] = set()
        self._alone_since = time.monotonic()
        self._ifaces: list = []
        self._net_ip, self._net_t = None, 0.0
        self._local_ip = bind_host if bind_host != "0.0.0.0" else "127.0.0.1"
        self._no_net = False
        self._warned_versions: set[str] = set()
        # audio
        self._audio_lock = threading.RLock()
        self._audio_info = AudioInfo(False, None, None, None, False, "Audio non démarré.")
        self._audio_wanted = False
        self._audio_announced = False
        self._cb_seen, self._cb_seen_t = 0, time.monotonic()
        self._snap_cb, self._snap_cb_t = -1, time.monotonic()   # fraîcheur du callback, vue par snapshot()
        self._next_audio_try, self._retry_delay = 0.0, 2.0
        # cycle de vie
        self._sock: socket.socket | None = None
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._started = self._closed = False
        self._recv_started = False
        self._too_many_warned = False
        for text in settings.manual_peers:
            try:
                self._manual.add(netutil.parse_address(text, self._port))
            except ValueError:
                log.info("pair manuel ignoré : %s", text)
        self._apply_chain_settings()
        self._slot.set(denoise.make(settings.denoise_level))

    # ======================================================== cycle de vie
    def start(self, online: bool = True) -> None:
        if self._started:
            return
        self._started = True
        if denoise.available():
            log.info("suppression du bruit : disponible (RNNoise vérifié), niveau %s", self.settings.denoise_level)
        else:
            log.warning("suppression du bruit : RNNoise indisponible (%s), niveau %s", denoise.unavailable_reason(),
                        self.settings.denoise_level)
        for w in self.settings.warnings:
            self._emit("settings_warning", w)
        self.settings.warnings.clear()
        # Hors ligne (assistant de premier lancement) : AUCUN socket, pour que Windows n'affiche pas son
        # message de pare-feu avant que l'assistant l'ait expliqué. Le socket s'ouvre dans go_online().
        if online:
            self._open_socket()
            self._online = True
        self._alone_since = time.monotonic()
        if self._broadcast:
            self._refresh_network()
        if online:
            self._recv_started = True
            self._start_thread(self._receiver, "vocal-recv")
        self._start_thread(self._housekeeping, "vocal-house")
        self._start_thread(self._audio_watch, "vocal-audio")
        self._audio_wanted = True
        info = self._open_audio()
        self._announce_audio(info, first=True)
        self._check_denoise()
        if online:
            self._send_beacons()

    def _start_thread(self, fn, name: str) -> None:
        t = threading.Thread(target=fn, name=name, daemon=True)
        t.start()
        self._threads.append(t)

    def _open_socket(self) -> None:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            if sys.platform == "win32":
                # liaison exclusive : sinon une 2e instance se lierait sans erreur mais ne recevrait rien
                s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            else:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.bind((self._bind_host, self._port))
        except OSError as e:
            s.close()
            if getattr(e, "winerror", None) in (10048, 10013) or e.errno in (98, 48, 10048, 10013):
                raise EngineError("PORT_USED", "Vocal LAN est peut-être déjà ouvert : regarde près de l'horloge ou "
                                  "dans la barre des tâches. Sinon, une autre application utilise la même "
                                  f"connexion réseau (numéro {self._port}).") from e
            raise EngineError("NETWORK", f"Impossible d'ouvrir le réseau ({e.strerror or e}).") from e
        s.settimeout(0.5)  # la boucle de réception vérifie régulièrement l'arrêt
        self._sock = s

    def go_online(self) -> None:
        """Ouvre le réseau (c'est ici que Windows peut demander le pare-feu) puis s'annonce.
        Lève EngineError("PORT_USED"|"NETWORK") ; on peut réessayer ensuite."""
        if self._online or self._closed:
            return
        if self._sock is None:
            self._open_socket()
        if not self._recv_started:
            self._recv_started = True
            self._start_thread(self._receiver, "vocal-recv")
        self._alone_since = time.monotonic()
        self._online = True
        self._send_beacons()

    def shutdown(self) -> None:
        """Idempotent. Prévient les autres (« au revoir »), arrête l'audio, ferme le réseau."""
        if self._closed:
            return
        self._closed = True
        self._audio_wanted = False
        if self._sock is not None and self._online:
            bye = HEADER.pack(MAGIC, PROTO, b"X", self._room, self._my_id, 0)
            for _ in range(2):
                for addr in self._destinations():
                    self._send(bye, addr)
        self._online = False
        self._stop.set()
        # le watchdog ou un changement de périphérique peut être en train de (ré)ouvrir le flux : on attend
        # la fin (au plus 3 s) puis on ferme, et _open_audio referme de lui-même si on est arrêté entre-temps
        got = self._audio_lock.acquire(timeout=3.0)
        try:
            self._backend.close()
        finally:
            if got:
                self._audio_lock.release()
        self._targets = ()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        for t in self._threads:
            if t is not threading.current_thread():
                t.join(timeout=1.5)

    # ======================================================== accesseurs / réglages
    @property
    def muted(self) -> bool:
        return self._muted

    @property
    def deafened(self) -> bool:
        return self._deaf

    def _touch(self) -> None:
        self.settings.dirty = True

    def _name(self) -> str:
        return self.settings.name or socket.gethostname()[:BEACON_NAME_MAX]

    def set_name(self, name: str) -> None:
        self.settings.name = _clean_text(name)
        self._touch()
        self._send_beacons()

    def set_muted(self, on: bool) -> None:
        """Rétablir le micro pendant la sourdine lève aussi la sourdine (comme Discord)."""
        with self._state_lock:
            self._muted = bool(on)
            if not on:
                self._deaf = False
        self._send_beacons()

    def set_deafened(self, on: bool) -> None:
        """Sourdine = son coupé ET micro coupé (comme Discord). En sortant, le micro revient comme avant."""
        on = bool(on)
        with self._state_lock:
            if on and not self._deaf:
                self._muted_before_deaf = self._muted
                self._muted = True
                self._deaf = True
            elif not on and self._deaf:
                self._deaf = False
                self._muted = self._muted_before_deaf
            else:
                return
        self._send_beacons()

    def set_mic_gain(self, v: float) -> None:
        v = _clamp(v, 0.0, 2.0)
        self.settings.mic_gain = v
        self._gain = np.float32(v)
        self._touch()

    def set_master(self, v: float) -> None:
        v = _clamp(v, 0.0, 2.0)
        self.settings.master = v
        self._master = np.float32(v)
        self._touch()

    def _set_gate_levels(self) -> None:
        on = VOICE_ON
        if self._gate_db > GATE_OFF:
            self._gate_lin = 32768 * 10 ** (self._gate_db / 20)
            on = max(on, self._gate_lin)
        else:
            self._gate_lin = 0.0
        self._voice_on, self._voice_off = on, on * 0.6

    def set_gate_db(self, v: float) -> None:
        v = _clamp(v, GATE_OFF, -20.0)
        self.settings.gate_db = v
        self._gate_db = v
        self._set_gate_levels()
        self._apply_chain_settings()
        self._touch()

    def set_monitor(self, on: bool, timeout_s: float | None = 15, processed: bool = True) -> None:
        """« Je m'entends » : rejoue le son TEL QUE LES AUTRES L'ENTENDENT (`processed`) ou le micro brut."""
        self._mon_processed = bool(processed)
        self._mon_w = 1.0 if processed else 0.0
        self._monitor_until = time.monotonic() + timeout_s if (on and timeout_s) else None
        self._monitor = bool(on)

    def set_monitor_processed(self, processed: bool) -> None:
        """Bascule A/B pendant l'écoute : Sans filtre (False) / Avec filtre (True), en fondu."""
        self._mon_processed = bool(processed)

    # ======================================================== traitement de la voix (2.1)
    def denoise_available(self) -> bool:
        return denoise.available()

    def _gate_effective_db(self) -> float:
        """Une seule porte à la fois : le filtre de bruit manuel ne sert que sans suppresseur « Normal / Fort »."""
        lvl = self.settings.denoise_level
        if lvl in ("normal", "strong") and self._slot.active_shown:
            return GATE_OFF
        return self._gate_db

    def _apply_chain_settings(self) -> None:
        s = self.settings
        self._chain.configure(style=s.voice_style, highpass=s.fx_highpass, highpass_hz=s.fx_highpass_hz,
                              limiter=s.fx_limiter, gate_db=self._gate_effective_db())

    def set_denoise_level(self, level: str) -> None:
        """Aucune / Léger / Normal / Fort. Ne lève pas : si le suppresseur est indisponible, le son passe sans
        traitement, `Snapshot.denoise_error_fr` explique pourquoi et l'événement `denoise_failed` est émis."""
        if level not in DENOISE_LEVELS:
            return
        self.settings.denoise_level = level
        self._touch()
        self._dn_error_sent = None
        self._slot.set(denoise.make(level))      # fabriqué ICI (thread UI) ; le callback l'adopte avec un fondu
        self._apply_chain_settings()
        self._check_denoise()

    def set_voice_style(self, style: str) -> None:
        if style not in VOICE_STYLES:
            return
        self.settings.voice_style = style
        self._apply_chain_settings()
        self._touch()

    def set_fx(self, *, highpass: bool | None = None, highpass_hz: int | None = None,
               limiter: bool | None = None, recv_leveler: bool | None = None) -> None:
        s = self.settings
        if highpass is not None:
            s.fx_highpass = bool(highpass)
        if highpass_hz is not None and highpass_hz in (80, 120):
            s.fx_highpass_hz = int(highpass_hz)
        if limiter is not None:
            s.fx_limiter = bool(limiter)
        if recv_leveler is not None:
            s.recv_leveler = bool(recv_leveler)
            for p in self._peers.values():
                if p.leveler is not None:
                    p.leveler.enabled = s.recv_leveler
        self._apply_chain_settings()
        self._touch()

    def disable_all_processing(self) -> None:
        """« Tout désactiver » : Aucune + Naturelle + passe-haut off + limiteur off + filtre de bruit manuel off = son
        identique à l'entrée, bit à bit (le filtre manuel, même à 12 dB, change le son : il doit l'être aussi). L'égalisation
        du volume des amis (réception) n'est pas touchée."""
        s = self.settings
        s.voice_style, s.fx_highpass, s.fx_limiter = "natural", False, False
        self._gate_db = s.gate_db = GATE_OFF
        self._set_gate_levels()
        self.set_denoise_level("off")          # applique aussi la chaîne et enregistre

    def _check_denoise(self) -> None:
        """Thread UI / entretien : annonce (une fois) une panne du suppresseur d'émission ou d'un ami."""
        err = self._slot.error
        if err and err != self._dn_error_sent:
            self._dn_error_sent = err
            self._emit("denoise_failed", err)
            self._apply_chain_settings()       # la porte manuelle reprend sa place
        with self._clean_lock:
            for p in self._peers.values():
                c = p.cleaner
                if c is not None:
                    if c.error and not p.clean_err:
                        p.clean_err = True
                        self._emit("denoise_failed", f"Le nettoyage de la voix de {p.name} s'est arrêté : "
                                   "sa voix passe sans traitement.", p.id, p.name, code="peer")
                    # libéré une fois son fondu de sortie fini (ou si l'ami se tait : plus de trame pour le finir)
                    if (c.error or not c.enabled) and (c.finished or p.gap > 40):
                        p.cleaner = None
                elif (p.clean and not p.clean_err and denoise.dll_path() is not None
                        and self._cleaners_active() < CLEAN_LIMIT):
                    # demandé (mémorisé) mais resté en pause faute de place : une place s'est libérée
                    c = denoise.make("strong")
                    if not c.error:
                        p.cleaner = c

    def set_buffer_mode(self, mode: str) -> None:
        if mode not in MIN_BUFFER:
            return
        self.settings.buffer_mode = mode
        self._min_buffer = MIN_BUFFER[mode]
        for p in self._peers.values():
            p.target = self._min_buffer
        self._touch()

    def set_peer_volume(self, peer_id: str, v: float) -> None:
        v = _clamp(v, 0.0, 2.0)
        p = self._peer_by_hex(peer_id)
        if p:
            p.volume = v
        self._pref(peer_id, p)["volume"] = v
        self._touch()

    def set_peer_muted(self, peer_id: str, on: bool) -> None:
        p = self._peer_by_hex(peer_id)
        if p:
            p.muted = bool(on)
        self._pref(peer_id, p)["muted"] = bool(on)
        self._touch()

    def _cleaners_active(self, skip: _Peer | None = None) -> int:
        """Nettoyeurs qui comptent pour la limite : en marche (ni arrêtés par l'utilisateur, ni en panne)."""
        return sum(1 for q in self._peers.values() if q is not skip and q.cleaner is not None
                   and q.cleaner.enabled and q.cleaner.error is None)

    def set_peer_clean(self, peer_id: str, on: bool) -> None:
        """« Nettoyer sa voix » (RNNoise sur CET ami, +25 ms pour lui seul). 3 amis au maximum en même temps :
        au-delà, lève EngineError("CLEAN_LIMIT", message_fr). Mémorisé par ami."""
        on = bool(on)
        p = self._peer_by_hex(peer_id)
        with self._clean_lock:
            c0 = p.cleaner if p is not None else None
            if on and p is not None and (c0 is None or c0.error is not None or not c0.enabled):
                if self._cleaners_active(skip=p) >= CLEAN_LIMIT:
                    raise EngineError("CLEAN_LIMIT", f"Tu peux nettoyer la voix de {CLEAN_LIMIT} amis au maximum en "
                                      "même temps. Arrête le nettoyage de l'un d'eux avant d'en ajouter un autre.")
                if c0 is not None and c0.error is None:
                    # arrêté par l'utilisateur et pas encore libéré : on le relance (en repartant d'un flux vide si son
                    # fondu de sortie est fini, sinon il rendrait 25 ms de vieux son)
                    if c0.finished:
                        c0.reset()
                        c0.par = None
                    c0.enabled = True
                else:
                    c = denoise.make("strong")    # fabriqué ici (jamais dans le callback) ; ~20 ms avec l'amorçage
                    if c.error:
                        raise EngineError("DENOISE_UNAVAILABLE", c.error)
                    p.clean_err = False
                    p.cleaner = c
            elif not on and c0 is not None:
                c0.enabled = False                # fondu de 20 ms vers le sec ; libéré ensuite par _check_denoise
            if p is not None:
                p.clean = on
        self._pref(peer_id, p)["clean"] = on
        self._touch()

    def _pref(self, peer_id: str, p: _Peer | None) -> dict:
        d = self.settings.peer_prefs.setdefault(peer_id, {"volume": 1.0, "muted": False, "clean": False, "name": ""})
        if p:
            d["name"] = p.name
        return d

    def _peer_by_hex(self, peer_id: str) -> _Peer | None:
        try:
            return self._peers.get(bytes.fromhex(peer_id))
        except ValueError:
            return None

    def set_ptt(self, mode: bool) -> None:
        self._ptt_mode = bool(mode)
        if not mode:
            self._ptt_active = False
        self.settings.ptt_mode = bool(mode)
        self._touch()

    def ptt_down(self) -> None:
        self._ptt_active = True

    def ptt_up(self) -> None:
        self._ptt_active = False

    def play_cue(self, kind: str) -> None:
        cue = self._cues.get(kind)
        if cue is not None:
            self._cue_pos = 0
            self._cue = cue

    # ======================================================== périphériques audio
    def list_devices(self) -> tuple[list[str], list[str]]:
        """Ne bloque jamais : le backend renvoie sa dernière liste connue si une ouverture est en cours."""
        return self._backend.list_devices()

    def refresh_devices(self) -> None:
        """Relit la liste des périphériques et rouvre le flux (sur un thread : ~0,3 s)."""
        def work():
            with self._audio_lock:
                info = self._open_audio(reinit=True)
            self._announce_audio(info)
            self._emit("devices_changed", self._backend.notice or "La liste des périphériques a été actualisée.")
        threading.Thread(target=work, name="vocal-refresh", daemon=True).start()

    def set_devices(self, dev_in: str | None, dev_out: str | None) -> AudioInfo:
        """Change de périphériques. Ne lève pas. En cas d'échec : les anciens réglages sont remis, l'ancien flux
        est rouvert et l'AudioInfo d'erreur (error_fr) est renvoyé ; le mauvais nom n'est pas gardé."""
        with self._audio_lock:  # le watchdog ne voit jamais le flux fermé pendant la bascule
            old = (self.settings.dev_in, self.settings.dev_out)
            self.settings.dev_in, self.settings.dev_out = dev_in, dev_out
            info = self._open_audio()
            if info.ok and self._backend.notice:  # périphérique introuvable : le backend est retombé sur le défaut
                info = AudioInfo(False, info.input_name, info.output_name, info.latency_ms, info.degraded,
                                 self._backend.notice)
            if info.ok:
                self._touch()
                self._announce_audio(info)
                return info
            log.warning("changement de périphérique impossible (%s) : retour à %s", info.error_fr, old)
            self.settings.dev_in, self.settings.dev_out = old
            back = self._open_audio()
            if not back.ok:  # l'ancien ne répond plus non plus : le watchdog réessaiera
                self._announce_audio(back)
            elif self._backend.notice:
                self._emit("devices_changed", self._backend.notice)
            return info

    def _open_audio(self, reinit: bool = False) -> AudioInfo:
        with self._audio_lock:
            if reinit:
                self._backend.reinit()
            else:
                self._backend.close()
            info = self._backend.open(self.settings.dev_in, self.settings.dev_out, self._callback, FRAME)
            if self._closed or not self._audio_wanted:  # arrêt demandé pendant l'ouverture : on ne laisse rien ouvert
                self._backend.close()
                info = AudioInfo(False, None, None, None, False, "Audio arrêté.")
            self._audio_info = info
            self._reset_req = True      # nouveau flux : le callback remet la chaîne et le suppresseur à zéro
            self._cb_seen, self._cb_seen_t = self._cb_count, time.monotonic()
            return info

    def _announce_audio(self, info: AudioInfo, first: bool = False) -> None:
        if info.ok:
            if self._backend.notice:
                self._emit("devices_changed", self._backend.notice)
            elif not first and self._audio_announced:
                self._emit("audio_recovered", "Le son est rétabli.")
            self._audio_announced = False
        else:
            self._emit("audio_error", info.error_fr or "Problème audio.")
            self._audio_announced = True

    def _watchdog(self, now: float) -> None:
        """Surveille le flux audio : reconnexion automatique si le casque est débranché, etc."""
        if not self._audio_wanted or now < self._next_audio_try:
            return
        # Tout se décide sous le verrou audio : pendant un changement volontaire de périphérique (set_devices,
        # Actualiser), on ne regarde rien (pas de fausse alerte) et on ne rouvre jamais après shutdown.
        if not self._audio_lock.acquire(blocking=False):
            return
        try:
            if not self._audio_wanted or self._closed:
                return
            info = self._audio_info
            if info.ok:
                if not self._backend.realtime:
                    return
                if self._cb_count != self._cb_seen:
                    self._cb_seen, self._cb_seen_t = self._cb_count, now
                if self._backend.healthy() and now - self._cb_seen_t < 1.5:
                    return
                self._emit("audio_error", "Ton casque ne répond plus (débranché ?). Reconnexion…")
                self._audio_announced = True
            new = self._open_audio(reinit=True)
            if not self._audio_wanted:
                return
            if new.ok:
                self._retry_delay = 2.0
                self._emit("audio_recovered", f"Le son est rétabli ({new.input_name}).")
                if self._backend.notice:
                    self._emit("devices_changed", self._backend.notice)
                self._audio_announced = False
            else:
                self._retry_delay = min(self._retry_delay * 1.6, 10.0)
                if not self._audio_announced:
                    self._emit("audio_error", new.error_fr or "Problème audio.")
                    self._audio_announced = True
            self._next_audio_try = time.monotonic() + self._retry_delay
        finally:
            self._audio_lock.release()

    # ======================================================== callback audio (chemin chaud)
    def _callback(self, indata, outdata, frames, t, status):
        try:
            self._cb_count += 1
            if status:
                self._status_errors += 1
            if frames != FRAME:  # le pilote n'a pas respecté le blocksize
                outdata[:] = 0
                self._cb_errors += 1
                return
            x = self._x
            np.multiply(indata[:, 0], self._gain, out=x)
            lvl_in = math.sqrt(float(np.dot(x, x)) / FRAME)              # « avant » : micro après le gain
            ml = self._mic_level * METER_DECAY
            self._mic_level = lvl_in if lvl_in > ml else ml
            mon = self._monitor
            if mon:
                np.copyto(self._raw, x)                                  # copie « Sans filtre », non retardée

            # chaîne d'émission : passe-haut -> suppresseur de bruit -> porte, volume, voix, limiteur
            chain = self._chain
            slot = self._slot
            slot.tick = self._cb_count
            if self._reset_req:                                          # périphérique rouvert : états remis à zéro
                self._reset_req = False
                chain.reset()
                self._flush_slot()
            chain.process_pre(x)
            ep = float(np.dot(x, x))
            if not ep < 1e30:                                            # NaN / inf : un état de filtre empoisonné
                self._poisoned(x)                                        # le resterait pour toujours
            slot.process(x)
            chain.process_post(x)
            lvl = math.sqrt(float(np.dot(x, x)) / FRAME)                 # « après » : ce que les autres entendent
            if not lvl < 1e5:
                self._poisoned(x)
                lvl = 0.0
            mo = self._out_level * METER_DECAY
            self._out_level = lvl if lvl > mo else mo

            # « je parle » avec hystérésis (sur le son traité : un bruit supprimé n'allume plus le voyant)
            if lvl >= (self._voice_off if self._speaking else self._voice_on):
                self._sp_hold = SPEAK_HOLD_ME
            elif self._sp_hold > 0:
                self._sp_hold -= 1
            self._speaking = sp = self._sp_hold > 0
            muted = self._muted
            self._twm = muted and sp
            if muted:
                tx = False
            elif self._ptt_mode:
                tx = self._ptt_active
            elif slot.cur is not None and slot.cur.error is None and slot.cur.level != "light":
                # suppresseur Normal / Fort : on envoie tant que le son traité a du niveau, ou que le VAD dit « voix »
                # avec un vrai signal à l'entrée (le VAD n'ouvre jamais seul) ; on ne ferme qu'après 300 ms de calme
                v = slot.cur.vad
                if lvl >= SEND_ON or (v is not None and v >= VAD_ON and lvl_in >= SEND_VAD_IN):
                    self._tx_hold = GATE_HOLD
                elif self._tx_hold > 0:
                    self._tx_hold -= 1
                tx = self._tx_hold > 0
            else:
                tx = chain.gate_open           # micro toujours ouvert (2.0), sauf filtre de bruit manuel
            self._tx = tx

            # mixage des autres
            mix = self._mix
            if mon:
                # « Avec filtre » = ce que les autres entendent : si l'envoi est fermé (porte, appuyer-pour-parler relâché,
                # micro coupé), il est silencieux, en fondu de 3 trames
                px = x
                t0 = self._mon_tx
                tt = 1.0 if tx else 0.0
                dt_ = tt - t0
                t1 = t0 + MON_FADE if dt_ > MON_FADE else (t0 - MON_FADE if dt_ < -MON_FADE else tt)
                self._mon_tx = t1
                if t0 != 1.0 or t1 != 1.0:
                    px = self._pbuf
                    np.multiply(self._ramp, np.float32(t1 - t0), out=px)
                    px += np.float32(t0)
                    px *= x
                w0 = self._mon_w
                tw = 1.0 if self._mon_processed else 0.0
                dw = tw - w0
                w1 = w0 + MON_FADE if dw > MON_FADE else (w0 - MON_FADE if dw < -MON_FADE else tw)
                self._mon_w = w1
                if w0 == w1:
                    np.copyto(mix, px if w1 >= 1.0 else self._raw)
                else:                                                    # fondu entre les deux copies
                    mb = self._mbuf
                    np.multiply(self._ramp, np.float32(w1 - w0), out=mb)
                    mb += np.float32(w0)
                    np.subtract(px, self._raw, out=mix)
                    mix *= mb
                    mix += self._raw
            else:
                mix.fill(0)
                self._mon_tx = 1.0 if tx else 0.0
            tmp = self._tmp
            deaf = self._deaf
            for p in self._peers.values():  # table remplacée en bloc, jamais modifiée sur place
                f = self._next_frame(p)
                if f is None:
                    p.gap += 1
                    p.level *= METER_DECAY
                    if p.hold > 0:
                        p.hold -= 1
                        p.speaking = p.hold > 0
                    continue
                np.copyto(tmp, f, casting="unsafe")
                cl = p.cleaner
                if cl is not None:                 # « Nettoyer sa voix » : seulement sur de vraies trames
                    if p.gap > CLEAN_GAP:
                        cl.wake()
                        cl.par = None
                    if cl.par is None:             # flux neuf : phase de calcul décalée des autres instances RNNoise
                        cl.sync(self._cb_count, self._pick_par(cl, 1))
                    cl.process(tmp)
                p.gap = 0
                pl = math.sqrt(float(np.dot(tmp, tmp)) / FRAME)
                pd = p.level * METER_DECAY
                p.level = pl if pl > pd else pd
                if pl >= (VOICE_ON * 0.6 if p.speaking else VOICE_ON):
                    p.hold = SPEAK_HOLD_PEER
                elif p.hold > 0:
                    p.hold -= 1
                p.speaking = p.hold > 0
                lv = p.leveler
                if lv is not None:
                    lv.process(tmp)                # égalise le volume de l'ami (borné +6 / -12 dB)
                if not p.muted and not deaf:  # sourdine : les pairs sont tus, « Je m'entends » reste audible
                    tmp *= np.float32(p.volume)
                    mix += tmp
            cue = self._cue
            if cue is not None:
                pos = self._cue_pos
                seg = cue[pos:pos + FRAME]
                mix[:len(seg)] += seg
                self._cue_pos = pos + FRAME
                if pos + FRAME >= len(cue):
                    self._cue = None
            mix *= self._master
            a = np.abs(mix, out=tmp)
            if float(a.max()) > LIMIT_T:  # limiteur doux : compression progressive au lieu d'un écrêtage sec
                over = a > LIMIT_T
                span = 32767.0 - LIMIT_T
                mix[over] = np.sign(mix[over]) * (LIMIT_T + span * np.tanh((a[over] - LIMIT_T) / span))
            outdata[:, 0] = mix

            # envoi (hors de tout verrou)
            if tx and self._online:
                targets = self._targets
                if targets:
                    np.clip(x, -32768, 32767, out=x)
                    self._pkt_pcm[:] = x
                    struct.pack_into("!II", self._pkt, 12, self._seq, self._sent)
                    self._sent = (self._sent + 1) & 0xFFFFFFFF
                    sock = self._sock
                    for addr in targets:
                        try:
                            sock.sendto(self._pkt, addr)
                        except OSError:
                            self._send_errors += 1
            self._seq = (self._seq + 1) & 0xFFFFFFFF  # compte aussi les trames non envoyées
        except Exception:
            # jamais d'exception vers PortAudio : elle arrêterait le flux
            self._cb_errors += 1
            try:
                outdata[:] = 0
            except Exception:
                pass
            now = time.monotonic()
            if now - self._last_err_log > 5:
                self._last_err_log = now
                log.exception("erreur dans le callback audio")

    def _flush_slot(self) -> None:
        """Nouveau flux : vide le suppresseur d'émission et recale sa phase de calcul (sinon elle repasse à 0)."""
        c = self._slot.cur
        if c is not None:
            c.flush()
            if getattr(c, "par", None) is not None:
                c.sync(self._cb_count, c.par)

    def _pick_par(self, proc, prefer: int = 1) -> int:
        """Parité des trames (paires/impaires) sur lesquelles `proc` fait son calcul RNNoise : la moins chargée, pour ne
        jamais empiler plus de calculs que nécessaire dans un même callback (4 instances au plus : 2 + 2)."""
        n = [0, 0]
        s = self._slot.cur
        if s is not None and s is not proc and getattr(s, "par", None) is not None:
            n[s.par] += 1
        for q in self._peers.values():
            c = q.cleaner
            if c is not None and c is not proc and c.par is not None and not c.finished:
                n[c.par] += 1
        if n[0] == n[1]:
            return prefer
        return 0 if n[0] < n[1] else 1

    def _poisoned(self, x) -> None:
        """Valeur non finie dans la chaîne (ne devrait jamais arriver) : on remet tout à zéro et on envoie du silence
        pour cette trame plutôt que de laisser un état NaN rendre la voix muette ou bruyante pour toute la session."""
        x.fill(0)
        self._chain.reset()
        self._flush_slot()
        self._cb_errors += 1
        now = time.monotonic()
        if now - self._last_err_log > 5:
            self._last_err_log = now
            log.error("valeur non finie dans la chaîne de traitement de la voix : remise à zéro")

    def _next_frame(self, p: _Peer):
        q = p.q
        if not p.playing:
            if len(q) < p.target:
                return None
            p.playing = True
        if not q:
            p.playing = False  # sous-alimenté : on re-remplit le tampon
            p.starved = True
            return None
        while len(q) > p.target + 3:  # retard accumulé : on rattrape
            q.popleft()
        p.stable += 1
        if p.stable >= SHRINK_AFTER:
            p.stable = 0
            p.target = max(p.target - 1, self._min_buffer)
        return q.popleft()

    # ======================================================== réseau
    def _send(self, pkt, addr) -> None:
        try:
            self._sock.sendto(pkt, addr)
        except (OSError, AttributeError):
            self._send_errors += 1

    def _beacon(self) -> bytes:
        flags = (1 if self._muted else 0) | (2 if self._deaf else 0)
        name = self._name()[:BEACON_NAME_MAX].encode("utf-8")  # tronqué en caractères, jamais au milieu d'un accent
        return HEADER.pack(MAGIC, PROTO, b"B", self._room, self._my_id, 0) + bytes([flags]) + name

    def _destinations(self) -> set:
        """Où envoyer les balises : broadcast dirigé de chaque interface, pairs manuels, pairs connus."""
        dest = set(self._manual)
        dest.update(p.addr for p in self._peers.values())
        if self._broadcast:
            bcs = {bc for _, _, bc in self._ifaces if bc}
            dest.update((bc, self._port) for bc in (bcs or {"255.255.255.255"}))
        return dest

    def _send_beacons(self) -> None:
        if not self._online or self._sock is None:
            return
        beacon = self._beacon()
        for addr in self._destinations():
            self._send(beacon, addr)

    def _receiver(self) -> None:
        while not self._stop.is_set():
            try:
                data, addr = self._sock.recvfrom(2048)
            except socket.timeout:
                continue
            except ConnectionResetError:  # Windows : ICMP « port injoignable » d'un pair parti
                continue
            except OSError:
                if self._stop.is_set():
                    return
                log.exception("lecture réseau")
                time.sleep(0.1)
                continue
            try:
                self._on_packet(data, addr)
            except Exception:
                self._bad_packets += 1
                log.exception("paquet mal formé ignoré")

    def _on_packet(self, data: bytes, addr) -> None:
        n = len(data)
        if n < HS:
            return
        magic, ver, kind, room, sid, seq = HEADER.unpack_from(data)
        if magic != MAGIC:
            return
        if ver != PROTO:
            if addr[0] not in self._warned_versions:
                self._warned_versions.add(addr[0])
                self._emit("network_warning", f"L'ordinateur {addr[0]} utilise une autre version de Vocal LAN : "
                           "il faut installer la même version partout.")
            return
        if room != self._room or sid == self._my_id or not self._online:
            return
        p = self._peers.get(sid)
        if kind == b"A":  # chemin le plus fréquent
            if p is None or n != AUDIO_SIZE:
                return
            now = time.monotonic()
            p.seen = now
            p.addr = addr
            if seq <= p.seq and p.seq - seq < 64:
                return  # en retard ou dupliqué : on le jette
            (sent,) = struct.unpack_from("!I", data, HS)
            if p.sent >= 0:
                d = (sent - p.sent) & 0xFFFFFFFF
                if 1 < d < 1000:
                    p.lost += d - 1
            p.recv += 1
            if p.recv + p.lost > 2000:  # fenêtre glissante approximative (~10 s de parole)
                p.recv >>= 1
                p.lost >>= 1
            if p.starved:
                p.starved = False
                # la trame attendue est arrivée en retard (ni perdue ni silence) : tampon trop court
                if seq == p.seq + 1:
                    p.target = min(p.target + 1, MAX_BUFFER)
                    p.stable = 0
            p.seq, p.sent = seq, sent
            p.q.append(np.frombuffer(data, np.int16, count=FRAME, offset=HS + AUDIO_EXTRA))
            return
        if kind == b"P":
            if n != HS + STAMP.size:
                return
            self._send(HEADER.pack(MAGIC, PROTO, b"Q", self._room, self._my_id, 0) + data[HS:], addr)
            if p:
                p.seen = time.monotonic()
        elif kind == b"Q":
            if p is None or n != HS + STAMP.size:
                return
            rtt = (time.perf_counter() - STAMP.unpack_from(data, HS)[0]) * 1000
            now = time.monotonic()
            p.seen = p.last_pong = now
            if 0 <= rtt < 5000:
                p.ping = rtt if p.ping is None else p.ping * 0.7 + rtt * 0.3
        elif kind == b"X":
            if p:
                self._remove_peer(sid)
        elif kind == b"B":
            if n < HS + 1 or n > HS + 1 + 4 * BEACON_NAME_MAX + 8:
                return
            flags = data[HS]
            name = _clean_text(data[HS + 1:].decode("utf-8", "replace"), BEACON_NAME_MAX) or addr[0]
            if p is None:
                if len(self._peers) >= MAX_PEERS:  # table pleine : on ignore les nouveaux (anti-inondation)
                    if not self._too_many_warned:
                        self._too_many_warned = True
                        log.warning("plus de %d participants : les suivants sont ignorés", MAX_PEERS)
                        self._emit("network_warning", f"Plus de {MAX_PEERS} participants : les suivants sont ignorés.")
                    return
                p = self._add_peer(sid, addr, name)
                self._send(self._beacon(), addr)  # qu'il nous découvre tout de suite aussi
                self._send_ping(p)
            p.seen = time.monotonic()
            p.addr = addr
            p.r_muted, p.r_deaf = bool(flags & 1), bool(flags & 2)
            if p.name != name:
                p.name = name
                pref = self.settings.peer_prefs.get(p.id)
                if pref is not None and pref.get("name") != name:
                    pref["name"] = name
                    self._touch()

    def _add_peer(self, sid: bytes, addr, name: str) -> _Peer:
        p = _Peer(sid, addr, name, self._min_buffer, time.monotonic())
        pref = self.settings.peer_prefs.get(p.id)
        if pref is None and name in self.settings.legacy_volumes:  # migration depuis l'ancienne version
            pref = self.settings.peer_prefs[p.id] = {"volume": min(max(self.settings.legacy_volumes.pop(name), 0.0), 2.0),
                                                     "muted": False, "name": name}
            self._touch()
        if pref:
            p.volume, p.muted = float(pref.get("volume", 1.0)), bool(pref.get("muted", False))
            p.clean = bool(pref.get("clean", False))
        p.leveler = dsp.Leveler()
        p.leveler.enabled = self.settings.recv_leveler
        why = None
        if p.clean:
            if not denoise.available():
                why = MSG_CLEAN_UNAVAILABLE
            else:
                with self._clean_lock:
                    if self._cleaners_active() < CLEAN_LIMIT:
                        c = denoise.make("strong")
                        if not c.error:
                            p.cleaner = c
                        else:
                            why = MSG_CLEAN_UNAVAILABLE
                    else:
                        why = MSG_CLEAN_LIMIT.format(n=CLEAN_LIMIT)
        with self._lock:
            peers = dict(self._peers)
            peers[sid] = p
            self._peers = peers
            self._targets = tuple(q.addr for q in peers.values())
        self._emit("peer_joined", f"{name} est connecté.", p.id, name)
        if why:        # « Nettoyer » mémorisé mais impossible : on le dit (la carte de l'ami affiche « en pause »)
            self._emit("denoise_failed", f"Le nettoyage de la voix de {name} est en pause. {why}", p.id, name, code="peer")
        return p

    def _remove_peer(self, sid: bytes) -> None:
        with self._lock:
            p = self._peers.get(sid)
            if p is None:
                return
            peers = dict(self._peers)
            del peers[sid]
            self._peers = peers
            self._targets = tuple(q.addr for q in peers.values())
            if not peers:
                self._alone_since = time.monotonic()
        self._emit("peer_left", f"{p.name} est parti.", p.id, p.name)

    def _send_ping(self, p: _Peer) -> None:
        self._send(HEADER.pack(MAGIC, PROTO, b"P", self._room, self._my_id, 0) + STAMP.pack(time.perf_counter()), p.addr)

    def _refresh_network(self, force: bool = True) -> None:
        """Liste des interfaces (psutil). Cette énumération garde le GIL ~7-10 ms (mesuré) : toutes les 5 s elle retardait
        un callback audio de 7-10 ms. On ne la refait donc que si la route par défaut a changé (test à 0,1 ms) ou toutes
        les 30 s (`force` : démarrage)."""
        now = time.monotonic()
        d = netutil.default_ip()
        if not force and d == self._net_ip and now - self._net_t < NET_REFRESH_S:
            return
        self._net_ip, self._net_t = d, now
        self._ifaces = netutil.get_interfaces()
        if self._ifaces:
            ips = [ip for _, ip, _ in self._ifaces]
            self._local_ip = d if d in ips else ips[0]

    def _housekeeping(self) -> None:
        tick = 0
        while not self._stop.wait(0.25):
            tick += 1
            try:
                now = time.monotonic()
                if self._monitor and self._monitor_until and now >= self._monitor_until:
                    self._monitor, self._monitor_until = False, None
                self._check_denoise()
                if tick % 4 == 0:
                    self._every_second(now, tick)
            except Exception:
                log.exception("tâche périodique")

    def _audio_watch(self) -> None:
        """Thread à part : rouvrir l'audio peut bloquer plusieurs secondes (casque USB débranché) ; pendant ce
        temps les balises et les pings doivent continuer, sinon les autres nous croient partis (délai 5 s)."""
        while not self._stop.wait(0.25):
            try:
                self._watchdog(time.monotonic())
            except Exception:
                log.exception("surveillance audio")

    def _every_second(self, now: float, tick: int) -> None:
        if self._broadcast and tick % 20 == 0:
            self._refresh_network(force=False)
        if self._online:
            self._send_beacons()
            for sid, p in list(self._peers.items()):
                if now - p.seen > TIMEOUT:
                    self._remove_peer(sid)
                else:
                    self._send_ping(p)
        # état du réseau pour l'aide au diagnostic
        no_net = self._broadcast and not self._ifaces
        if no_net and not self._no_net:
            self._emit("network_warning", "Aucun réseau détecté : branche-toi au même réseau que tes amis "
                       "(Wi-Fi ou câble).")
        if self._no_net and not no_net:  # le réseau revient : le compteur « personne depuis X s » repart de zéro
            self._alone_since = now
        self._no_net = no_net

    # ======================================================== adresses manuelles, pare-feu
    def add_peer_ip(self, text: str) -> None:
        try:
            ip, port = netutil.parse_address(text, self._port)
        except ValueError as e:
            raise EngineError("BAD_ADDRESS", str(e)) from None
        own = {ip_ for _, ip_, _ in self._ifaces} | {self._local_ip}
        if port == self._port and ip in own:
            raise EngineError("BAD_ADDRESS", "C'est ta propre adresse : tape celle d'un autre ordinateur.")
        self._manual.add((ip, port))
        entry = ip if port == self._port else f"{ip}:{port}"
        if entry not in self.settings.manual_peers:
            self.settings.manual_peers.append(entry)
            self._touch()
        if self._online and self._sock is not None:
            self._send(self._beacon(), (ip, port))

    def remove_peer_ip(self, text: str) -> None:
        try:
            ip, port = netutil.parse_address(text, self._port)
        except ValueError:
            return
        self._manual.discard((ip, port))
        self.settings.manual_peers = [e for e in self.settings.manual_peers
                                      if e not in (ip, f"{ip}:{port}")]
        self._touch()

    def repair_firewall(self) -> None:
        def work():
            code, msg = netutil.add_firewall_rule(self._port)
            self._emit("firewall_result", msg, code=code)
        threading.Thread(target=work, name="vocal-firewall", daemon=True).start()

    # ======================================================== état
    def _emit(self, kind: str, message_fr: str = "", peer_id=None, peer_name=None, code=None) -> None:
        self.events.put(Event(kind, message_fr, peer_id, peer_name, code))

    def stats(self) -> dict:
        return {"callbacks": self._cb_count, "callback_errors": self._cb_errors,
                "status_errors": self._status_errors, "send_errors": self._send_errors,
                "bad_packets": self._bad_packets}

    def _audio_alive(self, now: float) -> bool:
        """Flux ouvert ET callback vu il y a moins de 0,5 s (jugé ici, jamais dans le callback)."""
        if not self._audio_info.ok:
            return False
        cb = self._cb_count
        if cb != self._snap_cb:
            self._snap_cb, self._snap_cb_t = cb, now
            return True
        return now - self._snap_cb_t <= AUDIO_STALE

    def snapshot(self) -> Snapshot:
        now = time.monotonic()
        alive = self._audio_alive(now)
        views = tuple(self._view(p, now, alive) for p in sorted(self._peers.values(), key=lambda q: q.name.lower()))
        gate = to_meter(self._gate_lin) if self._gate_lin else 0.0
        sending = alive and self._tx and self._online and bool(self._targets)
        return Snapshot(
            online=self._online, name=self._name(), local_ip=self._local_ip, port=self._port,
            muted=self._muted, deafened=self._deaf, monitor=self._monitor, ptt_mode=self._ptt_mode,
            ptt_active=self._ptt_active, transmitting=sending, speaking=alive and self._speaking,
            talking_while_muted=alive and self._twm, mic_level=to_meter(self._mic_level) if alive else 0.0,
            gate_level=gate, mic_gain=float(self._gain), master=float(self._master), gate_db=self._gate_db,
            buffer_mode=self.settings.buffer_mode, audio=self._audio_info,
            network="no_network" if self._no_net else ("ok" if self._peers else "alone"),
            seconds_alone=0.0 if self._peers or not self._online else now - self._alone_since, peers=views,
            denoise_level=self.settings.denoise_level, denoise_active=self._slot.active_shown,
            denoise_error_fr=self._slot.error, voice_style=self.settings.voice_style,
            latency_added_ms=(self._slot.latency_shown + self._chain.latency_samples) / 48.0,
            mic_level_out=to_meter(self._out_level) if alive else 0.0, clipping=bool(self._chain.clipping),
            monitor_processed=self._mon_processed, fx_highpass=self.settings.fx_highpass,
            fx_highpass_hz=self.settings.fx_highpass_hz, fx_limiter=self.settings.fx_limiter,
            recv_leveler=self.settings.recv_leveler)

    def _view(self, p: _Peer, now: float, alive: bool = True) -> PeerView:
        hears = (now - p.last_pong < 3.5) or (p.last_pong == 0.0 and now - p.joined < 3.0)
        total = p.recv + p.lost
        loss = 100.0 * p.lost / total if total >= 20 else 0.0
        ping = p.ping
        if not hears:
            quality = "unknown" if now - p.joined < 3.0 else "bad"
        elif ping is None:
            quality = "unknown"
        elif loss > 5 or ping > 100:
            quality = "bad"
        elif loss > 1.5 or ping > 40:
            quality = "ok"
        else:
            quality = "good"
        c = p.cleaner
        active = c is not None and c.enabled and c.error is None
        reason = None
        if p.clean and not active:
            if p.clean_err or (c is not None and c.error):
                reason = f"Le nettoyage de la voix de {p.name} s'est arrêté : sa voix passe sans traitement."
            elif denoise.dll_path() is None:
                reason = MSG_CLEAN_UNAVAILABLE
            elif self._cleaners_active() >= CLEAN_LIMIT:
                reason = MSG_CLEAN_LIMIT.format(n=CLEAN_LIMIT)
        return PeerView(id=p.id, name=p.name, ip=p.addr[0], speaking=alive and p.speaking, level=to_meter(p.level) if alive else 0.0,
                        volume=p.volume, muted=p.muted, remote_muted=p.r_muted, remote_deafened=p.r_deaf,
                        quality=quality, ping_ms=ping, loss_pct=round(loss, 1), buffer_ms=p.target * FRAME_MS,
                        hears_me=hears, clean=p.clean, clean_active=active,
                        noisy=bool(p.leveler is not None and p.leveler.noisy), clean_reason=reason)

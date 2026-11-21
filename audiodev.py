"""Périphériques audio : énumération, ouverture WASAPI avec repli, surveillance.

Le moteur ne parle qu'à un « backend » : `AudioBackend` (vraie carte son, via sounddevice)
ou `FakeAudio` (tests, aucune carte son : on appelle directement le callback).
"""
from __future__ import annotations

import logging
import sys
import threading
import time

import numpy as np

log = logging.getLogger("vocal.audio")
RATE = 48000


def _sd():
    import sounddevice as sd  # importé tard : `import engine` doit marcher sans carte son
    return sd


def _api_order(sd) -> list[dict]:
    """APIs audio dans l'ordre de préférence : WASAPI (faible latence) puis DirectSound puis MME."""
    apis = list(sd.query_hostapis())
    rank = lambda a: next((i for i, k in enumerate(("WASAPI", "DirectSound", "MME")) if k in a["name"]), 9)
    if sys.platform != "win32":
        return apis
    return sorted(apis, key=rank)


def _dedupe(names):
    seen, out = set(), []
    for n in names:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _friendly_error(exc: Exception, have_in: bool, have_out: bool) -> str:
    if not have_in:
        return "Aucun micro trouvé. Branche un micro ou un casque, puis clique sur « Actualiser » (Réglages > Audio)."
    if not have_out:
        return "Aucune sortie audio trouvée. Branche un casque ou des enceintes."
    low = str(exc).lower()
    if "in use" in low or "exclusive" in low or "busy" in low:
        return ("Une autre application (jeu, logiciel d'appel) utilise déjà ton micro ou ton casque. Ferme-la ou "
                "choisis un autre appareil dans les Réglages.")
    return "Impossible d'ouvrir la carte son (le périphérique est peut-être débranché ou occupé)."


class AudioBackend:
    """Vraie carte son. `open()` ne lève jamais : il renvoie un AudioInfo."""
    realtime = True  # le moteur surveille l'avancement du callback

    def __init__(self):
        self.stream = None
        self.notice: str | None = None  # remarque après repli (ex. « micro introuvable, défaut utilisé »)
        self._lock = threading.RLock()
        self._last_lists: tuple[list[str], list[str]] = ([], [])

    # ---------- énumération ----------
    def _devices(self, sd, api: dict | None):
        devs = sd.query_devices()
        idx = api["devices"] if api else range(len(devs))
        return [(i, devs[i]) for i in idx]

    def list_devices(self) -> tuple[list[str], list[str]]:
        """Noms lisibles (entrées, sorties) de la meilleure API disponible, sans doublons.
        Ne bloque JAMAIS : appelée par l'interface, elle rend la dernière liste connue (remplie à chaque
        ouverture) si une (ré)ouverture tient le verrou, au lieu de figer la fenêtre."""
        if not self._lock.acquire(blocking=False):
            return list(self._last_lists[0]), list(self._last_lists[1])
        try:
            return self._read_lists()
        finally:
            self._lock.release()

    def _read_lists(self) -> tuple[list[str], list[str]]:
        """Relit la liste et met le cache à jour. À appeler avec le verrou pris."""
        try:
            sd = _sd()
            api = _api_order(sd)[0] if sys.platform == "win32" else None
            devs = self._devices(sd, api)
        except Exception as e:
            log.warning("liste des périphériques : %s", e)
            return list(self._last_lists[0]), list(self._last_lists[1])
        ins = _dedupe(d["name"] for _, d in devs if d["max_input_channels"] > 0)
        outs = _dedupe(d["name"] for _, d in devs if d["max_output_channels"] > 0)
        self._last_lists = (ins, outs)
        return list(ins), list(outs)

    def reinit(self) -> None:
        """Relit les périphériques (branchement à chaud). Flux fermé obligatoire."""
        with self._lock:
            self.close()
            sd = _sd()
            try:
                sd._terminate()
                sd._initialize()
            except Exception as e:
                log.warning("réinitialisation de PortAudio : %s", e)

    # ---------- ouverture ----------
    @staticmethod
    def _pick(devs, name, kind, default_idx):
        """Index du périphérique `name` (égalité, sinon préfixe : MME tronque les noms), sinon None."""
        key = "max_input_channels" if kind == "in" else "max_output_channels"
        cands = [(i, d) for i, d in devs if d[key] > 0]
        if name is None:
            return default_idx, False
        for i, d in cands:
            if d["name"] == name:
                return i, False
        for i, d in cands:
            if d["name"].startswith(name) or name.startswith(d["name"]):
                return i, False
        return default_idx, True  # introuvable : repli sur le défaut

    def open(self, dev_in, dev_out, callback, frame: int):
        from engine import AudioInfo  # import tardif (évite la dépendance circulaire)
        with self._lock:
            self.close()
            self.notice = None
            try:
                sd = _sd()
                apis = _api_order(sd)
                all_devs = sd.query_devices()
            except Exception as e:
                return AudioInfo(False, None, None, None, False, _friendly_error(e, True, True))
            self._read_lists()  # le cache de list_devices() suit chaque ouverture
            have_in = any(d["max_input_channels"] > 0 for d in all_devs)
            have_out = any(d["max_output_channels"] > 0 for d in all_devs)
            last_exc: Exception = RuntimeError("aucun pilote audio")
            for n, api in enumerate(apis):
                di, do = api["default_input_device"], api["default_output_device"]
                if di < 0 or do < 0:
                    continue
                devs = self._devices(sd, api)
                ii, miss_in = self._pick(devs, dev_in, "in", di)
                oo, miss_out = self._pick(devs, dev_out, "out", do)
                kw = dict(samplerate=RATE, blocksize=frame, dtype="int16", channels=1,
                          latency="low", callback=callback)
                if "WASAPI" in api["name"]:
                    kw["extra_settings"] = sd.WasapiSettings(auto_convert=True)
                try:
                    stream = sd.Stream(device=(ii, oo), **kw)
                    stream.start()
                except Exception as e:
                    last_exc = e
                    log.warning("ouverture %s impossible : %s", api["name"], e)
                    continue
                self.stream = stream
                degraded = "WASAPI" not in api["name"]
                notes = []
                if miss_in:
                    notes.append(f"Le micro « {dev_in} » est introuvable : le micro par défaut est utilisé.")
                if miss_out:
                    notes.append(f"La sortie « {dev_out} » est introuvable : la sortie par défaut est utilisée.")
                self.notice = " ".join(notes) or None
                lat = stream.latency
                lat_ms = (float(lat[0]) * 1000, float(lat[1]) * 1000) if isinstance(lat, (tuple, list)) else None
                return AudioInfo(True, sd.query_devices(ii)["name"], sd.query_devices(oo)["name"], lat_ms,
                                 degraded, None)
            return AudioInfo(False, None, None, None, False, _friendly_error(last_exc, have_in, have_out))

    def close(self) -> None:
        with self._lock:
            s, self.stream = self.stream, None
            if s is not None:
                try:
                    s.stop()
                except Exception:
                    pass
                try:
                    s.close()
                except Exception:
                    pass

    def healthy(self) -> bool:
        s = self.stream
        try:
            return s is not None and bool(s.active)
        except Exception:
            return False


class FakeAudio:
    """Backend sans carte son pour les tests : `tick()` appelle le callback du moteur à la main."""
    realtime = False

    def __init__(self, ins=("Micro test",), outs=("Sortie test",)):
        self.ins, self.outs = list(ins), list(outs)
        self.callback = None
        self.frame = 240
        self.notice = None
        self.fail_open = False
        self.opened = 0

    def list_devices(self):
        return list(self.ins), list(self.outs)

    def reinit(self):
        pass

    def open(self, dev_in, dev_out, callback, frame):
        from engine import AudioInfo
        self.opened += 1
        if self.fail_open:
            self.callback = None
            return AudioInfo(False, None, None, None, False, "Aucun micro trouvé.")
        self.callback, self.frame = callback, frame
        return AudioInfo(True, dev_in or self.ins[0], dev_out or self.outs[0], (5.0, 5.0), False, None)

    def close(self):
        self.callback = None

    def healthy(self):
        return self.callback is not None

    def tick(self, mic=None, status=None):
        """Un bloc de `frame` échantillons : renvoie ce que le moteur envoie aux haut-parleurs (int16)."""
        indata = np.zeros((self.frame, 1), np.int16) if mic is None else np.asarray(mic, np.int16).reshape(-1, 1)
        outdata = np.zeros((self.frame, 1), np.int16)
        self.callback(indata, outdata, self.frame, None, status)
        return outdata[:, 0].copy()

    def run(self, seconds, mic=None, realtime=True, mic_fn=None):
        """Fait tourner `seconds` secondes de blocs de 5 ms ; renvoie la sortie concaténée.
        `mic_fn(k)` peut fournir le bloc numéro k (sinon `mic` répété ou silence)."""
        n = int(seconds * RATE / self.frame)
        outs = []
        t0 = time.perf_counter()
        for k in range(n):
            outs.append(self.tick(mic_fn(k) if mic_fn else mic))
            if realtime:
                dt = t0 + (k + 1) * self.frame / RATE - time.perf_counter()
                if dt > 0:
                    time.sleep(dt)
        return np.concatenate(outs) if outs else np.zeros(0, np.int16)

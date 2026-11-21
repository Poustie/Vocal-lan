"""Vocal LAN : chat vocal basse latence en réseau local (point d'entrée).

Usage : python vocal.py [IP ...] [--port 50555] [--minimized] [--fake]
Les IP sont optionnelles : les autres sont découverts automatiquement sur le réseau local.
Utilise un casque (pas d'annulation d'écho).
"""
import argparse
import gc
import logging
import logging.handlers
import os
import sys
import tempfile
import threading

import netutil
from settings import VERSION, Settings, app_dir

log = logging.getLogger("vocal")


def setup_logging() -> None:
    """Journal dans %APPDATA%\\Vocal LAN\\vocal.log (200 Ko, 2 sauvegardes) + exceptions non attrapées."""
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        h = logging.handlers.RotatingFileHandler(app_dir() / "vocal.log", maxBytes=200_000, backupCount=2,
                                                 encoding="utf-8")
        h.setFormatter(fmt)
        root.addHandler(h)
    except OSError:
        pass
    if sys.stderr is not None:  # pas de console dans l'exe
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)

    def hook(exc_type, exc, tb):
        log.critical("exception non gérée", exc_info=(exc_type, exc, tb))

    sys.excepthook = hook
    threading.excepthook = lambda a: log.critical("exception dans le thread %s", a.thread and a.thread.name,
                                                  exc_info=(a.exc_type, a.exc_value, a.exc_traceback))
    log.info("Vocal LAN %s démarre (Python %s)", VERSION, sys.version.split()[0])


class _ArgError(Exception):
    pass


class _QuietParser(argparse.ArgumentParser):
    """Dans l'exe sans console, sys.stderr vaut None : argparse ne doit ni écrire ni quitter."""

    def error(self, message):
        raise _ArgError(message)


def parse_args(argv=None):
    """(args, remarques). Une option inconnue ou invalide est ignorée (journalisée plus tard), jamais fatale."""
    ap = _QuietParser(description="Vocal LAN", add_help=False)
    ap.add_argument("peers", nargs="*", help="IP des autres (optionnel, découverte auto sinon)")
    ap.add_argument("--port", type=int, help="port UDP pour cette session seulement (50555 par défaut)")
    ap.add_argument("--minimized", action="store_true", help="démarre réduit (démarrage avec Windows)")
    ap.add_argument("--fake", action="store_true", help="moteur factice (démo de l'interface, sans son ni réseau)")
    ap.add_argument("--diag", action="store_true", help="écrit un diagnostic (suppression du bruit...) dans le "
                    "journal et dans diagnostic.txt puis quitte, sans toucher au son ni au réseau")
    notes = []
    try:
        args, unknown = ap.parse_known_args(argv)
        if unknown:
            notes.append(f"options ignorées : {unknown}")
    except (_ArgError, SystemExit) as e:
        notes.append(f"arguments invalides ignorés ({e})")
        args = ap.parse_known_args([])[0]
    return args, notes


def diagnostic() -> str:
    """Résumé d'état pour vérifier une installation (exe compris) : version, suppression du bruit."""
    import denoise
    ok = denoise.available()
    lib = denoise.dll_path()
    lines = [f"Vocal LAN {VERSION}",
             f"Python {sys.version.split()[0]}  exe={'oui' if getattr(sys, 'frozen', False) else 'non'}  "
             f"_MEIPASS={getattr(sys, '_MEIPASS', None)}",
             f"denoise.available() = {ok}",
             f"DLL RNNoise : {lib if ok else denoise.unavailable_reason()}"]
    if ok:
        p = denoise.make("strong")
        lines.append(f"make('strong') : erreur={p.error} retard={p.latency_samples} échantillons")
    return chr(10).join(lines)


def main(argv=None) -> int:
    args, arg_notes = parse_args(argv)

    if args.fake and not os.environ.get("VOCAL_LAN_DATA"):  # démo : réglages jetables (sauf dossier imposé)
        os.environ["VOCAL_LAN_DATA"] = tempfile.mkdtemp(prefix="vocal_lan_fake_")
    setup_logging()
    for n in arg_notes:
        log.warning("ligne de commande : %s", n)
    if args.diag:  # vérification d'une installation : ni son, ni réseau, ni fenêtre
        text = diagnostic()
        log.info("diagnostic : %s", text)
        try:
            (app_dir() / "diagnostic.txt").write_text(text + chr(10), encoding="utf-8")
        except OSError:
            pass
        return 0
    sys.setswitchinterval(0.0005)  # le callback audio ne doit jamais attendre le GIL jusqu'à 5 ms
    netutil.boost_process()
    if args.minimized:
        os.environ["VOCAL_LAN_MINIMIZED"] = "1"  # lu par l'interface

    # --fake ne prend pas l'instance unique, sauf si VOCAL_LAN_INSTANCE est posée (tests à deux processus)
    if (not args.fake or os.environ.get("VOCAL_LAN_INSTANCE")) and not netutil.acquire_single_instance():
        if not netutil.bring_existing_to_front():
            netutil.message_box("Vocal LAN", "Vocal LAN est déjà ouvert. Regarde près de l'horloge "
                                "(zone de notification) ou dans la barre des tâches.", error=False)
        return 0

    settings = Settings.load()
    # --port ne vaut que pour cette session : il va au moteur, jamais dans settings.port (donc pas sauvegardé)
    session_port = args.port if args.port and 1024 <= args.port <= 65535 else None
    if args.port and session_port is None:
        log.warning("--port %s ignoré (doit être entre 1024 et 65535)", args.port)

    if args.fake:
        from fake_engine import Event, FakeEngine
        engine = FakeEngine(settings)
        try:
            from fake_engine import FakeHotkeys
            hotkeys = FakeHotkeys(engine, settings)
        except ImportError:
            hotkeys = None
    else:
        from engine import Engine, Event
        from hotkeys import Hotkeys
        engine = Engine(settings, port=session_port)
        hotkeys = Hotkeys(engine, settings)

    try:
        engine.start(online=settings.ui.first_run_done)
    except Exception as e:
        code = getattr(e, "code", None)
        msg = getattr(e, "message_fr", None) or f"Vocal LAN n'a pas pu démarrer : {e}"
        log.error("démarrage impossible (%s): %s", code, e)
        netutil.message_box("Vocal LAN", msg)
        return 1

    for ip in args.peers:
        try:
            engine.add_peer_ip(ip)
        except Exception as e:
            log.warning("adresse ignorée %s : %s", ip, e)

    if hotkeys is not None:
        hotkeys.start()
    # une 2e instance lancée par l'utilisateur demande à revoir la fenêtre (cachée dans la zone de notification...)
    netutil.start_activation_listener(lambda: engine.events.put(Event("show_window")))
    if settings.ui.start_with_windows and not args.fake and netutil.is_autostart():  # le dossier a pu changer
        try:
            netutil.set_autostart(True)
        except OSError:
            log.warning("démarrage avec Windows : chemin non mis à jour", exc_info=True)
    # Ramasse-miettes : une collecte complète parcourt les ~40 000 objets de l'interface en ~3,5 ms en gardant le GIL, ce qui
    # retarde le callback audio d'autant. Une fois la fenêtre construite, on « gèle » les objets existants : ils sortent
    # des collectes (mesuré : 3,6 ms -> 0 ms) ; les objets créés ensuite restent collectés normalement.
    for delay in (6.0, 60.0):
        t = threading.Timer(delay, gc.freeze)
        t.daemon = True
        t.start()
    try:
        import ui
        ui.run(engine, settings, hotkeys)
    except Exception:
        log.critical("l'interface a planté", exc_info=True)
        netutil.message_box("Vocal LAN", "Vocal LAN a rencontré un problème et doit se fermer.\n"
                            f"Le détail est dans le fichier :\n{app_dir() / 'vocal.log'}")
        return 1
    finally:
        netutil.stop_activation_listener()
        try:
            if hotkeys is not None:
                hotkeys.stop()
            settings.save()
            engine.shutdown()
        except Exception:
            log.exception("arrêt")
    return 0


if __name__ == "__main__":
    rc = main()
    logging.shutdown()
    # Garde-fou : un thread non démon oublié (l'icône de la zone de notification, par exemple) laisserait un
    # processus fantôme qui garde le port et l'instance unique alors qu'aucune fenêtre n'est visible.
    os._exit(rc or 0)

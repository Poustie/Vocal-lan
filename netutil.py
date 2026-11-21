"""Utilitaires réseau et Windows : interfaces, adresses, pare-feu, instance unique, priorité, autostart."""
from __future__ import annotations

import ctypes
import ipaddress
import logging
import os
import socket
import subprocess
import sys
import threading

log = logging.getLogger("vocal.net")
IS_WIN = sys.platform == "win32"
FIREWALL_RULE = "Vocal LAN"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
# interfaces virtuelles inutiles pour une partie en LAN (machines virtuelles, conteneurs...)
_VIRTUAL = ("vmware", "virtualbox", "vbox", "vethernet", "hyper-v", "docker", "wsl", "loopback",
            "pseudo", "bluetooth")


# ---------- adresses et interfaces ----------
def default_ip() -> str:
    """IP de l'interface qui sert de route par défaut (aucun paquet n'est envoyé)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def get_interfaces() -> list[tuple[str, str, str | None]]:
    """[(nom, ip, adresse de broadcast dirigé ou None)] des interfaces IPv4 actives, hors virtuelles.
    Si le filtre ne laisse rien (réseau pontté via Hyper-V...), on garde tout sauf la boucle locale."""
    try:
        import psutil
        addrs, stats = psutil.net_if_addrs(), psutil.net_if_stats()
    except Exception as e:  # psutil absent ou erreur système
        log.warning("énumération des interfaces impossible : %s", e)
        return []
    found = []
    for name, lst in addrs.items():
        st = stats.get(name)
        if st is not None and not st.isup:
            continue
        for a in lst:
            if a.family != socket.AF_INET or not a.address:
                continue
            ip = a.address
            if ip.startswith(("127.", "169.254.", "0.")):
                continue
            bc = None
            try:
                net = ipaddress.IPv4Network(f"{ip}/{a.netmask}", strict=False)
                if net.prefixlen <= 30:
                    bc = str(net.broadcast_address)
            except ValueError:
                pass
            found.append((name, ip, bc))
    real = [f for f in found if not any(v in f[0].lower() for v in _VIRTUAL)]
    return real or found


def resolve_host(text: str, timeout: float = 2.0) -> str | None:
    """Résout un nom d'hôte en IPv4, avec délai maximal (la résolution DNS peut bloquer)."""
    out: list[str] = []

    def work():
        try:
            out.append(socket.gethostbyname(text))
        except OSError:
            pass

    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(timeout)
    return out[0] if out else None


def parse_address(text: str, default_port: int) -> tuple[str, int]:
    """'ip', 'ip:port' ou 'nom[:port]' -> (ip, port). Lève ValueError avec un message en français."""
    text = (text or "").strip()
    if not text:
        raise ValueError("Tape l'adresse IP de ton ami (par exemple 192.168.1.25).")
    host, port = text, default_port
    if text.count(":") == 1:
        host, p = text.split(":")
        try:
            port = int(p)
        except ValueError:
            raise ValueError(f"Le port « {p} » n'est pas un nombre.") from None
        if not 1024 <= port <= 65535:
            raise ValueError("Le port doit être compris entre 1024 et 65535.")
    host = host.strip()
    try:
        ip = str(ipaddress.IPv4Address(host))
    except ValueError:
        ip = resolve_host(host) if host and len(host) <= 100 else None
        if ip is None:
            raise ValueError(f"Adresse invalide : « {text} ». Exemple : 192.168.1.25") from None
    a = ipaddress.IPv4Address(ip)
    if a.is_unspecified or a.is_multicast or ip == "255.255.255.255":
        raise ValueError(f"« {ip} » n'est pas l'adresse d'un ordinateur.")
    return ip, port


# ---------- pare-feu ----------
def build_firewall_command(exe: str, port: int, profiles: str = "private,domain",
                           purge_program: bool = False) -> str:
    """Paramètres passés à cmd.exe (élevé) : supprime les anciennes règles puis ajoute la nouvelle.
    `purge_program` : supprime TOUTES les règles visant ce programme, y compris les règles « Bloquer » que
    Windows crée quand on clique sur Annuler dans sa fenêtre d'autorisation (un blocage l'emporte sur une
    autorisation). À réserver à notre propre .exe : avec python.exe on ne touche qu'à la règle « Vocal LAN »."""
    if any(c in exe for c in '&|<>^%"'):
        raise ValueError("chemin non supporté")
    base = "netsh advfirewall firewall"
    which = "all" if purge_program else f'"{FIREWALL_RULE}"'
    return (f'/c {base} delete rule name={which} program="{exe}" >nul 2>&1 & '
            f'{base} add rule name="{FIREWALL_RULE}" dir=in action=allow protocol=UDP '
            f'localport={int(port)} program="{exe}" profile={profiles} enable=yes')


def network_is_public() -> bool:
    """True si une connexion réseau est classée « Public » (Windows). Faux en cas de doute."""
    if not IS_WIN:
        return False
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                            "(Get-NetConnectionProfile).NetworkCategory"],
                           capture_output=True, text=True, timeout=8, creationflags=0x08000000)
        return "public" in r.stdout.lower()
    except Exception:
        return False


def _run_elevated(exe: str, params: str, timeout_ms: int = 60000) -> int | None:
    """Lance `exe params` avec élévation (UAC), attend la fin. Renvoie le code de sortie,
    None si l'utilisateur a refusé. Lève OSError si le lancement échoue."""
    from ctypes import wintypes

    class SEI(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("fMask", wintypes.ULONG), ("hwnd", wintypes.HWND),
                    ("lpVerb", wintypes.LPCWSTR), ("lpFile", wintypes.LPCWSTR),
                    ("lpParameters", wintypes.LPCWSTR), ("lpDirectory", wintypes.LPCWSTR),
                    ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE),
                    ("lpIDList", ctypes.c_void_p), ("lpClass", wintypes.LPCWSTR),
                    ("hkeyClass", wintypes.HKEY), ("dwHotKey", wintypes.DWORD),
                    ("hIcon", wintypes.HANDLE), ("hProcess", wintypes.HANDLE)]

    info = SEI()
    info.cbSize = ctypes.sizeof(SEI)
    info.fMask = 0x40  # SEE_MASK_NOCLOSEPROCESS
    info.lpVerb, info.lpFile, info.lpParameters, info.nShow = "runas", exe, params, 0
    if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(info)):
        if ctypes.GetLastError() == 1223:  # ERROR_CANCELLED : refus de l'UAC
            return None
        raise ctypes.WinError()
    k32 = ctypes.windll.kernel32
    k32.WaitForSingleObject(info.hProcess, timeout_ms)
    code = wintypes.DWORD(1)
    k32.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
    k32.CloseHandle(info.hProcess)
    return code.value


def add_firewall_rule(port: int) -> tuple[str, str]:
    """Ajoute la règle entrante UDP pour cet exécutable (une seule élévation UAC).
    Renvoie (code, message_fr) avec code "ok" | "cancelled" | "failed"."""
    if not IS_WIN:
        return "failed", "Le pare-feu Windows n'existe pas sur ce système."
    exe = sys.executable
    public = network_is_public()
    profiles = "private,domain,public" if public else "private,domain"
    try:
        params = build_firewall_command(exe, port, profiles, purge_program=bool(getattr(sys, "frozen", False)))
        code = _run_elevated(os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "cmd.exe"), params)
    except Exception as e:
        log.warning("règle de pare-feu : %s", e)
        return "failed", "Impossible d'ajouter l'autorisation dans le pare-feu Windows."
    if code is None:
        return "cancelled", "Tu as refusé la demande de Windows : le pare-feu n'a pas été modifié."
    if code != 0:
        return "failed", "Windows n'a pas accepté la modification du pare-feu."
    msg = "Vocal LAN est maintenant autorisé dans le pare-feu Windows."
    if public:
        msg += " Ton réseau est classé « Public » : l'autorisation a aussi été donnée pour ce type de réseau."
    return "ok", msg


# ---------- instance unique, fenêtre existante ----------
# Principe : la 1re instance tient un mutex, publie son PID (mémoire partagée nommée) et attend un
# événement nommé « activer ». La 2e instance retrouve la fenêtre principale (EnumWindows : titre qui
# commence par « Vocal LAN », classe Tk, sans propriétaire donc pas une fenêtre secondaire). Visible :
# premier plan. Cachée dans la zone de notification ou réduite : elle signale l'événement, la 1re
# instance se ré-affiche elle-même (Tk reste cohérent) et, si l'interface ne réagit pas, on force
# ShowWindow en dernier recours. VOCAL_LAN_INSTANCE ajoute un suffixe aux noms (tests uniquement).
_mutex = None
_pid_map = None
_event = None
_listener_stop = threading.Event()
_listener: threading.Thread | None = None
_SW_SHOWNORMAL, _SW_RESTORE = 1, 9


def _suffix() -> str:
    return os.environ.get("VOCAL_LAN_INSTANCE", "")


def _k32():
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateMutexW.restype = ctypes.c_void_p
    k.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    k.CreateEventW.restype = ctypes.c_void_p
    k.CreateEventW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_wchar_p]
    k.OpenEventW.restype = ctypes.c_void_p
    k.OpenEventW.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_wchar_p]
    k.SetEvent.argtypes = [ctypes.c_void_p]
    k.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    k.CloseHandle.argtypes = [ctypes.c_void_p]
    k.CreateFileMappingW.restype = ctypes.c_void_p
    k.CreateFileMappingW.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                                     ctypes.c_uint32, ctypes.c_wchar_p]
    k.OpenFileMappingW.restype = ctypes.c_void_p
    k.OpenFileMappingW.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_wchar_p]
    k.MapViewOfFile.restype = ctypes.c_void_p
    k.MapViewOfFile.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
                                ctypes.c_size_t]
    k.UnmapViewOfFile.argtypes = [ctypes.c_void_p]
    return k


def acquire_single_instance(name: str | None = None) -> bool:
    """True si on est la seule instance (le mutex reste tenu jusqu'à la fin du processus).
    Publie aussi le PID et crée l'événement d'activation pour les prochaines instances."""
    global _mutex, _pid_map, _event
    if not IS_WIN:
        return True
    k = _k32()
    sfx = _suffix()
    _mutex = k.CreateMutexW(None, False, name or ("Local\\VocalLAN_SingleInstance" + sfx))
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        return False
    try:
        _event = k.CreateEventW(None, False, False, "Local\\VocalLAN_Activate" + sfx)  # remise à zéro auto
        _pid_map = k.CreateFileMappingW(ctypes.c_void_p(-1), None, 0x04, 0, 4, "Local\\VocalLAN_Pid" + sfx)
        view = k.MapViewOfFile(_pid_map, 0x0002, 0, 0, 4) if _pid_map else None  # FILE_MAP_WRITE
        if view:
            ctypes.c_uint32.from_address(view).value = os.getpid()
            k.UnmapViewOfFile(view)
    except Exception as e:  # la réactivation sera dégradée, pas l'instance unique
        log.warning("instance unique : événement d'activation indisponible (%s)", e)
    return True


def _primary_pid() -> int | None:
    """PID de la 1re instance (mémoire partagée), None si inconnu (ancienne version)."""
    k = _k32()
    h = k.OpenFileMappingW(0x0004, False, "Local\\VocalLAN_Pid" + _suffix())  # FILE_MAP_READ
    if not h:
        return None
    try:
        view = k.MapViewOfFile(h, 0x0004, 0, 0, 4)
        if not view:
            return None
        pid = ctypes.c_uint32.from_address(view).value
        k.UnmapViewOfFile(view)
        return pid or None
    finally:
        k.CloseHandle(h)


def find_main_windows(prefix: str = "Vocal LAN", pid: int | None = None, exclude_pid: int | None = None) -> list[int]:
    """Fenêtres principales : titre « Vocal LAN » ou « Vocal LAN (3) », classe Tk, SANS propriétaire
    (les fenêtres secondaires - réglages, aide, dialogues - sont transient donc possédées)."""
    if not IS_WIN:
        return []
    from ctypes import wintypes
    u = ctypes.WinDLL("user32", use_last_error=True)
    u.GetWindow.restype = wintypes.HWND
    u.GetWindow.argtypes = [wintypes.HWND, ctypes.c_uint]
    u.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    u.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    found: list[int] = []
    proc = ctypes.WINFUNCTYPE(ctypes.c_int, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _lp):
        try:
            if u.GetWindow(hwnd, 4):  # GW_OWNER : fenêtre secondaire
                return 1
            buf = ctypes.create_unicode_buffer(256)
            u.GetClassNameW(hwnd, buf, 256)
            if not buf.value.startswith("Tk"):
                return 1
            u.GetWindowTextW(hwnd, buf, 256)
            if not (buf.value == prefix or buf.value.startswith(prefix + " (")):
                return 1
            wpid = wintypes.DWORD(0)
            u.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
            if (pid is not None and wpid.value != pid) or (exclude_pid is not None and wpid.value == exclude_pid):
                return 1
            found.append(int(hwnd))
        except Exception:
            pass
        return 1

    u.EnumWindows(proc(cb), 0)
    return found


def _is_shown(hwnd) -> bool:
    u = ctypes.windll.user32
    return bool(u.IsWindowVisible(hwnd)) and not u.IsIconic(hwnd)


def _force_show(hwnd) -> None:
    """Dernier recours : l'interface n'a pas réagi. Tk garde alors une idée fausse de l'état de sa fenêtre
    (sans conséquence visible), mais la fenêtre est bien là."""
    u = ctypes.windll.user32
    if u.IsIconic(hwnd):
        u.ShowWindow(hwnd, _SW_RESTORE)
    elif not u.IsWindowVisible(hwnd):
        u.ShowWindow(hwnd, _SW_SHOWNORMAL)


def signal_existing() -> bool:
    """Demande à la 1re instance de se ré-afficher. False si elle ne sait pas répondre."""
    if not IS_WIN:
        return False
    k = _k32()
    h = k.OpenEventW(0x0002, False, "Local\\VocalLAN_Activate" + _suffix())  # EVENT_MODIFY_STATE
    if not h:
        return False
    try:
        return bool(k.SetEvent(h))
    finally:
        k.CloseHandle(h)


def bring_existing_to_front(title: str = "Vocal LAN", wait_s: float = 2.0) -> bool:
    """Remet au premier plan la fenêtre de l'instance déjà lancée, même cachée dans la zone de notification
    ou réduite. False si aucune fenêtre n'a pu être trouvée."""
    if not IS_WIN:
        return False
    import time
    u = ctypes.windll.user32
    pid = _primary_pid()
    hwnds = find_main_windows(title, pid=pid, exclude_pid=os.getpid())
    if not hwnds:  # l'interface n'est peut-être pas encore créée : on prévient quand même et on attend
        if signal_existing():
            end = time.monotonic() + wait_s
            while not hwnds and time.monotonic() < end:
                time.sleep(0.1)
                hwnds = find_main_windows(title, pid=pid, exclude_pid=os.getpid())
        if not hwnds:
            return False
    hwnd = hwnds[0]
    owner = ctypes.c_ulong(0)
    u.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
    u.AllowSetForegroundWindow(owner.value)
    if not _is_shown(hwnd):
        signal_existing()  # la 1re instance se ré-affiche elle-même
        end = time.monotonic() + wait_s
        while not _is_shown(hwnd) and time.monotonic() < end:
            time.sleep(0.05)
        if not _is_shown(hwnd):
            _force_show(hwnd)
    u.BringWindowToTop(hwnd)
    if not u.SetForegroundWindow(hwnd):
        u.FlashWindow(hwnd, True)  # le système refuse le focus : au moins la barre des tâches clignote
    return True


def start_activation_listener(on_activate) -> None:
    """Dans la 1re instance : appelle `on_activate()` (thread dédié, ne pas toucher à Tk) quand une 2e instance
    demande à voir la fenêtre. Si la fenêtre n'est pas visible 0,8 s plus tard, elle est forcée."""
    global _listener
    if not IS_WIN or _event is None or _listener is not None:
        return
    import time
    k = _k32()
    me = os.getpid()
    ev = _event

    def run():
        while not _listener_stop.is_set():
            if k.WaitForSingleObject(ev, 500) != 0:  # WAIT_OBJECT_0
                continue
            try:
                on_activate()
                time.sleep(0.8)
                for hwnd in find_main_windows(pid=me):
                    if not _is_shown(hwnd):
                        _force_show(hwnd)
            except Exception:
                log.exception("réactivation de la fenêtre")

    _listener = threading.Thread(target=run, name="vocal-activate", daemon=True)
    _listener.start()


def stop_activation_listener() -> None:
    _listener_stop.set()


def message_box(title: str, text: str, error: bool = True) -> None:
    """Boîte de dialogue native (utile avant l'interface ou sans console)."""
    if IS_WIN:
        try:
            ctypes.windll.user32.MessageBoxW(None, text, title, 0x10 if error else 0x40)
            return
        except Exception:
            pass
    print(f"{title}: {text}", file=sys.stderr)


# ---------- priorité et horloge ----------
def boost_process() -> None:
    """Priorité au-dessus de la normale, pas de bridage énergétique (EcoQoS), horloge à 1 ms."""
    if not IS_WIN:
        return
    try:
        k32 = ctypes.windll.kernel32
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        h = ctypes.c_void_p(k32.GetCurrentProcess())
        k32.SetPriorityClass(h, 0x8000)  # ABOVE_NORMAL_PRIORITY_CLASS

        class THROTTLE(ctypes.Structure):
            _fields_ = [("Version", ctypes.c_ulong), ("ControlMask", ctypes.c_ulong), ("StateMask", ctypes.c_ulong)]

        t = THROTTLE(1, 1, 0)  # contrôle de l'exécution, état « pas de bridage »
        k32.SetProcessInformation(h, 4, ctypes.byref(t), ctypes.sizeof(t))  # ProcessPowerThrottling
        ctypes.windll.winmm.timeBeginPeriod(1)
    except Exception as e:
        log.warning("priorité du processus : %s", e)


# ---------- démarrage avec Windows ----------
def _autostart_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --minimized'
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vocal.py")
    return f'"{pyw if os.path.exists(pyw) else sys.executable}" "{script}" --minimized'


def set_autostart(enabled: bool) -> None:
    if not IS_WIN:
        return
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if enabled:
            winreg.SetValueEx(k, "Vocal LAN", 0, winreg.REG_SZ, _autostart_command())
        else:
            try:
                winreg.DeleteValue(k, "Vocal LAN")
            except FileNotFoundError:
                pass


def is_autostart() -> bool:
    if not IS_WIN:
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, "Vocal LAN")
            return True
    except OSError:
        return False

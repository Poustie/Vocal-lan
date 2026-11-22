# -*- mode: python ; coding: utf-8 -*-
# PyInstaller : dossier (onedir) sans console -> dist\Vocal LAN\Vocal LAN.exe
import os
from PyInstaller.utils.hooks import collect_data_files

here = os.path.dirname(os.path.abspath(SPEC))
icon = os.path.join(here, "assets", "icon.ico")
datas = collect_data_files("customtkinter") + collect_data_files("sounddevice")
if os.path.isdir(os.path.join(here, "assets")):
    datas.append((os.path.join(here, "assets"), "assets"))
# Suppression du bruit : rnnoise.dll (BSD-3, xiph.org) + sa licence et sa provenance, sous third_party/rnnoise/
# (denoise.py la cherche dans sys._MEIPASS/third_party/rnnoise/ et verifie son SHA-256 avant de la charger)
rn = os.path.join(here, "third_party", "rnnoise")
binaries = []
if os.path.isfile(os.path.join(rn, "rnnoise.dll")):
    binaries.append((os.path.join(rn, "rnnoise.dll"), os.path.join("third_party", "rnnoise")))
    for name in ("LICENSE", "SOURCE.txt"):
        if os.path.isfile(os.path.join(rn, name)):
            datas.append((os.path.join(rn, name), os.path.join("third_party", "rnnoise")))
hidden = [m for m in ("ui", "wizard", "tray", "fake_engine", "engine", "hotkeys", "audiodev", "netutil", "settings",
                      "denoise", "dsp", "room_echo")
          if os.path.exists(os.path.join(here, m + ".py"))]
hidden += ["pystray._win32", "PIL._tkinter_finder", "_sounddevice_data"]

a = Analysis(
    ["vocal.py"],
    pathex=[here],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden,
    excludes=["pytest", "matplotlib", "scipy", "pandas", "IPython", "notebook"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="Vocal LAN",
    console=False,
    icon=icon if os.path.exists(icon) else None,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="Vocal LAN", upx=False)

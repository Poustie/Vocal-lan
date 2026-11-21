@echo off
rem Lanceur pour les developpeurs. Les utilisateurs lancent "Vocal LAN.exe".
cd /d "%~dp0"
python -c "pass" >nul 2>&1
if errorlevel 1 (
  echo Python n est pas installe. Installe Python 3.11 depuis python.org ^(coche "Add to PATH"^),
  echo ou utilise la version "Vocal LAN.exe" qui n en a pas besoin.
  pause
  exit /b 1
)
rem Installation des dependances seulement si une importation echoue
python -c "import customtkinter, numpy, sounddevice, psutil, pystray, PIL" >nul 2>&1 || python -m pip install -r requirements.txt
python vocal.py %*
if errorlevel 1 pause

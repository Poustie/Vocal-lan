@echo off
rem Fabrique dist\Vocal LAN\Vocal LAN.exe (+ dist\Vocal LAN.zip a distribuer). Necessite : pip install pyinstaller
rem Apres la fabrication, nettoie build\, les __pycache__ et .pytest_cache : seul dist\ reste a donner.
cd /d "%~dp0"
python -c "import PyInstaller" >nul 2>&1 || python -m pip install pyinstaller
if exist "assets\make_icon.py" if not exist "assets\icon.ico" python assets\make_icon.py
python -m PyInstaller vocal_lan.spec --noconfirm --clean
if errorlevel 1 (
  echo La fabrication a echoue.
  pause
  exit /b 1
)
copy /y LISEZ-MOI.txt "dist\Vocal LAN\LISEZ-MOI.txt" >nul
rem Licence et provenance de rnnoise.dll (suppression du bruit) : visibles a cote de l'executable
if not exist "dist\Vocal LAN\Licences" mkdir "dist\Vocal LAN\Licences"
copy /y "third_party\rnnoise\LICENSE" "dist\Vocal LAN\Licences\RNNoise-LICENSE.txt" >nul
copy /y "third_party\rnnoise\SOURCE.txt" "dist\Vocal LAN\Licences\RNNoise-SOURCE.txt" >nul
powershell -NoProfile -Command "Compress-Archive -Path 'dist\Vocal LAN' -DestinationPath 'dist\Vocal LAN.zip' -Force"
rem Nettoyage (sans toucher a dist\)
if exist build rmdir /s /q build
if exist .pytest_cache rmdir /s /q .pytest_cache
for /d /r %%d in (__pycache__) do if exist "%%d" rmdir /s /q "%%d"
echo.
echo Termine : dist\Vocal LAN\Vocal LAN.exe et dist\Vocal LAN.zip

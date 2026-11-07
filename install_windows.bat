@echo off
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (py -m venv .venv) else (python -m venv .venv)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt
echo Instalacion terminada. Ejecuta run_fundamentallab.bat
pause

@echo off
cd /d "%~dp0"

if not exist ".venv\Scripts\activate.bat" (
    echo Falta el entorno virtual. Ejecuta install_windows.bat primero.
    pause
    exit /b 1
)

if not exist "data\technical_monitor.json" (
    echo No existe data\technical_monitor.json
    echo.
    echo Copiando ejemplo...
    copy /Y "technical_monitor.example.json" "data\technical_monitor.json" >nul
    echo.
    echo Edita data\technical_monitor.json y completa las credenciales de Alpaca.
    echo Si queres alertas de Telegram, completa bot token y chat id.
    pause
    exit /b 1
)

call .venv\Scripts\activate.bat
python technical_monitor.py
pause

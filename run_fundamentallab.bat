@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\activate.bat" (echo Ejecuta install_windows.bat primero & pause & exit /b 1)
call .venv\Scripts\activate.bat
streamlit run app.py
pause

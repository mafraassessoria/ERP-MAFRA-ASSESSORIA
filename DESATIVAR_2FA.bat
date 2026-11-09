@echo off
REM Remove a verificacao em duas etapas de um usuario (perdeu o celular). Uso: DESATIVAR_2FA.bat login
cd /d "%~dp0"
".venv\Scripts\python.exe" -m app.desativar_2fa %1
pause

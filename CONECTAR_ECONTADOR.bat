@echo off
REM Login no e-Contador (com verificacao) para o ERP poder dar baixa, enviar e excluir documentos.
cd /d "%~dp0"
".venv\Scripts\python.exe" -m app.econtador.conectar
pause

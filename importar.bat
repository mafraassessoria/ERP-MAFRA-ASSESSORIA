@echo off
REM Executado pela tarefa agendada "Emissor NFS-e - Importacao ADN".
REM Tambem pode ser executado manualmente com dois cliques.
cd /d "%~dp0"
if not exist logs mkdir logs
".venv\Scripts\python.exe" -m scripts.importar >> logs\importacao.log 2>&1

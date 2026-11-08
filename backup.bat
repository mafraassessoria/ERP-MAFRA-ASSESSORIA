@echo off
REM Executado pela tarefa agendada "Emissor NFS-e - Backup diario".
REM Tambem pode ser executado manualmente com dois cliques.
cd /d "%~dp0"
if not exist logs mkdir logs
echo [%date% %time%] Iniciando backup >> logs\backup.log
".venv\Scripts\python.exe" -m scripts.backup >> logs\backup.log 2>&1

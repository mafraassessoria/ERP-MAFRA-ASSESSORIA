@echo off
REM Executado pela tarefa agendada "Emissor NFS-e - Servidor".
REM Mantem o sistema no ar: se ele parar por qualquer motivo, reinicia em 10 segundos.
REM Endereco 127.0.0.1 = acessivel somente de dentro do servidor (uso via RDP).
cd /d "%~dp0"
if not exist logs mkdir logs
:inicio
echo [%date% %time%] Iniciando servidor >> logs\servidor.log
".venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8800 --no-access-log >> logs\servidor.log 2>&1
echo [%date% %time%] Servidor parou (codigo %errorlevel%). Reiniciando em 10 segundos >> logs\servidor.log
ping -n 11 127.0.0.1 > nul
goto inicio

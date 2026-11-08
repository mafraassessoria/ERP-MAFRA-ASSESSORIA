@echo off
REM Inicia o Emissor NFS-e no Windows (porta 8800)
cd /d %~dp0
if not exist .venv (
  echo Criando ambiente Python...
  py -3 -m venv .venv
  .venv\Scripts\pip install -r requirements.txt
)
.venv\Scripts\python -m uvicorn app.main:app --host 127.0.0.1 --port 8800

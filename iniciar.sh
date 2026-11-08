#!/usr/bin/env bash
# Inicia o Emissor NFS-e no Linux (porta 8800)
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  python3 -m venv .venv
  .venv/bin/pip install -r requirements.txt
fi
exec .venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8800 --proxy-headers

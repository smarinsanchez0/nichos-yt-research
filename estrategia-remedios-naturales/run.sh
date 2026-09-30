#!/usr/bin/env bash
# Arranca ESTRATEGIA REMEDIOS NATURALES en http://127.0.0.1:8000
cd "$(dirname "$0")"
[ -d .venv ] || python3 -m venv .venv
source .venv/bin/activate
python -m pip install -q --upgrade pip && python -m pip install -q -r requirements.txt
exec uvicorn app.main:app --host 127.0.0.1 --port 8000

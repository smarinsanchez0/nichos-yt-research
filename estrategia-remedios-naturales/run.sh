#!/usr/bin/env bash
# Arranca ESTRATEGIA REMEDIOS NATURALES en http://127.0.0.1:8000
cd "$(dirname "$0")"
ARCH=""; [ "$(sysctl -n hw.optional.arm64 2>/dev/null)" = "1" ] && ARCH="arch -arm64"
[ -d .venv ] || $ARCH /usr/bin/python3 -m venv .venv 2>/dev/null || python3 -m venv .venv
source .venv/bin/activate
python -m pip install -q --upgrade pip && python -m pip install -q -r requirements.txt
exec $ARCH .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000

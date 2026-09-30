#!/bin/bash
# Ejecutable de la app "ESTRATEGIA REMEDIOS NATURALES": arranca el servidor y abre la interfaz.
APP_CONTENTS="$(cd "$(dirname "$0")/.." && pwd)"
PROJECT="$(cat "$APP_CONTENTS/Resources/project_path")"
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
PORT="${ERN_PORT:-8000}"
URL="http://127.0.0.1:$PORT"
LOG="$HOME/Library/Logs/EstrategiaRemediosNaturales.log"
mkdir -p "$(dirname "$LOG")"

notify() { osascript -e "display notification \"$1\" with title \"ESTRATEGIA REMEDIOS NATURALES\"" >/dev/null 2>&1; }
alert()  { osascript -e "display alert \"ESTRATEGIA REMEDIOS NATURALES\" message \"$1\"" >/dev/null 2>&1; }
up()     { curl -fs -m 2 "$URL/api/status" >/dev/null 2>&1; }
open_ui() {
  if [ -d "/Applications/Google Chrome.app" ]; then open -na "Google Chrome" --args --app="$URL"
  else open "$URL"; fi
}

if up; then open_ui; exit 0; fi           # ya esta corriendo: solo abrir la ventana

if [ ! -d "$PROJECT/app" ]; then
  alert "No encuentro la carpeta del proyecto ($PROJECT). Vuelve a ejecutar mac/instalar_mac.sh."; exit 1
fi
cd "$PROJECT" || exit 1
PY="$(command -v python3)"
if [ -z "$PY" ]; then
  alert "Falta Python 3. Abre Terminal y ejecuta: xcode-select --install  (o brew install python). Luego abre la app otra vez."; exit 1
fi
if [ ! -x .venv/bin/uvicorn ]; then
  notify "Preparando por primera vez (2-3 min)…"
  { "$PY" -m venv .venv && .venv/bin/pip install -q -r requirements.txt; } >>"$LOG" 2>&1 \
    || { alert "No se pudieron instalar las dependencias. Revisa $LOG"; exit 1; }
fi

.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port "$PORT" >>"$LOG" 2>&1 &
SERVER=$!
trap 'kill $SERVER 2>/dev/null; exit 0' TERM INT HUP
for _ in $(seq 1 60); do up && break; sleep 0.5; done
if ! up; then alert "El servidor no arranco. Revisa $LOG"; kill $SERVER 2>/dev/null; exit 1; fi
notify "Lista. Abriendo la interfaz…"
open_ui
wait $SERVER

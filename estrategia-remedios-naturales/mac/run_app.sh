#!/bin/bash
# Ejecutable de la app "ESTRATEGIA REMEDIOS NATURALES": arranca el servidor y abre la interfaz.
PROJECT="$(cd "$(dirname "$0")/.." && pwd)"
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
# Apple Silicon: forzar modo nativo arm64 (si el lanzador corre bajo Rosetta se instalan piezas Intel que luego no cargan)
ARCH=""
if [ "$(sysctl -n hw.optional.arm64 2>/dev/null)" = "1" ]; then ARCH="arch -arm64"; fi
PY="/usr/bin/python3"; [ -x "$PY" ] || PY="$(command -v python3)"
if [ -z "$PY" ]; then
  alert "Falta Python 3. Abre Terminal y ejecuta: xcode-select --install  (o brew install python). Luego abre la app otra vez."; exit 1
fi
healthy() { $ARCH .venv/bin/python -c "import pydantic_core, uvicorn, PIL, av, ctranslate2" >/dev/null 2>&1; }
STAMP="$(shasum requirements.txt 2>/dev/null | cut -d' ' -f1)"
if [ ! -x .venv/bin/uvicorn ] || [ "$(cat .venv/.req_stamp 2>/dev/null)" != "$STAMP" ] || ! healthy; then
  notify "Instalando / reparando dependencias (2-3 min)…"
  if [ -d .venv ] && ! healthy; then rm -rf .venv; fi          # entorno danado: se reconstruye desde cero
  { [ -d .venv ] || $ARCH "$PY" -m venv .venv; $ARCH .venv/bin/python -m pip install -q --upgrade pip; \
    $ARCH .venv/bin/python -m pip install -q -r requirements.txt; } >>"$LOG" 2>&1 \
    || { alert "No se pudieron instalar las dependencias. Revisa $LOG"; exit 1; }
  echo "$STAMP" > .venv/.req_stamp
fi

$ARCH .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port "$PORT" >>"$LOG" 2>&1 &
SERVER=$!
trap 'kill $SERVER 2>/dev/null; exit 0' TERM INT HUP
for _ in $(seq 1 240); do up && break; kill -0 $SERVER 2>/dev/null || break; sleep 0.5; done
if ! up; then
  ERR="$(tail -n 5 "$LOG" 2>/dev/null | tr '"' "'" | tr '\n' ' ' | cut -c1-380)"
  alert "El servidor no arranco. Ultimo error: $ERR"
  kill $SERVER 2>/dev/null; exit 1
fi
notify "Lista. Abriendo la interfaz…"
open_ui
wait $SERVER

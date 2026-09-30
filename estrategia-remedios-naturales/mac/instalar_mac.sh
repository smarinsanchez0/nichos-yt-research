#!/bin/bash
# Crea la app "ESTRATEGIA REMEDIOS NATURALES.app" (con logo) en /Applications.
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
PROJECT="$(dirname "$HERE")"
NAME="ESTRATEGIA REMEDIOS NATURALES"
DEST="${ERN_APP_DEST:-/Applications}"
[ -w "$DEST" ] || { DEST="$HOME/Applications"; mkdir -p "$DEST"; }
APP="$DEST/$NAME.app"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
# el ejecutable solo delega en mac/run_app.sh: al hacer `git pull` la app se actualiza sola
printf '#!/bin/bash\nexec /bin/bash "%s/mac/run_app.sh"\n' "$PROJECT" > "$APP/Contents/MacOS/launcher"
chmod +x "$APP/Contents/MacOS/launcher"
cp "$HERE/icon.icns" "$APP/Contents/Resources/icon.icns"
printf '%s' "$PROJECT" > "$APP/Contents/Resources/project_path"
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>$NAME</string>
  <key>CFBundleDisplayName</key><string>$NAME</string>
  <key>CFBundleIdentifier</key><string>com.estrategia.remedios-naturales</string>
  <key>CFBundleExecutable</key><string>launcher</string>
  <key>CFBundleIconFile</key><string>icon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
  <key>NSHighResolutionCapable</key><true/>
</dict></plist>
PLIST
if command -v xattr >/dev/null; then xattr -cr "$APP"; fi
touch "$APP"
echo "Listo: $APP"
command -v open >/dev/null && open -R "$APP" || true
echo "Arrastra el icono al Dock. La primera vez tarda 2-3 min (instala dependencias)."

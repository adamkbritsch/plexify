#!/bin/bash
# Build Plexify.app — a native macOS SwiftUI app (PLEXIFY OLED theme) that wraps + polls the
# reused Python engine's JSON API. No WebView. Dev build: the app launches the existing venv +
# engine-run by path; a later build will embed the engine + venv into Contents/Resources.
set -e
# Repo root — derived from this script's location (macapp/build.sh), overridable via env.
ROOT="${PLEXIFY_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
# App bundle id — override via env (e.g. keep your own for stable TCC/LaunchAgent grants).
BUNDLE_ID="${BUNDLE_ID:-com.plexify.app}"
APP="$ROOT/Plexify.app"
STATIC="$ROOT/engine-run/app/static"
TMP="$(mktemp -d)"
BIN="$TMP/Plexify"

# 1. Compile ALL Swift sources together to a temp path (a build error never wrecks a working app).
swiftc -target arm64-apple-macosx14.0 -O \
  "$ROOT"/macapp/*.swift \
  -framework AppKit -framework SwiftUI \
  -o "$BIN"

# 2. Assemble the bundle (icon: Icon Composer Assets.car = live glass icon, .icns fallback;
#    compiled from macapp/Plexify.icon via Xcode actool — same approach as Visionary).
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
[ -f "$ROOT/macapp/Assets.car" ]   && cp "$ROOT/macapp/Assets.car"   "$APP/Contents/Resources/Assets.car"
[ -f "$ROOT/macapp/Plexify.icns" ] && cp "$ROOT/macapp/Plexify.icns" "$APP/Contents/Resources/Plexify.icns"
# Bundle the native-rendered brand wordmark (white SVG) used by the top bar.
[ -f "$STATIC/logo-plexify.svg" ] && cp "$STATIC/logo-plexify.svg" "$APP/Contents/Resources/logo-plexify.svg"
[ -f "$STATIC/icons.svg" ]        && cp "$STATIC/icons.svg"        "$APP/Contents/Resources/icons.svg"
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Plexify</string>
  <key>CFBundleDisplayName</key><string>Plexify</string>
  <key>CFBundleExecutable</key><string>Plexify</string>
  <key>CFBundleIdentifier</key><string>${BUNDLE_ID}</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleVersion</key><string>0.2</string>
  <key>CFBundleShortVersionString</key><string>0.2</string>
  <key>LSMinimumSystemVersion</key><string>14.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>CFBundleIconName</key><string>Plexify</string>
  <key>CFBundleIconFile</key><string>Plexify</string>
</dict></plist>
PLIST
cp "$BIN" "$APP/Contents/MacOS/Plexify"

# 3. Sign. Prefer a stable self-signed identity if present (survives rebuilds); else ad-hoc.
IDENTITY="Plexify Local Signing"
if security find-identity -v -p codesigning 2>/dev/null | grep -q "$IDENTITY"; then
  codesign --force --deep -s "$IDENTITY" --timestamp=none "$APP" 2>/dev/null && echo "signed with '$IDENTITY'"
else
  codesign --force --deep -s - "$APP" 2>/dev/null || true
  echo "ad-hoc signed (run macapp/setup-signing-cert.sh for a stable identity)"
fi
rm -rf "$TMP"
echo "built: $APP"

#!/bin/bash
# Build DEMASCAS.app — DEMASCAS as a menu-bar app that runs by itself
# (no Terminal).  Installs to ~/Applications/DEMASCAS.app.
#
#   ./scripts/build_app.sh            # build + install
#   open ~/Applications/DEMASCAS.app  # start it (menu: Open at Login)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# the real Applications folder (Launchpad / Finder / Spotlight); ~/Applications if not writable
if [ -w /Applications ]; then OUT="/Applications/DEMASCAS.app"; else OUT="${HOME}/Applications/DEMASCAS.app"; fi
BUILD="$(mktemp -d)"
trap 'rm -rf "$BUILD"' EXIT

echo "[*] Compiling the app…"
swiftc -O -o "$BUILD/DEMASCAS" "$ROOT/app/DEMASCAS/main.swift" "$ROOT/app/DEMASCAS/Window.swift" \
    -framework AppKit -framework ServiceManagement -framework WebKit -framework AVFoundation -framework Network

APP="$BUILD/DEMASCAS.app"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$BUILD/DEMASCAS" "$APP/Contents/MacOS/DEMASCAS"
cp "$ROOT/app/DEMASCAS/noir.html" "$APP/Contents/Resources/noir.html"
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>DEMASCAS</string>
  <key>CFBundleDisplayName</key><string>DEMASCAS</string>
  <key>CFBundleIdentifier</key><string>com.demascas.app</string>
  <key>CFBundleExecutable</key><string>DEMASCAS</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>LSUIElement</key><true/>
  <key>NSMicrophoneUsageDescription</key><string>DEMASCAS listens for its name and your requests.</string>
  <key>NSAppleEventsUsageDescription</key><string>DEMASCAS controls apps you ask it to (Brave, Mail, Notes, System Events).</string>
  <key>NSContactsUsageDescription</key><string>DEMASCAS finds the people you ask it to call or message.</string>
  <key>NSCalendarsUsageDescription</key><string>DEMASCAS reads and adds events when you ask.</string>
  <key>NSRemindersUsageDescription</key><string>DEMASCAS sets the reminders you ask for.</string>
  <key>DEMASCASRoot</key><string>${ROOT}</string>
</dict></plist>
PLIST

# A STABLE signature keeps macOS permissions across rebuilds (an ad-hoc one
# changes every build, so Accessibility / Screen Recording had to be granted
# again each time).  The first signing asks once for keychain access — click
# "Always Allow".  No local identity yet → ad-hoc (permissions reset per build).
IDENTITY="DEMASCAS Local Signing"
if [ -z "${DMX_ADHOC:-}" ] && security find-identity -p codesigning 2>/dev/null | grep -q "$IDENTITY"; then
    echo "[*] Signing with \"$IDENTITY\" (stable — permissions survive rebuilds)…"
    codesign --force --deep --sign "$IDENTITY" "$APP"
else
    echo "[*] Signing ad-hoc (permissions may need granting again after each rebuild)."
    codesign --force --deep --sign - "$APP" >/dev/null
fi

mkdir -p "${HOME}/Applications"
if pgrep -x DEMASCAS >/dev/null; then
    echo "[!] DEMASCAS.app is running — quit it from the menu bar first, then build again."
    exit 1
fi
rm -rf "$OUT"
cp -R "$APP" "$OUT"
# one copy only: an older build in ~/Applications would confuse Spotlight and the permission lists
[ "$OUT" != "${HOME}/Applications/DEMASCAS.app" ] && rm -rf "${HOME}/Applications/DEMASCAS.app"
echo "[✓] Installed $OUT"
echo "    Start it:  open \"$OUT\"   (a waveform icon appears in the menu bar)"
echo "    First run: allow Microphone, then add DEMASCAS in System Settings →"
echo "    Privacy & Security → Accessibility and Screen Recording."

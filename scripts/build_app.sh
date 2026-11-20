#!/bin/bash
# Build DEMASCAS.app and install it in /Applications (the main Applications
# folder — NOT the "Applications" inside your home folder), then start it.
#
#   ./scripts/build_app.sh             # build, install, (re)start
#   ./scripts/build_app.sh --no-open   # build + install only
#   or double-click "Build DEMASCAS.command" in Finder
#
# If DEMASCAS is running it is quit first and started again afterwards.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OPEN=1; [ "${1:-}" = "--no-open" ] && OPEN=0

# the Swift compiler comes with Xcode or the Command Line Tools
if ! xcrun --find swiftc >/dev/null 2>&1; then
    echo "[-] The Swift compiler isn't installed. Run:  xcode-select --install"
    echo "    (a dialog installs the Command Line Tools), then run this again."
    exit 1
fi
# the real Applications folder (Launchpad / Finder / Spotlight); ~/Applications if not writable
if [ -w /Applications ]; then OUT="/Applications/DEMASCAS.app"; else OUT="${HOME}/Applications/DEMASCAS.app"; fi
BUILD="$(mktemp -d)"
trap 'rm -rf "$BUILD"' EXIT

echo "[*] Compiling the app…"
swiftc -O -suppress-warnings -o "$BUILD/DEMASCAS" "$ROOT/app/DEMASCAS/main.swift" "$ROOT/app/DEMASCAS/Window.swift" \
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

# quit the running copy (it stops its own services), so the new one can replace it
if pgrep -x DEMASCAS >/dev/null; then
    echo "[*] Quitting the running DEMASCAS…"
    osascript -e 'quit app "DEMASCAS"' >/dev/null 2>&1 || true
    for _ in $(seq 1 30); do pgrep -x DEMASCAS >/dev/null || break; sleep 1; done
    if pgrep -x DEMASCAS >/dev/null; then
        pkill -x DEMASCAS || true; sleep 2
    fi
fi
rm -rf "$OUT"
cp -R "$APP" "$OUT"
# one copy only: an older build in ~/Applications would confuse Spotlight and the permission lists
[ "$OUT" != "${HOME}/Applications/DEMASCAS.app" ] && rm -rf "${HOME}/Applications/DEMASCAS.app"
# …and a shortcut in the home-folder Applications too, so it's found in either one
if [ "$OUT" = "/Applications/DEMASCAS.app" ]; then
    rm -f "${HOME}/Applications/DEMASCAS"
    osascript -e "tell application \"Finder\" to set a to make alias file to (POSIX file \"$OUT\") at (POSIX file \"${HOME}/Applications\")" \
              -e 'tell application "Finder" to set name of a to "DEMASCAS"' >/dev/null 2>&1 || true
fi
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$OUT" >/dev/null 2>&1 || true
echo ""
echo "[✓] Installed: $OUT"
echo "    Find it in Finder → Applications (the main one), Launchpad, or Spotlight: DEMASCAS."
if [ "$OPEN" = 1 ]; then
    open "$OUT"
    echo "[✓] Started — the waveform icon is in the menu bar."
else
    echo "    Start it:  open \"$OUT\""
fi
echo "    First run only: allow the Microphone, and turn DEMASCAS on in System Settings →"
echo "    Privacy & Security → Accessibility and Screen Recording."

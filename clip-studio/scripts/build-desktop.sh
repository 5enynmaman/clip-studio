#!/bin/bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="$PROJECT_ROOT/.venv/bin/python"
APP_NAME="Clip Studio"
BUILD_DIR="${TMPDIR:-/tmp}/clip-studio-desktop-build-$UID"
APP_BUNDLE="$BUILD_DIR/$APP_NAME.app"
INSTALL_DIR="$HOME/Applications"
INSTALL_PATH="$INSTALL_DIR/$APP_NAME.app"
STAGING_PATH="$INSTALL_DIR/.$APP_NAME.app.new"
BACKUP_PATH="$INSTALL_DIR/.$APP_NAME.app.previous"

if [[ ! -x "$PYTHON" ]]; then
  echo "Missing project virtualenv: $PYTHON" >&2
  exit 1
fi
"$PYTHON" "$PROJECT_ROOT/scripts/local_desktop.py" --project-root "$PROJECT_ROOT" --check

rm -rf "$APP_BUNDLE"
mkdir -p "$APP_BUNDLE/Contents/MacOS" "$APP_BUNDLE/Contents/Resources"
cp "$PROJECT_ROOT/scripts/local_desktop.py" "$APP_BUNDLE/Contents/Resources/local_desktop.py"
cat > "$APP_BUNDLE/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleExecutable</key><string>ClipStudio</string>
  <key>CFBundleIdentifier</key><string>com.clipstudio.local</string>
  <key>CFBundleName</key><string>Clip Studio</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>LSMultipleInstancesProhibited</key><true/>
  <key>NSHighResolutionCapable</key><true/>
  <key>ClipStudioProjectRoot</key><string>$PROJECT_ROOT</string>
  <key>ClipStudioPython</key><string>$PYTHON</string>
</dict>
</plist>
PLIST
swiftc -parse-as-library -O -framework AppKit -framework WebKit -framework UniformTypeIdentifiers \
  -module-cache-path "$BUILD_DIR/module-cache" \
  "$PROJECT_ROOT/desktop/ClipStudioApp.swift" -o "$APP_BUNDLE/Contents/MacOS/ClipStudio"
chmod 755 "$APP_BUNDLE/Contents/MacOS/ClipStudio"
codesign --force --deep --sign - "$APP_BUNDLE"
if [[ "${1:-}" == "--build-only" ]]; then
  echo "Built without replacing the installed app: $APP_BUNDLE"
  exit 0
fi
mkdir -p "$INSTALL_DIR"
if [[ -e "$INSTALL_PATH" ]]; then
  existing_id="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$INSTALL_PATH/Contents/Info.plist" 2>/dev/null || true)"
  if [[ "$existing_id" != "com.clipstudio.local" ]]; then
    echo "Refusing to replace an app that is not Clip Studio: $INSTALL_PATH" >&2
    exit 1
  fi
  if [[ -e "$BACKUP_PATH" ]]; then
    echo "Previous app backup already exists: $BACKUP_PATH" >&2
    exit 1
  fi
  rm -rf "$STAGING_PATH"
  cp -R "$APP_BUNDLE" "$STAGING_PATH"
  mv "$INSTALL_PATH" "$BACKUP_PATH"
  if mv "$STAGING_PATH" "$INSTALL_PATH"; then
    rm -rf "$BACKUP_PATH"
  else
    mv "$BACKUP_PATH" "$INSTALL_PATH"
    exit 1
  fi
else
  cp -R "$APP_BUNDLE" "$INSTALL_PATH"
fi
echo "Built and installed: $INSTALL_PATH"

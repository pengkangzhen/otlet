#!/bin/bash
# Build Agent-Lit macOS app bundle
set -e

ROOT="$(cd "$(dirname "$0")" && pwd)"
DIST="$ROOT/dist"
APP="$DIST/Agent-Lit.app"
VERSION="$(grep '^version' "$ROOT/pyproject.toml" | head -1 | sed 's/.*= *"\(.*\)".*/\1/')"

echo "==> Building Agent-Lit v${VERSION:-0.1.0}..."
uv run --no-sync pyinstaller agent-lit.spec --clean --noconfirm

echo "==> Creating .app bundle..."
rm -rf "$APP"

# Standard macOS .app structure — no AppleScript wrapper
mkdir -p "$APP/Contents/MacOS"
mkdir -p "$APP/Contents/Resources"

# Copy onedir output into MacOS/ (executable + _internal/)
cp -R "$DIST/agent-lit/"* "$APP/Contents/MacOS/"

# PyInstaller inside .app/Contents/MacOS/ looks for libraries at
# ../Frameworks/ and data at ../Resources/ — symlink _internal there
ln -s MacOS/_internal "$APP/Contents/Frameworks"

# Write Info.plist with production metadata
cat > "$APP/Contents/Info.plist" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key>
    <string>agent-lit</string>
    <key>CFBundleIdentifier</key>
    <string>com.agent-lit.app</string>
    <key>CFBundleName</key>
    <string>Agent-Lit</string>
    <key>CFBundleDisplayName</key>
    <string>Agent-Lit</string>
    <key>CFBundleVersion</key>
    <string>${VERSION:-0.1.0}</string>
    <key>CFBundleShortVersionString</key>
    <string>${VERSION:-0.1.0}</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleInfoDictionaryVersion</key>
    <string>6.0</string>
    <key>NSHighResolutionCapable</key>
    <true/>
    <key>LSMinimumSystemVersion</key>
    <string>13.0</string>
    <key>LSUIElement</key>
    <false/>
    <key>NSHumanReadableCopyright</key>
    <string>© 2026 Agent-Lit. All rights reserved.</string>
    <key>NSRequiresAquaSystemAppearance</key>
    <false/>
    <key>CFBundleIconFile</key>
    <string>AppIcon</string>
    <key>CFBundleIconName</key>
    <string>AppIcon</string>
</dict>
</plist>
PLIST

# Copy app icon
if [ -f "$ROOT/assets/AppIcon.icns" ]; then
    cp "$ROOT/assets/AppIcon.icns" "$APP/Contents/Resources/AppIcon.icns"
    echo "    Icon: assets/AppIcon.icns"
fi

echo "==> Done: $APP (v${VERSION:-0.1.0})"

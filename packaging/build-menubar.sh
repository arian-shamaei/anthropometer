#!/bin/sh
# Assemble amtrino.app from the SPM build — no Xcode project, just swiftc.
# Output: menubar/.build/amtrino.app (ad-hoc signed, selfchecked).
# Usage: sh packaging/build-menubar.sh [version]
set -e
cd "$(dirname "$0")/.."
VERSION="${1:-0.1.1}"

sh packaging/sync-engine.sh
cd menubar
swift build -c release

APP=.build/amtrino.app
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp .build/release/AmtrBar "$APP/Contents/MacOS/amtrino"
# SPM resource bundle (the engine) must sit in Contents/Resources for
# Bundle.module to resolve inside the app
cp -R .build/release/AmtrBar_AmtrBar.bundle "$APP/Contents/Resources/"

# app icon: rendered by the binary's own palette code, then compiled to icns
rm -rf .build/AppIcon.iconset
.build/release/AmtrBar --render-appicon .build/AppIcon.iconset
iconutil -c icns .build/AppIcon.iconset -o "$APP/Contents/Resources/AppIcon.icns"

# modern layered icon (Icon Composer .icon → Assets.car): macOS 26
# notification banners ignore legacy .icns AND classic appiconset catalogs
# (both verified) — only the layered IconImageStack renders there. Compiled
# only when a full Xcode is present (actool is not in the Command Line
# Tools); the hand-built icns above remains the fallback everywhere else.
# actool also emits its own glass-treated AppIcon.icns, overwriting ours —
# desirable: one consistent look in Finder, Dock, and banners.
ICON_NAME_KEY=""
if [ -d /Applications/Xcode.app ]; then
  IC=.build/AppIcon.icon
  rm -rf "$IC"
  mkdir -p "$IC/Assets"
  cp .build/layer_dots.png "$IC/Assets/dots.png"
  cat > "$IC/icon.json" <<'JSON'
{
  "fill" : {
    "solid" : "srgb:0.094,0.102,0.129,1.0"
  },
  "groups" : [
    {
      "layers" : [
        {
          "image-name" : "dots.png",
          "name" : "dots"
        }
      ]
    }
  ],
  "supported-platforms" : {
    "circles" : [ "watchOS" ],
    "squares" : "shared"
  }
}
JSON
  # absolute paths: actool resolves relative --compile paths against the
  # wrong root (observed: dropped the menubar/ component)
  DEVELOPER_DIR=/Applications/Xcode.app xcrun actool "$PWD/$IC" \
    --compile "$PWD/$APP/Contents/Resources" --platform macosx \
    --minimum-deployment-target 13.0 --app-icon AppIcon \
    --output-partial-info-plist "$PWD/.build/actool-info.plist" >/dev/null
  if [ -f "$APP/Contents/Resources/Assets.car" ]; then
    ICON_NAME_KEY="<key>CFBundleIconName</key><string>AppIcon</string>"
    echo "compiled layered icon (Assets.car + glass icns)"
  else
    echo "warning: actool produced no Assets.car — icns fallback only"
  fi
fi

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key><string>amtrino</string>
    <key>CFBundleIdentifier</key><string>dev.arian-shamaei.amtrino</string>
    <key>CFBundleName</key><string>amtrino</string>
    <key>CFBundleDisplayName</key><string>amtrino</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleShortVersionString</key><string>${VERSION}</string>
    <key>CFBundleVersion</key><string>${VERSION}</string>
    <key>CFBundleIconFile</key><string>AppIcon</string>
    ${ICON_NAME_KEY}
    <key>LSMinimumSystemVersion</key><string>13.0</string>
    <key>LSUIElement</key><true/>
    <key>NSHumanReadableCopyright</key><string>MIT — github.com/arian-shamaei/anthropometer</string>
</dict>
</plist>
PLIST

# signing: Developer ID + hardened runtime when the cert exists
# (notarizable); ad-hoc otherwise (local dev). $AMTRINO_SIGN_ID overrides.
SIGN_ID="${AMTRINO_SIGN_ID:-$(security find-identity -v -p codesigning 2>/dev/null \
  | awk -F'"' '/Developer ID Application/{print $2; exit}')}"
if [ -n "$SIGN_ID" ]; then
  echo "signing: $SIGN_ID (hardened runtime)"
  codesign --force --options runtime --timestamp --sign "$SIGN_ID" \
    "$APP/Contents/MacOS/amtrino"
  codesign --force --options runtime --timestamp --sign "$SIGN_ID" "$APP"
else
  echo "signing: ad-hoc (no Developer ID Application cert found)"
  codesign --force --sign - "$APP/Contents/MacOS/amtrino"
  codesign --force --sign - "$APP"
fi

# the build gate: golden palette parity + wire model + engine resolution
"$APP/Contents/MacOS/amtrino" --selfcheck

# notarization: runs when signed with Developer ID AND credentials are
# stored (one-time: xcrun notarytool store-credentials amtrino
#   --apple-id <you> --team-id <TEAM> --password <app-specific-password>)
if [ -n "$SIGN_ID" ] && DEVELOPER_DIR=/Applications/Xcode.app \
     xcrun notarytool history --keychain-profile amtrino >/dev/null 2>&1; then
  echo "notarizing (keychain profile: amtrino)…"
  ditto -c -k --keepParent "$APP" .build/amtrino-notarize.zip
  DEVELOPER_DIR=/Applications/Xcode.app xcrun notarytool submit \
    .build/amtrino-notarize.zip --keychain-profile amtrino --wait
  DEVELOPER_DIR=/Applications/Xcode.app xcrun stapler staple "$APP"
  echo "notarized + stapled"
elif [ -n "$SIGN_ID" ]; then
  echo "notarization skipped: no 'amtrino' keychain profile" \
       "(xcrun notarytool store-credentials amtrino …)"
fi

echo "built $APP (v${VERSION})"

#!/bin/sh
# Build "lora.app": double-click to open the TUI in Ghostty (falls back to Terminal if Ghostty isn't installed).
# Usage: tools/make_app.sh [destination dir, default ~/Applications]
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST="${1:-$HOME/Applications}"
APP="$DEST/lora.app"
TMP="$(mktemp -d)"
rm -rf "$APP"; mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources" "$DEST"

cat > "$APP/Contents/Info.plist" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
 <key>CFBundleName</key><string>lora</string>
 <key>CFBundleDisplayName</key><string>lora</string>
 <key>CFBundleIdentifier</key><string>local.bruce.lora-remote</string>
 <key>CFBundleExecutable</key><string>bruce-lora</string>
 <key>CFBundleIconFile</key><string>AppIcon</string>
 <key>CFBundlePackageType</key><string>APPL</string>
 <key>CFBundleShortVersionString</key><string>1.0</string>
 <key>LSMinimumSystemVersion</key><string>11.0</string>
 <key>NSHighResolutionCapable</key><true/>
</dict></plist>
PL

cat > "$APP/Contents/MacOS/bruce-lora" <<LAUNCH
#!/bin/sh
RUN="$ROOT/run.sh"
# find Ghostty by bundle id, so it works even if the app was renamed
GHOSTTY="\$(mdfind "kMDItemCFBundleIdentifier == 'com.mitchellh.ghostty'" | head -1)"
if [ -n "\$GHOSTTY" ]; then
  exec open -na "\$GHOSTTY" --args --title="lora" --window-width=150 --window-height=46 \\
    --confirm-close-surface=false --quit-after-last-window-closed=true --command="\$RUN"
fi
exec osascript -e 'tell application "Terminal"' -e 'activate' -e "do script \"exec '\$RUN'\"" -e 'end tell'
LAUNCH
chmod +x "$APP/Contents/MacOS/bruce-lora"

# icon: black rounded square, light-blue signal arcs
cat > "$TMP/icon.svg" <<'SV'
<svg xmlns="http://www.w3.org/2000/svg" width="1024" height="1024" viewBox="0 0 1024 1024">
<rect x="60" y="60" width="904" height="904" rx="200" fill="#000"/>
<rect x="60" y="60" width="904" height="904" rx="200" fill="none" stroke="#5ec8ff" stroke-width="14"/>
<g fill="none" stroke="#5ec8ff" stroke-linecap="round" stroke-width="56">
<path d="M330 420 A260 260 0 0 1 330 700" opacity=".55"/><path d="M694 420 A260 260 0 0 0 694 700" opacity=".55"/>
<path d="M410 470 A140 140 0 0 1 410 650"/><path d="M614 470 A140 140 0 0 0 614 650"/></g>
<circle cx="512" cy="560" r="44" fill="#5ec8ff"/>
<path d="M300 300 L400 350 M300 300 L400 250" stroke="#5ec8ff" stroke-width="0"/>
<text x="512" y="330" font-family="Menlo,monospace" font-size="120" font-weight="bold" fill="#5ec8ff" text-anchor="middle">&gt;_</text>
</svg>
SV
# ...unless lora.png sits in the project root: then that is the icon
if [ -f "$ROOT/lora-icon.png" ]; then cp "$ROOT/lora-icon.png" "$TMP/icon.svg.png"   # full-bleed version of lora.png
elif [ -f "$ROOT/lora.png" ]; then cp "$ROOT/lora.png" "$TMP/icon.svg.png"
else qlmanage -t -s 1024 -o "$TMP" "$TMP/icon.svg" >/dev/null 2>&1 || true; fi
if [ -f "$TMP/icon.svg.png" ]; then
  IS="$TMP/AppIcon.iconset"; mkdir "$IS"
  for s in 16 32 128 256 512; do
    sips -z $s $s "$TMP/icon.svg.png" --out "$IS/icon_${s}x${s}.png" >/dev/null
    sips -z $((s*2)) $((s*2)) "$TMP/icon.svg.png" --out "$IS/icon_${s}x${s}@2x.png" >/dev/null
  done
  iconutil -c icns "$IS" -o "$APP/Contents/Resources/AppIcon.icns" || true
fi
rm -rf "$TMP"
touch "$APP"
echo "built: $APP"

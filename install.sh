#!/usr/bin/env bash
# Install LOQ Control for the current user.
#
#   ./install.sh          install
#   ./install.sh --undo   remove
#
# The two things this gets right that a hand-written .desktop usually does not:
#
#   1. The .desktop basename must be EXACTLY loq-control.desktop, because under
#      Wayland GNOME matches a window to its launcher by app_id — which Qt takes
#      from setDesktopFileName('loq-control'). A file named anything else means
#      no dock icon and a generic name, no matter what is inside it.
#
#   2. Icon= names a THEME icon, not a path. It only resolves once the PNGs are
#      installed into hicolor at real sizes and the cache is refreshed. An
#      Icon= pointing at a file in the source tree works when launched from a
#      terminal and silently fails from the dock.
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
APPS="$HOME/.local/share/applications"
ICONS="$HOME/.local/share/icons/hicolor"
DESKTOP="$APPS/loq-control.desktop"

if [ "${1:-}" = "--undo" ]; then
  rm -f "$DESKTOP"
  for s in 16 24 32 48 64 128 256; do
    rm -f "$ICONS/${s}x${s}/apps/loq-control.png"
  done
  rm -f "$ICONS/scalable/apps/loq-control.svg"
  gtk-update-icon-cache -f -t "$ICONS" 2>/dev/null || true
  update-desktop-database "$APPS" 2>/dev/null || true
  echo "removed."
  exit 0
fi

# ── icon ───────────────────────────────────────────────────────────────
if [ -f "$SRC/loq.png" ]; then
  for s in 16 24 32 48 64 128 256; do
    mkdir -p "$ICONS/${s}x${s}/apps"
    # Pillow first: ImageMagick's PNG delegate is missing or policy-blocked on
    # a surprising number of distributions, and it fails with "no decode
    # delegate" on a perfectly valid file.
    if python3 -c "import PIL" 2>/dev/null; then
      python3 - "$SRC/loq.png" "$ICONS/${s}x${s}/apps/loq-control.png" "$s" <<'PYEOF'
import sys
from PIL import Image
src, dst, size = sys.argv[1], sys.argv[2], int(sys.argv[3])
Image.open(src).convert("RGBA").resize((size, size), Image.LANCZOS).save(dst)
PYEOF
    elif command -v magick >/dev/null 2>&1; then
      magick "$SRC/loq.png" -resize "${s}x${s}" "$ICONS/${s}x${s}/apps/loq-control.png"
    else
      cp "$SRC/loq.png" "$ICONS/${s}x${s}/apps/loq-control.png"
    fi
  done
  gtk-update-icon-cache -f -t "$ICONS" 2>/dev/null || true
  echo "  icon installed into hicolor"
else
  echo "  ! loq.png not found — the dock will show a generic icon"
fi

# ── launcher ───────────────────────────────────────────────────────────
mkdir -p "$APPS"
cat > "$DESKTOP" <<EOF
[Desktop Entry]
Type=Application
Name=LOQ Control
GenericName=Laptop Control Centre
Comment=Fans, power profiles, GPU and battery for Lenovo LOQ laptops
Exec=$SRC/loq-control.py
Icon=loq-control
Terminal=false
Categories=System;Settings;HardwareSettings;
Keywords=fan;power;profile;gpu;battery;thermal;overclock;
# X11 only — Wayland uses app_id from setDesktopFileName() instead. Kept so the
# icon still matches if the session is Xorg or the app runs under XWayland.
StartupWMClass=loq-control.py
StartupNotify=true
SingleMainWindow=true
Actions=Quiet;Balanced;Performance;

[Desktop Action Quiet]
Name=Quiet profile
Exec=$SRC/loq-control.py --profile quiet

[Desktop Action Balanced]
Name=Balanced profile
Exec=$SRC/loq-control.py --profile balanced

[Desktop Action Performance]
Name=Performance profile
Exec=$SRC/loq-control.py --profile performance
EOF
chmod +x "$DESKTOP" "$SRC/loq-control.py"
update-desktop-database "$APPS" 2>/dev/null || true
echo "  launcher installed -> $DESKTOP"

# ── remove the old, wrongly-named entry ────────────────────────────────
# It could never match app_id under Wayland, and leaving it behind means two
# entries in the app grid.
if [ -f "$APPS/loq-fan-control.desktop" ]; then
  rm -f "$APPS/loq-fan-control.desktop"
  echo "  removed the old loq-fan-control.desktop (wrong basename for Wayland app_id)"
fi

echo
echo "Done. Log out and back in if the dock still shows the old icon."

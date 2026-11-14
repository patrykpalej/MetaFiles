#!/usr/bin/env bash
# Install MetaFiles for the current user (no root needed).
# Usage: ./install.sh            install
#        ./install.sh --uninstall remove
set -euo pipefail

APP_ID="io.github.metafiles.MetaFiles"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="$HOME/.local/bin"
APPS_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
BIN="$BIN_DIR/metafiles"
DESKTOP="$APPS_DIR/$APP_ID.desktop"
ICON_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor"
ICON="$ICON_DIR/scalable/apps/$APP_ID.svg"

if [ "${1:-}" = "--uninstall" ]; then
    rm -f "$BIN" "$DESKTOP" "$ICON"
    update-desktop-database "$APPS_DIR" 2>/dev/null || true
    echo "MetaFiles removed (settings kept in ~/.config/metafiles and ~/.local/share/metafiles)."
    exit 0
fi

# Required: GTK 4 and libadwaita through PyGObject.
if ! python3 - <<'PY'
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw
PY
then
    echo "Missing required packages. Install them with:"
    echo "  sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1"
    exit 1
fi

# Optional components: report what is missing, but install anyway.
optional=$(python3 - "$SRC" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
import metafiles  # noqa: F401
from metafiles import features
print(" ".join(features.missing_packages()))
PY
)

mkdir -p "$BIN_DIR" "$APPS_DIR" "$(dirname "$ICON")"
install -m 644 "$SRC/data/icons/hicolor/scalable/apps/$APP_ID.svg" "$ICON"
gtk-update-icon-cache -q -t "$ICON_DIR" 2>/dev/null || true
chmod +x "$SRC/bin/metafiles"
ln -sf "$SRC/bin/metafiles" "$BIN"
sed "s|@BIN@|$BIN|g" "$SRC/data/$APP_ID.desktop.in" > "$DESKTOP"
update-desktop-database "$APPS_DIR" 2>/dev/null || true

echo "Installed:"
echo "  command:  $BIN"
echo "  launcher: $DESKTOP"
case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) echo "Note: $BIN_DIR is not in PATH; the GNOME launcher works regardless." ;;
esac
if [ -n "$optional" ]; then
    echo
    echo "Optional features need more packages (terminal panel, syntax highlighting, PDF preview, Meld):"
    echo "  sudo apt install $optional"
fi

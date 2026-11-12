#!/usr/bin/env bash
# Install a desktop menu entry (KDE, GNOME, ...) for the current user that
# starts Grembyte through scripts/grembyte.sh.
# Remove it again with: install-desktop-entry.sh --uninstall
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
APPS_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
# The file name has to match DESKTOP_FILE_NAME in grembyte/main.py, so that
# Wayland shows the icon of this entry.
DESKTOP_FILE="$APPS_DIR/grembyte.desktop"
# The entry of Hermes Hook, as the project was called up to 0.6
OLD_DESKTOP_FILE="$APPS_DIR/hermes-hook.desktop"

refresh_menu() {
    command -v update-desktop-database >/dev/null && update-desktop-database "$APPS_DIR" || true
    command -v kbuildsycoca6 >/dev/null && kbuildsycoca6 >/dev/null 2>&1 || true
}

if [[ "${1:-}" == "--uninstall" ]]; then
    rm -f "$DESKTOP_FILE" "$OLD_DESKTOP_FILE"
    refresh_menu
    echo "Removed $DESKTOP_FILE"
    exit 0
fi

rm -f "$OLD_DESKTOP_FILE"
chmod +x "$PROJECT_DIR/scripts/grembyte.sh"
mkdir -p "$APPS_DIR"
cat > "$DESKTOP_FILE" <<ENTRY
[Desktop Entry]
Type=Application
Name=Grembyte
GenericName=Memory Scanner and Hook Engine
Comment=Linux memory scanner, hook engine and reverse engineering tool
Comment[de]=Speicherscanner, Hook-Engine und Reverse-Engineering-Werkzeug für Linux
Exec="$PROJECT_DIR/scripts/grembyte.sh"
Path=$PROJECT_DIR
Icon=$PROJECT_DIR/grembyte/ui/resources/favicon.svg
Terminal=false
Categories=Development;Debugger;Game;
Keywords=memory;scanner;hook;trainer;cheat;
StartupWMClass=grembyte
ENTRY

command -v desktop-file-validate >/dev/null && desktop-file-validate "$DESKTOP_FILE" || true
refresh_menu
echo "Installed $DESKTOP_FILE"

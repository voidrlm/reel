#!/usr/bin/env bash
# Reel installer: adds a launcher and app-grid entry for the current user.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
APP_ID=io.github.voidrlm.Reel

if ! python3 - <<'PY' 2>/dev/null
import gi
gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1"); gi.require_version("Gst", "1.0")
from gi.repository import Gtk, Adw, Gst
assert (Adw.get_major_version(), Adw.get_minor_version()) >= (1, 8)
Gst.init(None)
assert Gst.ElementFactory.find("gtk4paintablesink")
PY
then
    echo "Missing dependencies: PyGObject, GTK 4, libadwaita 1.8+ and the GStreamer GTK 4 sink are required." >&2
    echo "  Fedora: sudo dnf install python3-gobject gtk4 libadwaita gstreamer1-plugin-gtk4 gstreamer1-plugins-good gstreamer1-plugins-bad-free gstreamer1-plugin-libav" >&2
    exit 1
fi

chmod +x "$here/reel"
mkdir -p ~/.local/bin ~/.local/share/applications ~/.local/share/icons/hicolor/scalable/apps
ln -sf "$here/reel" ~/.local/bin/reel
ln -sf "$here/$APP_ID.svg" ~/.local/share/icons/hicolor/scalable/apps/$APP_ID.svg
sed "s|^Exec=reel|Exec=$HOME/.local/bin/reel|" "$here/$APP_ID.desktop" > ~/.local/share/applications/$APP_ID.desktop
update-desktop-database ~/.local/share/applications 2>/dev/null || true
gtk-update-icon-cache -q -t ~/.local/share/icons/hicolor 2>/dev/null || true
if [ "${1:-}" = "--default" ]; then
    xdg-mime default $APP_ID.desktop $(sed -n 's/^MimeType=//p' "$here/$APP_ID.desktop" | tr ';' ' ')
    echo "Reel is now the default player for videos and music."
fi
echo "Installed. Open “Reel” from the app grid, or run: reel [file|url]"

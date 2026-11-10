#!/usr/bin/env bash
# Installs Anbled Screen for the current user (no root needed), on any Linux with systemd.
#
#   curl -fsSL https://raw.githubusercontent.com/vitroyfix/anbled-screen/main/install.sh | bash
#   ./install.sh               from a clone of the repository
#   ./install.sh --uninstall   removes it again
set -euo pipefail

REPO="https://github.com/vitroyfix/anbled-screen"
BRANCH="${ANBLED_SCREEN_BRANCH:-main}"
SHARE="$HOME/.local/share"
BIN="$HOME/.local/bin"
UNITS="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
APPS="$SHARE/applications"
ICONS="$SHARE/icons/hicolor/512x512/apps"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
fail() { printf '\033[31mError:\033[0m %s\n' "$*" >&2; exit 1; }

uninstall() {
  systemctl --user stop anbled-screen.service 2>/dev/null || true
  systemctl --user disable anbled-screen.service 2>/dev/null || true
  rm -f "$UNITS/anbled-screen.service" "$BIN/anbled-screen" "$APPS/anbled-screen.desktop" \
        "$ICONS/anbled-screen.png" "${XDG_CONFIG_HOME:-$HOME/.config}/autostart/anbled-screen.desktop"
  rm -rf "$SHARE/anbled-screen"
  systemctl --user daemon-reload 2>/dev/null || true
  bold "Anbled Screen is uninstalled."
  echo "Firewall openings (SSDP, TCP 49494) were left as they are."
}

vlc_hint() {
  local id=""
  [ -r /etc/os-release ] && id="$(. /etc/os-release; echo "${ID:-} ${ID_LIKE:-}")"
  case "$id" in
    *fedora*|*rhel*|*centos*) echo "  sudo dnf install vlc";;
    *debian*|*ubuntu*)        echo "  sudo apt install vlc";;
    *arch*)                   echo "  sudo pacman -S vlc";;
    *suse*)                   echo "  sudo zypper install vlc";;
  esac
  echo "  flatpak install flathub org.videolan.VLC"
}

[ "${1:-}" = "--uninstall" ] && { uninstall; exit 0; }

# ---- what this computer needs ----
command -v systemctl >/dev/null || fail "systemd is needed (systemctl wasn't found)."
command -v python3 >/dev/null || fail "Python 3.9 or newer is needed."
python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' || fail "Python 3.9 or newer is needed (found $(python3 -V))."
if ! { command -v flatpak >/dev/null && flatpak info org.videolan.VLC >/dev/null 2>&1; } && ! command -v vlc >/dev/null; then
  echo "VLC plays what phones cast, and it isn't installed. Install it with one of:"
  vlc_hint
  fail "install VLC, then run this again."
fi

# ---- the files: from this folder, or downloaded ----
SRC="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
if [ -z "$SRC" ] || [ ! -f "$SRC/anbled_screen.py" ]; then
  TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
  echo "Downloading Anbled Screen ($BRANCH)..."
  URL="$REPO/archive/refs/heads/$BRANCH.tar.gz"
  if command -v curl >/dev/null; then curl -fsSL "$URL" | tar -xz -C "$TMP"
  elif command -v wget >/dev/null; then wget -qO- "$URL" | tar -xz -C "$TMP"
  else fail "curl or wget is needed to download it."
  fi
  SRC="$(find "$TMP" -mindepth 1 -maxdepth 1 -type d | head -1)"
  [ -f "$SRC/anbled_screen.py" ] || fail "the download didn't contain Anbled Screen."
fi

# ---- install ----
install -Dm644 "$SRC/anbled_screen.py"      "$SHARE/anbled-screen/anbled_screen.py"
install -Dm755 "$SRC/anbled-screen"         "$BIN/anbled-screen"
install -Dm644 "$SRC/anbled-screen.service" "$UNITS/anbled-screen.service"
install -Dm644 "$SRC/anbled-screen.png"     "$ICONS/anbled-screen.png"
mkdir -p "$APPS"
cat > "$APPS/anbled-screen.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=Anbled Screen
GenericName=Cast receiver
Comment=Cast songs and videos from the Anbled app to this computer
Exec=$BIN/anbled-screen
Icon=anbled-screen
Terminal=false
Categories=AudioVideo;Audio;Video;Player;
Keywords=anbled;cast;dlna;upnp;screen;tv;receiver;
Actions=off;autostart;no-autostart;

[Desktop Action off]
Name=Turn off
Exec=$BIN/anbled-screen off

[Desktop Action autostart]
Name=Start at every login
Exec=$BIN/anbled-screen autostart

[Desktop Action no-autostart]
Name=Don't start at login
Exec=$BIN/anbled-screen no-autostart
DESKTOP
systemctl --user daemon-reload
command -v update-desktop-database >/dev/null && update-desktop-database "$APPS" 2>/dev/null || true
command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -f -t "$SHARE/icons/hicolor" 2>/dev/null || true
# Already running (an update): the new version takes over now.
systemctl --user is-active --quiet anbled-screen.service && systemctl --user restart anbled-screen.service || true

bold "Anbled Screen is installed."
echo "Open \"Anbled Screen\" from your applications menu, or run:  anbled-screen"
case ":$PATH:" in *":$BIN:"*) ;; *) echo "(Add $BIN to your PATH to run it from a terminal.)";; esac

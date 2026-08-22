#!/usr/bin/env bash
# Install roku-monitor-linux as a systemd --user service for the current user.
#   ./install.sh [--copy] [--no-discover] [--no-enable]
# --copy        copy the script into ~/.local/bin instead of symlinking the clone
# --no-discover skip the interactive "which TV?" step
# --no-enable   install files only; do not enable/start the service
# No sudo is used anywhere; everything lands under $HOME.
set -euo pipefail

APP=roku-monitor-linux
REPO_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
BIN_DIR=${XDG_BIN_HOME:-$HOME/.local/bin}
CONF_DIR=${XDG_CONFIG_HOME:-$HOME/.config}/$APP
UNIT_DIR=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user
CONF="$CONF_DIR/env"
COPY=0 DISCOVER=1 ENABLE=1

for arg in "$@"; do
  case "$arg" in
    --copy) COPY=1 ;;
    --no-discover) DISCOVER=0 ;;
    --no-enable) ENABLE=0 ;;
    -h|--help) sed -n '2,8p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

say()  { printf '\033[1m%s\033[0m\n' "$*"; }
warn() { printf '\033[33mwarning:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

# --- prerequisites ----------------------------------------------------------
command -v /usr/bin/python3 >/dev/null || die "/usr/bin/python3 not found (apt install python3)"
if ! /usr/bin/python3 -c 'import gi; gi.require_version("Gio","2.0"); from gi.repository import Gio, GLib' 2>/dev/null; then
  warn "python3-gi is not installed: the TV will still follow your displays, but it cannot be"
  warn "turned off *before* suspend/shutdown. Fix: sudo apt install python3-gi gir1.2-glib-2.0"
fi
command -v systemctl >/dev/null || die "systemctl not found; this installer needs systemd --user"
systemctl --user show-environment >/dev/null 2>&1 || die "systemd --user is not reachable. Run this from a desktop session (not plain SSH)."
[ -d /sys/class/drm ] || die "/sys/class/drm is missing; this needs a Linux DRM/KMS graphics driver"
if ! /usr/bin/python3 "$REPO_DIR/roku_monitor.py" status 2>/dev/null | grep -q '<- Roku'; then
  warn "no connected display with a Roku EDID was found right now. If the TV is off or on another"
  warn "input that is normal; otherwise set ROKU_CONNECTOR=<name> (see: ls /sys/class/drm) in $CONF"
fi

# --- files -------------------------------------------------------------------
mkdir -p "$BIN_DIR" "$CONF_DIR" "$UNIT_DIR"
chmod +x "$REPO_DIR/roku_monitor.py"
if [ "$COPY" = 1 ]; then
  install -m 755 "$REPO_DIR/roku_monitor.py" "$BIN_DIR/$APP"
else
  ln -sfn "$REPO_DIR/roku_monitor.py" "$BIN_DIR/$APP"
fi
install -m 644 "$REPO_DIR/$APP.service" "$UNIT_DIR/$APP.service"
case ":$PATH:" in *":$BIN_DIR:"*) ;; *) warn "$BIN_DIR is not on your PATH (the service does not care; the CLI does)";; esac

if [ ! -f "$CONF" ]; then
  install -m 600 "$REPO_DIR/.env.example" "$CONF"
  say "created $CONF from .env.example"
else
  say "keeping existing $CONF"
fi

# --- which TV? ---------------------------------------------------------------
set_conf() { # key value  (keys are fixed names from .env.example, so sed on ^KEY= is safe)
  if grep -q "^$1=" "$CONF"; then sed -i "s|^$1=.*|$1=$2|" "$CONF"; else printf '%s=%s\n' "$1" "$2" >>"$CONF"; fi
}
if [ "$DISCOVER" = 1 ] && [ -t 0 ] && [ -t 1 ]; then
  say "Looking for Roku TVs on your network (turn the TV on first)..."
  tmp=$(mktemp)
  /usr/bin/python3 "$REPO_DIR/roku_monitor.py" discover | tee "$tmp" || true
  # rows look like: " 1  192.168.x.y  Name  TV  SERIAL  MAC  PowerOn"
  mapfile -t rows < <(grep -E '^ *[0-9]+ +[0-9.]+ ' "$tmp" || true)
  rm -f "$tmp"
  if [ "${#rows[@]}" -gt 0 ]; then
    printf 'Which one is the TV you use as a monitor? [1-%d, or s to skip] ' "${#rows[@]}"
    read -r pick
    if [[ "$pick" =~ ^[0-9]+$ ]] && [ "$pick" -ge 1 ] && [ "$pick" -le "${#rows[@]}" ]; then
      row=${rows[$((pick-1))]}
      ip=$(awk '{print $2}' <<<"$row")
      # serial and MAC are the last three fields before the power column
      serial=$(awk '{print $(NF-2)}' <<<"$row")
      mac=$(awk '{print $(NF-1)}' <<<"$row")
      set_conf ROKU_TV_IP "$ip"
      [[ "$serial" =~ ^[A-Za-z0-9]+$ ]] && set_conf ROKU_TV_SERIAL "$serial"
      [[ "$mac" =~ ^([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$ ]] && set_conf ROKU_TV_MAC "$mac"
      printf 'Which input is the PC on? [hdmi1/hdmi2/hdmi3/hdmi4/av1] (hdmi3) '
      read -r inp
      inp=${inp:-hdmi3}
      set_conf ROKU_INPUT "$inp"
      say "wrote ROKU_TV_IP=$ip ROKU_INPUT=$inp to $CONF"
    else
      say "skipped; edit $CONF by hand (ROKU_TV_IP is required)"
    fi
  else
    warn "no Roku answered; turn the TV on and run: $APP discover  — then edit $CONF"
  fi
fi

# --- service -----------------------------------------------------------------
systemctl --user daemon-reload
if command -v systemd-analyze >/dev/null; then
  # verify loads every unit it can see; only our own unit's complaints matter
  systemd-analyze --user verify "$UNIT_DIR/$APP.service" 2>&1 | grep "$APP" || true
fi
if [ "$ENABLE" = 1 ]; then
  systemctl --user enable --now "$APP.service"
  sleep 1
  systemctl --user --no-pager --lines=8 status "$APP.service" || true
else
  say "installed but not enabled. Enable with: systemctl --user enable --now $APP"
fi

say "done."
echo "  config:  $CONF"
echo "  logs:    journalctl --user -u $APP -f"
echo "  status:  $APP status"

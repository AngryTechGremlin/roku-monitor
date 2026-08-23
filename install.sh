#!/usr/bin/env bash
# Install roku-monitor as a systemd --user service for the current user.
#   ./install.sh [--copy] [--no-discover] [--no-enable] [--volume]
# --copy        copy the script into ~/.local/bin instead of symlinking the clone
# --no-discover skip the interactive "which TV?" step
# --no-enable   install files only; do not enable/start the service
# --volume      also make the PC's volume control drive the TV's volume: adds a
#               PipeWire "Roku TV" output (drop-in, restarts PipeWire once) and
#               sets ROKU_VOLUME=true. Needs pw-dump/wpctl and the TV on.
# No sudo is used anywhere; everything lands under $HOME.
set -euo pipefail

APP=roku-monitor
REPO_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
BIN_DIR=${XDG_BIN_HOME:-$HOME/.local/bin}
CONF_DIR=${XDG_CONFIG_HOME:-$HOME/.config}/$APP
UNIT_DIR=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user
CONF="$CONF_DIR/env"
PW_CONF_DIR=${XDG_CONFIG_HOME:-$HOME/.config}/pipewire/pipewire.conf.d
PW_CONF="$PW_CONF_DIR/90-$APP.conf"
COPY=0 DISCOVER=1 ENABLE=1 VOLUME=0

for arg in "$@"; do
  case "$arg" in
    --copy) COPY=1 ;;
    --no-discover) DISCOVER=0 ;;
    --no-enable) ENABLE=0 ;;
    --volume) VOLUME=1 ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
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
if [ "$VOLUME" = 1 ] && { ! command -v pw-dump >/dev/null || ! command -v wpctl >/dev/null; }; then
  die "--volume needs PipeWire and WirePlumber (pw-dump, wpctl): sudo apt install pipewire-bin wireplumber"
fi
# capture first: status exits 1 when no TV is configured yet, which must not trip pipefail here
status_out=$(/usr/bin/python3 "$REPO_DIR/roku_monitor.py" status 2>/dev/null || true)
if ! grep -q '<- Roku' <<<"$status_out"; then
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
  case "$2" in *[\|\&\\]*|*$'\n'*) die "refusing to write $1: value contains | & \\ or a newline" ;; esac
  if grep -q "^$1=" "$CONF"; then sed -i "s|^$1=.*|$1=$2|" "$CONF"; else printf '%s=%s\n' "$1" "$2" >>"$CONF"; fi
}
ask_input() { # prompt for the HDMI input and store it
  while :; do
    printf 'Which input is the PC on? [hdmi1/hdmi2/hdmi3/hdmi4/av1/tuner] (hdmi3) '
    read -r inp
    inp=${inp:-hdmi3}; inp=${inp,,}; inp=${inp// /}   # 'HDMI 3' -> hdmi3
    [[ "$inp" =~ ^[1-4]$ ]] && inp=hdmi$inp            # '3' -> hdmi3
    case "$inp" in hdmi[1-4]|av1|tuner) break ;; *) warn "'$inp' is not one of hdmi1..4/av1/tuner" ;; esac
  done
  set_conf ROKU_INPUT "$inp"
}

by_ip() { # ask for an address and read the TV directly; returns 1 if nothing answered
  printf "TV's IP address (or blank to skip): "
  read -r manual
  [ -n "$manual" ] || return 1
  out=$(/usr/bin/python3 "$REPO_DIR/roku_monitor.py" discover --ip "$manual") || { echo "$out"; return 1; }
  echo "$out"
  ip=$(sed -n 's/^ *ROKU_TV_IP=//p' <<<"$out")
  serial=$(sed -n 's/^ *ROKU_TV_SERIAL=//p' <<<"$out")
  mac=$(sed -n 's/^ *ROKU_TV_MAC=//p' <<<"$out")
  [ -n "$ip" ] || return 1
  set_conf ROKU_TV_IP "$ip"
  [[ "$serial" =~ ^[A-Za-z0-9]{6,}$ ]] && set_conf ROKU_TV_SERIAL "$serial"
  [[ "$mac" =~ ^([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$ ]] && set_conf ROKU_TV_MAC "$mac"
  ask_input
  say "wrote ROKU_TV_IP=$ip ROKU_INPUT=$inp to $CONF"
}

if [ "$DISCOVER" = 1 ] && [ -t 0 ] && [ -t 1 ]; then
  say "Looking for Roku TVs on your network (turn the TV on first)..."
  tmp=$(mktemp)
  /usr/bin/python3 "$REPO_DIR/roku_monitor.py" discover | tee "$tmp" || true
  # rows look like: " 1  <ip>  Name  TV  SERIAL  MAC  PowerOn" (empty cells print as '-')
  mapfile -t rows < <(grep -E '^ *[0-9]+ +[0-9.]+ ' "$tmp" | grep -v '(no ECP answer' || true)
  rm -f "$tmp"
  if [ "${#rows[@]}" -gt 0 ]; then
    printf 'Which one is the TV you use as a monitor? [1-%d, i to type an IP, or s to skip] ' "${#rows[@]}"
    read -r pick
    if [[ "$pick" =~ ^[0-9]+$ ]] && [ "$pick" -ge 1 ] && [ "$pick" -le "${#rows[@]}" ]; then
      row=${rows[$((pick-1))]}
      ip=$(awk '{print $2}' <<<"$row")
      # serial and MAC are the last three fields before the power column
      serial=$(awk '{print $(NF-2)}' <<<"$row")
      mac=$(awk '{print $(NF-1)}' <<<"$row")
      set_conf ROKU_TV_IP "$ip"
      [[ "$serial" =~ ^[A-Za-z0-9]{6,}$ ]] && set_conf ROKU_TV_SERIAL "$serial"
      [[ "$mac" =~ ^([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$ ]] && set_conf ROKU_TV_MAC "$mac"
      ask_input
      say "wrote ROKU_TV_IP=$ip ROKU_INPUT=$inp to $CONF"
    elif [ "$pick" = i ] || [ "$pick" = I ]; then
      by_ip || say "skipped; edit $CONF by hand (ROKU_TV_IP is required)"
    else
      say "skipped; edit $CONF by hand (ROKU_TV_IP is required)"
    fi
  else
    # Discovery is multicast; some networks never deliver it to a given TV,
    # so offer the address directly rather than sending people to a text editor.
    warn "no Roku answered the search (some networks block multicast)."
    by_ip || warn "edit $CONF by hand: ROKU_TV_IP is required"
  fi
fi

# --- volume mirror (optional) --------------------------------------------------
# A PipeWire drop-in adds a "Roku TV" output whose slider the daemon mirrors to
# the TV; the relay inside it must name the HDMI sink that feeds the TV, which
# `status` prints while that sink exists (the TV on, or in Fast-start standby).
pw_changed=0 conf_changed=0
if [ "$VOLUME" = 1 ]; then
  # status prints "Audio sink to TV: <node.name> (<level>)"; the not-found shape
  # starts its text with "(" so it can never be mistaken for a name.
  sink=$(sed -n 's/^Audio sink to TV: \([A-Za-z0-9._-]\{1,\}\) (.*/\1/p' <<<"$status_out" | head -1)
  if [ -z "$sink" ] && [ -t 0 ] && [ -t 1 ]; then
    warn "no audio sink named after the Roku is present right now (it appears while the TV asserts HDMI hot-plug)."
    printf "PipeWire node.name of the HDMI output that feeds the TV (see: wpctl status -n), or blank to skip: "
    read -r sink
  fi
  if [ -z "$sink" ]; then
    warn "volume mirror not installed. Turn the TV on and re-run: ./install.sh --volume"
  else
    [[ "$sink" =~ ^[A-Za-z0-9._-]+$ ]] || die "unexpected sink name: $sink"
    mkdir -p "$PW_CONF_DIR"
    tmp=$(mktemp "$PW_CONF_DIR/.90-$APP.XXXXXX")
    sed "s|@ROKU_SINK@|$sink|" "$REPO_DIR/$APP.pipewire.conf" >"$tmp"
    if [ -f "$PW_CONF" ] && cmp -s "$tmp" "$PW_CONF"; then
      rm -f "$tmp"
    else
      mv "$tmp" "$PW_CONF"; pw_changed=1
    fi
    # PipeWire reads every drop-in at start and a broken one can mean no sound at
    # all, so make sure the merged config still parses and contains our output.
    if command -v pw-config >/dev/null && ! pw-config -N -r merge context.objects 2>/dev/null | grep -q '"roku-tv"'; then
      rm -f "$PW_CONF"
      die "PipeWire did not accept $PW_CONF (removed again). Please report the output of: pw-config -N -r merge context.objects"
    fi
    if ! grep -q '^ROKU_VOLUME=true$' "$CONF"; then set_conf ROKU_VOLUME true; conf_changed=1; fi
    say "wrote $PW_CONF (relay into $sink) and ROKU_VOLUME=true"
    if [ "$pw_changed" = 1 ]; then
      warn "restarting PipeWire to load it (sound pauses for a second)"
      systemctl --user restart pipewire.service || warn "could not restart PipeWire; log out and in again to load it"
    fi
  fi
fi

# --- service -----------------------------------------------------------------
was_active=0; systemctl --user is-active --quiet "$APP.service" && was_active=1
systemctl --user daemon-reload
if command -v systemd-analyze >/dev/null; then
  # verify loads every unit it can see; only our own unit's complaints matter
  systemd-analyze --user verify "$UNIT_DIR/$APP.service" 2>&1 | grep "$APP" || true
fi
if [ "$ENABLE" = 1 ]; then
  systemctl --user enable --now "$APP.service"
  if [ "$was_active" = 1 ] && { [ "$pw_changed" = 1 ] || [ "$conf_changed" = 1 ]; }; then
    # a running daemon only reads its config at start (expect the TV to blink off/on)
    systemctl --user restart "$APP.service"
  fi
  sleep 1
  systemctl --user --no-pager --lines=8 status "$APP.service" || true
else
  say "installed but not enabled. Enable with: systemctl --user enable --now $APP"
fi

say "done."
echo "  config:  $CONF"
if [ "$VOLUME" = 1 ] && [ -f "$PW_CONF" ]; then
  echo "  volume:  $PW_CONF (the 'Roku TV' output; the daemon makes it the default)"
fi
echo "  logs:    journalctl --user -u $APP -f"
echo "  status:  $APP status"

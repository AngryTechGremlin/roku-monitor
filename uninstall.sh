#!/usr/bin/env bash
# Remove the roku-monitor-linux user service and CLI shim.
#   ./uninstall.sh [--purge]     --purge also deletes ~/.config/roku-monitor-linux
# Note: stopping the service turns the TV off (if ROKU_OFF_ON_STOP is true).
set -euo pipefail

APP=roku-monitor-linux
BIN_DIR=${XDG_BIN_HOME:-$HOME/.local/bin}
CONF_DIR=${XDG_CONFIG_HOME:-$HOME/.config}/$APP
UNIT_DIR=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user
PURGE=0
[ "${1:-}" = "--purge" ] && PURGE=1

systemctl --user disable --now "$APP.service" 2>/dev/null || true
rm -f "$UNIT_DIR/$APP.service"
systemctl --user daemon-reload 2>/dev/null || true
rm -f "$BIN_DIR/$APP"
if [ "$PURGE" = 1 ]; then
  rm -rf "$CONF_DIR"
  echo "removed service, CLI shim and $CONF_DIR"
else
  echo "removed service and CLI shim; kept $CONF_DIR (use --purge to delete it)"
fi

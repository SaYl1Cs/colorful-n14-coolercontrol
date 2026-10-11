#!/usr/bin/env bash
# Install the guarded Colorful N14 CoolerControl integration.
set -euo pipefail
umask 077

if [[ ${EUID} -ne 0 ]]; then
  echo 'Run this installer with sudo: sudo ./install.sh' >&2
  exit 1
fi

ROOT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PREFIX=/usr/local/share/colorful-n14-control
ADAPTER_TARGET=/usr/local/libexec/colorful-n14-coolercontrol
PLUGIN_DIR=/etc/coolercontrol/plugins/custom-device
BACKUP_ROOT=/var/backups/colorful-n14-control/$(date +%Y%m%d-%H%M%S)
PREPARE_UNIT=colorful-n14-nbfc-prepare.service
SENSORS_UNIT=colorful-n14-sensors.service
ADAPTER_UNIT=colorful-n14-adapter.service
DROPIN_DIR=/etc/systemd/system/coolercontrold.service.d
LEGACY_DROPIN="$DROPIN_DIR/n14.conf"

fail() {
  echo "STOP: $*" >&2
  exit 1
}

for command in python3 systemctl install modprobe cc flock; do
  command -v "$command" >/dev/null || fail "required command is missing: $command"
done

if ! command -v pacman >/dev/null; then
  fail 'this installer targets Arch-based systems and requires pacman'
fi

if ! command -v coolercontrold >/dev/null || [[ ! -d "$PLUGIN_DIR" ]]; then
  cat >&2 <<'EOF'
CoolerControl with the custom-device plugin is not installed.
Install it first using your distribution's packages, for example:
  sudo pacman -S --needed coolercontrol
Then run this installer again.
EOF
  exit 1
fi

if systemctl is-active --quiet nbfc_service.service || pgrep -x nbfc_service >/dev/null 2>&1; then
  fail 'NBFC is active; stop it before installing the CoolerControl backend'
fi

[[ -f "$ROOT_DIR/coolercontrol_adapter.py" ]] || fail 'coolercontrol_adapter.py is missing'
[[ -f "$ROOT_DIR/coolercontrol-config.json" ]] || fail 'coolercontrol-config.json is missing'
python3 -m py_compile \
  "$ROOT_DIR/prepare.py" \
  "$ROOT_DIR/prepare-autostart.py" \
  "$ROOT_DIR/sensor_cache.py" \
  "$ROOT_DIR/coolercontrol_adapter.py"
python3 -m json.tool "$ROOT_DIR/coolercontrol-config.json" >/dev/null
python3 "$ROOT_DIR/prepare.py" check >/dev/null || fail 'this machine does not match the pinned N14 24 BIOS/DSDT checks'
exec 9>/run/colorful-n14-install.lock
flock -n 9 || fail 'another installer is running'
STAGING=$(mktemp -d)
trap 'rm -rf -- "$STAGING"' EXIT
cc -std=c11 -O2 -Wall -Wextra -Werror -D_FORTIFY_SOURCE=2 -fstack-protector-strong \
  "$ROOT_DIR/src/adapter_client.c" -o "$STAGING/adapter-client"
# Only replace our own device definition. Preserve other custom devices/settings.
python3 - "$ROOT_DIR/coolercontrol-config.json" "$PLUGIN_DIR/config.json" "$STAGING/config.json" <<'PY'
import json, pathlib, sys
source, target, output = map(pathlib.Path, sys.argv[1:])
ours = json.loads(source.read_text())['devices'][0]
data = json.loads(target.read_text()) if target.exists() else {'devices': []}
data['devices'] = [ours if d.get('id') == ours['id'] else d for d in data['devices']]
if not any(d.get('id') == ours['id'] for d in data['devices']):
    data['devices'].append(ours)
output.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
PY

backup_path() {
  local path=$1
  if [[ -e "$path" || -L "$path" ]]; then
    mkdir -p "$BACKUP_ROOT$(dirname -- "$path")"
    cp -a --no-dereference "$path" "$BACKUP_ROOT$path"
  fi
}

mkdir -p -m 0700 "$BACKUP_ROOT"
backup_path "$PREFIX"
backup_path /etc/coolercontrol/config.toml
backup_path /etc/coolercontrol/config-ui.json
backup_path "$ADAPTER_TARGET"
backup_path "/etc/systemd/system/$ADAPTER_UNIT"
backup_path "$PLUGIN_DIR/config.json"
backup_path "/etc/systemd/system/$PREPARE_UNIT"
backup_path "/etc/systemd/system/$SENSORS_UNIT"
backup_path "$DROPIN_DIR/colorful-n14-control.conf"
backup_path "$LEGACY_DROPIN"
backup_path "/etc/modules-load.d/colorful-n14-control.conf"

# Stop control first while the OLD backend and caches still work. Its reset
# requests full fans. Do not copy a partial backend underneath live processes.
systemctl stop coolercontrold.service
if systemctl is-active --quiet "$ADAPTER_UNIT"; then
  systemctl stop "$ADAPTER_UNIT"
fi
systemctl stop "$SENSORS_UNIT" || true

# Older revisions installed an invalid n14.conf with Conflicts in [Service].
# Remove only that known integration file; preserve unrelated administrator drop-ins.
if [[ -f "$LEGACY_DROPIN" ]] && grep -q 'colorful-n14-nbfc-prepare.service' "$LEGACY_DROPIN"; then
  rm -f "$LEGACY_DROPIN"
fi

install -d -m 0755 "$PREFIX/configs" "$PREFIX/src" /usr/local/libexec
install -m 0644 "$ROOT_DIR/build_profile.py" "$PREFIX/build_profile.py"
install -m 0644 "$ROOT_DIR/curves.py" "$PREFIX/curves.py"
install -m 0644 "$ROOT_DIR/curves.json" "$PREFIX/curves.json"
install -m 0644 "$ROOT_DIR/prepare.py" "$PREFIX/prepare.py"
install -m 0755 "$ROOT_DIR/prepare-autostart.py" "$PREFIX/prepare-autostart.py"
install -m 0755 "$ROOT_DIR/sensor_cache.py" "$PREFIX/sensor_cache.py"
install -m 0644 "$ROOT_DIR/configs/Colorful N14 24 - Temperature Curves - GPU Command Readback.json" \
  "$PREFIX/configs/Colorful N14 24 - Temperature Curves - GPU Command Readback.json"
install -m 0644 "$ROOT_DIR/src/transport.lua" "$PREFIX/src/transport.lua"
for module in coolercontrol_adapter.py adapter_server.py thermal_state.py; do
  install -m 0644 "$ROOT_DIR/$module" "$PREFIX/$module"
done
install -m 0755 "$STAGING/adapter-client" "$ADAPTER_TARGET.new"
mv -f "$ADAPTER_TARGET.new" "$ADAPTER_TARGET"
install -d -m 0755 "$PLUGIN_DIR"
install -m 0600 "$STAGING/config.json" "$PLUGIN_DIR/config.json"
install -m 0644 "$ROOT_DIR/colorful-n14-adapter.service" "/etc/systemd/system/$ADAPTER_UNIT"
install -m 0644 "$ROOT_DIR/colorful-n14-nbfc-prepare.service" "/etc/systemd/system/$PREPARE_UNIT"
install -m 0644 "$ROOT_DIR/colorful-n14-sensors.service" "/etc/systemd/system/$SENSORS_UNIT"
install -d -m 0755 "$DROPIN_DIR" /etc/modules-load.d
install -m 0644 "$ROOT_DIR/coolercontrold-n14.conf" "$DROPIN_DIR/colorful-n14-control.conf"
printf '%s\n' acpi_call > /etc/modules-load.d/colorful-n14-control.conf

systemctl daemon-reload
systemctl enable "$PREPARE_UNIT" "$SENSORS_UNIT" "$ADAPTER_UNIT" >/dev/null
systemctl restart "$PREPARE_UNIT"
systemctl restart "$SENSORS_UNIT"
systemctl start "$ADAPTER_UNIT"
systemctl start coolercontrold.service

printf '\nInstalled Colorful N14 CoolerControl integration.\n'
printf 'Backup: %s\n' "$BACKUP_ROOT"
printf 'Sensor cache: %s\n' "$PREFIX/sensor_cache.py"
printf 'Check status with: systemctl --no-pager status %s %s %s coolercontrold.service\n' "$PREPARE_UNIT" "$SENSORS_UNIT" "$ADAPTER_UNIT"
printf '%s\n' 'In CoolerControl, create CPU and GPU graph profiles, then a Maximum mix profile and assign it to Both Fans.'
printf '%s\n' 'The UI 0% command maps to raw 1; raw 0 is intentionally never written.'

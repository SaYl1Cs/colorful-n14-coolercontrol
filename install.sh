#!/usr/bin/env bash
# Transactional upgrade only. No package installs, reboot, module unload or global EC write-policy change.
set -Eeuo pipefail
umask 077
export PYTHONDONTWRITEBYTECODE=1

fail() { echo "STOP: preflight refusal: $*; installed cooling has not been changed." >&2; exit 1; }
# Builtins only until dependencies/root have been checked: safe in incomplete offline environments.
for command in python3 systemctl timeout flock cp; do
  command -v "$command" >/dev/null || fail "required command is missing: $command"
done
[[ $EUID == 0 ]] || fail 'root required; run sudo bash install.sh'
SOURCE_SCRIPT=${BASH_SOURCE[0]}
[[ $SOURCE_SCRIPT == */* ]] || SOURCE_SCRIPT=./$SOURCE_SCRIPT
ROOT_DIR=$(cd -- "${SOURCE_SCRIPT%/*}" && pwd)
PREFIX=/usr/local/share/colorful-n14-control
CLIENT=/usr/local/libexec/colorful-n14-coolercontrol
PREPARE_UNIT=colorful-n14-nbfc-prepare.service
SENSORS_UNIT=colorful-n14-sensors.service
ADAPTER_UNIT=colorful-n14-adapter.service
SELECTOR_UNIT=colorful-n14-profile-selector.service
PLUGIN_UNIT=cc-plugin-custom-device.service
PREREQUISITE_UNIT=colorful-n14-ec-readonly.service

ctl() { timeout --kill-after=2s 25s systemctl "$@"; }

# Shared by the ERR trap, external timer, and deterministic manual rollback.
# Backup code is self-contained and is never imported from the installed prefix.
recover_backup() {
  local backup=$1 units unit
  local tx="$backup/recovery/install_transaction.py"
  python3 -B "$tx" validate "$backup" || return 1
  units=$(python3 -B "$tx" units "$backup") || return 1
  # Stop selector/daemon/plugin before reset so no producer can queue a lower duty.
  # Absent old selector is expected on first upgrade; a loaded unit must stop cleanly.
  if [[ $(ctl show "$SELECTOR_UNIT" -p LoadState --value) != not-found ]]; then
    ctl stop "$SELECTOR_UNIT" || return 1
  fi
  ctl stop coolercontrold.service || return 1
  if [[ $(ctl show "$PLUGIN_UNIT" -p LoadState --value) != not-found ]]; then
    ctl stop "$PLUGIN_UNIT" || return 1
  fi
  if ctl is-active --quiet "$ADAPTER_UNIT"; then
    timeout --kill-after=1s 5s "$CLIENT" reset >/dev/null || return 1
  fi
  ctl stop "$ADAPTER_UNIT" || return 1
  ctl stop "$SENSORS_UNIT" || return 1
  ctl stop "$PREPARE_UNIT" || return 1
  if [[ $(ctl show "$PREREQUISITE_UNIT" -p LoadState --value) != not-found ]]; then
    ctl stop "$PREREQUISITE_UNIT" || return 1
  fi
  python3 -B "$tx" restore "$backup" || return 1
  ctl daemon-reload || return 1
  # Only originally active units; no enable/disable or global target changes.
  while IFS= read -r unit; do
    [[ -n $unit ]] || continue
    ctl start "$unit" || return 1
  done <<< "$units"
  printf 'restored\n' > "$backup/result"
  echo "Previous installation restored; backup retained: $backup" >&2
}

# --restore is usable even after commit. --recover is the automatic timer path
# and does not undo a successfully committed or already rolled-back transaction.
if [[ ${1:-} == --restore || ${1:-} == --recover ]]; then
  ACTION=$1
  BACKUP_ROOT=${2:?backup directory required}
  [[ $BACKUP_ROOT == /var/backups/colorful-n14-control/* && -d $BACKUP_ROOT/recovery ]] || exit 1
  if [[ $ACTION == --recover ]]; then
    [[ ! -f $BACKUP_ROOT/result ]] || exit 0
    # Verify PID start-time before signalling: never kill a recycled PID.
    python3 -B "$BACKUP_ROOT/recovery/install_transaction.py" interrupt "$BACKUP_ROOT"
  fi
  exec 9>/run/colorful-n14-install.lock
  flock -w 90 9 || { echo 'Recovery cannot acquire installer lock; no files restored under live installer.' >&2; exit 1; }
  if [[ $ACTION == --recover && -f $BACKUP_ROOT/result ]]; then exit 0; fi
  recover_backup "$BACKUP_ROOT"
  exit $?
fi

for command in mktemp rm cat cc ldd modprobe lua5.4 systemd-run; do
  command -v "$command" >/dev/null || fail "required deployment command is missing: $command"
done
MODE=install
PLUGIN_ARGS=()
while (($#)); do
  case $1 in
    --preflight) MODE=preflight; shift ;;
    --plugin-dir) [[ $# -ge 2 ]] || fail '--plugin-dir requires an absolute custom-device directory'; PLUGIN_ARGS=(--plugin-dir "$2"); shift 2 ;;
    *) fail "unknown option: $1" ;;
  esac
done
command -v systemd-run >/dev/null || fail 'systemd-run is required for independent timed recovery'
python3 -B -c 'import tomllib; import sys; assert sys.version_info >= (3, 11)' || fail 'Python 3.11+ required'
# These queries and checks do not load modules, sample EC bytes or change flags.
modprobe --show-depends acpi_call >/dev/null || fail 'acpi_call module unavailable for preparation on boot'
modprobe --show-depends ec_sys >/dev/null || fail 'ec_sys module unavailable on boot'
exec 9>/run/colorful-n14-install.lock
flock -n 9 || fail 'another installer/recovery is running'
STAGING=$(mktemp -d /run/colorful-n14-install.XXXXXXXX)
trap 'rm -rf -- "$STAGING"' EXIT
python3 -B "$ROOT_DIR/install_transaction.py" stage "$STAGING" --source "$ROOT_DIR" "${PLUGIN_ARGS[@]}" || fail 'release/dependency/path staging failed'
SOURCE="$STAGING/source"
TX="$SOURCE/install_transaction.py"
# Run the staged, hermetic offline suite; no live API/EC/service mutation in tests.
PYTHONPATH="$SOURCE:$SOURCE/tests" python3 -B -m unittest discover -s "$SOURCE/tests" -v >"$STAGING/tests.log" 2>&1 || {
  cat "$STAGING/tests.log" >&2
  fail 'offline regression suite failed'
}
python3 -B "$SOURCE/prepare.py" check >/dev/null || fail 'pinned N14 BIOS/DSDT identity check failed'
python3 -B "$SOURCE/install_prerequisites.py" || fail 'mounted debugfs/read-only access/NBFC check failed'
[[ -e /proc/acpi/call ]] || fail 'acpi_call must already be loaded for the running backend'
# The capture validates root-owned 0700 token parent and root0600 credential,
# authenticates to the LIVE OLD API, validates profiles, and prints IDs only.
timeout --kill-after=1s 30s python3 -B "$SOURCE/capture_profile_bindings.py" >"$STAGING/profile-bindings.json" || fail 'authenticated live profile binding capture failed'
timeout --kill-after=1s 30s python3 -B "$TX" check-bindings "$STAGING/profile-bindings.json" || fail 'live profile policy/poll-rate validation failed'
cc -std=c11 -O2 -Wall -Wextra -Werror -D_FORTIFY_SOURCE=2 -fstack-protector-strong \
  "$SOURCE/src/adapter_client.c" -o "$STAGING/adapter-client" || fail 'C client compilation failed'
# No lower-duty requests occur during preflight. Never overwrite/enroll the token.
if [[ $MODE == preflight ]]; then
  echo 'Preflight passed: patched releases, dependencies, offline tests, identity, mounts, token and live bindings. No cooling services or installed files changed.'
  exit 0
fi

BACKUP_ROOT=$(python3 -B -c 'import datetime,os; print("/var/backups/colorful-n14-control/" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + str(os.getppid()))')
python3 -B "$TX" snapshot "$BACKUP_ROOT" --source "$SOURCE" || fail 'complete backup failed'
cp -- "$STAGING/tests.log" "$BACKUP_ROOT/preflight-tests.log"
python3 -B "$TX" owner "$BACKUP_ROOT" --pid "$$"
RECOVERY_UNIT="colorful-n14-recovery-${BACKUP_ROOT##*/}"
printf '\nBackup: %s\nRollback: sudo bash %q --restore %q\n' "$BACKUP_ROOT" "$BACKUP_ROOT/recovery/install.sh" "$BACKUP_ROOT"
TRANSACTION_STARTED=0
rollback() {
  local status=$?
  trap - ERR INT TERM
  set +e
  if [[ $TRANSACTION_STARTED == 1 ]]; then
    if recover_backup "$BACKUP_ROOT"; then
      ctl stop "$RECOVERY_UNIT.timer" >/dev/null 2>&1
    else
      echo "STOP: rollback incomplete; recovery timer/backup retained: $BACKUP_ROOT" >&2
    fi
  else
    # Timer creation/check failed before the first service stop. Do not leave
    # a late timer to undo an installation that was never attempted.
    printf 'not-started\n' > "$BACKUP_ROOT/result"
    ctl stop "$RECOVERY_UNIT.timer" >/dev/null 2>&1
  fi
  exit "$((status == 0 ? 1 : status))"
}
trap rollback ERR
trap 'false' INT TERM
# Independent recovery is armed and checked BEFORE stopping cooling. FD 9 is
# explicitly not inherited by the transient service. No persistent unit enable.
systemd-run --quiet --unit="$RECOVERY_UNIT" --on-active=180s \
  --timer-property=AccuracySec=1s --property=Type=oneshot --property=TimeoutStartSec=300s \
  /bin/bash "$BACKUP_ROOT/recovery/install.sh" --recover "$BACKUP_ROOT" 9>&-
ctl is-active --quiet "$RECOVERY_UNIT.timer"
TRANSACTION_STARTED=1

if [[ $(ctl show "$SELECTOR_UNIT" -p LoadState --value) != not-found ]]; then
  ctl stop "$SELECTOR_UNIT"
fi
ctl stop coolercontrold.service
if [[ $(ctl show "$PLUGIN_UNIT" -p LoadState --value) != not-found ]]; then
  ctl stop "$PLUGIN_UNIT"
fi
# Revoke OLD backend control while OLD sensors and adapter are still alive.
# Older backends can request full on reset; the current backend does not write.
timeout --kill-after=1s 5s "$CLIENT" reset >/dev/null
ctl stop "$ADAPTER_UNIT"
ctl stop "$SENSORS_UNIT"
ctl stop "$PREPARE_UNIT"
timeout --kill-after=2s 60s python3 -B "$TX" apply "$STAGING" --backup "$BACKUP_ROOT"
ctl daemon-reload
ctl start "$PREPARE_UNIT"
ctl start "$SENSORS_UNIT"
ctl start "$ADAPTER_UNIT"
# CoolerControl creates and starts the managed plugin unit during startup.
# It removes that unit during shutdown; do not start it before the daemon.
# The cooler drop-in Wants selector; its After=coolercontrold ordering avoids a cycle.
ctl start coolercontrold.service
ctl is-active --quiet "$PLUGIN_UNIT"
ctl start "$SELECTOR_UNIT"
# 58s + 1s kill grace is strictly below the requested 60s wall-clock health cap.
timeout --kill-after=1s 58s python3 -B "$PREFIX/install_transaction.py" health
# Cancel timer before commit, then record success while holding the installer lock.
ctl stop "$RECOVERY_UNIT.timer"
printf 'committed\n' > "$BACKUP_ROOT/result"
TRANSACTION_STARTED=0
trap - ERR INT TERM
printf '\nInstalled EC release; global EC write policy preserved. No reboot required.\n'
printf 'Backup and deterministic rollback: sudo bash %q --restore %q\n' "$BACKUP_ROOT/recovery/install.sh" "$BACKUP_ROOT"
printf '%s\n' 'Stable device/profile IDs retained. CPU-only while GPU sleeps; Maximum CPU/GPU while awake. No emergency fan override; awaiting provenance leaves targets unchanged.'

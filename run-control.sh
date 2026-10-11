#!/usr/bin/env bash
# EXPERIMENTAL REAL FAN WRITES. Read README.md and complete read-only tests first.
# On graceful exit the profile requests BOTH fans FULL, not automatic EC mode.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
command -v python3 >/dev/null
test -x /usr/local/bin/nbfc_service
printf '\nWARNING: REAL FAN CONTROL. GPU RPM UNKNOWN; 40%% minimum; 0%% unverified.\n'
printf 'Ctrl+C requests BOTH fans FULL on graceful cleanup after control writes.\n'
printf 'Stay present. Do not suspend, hibernate, or stress-test the laptop.\n'
# prepare retains the exclusive session lock across exec of this fixed binary.
exec python3 prepare.py arm --launch /usr/local/bin/nbfc_service "$@"

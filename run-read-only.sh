#!/usr/bin/env bash
# Explicit diagnostics only: index-port selection occurs, no fan setting writes.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
command -v python3 >/dev/null
test -x /usr/local/bin/nbfc_service
printf '\nEXPLICIT HARDWARE DIAGNOSTICS, not offline. GPU command feedback is NOT RPM.\n'
# prepare retains the exclusive session lock across exec of this fixed binary.
exec python3 prepare.py probe --launch /usr/local/bin/nbfc_service "$@"

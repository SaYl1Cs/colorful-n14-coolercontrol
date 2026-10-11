#!/usr/bin/env bash
# Backwards-compatible entry point. The complete installer is install.sh.
set -euo pipefail
exec "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/install.sh" "$@"

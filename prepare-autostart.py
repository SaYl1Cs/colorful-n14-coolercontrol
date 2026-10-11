#!/usr/bin/env python3
"""Boot-time permission preparation for the CoolerControl N14 adapter.

This intentionally does not validate tachometer ranges: the GPU tachometer is
known to be unreliable on this machine. It still performs the exact DMI,
DSDT, WMI and EC identity checks, then creates the short-lived control ticket.
It never writes a fan command.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from prepare import (  # noqa: E402
    PERMISSION_DIR,
    assert_service_stopped,
    check_platform,
    packet,
    read_byte,
    session_lock,
    write_permission,
)

profile = check_platform()
with session_lock():
    assert_service_stopped()
    # Do not leave a previous boot's ticket usable after a failed probe.
    (PERMISSION_DIR / "permission").unlink(missing_ok=True)
    brand = (read_byte(packet(0x2E, 0x20)), read_byte(packet(0x2E, 0x21)))
    if brand != (0x55, 0x71):
        raise SystemExit(f"STOP: unexpected embedded-controller ID {brand[0]:02X}:{brand[1]:02X}")
    write_permission(brand, True, profile)
print("Control permission prepared for EC 55:71; no fan setting was written.")

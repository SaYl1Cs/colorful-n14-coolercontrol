#!/usr/bin/env python3
"""Root-only CoolerControl Custom Device adapter for Colorful N14 24.

This exposes one logical channel controlling both physical fans. GPU RPM is
never fabricated: get-rpm reports the CPU tachometer only, while CPU and GPU
temperatures are read from the fresh cache maintained by sensor_cache.py.
ACPI access is guarded by exact platform, controller, and temporary
control-permission checks.
"""
from __future__ import annotations

import hashlib
import os
import re
import sys
import time
from pathlib import Path
from typing import NoReturn

DMI = Path("/sys/class/dmi/id")
EXPECTED_DMI = {
    "sys_vendor": "COLORFUL",
    "product_name": "N14 24",
    "bios_version": "2.07",
}
EXPECTED_DSDT = "4aab3c89fdaac8ab9e20d31cff3fe0a18b83877442520eb294429ff9237bef06"
WMI_GUID = "3161c7c3-489f-4074-82ae-538101cce1c2"
EXPECTED_EC = (0x55, 0x71)
METHOD = r"\_SB.AMW0.WMBA 0 2 b"
PROC_CALL = Path("/proc/acpi/call")
PERMISSION = Path("/run/colorful-n14-control/permission")
IDENTITY_PORT = 0x2E
PORT = 0x302
MODE = 0x50
CPU_TARGET = 0x54
GPU_TARGET = 0x55
CPU_TACH = 0x5C
MIN_COMMAND = 1  # UI 0%: fans physically stop on this machine.
MAX_RAW = 70     # UI 100%.


def fail(message: str) -> NoReturn:
    print(f"colorful-n14 adapter: {message}", file=sys.stderr)
    raise SystemExit(1)


def check_nbfc_stopped() -> None:
    """Reject a concurrent legacy NBFC process before touching ACPI."""
    if Path("/run/nbfc_service.pid").exists():
        fail("NBFC PID file exists; stop NBFC before using CoolerControl")
    try:
        for process in Path("/proc").iterdir():
            if not process.name.isdecimal():
                continue
            try:
                name = (process / "comm").read_text(encoding="ascii").strip()
            except (FileNotFoundError, ProcessLookupError):
                continue
            except PermissionError as exc:
                fail(f"cannot verify that NBFC is stopped: {exc}")
            if name == "nbfc_service":
                fail("nbfc_service is running; stop NBFC before using CoolerControl")
    except OSError as exc:
        fail(f"cannot inspect running processes: {exc}")


def check_identity() -> None:
    if os.geteuid() != 0:
        fail("must run as root")
    check_nbfc_stopped()
    for name, expected in EXPECTED_DMI.items():
        try:
            actual = (DMI / name).read_text(encoding="ascii").strip()
        except OSError as exc:
            fail(f"cannot read DMI {name}: {exc}")
        if actual != expected:
            fail(f"unsupported {name}: {actual!r}")
    try:
        digest = hashlib.sha256(Path("/sys/firmware/acpi/tables/DSDT").read_bytes()).hexdigest()
    except OSError as exc:
        fail(f"cannot read DSDT: {exc}")
    if digest != EXPECTED_DSDT:
        fail("DSDT hash mismatch")
    wmi = Path("/sys/bus/wmi/devices")
    if not wmi.is_dir() or not any(
        re.fullmatch(re.escape(WMI_GUID) + r"(?:-|:)\d+", entry.name, re.IGNORECASE)
        for entry in wmi.iterdir()
    ):
        fail("expected WMI interface is absent")
    if not PROC_CALL.exists():
        fail("acpi_call is not loaded")


def packet(index: int, value: int | None = None) -> bytes:
    data = bytearray(24)
    data[:4] = b"BYDL"
    data[4:8] = int(PORT).to_bytes(4, "little")
    data[12] = 1
    data[14] = 8
    data[15] = index
    if value is not None:
        if not 0 <= value <= 255:
            fail("ACPI byte out of range")
        data[13] = 1
        data[16] = value
    return bytes(data)


def parse_result(text: str) -> int:
    text = text.rstrip("\x00\r\n ")
    if not re.fullmatch(r"0x[0-9a-fA-F]+", text):
        fail(f"unexpected ACPI result: {text!r}")
    value = int(text, 16)
    if not 0 <= value <= 255:
        fail("firmware returned a non-byte result")
    return value


def acpi(index: int, value: int | None = None, port: int = PORT) -> int:
    request_packet = bytearray(packet(index, value))
    request_packet[4:8] = int(port).to_bytes(4, "little")
    request = f"{METHOD}{bytes(request_packet).hex()}".encode("ascii")
    try:
        fd = os.open(PROC_CALL, os.O_WRONLY | os.O_CLOEXEC)
        try:
            if os.write(fd, request) != len(request):
                fail("short ACPI request write")
        finally:
            os.close(fd)
        fd = os.open(PROC_CALL, os.O_RDONLY | os.O_CLOEXEC)
        try:
            response = os.read(fd, 4096).decode("ascii")
        finally:
            os.close(fd)
    except OSError as exc:
        fail(f"ACPI call failed: {exc}")
    return parse_result(response)


def check_permission(require_control: bool) -> None:
    try:
        ticket = PERMISSION.read_text(encoding="ascii")
    except OSError as exc:
        fail(f"control permission is missing: {exc}")
    if not re.fullmatch(EXPECTED_DSDT + r" 55 71 (read|control)\n", ticket):
        fail("invalid or stale control permission")
    if require_control and not ticket.endswith(" control\n"):
        fail("control permission is not armed; run prepare.py arm first")
    if tuple((acpi(i, port=IDENTITY_PORT) for i in (0x20, 0x21))) != EXPECTED_EC:
        fail("embedded-controller identity changed")


def guarded(require_control: bool = False) -> None:
    check_identity()
    check_permission(require_control)


def read_raw(index: int) -> int:
    value = acpi(index)
    if not 0 <= value <= MAX_RAW:
        fail(f"register 0x{index:02x} returned {value}, outside 0..{MAX_RAW}")
    return value


def write_raw(index: int, value: int) -> None:
    if index not in (MODE, CPU_TARGET, GPU_TARGET):
        fail("attempted write to an unapproved register")
    if not 0 <= value <= MAX_RAW:
        fail("fan command must be in 0..70")
    if acpi(index, value) != 0:
        fail(f"firmware rejected write to 0x{index:02x}")


def set_both(raw: int) -> None:
    if not MIN_COMMAND <= raw <= MAX_RAW:
        fail(f"fan command must be in {MIN_COMMAND}..{MAX_RAW}")
    guarded(require_control=True)
    try:
        write_raw(MODE, 2)
        write_raw(CPU_TARGET, raw)
        write_raw(GPU_TARGET, raw)
    except BaseException:
        # Best-effort thermal failsafe if a partial command was accepted.
        for index in (MODE, CPU_TARGET, GPU_TARGET):
            try:
                write_raw(index, 2 if index == MODE else MAX_RAW)
            except BaseException:
                pass
        raise


def fixed_full() -> None:
    set_both(MAX_RAW)


def read_cpu_rpm() -> None:
    guarded(False)
    print(read_raw(CPU_TACH) * 100)


def read_duty() -> None:
    guarded(False)
    cpu = read_raw(CPU_TARGET)
    gpu = read_raw(GPU_TARGET)
    # The logical channel drives both fans; report the higher commanded value.
    raw = max(cpu, gpu)
    percent = (raw - MIN_COMMAND) * 100 / (MAX_RAW - MIN_COMMAND)
    print(round(max(0, min(100, percent))))


def read_cached_temp(sensor: str) -> None:
    """Read a fresh temperature written by the persistent sensor service."""
    path = Path("/run/colorful-n14-control") / f"{sensor}_temp"
    try:
        value = int(path.read_text(encoding="ascii").strip())
        age = max(0.0, time.time() - path.stat().st_mtime)
    except (OSError, ValueError) as exc:
        fail(f"{sensor} temperature cache is unavailable: {exc}")
    if not 0 < value < 120000 or age > 5:
        fail(f"{sensor} temperature cache is stale or invalid")
    print(value)


def read_cpu_temp() -> None:
    read_cached_temp("cpu")


def read_gpu_temp() -> None:
    read_cached_temp("gpu")


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        fail("usage: get-rpm|get-duty|set-duty|enable|reset|temp-cpu|temp-gpu")
    action = argv[1]
    if action == "get-rpm":
        read_cpu_rpm()
    elif action == "get-duty":
        read_duty()
    elif action == "set-duty":
        if len(argv) != 3 or not re.fullmatch(r"(?:100|[0-9]{1,2})", argv[2]):
            fail("duty must be an integer from 0 to 100")
        duty = int(argv[2])
        raw = MIN_COMMAND + round(duty * (MAX_RAW - MIN_COMMAND) / 100)
        set_both(raw)
    elif action == "enable":
        guarded(require_control=True)
        write_raw(MODE, 2)
    elif action == "reset":
        fixed_full()
    elif action == "temp-cpu":
        read_cpu_temp()
    elif action == "temp-gpu":
        read_gpu_temp()
    elif action == "self-check":
        guarded(False)
        print("ok")
    else:
        fail(f"unknown action: {action}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

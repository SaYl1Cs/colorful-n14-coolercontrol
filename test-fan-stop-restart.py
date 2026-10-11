#!/usr/bin/env python3
"""Supervised N14 fan-stop/restart test; direct ACPI writes, no CoolerControl.

This deliberately tests 0% once. It must be run as root with NBFC and
CoolerControl stopped. It restores the saved command on success. Any error
attempts both-fans-full as a best-effort failsafe.
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import NoReturn

from sensor_cache import Nvml

DMI = Path("/sys/class/dmi/id")
DSDT = Path("/sys/firmware/acpi/tables/DSDT")
PROC = Path("/proc/acpi/call")
EXPECTED = {"sys_vendor": "COLORFUL", "product_name": "N14 24", "bios_version": "2.07"}
EXPECTED_HASH = "4aab3c89fdaac8ab9e20d31cff3fe0a18b83877442520eb294429ff9237bef06"
METHOD = r"\_SB.AMW0.WMBA 0 2 b"
IDENTITY_PORT = 0x2E
PORT = 0x302
EXPECTED_EC = (0x55, 0x71)
MODE, CPU, GPU, CPU_TACH, GPU_TACH = 0x50, 0x54, 0x55, 0x5C, 0x5D
MAX_RAW, MIN_RAW = 70, 28  # 100% and 40%; only this test may issue raw zero.


def stop(message: str) -> NoReturn:
    raise RuntimeError(message)


def packet(index: int, value: int | None = None) -> bytes:
    data = bytearray(24)
    data[:4] = b"BYDL"
    data[4:8] = int(PORT).to_bytes(4, "little")
    data[12] = 1
    data[14] = 8
    data[15] = index
    if value is not None:
        data[13] = 1
        data[16] = value
    return bytes(data)


def result(text: str) -> int:
    text = text.rstrip("\x00\r\n ")
    if not re.fullmatch(r"0x[0-9a-fA-F]+", text):
        stop(f"unexpected ACPI result: {text!r}")
    value = int(text, 16)
    if not 0 <= value <= 255:
        stop("ACPI result is outside one byte")
    return value


def acpi(index: int, value: int | None = None, port: int = PORT) -> int:
    request_packet = bytearray(packet(index, value))
    request_packet[4:8] = int(port).to_bytes(4, "little")
    request = f"{METHOD}{bytes(request_packet).hex()}".encode("ascii")
    fd = os.open(PROC, os.O_WRONLY | os.O_CLOEXEC)
    try:
        if os.write(fd, request) != len(request):
            stop("short ACPI request")
    finally:
        os.close(fd)
    fd = os.open(PROC, os.O_RDONLY | os.O_CLOEXEC)
    try:
        text = os.read(fd, 4096).decode("ascii")
    finally:
        os.close(fd)
    return result(text)


def write(index: int, value: int) -> None:
    if index not in (MODE, CPU, GPU) or not 0 <= value <= MAX_RAW:
        stop("unapproved write")
    if acpi(index, value) != 0:
        stop(f"firmware rejected write 0x{index:02x}={value}")


def both(value: int) -> None:
    write(MODE, 2)
    write(CPU, value)
    write(GPU, value)


def temperatures() -> tuple[float, float]:
    cpu_values = []
    for hw in Path("/sys/class/hwmon").glob("hwmon*"):
        try:
            if (hw / "name").read_text().strip() == "coretemp":
                cpu_values += [int(p.read_text()) / 1000 for p in hw.glob("temp*_input")]
        except (OSError, ValueError):
            pass
    if not cpu_values:
        stop("CPU temperature unavailable")
    nvml = Nvml()
    try:
        nvml.start()
        gpu_temperature = nvml.gpu_temperature_millic() / 1000
    except (OSError, RuntimeError, ValueError) as exc:
        stop(f"GPU temperature unavailable: {exc}")
    finally:
        nvml.close()
    return max(cpu_values), gpu_temperature


def ensure_platform() -> None:
    if os.geteuid() != 0:
        stop("run as root")
    for key, expected in EXPECTED.items():
        if (DMI / key).read_text().strip() != expected:
            stop(f"unsupported {key}")
    if hashlib.sha256(DSDT.read_bytes()).hexdigest() != EXPECTED_HASH:
        stop("DSDT hash mismatch")
    if not PROC.exists():
        stop("acpi_call is not loaded")
    if tuple(acpi(i, port=IDENTITY_PORT) for i in (0x20, 0x21)) != EXPECTED_EC:
        stop("EC ID is not 55:71")
    if subprocess.run(["/usr/bin/systemctl", "is-active", "--quiet", "nbfc_service.service"]).returncode == 0:
        stop("NBFC is active")
    if subprocess.run(["/usr/bin/pgrep", "-x", "nbfc_service"], stdout=subprocess.DEVNULL).returncode == 0:
        stop("nbfc_service process exists")
    if subprocess.run(["/usr/bin/systemctl", "is-active", "--quiet", "coolercontrold.service"]).returncode == 0:
        stop("CoolerControl is active; stop it before this direct test")


def main() -> int:
    ensure_platform()
    before = {index: acpi(index) for index in (MODE, CPU, GPU)}
    cpu_temp, gpu_temp = temperatures()
    if cpu_temp >= 65 or gpu_temp >= 60:
        stop(f"temperatures too high for test: CPU {cpu_temp:.0f} C, GPU {gpu_temp:.0f} C")
    print(f"Saved mode={before[MODE]}, CPU command={before[CPU]}, GPU command={before[GPU]}")
    print(f"Before: CPU {cpu_temp:.0f} C, GPU {gpu_temp:.0f} C")
    print("PHYSICAL CHECK 1: after 0% command, confirm whether BOTH fans stop.", flush=True)
    try:
        both(0)
        time.sleep(10)
        zero_cpu, zero_gpu = acpi(CPU_TACH), acpi(GPU_TACH)
        print(f"At 0% command: CPU tach raw={zero_cpu}; GPU tach raw={zero_gpu} (GPU tach is not trusted)", flush=True)
        print("PHYSICAL CHECK 2: after 40% command, confirm whether BOTH fans restart.", flush=True)
        both(MIN_RAW)
        time.sleep(10)
        restart_cpu, restart_gpu = acpi(CPU_TACH), acpi(GPU_TACH)
        cpu_temp, gpu_temp = temperatures()
        print(f"At 40% command: CPU tach raw={restart_cpu}; GPU tach raw={restart_gpu}; CPU {cpu_temp:.0f} C; GPU {gpu_temp:.0f} C", flush=True)
        if restart_cpu == 0 or restart_cpu > MAX_RAW:
            stop(f"CPU fan did not return to a valid tachometer range: {restart_cpu}")
        restore = before[CPU] if before[CPU] == before[GPU] else MIN_RAW
        if not 0 <= restore <= MAX_RAW:
            restore = MIN_RAW
        both(restore)
        print(f"SUCCESS: restored both-fan command {restore}/70 ({restore * 100 / 70:.0f}%).")
        print("GPU physical stop/restart still requires the operator's observation; GPU RPM is not used as proof.")
        return 0
    except BaseException as exc:
        print(f"TEST FAILED: {exc}; requesting both fans full.", file=sys.stderr, flush=True)
        try:
            both(MAX_RAW)
            print("Failsafe command 100% sent to both fans.", file=sys.stderr, flush=True)
        except BaseException as failsafe:
            print(f"FAILSAFE WRITE FAILED: {failsafe}", file=sys.stderr, flush=True)
        raise


if __name__ == "__main__":
    raise SystemExit(main())

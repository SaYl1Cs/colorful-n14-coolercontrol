#!/usr/bin/env python3
"""Keep CPU and NVIDIA temperatures in a tiny root-owned runtime cache.

The process stays alive and talks to NVML directly. CoolerControl therefore
never has to start an external NVIDIA CLI for every one-second sensor poll.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import glob
import os
from pathlib import Path
import stat
import signal
import sys
import tempfile
import threading
import time

RUNTIME_DIR = Path("/run/colorful-n14-control")
POLL_SECONDS = 1.0
MAX_TEMP_MILLIC = 120_000
NVML_SUCCESS = 0
NVML_TEMPERATURE_GPU = 0


class Nvml:
    """Small ctypes wrapper around the stable NVML temperature API."""

    def __init__(self) -> None:
        library = ctypes.util.find_library("nvidia-ml") or "libnvidia-ml.so.1"
        self.lib = ctypes.CDLL(library)
        self.lib.nvmlInit_v2.restype = ctypes.c_int
        self.lib.nvmlShutdown.restype = ctypes.c_int
        self.lib.nvmlDeviceGetCount_v2.argtypes = [ctypes.POINTER(ctypes.c_uint)]
        self.lib.nvmlDeviceGetCount_v2.restype = ctypes.c_int
        self.lib.nvmlDeviceGetHandleByIndex_v2.argtypes = [
            ctypes.c_uint,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        self.lib.nvmlDeviceGetHandleByIndex_v2.restype = ctypes.c_int
        self.lib.nvmlDeviceGetTemperature.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint,
            ctypes.POINTER(ctypes.c_uint),
        ]
        self.lib.nvmlDeviceGetTemperature.restype = ctypes.c_int
        self.lib.nvmlErrorString.argtypes = [ctypes.c_int]
        self.lib.nvmlErrorString.restype = ctypes.c_char_p
        self._initialized = False

    def _check(self, result: int, operation: str) -> None:
        if result == NVML_SUCCESS:
            return
        try:
            detail = self.lib.nvmlErrorString(result).decode("ascii", "replace")
        except (AttributeError, OSError, UnicodeError):
            detail = f"NVML error {result}"
        raise RuntimeError(f"{operation}: {detail}")

    def start(self) -> None:
        self._check(self.lib.nvmlInit_v2(), "nvmlInit_v2")
        self._initialized = True

    def close(self) -> None:
        if self._initialized:
            self.lib.nvmlShutdown()
            self._initialized = False

    def gpu_temperature_millic(self) -> int:
        count = ctypes.c_uint()
        self._check(
            self.lib.nvmlDeviceGetCount_v2(ctypes.byref(count)),
            "nvmlDeviceGetCount_v2",
        )
        if count.value < 1:
            raise RuntimeError("no NVIDIA GPU was found")
        handle = ctypes.c_void_p()
        self._check(
            self.lib.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(handle)),
            "nvmlDeviceGetHandleByIndex_v2",
        )
        temperature = ctypes.c_uint()
        self._check(
            self.lib.nvmlDeviceGetTemperature(
                handle, NVML_TEMPERATURE_GPU, ctypes.byref(temperature)
            ),
            "nvmlDeviceGetTemperature",
        )
        value = temperature.value * 1000
        if not 0 < value < MAX_TEMP_MILLIC:
            raise RuntimeError(f"invalid GPU temperature {value}")
        return value


def cpu_temperature_millic() -> int:
    values: list[int] = []
    for hwmon in glob.glob("/sys/class/hwmon/hwmon*"):
        try:
            if Path(hwmon, "name").read_text(encoding="ascii").strip() != "coretemp":
                continue
            for path in glob.glob(f"{hwmon}/temp*_input"):
                value = int(Path(path).read_text(encoding="ascii").strip())
                if 0 < value < MAX_TEMP_MILLIC:
                    values.append(value)
        except (OSError, ValueError):
            continue
    if not values:
        raise RuntimeError("coretemp is unavailable")
    return max(values)


def ensure_runtime_dir() -> None:
    RUNTIME_DIR.mkdir(mode=0o700, exist_ok=True)
    info = RUNTIME_DIR.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0:
        raise RuntimeError("sensor cache directory must be a root-owned directory")
    os.chmod(RUNTIME_DIR, 0o700)


def write_cache(sensor: str, value: int) -> None:
    target = RUNTIME_DIR / f"{sensor}_temp"
    fd, temporary = tempfile.mkstemp(prefix=f".{sensor}_temp.", dir=RUNTIME_DIR)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as stream:
            stream.write(f"{value}\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def remove_cache(sensor: str) -> None:
    (RUNTIME_DIR / f"{sensor}_temp").unlink(missing_ok=True)


def main() -> int:
    if os.geteuid() != 0:
        print("colorful-n14 sensors: run as root", file=sys.stderr)
        return 1
    ensure_runtime_dir()
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    nvml: Nvml | None = None
    last_error: str | None = None
    try:
        while not stop.is_set():
            try:
                if nvml is None:
                    nvml = Nvml()
                    nvml.start()
                write_cache("gpu", nvml.gpu_temperature_millic())
                error = None
            except (OSError, RuntimeError, ValueError) as exc:
                error = str(exc)
                remove_cache("gpu")
                if nvml is not None:
                    nvml.close()
                    nvml = None
            if error != last_error and error:
                print(f"colorful-n14 sensors: {error}", file=sys.stderr, flush=True)
            last_error = error

            try:
                write_cache("cpu", cpu_temperature_millic())
            except (OSError, RuntimeError, ValueError) as exc:
                error = f"CPU: {exc}"
                remove_cache("cpu")
                if error != last_error:
                    print(f"colorful-n14 sensors: {error}", file=sys.stderr, flush=True)
                last_error = error

            stop.wait(POLL_SECONDS)
    finally:
        remove_cache("cpu")
        remove_cache("gpu")
        if nvml is not None:
            nvml.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

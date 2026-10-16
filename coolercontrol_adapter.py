#!/usr/bin/env python3
"""Root-only CoolerControl Custom Device adapter for Colorful N14 24.

This exposes one logical channel controlling both physical fans. GPU RPM is
never fabricated: get-rpm reports the CPU tachometer only, while CPU and GPU
temperatures are read from the fresh cache maintained by sensor_cache.py.
ACPI access is guarded by exact platform, controller, and temporary
control-permission checks.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import NoReturn

import thermal_state
from thermal_state import read_cpu_snapshot, read_snapshot, critical_temperature
import provenance_fence

import control_policy

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
_IDENTITY_CHECKED = False
_RISE_STATE_UNCERTAIN = False  # Cleared only by a successfully recorded fan command.
_LAST_SAFE_SNAPSHOT = None  # Process-local; never rejuvenate original sample clocks.


class DutyHeld(ValueError):
    """Command rejected for transient sensor loss; no fan write was performed."""


class GpuAsleep(ValueError):
    """Expected lack of a GPU temperature in a validated D3cold snapshot."""


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
    global _IDENTITY_CHECKED
    if os.geteuid() != 0:
        fail("must run as root")
    if _IDENTITY_CHECKED:
        return
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
    _IDENTITY_CHECKED = True


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


_GPU_DEVICE = None


def read_live_power_state():
    """Read both sysfs PM files afresh, without a GPU driver operation."""
    global _GPU_DEVICE
    from sensor_cache import discover_gpu, power_state
    if _GPU_DEVICE is None:
        _GPU_DEVICE = discover_gpu()
    return power_state(_GPU_DEVICE)


def current_control_snapshot():
    data = read_snapshot(require_metadata=True, require_samples=True)
    if critical_temperature(data):
        raise ValueError('critical temperature')
    if read_live_power_state() != (data['gpu_state'], data['gpu_power_state']):
        raise ValueError('current PCI power mode differs from snapshot')
    return data


def remember_safe(data):
    global _LAST_SAFE_SNAPSHOT
    if (_LAST_SAFE_SNAPSHOT is None or data['sampled_at_monotonic_ns'] >
            _LAST_SAFE_SNAPSHOT['sampled_at_monotonic_ns']):
        _LAST_SAFE_SNAPSHOT = deepcopy(data)


def forget_safe():
    global _LAST_SAFE_SNAPSHOT
    _LAST_SAFE_SNAPSHOT = None


def cool_for_hold(data):
    # Extra margin for lifecycle holds and loss of observability, not new curves.
    return data['cpu_temp'] < 80000 and (data['gpu_state'] == 'suspended' or data['gpu_temp'] < 70000)


def sensor_grace_deadline(data):
    # Count the oldest required measurement and both original clocks. This is
    # never now+5: merely rereading a snapshot cannot buy another grace period.
    stamps = [data['sampled_at'], data['sampled_at_monotonic_ns'] / 1e9]
    if data['gpu_state'] == 'active':
        stamps.append(data['gpu_sampled_at'])
    wall_remaining = thermal_state.SENSOR_GRACE - (thermal_state.unix_ms() - data['sampled_at_unix_ms']) / 1000
    return min(min(stamps) + thermal_state.SENSOR_GRACE, thermal_state.boottime() + wall_remaining)


def sensor_grace():
    """Return original safe data only while its TOTAL age is strictly under 5s."""
    check_applied_duty()
    data = _LAST_SAFE_SNAPSHOT
    now = thermal_state.boottime()
    if data is None or not data['sampled_at'] <= now < sensor_grace_deadline(data):
        raise ValueError('sensor grace expired')
    thermal_state.read_snapshot_data(data, now, True, True)
    if not cool_for_hold(data):
        raise ValueError('temperature too high for hold')
    state = provenance_fence._read()
    if state['phase'] == 'closed':
        provenance_fence.check_hold(state, now)
    else:
        provenance_fence.check_current(state, data, now)
    return data


def quiet_hand_over(live_policy=None, *, startup=False, shutdown=False):
    """Serial owner only. Preserve known individual targets, never invent zero."""
    guarded(True)
    check_applied_duty()
    data = current_control_snapshot()
    if not cool_for_hold(data):
        raise ValueError('temperature too high for hold')
    if live_policy is not None:
        control_policy.validate_policy(live_policy)
    if startup:
        if read_raw(MODE) != 2:
            raise ValueError('unknown fan control mode')
        raw = [read_raw(CPU_TARGET), read_raw(GPU_TARGET)]
        if not all(type(v) is int and MIN_COMMAND <= v <= MAX_RAW for v in raw):
            raise ValueError('invalid last applied duty')
    else:
        state = provenance_fence._read()
        if 'hold_until' in state:
            provenance_fence.check_hold(state)
            if state['phase'] != 'closed':
                provenance_fence.check_authority(state)
            raw = state['held_raw']
            if live_policy is None:
                live_policy = state['hold_policy']
        elif state['phase'] == 'closed':
            if state.get('full_confirmed') is True:
                return provenance_fence.status()  # Already full; do not claim a hold.
            raise ValueError('invalid last applied duty')
        else:
            provenance_fence.check_authority(state)
            raw = [MIN_COMMAND + round(state['last_duty'] * (MAX_RAW - MIN_COMMAND) / 100)] * 2
            if live_policy is None:
                live_policy = state['policy']
    provenance_fence.begin_hold(raw, data, live_policy, 'shutdown' if shutdown else 'handover')
    raise_hold_floor(data)
    remember_safe(data)
    return provenance_fence.status()


def raise_hold_floor(data):
    """Only increases, individually. A stronger fan is never lowered to its peer."""
    state = provenance_fence.check_hold(provenance_fence._read())
    if not cool_for_hold(data):
        raise ValueError('temperature too high for hold')
    live = state['hold_policy']
    if live is None:
        return  # Startup: short cool-state hold until authenticated live policy.
    control_policy.validate_policy(live)
    demand = control_policy.curve_demand(live, data)
    floor = MIN_COMMAND + round(demand * (MAX_RAW - MIN_COMMAND) / 100)
    old = state['held_raw']
    new = [max(v, floor) for v in old]
    if old == new:
        return
    guarded(True)
    if read_live_power_state() != (data['gpu_state'], data['gpu_power_state']):
        raise ValueError('GPU changed mode before fan write')
    try:
        for index, before, after in zip((CPU_TARGET, GPU_TARGET), old, new):
            if after > before:
                write_raw(index, after)
        state['held_raw'] = new
        if state['phase'] != 'closed':
            state['last_duty'] = min((v - 1) * 100 // 69 for v in new)
        provenance_fence._atomic(None, state)
    except BaseException:
        _failed_write_full()
        raise


def set_both(raw: int, label=None, duty=None) -> None:
    if not isinstance(raw, int) or isinstance(raw, bool) or not MIN_COMMAND <= raw <= MAX_RAW:
        fail(f"fan command must be in {MIN_COMMAND}..{MAX_RAW}")
    guarded(require_control=True)
    rejection = None
    applied = 100
    if raw < MAX_RAW or label is not None:
        try:
            try:
                temperatures = current_control_snapshot()
            except thermal_state.SensorUnavailable:
                previous = sensor_grace()
                provenance_fence.reject_during_gap(label, previous, duty)
                raise DutyHeld('sensor unavailable; bounded last-duty hold') from None
            if label is None:
                raise ValueError('lower duty requires producer provenance')
            applied = provenance_fence.accept_command(
                label, current_snapshot=temperatures, expected_duty=duty)
            raw = MIN_COMMAND + round(applied * (MAX_RAW - MIN_COMMAND) / 100)
            # Re-observe PM after persistent validation, as close to writes as
            # practical. Firmware/PM can still change after this observation.
            if read_live_power_state() != (temperatures['gpu_state'], temperatures['gpu_power_state']):
                raise ValueError('GPU changed mode before fan write')
        except DutyHeld:
            raise
        except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
            rejection = exc
            raw, applied = MAX_RAW, 100
            try:
                provenance_fence.close()
            except OSError:
                # A failed state write must not prevent best-effort full duty.
                # Future low writes still have to persist their reserved ID.
                pass
    _write_both(raw, applied)
    if rejection is None and label is not None:
        remember_safe(temperatures)
    if rejection is not None:
        raise ValueError('lower-duty validation failed; full duty requested') from rejection


def _write_both(raw, applied):
    """Serial owner only; record actual commanded duty after both writes succeed."""
    global _RISE_STATE_UNCERTAIN
    try:
        write_raw(MODE, 2)
        write_raw(CPU_TARGET, raw)
        write_raw(GPU_TARGET, raw)
        provenance_fence.record_applied_duty(applied)
        _RISE_STATE_UNCERTAIN = False
        if applied == 100:
            forget_safe()
    except BaseException:
        _failed_write_full()
        raise


def _failed_write_full():
    global _RISE_STATE_UNCERTAIN
    _RISE_STATE_UNCERTAIN = True
    forget_safe()
    try:
        provenance_fence.close()
    except OSError:
        pass
    for index in (MODE, CPU_TARGET, GPU_TARGET):
        try:
            write_raw(index, 2 if index == MODE else MAX_RAW)
        except BaseException:
            pass


def check_applied_duty() -> None:
    if _RISE_STATE_UNCERTAIN:
        raise ValueError('last applied duty uncertain')


def raise_transition_floor() -> None:
    """Local watchdog safety only: no fabricated label and no unproven lower.

    Re-read current data/fence rather than writing a cached watchdog decision.
    This runs on the same serial owner as producer writes and assignment RPCs.
    """
    guarded(require_control=True)
    temperatures = current_control_snapshot()
    check_applied_duty()
    if 'hold_until' in provenance_fence.status():
        raise_hold_floor(temperatures)
        return
    applied = provenance_fence.transition_raise_duty(temperatures)
    if applied is None:
        return
    if read_live_power_state() != (temperatures['gpu_state'], temperatures['gpu_power_state']):
        raise ValueError('GPU changed mode before fan write')
    raw = MIN_COMMAND + round(applied * (MAX_RAW - MIN_COMMAND) / 100)
    _write_both(raw, applied)


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


def _read_temp_data(sensor: str):
    if sensor == 'cpu':
        return read_cpu_snapshot()
    if sensor == 'gpu':
        return read_snapshot()
    fail('unknown temperature sensor')


def read_cached_temp(sensor: str) -> None:
    """Legacy millidegree endpoint; it never substitutes a sleeping GPU."""
    try:
        data = _read_temp_data(sensor)
    except (OSError, ValueError, TypeError) as exc:
        fail(f"temperature cache unavailable: {exc}")
    value = data['cpu_temp' if sensor == 'cpu' else 'gpu_temp']
    if (sensor == 'gpu' and value is None and
            (data['gpu_state'], data['gpu_power_state']) == ('suspended', 'D3cold')):
        raise GpuAsleep('GPU asleep (D3cold); temperature unavailable')
    if value is None:
        fail('temperature unavailable')
    print(value)


def read_cached_temp_json(sensor: str) -> None:
    """Return Celsius plus the sampler's original, paired provenance object."""
    try:
        data = _read_temp_data(sensor)
        if (sensor == 'gpu' and data['gpu_temp'] is None and
                (data['gpu_state'], data['gpu_power_state']) == ('suspended', 'D3cold')):
            # Never hide bad clocks, malformed metadata or stale/error snapshots.
            thermal_state.read_snapshot_data(data, require_metadata=True, require_samples=True)
            raise GpuAsleep('GPU asleep (D3cold); temperature unavailable')
        payload = thermal_state.provenance_payload(data, sensor)
    except GpuAsleep:
        raise
    except (OSError, ValueError, TypeError, KeyError) as exc:
        fail(f"temperature provenance unavailable: {exc}")
    print(json.dumps(payload, separators=(',', ':'), allow_nan=False))


def read_cpu_temp() -> None:
    read_cached_temp("cpu")


def read_gpu_temp() -> None:
    read_cached_temp("gpu")


def read_cpu_temp_json() -> None:
    read_cached_temp_json('cpu')


def read_gpu_temp_json() -> None:
    read_cached_temp_json('gpu')


def set_duty_provenance(argv: list[str]) -> None:
    if len(argv) != 4 or not re.fullmatch(r"(?:100|[0-9]{1,2})", argv[2]):
        fail("provenance duty arguments are invalid")
    if len(argv[3].encode('utf-8')) > 65536:
        fail('provenance too large')
    label = thermal_state.strict_json(argv[3])
    duty = int(argv[2])
    raw = MIN_COMMAND + round(duty * (MAX_RAW - MIN_COMMAND) / 100)
    set_both(raw, label=label, duty=duty)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        fail("usage: get-rpm|get-duty|set-duty|set-duty-provenance|enable|reset|temp-cpu|temp-gpu|temp-cpu-json|temp-gpu-json")
    action = argv[1]
    if action == "set-duty-provenance":
        set_duty_provenance(argv)
    elif action == "get-rpm":
        read_cpu_rpm()
    elif action == "get-duty":
        read_duty()
    elif action == "set-duty":
        if len(argv) != 3 or not re.fullmatch(r"(?:100|[0-9]{1,2})", argv[2]):
            fail("duty must be an integer from 0 to 100")
        duty = int(argv[2])
        if duty != 100:
            fail('unlabelled lower duty is disabled; use producer provenance')
        fixed_full()
    elif action == "enable":
        guarded(require_control=True)
        write_raw(MODE, 2)
    elif action == "reset":
        fixed_full()
    elif action == "temp-cpu":
        read_cpu_temp()
    elif action == "temp-gpu":
        read_gpu_temp()
    elif action in ("temp-cpu-json", "temp-cpu/json"):
        read_cpu_temp_json()
    elif action in ("temp-gpu-json", "temp-gpu/json"):
        read_gpu_temp_json()
    elif action == "self-check":
        guarded(False)
        print("ok")
    else:
        fail(f"unknown action: {action}")
    return 0


if __name__ == "__main__":
    if sys.argv[1:] == ['serve']:
        from adapter_server import serve
        raise SystemExit(serve())
    raise SystemExit(main(sys.argv))

#!/usr/bin/env python3
"""Read-only EC sampler for the Colorful N14 CPU and GPU inputs.

Only EC CPU 0x56 and GPU 0x58 are read.  GPU temperature is present only when
runtime PM is ``active`` and PCI power is ``D0``.  A suspended/``D3cold`` GPU
has no temperature value; it is never replaced with the CPU temperature.

The sampler owns a process session and a monotonically increasing sample ID.
Its generation changes when the observed mode enters a different state
(active, suspended, or error).  The same metadata is attached to the CPU and
GPU values from one observation so the JSON temperature command is an atomic
value/provenance pair from the consumer's point of view.
"""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import signal
import stat
import sys
import tempfile
import threading
import time
import uuid

from thermal_state import RUNTIME_DIR, boottime, notify_ready

POLL_SECONDS = 1.0
EC_IO = Path('/sys/kernel/debug/ec/ec0/io')
EC_CPU_TEMP = 0x56
EC_GPU_TEMP = 0x58


def discover_gpu(root=Path('/sys/bus/pci/devices')):
    """Find the one NVIDIA display function using sysfs metadata only."""
    devices = []
    for device in root.iterdir():
        try:
            if ((device / 'vendor').read_text().strip() == '0x10de'
                    and int((device / 'class').read_text().strip(), 16) >> 16 == 3):
                devices.append(device)
        except (OSError, ValueError):
            continue
    if len(devices) != 1:
        raise RuntimeError('expected exactly one NVIDIA display PCI device')
    return devices[0]


def power_state(device):
    """Read runtime and PCI PM twice; this is not an atomic kernel lease."""
    def read():
        return ((device / 'power/runtime_status').read_text(encoding='ascii').strip(),
                (device / 'power_state').read_text(encoding='ascii').strip())
    first = read()
    if read() != first:
        raise RuntimeError('GPU power state changed during observation')
    if first not in (('suspended', 'D3cold'), ('active', 'D0')):
        raise RuntimeError(f'GPU power state unconfirmed: {first!r}')
    return first


class EcSensor:
    def __init__(self, path=EC_IO):
        self.path = Path(path)

    def read(self, offset):
        if type(offset) is not int or offset not in (EC_CPU_TEMP, EC_GPU_TEMP):
            raise ValueError('EC temperature offset is not whitelisted')
        # Open afresh so unavailable debugfs/resume failures can recover.  No
        # EC scans, index-port writes, or write_support requirement are used.
        fd = os.open(self.path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            value = os.pread(fd, 1, offset)
        finally:
            os.close(fd)
        if len(value) != 1 or not 0 < value[0] < 120:
            raise ValueError(f'EC temperature at 0x{offset:02x} is invalid: {value.hex()}')
        return value[0] * 1000


class CpuEcSensor:
    def __init__(self, ec):
        self.ec = ec

    def read(self):
        return self.ec.read(EC_CPU_TEMP)


def cpu_temperature_millic():
    # Compatibility for supervised manual tools, with the same platform guard.
    from prepare import check_platform
    check_platform()
    return CpuEcSensor(EcSensor()).read()


class GpuEcSensor:
    def __init__(self, ec, device=None):
        self.ec = ec
        self.device = device

    def read(self, now):
        if self.device is None:
            self.device = discover_gpu()
        before = power_state(self.device)
        value = self.ec.read(EC_GPU_TEMP) if before == ('active', 'D0') else None
        if power_state(self.device) != before:
            raise RuntimeError('GPU changed power state during EC sample')
        return {'gpu_state': before[0], 'gpu_power_state': before[1],
                'gpu_temp': value, 'gpu_sampled_at': now if value is not None else None}


class SamplerState:
    """Process-local identity and mode generation for EC observations."""
    def __init__(self, session=None):
        self.session = session or str(uuid.uuid4())
        self.generation = 0
        self.sample_id = 0
        self.mode = None

    def begin(self, mode):
        if mode != self.mode:
            self.generation += 1
            self.mode = mode
        self.sample_id += 1
        return self.generation, self.sample_id


def _unix_ms():
    return time.time_ns() // 1_000_000


def ensure_runtime_dir():
    RUNTIME_DIR.mkdir(mode=0o700, exist_ok=True)
    info = RUNTIME_DIR.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0:
        raise RuntimeError('sensor cache directory must be a root-owned directory')
    os.chmod(RUNTIME_DIR, 0o700)


def write_snapshot(data):
    fd, temporary = tempfile.mkstemp(prefix='.temperatures.', dir=RUNTIME_DIR)
    try:
        with os.fdopen(fd, 'w', encoding='ascii') as stream:
            json.dump(data, stream, allow_nan=False, separators=(',', ':'))
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, RUNTIME_DIR / 'temperatures.json')
    finally:
        Path(temporary).unlink(missing_ok=True)


def sample(cpu, gpu, state=None):
    """Take one paired observation without retaining stale sensor values.

    ``sampled_at`` is the beginning of I/O, so a blocked EC read ages the
    previous snapshot rather than rejuvenating it.  A caller can inject a
    ``SamplerState`` in offline tests to exercise mode generations.
    """
    state = SamplerState() if state is None else state
    sampled_at = boottime()
    sampled_at_monotonic_ns = int(sampled_at * 1_000_000_000)
    sampled_at_unix_ms = _unix_ms()
    errors = []
    try:
        cpu_value = cpu.read()
    except (OSError, ValueError, RuntimeError) as exc:
        cpu_value = None
        errors.append(f'CPU EC read: {exc}')
    try:
        gpu_data = gpu.read(sampled_at)
    except (OSError, ValueError, RuntimeError) as exc:
        gpu_data = {'gpu_state': 'error', 'gpu_power_state': None,
                    'gpu_temp': None, 'gpu_sampled_at': None}
        errors.append(f'GPU EC/PM read: {exc}')

    gpu_state = gpu_data.get('gpu_state')
    mode = gpu_state if not errors and gpu_state in (
        'active', 'suspended') else 'error'
    generation, sample_id = state.begin(mode)
    common = dict(session=state.session, generation=generation, sample_id=sample_id,
                  sampled_at_unix_ms=sampled_at_unix_ms,
                  sampled_at_monotonic_ns=sampled_at_monotonic_ns)
    cpu_sample = None
    if cpu_value is not None:
        cpu_sample = dict(common, sensor='cpu', power_state=mode)
    gpu_sample = None
    if gpu_data.get('gpu_temp') is not None:
        gpu_sample = dict(common, sensor='gpu', power_state=mode)

    return dict(gpu_data, schema_version=2, source='ec0/io',
                cpu_temp=cpu_value, sampled_at=sampled_at,
                sampled_at_unix_ms=sampled_at_unix_ms,
                sampled_at_monotonic_ns=sampled_at_monotonic_ns,
                sampler_session=state.session, sampler_generation=generation,
                sample_id=sample_id, cpu_sample=cpu_sample, gpu_sample=gpu_sample,
                errors=errors)


def main():
    if os.geteuid() != 0:
        print('colorful-n14 sensors: run as root', file=sys.stderr)
        return 1
    # DMI, BIOS, SMBIOS EC version and exact DSDT checks precede any EC read.
    from prepare import check_platform
    check_platform()
    ensure_runtime_dir()
    lock_fd = os.open(RUNTIME_DIR / 'sensors.lock',
                      os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        ec = EcSensor()
        cpu, gpu = CpuEcSensor(ec), GpuEcSensor(ec)
        state = SamplerState()
        stop = threading.Event()
        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, lambda *_: stop.set())
        last_errors = None
        ready = False
        while not stop.is_set():
            data = sample(cpu, gpu, state)
            write_snapshot(data)
            if not ready:
                notify_ready()  # Ready to publish, NOT a claim of valid sensors.
                ready = True
            if data['errors'] != last_errors:
                if data['errors']:
                    print('; '.join(data['errors']), file=sys.stderr, flush=True)
                last_errors = data['errors']
            stop.wait(POLL_SECONDS)
    finally:
        os.close(lock_fd)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

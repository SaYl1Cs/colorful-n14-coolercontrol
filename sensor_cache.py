#!/usr/bin/env python3
"""Low-overhead CPU sampling and runtime-PM-aware NVIDIA sampling.

No NVIDIA driver calls in observed suspended/transition/unknown states. NVML
is initialized only for an active sample and shut down before the next sleep.
Sysfs checks and NVML calls are not atomic; see README for the strict no-query
mode and the limitation of userspace runtime-PM checks.
"""
from __future__ import annotations

import ctypes
import fcntl
import json
import os
from pathlib import Path
import signal
import stat
import sys
import tempfile
import threading

from thermal_state import RUNTIME_DIR, boottime, valid_temp, notify_ready

POLL_SECONDS = 1.0
GPU_POLL_SECONDS = 5.0
COOLDOWN_SECONDS = 15.0
MAX_TEMP_MILLIC = 120000
NVML_TEMPERATURE_GPU = 0


class Nvml:
    """Lazy library loading; no enumeration and no persistent driver session."""

    def __init__(self):
        self.lib = ctypes.CDLL('libnvidia-ml.so.1')
        signatures = {
            'nvmlInitWithFlags': ([ctypes.c_uint], ctypes.c_int),
            'nvmlShutdown': ([], ctypes.c_int),
            'nvmlDeviceGetHandleByPciBusId_v2':
                ([ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p)], ctypes.c_int),
            'nvmlDeviceGetTemperature':
                ([ctypes.c_void_p, ctypes.c_uint, ctypes.POINTER(ctypes.c_uint)], ctypes.c_int),
            'nvmlErrorString': ([ctypes.c_int], ctypes.c_char_p),
        }
        for name, (args, result) in signatures.items():
            function = getattr(self.lib, name)
            function.argtypes, function.restype = args, result
        self._initialized = False

    def _check(self, code, operation):
        if code:
            detail = self.lib.nvmlErrorString(code)
            raise RuntimeError(f'{operation}: {detail!r} ({code})')

    def start(self):
        # The no-attach flag cannot resolve a PCI handle on this driver. This
        # function is only reached after runtime_status confirmed active.
        self._check(self.lib.nvmlInitWithFlags(0), 'NVML init')
        self._initialized = True

    def close(self):
        if self._initialized:
            self._initialized = False
            self._check(self.lib.nvmlShutdown(), 'NVML shutdown')

    def gpu_temperature_millic(self, device=None):
        device = discover_gpu() if device is None else device
        handle = ctypes.c_void_p()
        self._check(self.lib.nvmlDeviceGetHandleByPciBusId_v2(
            device.name.encode('ascii'), ctypes.byref(handle)), 'GPU handle')
        # Recheck immediately before the temperature call as well.
        if runtime_status(device) != 'active':
            raise RuntimeError('GPU is no longer active')
        temperature = ctypes.c_uint()
        self._check(self.lib.nvmlDeviceGetTemperature(
            handle, NVML_TEMPERATURE_GPU, ctypes.byref(temperature)), 'GPU temperature')
        value = temperature.value * 1000
        if not valid_temp(value):
            raise ValueError('invalid GPU temperature')
        return value


def discover_gpu(root=Path('/sys/bus/pci/devices')):
    # Only sysfs metadata: no NVIDIA ioctls, CUDA, tools or GPU enumeration.
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


def runtime_status(device):
    return (device / 'power/runtime_status').read_text(encoding='ascii').strip()


class CpuSensor:
    def __init__(self, root=Path('/sys/class/hwmon')):
        self.root, self.paths = root, []

    def read(self):
        if not self.paths:
            for hwmon in self.root.glob('hwmon*'):
                if (hwmon / 'name').read_text().strip() == 'coretemp':
                    self.paths.extend(sorted(hwmon.glob('temp*_input')))
        try:
            values = [int(path.read_text(encoding='ascii')) for path in self.paths]
            if not values or not all(valid_temp(value) for value in values):
                raise ValueError('coretemp unavailable or invalid')
            return max(values)
        except (OSError, ValueError):
            self.paths = []  # Rediscover after resume/hwmon renumbering.
            raise


def cpu_temperature_millic():
    # Compatibility with the separate supervised manual test, not the daemon.
    return CpuSensor().read()


class GpuSensor:
    def __init__(self, device, factory=Nvml, poll=True):
        self.device, self.factory, self.poll = device, factory, poll
        self.nvml = None
        self.last_value = self.last_at = None
        self.previous_state = None
        self.next_query = 0.0
        self.suspended_since = None

    def read(self, cpu, now):
        state = runtime_status(self.device)
        if state == 'suspended':
            if self.suspended_since is None:
                self.suspended_since = now
            self.previous_state = state
            # Not a GPU measurement: explicitly labelled cooling-curve input.
            demand = cpu
            if (valid_temp(cpu) and self.last_value is not None
                    and 0 <= now - self.last_at <= COOLDOWN_SECONDS):
                demand = max(cpu, self.last_value)
            return {'gpu_state': state, 'gpu_temp': None,
                    'gpu_input': demand, 'gpu_sampled_at': None}
        self.suspended_since = None
        if state != 'active':
            self.previous_state = state
            raise RuntimeError(f'GPU runtime state is {state!r}; not querying driver')
        if not self.poll:
            raise RuntimeError('strict no-query mode: active GPU temperature unavailable')
        if self.previous_state != 'active' or now >= self.next_query:
            self.last_value = self.last_at = None
            # The second gate precedes even lazy library loading/init.
            if runtime_status(self.device) != 'active':
                raise RuntimeError('GPU changed state before NVML init')
            if self.nvml is None:
                self.nvml = self.factory()
            try:
                self.nvml.start()
                self.last_value = self.nvml.gpu_temperature_millic(self.device)
            finally:
                # Even an exception must release the GPU before waiting.
                self.nvml.close()
            self.last_at = boottime()
            self.next_query = self.last_at + GPU_POLL_SECONDS
        self.previous_state = state
        return {'gpu_state': state, 'gpu_temp': self.last_value,
                'gpu_input': self.last_value, 'gpu_sampled_at': self.last_at}


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
            json.dump(data, stream, allow_nan=False)
            stream.write('\n')
        # /run is volatile; fsync buys no persistence. Atomic rename is enough.
        os.replace(temporary, RUNTIME_DIR / 'temperatures.json')
    finally:
        Path(temporary).unlink(missing_ok=True)


def sample(cpu, gpu):
    errors = []
    try:
        cpu_value = cpu.read()
    except (OSError, ValueError, RuntimeError) as exc:
        cpu_value = None
        errors.append(f'CPU: {exc}')
    now = boottime()
    try:
        gpu_data = gpu.read(cpu_value, now)
    except (OSError, ValueError, RuntimeError, AttributeError) as exc:
        gpu_data = {'gpu_state': 'error', 'gpu_temp': None,
                    'gpu_input': cpu_value, 'gpu_sampled_at': None}
        errors.append(f'GPU: {exc}')
    return dict(gpu_data, cpu_temp=cpu_value, sampled_at=now, errors=errors)


def main():
    if os.geteuid() != 0:
        print('colorful-n14 sensors: run as root', file=sys.stderr)
        return 1
    ensure_runtime_dir()
    lock_fd = os.open(RUNTIME_DIR / 'sensors.lock',
                      os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        cpu = CpuSensor()
        gpu = GpuSensor(discover_gpu(), poll=os.environ.get('N14_GPU_POLL', 'never') != 'never')
        stop = threading.Event()
        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, lambda *_: stop.set())
        last_errors = None
        ready = False
        while not stop.is_set():
            data = sample(cpu, gpu)
            write_snapshot(data)
            if not ready:
                notify_ready()
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

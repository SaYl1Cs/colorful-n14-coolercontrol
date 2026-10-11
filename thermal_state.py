"""Shared, suspend-aware snapshot format. No hardware access on import."""
import json
from pathlib import Path
import time

RUNTIME_DIR = Path('/run/colorful-n14-control')
SNAPSHOT = RUNTIME_DIR / 'temperatures.json'
MAX_AGE = 5.0
GPU_MAX_AGE = 8.0


def boottime():
    # Unlike monotonic(), includes time spent in system suspend.
    return time.clock_gettime(time.CLOCK_BOOTTIME)


def valid_temp(value):
    return type(value) is int and 0 < value < 120000


def read_snapshot(path=None, now=None):
    now = boottime() if now is None else now
    data = json.loads((SNAPSHOT if path is None else path).read_text(encoding='ascii'))
    stamp = data.get('sampled_at')
    if type(stamp) not in (int, float) or not 0 <= now - stamp <= MAX_AGE:
        raise ValueError('temperature snapshot is stale or invalid')
    if not valid_temp(data.get('cpu_temp')):
        raise ValueError('CPU temperature unavailable')
    state = data.get('gpu_state')
    if state == 'active':
        stamp = data.get('gpu_sampled_at')
        if (not valid_temp(data.get('gpu_temp')) or type(stamp) not in (int, float)
                or not 0 <= now - stamp <= GPU_MAX_AGE):
            raise ValueError('active GPU temperature unavailable or stale')
        if data.get('gpu_input') != data['gpu_temp']:
            raise ValueError('active GPU cooling input does not match measurement')
    elif state in ('suspended', 'error'):
        if data.get('gpu_temp') is not None or not valid_temp(data.get('gpu_input')):
            raise ValueError('invalid GPU fallback cooling input')
        if data['gpu_input'] < data['cpu_temp']:
            raise ValueError('GPU fallback must not be below CPU temperature')
    else:
        raise ValueError('GPU power state is transitional or unknown')
    return data


def read_cpu_snapshot(path=None, now=None):
    """Validate only the CPU part; GPU failure must not hide CPU temperature."""
    now = boottime() if now is None else now
    data = json.loads((SNAPSHOT if path is None else path).read_text(encoding='ascii'))
    stamp = data.get('sampled_at')
    if type(stamp) not in (int, float) or not 0 <= now - stamp <= MAX_AGE:
        raise ValueError('temperature snapshot is stale or invalid')
    if not valid_temp(data.get('cpu_temp')):
        raise ValueError('CPU temperature unavailable')
    return data


def notify_ready():
    import os
    import socket
    address = os.environ.get('NOTIFY_SOCKET')
    if address:
        if address.startswith('@'):
            address = '\0' + address[1:]
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.sendto(b'READY=1', address)

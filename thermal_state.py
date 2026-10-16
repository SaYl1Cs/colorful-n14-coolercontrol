"""EC snapshot and original sample validation; no hardware access on import."""
import json
import math
from pathlib import Path
import time
import uuid

RUNTIME_DIR = Path('/run/colorful-n14-control')
SNAPSHOT = RUNTIME_DIR / 'temperatures.json'
MAX_AGE = 5.0
GPU_MAX_AGE = 5.0
SAMPLE_MAX_AGE_MS = 5000
SAMPLE_KEYS = {'session', 'generation', 'sample_id', 'sampled_at_unix_ms',
               'sampled_at_monotonic_ns', 'sensor', 'power_state'}


SENSOR_GRACE = 5.0  # Total age of the original safe sample, NOT time since failure.


class SensorUnavailable(ValueError):
    """Thermal availability only; never provenance, policy, clocks or writes."""


def boottime():
    return time.clock_gettime(time.CLOCK_BOOTTIME)


def unix_ms():
    return time.time_ns() // 1_000_000


def integer(value, low=0, high=(1 << 64) - 1):
    return type(value) is int and low <= value <= high


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def session_uuid(value):
    try:
        parsed = uuid.UUID(value)
        return type(value) is str and str(parsed) == value and parsed.version == 4
    except (ValueError, TypeError, AttributeError):
        return False


def valid_temp(value):
    return integer(value, 1, 119999)


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate JSON member')
            result[key] = value
        return result
    def constant(_):
        raise ValueError('nonfinite JSON')
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)


def validate_sample(sample, now_unix_ms=None, max_age_ms=SAMPLE_MAX_AGE_MS,
                    sensor=None, generation=None, session=None, now_ns=None):
    if type(sample) is not dict or set(sample) != SAMPLE_KEYS:
        raise ValueError('incomplete or unknown sensor sample members')
    if not session_uuid(sample['session']):
        raise ValueError('invalid sampler session UUID')
    if not integer(sample['generation'], 1) or not integer(sample['sample_id'], 1):
        raise ValueError('invalid sampler generation/sample ID')
    wall = unix_ms() if now_unix_ms is None else now_unix_ms
    boot = int(boottime() * 1e9) if now_ns is None else now_ns
    stamp, mono = sample['sampled_at_unix_ms'], sample['sampled_at_monotonic_ns']
    if not integer(stamp) or not integer(mono, 1):
        raise ValueError('invalid sample clocks')
    wall_age, boot_age = wall - stamp, (boot - mono) / 1e6
    if (not 0 <= wall_age <= max_age_ms or not 0 <= boot_age <= max_age_ms
            or abs(wall_age - boot_age) > 1000):
        raise ValueError('stale sample or clock discontinuity')
    if sample['sensor'] not in ('cpu', 'gpu') or sample['power_state'] not in (
            'active', 'suspended', 'error'):
        raise ValueError('invalid sensor identity/mode')
    for expected, key in ((sensor, 'sensor'), (generation, 'generation'), (session, 'session')):
        if expected is not None and sample[key] != expected:
            raise ValueError('sensor identity mismatch: ' + key)
    return sample


def has_sampler_metadata(data):
    return 'sampler_session' in data


def epoch(data):
    return [data['sampler_session'], data['sampler_generation'], data['gpu_state']]


def validate_sampler_metadata(data, now=None, now_unix_ms=None, require_samples=True):
    boot = boottime() if now is None else now
    wall = unix_ms() if now_unix_ms is None else now_unix_ms
    mode = data['gpu_state'] if not data.get('errors') else 'error'
    common = dict(session=data.get('sampler_session'), generation=data.get('sampler_generation'),
                  sample_id=data.get('sample_id'), sampled_at_unix_ms=data.get('sampled_at_unix_ms'),
                  sampled_at_monotonic_ns=data.get('sampled_at_monotonic_ns'))
    validate_sample(dict(common, sensor='cpu', power_state=mode), wall, now_ns=int(boot * 1e9))
    if abs(common['sampled_at_monotonic_ns'] / 1e9 - data['sampled_at']) > 0.001:
        raise ValueError('snapshot clock mismatch')
    for role in ('cpu', 'gpu'):
        sample = data.get(role + '_sample')
        value = data.get(role + '_temp')
        if value is None:
            if sample is not None:
                raise ValueError('sample without a measurement')
        elif require_samples or sample is not None:
            expected = dict(common, sensor=role, power_state=mode)
            if type(sample) is not dict or strict_json(json.dumps(sample)) != expected:
                raise ValueError('sample not paired with snapshot')
            validate_sample(sample, wall, sensor=role, now_ns=int(boot * 1e9))
    return data


def _common(data, now, require_metadata, require_samples, now_unix_ms=None):
    if (type(data) is not dict or type(data.get('schema_version')) is not int
            or data['schema_version'] != 2 or data.get('source') != 'ec0/io' or 'gpu_input' in data):
        raise ValueError('unsupported snapshot schema/source')
    stamp = data.get('sampled_at')
    if finite(stamp) and now - stamp > MAX_AGE:
        raise SensorUnavailable('temperature snapshot stale or invalid')
    if not finite(stamp) or now < stamp:
        raise ValueError('temperature snapshot stale or invalid')
    if require_metadata or has_sampler_metadata(data):
        validate_sampler_metadata(data, now, now_unix_ms, require_samples)
    if data.get('cpu_temp') is None:
        raise SensorUnavailable('CPU temperature unavailable')
    if not valid_temp(data.get('cpu_temp')):
        raise ValueError('CPU temperature unavailable')
    return data


def read_cpu_snapshot_data(data, now=None, require_metadata=False, require_samples=False,
                           now_unix_ms=None):
    return _common(data, boottime() if now is None else now, require_metadata,
                   require_samples, now_unix_ms)


def read_snapshot_data(data, now=None, require_metadata=False, require_samples=False,
                       now_unix_ms=None):
    now = boottime() if now is None else now
    # A hot available sensor overrides even a simultaneous loss of the other.
    if (type(data) is dict and (require_metadata and require_samples or
            data.get('errors') or data.get('cpu_temp') is None or
            data.get('gpu_state') == 'active' and data.get('gpu_temp') is None) and
            any(finite(data.get(role + '_temp')) and data[role + '_temp'] >= limit
                for role, limit in (('cpu', 90000), ('gpu', 80000)))):
        raise ValueError('critical temperature')
    _common(data, now, require_metadata, require_samples, now_unix_ms)
    if data.get('gpu_state') == 'active':
        stamp = data.get('gpu_sampled_at')
        if data.get('gpu_temp') is None and data.get('gpu_power_state') == 'D0':
            raise SensorUnavailable('active GPU temperature unavailable or stale')
        if (not valid_temp(data.get('gpu_temp')) or not finite(stamp)
                or not 0 <= now - stamp <= GPU_MAX_AGE or data.get('gpu_power_state') != 'D0'):
            raise ValueError('active GPU temperature unavailable or stale')
    elif data.get('gpu_state') == 'suspended':
        if (data.get('gpu_power_state') != 'D3cold' or data.get('gpu_temp') is not None
                or data.get('gpu_sampled_at') is not None):
            raise ValueError('GPU sleep not confirmed or fabricated temperature')
    elif data.get('gpu_state') == 'error' and data.get('gpu_power_state') is None:
        raise SensorUnavailable('GPU power state unknown/transitional/error')
    else:
        raise ValueError('GPU power state unknown/transitional/error')
    if type(data.get('errors')) is list and data['errors']:
        raise SensorUnavailable('snapshot reports errors')
    if data.get('errors') != []:
        raise ValueError('snapshot reports errors')
    return data


def _read(path):
    raw = (SNAPSHOT if path is None else path).read_text(encoding='ascii')
    if len(raw) > 16384:
        raise ValueError('snapshot too large')
    return strict_json(raw)


def read_snapshot(path=None, now=None, require_metadata=False, require_samples=False):
    try:
        data = _read(path)
    except OSError as exc:
        raise SensorUnavailable('temperature snapshot unavailable') from exc
    return read_snapshot_data(data, now, require_metadata, require_samples)


def read_cpu_snapshot(path=None, now=None, require_metadata=False, require_samples=False):
    return read_cpu_snapshot_data(_read(path), now, require_metadata, require_samples)


def critical_temperature(data):
    return data['cpu_temp'] >= 90000 or (data['gpu_state'] == 'active' and data['gpu_temp'] >= 80000)


def requested_curve(data):
    if (data['gpu_state'], data['gpu_power_state']) == ('suspended', 'D3cold'):
        return 'cpu'
    if (data['gpu_state'], data['gpu_power_state']) == ('active', 'D0'):
        return 'max'
    raise ValueError('unconfirmed GPU power mode')


def provenance_payload(data, sensor, now_unix_ms=None):
    validator = read_cpu_snapshot_data if sensor == 'cpu' else read_snapshot_data
    validator(data, require_metadata=True, require_samples=True, now_unix_ms=now_unix_ms)
    if sensor not in ('cpu', 'gpu') or data.get(sensor + '_temp') is None:
        raise ValueError('sensor temperature unavailable')
    return {'temperature': data[sensor + '_temp'] / 1000.0, 'sample': data[sensor + '_sample']}


def notify_ready():
    import os
    import socket
    address = os.environ.get('NOTIFY_SOCKET')
    if address:
        if address.startswith('@'):
            address = '\0' + address[1:]
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.sendto(b'READY=1', address)

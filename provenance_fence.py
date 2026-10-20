"""Persistent write-boundary fence. Only the serial adapter owns this state.

Labels are not capabilities: root-only IPC and trusted producer/plugin config
are part of the boundary. No power lease or hardware freshness proof is claimed.
"""
import json
import os
from pathlib import Path
import tempfile
import uuid

import control_policy as policy
import thermal_state as thermal

STATE = thermal.RUNTIME_DIR / 'provenance-fence.json'
MAX_COMMAND_AGE_MS = 2000
MAX_RETAINED_AGE_MS = 65000  # Standard safety latch: 30..60 seconds at 1s polling
SELECTOR_LEASE = 15.0
MAX_OUTPUT_SILENCE = 1900.0  # Standard <=60s * (30 Mix suppressions + 1), plus scheduling slack
MAX_FIRST_OUTPUT_WAIT = 100.0
MAX_HANDOVER = 10.0
HOLD_KEYS = ('held_raw', 'hold_started_at', 'hold_until', 'hold_kind', 'hold_epoch', 'hold_policy')
ENVELOPE = {'schema', 'producer_session', 'device_uid', 'channel_name', 'assignment'}
CALCULATION = {'calculation_id', 'calculated_at_unix_ms', 'profile_uid', 'duty', 'inputs'}
GRAPH = {'kind', 'source_device_uid', 'source_temp_name', 'function_uid', 'sensor_sample',
         'status_observations_newest_first', 'freshness_scope', 'stateful_function'}


# Only exact, source-owned messages may reach logs. Unknown exception text can
# contain JSON, paths, credentials or terminal control characters; never echo it.
SAFE_REJECTION_REASONS = frozenset({
    'provenance rejected', 'invalid provenance members', 'invalid fence state',
    'invalid producer schema', 'invalid producer session UUID', 'invalid assignment counters',
    'invalid identifier', 'invalid live cooling policy', 'bound sensor source changed',
    'hand-over superseded or not full', 'hand-over superseded or not closed', 'thermal epoch changed during PUT',
    'assignment target mismatch', 'assignment inappropriate for power mode', 'stale assignment response',
    'critical temperature', 'fence closed', 'sampler epoch/mode changed', 'selector lease expired',
    'producer output absent', 'no valid first calculation', 'renewal identity mismatch',
    'wrong graph source/function', 'invalid graph freshness/function semantics',
    'invalid observation window', 'invalid newest observation', 'missing/mismatched newest sample',
    'stale/unordered observation', 'observation after calculation', 'invalid input temperature',
    'input power mode mismatch', 'future/unordered original sample', 'sample after observation',
    'current sample value mismatch', 'calculation predates assignment', 'invalid duty',
    'wrong calculation profile', 'stale/future calculation', 'invalid child calculation order',
    'invalid Max mix', 'unknown/duplicate mix member', 'unknown mix member kind',
    'Mix duty is not Maximum(CPU,GPU)', 'producer session/generation/assignment mismatch',
    'replayed/out-of-order command', 'duty argument mismatch', 'invalid last applied duty',
    'last applied duty uncertain', 'sensor grace expired', 'temperature too high for hold',
    'handover expired or invalid', 'unknown fan control mode', 'temperature snapshot unavailable',
    'duplicate JSON member', 'nonfinite JSON', 'incomplete or unknown sensor sample members',
    'invalid sampler session UUID', 'invalid sampler generation/sample ID', 'invalid sample clocks',
    'stale sample or clock discontinuity', 'invalid sensor identity/mode',
    'sensor identity mismatch: sensor', 'sensor identity mismatch: generation', 'sensor identity mismatch: session',
    'snapshot clock mismatch', 'sample without a measurement', 'sample not paired with snapshot',
    'unsupported snapshot schema/source', 'temperature snapshot stale or invalid', 'CPU temperature unavailable',
    'active GPU temperature unavailable or stale', 'GPU sleep not confirmed or fabricated temperature',
    'GPU power state unknown/transitional/error', 'snapshot reports errors', 'snapshot too large',
    'current PCI power mode differs from snapshot', 'lower duty requires producer provenance',
    'GPU changed mode before fan write', 'current power mode mismatch', 'fence is closed without full duty',
    'RPC payload too large', 'invalid provenance arguments', 'invalid command', 'invalid framing', 'oversize reply',
    'expected exactly one NVIDIA display PCI device', 'GPU power state changed during observation',
})


def rejection_reason(error):
    """Bounded diagnostic, not arbitrary exception/cause or caller text."""
    if type(error) is ValueError and error.args == ('lower-duty validation failed; no fan override',):
        error = error.__cause__
    if type(error) in (ValueError, RuntimeError, thermal.SensorUnavailable) and len(error.args) == 1 and type(error.args[0]) is str:
        if error.args[0] in SAFE_REJECTION_REASONS:
            return error.args[0][:160]
        if type(error) is RuntimeError and error.args[0].startswith('GPU power state unconfirmed: '):
            return 'GPU power state unconfirmed'  # Drop the sysfs value/repr suffix.
    for kind, reason in ((OSError, 'OS operation failed'), (json.JSONDecodeError, 'invalid JSON'),
                         (UnicodeError, 'invalid text encoding'), (KeyError, 'missing required member'),
                         (TypeError, 'invalid value type'), (RecursionError, 'nesting limit exceeded'),
                         (SystemExit, 'adapter guard/write failed'), (RuntimeError, 'runtime operation failed')):
        if isinstance(error, kind):
            return reason
    return 'unspecified validation failure'


def require(condition, message='provenance rejected'):
    if not condition:
        raise ValueError(message)


def keys(value, expected):
    require(type(value) is dict and set(value) == set(expected), 'invalid provenance members')


def _atomic(path, value):
    path = STATE if path is None else Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.fence-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='ascii') as stream:
            json.dump(value, stream, separators=(',', ':'), allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _read(path=None):
    value = thermal.strict_json((STATE if path is None else Path(path)).read_text(encoding='ascii'))
    require(type(value) is dict and value.get('phase') in ('closed', 'awaiting', 'active'), 'invalid fence state')
    return value


def close(path=None):
    """Revoke command authority without writing or claiming a known fan duty."""
    _atomic(path, {'phase': 'closed', 'close_id': str(uuid.uuid4()),
                   'full_confirmed': False, 'override_disabled': True})
    return status(path)


def check_hold(state, now=None):
    """A bounded command hold is never a full-duty confirmation."""
    now = thermal.boottime() if now is None else now
    start, end = state.get('hold_started_at'), state.get('hold_until')
    require(thermal.finite(start) and thermal.finite(end) and
            start <= now < end <= start + MAX_HANDOVER, 'handover expired or invalid')
    raw = state.get('held_raw')
    require(type(raw) is list and len(raw) == 2 and
            all(thermal.integer(v, 1, 70) for v in raw), 'invalid last applied duty')
    require(state.get('hold_kind') in ('handover', 'shutdown'), 'invalid fence state')
    return state


def begin_hold(raw, current, live_policy=None, kind='handover', path=None, now=None):
    now = thermal.boottime() if now is None else now
    thermal.read_snapshot_data(current, now, True, True)
    if live_policy is not None:
        policy.validate_policy(live_policy)
    try:
        previous = _read(path)
    except FileNotFoundError:
        previous = {}
    # Neither retries, a new close_id nor assignment installation restart time.
    start, end = now, now + MAX_HANDOVER
    if 'hold_until' in previous:
        check_hold(previous, now)
        start, end = previous['hold_started_at'], previous['hold_until']
    state = dict(phase='closed', close_id=str(uuid.uuid4()), full_confirmed=False,
                 held_raw=list(raw), hold_started_at=start, hold_until=end,
                 hold_kind=kind, hold_epoch=thermal.epoch(current), hold_policy=live_policy)
    check_hold(state, now)
    _atomic(path, state)
    return status(path)


def mark_full(path=None):
    state = _read(path)
    require(state['phase'] == 'closed')
    state['full_confirmed'] = True  # command succeeded, NOT verified physical fan rotation
    for key in HOLD_KEYS:
        state.pop(key, None)
    _atomic(path, state)
    return status(path)


def status(path=None):
    try:
        state = _read(path)
    except (OSError, ValueError):
        return {'phase': 'closed', 'full_confirmed': False}
    return {k: state[k] for k in ('phase', 'close_id', 'full_confirmed', 'override_disabled', 'assignment',
                                 'epoch', 'policy_digest', 'lease_until', 'last_calculation_id',
                                 'last_duty', 'transition_until', *HOLD_KEYS) if k in state}


def assignment(response):
    keys(response, ENVELOPE)
    require(type(response['schema']) is int and response['schema'] == 1, 'invalid producer schema')
    require(thermal.session_uuid(response['producer_session']), 'invalid producer session UUID')
    policy.uid(response['device_uid']); policy.uid(response['channel_name'])
    a = response['assignment']
    keys(a, ('generation', 'profile_uid', 'assigned_at_unix_ms'))
    require(thermal.integer(a['generation'], 1) and thermal.integer(a['assigned_at_unix_ms']),
            'invalid assignment counters')
    policy.uid(a['profile_uid'])
    return response


def install_assignment(response, live_policy, expected_epoch, close_id, current_snapshot,
                       path=None, now=None, now_ms=None):
    now = thermal.boottime() if now is None else now
    wall = thermal.unix_ms() if now_ms is None else now_ms
    assignment(response)
    policy.validate_policy(live_policy)
    thermal.read_snapshot_data(current_snapshot, now, True, True, wall)
    state = _read(path)
    held = 'hold_until' in state
    if held:
        check_hold(state, now)
        require(state['hold_kind'] == 'handover' and
                state['hold_epoch'] == expected_epoch, 'thermal epoch changed during PUT')
    require(state['phase'] == 'closed' and (held or state.get('full_confirmed') is True
            or state.get('override_disabled') is True) and state.get('close_id') == close_id,
            'hand-over superseded or not closed')
    require(expected_epoch == thermal.epoch(current_snapshot), 'thermal epoch changed during PUT')
    target = live_policy['bindings']['target']
    require(response['device_uid'] == target['device_uid']
            and response['channel_name'] == target['channel_name'], 'assignment target mismatch')
    role = 'cpu' if current_snapshot['gpu_state'] == 'suspended' else 'mix'
    require(response['assignment']['profile_uid'] == live_policy['bindings']['profiles'][role],
            'assignment inappropriate for power mode')
    require(0 <= wall - response['assignment']['assigned_at_unix_ms'] <= 15000, 'stale assignment response')
    state.update(phase='awaiting', assignment=response, policy=live_policy,
                 policy_digest=policy.fingerprint(live_policy), epoch=expected_epoch,
                 lease_until=now + SELECTOR_LEASE, installed_at=now,
                 transition_until=now + policy.transition_seconds(live_policy),
                 last_calculation_id=0, last_output_at=None,
                 last_duty=min((v - 1) * 100 // 69 for v in state['held_raw']) if held
                 else 100 if state.get('full_confirmed') is True else None)
    if held:
        state['hold_policy'] = live_policy
    _atomic(path, state)
    return status(path)


def check_current(state, current, now=None, now_ms=None):
    now = thermal.boottime() if now is None else now
    wall = thermal.unix_ms() if now_ms is None else now_ms
    thermal.read_snapshot_data(current, now, True, True, wall)
    # High but valid temperatures remain usable control inputs.
    check_authority(state, now)
    require(state['epoch'] == thermal.epoch(current), 'sampler epoch/mode changed')
    return state


def check_authority(state, now=None):
    """Health independent of sensor availability; grace must not hide expiry."""
    now = thermal.boottime() if now is None else now
    require(state.get('phase') in ('awaiting', 'active'), 'fence closed')
    require(thermal.integer(state.get('last_duty'), 0, 100) or
            state['phase'] == 'awaiting' and state.get('override_disabled') is True
            and state.get('last_duty') is None, 'invalid last applied duty')
    if 'hold_until' in state:
        check_hold(state, now)
    require(0 <= state['lease_until'] - now <= SELECTOR_LEASE, 'selector lease expired')
    last = state.get('last_output_at')
    if state['phase'] == 'active':
        require(last is not None and 0 <= now - last <= MAX_OUTPUT_SILENCE, 'producer output absent')
    else:
        # Allow the live response-delay history to fill after a mode change.
        require(0 <= now - state['installed_at'] <= policy.transition_seconds(state['policy']) + MAX_FIRST_OUTPUT_WAIT,
                'no valid first calculation')
    return state


def renew(response, digest, expected_epoch, current_snapshot, path=None, now=None, now_ms=None):
    state = _read(path)
    now = thermal.boottime() if now is None else now
    check_current(state, current_snapshot, now, now_ms)
    require(response == state['assignment'] and digest == state['policy_digest']
            and expected_epoch == state['epoch'], 'renewal identity mismatch')
    state['lease_until'] = now + SELECTOR_LEASE
    _atomic(path, state)
    return status(path)


def _graph(calc, role, state, current, wall, boot_ns, retained):
    inputs = calc['inputs']
    keys(inputs, GRAPH)
    live = state['policy']
    source = live['bindings']['sources'][role]
    require(inputs['kind'] == 'graph' and inputs['source_device_uid'] == source['device_uid']
            and inputs['source_temp_name'] == source['temp_name']
            and inputs['function_uid'] == live['profiles'][role]['function_uid'], 'wrong graph source/function')
    require(inputs['freshness_scope'] == 'daemon_status_not_sensor_sample'
            and inputs['stateful_function'] is True, 'invalid graph freshness/function semantics')
    observations = inputs['status_observations_newest_first']
    count = policy.history_seconds(live, role)
    require(type(observations) is list and 1 <= len(observations) <= count, 'invalid observation window')
    require(type(observations[0]) is dict, 'invalid newest observation')
    require(type(inputs['sensor_sample']) is dict and inputs['sensor_sample'] == observations[0].get('sensor_sample'),
            'missing/mismatched newest sample')
    # Historical/retained Standard inputs may be older, but NEVER another mode/session/generation.
    age = (MAX_RETAINED_AGE_MS if retained else 0) + count * 1000 + 5000
    previous_stamp, previous_id, previous_ns = wall, current['sample_id'], current['sampled_at_monotonic_ns']
    for index, obs in enumerate(observations):
        keys(obs, ('status_unix_ms', 'temperature', 'sensor_sample'))
        stamp = obs['status_unix_ms']
        require(thermal.integer(stamp) and 0 <= wall - stamp <= age and stamp <= previous_stamp,
                'stale/unordered observation')
        require(stamp <= calc['calculated_at_unix_ms'] + 1000, 'observation after calculation')
        require(thermal.finite(obs['temperature']) and 0 < obs['temperature'] < 120, 'invalid input temperature')
        sample = obs['sensor_sample']
        sample_age = (MAX_RETAINED_AGE_MS if retained else 0) + 5000 if index == 0 else age
        thermal.validate_sample(sample, wall, sample_age, role, current['sampler_generation'],
                                current['sampler_session'], boot_ns)
        require(sample['power_state'] == current['gpu_state'], 'input power mode mismatch')
        require(sample['sample_id'] <= previous_id and sample['sampled_at_monotonic_ns'] <= previous_ns,
                'future/unordered original sample')
        require(sample['sampled_at_unix_ms'] <= stamp + 1000, 'sample after observation')
        if sample['sample_id'] == current['sample_id']:
            require(sample == current[role + '_sample']
                    and obs['temperature'] == current[role + '_temp'] / 1000, 'current sample value mismatch')
        previous_stamp, previous_id, previous_ns = stamp, sample['sample_id'], sample['sampled_at_monotonic_ns']


def _calculation(calc, role, state, current, wall, boot_ns, retained=False, parent=None):
    keys(calc, CALCULATION)
    a = state['assignment']['assignment']
    require(thermal.integer(calc['calculation_id'], a['generation'] + 1), 'calculation predates assignment')
    require(type(calc['duty']) is int and 0 <= calc['duty'] <= 100, 'invalid duty')
    require(calc['profile_uid'] == state['policy']['bindings']['profiles'][role], 'wrong calculation profile')
    stamp = calc['calculated_at_unix_ms']
    limit = MAX_RETAINED_AGE_MS if retained else MAX_COMMAND_AGE_MS
    require(thermal.integer(stamp) and a['assigned_at_unix_ms'] <= stamp <= wall and wall - stamp <= limit,
            'stale/future calculation')
    if parent is not None:
        require(calc['calculation_id'] < parent['calculation_id']
                and stamp <= parent['calculated_at_unix_ms'], 'invalid child calculation order')
    if role != 'mix':
        _graph(calc, role, state, current, wall, boot_ns, retained)
        return
    inputs = calc['inputs']
    keys(inputs, ('kind', 'members'))
    require(inputs['kind'] == 'mix' and type(inputs['members']) is list and len(inputs['members']) == 2,
            'invalid Max mix')
    roles = {state['policy']['bindings']['profiles'][r]: r for r in ('cpu', 'gpu')}
    seen, duties = set(), []
    for member in inputs['members']:
        keys(member, ('profile_uid', 'kind', 'retained', 'calculation'))
        uid = member['profile_uid']
        require(type(uid) is str and uid in roles and uid not in seen, 'unknown/duplicate mix member')
        require(member['kind'] == 'calculated' and type(member['retained']) is bool, 'unknown mix member kind')
        child = member['calculation']
        _calculation(child, roles[uid], state, current, wall, boot_ns, member['retained'], calc)
        seen.add(uid)
        duties.append(child['duty'])
    require(calc['duty'] == max(duties), 'Mix duty is not Maximum(CPU,GPU)')


def validate_command(raw, path=None, now_ms=None, current_snapshot=None, now=None, expected_duty=None):
    state = _read(path)
    now = thermal.boottime() if now is None else now
    wall = thermal.unix_ms() if now_ms is None else now_ms
    current = thermal.read_snapshot(require_metadata=True, require_samples=True) if current_snapshot is None else current_snapshot
    check_current(state, current, now, wall)
    keys(raw, ENVELOPE | {'calculation'})
    envelope = {k: raw[k] for k in ENVELOPE}
    assignment(envelope)
    require(envelope == state['assignment'], 'producer session/generation/assignment mismatch')
    role = 'cpu' if current['gpu_state'] == 'suspended' else 'mix'
    calc = raw['calculation']
    _calculation(calc, role, state, current, wall, int(now * 1e9))
    require(calc['calculation_id'] > state['last_calculation_id'], 'replayed/out-of-order command')
    require(expected_duty is None or type(expected_duty) is int and expected_duty == calc['duty'], 'duty argument mismatch')
    return calc['duty']


def accept_command(raw, path=None, now_ms=None, current_snapshot=None, now=None, expected_duty=None):
    """Reserve ID before writing; the serial writer records duty after success."""
    now = thermal.boottime() if now is None else now
    current = thermal.read_snapshot(require_metadata=True, require_samples=True) if current_snapshot is None else current_snapshot
    duty = validate_command(raw, path, now_ms, current, now, expected_duty)
    state = _read(path)
    if now <= state['transition_until']:
        duty = max(duty, policy.curve_demand(state['policy'], current))
    state.update(phase='active', last_calculation_id=raw['calculation']['calculation_id'],
                 last_output_at=now)
    for key in HOLD_KEYS:
        state.pop(key, None)
    _atomic(path, state)
    return duty


def reject_during_gap(raw, current, expected_duty, path=None):
    """Consume a validated but unapplied command without renewing any deadline."""
    validate_command(raw, path, current_snapshot=current, expected_duty=expected_duty)
    state = _read(path)
    state['last_calculation_id'] = raw['calculation']['calculation_id']
    _atomic(path, state)


def record_applied_duty(duty, path=None):
    """Record a successful command to BOTH fans, not physical rotation/readback.

    Local raises/full writes must not impersonate fresh producer output, renew a
    lease or extend the transition. A failed write closes the fence instead.
    """
    require(thermal.integer(duty, 0, 100), 'invalid duty')
    try:
        state = _read(path)
    except FileNotFoundError:
        if duty == 100:  # Standalone emergency full may precede fence creation.
            return
        raise
    if state['phase'] == 'closed':
        require(duty == 100, 'fence closed')
        return
    state['last_duty'] = duty
    _atomic(path, state)


def watchdog_check(current, path=None, now=None):
    """Validate current health; same-epoch demand rises do not revoke assignment."""
    state = check_current(_read(path), current, now)
    # Awaiting a first validated write may legitimately have unknown duty.
    # check_current/check_authority already validate that specific state.
    return state


def transition_raise_duty(current, path=None, now=None):
    """Return only a needed rise, never a lower command or producer provenance.

    Once transition ends, Standard's stateful producer output controls duty.
    This read-only query does not change the health/status caller's interface.
    """
    now = thermal.boottime() if now is None else now
    state = watchdog_check(current, path, now)
    if (state['phase'] == 'active' or 'hold_until' in state) and now <= state['transition_until']:
        demand = policy.curve_demand(state['policy'], current)
        if demand > state['last_duty']:
            return demand
    return None

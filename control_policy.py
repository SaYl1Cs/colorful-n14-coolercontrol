"""Validate live CoolerControl graphs without editing saved profiles/functions."""
import hashlib
import json
import math
import re

from thermal_state import finite, integer


def require(condition, message='invalid live cooling policy'):
    if not condition:
        raise ValueError(message)


def uid(value):
    require(type(value) is str and re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', value)
            and value not in ('.', '..'), 'invalid identifier')
    return value


def catalog(payload, key):
    require(type(payload) is dict and type(payload.get(key)) is list)
    result = {}
    for item in payload[key]:
        require(type(item) is dict)
        key_id = uid(item.get('uid'))
        require(key_id not in result)
        result[key_id] = item
    return result


def validate_bindings(binding):
    require(type(binding) is dict and set(binding) == {'profiles', 'sources', 'target'})
    require(type(binding['profiles']) is dict and set(binding['profiles']) == {'cpu', 'gpu', 'mix'})
    require(len({uid(v) for v in binding['profiles'].values()}) == 3)
    require(type(binding['sources']) is dict and set(binding['sources']) == {'cpu', 'gpu'})
    for source in binding['sources'].values():
        require(type(source) is dict and set(source) == {'device_uid', 'temp_name'})
        uid(source['device_uid']); uid(source['temp_name'])
    require(binding['sources']['cpu'] != binding['sources']['gpu'])
    target = binding['target']
    require(type(target) is dict and set(target) == {'device_uid', 'channel_name'})
    uid(target['device_uid']); uid(target['channel_name'])
    return binding


def validate_policy(policy):
    require(type(policy) is dict and set(policy) == {'bindings', 'profiles', 'functions', 'temp_max'})
    binding = validate_bindings(policy['bindings'])
    profiles, functions = policy['profiles'], policy['functions']
    require(type(profiles) is dict and set(profiles) == {'cpu', 'gpu', 'mix'})
    require(type(functions) is dict)
    require(type(policy['temp_max']) is dict and set(policy['temp_max']) == {'cpu', 'gpu'})
    referenced = set()
    for role, profile in profiles.items():
        require(type(profile) is dict and profile.get('uid') == binding['profiles'][role])
        uid(profile.get('function_uid'))
        require(type(profile.get('name')) is str)
        common = {'uid', 'name', 'function_uid', 'p_type'}
        if role == 'mix':
            require(set(profile) == common | {'member_profile_uids', 'mix_function_type'})
            require(profile['p_type'] == 'Mix' and profile['mix_function_type'] == 'Max')
            members = profile['member_profile_uids']
            require(type(members) is list and len(members) == 2
                    and set(members) == {binding['profiles']['cpu'], binding['profiles']['gpu']})
            # Mix's default Identity function (UID "0") is NOT a Graph function.
            continue
        require(set(profile) == common | {'speed_profile', 'temp_source', 'temp_min', 'temp_max'})
        require(profile['p_type'] == 'Graph' and profile['temp_source'] == binding['sources'][role],
                'bound sensor source changed')
        points = profile['speed_profile']
        require(type(points) is list and 2 <= len(points) <= 256)
        previous = -1
        for point in points:
            require(type(point) is list and len(point) == 2 and finite(point[0])
                    and 0 <= point[0] <= 120 and integer(point[1], 0, 100) and point[0] > previous)
            previous = point[0]
        low, high = profile['temp_min'], profile['temp_max']
        require(low is None or finite(low) and 0 <= low <= 120)
        require(high is None or finite(high) and 0 <= high <= 120)
        require(low is None or high is None or low < high)
        require(finite(policy['temp_max'][role]) and 0 < policy['temp_max'][role] <= 120)
        fun_id = profile['function_uid']
        referenced.add(fun_id)
        function = functions.get(fun_id)
        require(type(function) is dict and function.get('uid') == fun_id
                and function.get('f_type') == 'Standard')
        for key in ('duty_minimum', 'duty_maximum', 'step_size_min_decreasing', 'step_size_max_decreasing'):
            require(integer(function.get(key), 0, 100))
        for key in ('threshold_hopping', 'bypass_min_at_extremes', 'only_downward'):
            require(type(function.get(key)) is bool)
        require(finite(function.get('deviance')) and 0 <= function['deviance'] <= 120)
        require(integer(function.get('response_delay'), 0, 255))
    require(set(functions) == referenced)
    return policy


def live_policy(binding, profiles, functions, devices):
    validate_bindings(binding)
    selected = {role: profiles[profile_id] for role, profile_id in binding['profiles'].items()}
    referenced = {selected[role]['function_uid'] for role in ('cpu', 'gpu')}
    maxima = {}
    for role, source in binding['sources'].items():
        device = devices[source['device_uid']]
        require(source['temp_name'] in device['info']['temps'])
        maxima[role] = device['info']['temp_max']
    target = binding['target']
    options = devices[target['device_uid']]['info']['channels'][target['channel_name']]['speed_options']
    require(type(options) is dict and options.get('fixed_enabled') is True
            and type(options.get('min_duty')) is int and options['min_duty'] == 0
            and type(options.get('max_duty')) is int and options['max_duty'] == 100)
    return validate_policy(dict(bindings=binding, profiles=selected,
                                functions={key: functions[key] for key in referenced}, temp_max=maxima))


def fingerprint(policy):
    return hashlib.sha256(json.dumps(policy, sort_keys=True, separators=(',', ':'),
                                     allow_nan=False).encode()).hexdigest()


def history_seconds(policy, role):
    return max(1, policy['functions'][policy['profiles'][role]['function_uid']]['response_delay'])


def transition_seconds(policy):
    return max(history_seconds(policy, role) for role in ('cpu', 'gpu')) + 65


def curve_demand(policy, data):
    """Transition floor only; same normalization/interpolation as the producer.

    Does not emulate Standard hysteresis. Stable operation uses producer duty
    unchanged. The source device's temp_max supplies upstream's failsafe point.
    """
    roles = ('cpu',) if data['gpu_state'] == 'suspended' else ('cpu', 'gpu')
    duties = []
    for role in roles:
        points = sorted(policy['profiles'][role]['speed_profile'] + [[policy['temp_max'][role], 100]],
                        key=lambda point: (point[0], -point[1]))
        normalized = []
        for temp, duty in points:
            if normalized and temp == normalized[-1][0]:
                continue
            duty = max(duty, normalized[-1][1] if normalized else 0)
            normalized.append((temp, duty))
            if duty == 100:
                break
        temp = data[role + '_temp'] / 1000
        demand = normalized[-1][1]
        if temp <= normalized[0][0]:
            demand = normalized[0][1]
        else:
            for (a, x), (b, y) in zip(normalized, normalized[1:]):
                if temp <= b:
                    demand = math.floor(x + (temp - a) / (b - a) * (y - x) + 0.5)
                    break
        duties.append(demand)
    return max(duties)

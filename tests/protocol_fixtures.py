"""Shared deterministic offline harness. All state and EC files are temporary."""
from contextlib import ExitStack
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import uuid
from unittest.mock import Mock, patch

import adapter_server as server
import coolercontrol_adapter as adapter
import control_policy
import provenance_fence as fence
import sensor_cache as sensors
import thermal_state as thermal
from profile_selector import ProfileSelector
from profile_selector_service import AdapterFenceRPC

SESSION = '12345678-1234-4234-8234-123456789abc'
PRODUCER = '23456789-2345-4345-9345-23456789abcd'
BINDINGS = {'profiles': {'cpu': 'cpu', 'gpu': 'gpu', 'mix': 'mix'},
            'sources': {r: {'device_uid': 'ec', 'temp_name': r + '_temp'} for r in ('cpu', 'gpu')},
            'target': {'device_uid': 'ec', 'channel_name': 'both_fans'}}
FUNCTION = dict(uid='standard', name='arbitrary', f_type='Standard', duty_minimum=2,
                duty_maximum=100, step_size_min_decreasing=0, step_size_max_decreasing=0,
                threshold_hopping=True, bypass_min_at_extremes=False, deviance=2.0,
                only_downward=True, response_delay=2)
PROFILES = {r: dict(uid=r, name=r, p_type='Graph', function_uid='standard',
                   speed_profile=[[20.0, 30], [90.0, 100]], temp_min=None, temp_max=None,
                   temp_source=BINDINGS['sources'][r]) for r in ('cpu', 'gpu')}
PROFILES['mix'] = dict(uid='mix', name='Max', p_type='Mix', function_uid='0',
                       member_profile_uids=['cpu', 'gpu'], mix_function_type='Max')
DEVICES = {'ec': {'uid': 'ec', 'info': {'temps': {'cpu_temp': {}, 'gpu_temp': {}},
            'temp_max': 100.0, 'driver_info': {'name': 'CustomDevice'}, 'channels': {'both_fans': {
                'speed_options': dict(min_duty=0, max_duty=100, fixed_enabled=True)}}}}}


class RPC(AdapterFenceRPC):
    """Exercise the actual dispatcher with in-process fake transport."""
    def request(self, command, value=None):
        if value is not None:
            command += ' ' + json.dumps(value, separators=(',', ':'))
        reply = server.dispatch(command)
        if not reply.startswith(b'OK '):
            raise ValueError('fake RPC rejected: ' + command.split(' ')[0])
        return json.loads(reply[3:])


class API:
    def __init__(self, harness):
        self.h = harness
        self.profiles = deepcopy(PROFILES)
        self.functions = {'standard': deepcopy(FUNCTION)}
        self.devices = deepcopy(DEVICES)
        self.poll_rate = 1.0
        self.puts = []
        self.applied = None
        self.session = PRODUCER
        self.pid = 10000
        self.started = 0
        self.counter = 0
        self.envelope = None
        self.fail = False
        self.on_put = None

    def request(self, method, path, body=None):
        if self.fail:
            raise RuntimeError('SECRET')
        if method == 'GET':
            if path == '/profiles':
                return {'profiles': deepcopy(list(self.profiles.values()))}
            if path == '/functions':
                return {'functions': deepcopy(list(self.functions.values()))}
            if path == '/devices':
                return {'devices': deepcopy(list(self.devices.values()))}
            if path == '/settings':
                return {'poll_rate': self.poll_rate}
            if path == '/health':
                seconds = int(self.h.boot - self.started)
                return {'details': {'pid': self.pid,
                        'uptime': f'{seconds // 3600:02}:{seconds // 60 % 60:02}:{seconds % 60:02}'}}
            if path == '/devices/ec/settings':
                return {'settings': [{'channel_name': 'both_fans', 'profile_uid': self.applied}]}
            raise AssertionError(path)
        assert method == 'PUT' and path == '/devices/ec/settings/both_fans/profile'
        state = fence.status()
        assert state['phase'] == 'closed'
        if (state.get('full_confirmed') is not True and
                state.get('override_disabled') is not True):
            fence.check_hold(state)
            assert state['hold_kind'] == 'handover'
        self.applied = body['profile_uid']
        self.counter += 1
        self.envelope = dict(schema=1, producer_session=self.session, device_uid='ec',
                             channel_name='both_fans', assignment=dict(generation=self.counter,
                             profile_uid=self.applied, assigned_at_unix_ms=self.h.wall))
        self.puts.append(deepcopy(self.envelope))
        if self.on_put:
            self.on_put()
        return deepcopy(self.envelope)

    def graph(self, role, duty, snapshot=None):
        snap = snapshot or self.h.data
        sample = deepcopy(snap[role + '_sample'])
        inputs = dict(kind='graph', source_device_uid='ec', source_temp_name=role + '_temp',
                      function_uid=self.profiles[role]['function_uid'], sensor_sample=sample,
                      status_observations_newest_first=[dict(status_unix_ms=snap['sampled_at_unix_ms'],
                      temperature=snap[role + '_temp'] / 1000, sensor_sample=deepcopy(sample))],
                      freshness_scope='daemon_status_not_sensor_sample', stateful_function=True)
        self.counter += 1
        return dict(calculation_id=self.counter, calculated_at_unix_ms=self.h.wall,
                    profile_uid=role, duty=duty, inputs=inputs)

    def command(self, duty=80, gpu_duty=75, retained_gpu=None):
        cpu = self.graph('cpu', duty)
        if self.applied == 'cpu':
            calculation = cpu
        else:
            gpu = deepcopy(retained_gpu) if retained_gpu else self.graph('gpu', gpu_duty)
            self.counter += 1
            calculation = dict(calculation_id=self.counter, calculated_at_unix_ms=self.h.wall,
                               profile_uid='mix', duty=max(duty, gpu['duty']), inputs=dict(kind='mix', members=[
                                   dict(profile_uid='cpu', kind='calculated', retained=False, calculation=cpu),
                                   dict(profile_uid='gpu', kind='calculated', retained=retained_gpu is not None,
                                        calculation=gpu)]))
        return dict(deepcopy(self.envelope), calculation=calculation)


class Harness:
    def __init__(self, sleep=False):
        self.stack = ExitStack()
        self.tmp = tempfile.TemporaryDirectory()
        self.stack.callback(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.boot = 1000.0
        self.wall = 1800000000000
        self.sleep = sleep
        self.cpu = 61000
        self.gpu = 55000
        self.state = sensors.SamplerState(SESSION)
        self.snapshot_path = self.root / 'temperatures.json'
        for obj, key, value in ((thermal, 'SNAPSHOT', self.snapshot_path),
                                (fence, 'STATE', self.root / 'fence.json'),
                                (sensors, 'RUNTIME_DIR', self.root)):
            self.stack.enter_context(patch.object(obj, key, value))
        self.stack.enter_context(patch.object(thermal, 'boottime', side_effect=lambda: self.boot))
        self.stack.enter_context(patch.object(thermal, 'unix_ms', side_effect=lambda: self.wall))
        self.stack.enter_context(patch.object(sensors, 'boottime', side_effect=lambda: self.boot))
        self.stack.enter_context(patch.object(sensors, '_unix_ms', side_effect=lambda: self.wall))
        self.stack.enter_context(patch.object(adapter, 'read_live_power_state', side_effect=self.live))
        self.stack.enter_context(patch.object(adapter, 'guarded'))
        self.stack.enter_context(patch.object(adapter, '_LAST_SAFE_SNAPSHOT', None))
        self.stack.enter_context(patch.object(adapter, '_RISE_STATE_UNCERTAIN', False))
        self.stopping = self.stack.enter_context(patch.object(server, 'shutdown_confirmed', return_value=False))
        self.writes = self.stack.enter_context(patch.object(adapter, 'write_raw'))
        self.snapshot()
        fence.close()
        self.rpc = RPC()
        self.api = API(self)
        self.selector = self.make_selector()
        self.watchdog = server.ThermalWatchdog()

    def close(self):
        self.stack.close()

    def live(self):
        return ('suspended', 'D3cold') if self.sleep else ('active', 'D0')

    def snapshot(self, error=False):
        cpu = Mock(read=Mock(return_value=self.cpu))
        gpu = Mock()
        gpu.read.side_effect = ValueError('offline EC failure') if error else None
        gpu.read.return_value = dict(gpu_state=self.live()[0], gpu_power_state=self.live()[1],
                                    gpu_temp=None if self.sleep else self.gpu,
                                    gpu_sampled_at=None if self.sleep else self.boot)
        self.data = sensors.sample(cpu, gpu, self.state)
        sensors.write_snapshot(self.data)
        return deepcopy(self.data)

    def advance(self, seconds=1, sample=True):
        self.boot += seconds
        self.wall += int(seconds * 1000)
        if sample:
            self.snapshot()

    def make_selector(self):
        return ProfileSelector(self.api, BINDINGS, self.rpc, self.snapshot_path)

    def select(self):
        return self.selector.select()

    def send(self, command=None, duty=None):
        command = self.api.command() if command is None else command
        duty = command['calculation']['duty'] if duty is None else duty
        return server.dispatch('set-duty-provenance ' + str(duty) + ' ' + json.dumps(command))

    def policy(self):
        return control_policy.live_policy(BINDINGS, self.api.profiles, self.api.functions, self.api.devices)

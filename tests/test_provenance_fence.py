"""Strict complete producer envelopes; no legacy/synthetic metadata acceptance."""
from copy import deepcopy
import unittest

import provenance_fence as fence
from protocol_fixtures import Harness


class ProvenanceFenceTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.addCleanup(self.h.close)
        self.h.select()

    def validate(self, command):
        return fence.validate_command(command, current_snapshot=self.h.data)

    def test_valid_max_and_replay_reservation(self):
        command = self.h.api.command()
        self.assertEqual(self.validate(command), 80)
        self.assertEqual(fence.accept_command(command, current_snapshot=self.h.data), 80)
        fence.record_applied_duty(80)  # Serial writer records completion after reservation.
        with self.assertRaisesRegex(ValueError, 'replayed'):
            self.validate(command)

    def test_strict_integer_duty_and_ids(self):
        for field in ('duty', 'calculation_id', 'calculated_at_unix_ms'):
            for value in (True, False, 42.0, '42', None, -1):
                command = self.h.api.command()
                command['calculation'][field] = value
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    self.validate(command)

    def test_envelope_identity_exact_and_unknown_fields_rejected(self):
        for field, value in (('producer_session', '3456789a-3456-4456-a456-3456789abcde'),
                             ('device_uid', 'other'), ('channel_name', 'other'), ('schema', True),
                             ('unknown', 1)):
            command = self.h.api.command()
            command[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.validate(command)
        for field, value in (('generation', True), ('profile_uid', 'cpu'), ('assigned_at_unix_ms', 1)):
            command = self.h.api.command()
            command['assignment'][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.validate(command)

    def test_missing_null_fixed_unknown_and_duplicate_mix_members(self):
        base = self.h.api.command()
        members = base['calculation']['inputs']['members']
        mutations = [[], members[:1], members + [members[0]], [members[0], members[0]],
                     [dict(members[0], calculation=None), members[1]],
                     [dict(members[0], kind='other'), members[1]],
                     [dict(profile_uid='cpu', kind='fixed', duty=80), members[1]],
                     [dict(members[0], profile_uid='other'), members[1]],
                     [dict(members[0], retained=1), members[1]]]
        for value in mutations:
            command = deepcopy(base)
            command['calculation']['inputs']['members'] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.validate(command)

    def test_member_order_and_max_duty(self):
        for mutation in ('id', 'timestamp', 'profile', 'max'):
            command = self.h.api.command()
            child = command['calculation']['inputs']['members'][0]['calculation']
            if mutation == 'id':
                child['calculation_id'] = command['calculation']['calculation_id']
            elif mutation == 'timestamp':
                child['calculated_at_unix_ms'] += 1
            elif mutation == 'profile':
                child['profile_uid'] = 'gpu'
            else:
                command['calculation']['duty'] -= 1
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                self.validate(command)

    def test_all_observations_require_original_same_epoch_samples(self):
        for mutation in ('null', 'source', 'function', 'clock', 'generation', 'session', 'sensor',
                         'mode', 'future_id', 'temperature', 'unknown', 'graph_copy'):
            command = self.h.api.command()
            graph = command['calculation']['inputs']['members'][0]['calculation']['inputs']
            obs = graph['status_observations_newest_first'][0]
            sample = obs['sensor_sample']
            if mutation == 'null':
                obs['sensor_sample'] = None
            elif mutation == 'source':
                graph['source_temp_name'] = 'gpu_temp'
            elif mutation == 'function':
                graph['function_uid'] = 'other'
            elif mutation == 'clock':
                sample['sampled_at_monotonic_ns'] -= 10_000_000_000
            elif mutation == 'generation':
                sample['generation'] += 1
            elif mutation == 'session':
                sample['session'] = '3456789a-3456-4456-a456-3456789abcde'
            elif mutation == 'sensor':
                sample['sensor'] = 'gpu'
            elif mutation == 'mode':
                sample['power_state'] = 'suspended'
            elif mutation == 'future_id':
                sample['sample_id'] += 1
            elif mutation == 'temperature':
                obs['temperature'] = 1
            elif mutation == 'unknown':
                sample['extra'] = 1
            else:
                graph['sensor_sample'] = None
            if mutation != 'graph_copy':
                graph['sensor_sample'] = deepcopy(obs['sensor_sample'])
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                self.validate(command)

    def test_reasonable_retained_same_mode_history_and_stale_retained_rejected(self):
        retained = self.h.api.graph('gpu', 75)
        for _ in range(12):
            self.h.advance()
            self.h.select()
        command = self.h.api.command(retained_gpu=retained)
        self.assertEqual(self.validate(command), 80)
        for _ in range(54):
            self.h.advance()
            self.h.select()
        with self.assertRaises(ValueError):
            self.validate(self.h.api.command(retained_gpu=retained))

    def test_history_window_accepts_old_same_mode_not_old_generation(self):
        self.h.api.functions['standard']['response_delay'] = 10
        self.h.select()
        old = deepcopy(self.h.data)
        for _ in range(7):
            self.h.advance()
            self.h.select()
        command = self.h.api.command()
        graph = command['calculation']['inputs']['members'][0]['calculation']['inputs']
        graph['status_observations_newest_first'].append(dict(status_unix_ms=old['sampled_at_unix_ms'],
            temperature=old['cpu_temp']/1000, sensor_sample=old['cpu_sample']))
        self.assertEqual(self.validate(command), 80)
        graph['status_observations_newest_first'][1]['sensor_sample']['generation'] += 1
        with self.assertRaises(ValueError):
            self.validate(command)

    def test_command_age_future_clock_jump_and_duty_argument(self):
        command = self.h.api.command()
        self.h.advance(3)
        with self.assertRaises(ValueError):
            self.validate(command)
        command = self.h.api.command()
        self.h.wall -= 10
        with self.assertRaises(ValueError):
            self.validate(command)
        self.h.wall += 10
        with self.assertRaises(ValueError):
            fence.accept_command(command, current_snapshot=self.h.data, expected_duty=20)

    def test_close_removes_assignment(self):
        command = self.h.api.command()
        fence.close()
        with self.assertRaises(ValueError):
            self.validate(command)


if __name__ == '__main__':
    unittest.main()

"""High valid readings must reach existing curves, not revoke cooling control."""
from copy import deepcopy
import json
import unittest

import adapter_server as server
import coolercontrol_adapter as adapter
import provenance_fence as fence
import thermal_state as thermal
from protocol_fixtures import Harness


class HotCurveTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.addCleanup(self.h.close)

    def test_cpu_100_and_gpu_85_are_published_with_original_samples(self):
        self.h.cpu, self.h.gpu = 100000, 85000
        self.h.advance()
        for role, value in (('cpu', 100.0), ('gpu', 85.0)):
            with self.subTest(sensor=role):
                reply = server.dispatch('temp-' + role + '/json')
                self.assertTrue(reply.startswith(b'OK '), reply)
                payload = json.loads(reply[3:])
                self.assertEqual(payload['temperature'], value)
                self.assertEqual(payload['sample'], self.h.data[role + '_sample'])
        self.h.writes.assert_not_called()

    def test_first_full_command_at_100c_works_in_cpu_and_mix_modes(self):
        for sleeping in (False, True):
            with self.subTest(sleeping=sleeping):
                self.h.sleep = sleeping
                self.h.cpu = 100000
                self.h.advance()
                self.h.select()
                self.assertTrue(self.h.send(self.h.api.command(100, 100)).startswith(b'OK'))
                self.assertEqual(fence.status()['phase'], 'active')
                self.assertEqual(fence.status()['last_duty'], 100)
                self.assertEqual(self.h.writes.call_args.args, (adapter.GPU_TARGET, adapter.MAX_RAW))

    def test_sustained_cpu_90_to_105_does_not_churn_assignment_or_drop_full(self):
        self.h.cpu = 95000
        self.h.advance()
        self.h.select()
        self.assertTrue(self.h.send(self.h.api.command(100, 100)).startswith(b'OK'))
        assignment = deepcopy(fence.status()['assignment'])
        profiles, functions = deepcopy(self.h.api.profiles), deepcopy(self.h.api.functions)
        for tick in range(90):
            self.h.cpu = (90, 100, 95, 105, 99)[tick % 5] * 1000
            self.h.advance()
            self.h.select()
            self.h.watchdog.tick()
            self.assertEqual(fence.status()['phase'], 'active')
            self.assertEqual(fence.status()['assignment'], assignment)
            self.assertTrue(self.h.send(self.h.api.command(100, 100)).startswith(b'OK'))
            self.assertEqual(fence.status()['last_duty'], 100)
        self.assertEqual(len(self.h.api.puts), 1)
        self.assertEqual(self.h.api.profiles, profiles)
        self.assertEqual(self.h.api.functions, functions)
        for call in self.h.writes.call_args_list:
            if call.args[0] != adapter.MODE:
                self.assertEqual(call.args[1], adapter.MAX_RAW)

    def test_gpu_full_curve_works_with_cpu_only_70c(self):
        self.h.cpu, self.h.gpu = 70000, 85000
        self.h.advance()
        self.h.select()
        self.assertTrue(self.h.send(self.h.api.command(54, 100)).startswith(b'OK'))
        self.assertEqual(fence.status()['last_duty'], 100)
        self.h.watchdog.tick()
        self.assertEqual(fence.status()['phase'], 'active')

    def test_curve_and_standard_values_are_not_rewritten_and_no_hot_floor_is_added(self):
        # A fixture with a user-selected 42% upper curve is intentional: removing
        # the rejection must not covertly insert a new 100% override instead.
        for role in ('cpu', 'gpu'):
            self.h.api.profiles[role]['speed_profile'] = [[0.0, 42], [110.0, 42]]
        profiles, functions = deepcopy(self.h.api.profiles), deepcopy(self.h.api.functions)
        self.h.cpu = 100000
        self.h.advance()
        self.h.select()
        for _ in range(7):
            self.h.advance(10)
            self.h.select()
        self.assertTrue(self.h.send(self.h.api.command(42, 42)).startswith(b'OK'))
        self.assertEqual(fence.status()['last_duty'], 42)
        self.h.writes.reset_mock()
        self.h.watchdog.tick()
        self.h.writes.assert_not_called()
        self.assertEqual(self.h.api.profiles, profiles)
        self.assertEqual(self.h.api.functions, functions)

    def test_hot_command_replay_remains_rejected_without_any_extra_writes(self):
        self.h.cpu = 100000
        self.h.advance()
        self.h.select()
        command = self.h.api.command(100, 100)
        self.assertTrue(self.h.send(command).startswith(b'OK'))
        self.h.writes.reset_mock()
        self.assertTrue(self.h.send(command).startswith(b'ERR'))
        self.h.writes.assert_not_called()

    def test_stale_hot_snapshot_is_still_not_a_current_temperature(self):
        self.h.cpu = 100000
        self.h.advance()
        self.h.advance(6, sample=False)
        with self.assertRaises(thermal.SensorUnavailable):
            thermal.read_snapshot(require_metadata=True, require_samples=True)
        self.h.watchdog.tick()
        self.h.writes.assert_not_called()


if __name__ == '__main__':
    unittest.main()

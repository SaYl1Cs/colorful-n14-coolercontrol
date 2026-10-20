"""No emergency speed substitution: real firmware writes are always mocked."""
from copy import deepcopy
import unittest
from unittest.mock import patch

import adapter_server as server
import coolercontrol_adapter as adapter
import provenance_fence as fence
from profile_selector import StagedError
from protocol_fixtures import Harness


class NoEmergencyOverrideTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.addCleanup(self.h.close)

    def active(self, duty=40):
        self.h.select()
        self.assertTrue(self.h.send(self.h.api.command(duty, duty)).startswith(b'OK'))
        self.h.writes.reset_mock()

    def assert_closed_without_writes(self):
        self.h.writes.assert_not_called()
        state = fence.status()
        self.assertEqual(state['phase'], 'closed')
        self.assertTrue(state['override_disabled'])
        self.assertFalse(state['full_confirmed'])
        self.assertNotIn('assignment', state)
        self.assertNotIn('last_duty', state)

    def test_first_assignment_neither_writes_nor_invents_full_confirmation(self):
        self.h.select()
        self.h.writes.assert_not_called()
        self.assertIsNone(fence.status()['last_duty'])
        self.assertFalse(fence.status()['full_confirmed'])
        self.h.watchdog.tick()
        self.h.writes.assert_not_called()
        self.assertTrue(self.h.send().startswith(b'OK'))
        self.assertEqual(fence.status()['last_duty'], 80)

    def test_sensor_loss_has_no_replacement_speed_and_recovers(self):
        self.active()
        self.h.snapshot_path.unlink()
        self.h.advance(60, sample=False)
        self.h.watchdog.tick()
        self.assert_closed_without_writes()
        self.h.advance()
        self.h.select()
        self.h.writes.assert_not_called()
        self.assertIsNone(fence.status()['last_duty'])
        self.assertTrue(self.h.send().startswith(b'OK'))
        adapter.check_applied_duty()

    def test_deep_resume_gap_cannot_accept_old_command_or_force_full(self):
        self.active()
        old = self.h.api.command()
        self.h.advance(3600, sample=False)  # BOOTTIME includes suspend.
        self.h.watchdog.tick()
        self.assert_closed_without_writes()
        self.h.advance()
        self.h.select()
        self.h.writes.assert_not_called()
        self.assertTrue(self.h.send(old).startswith(b'ERR'))
        self.assert_closed_without_writes()
        self.h.select()
        self.assertTrue(self.h.send().startswith(b'OK'))

    def test_high_valid_temperature_does_not_close_or_override_normal_control(self):
        for role, value in (('cpu', 90000), ('gpu', 80000)):
            with self.subTest(role=role):
                self.h.cpu, self.h.gpu = 61000, 55000
                self.h.advance()
                self.active()
                # Past the transition floor, normal producer output is preserved.
                for _ in range(7):
                    self.h.advance(10)
                    self.h.select()
                self.h.writes.reset_mock()
                setattr(self.h, role, value)
                self.h.advance()
                self.h.watchdog.tick()
                self.h.writes.assert_not_called()
                self.assertEqual(fence.status()['phase'], 'active')
                self.assertTrue(self.h.send(self.h.api.command(100, 100)).startswith(b'OK'))
                self.assertEqual(fence.status()['last_duty'], 100)

    def test_stale_replayed_or_malformed_command_never_substitutes_duty(self):
        self.active()
        command = self.h.api.command()
        self.assertTrue(self.h.send(command).startswith(b'OK'))
        self.h.writes.reset_mock()
        self.assertTrue(self.h.send(command).startswith(b'ERR'))
        self.assert_closed_without_writes()
        for request in ('set-duty-provenance 42 {}', 'set-duty-provenance 42 NaN',
                        'set-duty-provenance 42 {', 'set-duty 5'):
            with self.subTest(request=request):
                self.assertTrue(server.dispatch(request).startswith(b'ERR'))
                self.assert_closed_without_writes()

    def test_selector_api_failure_revokes_without_writes_and_recovers(self):
        self.active()
        self.h.api.fail = True
        with self.assertRaises(StagedError):
            self.h.select()
        self.assert_closed_without_writes()
        self.h.api.fail = False
        self.h.select()
        self.h.writes.assert_not_called()
        self.assertTrue(self.h.send().startswith(b'OK'))

    def test_lease_expiry_has_no_fan_override(self):
        self.active()
        self.h.advance(16)
        self.h.watchdog.tick()
        self.assert_closed_without_writes()

    def test_reset_and_legacy_close_full_are_now_no_write_operations(self):
        for action in ('reset', 'lifecycle-stop', 'fence-close-full', 'fence-close'):
            with self.subTest(action=action):
                self.active()
                self.assertTrue(server.dispatch(action).startswith(b'OK'))
                self.assert_closed_without_writes()

    def test_standalone_reset_does_not_write(self):
        adapter.main(['adapter', 'reset'])
        self.assert_closed_without_writes()

    def test_lighting_failure_does_not_issue_recovery_fan_commands(self):
        self.active()
        adapter._keylight_return_fans((0, 1))
        self.assert_closed_without_writes()

    def test_close_storage_failure_cannot_trigger_hardware_write(self):
        self.active()
        with patch.object(fence, 'close', side_effect=OSError('offline state failure')):
            self.assertTrue(server.dispatch('reset').startswith(b'ERR'))
        self.h.writes.assert_not_called()

    def test_each_partial_fan_write_failure_stops_without_retry(self):
        for failed_index, count in ((adapter.MODE, 1), (adapter.CPU_TARGET, 2),
                                    (adapter.GPU_TARGET, 3)):
            with self.subTest(index=failed_index):
                self.h.advance()
                self.active()
                def write(index, raw):
                    if index == failed_index:
                        raise OSError('offline firmware failure')
                with patch.object(adapter, 'write_raw', side_effect=write) as writes:
                    self.assertTrue(self.h.send().startswith(b'ERR'))
                    self.assertEqual(writes.call_count, count)
                    for call in writes.call_args_list:
                        if call.args[0] != adapter.MODE:
                            self.assertLess(call.args[1], adapter.MAX_RAW)
                self.assert_closed_without_writes()
                with self.assertRaisesRegex(ValueError, 'last applied duty uncertain'):
                    adapter.check_applied_duty()
                self.h.select()
                self.assertTrue(self.h.send().startswith(b'OK'))
                adapter.check_applied_duty()

    def test_fresh_zero_producer_demand_is_not_replaced_by_emergency_minimum(self):
        for profile in self.h.api.profiles.values():
            if profile['p_type'] == 'Graph':
                profile['speed_profile'] = [[20.0, 0], [90.0, 0]]
        self.h.select()
        self.h.writes.assert_not_called()
        self.assertTrue(self.h.send(self.h.api.command(0, 0)).startswith(b'OK'))
        self.assertEqual(fence.status()['last_duty'], 0)
        self.assertEqual(self.h.writes.call_args.args, (adapter.GPU_TARGET, adapter.MIN_COMMAND))

    def test_explicit_operator_full_and_normal_full_curve_still_work(self):
        self.h.select()
        self.assertTrue(server.dispatch('set-duty 100').startswith(b'OK'))
        self.assertEqual(self.h.writes.call_args.args, (adapter.GPU_TARGET, adapter.MAX_RAW))
        self.assertTrue(self.h.send(self.h.api.command(100, 100)).startswith(b'OK'))
        self.assertEqual(fence.status()['last_duty'], 100)

    def test_unknown_awaiting_duty_is_not_allowed_in_active_state(self):
        self.h.select()
        state = deepcopy(fence._read())
        state.update(phase='active', last_output_at=self.h.boot)
        fence._atomic(None, state)
        with self.assertRaisesRegex(ValueError, 'invalid last applied duty'):
            fence.watchdog_check(self.h.data)


if __name__ == '__main__':
    unittest.main()

"""Offline lifecycle/grace regressions: no host systemd, EC or service operations."""
from contextlib import nullcontext
from copy import deepcopy
import json
import signal
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import adapter_server as server
import coolercontrol_adapter as adapter
import provenance_fence as fence
import sensor_cache as sensors
import thermal_state as thermal
from profile_selector import APIUnavailable, StagedError
from protocol_fixtures import Harness


class QuietLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.addCleanup(self.h.close)

    def startup(self, cpu=30, gpu=60, mode=2):
        registers = {adapter.MODE: mode, adapter.CPU_TARGET: cpu, adapter.GPU_TARGET: gpu}
        with patch.object(adapter, 'read_raw', side_effect=registers.__getitem__) as reads:
            result = server._bounded_hand_over(startup=True)
        return result, reads

    def active(self):
        self.h.select()
        self.assertTrue(self.h.send(self.h.api.command(80, 75)).startswith(b'OK'))
        self.h.writes.reset_mock()

    def assert_closed_without_override(self):
        state = fence.status()
        self.assertEqual(state['phase'], 'closed')
        self.assertFalse(state['full_confirmed'])
        self.assertTrue(state['override_disabled'])
        self.assertNotIn('assignment', state)
        self.assertNotIn('last_duty', state)  # No fabricated confirmation.
        for call in self.h.writes.call_args_list:
            if call.args[0] in (adapter.CPU_TARGET, adapter.GPU_TARGET):
                self.assertNotEqual(call.args[1], adapter.MAX_RAW)

    def test_startup_reads_both_individual_targets_without_writing_or_claiming_full(self):
        state, reads = self.startup()
        self.assertEqual(state['held_raw'], [30, 60])
        self.assertFalse(state['full_confirmed'])
        self.assertEqual(state['hold_until'], self.h.boot + 10)
        self.assertEqual([call.args[0] for call in reads.call_args_list],
                         [adapter.MODE, adapter.CPU_TARGET, adapter.GPU_TARGET])
        self.h.writes.assert_not_called()
        self.h.watchdog.tick()
        self.h.writes.assert_not_called()

    def test_identity_guard_precedes_startup_readback(self):
        with patch.object(adapter, 'guarded', side_effect=SystemExit(1)), \
                patch.object(adapter, 'read_raw') as reads:
            server._bounded_hand_over(startup=True)
        reads.assert_not_called()
        self.h.writes.assert_not_called()

    def test_invalid_startup_targets_revoke_without_override(self):
        for cpu, gpu, mode in ((0, 40, 2), (40, 0, 2), (71, 40, 2),
                               (True, 40, 2), (40, 40, 0)):
            with self.subTest(cpu=cpu, gpu=gpu, mode=mode):
                self.startup(cpu, gpu, mode)
                self.assert_closed_without_override()

    def test_startup_missing_stale_or_critical_snapshot_never_writes(self):
        for fault in ('missing', 'stale', 'cpu', 'gpu'):
            with self.subTest(fault=fault):
                self.h.cpu, self.h.gpu = 61000, 55000
                self.h.snapshot()
                if fault == 'missing':
                    self.h.snapshot_path.unlink()
                elif fault == 'stale':
                    self.h.advance(6, sample=False)
                else:
                    setattr(self.h, fault, 90000 if fault == 'cpu' else 80000)
                    self.h.snapshot()
                _, reads = self.startup()
                reads.assert_not_called()
                self.assert_closed_without_override()

    def test_startup_deadline_is_not_rearmed_by_restart(self):
        state, _ = self.startup()
        self.h.advance(9)
        again, _ = self.startup()
        self.assertEqual(again['hold_until'], state['hold_until'])
        self.h.advance(1)
        self.startup()
        self.assert_closed_without_override()

    def test_startup_to_assignment_raises_only_weaker_fan_and_keeps_deadline(self):
        before, _ = self.startup()
        self.h.advance(2)
        self.h.select()
        after = fence.status()
        self.assertEqual(after['phase'], 'awaiting')
        self.assertFalse(after['full_confirmed'])
        self.assertEqual(after['hold_until'], before['hold_until'])
        self.assertEqual(after['held_raw'], [50, 60])  # 71% fresh EC demand, individually.
        self.assertEqual([call.args for call in self.h.writes.call_args_list], [(adapter.CPU_TARGET, 50)])
        self.assertEqual(after['last_calculation_id'], 0)

    def test_handover_retries_renewals_and_fresh_samples_cannot_extend_ten_seconds(self):
        before, _ = self.startup()
        for _ in range(9):
            self.h.advance()
            self.h.selector = self.h.make_selector()
            self.h.select()
            self.h.watchdog.tick()
            self.assertEqual(fence.status()['hold_until'], before['hold_until'])
        self.h.advance()
        self.h.watchdog.tick()
        self.assert_closed_without_override()

    def test_first_valid_producer_write_replaces_hold_with_normal_control(self):
        self.startup()
        self.h.select()
        self.assertTrue(self.h.send().startswith(b'OK'))
        self.assertNotIn('hold_until', fence.status())
        count = self.h.writes.call_count
        for _ in range(20):
            self.h.advance()
            self.h.select()
            self.h.watchdog.tick()
        self.assertEqual(self.h.writes.call_count, count)

    def test_awaiting_assignment_locally_raises_without_lowering_peer_or_rearming(self):
        self.startup()
        self.h.select()
        before = deepcopy(fence._read())
        self.h.cpu = 75000
        self.h.advance()
        self.h.writes.reset_mock()
        self.h.watchdog.tick()
        after = fence._read()
        self.assertEqual(after['held_raw'], [60, 60])
        self.assertEqual([call.args for call in self.h.writes.call_args_list], [(adapter.CPU_TARGET, 60)])
        for key in ('hold_until', 'hold_started_at', 'last_output_at', 'last_calculation_id', 'lease_until'):
            self.assertEqual(after[key], before[key])

    def test_mode_handover_preserves_valid_duty_but_rejects_old_command(self):
        self.active()
        old = self.h.api.command()
        self.h.sleep = True
        self.h.advance()
        self.h.watchdog.tick()
        self.assertFalse(fence.status()['full_confirmed'])
        self.h.select()
        self.h.writes.assert_not_called()
        self.assertTrue(self.h.send(old).startswith(b'ERR'))
        self.assert_closed_without_override()

    def test_shutdown_reset_selector_final_and_adapter_final_share_one_deadline(self):
        self.active()
        self.h.stopping.return_value = True
        first = json.loads(server.dispatch('reset')[3:])  # Existing plugin command, unchanged.
        for command in ('lifecycle-stop', 'reset', 'lifecycle-stop'):
            self.h.advance(2)
            reply = json.loads(server.dispatch(command)[3:])
            self.assertEqual(reply['hold_until'], first['hold_until'])
            self.assertEqual(reply['hold_kind'], 'shutdown')
            self.assertFalse(reply['full_confirmed'])
        self.h.writes.assert_not_called()
        self.h.advance(4)
        self.h.watchdog.tick()  # Emergency path cannot use shutdown suppression.
        self.assert_closed_without_override()

    def test_stop_and_legacy_close_full_never_override_fans(self):
        for command, stopping in (('reset', False), ('lifecycle-stop', False), ('fence-close-full', True)):
            with self.subTest(command=command):
                self.active()
                self.h.stopping.return_value = stopping
                self.assertTrue(server.dispatch(command).startswith(b'OK'))
                self.assert_closed_without_override()

    def test_shutdown_invalid_state_rejects_without_override(self):
        for fault in ('unknown', 'missing', 'critical', 'warm'):
            with self.subTest(fault=fault):
                self.h.cpu = 61000
                self.h.snapshot()
                self.active()
                self.h.stopping.return_value = True
                if fault == 'unknown':
                    state = fence._read()
                    state['last_duty'] = None
                    fence._atomic(None, state)
                elif fault == 'missing':
                    self.h.snapshot_path.unlink()
                else:
                    self.h.cpu = 90000 if fault == 'critical' else 80000
                    self.h.snapshot()
                server.dispatch('reset')
                self.assert_closed_without_override()

    def test_shutdown_hold_cannot_install_assignment(self):
        self.active()
        self.h.stopping.return_value = True
        server.dispatch('reset')
        state = fence.status()
        with self.assertRaises(ValueError):
            self.h.rpc.install_assignment(self.h.api.envelope, self.h.policy(), thermal.epoch(self.h.data),
                                          state['close_id'])
        self.assert_closed_without_override()

    def run_service(self, *, crash=False, ready_failure=False, cleanup_failure=False, stop_during_startup=False):
        handlers = {}
        loop = Mock()
        loop.step.side_effect = RuntimeError('offline crash') if crash else lambda: handlers[signal.SIGTERM]()
        loop.close.side_effect = RuntimeError('offline cleanup') if cleanup_failure else None
        def read_raw(index):
            if stop_during_startup:
                handlers[signal.SIGTERM]()  # Handler must exist before startup target reads.
            return 2 if index == adapter.MODE else 40
        with patch.object(server, 'SOCKET', self.h.root / 'adapter.sock'), \
                patch.object(server, 'session_lock', return_value=nullcontext()), \
                patch.object(server.os, 'geteuid', return_value=0), \
                patch.object(server.signal, 'signal', side_effect=lambda n, f: handlers.__setitem__(n, f)), \
                patch.object(server, 'SocketLoop', return_value=loop), \
                patch.object(server, 'notify_ready', side_effect=RuntimeError('offline setup') if ready_failure else None), \
                patch.object(adapter, 'read_raw', side_effect=read_raw):
            server.serve()
        if stop_during_startup:
            loop.step.assert_not_called()

    def test_service_stop_never_overrides_and_shutdown_can_preserve_hold(self):
        for startup in (False, True):
            with self.subTest(stop_during_startup=startup):
                self.h.stopping.return_value = False
                self.run_service(stop_during_startup=startup)
                self.assert_closed_without_override()
                self.h.writes.reset_mock()
                self.h.stopping.return_value = True
                self.run_service(stop_during_startup=startup)
                self.h.writes.assert_not_called()
                self.assertEqual(fence.status()['hold_kind'], 'shutdown')

    def test_service_setup_and_runtime_exception_revoke_without_override(self):
        self.h.stopping.return_value = True
        for fault in ('runtime', 'ready', 'cleanup'):
            with self.subTest(fault=fault), self.assertRaises(RuntimeError):
                self.run_service(crash=fault == 'runtime', ready_failure=fault == 'ready',
                                 cleanup_failure=fault == 'cleanup')
            self.assert_closed_without_override()

    def test_sensor_gap_selector_watchdog_and_valid_rejected_command_do_not_write(self):
        self.active()
        command = self.h.api.command(60, 55)
        before = deepcopy(fence._read())
        self.h.advance(sample=False)
        self.h.snapshot(error=True)
        self.h.watchdog.tick()
        with self.assertRaises(StagedError):
            self.h.select()
        self.assertIn(b'bounded last-duty hold', self.h.send(command))
        self.h.writes.assert_not_called()
        after = fence._read()
        for key in ('last_output_at', 'lease_until', 'transition_until', 'last_duty'):
            self.assertEqual(after[key], before[key])
        self.assertEqual(after['last_calculation_id'], command['calculation']['calculation_id'])

    def test_replaying_command_during_gap_revokes_without_override(self):
        self.active()
        command = self.h.api.command()
        self.h.advance(sample=False)
        self.h.snapshot(error=True)
        self.assertIn(b'bounded last-duty hold', self.h.send(command))
        self.assertTrue(self.h.send(command).startswith(b'ERR'))
        self.assert_closed_without_override()

    def test_sensor_gap_recovery_uses_new_epoch_and_fresh_producer_command(self):
        self.active()
        self.h.advance(sample=False)
        self.h.snapshot(error=True)
        self.h.watchdog.tick()
        self.h.advance()
        self.h.watchdog.tick()
        self.h.select()
        self.h.writes.assert_not_called()
        self.assertTrue(self.h.send().startswith(b'OK'))
        self.assertEqual(fence.status()['phase'], 'active')

    def test_repeated_error_snapshots_and_checks_cannot_rearm_five_seconds(self):
        self.active()
        original = self.h.boot
        for _ in range(4):
            self.h.advance(sample=False)
            self.h.snapshot(error=True)
            self.h.watchdog.tick()
            result = self.h.rpc.sensor_check()
            self.assertEqual(result['sensor_grace_until'], original + 5)
            self.assertEqual(adapter._LAST_SAFE_SNAPSHOT['sampled_at'], original)
        self.h.writes.assert_not_called()
        self.h.advance(sample=False)
        self.h.snapshot(error=True)
        self.h.watchdog.tick()
        self.assert_closed_without_override()
        self.h.rpc.sensor_check()
        self.assertNotIn('hold_until', fence.status())

    def test_existing_sample_age_is_counted_not_stamped_fresh_on_watchdog_read(self):
        self.active()
        stamp = adapter._LAST_SAFE_SNAPSHOT['sampled_at']
        self.h.advance(4, sample=False)
        self.h.watchdog.tick()  # The same old snapshot is still nominally valid.
        self.assertEqual(adapter._LAST_SAFE_SNAPSHOT['sampled_at'], stamp)
        self.h.snapshot_path.unlink()
        result = self.h.rpc.sensor_check()
        self.assertEqual(result['sensor_grace_until'], stamp + 5)
        self.h.advance(1, sample=False)
        self.h.rpc.sensor_check()
        self.assert_closed_without_override()

    def test_no_prior_safe_sample_or_known_duty_has_no_sensor_grace(self):
        self.h.snapshot(error=True)
        self.h.watchdog.tick()
        self.assert_closed_without_override()

    def test_incomplete_snapshot_never_fabricates_hot_curve_inputs(self):
        for role in ('cpu', 'gpu'):
            with self.subTest(role=role):
                self.h.cpu, self.h.gpu = 61000, 55000
                self.h.snapshot()
                self.active()
                if role == 'cpu':
                    self.h.cpu = 90000
                    self.h.snapshot(error=True)
                else:
                    self.h.cpu, self.h.gpu = None, 80000
                    self.h.snapshot()
                self.h.watchdog.next_check = 0
                self.h.watchdog.tick()
                self.h.writes.assert_not_called()
                self.h.advance(6, sample=False)
                self.h.watchdog.tick()
                self.assert_closed_without_override()

    def test_cpu_loss_is_typed_thermal_error_with_paired_gpu_error_metadata(self):
        self.active()
        cpu, gpu = Mock(), Mock()
        cpu.read.side_effect = OSError('offline')
        gpu.read.return_value = dict(gpu_state='active', gpu_power_state='D0', gpu_temp=55000,
                                     gpu_sampled_at=self.h.boot)
        data = sensors.sample(cpu, gpu, self.h.state)
        self.assertEqual(data['gpu_sample']['power_state'], 'error')
        sensors.write_snapshot(data)
        self.h.watchdog.tick()
        self.h.writes.assert_not_called()

    def test_sensor_grace_does_not_hide_unknown_duty_or_expired_authority(self):
        for key, value in (('last_duty', None), ('lease_until', self.h.boot - 1)):
            with self.subTest(key=key):
                self.h.snapshot()
                self.active()
                state = fence._read()
                state[key] = value
                fence._atomic(None, state)
                self.h.snapshot(error=True)
                self.h.rpc.sensor_check()
                self.assert_closed_without_override()

    def test_malformed_snapshot_or_command_is_not_a_thermal_grace(self):
        for fault in ('json', 'schema', 'sample', 'future', 'command'):
            with self.subTest(fault=fault):
                self.h.snapshot()
                self.active()
                data = deepcopy(self.h.data)
                if fault == 'command':
                    self.h.snapshot(error=True)
                    self.h.send({'bad': 'command'}, duty=20)
                else:
                    if fault == 'schema':
                        data['schema_version'] = 0
                    elif fault == 'sample':
                        data['cpu_sample'] = None
                    elif fault == 'future':
                        data['sampled_at'] += 1
                    self.h.snapshot_path.write_text('{' if fault == 'json' else json.dumps(data))
                    self.h.watchdog.next_check = 0
                    self.h.watchdog.tick()
                self.assert_closed_without_override()

    def test_actual_write_failure_never_gets_grace(self):
        self.active()
        def fail(index, raw):
            if index == adapter.CPU_TARGET and raw < adapter.MAX_RAW:
                raise OSError('offline partial write')
        self.h.writes.side_effect = fail
        self.assertTrue(self.h.send().startswith(b'ERR'))
        self.assert_closed_without_override()
        self.assertIsNone(adapter._LAST_SAFE_SNAPSHOT)

    def test_handover_raise_failure_has_no_recovery_fan_write(self):
        self.startup()
        def fail_lower(index, raw):
            if index != adapter.MODE and raw < adapter.MAX_RAW:
                raise OSError('offline partial handover write')
        self.h.writes.side_effect = fail_lower
        self.h.select()
        # Only the originally attempted raise is allowed; no recovery fan write.
        self.assertEqual(self.h.writes.call_count, 1)
        self.assertNotEqual(self.h.writes.call_args.args[1], adapter.MAX_RAW)
        self.assertNotIn('hold_until', fence.status())

    def test_old_gpu_measurement_consumes_grace_even_with_newer_cpu_stamp(self):
        data = deepcopy(self.h.data)
        data['gpu_sampled_at'] -= 4
        sensors.write_snapshot(data)
        self.active()
        self.h.advance(sample=False)
        self.h.snapshot(error=True)
        self.h.rpc.sensor_check()
        self.assert_closed_without_override()

    def test_wall_clock_age_also_bounds_sensor_grace(self):
        self.active()
        self.h.advance(4.25, sample=False)
        self.h.wall += 750  # Within existing 1s discontinuity tolerance, but age is 5s.
        self.h.snapshot_path.unlink()
        self.h.rpc.sensor_check()
        self.assert_closed_without_override()

    def test_corrupt_hold_marker_or_expired_close_id_cannot_install(self):
        for key, value in (('held_raw', [0, 40]), ('held_raw', [True, 40]),
                           ('hold_until', self.h.boot), ('hold_until', self.h.boot + 11)):
            with self.subTest(key=key, value=value):
                server._close_without_override()
                self.startup()
                state = fence._read()
                state[key] = value
                fence._atomic(None, state)
                self.h.watchdog.next_check = 0
                self.h.watchdog.tick()
                self.assert_closed_without_override()

    def test_no_caller_supplied_shutdown_flag_can_bypass_validation(self):
        self.active()
        self.assertTrue(server.dispatch('reset stopping').startswith(b'ERR'))
        self.assertNotIn('hold_until', fence.status())
        self.h.writes.assert_not_called()
        server.dispatch('reset')
        self.assert_closed_without_override()

    def test_valid_stopped_raw_one_is_preserved_but_not_invented(self):
        state, _ = self.startup(1, 1)
        self.assertEqual(state['held_raw'], [1, 1])
        self.h.writes.assert_not_called()
        self.h.select()
        self.assertEqual(fence.status()['held_raw'], [50, 50])
        self.assertNotIn((adapter.MODE, 2), [c.args for c in self.h.writes.call_args_list])

    def test_selector_inflight_disconnect_only_holds_after_stop_and_confirmed_shutdown(self):
        for stop_requested, confirmed, malformed in ((True, True, False), (True, False, False),
                                                     (False, True, False), (True, True, True)):
            with self.subTest(stop=stop_requested, confirmed=confirmed, malformed=malformed):
                server._close_without_override()
                self.active()
                self.h.stopping.return_value = confirmed
                self.h.selector._stopping = lambda: stop_requested
                error = StagedError('selector validation rejected') if malformed else APIUnavailable('local API request failed')
                with patch.object(self.h.api, 'request', side_effect=error), self.assertRaises(StagedError):
                    self.h.select()
                if stop_requested and confirmed and not malformed:
                    self.h.writes.assert_not_called()
                    self.assertEqual(fence.status()['hold_kind'], 'shutdown')
                else:
                    self.assert_closed_without_override()

    def test_api_policy_failure_not_mistaken_for_sensor_grace(self):
        self.active()
        self.h.api.fail = True
        with self.assertRaises(StagedError):
            self.h.select()
        self.assert_closed_without_override()


class ShutdownObservationTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.cache = patch.object(server, '_STOPPING_UNTIL', 0.0)
        self.clock = patch.object(thermal, 'boottime', side_effect=lambda: self.now)
        self.run = patch.object(server.subprocess, 'run')
        self.cache.start()
        self.clock.start()
        self.mock_run = self.run.start()
        self.addCleanup(self.cache.stop)
        self.addCleanup(self.clock.stop)
        self.addCleanup(self.run.stop)

    @staticmethod
    def result(text, code=0):
        return SimpleNamespace(stdout=text, returncode=code)

    def test_stopping_confirmation_is_cached_only_for_ten_seconds(self):
        self.mock_run.return_value = self.result(b'stopping\n', 1)
        self.assertTrue(server.shutdown_confirmed())
        self.now += 9
        self.assertTrue(server.shutdown_confirmed())
        self.assertEqual(self.mock_run.call_count, 1)
        self.now += 1
        self.mock_run.side_effect = [self.result(b'running\n'), self.result(b'No jobs running.\n')]
        self.assertFalse(server.shutdown_confirmed())
        self.assertEqual(self.mock_run.call_count, 3)
        for call in self.mock_run.call_args_list:
            self.assertLessEqual(call.kwargs['timeout'], 1.0)
            self.assertGreater(call.kwargs['timeout'], 0)
            self.assertNotIn('shell', call.kwargs)
            self.assertEqual(call.args[0][0], '/usr/bin/systemctl')

    def test_only_exact_shutdown_start_jobs_confirm(self):
        for target in ('shutdown', 'reboot', 'poweroff', 'halt'):
            server._STOPPING_UNTIL = 0
            self.mock_run.side_effect = [self.result(b'running\n'),
                                        self.result(f'42 {target}.target start waiting\n'.encode())]
            self.assertTrue(server.shutdown_confirmed())
        for line in (b'42 reboot.target stop waiting\n', b'42 fake-reboot.target start running\n',
                     b'42 coolercontrold.service stop running\n', b'reboot.target\n',
                     b'42 reboot.target start waiting\n'):
            server._STOPPING_UNTIL = 0
            code = 1 if line == b'42 reboot.target start waiting\n' else 0
            self.mock_run.side_effect = [self.result(b'running\n'), self.result(line, code)]
            self.assertFalse(server.shutdown_confirmed())

    def test_timeout_failure_and_nonshutdown_state_never_cache_a_bypass(self):
        for failure in (OSError('offline'), subprocess.TimeoutExpired('systemctl', 1)):
            self.mock_run.side_effect = failure
            self.assertFalse(server.shutdown_confirmed())
            self.assertEqual(server._STOPPING_UNTIL, 0)
        self.mock_run.side_effect = None
        self.mock_run.return_value = self.result(b'unknown\n', 1)
        self.assertFalse(server.shutdown_confirmed())
        self.assertEqual(server._STOPPING_UNTIL, 0)


if __name__ == '__main__':
    unittest.main()

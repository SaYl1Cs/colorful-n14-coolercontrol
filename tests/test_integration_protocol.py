"""Full offline protocol: sampler -> producer labels -> C/socket -> fake EC writes."""
from contextlib import redirect_stderr
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import threading
import time
import unittest
from unittest.mock import Mock, patch

import adapter_server as server
import coolercontrol_adapter as adapter
import control_policy
import provenance_fence as fence
import sensor_cache
import thermal_state
from profile_selector import StagedError
from protocol_fixtures import Harness

ROOT = Path(__file__).resolve().parents[1]


class FlowTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.addCleanup(self.h.close)

    def test_restart_health_change_reassigns_before_new_command(self):
        self.h.select()
        count = len(self.h.api.puts)
        self.h.api.pid += 1
        self.h.api.session = '3456789a-3456-4456-a456-3456789abcde'
        self.h.select()
        self.assertEqual(len(self.h.api.puts), count + 1)
        self.assertEqual(fence.status()['assignment']['producer_session'], self.h.api.session)
        self.h.api.started = self.h.boot  # Even PID reuse with lower uptime triggers a handover.
        self.h.select()
        self.assertEqual(len(self.h.api.puts), count + 2)

    def test_close_storage_failure_never_writes_fans(self):
        self.h.select()
        with patch.object(fence, 'close', side_effect=OSError('fixture filesystem failure')):
            self.assertTrue(server.dispatch('reset').startswith(b'ERR'))
        self.assertFalse(fence.status()['full_confirmed'])
        self.assertTrue(fence.status()['override_disabled'])

    def test_bound_capture_only_reads_catalog_and_preserves_graphs(self):
        from capture_profile_bindings import capture
        from protocol_fixtures import BINDINGS
        before = deepcopy(self.h.api.profiles)
        self.assertEqual(capture(self.h.api, BINDINGS['profiles']), BINDINGS)
        self.assertEqual(self.h.api.profiles, before)
        self.assertEqual(self.h.api.puts, [])
        self.h.api.profiles['cpu']['temp_source']['temp_name'] = 'gpu_temp'
        with self.assertRaises(ValueError):
            capture(self.h.api, BINDINGS['profiles'])

    def test_snapshot_metadata_future_clocks_and_missing_sensor_rejected(self):
        for field, value in (('sampled_at_monotonic_ns', int((self.h.boot+1)*1e9)),
                             ('sampler_generation', True), ('sampler_session', 'not-uuid'),
                             ('sample_id', 0), ('cpu_sample', None), ('gpu_sample', None)):
            bad = dict(deepcopy(self.h.data), **{field: value})
            with self.subTest(field=field), self.assertRaises(ValueError):
                thermal_state.read_snapshot_data(bad, require_metadata=True, require_samples=True)

    def test_nested_dedup_can_hold_valid_duty_for_over_thirty_minutes(self):
        self.h.api.functions['standard']['response_delay'] = 60
        self.h.select()
        self.assertTrue(self.h.send().startswith(b'OK'))
        count = self.h.writes.call_count
        for _ in range(1860):
            self.h.advance()
            self.h.select()
            self.h.watchdog.tick()
        self.assertEqual(self.h.writes.call_count, count)
        self.assertEqual(len(self.h.api.puts), 1)
        self.assertTrue(self.h.send().startswith(b'OK'))
        self.assertEqual(fence.status()['phase'], 'active')

    def test_active_sleep_wake_never_unlocks_cpu_from_http_success(self):
        self.h.select()
        old_active = self.h.api.command()
        self.assertTrue(self.h.send(old_active).startswith(b'OK'))
        self.h.sleep = True
        self.h.advance()
        self.h.watchdog.tick()
        self.assertEqual(fence.status()['phase'], 'closed')
        self.assertEqual(self.h.select(), 'cpu')
        sleep_command = self.h.api.command()
        self.assertTrue(self.h.send(sleep_command).startswith(b'OK'))
        self.h.sleep = False  # Wake before the sampler has published the new mode
        self.assertTrue(self.h.send(self.h.api.command()).startswith(b'ERR'))
        self.assertFalse(fence.status()['full_confirmed'])
        self.assertTrue(fence.status()['override_disabled'])
        self.h.advance()
        self.assertEqual(self.h.select(), 'mix')
        self.assertEqual(fence.status()['phase'], 'awaiting')
        for old in (sleep_command, old_active):
            self.assertTrue(self.h.send(old).startswith(b'ERR'))
            self.h.select()
        self.assertTrue(self.h.send().startswith(b'OK'))

    def test_every_lower_write_checks_current_pci_state(self):
        self.h.select()
        command = self.h.api.command()
        with patch.object(adapter, 'read_live_power_state', side_effect=RuntimeError('resuming')):
            self.assertTrue(self.h.send(command).startswith(b'ERR'))
        self.assertEqual(fence.status()['phase'], 'closed')
        self.assertFalse(fence.status()['full_confirmed'])
        self.assertTrue(fence.status()['override_disabled'])

    def test_watchdog_transient_holds_then_closes_without_override(self):
        for fault in ('missing', 'stale', 'error', 'transition', 'lease'):
            self.h.snapshot()
            self.h.select()
            self.assertTrue(self.h.send().startswith(b'OK'))
            if fault == 'missing':
                self.h.snapshot_path.unlink()
                self.h.advance(sample=False)
            elif fault == 'stale':
                self.h.advance(6, sample=False)
            elif fault == 'error':
                self.h.advance()
                self.h.snapshot(error=True)
            elif fault == 'transition':
                self.h.sleep = not self.h.sleep
                self.h.advance()
            else:
                self.h.advance(16)
            self.h.watchdog.tick()
            if fault in ('missing', 'error'):
                self.assertEqual(fence.status()['phase'], 'active', fault)
                self.h.advance(5, sample=False)
                self.h.watchdog.tick()
            elif fault == 'transition':
                self.assertEqual(fence.status()['phase'], 'closed', fault)
                self.assertFalse(fence.status()['full_confirmed'])
                fence.check_hold(fence.status())
                continue
            self.assertEqual(fence.status()['phase'], 'closed', fault)
            self.assertFalse(fence.status()['full_confirmed'])
            self.assertTrue(fence.status()['override_disabled'])

    def test_producer_sampler_selector_and_adapter_restarts(self):
        self.h.select()
        original = self.h.api.command()
        self.assertTrue(self.h.send(original).startswith(b'OK'))
        self.h.api.session = '3456789a-3456-4456-a456-3456789abcde'
        # A new producer is not authorized just because its configured profile is unchanged.
        self.h.api.envelope['producer_session'] = self.h.api.session
        self.assertTrue(self.h.send().startswith(b'ERR'))
        self.h.select()
        self.assertTrue(self.h.send().startswith(b'OK'))
        self.h.state = sensor_cache.SamplerState()
        self.h.advance()
        self.h.watchdog.tick()
        self.h.select()
        self.assertTrue(self.h.send().startswith(b'OK'))
        self.h.selector = self.h.make_selector()
        self.h.select()
        fence.close()  # adapter restart invalidates even an otherwise fresh label
        self.h.select()
        self.assertTrue(self.h.send().startswith(b'OK'))
        self.assertTrue(self.h.send(original).startswith(b'ERR'))

    def test_transition_floor_is_temporary_not_permanent_curve_override(self):
        self.h.select()
        self.assertTrue(self.h.send(self.h.api.command(duty=5, gpu_duty=6)).startswith(b'OK'))
        self.assertGreater(fence.status()['last_duty'], 6)
        for tick in range(80):
            self.h.advance()
            self.h.select()
            if tick % 20 == 0:
                self.h.send(self.h.api.command(duty=5, gpu_duty=6))
            self.h.watchdog.tick()
        self.assertTrue(self.h.send(self.h.api.command(duty=5, gpu_duty=6)).startswith(b'OK'))
        self.assertEqual(fence.status()['last_duty'], 6)
        self.assertEqual(len(self.h.api.puts), 1)

    def test_transition_demand_increase_raises_without_reassigning_or_faking_output(self):
        self.h.select()
        self.h.send(self.h.api.command(duty=5, gpu_duty=6))
        before = fence._read()
        self.h.writes.reset_mock()
        self.h.cpu = 85000
        self.h.advance()
        self.h.watchdog.tick()
        after = fence._read()
        self.assertEqual(after['phase'], 'active')
        self.assertEqual(after['last_duty'], 95)
        for key in ('assignment', 'epoch', 'transition_until', 'last_calculation_id',
                    'last_output_at', 'lease_until'):
            self.assertEqual(after[key], before[key], key)
        raw = adapter.MIN_COMMAND + round(95 * (adapter.MAX_RAW - adapter.MIN_COMMAND) / 100)
        self.assertEqual([c.args for c in self.h.writes.call_args_list],
                         [(adapter.MODE, 2), (adapter.CPU_TARGET, raw), (adapter.GPU_TARGET, raw)])
        self.h.select()
        self.assertEqual(len(self.h.api.puts), 1)

    def test_fluctuating_temperatures_keep_one_assignment_through_entire_transition(self):
        # Sparse commands model Standard dedup, not fabricated watchdog output.
        for sleep, sensor in ((True, 'cpu'), (False, 'cpu'), (False, 'gpu')):
            with self.subTest(sleep=sleep, sensor=sensor):
                h = Harness(sleep=sleep)
                try:
                    h.api.functions['standard']['response_delay'] = 60
                    h.gpu = 61000
                    h.snapshot()
                    h.select()
                    self.assertTrue(h.send(h.api.command(duty=5, gpu_duty=6)).startswith(b'OK'))
                    initial = fence._read()
                    for tick in range(1, 136):
                        before = fence._read()
                        setattr(h, sensor, (61 + (0, 1, 0, 2, 1)[tick % 5]) * 1000)
                        h.advance()
                        h.writes.reset_mock()
                        h.watchdog.tick()
                        after = fence._read()
                        self.assertEqual(after['phase'], 'active')
                        expected = before['last_duty']
                        if h.boot <= initial['transition_until']:
                            expected = max(expected, control_policy.curve_demand(h.policy(), h.data))
                        self.assertEqual(after['last_duty'], expected)
                        self.assertEqual(h.writes.call_count, 3 if expected > before['last_duty'] else 0)
                        for key in ('assignment', 'epoch', 'transition_until', 'last_calculation_id',
                                    'last_output_at', 'lease_until'):
                            self.assertEqual(after[key], before[key], key)
                        h.select()
                        if tick % 19 == 0:
                            self.assertTrue(h.send(h.api.command(duty=5, gpu_duty=6)).startswith(b'OK'))
                        for call in h.writes.call_args_list:
                            if call.args[0] != adapter.MODE:
                                self.assertLess(call.args[1], adapter.MAX_RAW)
                    self.assertEqual(len(h.api.puts), 1)
                    self.assertEqual(fence.status()['transition_until'], initial['transition_until'])
                    # After transition expiry, leave Standard's lower output alone
                    # even when the current raw curve asks for a higher duty.
                    self.assertEqual(fence.status()['last_duty'], 5 if sleep else 6)
                finally:
                    h.close()

    def test_watchdog_rechecks_latest_snapshot_before_local_rise(self):
        for temperature, expected in ((59000, 71), (85000, 95)):
            with self.subTest(temperature=temperature):
                self.h.cpu = 61000
                self.h.advance()
                self.h.select()
                self.h.send(self.h.api.command(duty=5, gpu_duty=6))
                self.h.cpu = 62000
                self.h.advance()
                self.h.writes.reset_mock()
                # Snapshot changes between the watchdog decision and serial write.
                def newer_snapshot(*args, **kwargs):
                    self.h.cpu = temperature
                    self.h.snapshot()
                with patch.object(adapter, 'guarded', side_effect=newer_snapshot):
                    self.h.watchdog.tick()
                self.assertEqual(fence.status()['last_duty'], expected)
                self.assertEqual(self.h.writes.call_count, 0 if expected == 71 else 3)
                self.assertEqual(len(self.h.api.puts), 1)

    def test_explicit_full_is_recorded_and_watchdog_cannot_lower_it(self):
        self.h.select()
        self.h.send(self.h.api.command(duty=5, gpu_duty=6))
        before = fence._read()
        self.assertTrue(server.dispatch('set-duty 100').startswith(b'OK'))
        self.assertEqual(fence.status()['last_duty'], 100)
        for key in ('last_calculation_id', 'last_output_at', 'transition_until', 'lease_until'):
            self.assertEqual(fence._read()[key], before[key])
        self.h.cpu = 85000
        self.h.advance()
        self.h.writes.reset_mock()
        self.h.watchdog.tick()
        self.h.writes.assert_not_called()
        self.h.select()
        self.assertEqual(len(self.h.api.puts), 1)
        # Only a fresh validated producer command can lower the explicit full.
        self.assertTrue(self.h.send(self.h.api.command(duty=5, gpu_duty=6)).startswith(b'OK'))
        self.assertEqual(fence.status()['last_duty'], 95)

    def test_actual_duty_is_recorded_only_after_both_fans_succeed(self):
        self.h.select()
        def still_full(*args):
            self.assertIsNone(fence.status()['last_duty'])
            self.assertGreater(fence.status()['last_calculation_id'], 0)
        with patch.object(adapter, 'write_raw', side_effect=still_full) as writes:
            self.assertTrue(self.h.send(self.h.api.command(duty=5, gpu_duty=6)).startswith(b'OK'))
        self.assertEqual(writes.call_count, 3)
        self.assertEqual(fence.status()['last_duty'], 71)
        self.h.cpu = 62000
        self.h.advance()
        def still_previous(*args):
            self.assertEqual(fence.status()['last_duty'], 71)
        with patch.object(adapter, 'write_raw', side_effect=still_previous) as writes:
            self.h.watchdog.tick()
        self.assertEqual(writes.call_count, 3)
        self.assertEqual(fence.status()['last_duty'], 72)

    def test_local_rises_do_not_keep_a_silent_producer_alive(self):
        self.h.select()
        self.h.send(self.h.api.command(duty=5, gpu_duty=6))
        with patch.object(fence, 'MAX_OUTPUT_SILENCE', 2):
            self.h.cpu = 62000
            self.h.advance()
            self.h.watchdog.tick()
            self.assertEqual(fence.status()['last_duty'], 72)
            self.h.cpu = 63000
            self.h.advance(2)
            with redirect_stderr(io.StringIO()) as errors:
                self.h.watchdog.tick()
        self.assertIn('producer output absent', errors.getvalue())
        self.assertEqual(fence.status()['phase'], 'closed')
        self.assertFalse(fence.status()['full_confirmed'])
        self.assertTrue(fence.status()['override_disabled'])

    def test_awaiting_first_provenance_keeps_unknown_duty_and_times_out(self):
        self.h.select()
        self.h.cpu = 85000
        for _ in range(16):
            self.h.advance(10)
            self.h.select()
            self.h.writes.reset_mock()
            self.h.watchdog.tick()
            self.h.writes.assert_not_called()
            self.assertEqual(fence.status()['phase'], 'awaiting')
            self.assertIsNone(fence.status()['last_duty'])
        self.h.advance(8)  # 67s transition + 100s first-output allowance expired.
        with redirect_stderr(io.StringIO()) as errors:
            self.h.watchdog.tick()
        self.assertIn('no valid first calculation', errors.getvalue())
        self.assertEqual(fence.status()['phase'], 'closed')

    def test_replay_after_local_rise_is_rejected_and_logs_exact_reason(self):
        self.h.select()
        command = self.h.api.command(duty=5, gpu_duty=6)
        self.h.send(command)
        self.h.cpu = 62000
        self.h.advance()
        self.h.watchdog.tick()
        with redirect_stderr(io.StringIO()) as errors:
            self.assertTrue(self.h.send(command).startswith(b'ERR'))
        self.assertEqual(errors.getvalue(), 'Adapter request rejected [set-duty-provenance]: replayed/out-of-order command\n')
        self.assertEqual(fence.status()['phase'], 'closed')
        self.assertFalse(fence.status()['full_confirmed'])
        self.assertTrue(fence.status()['override_disabled'])

    def test_local_rise_pm_races_at_all_three_observations_revoke_without_writes(self):
        for changed_at in range(3):
            with self.subTest(changed_at=changed_at):
                self.h.cpu = 61000
                self.h.advance()
                self.h.select()
                self.h.send(self.h.api.command(duty=5, gpu_duty=6))
                self.h.cpu = 62000
                self.h.advance()
                self.h.writes.reset_mock()
                states = [('active', 'D0')] * changed_at + [('suspended', 'D3cold')]
                with patch.object(adapter, 'read_live_power_state', side_effect=states):
                    self.h.watchdog.tick()
                self.assertEqual(fence.status()['phase'], 'closed')
                self.assertFalse(fence.status()['full_confirmed'])
                self.h.writes.assert_not_called()

    def test_local_rise_partial_write_or_record_failure_closes_without_retry(self):
        for fault in ('write', 'exit', 'record'):
            with self.subTest(fault=fault):
                self.h.cpu = 61000
                self.h.advance()
                self.h.select()
                self.h.send(self.h.api.command(duty=5, gpu_duty=6))
                self.h.cpu = 62000
                self.h.advance()
                self.h.writes.reset_mock()
                record = fence.record_applied_duty
                def fail_write(index, raw):
                    if index == adapter.GPU_TARGET and raw < adapter.MAX_RAW and fault != 'record':
                        raise SystemExit(1) if fault == 'exit' else OSError('offline write failure')
                def fail_record(duty, *args):
                    if fault == 'record' and duty < 100:
                        raise OSError('offline state failure')
                    return record(duty, *args)
                with patch.object(adapter, 'write_raw', side_effect=fail_write) as writes, \
                        patch.object(fence, 'record_applied_duty', side_effect=fail_record):
                    self.h.watchdog.tick()
                self.assertEqual(fence.status()['phase'], 'closed')
                self.assertFalse(fence.status()['full_confirmed'])
                self.assertEqual(writes.call_count, 3)
                for call in writes.call_args_list:
                    if call.args[0] != adapter.MODE:
                        self.assertLess(call.args[1], adapter.MAX_RAW)
                with self.assertRaisesRegex(ValueError, 'last applied duty uncertain'):
                    adapter.check_applied_duty()

    def test_failed_record_and_close_do_not_trigger_additional_fan_writes(self):
        for entrypoint in ('watchdog', 'producer', 'full'):
            with self.subTest(entrypoint=entrypoint):
                self.h.cpu = 61000
                self.h.advance()
                self.h.select()
                self.assertTrue(self.h.send(self.h.api.command(duty=5, gpu_duty=6)).startswith(b'OK'))
                self.h.cpu = 62000
                self.h.advance()
                self.h.writes.reset_mock()
                with patch.object(fence, '_atomic', side_effect=OSError('offline disk failure')), \
                        redirect_stderr(io.StringIO()):
                    if entrypoint == 'watchdog':
                        self.h.watchdog.tick()
                    elif entrypoint == 'producer':
                        self.assertTrue(self.h.send(self.h.api.command(duty=5, gpu_duty=6)).startswith(b'ERR'))
                    else:
                        self.assertTrue(server.dispatch('set-duty 100').startswith(b'ERR'))
                # Only the original requested write may have happened. There is
                # no emergency retry, whether storage failed before or after it.
                self.assertEqual(self.h.writes.call_count, 0 if entrypoint == 'producer' else 3)
                if self.h.writes.call_count:
                    expected = adapter.MAX_RAW if entrypoint == 'full' else adapter.MIN_COMMAND + round(72 * 69 / 100)
                    self.assertEqual(self.h.writes.call_args.args, (adapter.GPU_TARGET, expected))
                self.assertEqual(fence.status()['last_duty'], 71)
                with self.assertRaisesRegex(ValueError, 'last applied duty uncertain'):
                    adapter.check_applied_duty()
                self.h.advance()
                self.h.writes.reset_mock()
                self.h.watchdog.tick()
                self.h.writes.assert_not_called()
                self.assertEqual(fence.status()['phase'], 'closed')
                # Only a genuinely fresh validated command clears uncertainty.
                self.h.select()
                self.assertTrue(self.h.send().startswith(b'OK'))
                adapter.check_applied_duty()

    def test_high_temperatures_keep_curves_and_assignment_working(self):
        for sleep, sensor, temperature in ((True, 'cpu', 90000), (False, 'cpu', 90000),
                                           (False, 'gpu', 80000)):
            with self.subTest(sleep=sleep, sensor=sensor):
                h = Harness(sleep=sleep)
                try:
                    h.select()
                    h.send(h.api.command(duty=5, gpu_duty=6))
                    h.writes.reset_mock()
                    setattr(h, sensor, temperature)
                    h.advance()
                    with redirect_stderr(io.StringIO()) as errors:
                        h.watchdog.tick()
                    self.assertEqual(errors.getvalue(), '')
                    self.assertEqual(fence.status()['phase'], 'active')
                    self.assertEqual(fence.status()['last_duty'],
                                     control_policy.curve_demand(h.policy(), h.data))
                    self.assertTrue(h.send(h.api.command(100, 100)).startswith(b'OK'))
                    self.assertEqual(fence.status()['last_duty'], 100)
                finally:
                    h.close()

    def test_install_race_epoch_and_superseded_close_id(self):
        self.h.select()
        closed = self.h.rpc.close_and_full()
        self.h.rpc.close_and_full()
        with self.assertRaises(ValueError):
            self.h.rpc.install_assignment(self.h.api.envelope, self.h.policy(),
                                          thermal_state.epoch(self.h.data), closed['close_id'])
        self.h.api.on_put = lambda: (setattr(self.h, 'sleep', True), self.h.advance())
        with self.assertRaises(StagedError):
            self.h.select()
        self.assertEqual(fence.status()['phase'], 'closed')

    def test_unlabelled_lower_duty_and_malformed_json_cannot_write(self):
        self.h.select()
        count = self.h.writes.call_count
        for request in ('set-duty 0', 'set-duty-provenance 42 {}',
                        'set-duty-provenance 42 {"schema":1,"schema":1}',
                        'set-duty-provenance 42 NaN', 'enable\nset-duty 1'):
            self.assertTrue(server.dispatch(request).startswith(b'ERR'))
        self.assertEqual(self.h.writes.call_count, count)

    def test_original_json_values_identity_and_unavailable_gpu(self):
        cpu = json.loads(server.dispatch('temp-cpu/json')[3:])
        gpu = json.loads(server.dispatch('temp-gpu/json')[3:])
        self.assertEqual(cpu, dict(temperature=61.0, sample=self.h.data['cpu_sample']))
        self.assertEqual(gpu, dict(temperature=55.0, sample=self.h.data['gpu_sample']))
        self.assertEqual(server.dispatch('temp-cpu/json'), server.dispatch('temp-cpu-json'))
        self.h.sleep = True
        self.h.advance()
        self.assertTrue(server.dispatch('temp-gpu/json').startswith(b'ERR'))
        self.assertEqual(json.loads(server.dispatch('temp-cpu/json')[3:])['temperature'], 61.0)

    def test_sampler_generations_error_recovery_and_boottime_clock(self):
        samples = [deepcopy(self.h.data)]
        self.h.advance()
        samples.append(deepcopy(self.h.data))
        self.h.sleep = True
        self.h.advance()
        samples.append(deepcopy(self.h.data))
        samples.append(self.h.snapshot(error=True))
        self.h.advance()
        samples.append(deepcopy(self.h.data))
        self.h.sleep = False
        self.h.advance()
        samples.append(deepcopy(self.h.data))
        self.assertEqual([s['sampler_generation'] for s in samples], [1, 1, 2, 3, 4, 5])
        self.assertEqual([s['sample_id'] for s in samples], [1, 2, 3, 4, 5, 6])
        self.assertTrue(thermal_state.session_uuid(samples[0]['sampler_session']))
        self.h.boot += 100  # suspend ages BOOTTIME even if wall clock moves unexpectedly
        with self.assertRaises(ValueError):
            thermal_state.read_snapshot(require_metadata=True, require_samples=True)


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness(sleep=True)
        self.addCleanup(self.h.close)

    def test_lower_graph_rejection_logs_exact_reason_not_payload(self):
        for fault, reason in (('source', 'wrong graph source/function'),
                              ('history', 'incomplete or unknown sensor sample members')):
            self.h.select()
            command = self.h.api.command(duty=5)
            graph = command['calculation']['inputs']
            if fault == 'source':
                graph['source_temp_name'] = 'SECRET\nAuthorization: Bearer token\x1b[31m'
            else:
                graph['status_observations_newest_first'].append(dict(
                    status_unix_ms=self.h.wall, temperature=61.0, sensor_sample=None))
            with redirect_stderr(io.StringIO()) as errors:
                reply = self.h.send(command)
            self.assertEqual(reply, b'ERR adapter request rejected\n')
            self.assertEqual(errors.getvalue(), 'Adapter request rejected [set-duty-provenance]: ' + reason + '\n')
            self.assertEqual(fence.status()['phase'], 'closed')
            self.assertFalse(fence.status()['full_confirmed'])
            self.assertTrue(fence.status()['override_disabled'])

    def test_watchdog_logs_bounded_reason_never_arbitrary_exception_text(self):
        secret = 'SECRET\nAuthorization: Bearer token\x1b[31m' * 10000
        for error, reason in ((ValueError('temperature snapshot stale or invalid'),
                               'temperature snapshot stale or invalid'),
                              (ValueError(secret), 'unspecified validation failure'),
                              (OSError(secret), 'OS operation failed'),
                              (RuntimeError(secret), 'runtime operation failed'),
                              (RuntimeError('GPU power state unconfirmed: ' + secret),
                               'GPU power state unconfirmed'),
                              (RuntimeError('GPU power state changed during observation'),
                               'GPU power state changed during observation'),
                              (KeyError(secret), 'missing required member')):
            with self.subTest(reason=reason), redirect_stderr(io.StringIO()) as errors, \
                    patch.object(server, 'read_snapshot', side_effect=error):
                server.ThermalWatchdog().tick()
                self.assertEqual(errors.getvalue(), 'Fan watchdog: ' + reason + '; invalidating control without fan override\n')
                self.assertLess(len(errors.getvalue()), 240)
                self.assertEqual(fence.status()['phase'], 'closed')

    def test_watchdog_deduplicates_same_reason_but_reports_changed_failure(self):
        reasons = ('temperature snapshot stale or invalid', 'temperature snapshot stale or invalid',
                   'selector lease expired')
        with redirect_stderr(io.StringIO()) as errors, \
                patch.object(server, 'read_snapshot', side_effect=[ValueError(r) for r in reasons]):
            for _ in reasons:
                self.h.advance()
                self.h.watchdog.tick()
        self.assertEqual(errors.getvalue().splitlines(), [
            'Fan watchdog: temperature snapshot stale or invalid; invalidating control without fan override',
            'Fan watchdog: selector lease expired; invalidating control without fan override'])

    def test_dispatch_does_not_log_untrusted_decoder_or_captured_stderr(self):
        for request, action, reason in (('set-duty-provenance 5 {"SECRET":"token",', 'set-duty-provenance', 'invalid JSON'),
                                        ('SECRET\nAuthorization: Bearer token', 'invalid', 'invalid framing')):
            with redirect_stderr(io.StringIO()) as errors:
                self.assertTrue(server.dispatch(request).startswith(b'ERR'))
            self.assertEqual(errors.getvalue(), 'Adapter request rejected [' + action + ']: ' + reason + '\n')
        def unsafe_upstream(*args):
            print('SECRET Authorization: Bearer token', file=server.sys.stderr)
            raise RuntimeError('SECRET')
        with redirect_stderr(io.StringIO()) as errors, patch.object(server, '_rpc', side_effect=unsafe_upstream):
            self.assertTrue(server.dispatch('self-check').startswith(b'ERR'))
        self.assertEqual(errors.getvalue(), 'Adapter request rejected [self-check]: runtime operation failed\n')


class ActualEngineEnvelopeTests(unittest.TestCase):
    def test_exported_engine_envelope_requires_real_history_and_accepts_completed_window(self):
        h = Harness(sleep=True)
        self.addCleanup(h.close)
        exported = json.loads((ROOT / 'tests/fixtures/producer-engine.json').read_text())
        raw = deepcopy(exported['command'])
        graph = raw['calculation']['inputs']
        live = deepcopy(h.policy())
        profile_id = exported['assignment']['assignment']['profile_uid']
        function_id = graph['function_uid']
        live['bindings']['profiles']['cpu'] = profile_id
        live['profiles']['cpu']['uid'] = profile_id
        live['profiles']['mix']['member_profile_uids'] = [profile_id, 'gpu']
        live['bindings']['sources']['cpu'] = dict(device_uid=graph['source_device_uid'],
                                                  temp_name=graph['source_temp_name'])
        live['profiles']['cpu']['temp_source'] = deepcopy(live['bindings']['sources']['cpu'])
        live['profiles']['cpu']['function_uid'] = function_id
        fun = deepcopy(live['functions']['standard'])
        fun.update(uid=function_id, response_delay=30)
        live['functions'][function_id] = fun
        live['bindings']['target'] = dict(device_uid=raw['device_uid'], channel_name=raw['channel_name'])
        h.wall = raw['calculation']['calculated_at_unix_ms']
        h.state.sample_id = 100
        h.cpu = 63000
        h.snapshot()
        fence.close(); closed = fence.mark_full()
        fence.install_assignment(exported['assignment'], live, thermal_state.epoch(h.data),
                                 closed['close_id'], h.data)
        # The unmodified Rust export has extension/test identities and synthetic
        # startup rows with null provenance. It is deliberately NOT authorization.
        with self.assertRaises(ValueError):
            fence.validate_command(raw, current_snapshot=h.data)
        # Same actual envelope, assignment, calculation/source IDs and JSON
        # structure; simulate replacement of the startup window by sampler data.
        for i, observation in enumerate(graph['status_observations_newest_first']):
            sample = deepcopy(h.data['cpu_sample'])
            sample['sample_id'] -= i
            sample['sampled_at_unix_ms'] -= i * 1000
            sample['sampled_at_monotonic_ns'] -= i * 1_000_000_000
            observation.update(status_unix_ms=h.wall - i * 1000, temperature=63.0, sensor_sample=sample)
        graph['sensor_sample'] = deepcopy(graph['status_observations_newest_first'][0]['sensor_sample'])
        self.assertEqual(fence.validate_command(raw, current_snapshot=h.data), raw['calculation']['duty'])
        self.assertEqual(raw['assignment'], exported['command']['assignment'])
        self.assertEqual(raw['calculation']['calculation_id'], exported['command']['calculation']['calculation_id'])


class CompiledSocketTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.addCleanup(self.h.close)
        self.path = self.h.root / 'adapter.sock'
        self.binary = self.h.root / 'client'
        subprocess.run(['cc', '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror',
                        '-DSOCKET_PATH="' + str(self.path) + '"', str(ROOT / 'src/adapter_client.c'),
                        '-o', str(self.binary)], check=True, capture_output=True)
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(self.path)); self.listener.listen(16)
        self.h.rpc.close_and_full()  # This fixture starts with an explicit full fence.
        self.watchdog = server.ThermalWatchdog()
        self.watchdog.tick()
        # Peer UID injection is test-only. Production SocketLoop always requires root.
        self.loop = server.SocketLoop(self.listener, self.watchdog, peer_uid=os.geteuid())
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run_loop)
        self.thread.start()
        self.addCleanup(self.stop_loop)

    def run_loop(self):
        while not self.stop.is_set():
            self.loop.step(0.005)

    def stop_loop(self):
        self.stop.set(); self.thread.join(timeout=3)
        self.loop.close(); self.listener.close()

    def client(self, *args):
        return subprocess.run([str(self.binary), *args], capture_output=True, timeout=5)

    def test_actual_c_json_reads_and_provenance_write_up_to_64k(self):
        self.h.select()
        result = self.client('temp-cpu/json')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['sample'], self.h.data['cpu_sample'])
        command = self.h.api.command()
        label = json.dumps(command)
        label += ' ' * (65536 - len(label.encode()))
        result = self.client('set-duty-provenance', '80', label)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(fence.status()['phase'], 'active')
        self.assertNotEqual(self.client('set-duty-provenance', '80', label + ' ').returncode, 0)
        self.assertNotEqual(self.client('set-duty-provenance', '80', label).returncode, 0)  # replay

    def test_c_assignment_and_close_rpc_roundtrip(self):
        closed = json.loads(self.client('fence-close-full').stdout)
        response = self.h.api.request('PUT', '/devices/ec/settings/both_fans/profile', {'profile_uid': 'mix'})
        payload = dict(assignment=response, policy=self.h.policy(), epoch=thermal_state.epoch(self.h.data),
                       close_id=closed['close_id'])
        result = self.client('install-assignment', json.dumps(payload))
        self.assertEqual(result.returncode, 0, result.stderr)
        state = json.loads(self.client('assignment-status').stdout)
        self.assertEqual(state['assignment'], response)
        self.assertEqual(self.client('set-duty', '100').returncode, 0)
        self.assertNotEqual(self.client('set-duty', '20').returncode, 0)

    def test_existing_c_reset_protocol_supports_bounded_reboot_hold(self):
        self.h.select()
        self.assertTrue(self.h.send().startswith(b'OK'))
        count = self.h.writes.call_count
        self.h.stopping.return_value = True
        result = self.client('reset')
        self.assertEqual(result.returncode, 0, result.stderr)
        state = json.loads(result.stdout)
        self.assertEqual(state['hold_kind'], 'shutdown')
        self.assertFalse(state['full_confirmed'])
        self.assertEqual(state['hold_until'], self.h.boot + 10)
        self.assertEqual(self.h.writes.call_count, count)
        result = self.client('fence-close-full')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(json.loads(result.stdout)['full_confirmed'])
        self.assertTrue(json.loads(result.stdout)['override_disabled'])
        self.assertEqual(self.h.writes.call_count, count)

    def test_partial_slow_client_does_not_block_watchdog(self):
        self.h.select()
        self.h.send()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as slow:
            slow.connect(str(self.path))
            slow.sendall(b'set-duty-provenance ')
            self.h.advance(6, sample=False)
            deadline = time.monotonic() + 1.5
            while time.monotonic() < deadline and fence.status()['phase'] != 'closed':
                time.sleep(.01)
            self.assertEqual(fence.status()['phase'], 'closed')

    def test_multiple_lines_and_untrusted_peer_rejected(self):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.connect(str(self.path)); conn.sendall(b'get-duty\nreset\n')
            self.assertTrue(conn.recv(1024).startswith(b'ERR'))
        self.loop.peer_uid = os.geteuid() + 1
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(1); conn.connect(str(self.path))
            self.assertEqual(conn.recv(1024), b'')


if __name__ == '__main__':
    unittest.main()

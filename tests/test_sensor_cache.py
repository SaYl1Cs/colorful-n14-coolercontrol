"""Offline EC/schema/watchdog regressions: no access to host EC or GPU."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import sensor_cache as sensors
import thermal_state as thermal
import coolercontrol_adapter as adapter
import adapter_server as server


def snapshot(sleep=False):
    return dict(schema_version=2, source='ec0/io', sampled_at=100,
                cpu_temp=61000, gpu_state='suspended' if sleep else 'active',
                gpu_power_state='D3cold' if sleep else 'D0',
                gpu_temp=None if sleep else 55000,
                gpu_sampled_at=None if sleep else 100, errors=[])


def read(data, now=101):
    return thermal.read_snapshot(Mock(read_text=Mock(return_value=json.dumps(data))), now)


class EcTests(unittest.TestCase):
    def test_only_whitelisted_offsets_single_byte_readonly(self):
        ec = sensors.EcSensor('/fake/ec')
        with patch.object(sensors.os, 'open', return_value=7) as op, \
                patch.object(sensors.os, 'pread', return_value=b'7') as pread, \
                patch.object(sensors.os, 'close') as close:
            self.assertEqual(ec.read(0x56), 55000)
            self.assertEqual(ec.read(0x58), 55000)
            self.assertEqual([c.args for c in pread.call_args_list], [(7, 1, 0x56), (7, 1, 0x58)])
            flags = op.call_args.args[1]
            self.assertEqual(flags & sensors.os.O_ACCMODE, sensors.os.O_RDONLY)
            for offset in (0, 0x57, 0x59, True, 86.0):
                with self.assertRaises(ValueError):
                    ec.read(offset)
            self.assertEqual(op.call_count, 2)
            self.assertEqual(close.call_count, 2)

    def test_invalid_ec_values_and_short_reads(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'ec'
            ec = sensors.EcSensor(path)
            for value in (b'', b'\0', b'\x78', b'\xff'):
                path.write_bytes(b'\0' * 0x56 + value)
                with self.assertRaises(ValueError):
                    ec.read(0x56)
            path.write_bytes(b'\0' * 0x56 + b'F')
            self.assertEqual(ec.read(0x56), 70000)

    def test_missing_debugfs_recovers_without_cached_temperature(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'ec'
            ec = sensors.EcSensor(path)
            cpu = sensors.CpuEcSensor(ec)
            gpu = Mock(read=Mock(return_value={k: v for k, v in snapshot(True).items()
                                            if k.startswith('gpu_')}))
            self.assertIsNone(sensors.sample(cpu, gpu)['cpu_temp'])
            path.write_bytes(b'\0' * 0x56 + b'F')
            self.assertEqual(sensors.sample(cpu, gpu)['cpu_temp'], 70000)

    def test_platform_guard_precedes_ec_access(self):
        with patch('prepare.check_platform', side_effect=RuntimeError('wrong DSDT')), \
                patch.object(sensors.os, 'geteuid', return_value=0), \
                patch.object(sensors, 'EcSensor') as ec:
            with self.assertRaises(RuntimeError):
                sensors.main()
            ec.assert_not_called()

    def test_no_legacy_temperature_backends(self):
        source = (ROOT / 'sensor_cache.py').read_text()
        for forbidden in ('ctypes', 'Nvml', 'coretemp', 'nvidia-smi', 'N14_GPU_POLL'):
            self.assertNotIn(forbidden, source)


class PowerTests(unittest.TestCase):
    def test_requires_both_states_and_consistent_observation(self):
        with tempfile.TemporaryDirectory() as tmp:
            device = Path(tmp)
            (device / 'power').mkdir()
            for runtime, power in [('active', 'D0'), ('suspended', 'D3cold'),
                                   ('suspended', 'D0'), ('active', 'D3cold'),
                                   ('suspending', 'D3cold'), ('unknown', 'unknown')]:
                (device / 'power/runtime_status').write_text(runtime)
                (device / 'power_state').write_text(power)
                if (runtime, power) in [('active', 'D0'), ('suspended', 'D3cold')]:
                    self.assertEqual(sensors.power_state(device), (runtime, power))
                else:
                    with self.assertRaises(RuntimeError):
                        sensors.power_state(device)
            (device / 'power_state').unlink()
            with self.assertRaises(OSError):
                sensors.power_state(device)
        with patch.object(Path, 'read_text', side_effect=['active', 'D0', 'suspended', 'D3cold']):
            with self.assertRaises(RuntimeError):
                sensors.power_state(Path('/fake'))

    def test_sleep_has_no_gpu_ec_read_and_no_substitution(self):
        ec = Mock()
        gpu = sensors.GpuEcSensor(ec, Path('/fake'))
        with patch.object(sensors, 'power_state', return_value=('suspended', 'D3cold')):
            result = gpu.read(100)
        ec.read.assert_not_called()
        self.assertIsNone(result['gpu_temp'])
        self.assertIsNone(result['gpu_sampled_at'])
        self.assertNotIn('gpu_input', result)

    def test_active_samples_only_gput_and_wake_has_no_old_cache(self):
        ec = Mock(read=Mock(side_effect=[55000, 56000]))
        gpu = sensors.GpuEcSensor(ec, Path('/fake'))
        for state, expected in [(('active', 'D0'), 55000),
                                (('suspended', 'D3cold'), None),
                                (('active', 'D0'), 56000)]:
            with patch.object(sensors, 'power_state', return_value=state):
                self.assertEqual(gpu.read(100)['gpu_temp'], expected)
        self.assertEqual([c.args for c in ec.read.call_args_list], [(0x58,), (0x58,)])

    def test_race_is_error_not_sleep(self):
        for states in [[('active', 'D0'), ('suspended', 'D3cold')],
                       [('suspended', 'D3cold'), ('active', 'D0')]]:
            with patch.object(sensors, 'power_state', side_effect=states):
                gpu = sensors.GpuEcSensor(Mock(read=Mock(return_value=55000)), Path('/fake'))
                data = sensors.sample(Mock(read=Mock(return_value=61000)), gpu)
            self.assertEqual(data['gpu_state'], 'error')
            self.assertIsNone(data['gpu_temp'])
            self.assertNotIn('gpu_input', data)

    def test_invalid_active_value_never_becomes_sleep_or_cpu(self):
        gpu = sensors.GpuEcSensor(Mock(read=Mock(side_effect=ValueError('zero'))), Path('/fake'))
        with patch.object(sensors, 'power_state', return_value=('active', 'D0')):
            data = sensors.sample(Mock(read=Mock(return_value=61000)), gpu)
        self.assertEqual(data['gpu_state'], 'error')
        self.assertIsNone(data['gpu_temp'])
        self.assertTrue(data['errors'])

    def test_timestamp_precedes_io(self):
        with patch.object(sensors, 'boottime', return_value=100) as clock:
            cpu = Mock()
            cpu.read.side_effect = lambda: (clock.assert_called_once(), 61000)[1]
            data = sensors.sample(cpu, Mock(read=Mock(return_value={})))
        self.assertEqual(data['sampled_at'], 100)

    def test_discovery_rejects_ambiguous_devices(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, cls in [('gpu', '0x030000'), ('audio', '0x040300')]:
                path = root / name
                path.mkdir()
                (path / 'vendor').write_text('0x10de')
                (path / 'class').write_text(cls)
            self.assertEqual(sensors.discover_gpu(root), root / 'gpu')
            (root / 'audio/class').write_text('0x030200')
            with self.assertRaises(RuntimeError):
                sensors.discover_gpu(root)


class SnapshotTests(unittest.TestCase):
    def test_valid_states_request_distinct_curves(self):
        self.assertEqual(thermal.requested_curve(read(snapshot())), 'max')
        self.assertEqual(thermal.requested_curve(read(snapshot(True))), 'cpu')

    def test_error_unknown_or_contradictory_states_fail_closed(self):
        for changes in [dict(gpu_state='error'), dict(gpu_state='resuming'),
                        dict(gpu_power_state='D3cold'), dict(errors=['EC transport error']),
                        dict(gpu_input=61000), dict(schema_version=1), dict(source='nvml')]:
            data = snapshot()
            data.update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                read(data)
        for changes in [dict(gpu_power_state='D0'), dict(gpu_temp=0),
                        dict(gpu_temp=61000), dict(gpu_sampled_at=100)]:
            data = snapshot(True)
            data.update(changes)
            with self.assertRaises(ValueError):
                read(data)

    def test_stale_future_suspend_gap_and_invalid_values(self):
        for now in (99, 106, 10000, float('nan')):
            with self.assertRaises(ValueError):
                read(snapshot(), now)
        for field in ('cpu_temp', 'gpu_temp'):
            for value in (None, 0, -1, 120000, True, '55000', 55000.5):
                data = snapshot()
                data[field] = value
                with self.assertRaises(ValueError):
                    read(data)
        data = snapshot()
        data['gpu_sampled_at'] = 90
        with self.assertRaises(ValueError):
            read(data)
        for data in (None, [], 1, 'bad'):
            with self.assertRaises(ValueError):
                read(data)

    def test_cpu_remains_visible_on_gpu_failure(self):
        data = snapshot()
        data.update(gpu_state='error', gpu_temp=None, gpu_sampled_at=None)
        path = Mock(read_text=Mock(return_value=json.dumps(data)))
        self.assertEqual(thermal.read_cpu_snapshot(path, 101)['cpu_temp'], 61000)
        with self.assertRaises(ValueError):
            read(data)

    def test_atomic_write(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(sensors, 'RUNTIME_DIR', Path(tmp)):
            sensors.write_snapshot(snapshot())
            self.assertEqual(json.loads((Path(tmp) / 'temperatures.json').read_text()), snapshot())
            self.assertEqual(len(list(Path(tmp).iterdir())), 1)

    def test_suspend_aware_clock(self):
        with patch.object(thermal.time, 'clock_gettime', return_value=100) as clock:
            self.assertEqual(thermal.boottime(), 100)
            clock.assert_called_once_with(thermal.time.CLOCK_BOOTTIME)


class ConsumerTests(unittest.TestCase):
    def test_cpu_85_sleep_does_not_trigger_gpu_80_threshold(self):
        data = snapshot(True)
        data['cpu_temp'] = 85000
        self.assertFalse(thermal.critical_temperature(read(data)))
        data['cpu_temp'] = 90000
        self.assertTrue(thermal.critical_temperature(read(data)))
        data = snapshot()
        data['gpu_temp'] = 80000
        self.assertTrue(thermal.critical_temperature(read(data)))

    def test_adapter_never_emits_cpu_zero_or_none_as_sleep_temperature(self):
        out = io.StringIO()
        with patch.object(adapter, 'read_snapshot', return_value=snapshot(True)), \
                redirect_stdout(out), self.assertRaises(adapter.GpuAsleep):
            adapter.read_gpu_temp()
        self.assertEqual(out.getvalue(), '')

    def test_watchdog_only_closes_authority_on_invalid_data(self):
        for error in (ValueError('active GPU invalid'), OSError('transport unavailable')):
            with patch.object(server, 'read_snapshot', side_effect=error), \
                    patch.object(server, 'dispatch', return_value=b'OK ok\n') as dispatch:
                server.ThermalWatchdog().tick()
                dispatch.assert_called_once_with('fence-close')

    def test_sensor_error_rejects_command_without_any_fan_write(self):
        from protocol_fixtures import Harness
        harness = Harness()
        self.addCleanup(harness.close)
        with patch.object(adapter, 'read_snapshot', side_effect=ValueError('failed')):
            with self.assertRaises(ValueError):
                adapter.set_both(adapter.MIN_COMMAND)
        harness.writes.assert_not_called()


if __name__ == '__main__':
    unittest.main()

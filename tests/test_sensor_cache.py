"""Offline tests: all GPU/NVML access is mocked; no host GPU is queried."""
from pathlib import Path
import json
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import sensor_cache as sensors
import thermal_state as thermal


class GpuTests(unittest.TestCase):
    def setUp(self):
        self.factory = Mock()
        self.nvml = self.factory.return_value
        self.nvml.gpu_temperature_millic.return_value = 55000
        self.gpu = sensors.GpuSensor(Path('/fake/0000:01:00.0'), self.factory)

    def read(self, state, now=100):
        with patch.object(sensors, 'runtime_status', return_value=state), \
                patch.object(sensors, 'boottime', return_value=now):
            return self.gpu.read(61000, now)

    def test_sleep_does_not_even_load_nvml(self):
        for i in range(1000):
            result = self.read('suspended', now=i)
        self.factory.assert_not_called()
        self.assertIsNone(result['gpu_temp'])
        self.assertEqual(result['gpu_input'], 61000)

    def test_unknown_transition_missing_pm_never_queries(self):
        for state in ('suspending', 'resuming', 'unsupported', '', 'garbage'):
            with self.assertRaises(RuntimeError):
                self.read(state)
        with patch.object(sensors, 'runtime_status', side_effect=OSError):
            with self.assertRaises(OSError):
                self.gpu.read(61000, 100)
        self.factory.assert_not_called()

    def test_second_gate_precedes_library_loading(self):
        with patch.object(sensors, 'runtime_status', side_effect=['active', 'suspended']):
            with self.assertRaises(RuntimeError):
                self.gpu.read(61000, 100)
        self.factory.assert_not_called()

    def test_active_sample_always_closes_session(self):
        result = self.read('active')
        self.assertEqual(result['gpu_temp'], 55000)
        self.assertEqual([c[0] for c in self.nvml.mock_calls],
                         ['start', 'gpu_temperature_millic', 'close'])

    def test_active_sampling_is_rate_limited(self):
        self.read('active', 100)
        self.read('active', 101)
        self.read('active', 104.9)
        self.nvml.start.assert_called_once()
        self.read('active', 105)
        self.assertEqual(self.nvml.start.call_count, 2)

    def test_sleep_releases_no_session_and_wake_samples_immediately(self):
        self.read('active', 100)
        self.read('suspended', 101)
        self.nvml.close.assert_called_once()
        self.read('active', 102)
        self.assertEqual(self.nvml.start.call_count, 2)

    def test_failed_read_or_init_closes_and_invalidates(self):
        for method in ('start', 'gpu_temperature_millic'):
            with self.subTest(method=method):
                self.setUp()
                getattr(self.nvml, method).side_effect = RuntimeError('lost GPU')
                with self.assertRaises(RuntimeError):
                    self.read('active')
                self.nvml.close.assert_called_once()
                self.assertIsNone(self.gpu.last_at)

    def test_sleep_cooldown_does_not_forge_actual_temperature(self):
        self.nvml.gpu_temperature_millic.return_value = 75000
        self.read('active', 100)
        result = self.read('suspended', 102)
        self.assertEqual(result['gpu_input'], 75000)
        self.assertIsNone(result['gpu_temp'])
        self.assertEqual(self.read('suspended', 116)['gpu_input'], 61000)

    def test_strict_no_query_never_loads_driver(self):
        self.gpu.poll = False
        with self.assertRaises(RuntimeError):
            self.read('active')
        self.read('suspended')
        self.factory.assert_not_called()

    def test_read_errors_publish_invalid_not_last_good(self):
        cpu = Mock(read=Mock(return_value=61000))
        gpu = Mock(read=Mock(side_effect=RuntimeError('failed')))
        data = sensors.sample(cpu, gpu)
        self.assertIsNone(data['gpu_temp'])
        self.assertEqual(data['gpu_input'], 61000)
        self.assertEqual(data['gpu_state'], 'error')

    def test_discovery_ignores_audio_and_rejects_multiple_gpus(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, cls in [('gpu', '0x030000'), ('audio', '0x040300')]:
                path = root / name; path.mkdir()
                (path / 'vendor').write_text('0x10de')
                (path / 'class').write_text(cls)
            self.assertEqual(sensors.discover_gpu(root), root / 'gpu')
            (root / 'audio/class').write_text('0x030200')
            with self.assertRaises(RuntimeError):
                sensors.discover_gpu(root)

    def test_nvml_is_lazy_and_has_no_cli_runner(self):
        source = (ROOT / 'sensor_cache.py').read_text()
        self.assertNotIn('subprocess', source)
        self.assertNotIn('nvmlDeviceGetCount', source)
        self.assertNotIn('nvmlDeviceGetHandleByIndex', source)


class CpuTests(unittest.TestCase):
    def test_cache_paths_and_rediscover_on_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); hw = root / 'hwmon0'; hw.mkdir()
            (hw / 'name').write_text('coretemp')
            (hw / 'temp1_input').write_text('55000')
            (hw / 'temp2_input').write_text('60000')
            cpu = sensors.CpuSensor(root)
            self.assertEqual(cpu.read(), 60000)
            paths = cpu.paths
            self.assertEqual(cpu.read(), 60000)
            self.assertIs(cpu.paths, paths)
            (hw / 'temp2_input').unlink()
            with self.assertRaises(OSError):
                cpu.read()
            self.assertEqual(cpu.paths, [])
            self.assertEqual(cpu.read(), 55000)

    def test_invalid_cpu_not_silently_discarded(self):
        cpu = sensors.CpuSensor()
        cpu.paths = [Mock(read_text=Mock(return_value='-273150'))]
        with self.assertRaises(ValueError):
            cpu.read()


class SnapshotTests(unittest.TestCase):
    def data(self):
        return {'sampled_at': 100, 'cpu_temp': 61000, 'gpu_state': 'active',
                'gpu_temp': 55000, 'gpu_input': 55000, 'gpu_sampled_at': 97}

    def read(self, data, now=101):
        path = Mock(read_text=Mock(return_value=json.dumps(data)))
        return thermal.read_snapshot(path, now)

    def test_valid_active(self):
        self.assertEqual(self.read(self.data())['gpu_temp'], 55000)

    def test_cpu_snapshot_survives_gpu_error(self):
        data = self.data()
        data.update(gpu_state='error', gpu_temp=None, gpu_input=61000, gpu_sampled_at=None)
        path = Mock(read_text=Mock(return_value=json.dumps(data)))
        self.assertEqual(thermal.read_cpu_snapshot(path, 101)['cpu_temp'], 61000)

    def test_stale_future_and_suspend_gap_rejected(self):
        for now in (99, 106, 10000, float('nan')):
            with self.assertRaises(ValueError):
                self.read(self.data(), now)

    def test_gpu_age_independent_of_snapshot(self):
        data = self.data(); data['gpu_sampled_at'] = 90
        with self.assertRaises(ValueError):
            self.read(data)

    def test_invalid_temperatures(self):
        for key in ('cpu_temp', 'gpu_temp', 'gpu_input'):
            for value in (None, 0, -1, 120000, True, '55000', 55000.5):
                data = self.data(); data[key] = value
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    self.read(data)

    def test_sleep_fallback_is_explicit(self):
        data = self.data()
        data.update(gpu_state='suspended', gpu_temp=None, gpu_input=61000, gpu_sampled_at=None)
        self.assertIsNone(self.read(data)['gpu_temp'])
        data['gpu_temp'] = 55000
        with self.assertRaises(ValueError):
            self.read(data)

    def test_error_state_requires_cpu_fallback(self):
        data = self.data()
        data.update(gpu_state='error', gpu_temp=None, gpu_input=61000, gpu_sampled_at=None)
        self.assertEqual(self.read(data)['gpu_input'], 61000)
        data['gpu_input'] = None
        with self.assertRaises(ValueError):
            self.read(data)

    def test_unknown_state_not_converted_to_zero(self):
        data = self.data(); data['gpu_state'] = 'transitioning'
        with self.assertRaises(ValueError):
            self.read(data)

    def test_atomic_write_has_no_fsync_and_no_temporary_left(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(sensors, 'RUNTIME_DIR', Path(tmp)), \
                patch.object(sensors.os, 'fsync') as fsync:
            sensors.write_snapshot(self.data())
            self.assertEqual(json.loads((Path(tmp) / 'temperatures.json').read_text()), self.data())
            self.assertEqual(len(list(Path(tmp).iterdir())), 1)
            fsync.assert_not_called()

    def test_default_daemon_mode_never_queries_gpu(self):
        with patch.dict(sensors.os.environ, {}, clear=True):
            self.assertEqual(sensors.os.environ.get('N14_GPU_POLL'), None)
            self.assertFalse(sensors.os.environ.get('N14_GPU_POLL', 'never') != 'never')

    def test_clock_is_suspend_aware(self):
        with patch.object(thermal.time, 'clock_gettime', return_value=100) as clock:
            self.assertEqual(thermal.boottime(), 100)
            clock.assert_called_once_with(thermal.time.CLOCK_BOOTTIME)


if __name__ == '__main__':
    unittest.main()

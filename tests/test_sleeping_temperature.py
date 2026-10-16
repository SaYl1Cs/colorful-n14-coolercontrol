"""Sleeping GPU is absent, not a failed fan write. All hardware is mocked."""
from contextlib import redirect_stderr
from copy import deepcopy
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import adapter_server as server
import coolercontrol_adapter as adapter
import provenance_fence as fence
import sensor_cache
from protocol_fixtures import Harness


class SleepingTemperatureTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness(sleep=True)
        self.addCleanup(self.h.close)

    def test_sleep_read_endpoints_are_quiet_nonnumeric_and_do_not_change_control(self):
        self.h.select()
        self.h.send(self.h.api.command(duty=30))
        before = fence._read()
        count = self.h.writes.call_count
        for action in ('temp-gpu', 'temp-gpu-json', 'temp-gpu/json'):
            with self.subTest(action=action), redirect_stderr(io.StringIO()) as log:
                result = server.dispatch(action)
            self.assertEqual(result, b'ERR GPU_ASLEEP: D3cold; temperature unavailable\n')
            self.assertEqual(log.getvalue(), '')
            self.assertEqual(self.h.writes.call_count, count)
            self.assertEqual(fence._read(), before)

    def test_wake_returns_real_gpu_measurement(self):
        self.h.sleep = False
        self.h.advance()
        result = server.dispatch('temp-gpu/json')
        self.assertTrue(result.startswith(b'OK '))
        payload = json.loads(result[3:])
        self.assertEqual(payload['temperature'], 55.0)
        self.assertEqual(payload['sample']['power_state'], 'active')
        self.assertNotEqual(payload['temperature'], self.h.cpu / 1000)

    def test_stale_sleep_data_is_not_silenced(self):
        self.h.advance(10, sample=False)
        with redirect_stderr(io.StringIO()) as log:
            result = server.dispatch('temp-gpu/json')
        self.assertTrue(result.startswith(b'ERR adapter request rejected'))
        self.assertIn('[temp-gpu/json]', log.getvalue())
        self.assertNotIn(b'GPU_ASLEEP', result)

    def test_malformed_sleep_metadata_is_not_silenced(self):
        data = deepcopy(self.h.data)
        data['sampler_session'] = 'invalid'
        sensor_cache.write_snapshot(data)
        with redirect_stderr(io.StringIO()) as log:
            result = server.dispatch('temp-gpu/json')
        self.assertTrue(result.startswith(b'ERR adapter request rejected'))
        self.assertIn('[temp-gpu/json]', log.getvalue())

    def test_active_zero_is_not_classified_as_sleep(self):
        self.h.sleep = False
        self.h.gpu = 0
        self.h.advance()
        with redirect_stderr(io.StringIO()) as log:
            result = server.dispatch('temp-gpu/json')
        self.assertTrue(result.startswith(b'ERR adapter request rejected'))
        self.assertNotIn(b'GPU_ASLEEP', result)
        self.assertTrue(log.getvalue())

    def test_inconsistent_pm_and_error_snapshot_are_not_classified_as_sleep(self):
        for changes in ({'gpu_power_state': 'D3hot'}, {'errors': ['transport failed']}):
            self.h.advance()
            data = deepcopy(self.h.data)
            data.update(changes)
            sensor_cache.write_snapshot(data)
            with redirect_stderr(io.StringIO()) as log:
                result = server.dispatch('temp-gpu/json')
            self.assertTrue(result.startswith(b'ERR adapter request rejected'))
            self.assertTrue(log.getvalue())

    def test_sleep_status_cannot_bypass_control_error_handling(self):
        with patch.object(server, '_rpc', side_effect=adapter.GpuAsleep()), \
                patch.object(server, '_full_hand_over') as full, \
                redirect_stderr(io.StringIO()) as log:
            result = server.dispatch('set-duty-provenance 0 {}')
        full.assert_called_once()
        self.assertEqual(result, b'ERR adapter request rejected\n')
        self.assertIn('unexpected GPU-asleep status', log.getvalue())


if __name__ == '__main__':
    unittest.main()

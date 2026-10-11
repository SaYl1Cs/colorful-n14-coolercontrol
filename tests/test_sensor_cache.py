"""Offline tests for the persistent sensor cache."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import sensor_cache


class SensorCacheTests(unittest.TestCase):
    def test_atomic_cache_write_and_remove(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            directory = Path(temporary)
            with patch.object(sensor_cache, "RUNTIME_DIR", directory):
                sensor_cache.write_cache("gpu", 55000)
                self.assertEqual((directory / "gpu_temp").read_text(), "55000\n")
                self.assertFalse(list(directory.glob(".gpu_temp.*")))
                sensor_cache.remove_cache("gpu")
                self.assertFalse((directory / "gpu_temp").exists())

    def test_cache_values_are_millidegrees(self):
        self.assertEqual(sensor_cache.MAX_TEMP_MILLIC, 120000)
        self.assertEqual(sensor_cache.NVML_TEMPERATURE_GPU, 0)

    def test_sensor_daemon_has_no_cli_process_runner(self):
        source = (ROOT / "sensor_cache.py").read_text(encoding="utf-8")
        self.assertNotIn("subprocess", source)
        self.assertNotIn("nvidia-smi", source)
        self.assertIn("CDLL", source)


if __name__ == "__main__":
    unittest.main()

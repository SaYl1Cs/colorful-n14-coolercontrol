"""Offline Lua/packet tests. No ACPI, EC, service, or system files are accessed.

Requires lupa (tested with 2.8). Runs the actual generated profile handlers
against a simulated filesystem and ACPI firmware, using Lua 5.4.
"""
import importlib.util
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
# Optional local test-only installation used on the development workstation.
sys.path.insert(0, str(ROOT.parent / "igame-analysis" / "test-tools"))
from lupa.lua54 import LuaRuntime
from build_profile import build, PROFILE_ID
import prepare


class Firmware:
    def __init__(self, access="control", brand=(0x20, 0x10), ticket_brand=None, bootstrap=True):
        self.lua = LuaRuntime(unpack_returned_tuples=True)
        self.brand = brand
        ticket_brand = ticket_brand or brand
        self.files = {
            "/sys/class/dmi/id/sys_vendor": "COLORFUL\n",
            "/sys/class/dmi/id/product_name": "N14 24\n",
            "/sys/class/dmi/id/bios_version": "2.07\n",
            "/run/colorful-n14-nbfc/permission":
                f"{prepare.DSDT_SHA256} {ticket_brand[0]:02x} {ticket_brand[1]:02x} {access}\n",
        }
        self.calls = []
        self.targets = {0x54: 0, 0x55: 0}
        self.tachometers = {0x5C: 30, 0x5D: 25}
        self.value = 35
        self.fail_index = None
        self.write_response = 0
        self.lua.globals()["mock_file"] = lambda path: self.files.get(path)
        self.lua.globals()["acpi_call"] = self.call
        self.lua.globals()["get_value"] = lambda: self.value
        self.lua.execute('''
            io = { open = function(path, mode)
                local text = mock_file(path)
                if not text then return nil end
                return { read = function(self, pattern) return text end,
                         close = function(self) end }
            end }
        ''')
        self.profile = build()
        if bootstrap:
            # Unit tests may exercise functions directly; this defines them only.
            # Integration-order tests below use bootstrap=False and actual handlers.
            self.lua.execute((ROOT / "src" / "transport.lua").read_text(encoding="utf-8"))

    def call(self, command):
        method, instance, operation, encoded = command.split()
        assert method == r"\_SB.AMW0.WMBA"
        assert instance == "0" and operation == "2" and encoded.startswith("b")
        p = bytes.fromhex(encoded[1:])
        assert len(p) == 24 and p[:4] == b"BYDL"
        assert p[14] == 8 and p[17:] == bytes(7)
        address = int.from_bytes(p[4:12], "little")
        write = p[13]
        assert write in (0, 1)
        if p[12] == 1:
            assert address in (0x2E, 0x302)
            index = p[15]
        else:
            assert p[12] == 0 and p[15] == 0
            assert self.brand == (0x20, 0x10)
            index = address - 4262166784
        self.calls.append(p)
        if address == 0x2E:
            assert not write and index in (0x20, 0x21)
            return None, self.brand[index - 0x20]
        if index == self.fail_index:
            return "simulated ACPI error", 0
        if write:
            assert index in (0x50, 0x54, 0x55)
            if index == 0x50:
                assert p[16] == 2, "Never return to broken Normal"
            else:
                assert 28 <= p[16] <= 70
                self.targets[index] = p[16]
            return None, self.write_response
        assert index in (0x5C, 0x5D)
        return None, self.tachometers[index]

    def run(self, index, operation):
        code = "\n".join(self.profile["FanConfigurations"][index][operation + "LuaCode"])
        return self.lua.execute(code)

    def writes(self):
        return [p for p in self.calls if p[13] == 1]


class ProfileTests(unittest.TestCase):
    def test_generated_artifact_matches_source(self):
        path = ROOT / "configs" / (PROFILE_ID + ".json")
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), build())

    def test_artifact_below_upstream_size_limit(self):
        path = ROOT / "configs" / (PROFILE_ID + ".json")
        self.assertLess(path.stat().st_size, 32768)

    def test_no_raw_ec_or_register_operations(self):
        profile = build()
        self.assertEqual(profile["RegisterWriteConfigurations"], [])
        for fan in profile["FanConfigurations"]:
            self.assertNotIn("WriteRegister", fan)
            self.assertNotIn("ReadRegister", fan)
            for key in ("ReadLuaCode", "WriteLuaCode", "ResetLuaCode"):
                code = "\n".join(fan[key])
                self.assertNotIn("ec_write(", code)
                self.assertNotIn("ec_read(", code)

    def test_sensors_and_critical_limits(self):
        cpu, gpu = build()["FanConfigurations"]
        self.assertEqual(cpu["Sensors"], ["@CPU"])
        self.assertEqual(gpu["Sensors"], ["@GPU"])
        self.assertEqual(cpu["CriticalTemperature"], 90)
        self.assertEqual(gpu["CriticalTemperature"], 80)
        self.assertTrue(all(f["TemperatureAlgorithmType"] == "Max" for f in (cpu, gpu)))

    def test_curves_and_hysteresis(self):
        for fan in build()["FanConfigurations"]:
            rows = fan["TemperatureThresholds"]
            self.assertEqual(sorted(r["UpThreshold"] for r in rows), [r["UpThreshold"] for r in rows])
            self.assertTrue(all(r["FanSpeed"] >= 40 for r in rows))
            self.assertEqual(rows[-1]["FanSpeed"], 100)
            for i in range(1, len(rows)):
                self.assertLess(rows[i]["DownThreshold"], rows[i-1]["UpThreshold"])
            # Reproduce pinned upstream selection logic during a rising sweep.
            selected = 0
            for temp in range(0, 101):
                while selected > 0 and temp <= rows[selected]["DownThreshold"]:
                    selected -= 1
                while selected < len(rows)-1 and temp >= rows[selected]["UpThreshold"]:
                    selected += 1
                if temp >= (80 if fan["Sensors"] == ["@CPU"] else 72):
                    self.assertEqual(rows[selected]["FanSpeed"], 100)

    def test_preparer_packets_and_errors(self):
        self.assertEqual(prepare.packet(0x2E, 0x20).hex(),
                         "4259444c2e00000000000000010008200000000000000000")
        self.assertEqual(prepare.parse_result("0x20\x00"), 32)
        for text in ("Error: AE_NOT_FOUND", "0x80000001", "0x100", "bad"):
            with self.assertRaises(RuntimeError):
                prepare.parse_result(text)


class LuaTests(unittest.TestCase):
    def test_real_service_order(self):
        fw = Firmware(bootstrap=False)
        self.assertEqual(fw.run(0, "Read"), (None, 30))
        self.assertEqual(fw.run(1, "Read"), (None, 25))
        self.assertEqual(fw.run(0, "Write"), (None, 0))
        self.assertEqual(fw.run(1, "Write"), (None, 0))
        self.assertEqual(fw.run(0, "Reset"), (None, 0))
        self.assertEqual(fw.targets, {0x54: 70, 0x55: 70})

    def test_write_without_read_initialization_fails_closed(self):
        fw = Firmware(bootstrap=False)
        self.assertIsNotNone(fw.run(0, "Write")[0])
        self.assertEqual(fw.calls, [])
        self.assertEqual(fw.run(0, "Reset"), (None, 0))
        self.assertEqual(fw.calls, [])

    def test_read_both_branches(self):
        for brand in ((0x20, 0x10), (0x12, 0x34)):
            fw = Firmware(brand=brand, access="read")
            self.assertEqual(fw.run(0, "Read"), (None, 30))
            self.assertEqual(fw.run(1, "Read"), (None, 25))
            self.assertEqual(fw.writes(), [])
            self.assertEqual(len(fw.calls), 4)

    def test_read_permission_blocks_writes_and_cleanup(self):
        fw = Firmware(access="read")
        error, value = fw.run(0, "Write")
        self.assertIn("diagnostic", error)
        self.assertEqual(fw.run(0, "Reset"), (None, 0))
        self.assertEqual(fw.writes(), [])

    def test_platform_mismatch_no_calls(self):
        for field in ("sys_vendor", "product_name", "bios_version"):
            fw = Firmware()
            fw.files["/sys/class/dmi/id/" + field] = "other"
            self.assertIsNotNone(fw.run(0, "Read")[0])
            self.assertEqual(fw.calls, [])

    def test_missing_ticket_no_calls(self):
        fw = Firmware()
        del fw.files["/run/colorful-n14-nbfc/permission"]
        self.assertIsNotNone(fw.run(0, "Write")[0])
        self.assertEqual(fw.calls, [])

    def test_bad_hash_no_calls(self):
        fw = Firmware()
        key = "/run/colorful-n14-nbfc/permission"
        fw.files[key] = fw.files[key].replace(prepare.DSDT_SHA256, "0" * 64)
        self.assertIsNotNone(fw.run(0, "Write")[0])
        self.assertEqual(fw.calls, [])

    def test_brand_mismatch_no_writes(self):
        fw = Firmware(ticket_brand=(0x12, 0x34))
        self.assertIsNotNone(fw.run(0, "Write")[0])
        self.assertEqual(fw.writes(), [])
        self.assertEqual(fw.run(0, "Reset"), (None, 0))

    def test_invalid_brand_no_writes(self):
        for brand in ((0, 0), (255, 255)):
            fw = Firmware(brand=brand)
            self.assertIsNotNone(fw.run(0, "Write")[0])
            self.assertEqual(fw.writes(), [])

    def test_independent_targets_and_full_initialization(self):
        for brand in ((0x20, 0x10), (0x12, 0x34)):
            fw = Firmware(brand=brand)
            self.assertEqual(fw.run(0, "Write"), (None, 0))
            self.assertEqual(fw.targets, {0x54: 35, 0x55: 70})
            fw.value = 56
            self.assertEqual(fw.run(1, "Write"), (None, 0))
            self.assertEqual(fw.targets, {0x54: 35, 0x55: 56})
            self.assertEqual(len(fw.writes()), 6)

    def test_nominal_zero_is_clamped(self):
        fw = Firmware()
        fw.value = 0
        self.assertEqual(fw.run(1, "Write"), (None, 0))
        self.assertEqual(fw.targets[0x55], 28)

    def test_every_supported_target(self):
        for brand in ((0x20, 0x10), (0x12, 0x34)):
            fw = Firmware(brand=brand)
            for value in range(71):
                fw.value = value
                self.assertEqual(fw.run(0, "Write"), (None, 0))
                self.assertEqual(fw.targets[0x54], max(28, value))

    def test_invalid_target_no_calls(self):
        for value in (-1, 71, 2.5):
            fw = Firmware()
            fw.value = value
            self.assertIsNotNone(fw.run(0, "Write")[0])
            self.assertEqual(fw.calls, [])

    def test_cleanup_before_control_is_noop(self):
        fw = Firmware()
        self.assertEqual(fw.run(0, "Reset"), (None, 0))
        self.assertEqual(fw.calls, [])

    def test_cleanup_requests_both_full_never_normal(self):
        fw = Firmware()
        fw.run(0, "Write")
        self.assertEqual(fw.run(0, "Reset"), (None, 0))
        self.assertEqual(fw.run(1, "Reset"), (None, 0))
        self.assertEqual(fw.targets, {0x54: 70, 0x55: 70})

    def test_tachometer_is_not_faked(self):
        fw = Firmware()
        for value in (0, 70, 0, 25):
            fw.tachometers[0x5D] = value
            self.assertEqual(fw.run(1, "Read"), (None, value))
        self.assertEqual(fw.writes(), [])

    def test_bad_tachometer_returns_error(self):
        fw = Firmware()
        fw.tachometers[0x5D] = 255
        self.assertIsNotNone(fw.run(1, "Read")[0])
        self.assertEqual(fw.writes(), [])

    def test_failure_during_initial_write_allows_cleanup(self):
        fw = Firmware()
        fw.fail_index = 0x54
        self.assertIsNotNone(fw.run(0, "Write")[0])
        self.assertEqual(fw.targets[0x55], 70)
        fw.fail_index = None
        self.assertEqual(fw.run(0, "Reset"), (None, 0))
        self.assertEqual(fw.targets[0x54], 70)

    def test_write_return_is_checked(self):
        fw = Firmware()
        fw.write_response = 1
        self.assertIsNotNone(fw.run(0, "Write")[0])


if __name__ == "__main__":
    unittest.main()

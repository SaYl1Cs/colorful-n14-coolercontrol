"""Preparation tests: every platform/process/ACPI access is mocked or redirected.

Temporary fixtures live under the project. Never execute launchers or NBFC.
"""
from contextlib import ExitStack, nullcontext, redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import build_profile as builder
import prepare


def smbios(major=2, minor=0):
    return bytes([0, 24]) + bytes(20) + bytes([major, minor])


class PacketAndIdentityTests(unittest.TestCase):
    def test_exact_read_packets(self):
        self.assertEqual(prepare.packet(0x2E, 0x20).hex(),
                         "4259444c2e00000000000000010008200000000000000000")
        for index in (0x5C, 0x55):
            data = prepare.packet(0x302, index)
            self.assertEqual(len(data), 24)
            self.assertEqual(data[13], 0)
            self.assertEqual(data[15], index)
            self.assertEqual(data[4:8], bytes([2, 3, 0, 0]))

    def test_acpi_result_errors(self):
        self.assertEqual(prepare.parse_result("0x44\x00\n"), 68)
        for text in ("Error: AE_NOT_FOUND", "0x80000001", "0x100", "-1", "bad", "0x1 trailing"):
            with self.assertRaises(RuntimeError):
                prepare.parse_result(text)

    def test_read_byte_never_accepts_write_packet(self):
        packet = bytearray(prepare.packet(0x302, 0x55))
        packet[13] = 1
        with patch.object(prepare.os, "open") as opened:
            for data in (packet, b"short"):
                with self.assertRaises(RuntimeError):
                    prepare.read_byte(data)
            opened.assert_not_called()

    def test_acpi_reads_use_mocks_and_close_fds(self):
        packet = prepare.packet(0x302, 0x55)
        request = f"{prepare.METHOD} 0 2 b{packet.hex()}".encode()
        with patch.object(prepare.os, "open", side_effect=[101, 102]), \
             patch.object(prepare.os, "write", return_value=len(request)) as write, \
             patch.object(prepare.os, "read", return_value=b"0x46\x00"), \
             patch.object(prepare.os, "close") as close:
            self.assertEqual(prepare.read_byte(packet), 70)
            write.assert_called_once_with(101, request)
            self.assertEqual([call.args[0] for call in close.call_args_list], [101, 102])

    def test_short_acpi_write_propagates(self):
        with patch.object(prepare.os, "open", return_value=101), \
             patch.object(prepare.os, "write", return_value=1), \
             patch.object(prepare.os, "close") as close:
            with self.assertRaisesRegex(RuntimeError, "Short"):
                prepare.read_byte(prepare.packet(0x302, 0x55))
            close.assert_called_once_with(101)

    def test_acpi_os_errors_propagate(self):
        with patch.object(prepare.os, "open", side_effect=PermissionError("mock denied")):
            with self.assertRaises(PermissionError):
                prepare.read_byte(prepare.packet(0x302, 0x55))

    def test_smbios_requires_exact_2_0(self):
        self.assertEqual(prepare.smbios_ec_version(smbios()), "2.0")
        for data in (b"", smbios()[:23], smbios(2, 1), smbios(1, 0), smbios(255, 255),
                     b"\x01" + smbios()[1:], b"\x00\x19" + smbios()[2:]):
            with self.assertRaises(RuntimeError):
                prepare.smbios_ec_version(data)

    def test_wmi_exact_guid_suffix_regex(self):
        for name in (prepare.GUID, prepare.GUID.upper(), prepare.GUID + "-0", prepare.GUID + ":123"):
            self.assertTrue(prepare.matches_wmi_name(name), name)
        for name in ("x" + prepare.GUID, prepare.GUID + "x", prepare.GUID + "-", prepare.GUID + ":",
                     prepare.GUID + "-1x", prepare.GUID + "-1:2", prepare.GUID + "\n",
                     prepare.GUID + "-١", "{" + prepare.GUID + "}"):
            self.assertFalse(prepare.matches_wmi_name(name), name)


class PlatformTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.tmp = Path(self.stack.enter_context(tempfile.TemporaryDirectory(dir=ROOT)))
        self.dmi = self.tmp / "dmi"
        self.dmi.mkdir()
        for key, value in {"sys_vendor": "COLORFUL", "product_name": "N14 24", "bios_version": "2.07"}.items():
            (self.dmi / key).write_text(value + "\n")
        self.raw = self.tmp / "raw"
        self.raw.write_bytes(smbios())
        self.dsdt = self.tmp / "DSDT"
        self.dsdt.write_bytes(b"mock DSDT, not hardware")
        self.wmi = self.tmp / "wmi"
        self.wmi.mkdir()
        (self.wmi / (prepare.GUID + "-0")).mkdir()
        self.acpi = self.tmp / "acpi"
        self.acpi.touch()
        self.proc = self.tmp / "proc"
        self.proc.mkdir()
        self.pidfile = self.tmp / "nbfc_service.pid"
        self.mapping = {
            "/sys/class/dmi/id": self.dmi,
            "/sys/firmware/acpi/tables/DSDT": self.dsdt,
            "/sys/bus/wmi/devices": self.wmi,
            "/proc/acpi/call": self.acpi,
            "/proc": self.proc,
            "/run/nbfc_service.pid": self.pidfile,
        }
        def safe_path(value):
            if str(value) not in self.mapping:
                raise AssertionError(f"Unmocked path: {value}")
            return self.mapping[str(value)]
        self.stack.enter_context(patch.object(prepare, "Path", side_effect=safe_path))
        self.stack.enter_context(patch.object(prepare, "SMBIOS_RAW", self.raw))
        self.stack.enter_context(patch.object(prepare.sys, "platform", "linux"))
        self.uid = self.stack.enter_context(patch.object(prepare.os, "geteuid", return_value=0))
        self.hash = self.stack.enter_context(patch.object(prepare.hashlib, "sha256"))
        self.hash.return_value.hexdigest.return_value = prepare.DSDT_SHA256
        self.stack.enter_context(redirect_stdout(io.StringIO()))

    def test_valid_platform_reads_only_redirected_fixtures(self):
        self.assertEqual(prepare.check_platform(), builder.PROFILE_PATH)
        self.hash.assert_called_once_with(b"mock DSDT, not hardware")

    def test_root_and_linux_required(self):
        self.uid.return_value = 1000
        with self.assertRaises(RuntimeError):
            prepare.check_platform()
        self.uid.return_value = 0
        with patch.object(prepare.sys, "platform", "win32"), self.assertRaises(RuntimeError):
            prepare.check_platform()

    def test_all_dmi_checks_fail_closed(self):
        for field in ("sys_vendor", "product_name", "bios_version"):
            path = self.dmi / field
            old = path.read_text()
            path.write_text("mismatch")
            with self.assertRaisesRegex(RuntimeError, "Unsupported platform"):
                prepare.check_platform()
            path.write_text(old)

    def test_smbios_mismatch_stops_preflight(self):
        self.raw.write_bytes(smbios(2, 1))
        with self.assertRaisesRegex(RuntimeError, "SMBIOS"):
            prepare.check_platform()

    def test_dsdt_mismatch_has_no_override(self):
        self.hash.return_value.hexdigest.return_value = "0" * 64
        with self.assertRaisesRegex(RuntimeError, "DSDT hash mismatch"):
            prepare.check_platform()

    def test_wmi_absence_fails_closed(self):
        (self.wmi / (prepare.GUID + "-0")).rmdir()
        (self.wmi / (prepare.GUID + "-0-extra")).mkdir()
        with self.assertRaisesRegex(RuntimeError, "WMI"):
            prepare.check_platform()

    def test_modified_generated_profile_rejected(self):
        with patch.object(prepare, "build", return_value={}):
            with self.assertRaisesRegex(RuntimeError, "differs"):
                prepare.check_platform()

    def test_pidfile_even_stale_blocks_old_and_new(self):
        self.pidfile.write_text("999999")
        with self.assertRaisesRegex(RuntimeError, "PID file"):
            prepare.assert_service_stopped()

    def test_foreground_old_profile_process_blocks(self):
        process = self.proc / "1234"
        process.mkdir()
        (process / "comm").write_text("nbfc_service\n")
        with self.assertRaisesRegex(RuntimeError, "already running"):
            prepare.assert_service_stopped()

    def test_non_nbfc_and_vanished_process_allowed(self):
        for pid in ("123", "456"):
            (self.proc / pid).mkdir()
        (self.proc / "123" / "comm").write_text("python3\n")
        prepare.assert_service_stopped()

    def test_unknown_process_permission_fails_closed(self):
        (self.proc / "123").mkdir()
        with patch.object(Path, "read_text", side_effect=PermissionError("mock denied")):
            with self.assertRaisesRegex(RuntimeError, "Cannot verify"):
                prepare.assert_service_stopped()


class MainFlowTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.tmp = Path(self.stack.enter_context(tempfile.TemporaryDirectory(dir=ROOT)))
        self.stack.enter_context(patch.object(prepare, "PERMISSION_DIR", self.tmp))
        self.platform = self.stack.enter_context(patch.object(prepare, "check_platform", return_value=builder.PROFILE_PATH))
        self.stopped = self.stack.enter_context(patch.object(prepare, "assert_service_stopped"))
        self.lock = self.stack.enter_context(patch.object(prepare, "session_lock", side_effect=lambda: nullcontext(123)))
        self.read = self.stack.enter_context(patch.object(prepare, "read_byte", side_effect=[0x55, 0x71, 68, 70]))
        self.permission = self.stack.enter_context(patch.object(prepare, "write_permission"))
        self.path = self.stack.enter_context(patch.object(prepare, "Path"))
        self.path.return_value.exists.return_value = True
        self.exec = self.stack.enter_context(patch.object(prepare.os, "execv"))
        self.inherit = self.stack.enter_context(patch.object(prepare.os, "set_inheritable"))
        self.stdout = self.stack.enter_context(redirect_stdout(io.StringIO()))
        self.stack.enter_context(redirect_stderr(io.StringIO()))
        self.arm = ["arm", "--acknowledge-risk", "--sensors-verified", "--expected-ec-id", "55:71", "--ec-version", "2.0"]

    def test_default_check_no_acpi_or_permission(self):
        self.assertEqual(prepare.main([]), 0)
        self.read.assert_not_called()
        self.permission.assert_not_called()
        self.lock.assert_not_called()
        self.exec.assert_not_called()

    def test_probe_uses_cpu_tach_gpu_command_only(self):
        self.assertEqual(prepare.main(["probe", "--acknowledge-risk"]), 0)
        self.assertEqual([call.args[0][15] for call in self.read.call_args_list], [0x20, 0x21, 0x5C, 0x55])
        self.assertTrue(all(call.args[0][13] == 0 for call in self.read.call_args_list))
        report = json.loads(self.stdout.getvalue())
        self.assertEqual(report["cpu_rpm_nominal"], 6800)
        self.assertEqual(report["gpu_command"], 70)
        self.assertEqual(report["gpu_command_percent"], 100)
        self.assertIsNone(report["gpu_rpm"])
        self.permission.assert_called_once_with((0x55, 0x71), False, builder.PROFILE_PATH)
        self.exec.assert_not_called()

    def test_arm_requires_every_acknowledgement(self):
        cases = [["arm"], ["arm", "--acknowledge-risk"],
                 [arg for arg in self.arm if arg != "--sensors-verified"],
                 self.arm[:-2], self.arm[:-1] + ["2.1"],
                 ["55:70" if arg == "55:71" else arg for arg in self.arm]]
        for args in cases:
            with self.assertRaises(SystemExit):
                prepare.main(args)
        self.platform.assert_not_called()
        self.read.assert_not_called()
        self.permission.assert_not_called()

    def test_probe_requires_risk_ack(self):
        with self.assertRaises(SystemExit):
            prepare.main(["probe"])
        self.read.assert_not_called()

    def test_arm_control_permission_only_after_reads(self):
        self.assertEqual(prepare.main(self.arm), 0)
        self.assertEqual(self.read.call_count, 4)
        self.permission.assert_called_once_with((0x55, 0x71), True, builder.PROFILE_PATH)
        self.exec.assert_not_called()

    def test_bad_identity_no_permission_or_fan_reads(self):
        self.read.side_effect = [0x20, 0x10]
        self.assertEqual(prepare.main(self.arm), 1)
        self.assertEqual(self.read.call_count, 2)
        self.permission.assert_not_called()

    def test_bad_cpu_tach_and_gpu_command_no_permission(self):
        for cpu, gpu in ((71, 70), (68, 141), (68, 192), (68, 71)):
            self.read.side_effect = [0x55, 0x71, cpu, gpu]
            self.assertEqual(prepare.main(self.arm), 1)
        self.permission.assert_not_called()

    def test_acpi_error_propagates_without_ticket(self):
        self.read.side_effect = RuntimeError("ACPI failure")
        self.assertEqual(prepare.main(self.arm), 1)
        self.permission.assert_not_called()

    def test_existing_process_blocks_before_acpi(self):
        self.stopped.side_effect = RuntimeError("already running")
        self.assertEqual(prepare.main(self.arm), 1)
        self.read.assert_not_called()
        self.permission.assert_not_called()

    def test_old_permission_removed_before_bad_read(self):
        old = self.tmp / "permission"
        old.write_text("stale control permission")
        self.read.side_effect = RuntimeError("ACPI failure")
        self.assertEqual(prepare.main(self.arm), 1)
        self.assertFalse(old.exists())

    def test_read_only_launch_fixed_binary_and_flag(self):
        self.assertEqual(prepare.main(["probe", "--acknowledge-risk", "--launch", prepare.NBFC_SERVICE]), 0)
        self.exec.assert_called_once_with("/usr/local/bin/nbfc_service", ["/usr/local/bin/nbfc_service", "--read-only", "--config-file", str(self.tmp / "service.json")])
        self.inherit.assert_called_once_with(123, True)
        self.assertEqual(self.stopped.call_count, 2)

    def test_control_launch_fixed_binary_no_shell(self):
        self.assertEqual(prepare.main(self.arm + ["--launch", prepare.NBFC_SERVICE]), 0)
        self.exec.assert_called_once_with("/usr/local/bin/nbfc_service", ["/usr/local/bin/nbfc_service", "--config-file", str(self.tmp / "service.json")])

    def test_arbitrary_executable_and_check_launch_rejected(self):
        for args in (["check", "--launch", prepare.NBFC_SERVICE], self.arm + ["--launch", "/bin/sh"]):
            with self.assertRaises(SystemExit):
                prepare.main(args)
        self.exec.assert_not_called()
        self.read.assert_not_called()


class RuntimePermissionTests(unittest.TestCase):
    def test_permission_config_dummy_automatic_and_private(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp, \
             patch.object(prepare, "PERMISSION_DIR", Path(tmp)), \
             patch.object(prepare, "ensure_runtime_dir"), redirect_stdout(io.StringIO()):
            for control in (False, True):
                prepare.write_permission((0x55, 0x71), control, builder.PROFILE_PATH)
                permission = Path(tmp) / "permission"
                expected = "control" if control else "read"
                self.assertEqual(permission.read_text(), f"{prepare.DSDT_SHA256} 55 71 {expected}\n")
                self.assertEqual(stat.S_IMODE(permission.stat().st_mode), 0o600)
                config = json.loads((Path(tmp) / "service.json").read_text())
                self.assertEqual(config["EmbeddedControllerType"], "dummy")
                self.assertEqual(config["TargetFanSpeeds"], [-1, -1])
                self.assertEqual(config["SelectedConfigId"], str(builder.PROFILE_PATH))

    def test_unpinned_identity_cannot_create_ticket(self):
        with patch.object(prepare, "ensure_runtime_dir") as ensure, self.assertRaises(RuntimeError):
            prepare.write_permission((0x20, 0x10), True, builder.PROFILE_PATH)
        ensure.assert_not_called()

    def test_runtime_directory_refuses_symlink_and_wrong_owner(self):
        for mode, owner in ((stat.S_IFLNK, 0), (stat.S_IFDIR, 1000), (stat.S_IFREG, 0)):
            directory = Mock()
            directory.lstat.return_value = SimpleNamespace(st_mode=mode, st_uid=owner)
            with patch.object(prepare, "PERMISSION_DIR", directory), \
                 patch.object(prepare.os, "chmod") as chmod, self.assertRaises(RuntimeError):
                prepare.ensure_runtime_dir()
            chmod.assert_not_called()

    def test_exclusive_lock_uses_local_fixture_only(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp, \
             patch.object(prepare, "PERMISSION_DIR", Path(tmp)), \
             patch.object(prepare, "ensure_runtime_dir"), \
             patch.object(prepare.os, "fstat", return_value=SimpleNamespace(st_mode=stat.S_IFREG, st_uid=0, st_nlink=1)):
            with prepare.session_lock():
                with self.assertRaisesRegex(RuntimeError, "already running"):
                    with prepare.session_lock():
                        self.fail("second lock must fail")
            with prepare.session_lock():
                pass

    def test_launchers_fixed_binary_and_no_service_manager(self):
        for name, action in (("run-read-only.sh", "probe"), ("run-control.sh", "arm")):
            text = (ROOT / name).read_text()
            self.assertIn(f'exec python3 prepare.py {action} --launch /usr/local/bin/nbfc_service "$@"', text)
            for unsafe in ("sudo ", "systemctl ", "eval ", "$(which", "/run/colorful-n14-nbfc"):
                self.assertNotIn(unsafe, text)


if __name__ == "__main__":
    unittest.main()

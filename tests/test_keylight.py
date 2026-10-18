import json
import tempfile
import unittest
from contextlib import ExitStack
from copy import deepcopy
from pathlib import Path
from unittest.mock import call, patch

import adapter_server as server
import coolercontrol_adapter as adapter
import provenance_fence as fence
from protocol_fixtures import Harness


class KeylightConversionTests(unittest.TestCase):
    def test_indexed_protocol_boundaries(self):
        for percent, raw in ((0, 175), (50, 88), (100, 0)):
            self.assertEqual(adapter._keylight_raw_from_percent(percent), raw)
            self.assertEqual(adapter._keylight_percent_from_raw(raw), percent)

    def test_conversions_reject_invalid_values(self):
        for value in (-1, 101, True, 1.5, '20'):
            with self.subTest(value=value), self.assertRaises(SystemExit):
                adapter._keylight_raw_from_percent(value)
        for value in (-1, 176, True, 1.5, '20'):
            with self.subTest(value=value), self.assertRaises(SystemExit):
                adapter._keylight_percent_from_raw(value)


class Hardware:
    def __init__(self, mode=2, targets=(70, 70), raw=110):
        self.regs = {adapter.MODE: mode, adapter.CPU_TARGET: targets[0],
                     adapter.GPU_TARGET: targets[1], adapter.KEYLIGHT: raw}
        self.events = []
        self.light_result = 0
        self.fail_fan_index = None
        self.readback_wrong = False

    def read(self, index):
        return self.regs[index]

    def write(self, index, value):
        self.events.append(('fan', index, value))
        if index == self.fail_fan_index and value != 0:
            raise OSError('offline fan-write failure')
        self.regs[index] = value
        if index == adapter.MODE and value == 0:
            # Firmware may change both target registers in automatic mode.
            self.regs[adapter.CPU_TARGET] = 33
            self.regs[adapter.GPU_TARGET] = 44

    def acpi(self, index, value=None, port=adapter.PORT):
        if index != adapter.KEYLIGHT or port != adapter.PORT:
            raise AssertionError('Unexpected raw access in lighting test')
        if value is None:
            return 255 if self.readback_wrong else self.regs[index]
        self.events.append(('light', value))
        if self.light_result == 0:
            self.regs[index] = value
        return self.light_result

    def sleep(self, delay):
        self.events.append(('sleep', delay))


class KeylightTransactionTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.tmp = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.path = Path(self.tmp) / 'run' / 'keylight.json'
        self.hw = Hardware()
        self.guard = self.stack.enter_context(patch.object(adapter, 'guarded'))
        self.stack.enter_context(patch.object(adapter, 'KEYLIGHT_STATE', self.path))
        self.stack.enter_context(patch.object(adapter, 'read_raw', side_effect=self.hw.read))
        self.stack.enter_context(patch.object(adapter, 'write_raw', side_effect=self.hw.write))
        self.stack.enter_context(patch.object(adapter, 'acpi', side_effect=self.hw.acpi))
        self.stack.enter_context(patch.object(adapter.time, 'sleep', side_effect=self.hw.sleep))
        self.emergency = self.stack.enter_context(patch.object(adapter, '_failed_write_full'))

    def test_manual_full_transaction_has_exactly_one_pre_delay_and_restores_both_targets(self):
        self.assertEqual(adapter.set_key_brightness(50), 50)
        self.assertEqual(self.hw.events, [
            ('fan', adapter.MODE, 0), ('sleep', 0.02), ('light', 88),
            ('fan', adapter.MODE, 2), ('fan', adapter.CPU_TARGET, 70),
            ('fan', adapter.GPU_TARGET, 70)])
        self.assertEqual(self.hw.regs[adapter.MODE], 2)
        self.assertEqual(self.hw.regs[adapter.CPU_TARGET], 70)
        self.assertEqual(self.hw.regs[adapter.GPU_TARGET], 70)
        self.guard.assert_called_once_with(True)
        self.emergency.assert_not_called()
        self.assertEqual(json.loads(self.path.read_text()),
                         {'schema': 1, 'raw': 88, 'brightness': 50, 'pending': False})

    def test_validated_individual_targets_are_not_equalized_or_lost_to_firmware(self):
        self.hw.regs.update({adapter.CPU_TARGET: 22, adapter.GPU_TARGET: 39})
        with patch.object(adapter, '_keylight_fan_targets', return_value=(22, 39)) as validate:
            adapter.set_key_brightness(100)
        validate.assert_called_once_with((22, 39))
        self.assertEqual(self.hw.events[-3:], [('fan', adapter.MODE, 2),
                         ('fan', adapter.CPU_TARGET, 22), ('fan', adapter.GPU_TARGET, 39)])

    def test_original_automatic_mode_is_left_automatic(self):
        self.hw.regs[adapter.MODE] = 0
        adapter.set_key_brightness(0)
        self.assertEqual(self.hw.events, [('sleep', 0.02), ('light', 175)])
        self.assertEqual(self.hw.regs[adapter.MODE], 0)

    def test_invalid_percent_never_touches_firmware(self):
        with self.assertRaises(SystemExit):
            adapter.set_key_brightness(101)
        self.assertEqual(self.hw.events, [])
        self.guard.assert_not_called()

    def test_platform_guard_failure_never_touches_firmware(self):
        self.guard.side_effect = SystemExit(1)
        with self.assertRaises(SystemExit):
            adapter.set_key_brightness(50)
        self.assertEqual(self.hw.events, [])

    def test_unknown_mode_never_touches_hardware(self):
        self.hw.regs[adapter.MODE] = 3
        with self.assertRaises(SystemExit):
            adapter.set_key_brightness(50)
        self.assertEqual(self.hw.events, [])

    def test_lighting_failure_requests_emergency_full_and_does_not_save(self):
        self.hw.light_result = 1
        with self.assertRaises(SystemExit):
            adapter.set_key_brightness(50)
        self.emergency.assert_called_once()
        self.assertFalse(self.path.exists())

    def test_fan_return_failure_requests_emergency_full(self):
        self.hw.fail_fan_index = adapter.CPU_TARGET
        with self.assertRaises(OSError):
            adapter.set_key_brightness(50)
        self.emergency.assert_called_once()
        self.assertFalse(self.path.exists())

    def test_bad_readback_is_not_a_success(self):
        self.hw.readback_wrong = True
        with self.assertRaises(SystemExit):
            adapter.set_key_brightness(50)
        self.emergency.assert_called_once()
        self.assertFalse(self.path.exists())


class KeylightStateTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.tmp = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.path = Path(self.tmp) / 'run' / 'keylight.json'
        self.stack.enter_context(patch.object(adapter, 'KEYLIGHT_STATE', self.path))
        self.stack.enter_context(patch.object(adapter, 'guarded'))
        self.raw = self.stack.enter_context(patch.object(adapter, 'acpi', return_value=110))
        self.apply = self.stack.enter_context(patch.object(adapter, '_apply_key_raw'))

    def test_exact_raw_level_survives_without_percent_round_trip(self):
        self.assertEqual(adapter.save_key_brightness(), 38)
        # A percent round-trip would write 109, not the original raw 110.
        self.assertEqual(adapter._keylight_raw_from_percent(38), 109)
        self.assertEqual(adapter.restore_key_brightness(), 38)
        self.apply.assert_called_once_with(110)
        self.assertFalse(json.loads(self.path.read_text())['pending'])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_saved_off_stays_off(self):
        self.raw.return_value = 175
        self.assertEqual(adapter.save_key_brightness(), 0)
        self.assertEqual(adapter.restore_key_brightness(), 0)
        self.apply.assert_called_once_with(175)

    def test_duplicate_post_is_noop(self):
        adapter.save_key_brightness()
        adapter.restore_key_brightness()
        self.assertIsNone(adapter.restore_key_brightness())
        self.apply.assert_called_once()

    def test_no_saved_state_is_noop(self):
        self.assertIsNone(adapter.restore_key_brightness())
        self.apply.assert_not_called()

    def test_manual_set_cancels_old_pending_restore(self):
        adapter.save_key_brightness()
        adapter.set_key_brightness(50)
        self.apply.reset_mock()
        self.assertIsNone(adapter.restore_key_brightness())
        self.apply.assert_not_called()

    def test_failed_pre_read_invalidates_previous_pending_state(self):
        adapter.save_key_brightness()
        self.raw.side_effect = SystemExit(1)
        with self.assertRaises(SystemExit):
            adapter.save_key_brightness()
        self.assertIsNone(adapter.restore_key_brightness())
        self.apply.assert_not_called()

    def test_failed_restore_remains_pending_for_bounded_retry(self):
        adapter.save_key_brightness()
        self.apply.side_effect = OSError('offline failure')
        with self.assertRaises(OSError):
            adapter.restore_key_brightness()
        with self.path.open(encoding='ascii') as stream:
            self.assertTrue(json.load(stream)['pending'])
        self.assertEqual(self.apply.call_count, 1)
        self.apply.assert_called_once_with(110)

    def test_invalid_states_never_write(self):
        adapter.save_key_brightness()
        valid = json.loads(self.path.read_text())
        variants = [{'brightness': 50}, [], dict(valid, raw=True), dict(valid, raw=176),
                    dict(valid, pending=1), dict(valid, brightness=0), dict(valid, schema=True),
                    dict(valid, unknown=1)]
        for value in variants:
            with self.subTest(value=value):
                self.path.write_text(json.dumps(value))
                with self.assertRaises(SystemExit):
                    adapter.restore_key_brightness()
        self.apply.assert_not_called()

    def test_malformed_or_oversize_json_never_writes(self):
        self.path.parent.mkdir()
        for text in ('{', 'x' * 4097, '{"schema":1,"schema":2}'):
            self.path.write_text(text)
            with self.assertRaises(SystemExit):
                adapter.restore_key_brightness()
        self.apply.assert_not_called()


class LightingCoolingIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.addCleanup(self.h.close)
        self.h.select()
        self.assertTrue(self.h.send().startswith(b'OK'))
        self.initial = deepcopy(fence._read())
        duty = self.initial['last_duty']
        self.raw = adapter.MIN_COMMAND + round(duty * (adapter.MAX_RAW - adapter.MIN_COMMAND) / 100)

    def test_authorized_targets_survive_without_renewing_or_fabricating_provenance(self):
        self.assertEqual(adapter._keylight_fan_targets((self.raw, self.raw)), (self.raw, self.raw))
        self.assertEqual(fence._read(), self.initial)

    def test_cached_targets_that_do_not_match_applied_duty_are_rejected(self):
        wrong = max(1, self.raw - 1)
        with self.assertRaises(ValueError):
            adapter._keylight_fan_targets((wrong, wrong))

    def test_expired_samples_force_full_not_stale_low_targets(self):
        self.h.advance(20, sample=False)
        self.h.writes.reset_mock()
        with patch.object(adapter, 'read_raw', return_value=70):
            adapter._keylight_return_fans((self.raw, self.raw))
        self.assertEqual(self.h.writes.call_args_list, [call(adapter.MODE, 2),
                         call(adapter.CPU_TARGET, 70), call(adapter.GPU_TARGET, 70)])
        self.assertTrue(fence.status()['full_confirmed'])
        self.assertEqual(fence.status()['phase'], 'closed')

    def test_transition_requiring_higher_duty_rejects_restore_of_low_targets(self):
        self.h.cpu = 85000
        self.h.advance()
        with self.assertRaises(ValueError):
            adapter._keylight_fan_targets((self.raw, self.raw))

    def test_invalid_zero_target_is_never_restored(self):
        with self.assertRaises(ValueError):
            adapter._keylight_fan_targets((0, 70))

    def test_individual_bounded_hold_is_preserved(self):
        fence.begin_hold([60, 65], self.h.data, self.h.policy())
        before = deepcopy(fence._read())
        self.assertEqual(adapter._keylight_fan_targets((60, 65)), (60, 65))
        self.assertEqual(fence._read(), before)
        self.h.advance(11)
        with self.assertRaises(ValueError):
            adapter._keylight_fan_targets((60, 65))

    def test_closed_fence_does_not_authorize_cached_low_targets(self):
        fence.close()
        with self.assertRaises(ValueError):
            adapter._keylight_fan_targets((self.raw, self.raw))

    def test_full_return_does_not_require_fresh_low_duty_authority(self):
        self.h.advance(20, sample=False)
        self.assertEqual(adapter._keylight_fan_targets((70, 70)), (70, 70))

    def test_power_mode_race_rejects_low_return(self):
        with patch.object(adapter, 'read_live_power_state', side_effect=[('active', 'D0'), ('suspended', 'D3cold')]):
            with self.assertRaises(ValueError):
                adapter._keylight_fan_targets((self.raw, self.raw))

    def test_complete_rpc_preserves_fence_and_restores_fan_targets(self):
        hw = Hardware(targets=(self.raw, self.raw))
        self.h.writes.reset_mock()
        self.h.writes.side_effect = hw.write
        with patch.object(adapter, 'KEYLIGHT_STATE', self.h.root / 'keylight.json'), \
                patch.object(adapter, 'read_raw', side_effect=hw.read), \
                patch.object(adapter, 'acpi', side_effect=hw.acpi), \
                patch.object(adapter.time, 'sleep', side_effect=lambda delay: self.h.advance(delay)):
            self.assertEqual(server.dispatch('keylight-set 50'), b'OK 50\n')
        self.assertEqual(fence._read(), self.initial)
        self.assertEqual(hw.regs[adapter.CPU_TARGET], self.raw)
        self.assertEqual(hw.regs[adapter.GPU_TARGET], self.raw)
        self.h.watchdog.tick()
        self.assertEqual(fence._read()['phase'], 'active')


class LidTests(unittest.TestCase):
    def test_exact_acpi_paths_and_no_literal_newline_or_extra_root_prefix(self):
        paths = (r'\_SB.PC00.LPCB.EC0.PSTA', r'\LIDS', r'\_SB.PC00.LPCB.EC0._Q14')
        self.assertEqual((adapter.LID_CONTROLLER, adapter.LID_CACHE, adapter.LID_REFRESH), paths)
        for method in paths:
            with patch.object(adapter.os, 'open', side_effect=[101, 102]), \
                    patch.object(adapter.os, 'close'), \
                    patch.object(adapter.os, 'write', return_value=len(method)) as write, \
                    patch.object(adapter.os, 'read', return_value=b'0x0\x00'):
                self.assertEqual(adapter.acpi_method(method), 0)
            self.assertEqual(write.call_args.args, (101, method.encode('ascii')))

    def test_unapproved_method_is_rejected_before_transport(self):
        with patch.object(adapter.os, 'open') as opened, self.assertRaises(SystemExit):
            adapter.acpi_method(r'\_WAK')
        opened.assert_not_called()

    def test_refresh_reads_real_state_and_notifies_once_on_mismatch(self):
        with patch.object(adapter, 'guarded'), \
                patch.object(adapter, 'acpi_method', side_effect=[6, 0, 0, 6, 1]) as method:
            self.assertEqual(adapter.refresh_lid(), {'controller_open': True, 'cached_open': True})
        self.assertEqual(method.call_args_list, [call(adapter.LID_CONTROLLER), call(adapter.LID_CACHE),
                         call(adapter.LID_REFRESH), call(adapter.LID_CONTROLLER), call(adapter.LID_CACHE)])

    def test_matching_closed_lid_is_not_forced_open(self):
        with patch.object(adapter, 'guarded'), patch.object(adapter, 'acpi_method', side_effect=[2, 0]) as method:
            self.assertEqual(adapter.refresh_lid(), {'controller_open': False, 'cached_open': False})
        self.assertNotIn(call(adapter.LID_REFRESH), method.call_args_list)

    def test_matching_open_lid_is_noop(self):
        with patch.object(adapter, 'guarded'), patch.object(adapter, 'acpi_method', side_effect=[6, 1]) as method:
            self.assertTrue(adapter.refresh_lid()['cached_open'])
        self.assertEqual(method.call_count, 2)

    def test_refresh_failure_is_reported(self):
        with patch.object(adapter, 'guarded'), patch.object(adapter, 'acpi_method', side_effect=[6, 0, 0, 6, 0]), self.assertRaises(SystemExit):
            adapter.refresh_lid()

    def test_invalid_cached_state_is_not_interpreted_as_open(self):
        with patch.object(adapter, 'guarded'), patch.object(adapter, 'acpi_method', side_effect=[6, 255]), self.assertRaises(SystemExit):
            adapter.lid_state()

    def test_lid_failure_does_not_manipulate_fans(self):
        with patch.object(adapter, 'refresh_lid', side_effect=SystemExit(1)), patch.object(adapter, 'fixed_full') as full:
            self.assertTrue(server.dispatch('lid-refresh').startswith(b'ERR'))
        full.assert_not_called()

    def test_server_read_command_and_invalid_brightness(self):
        with patch.object(adapter, 'read_key_brightness', return_value=37):
            self.assertEqual(server.dispatch('keylight-get'), b'OK 37\n')
        for command in ('keylight-set 101', 'keylight-set -1', 'keylight-set 50 extra'):
            self.assertTrue(server.dispatch(command).startswith(b'ERR'))


if __name__ == '__main__':
    unittest.main()

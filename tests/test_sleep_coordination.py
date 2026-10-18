import importlib.machinery
import importlib.util
from pathlib import Path
import struct
import unittest
from unittest.mock import MagicMock, patch

path = Path(__file__).resolve().parents[1] / 'colorful-n14-acpi.system-sleep'
loader = importlib.machinery.SourceFileLoader('n14_sleep_hook', str(path))
spec = importlib.util.spec_from_loader(loader.name, loader)
hook = importlib.util.module_from_spec(spec)
loader.exec_module(hook)

OPEN = {'controller_open': True, 'cached_open': True}
CLOSED = {'controller_open': False, 'cached_open': False}


class SleepCoordinationTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.calls = []
        self.messages = []

    def clock(self):
        return self.now

    def sleep(self, delay):
        self.now += delay

    def run_hook(self, action, rpc):
        return hook.main(action, rpc=rpc, clock=self.clock, sleep=self.sleep, emit=self.messages.append)

    def test_pre_only_saves_current_brightness(self):
        def rpc(command):
            self.calls.append(command)
            return 35
        self.assertEqual(self.run_hook('pre', rpc), 0)
        self.assertEqual(self.calls, ['keylight-save'])

    def test_pre_failure_is_logged_but_does_not_block_sleep(self):
        def rpc(command):
            raise OSError('offline connection failure')
        self.assertEqual(self.run_hook('pre', rpc), 0)
        self.assertTrue(any('failed' in item for item in self.messages))

    def test_post_refreshes_lid_before_restoring_lights(self):
        def rpc(command, timeout):
            self.calls.append(command)
            return OPEN if command == 'lid-refresh' else 35
        self.assertEqual(self.run_hook('post', rpc), 0)
        self.assertEqual(self.calls, ['lid-refresh', 'keylight-restore', 'keylight-reapply'])

    def test_delayed_lid_update_is_repolled_outside_adapter(self):
        lid_calls = 0
        def rpc(command, timeout):
            nonlocal lid_calls
            self.calls.append(command)
            if command == 'lid-refresh':
                lid_calls += 1
                return OPEN if lid_calls == 3 else CLOSED
            return 35
        self.assertEqual(self.run_hook('post', rpc), 0)
        self.assertEqual(self.calls, ['lid-refresh', 'keylight-restore', 'keylight-reapply',
                                      'lid-refresh', 'lid-refresh'])
        self.assertAlmostEqual(self.now, 102.35)

    def test_lighting_error_does_not_skip_lid_reconciliation(self):
        def rpc(command, timeout):
            self.calls.append(command)
            if command == 'keylight-restore':
                raise ValueError('offline lighting failure')
            return CLOSED if self.calls == ['lid-refresh'] else OPEN
        self.assertEqual(self.run_hook('post', rpc), 0)
        self.assertEqual(self.calls, ['lid-refresh', 'keylight-restore', 'keylight-reapply',
                                      'lid-refresh'])
        self.assertTrue(any('keylight-restore failed' in item for item in self.messages))

    def test_lid_error_does_not_skip_lighting(self):
        def rpc(command, timeout):
            self.calls.append(command)
            if len(self.calls) == 1:
                raise OSError('offline lid failure')
            return OPEN if command == 'lid-refresh' else 0
        self.assertEqual(self.run_hook('post', rpc), 0)
        self.assertIn('keylight-restore', self.calls)
        self.assertIn('restore result=0', self.messages)

    def test_missing_pending_restore_is_noop(self):
        def rpc(command, timeout):
            self.calls.append(command)
            return OPEN if command == 'lid-refresh' else None
        self.assertEqual(self.run_hook('post', rpc), 0)
        self.assertEqual(self.calls.count('keylight-restore'), 1)

    def test_persistent_failures_obey_total_post_budget(self):
        def rpc(command, timeout):
            self.calls.append(command)
            self.now += timeout
            raise TimeoutError('offline timeout')
        self.assertEqual(self.run_hook('post', rpc), 0)
        self.assertLessEqual(self.now - 100, hook.POST_BUDGET + 0.00001)
        self.assertEqual(self.calls.count('keylight-restore'), 1)

    def test_matching_closed_lid_is_never_forced_open(self):
        def rpc(command, timeout):
            self.calls.append(command)
            return CLOSED if command == 'lid-refresh' else 35
        self.assertEqual(self.run_hook('post', rpc), 0)
        self.assertLess(self.now - 100, hook.POST_BUDGET)
        self.assertTrue(any('no forced-open' in item for item in self.messages))
        self.assertTrue(set(self.calls) <= hook.COMMANDS)

    def test_unknown_hook_phase_has_no_effect(self):
        def rpc(*args, **kwargs):
            raise AssertionError('No request expected')
        self.assertEqual(self.run_hook('invalid', rpc), 0)


class SleepSocketTests(unittest.TestCase):
    def test_partial_reply_is_reassembled_and_request_is_allowlisted(self):
        connection = MagicMock()
        connection.getsockopt.return_value = struct.pack('3i', 10, 0, 0)
        connection.recv.side_effect = [b'OK {"controller_open":true,', b'"cached_open":true}\n']
        with patch.object(hook.socket, 'socket') as factory:
            factory.return_value.__enter__.return_value = connection
            self.assertEqual(hook.request('lid-refresh'), OPEN)
        connection.sendall.assert_called_once_with(b'lid-refresh\n')

    def test_non_root_peer_is_rejected(self):
        connection = MagicMock()
        connection.getsockopt.return_value = struct.pack('3i', 10, 1000, 1000)
        with patch.object(hook.socket, 'socket') as factory:
            factory.return_value.__enter__.return_value = connection
            with self.assertRaises(ValueError):
                hook.request('lid-refresh')
        connection.sendall.assert_not_called()

    def test_bad_frames_and_errors_are_not_successes(self):
        for frame in (b'ERR rejected\n', b'OK 35', b'OK 35\nOK 36\n', b'OK 35\x00\n', b'x' * 4096):
            with self.subTest(frame=frame[:40]):
                connection = MagicMock()
                connection.getsockopt.return_value = struct.pack('3i', 10, 0, 0)
                connection.recv.side_effect = [frame, b'']
                with patch.object(hook.socket, 'socket') as factory:
                    factory.return_value.__enter__.return_value = connection
                    with self.assertRaises(ValueError):
                        hook.request('lid-refresh')

    def test_unapproved_command_is_rejected_without_socket(self):
        with patch.object(hook.socket, 'socket') as factory, self.assertRaises(ValueError):
            hook.request('set-duty 0')
        factory.assert_not_called()


if __name__ == '__main__':
    unittest.main()

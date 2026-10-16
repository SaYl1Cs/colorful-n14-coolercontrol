"""Offline only: fake snapshots, catalog, HTTP, safety fence and credentials."""
import copy
import json
from pathlib import Path
import stat
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import profile_selector as selector


TOKEN = dict(token='cc_' + 'a' * 32, id='12345678-1234-1234-1234-123456789abc',
             label='Colorful N14 EC profile selector', write_access=True)


class SelectorTests(unittest.TestCase):
    def setUp(self):
        from protocol_fixtures import Harness
        self.h = Harness()
        self.addCleanup(self.h.close)

    def test_mix_default_identity_function_is_valid(self):
        self.assertEqual(self.h.select(), 'mix')
        self.assertEqual(self.h.api.profiles['mix']['function_uid'], '0')

    def test_stable_no_full_or_put_for_long_running_selector(self):
        self.h.select()
        self.assertTrue(self.h.send().startswith(b'OK'))
        writes = self.h.writes.call_count
        for tick in range(400):
            self.h.advance()
            self.h.select()
            if tick % 30 == 0:
                self.assertTrue(self.h.send().startswith(b'OK'))
                writes += 3
            self.h.watchdog.tick()
        self.assertEqual(len(self.h.api.puts), 1)
        self.assertEqual(self.h.writes.call_count, writes)

    def test_restart_reselects_and_adapter_restart_rearms(self):
        import provenance_fence as fence
        self.h.select()
        self.h.selector = self.h.make_selector()
        self.h.select()
        self.assertEqual(len(self.h.api.puts), 2)
        fence.close()
        self.h.select()
        self.assertEqual(len(self.h.api.puts), 3)
        self.assertTrue(self.h.send().startswith(b'OK'))

    def test_live_curve_and_function_edits_preserved_without_config_writes(self):
        self.h.select()
        self.h.api.profiles['cpu']['speed_profile'][0][1] = 12
        self.h.api.functions['standard']['deviance'] = 3.5
        saved = copy.deepcopy(self.h.api.profiles)
        self.h.select()
        self.assertEqual(self.h.api.profiles, saved)
        self.assertEqual(len(self.h.api.puts), 2)
        self.assertEqual(self.h.api.functions['standard']['deviance'], 3.5)

    def test_invalid_source_or_mix_edit_does_not_rebind(self):
        for change in ('source', 'members', 'mix', 'missing', 'sensor', 'channel', 'function', 'poll'):
            with self.subTest(change=change):
                profiles, devices, functions = (copy.deepcopy(self.h.api.profiles),
                    copy.deepcopy(self.h.api.devices), copy.deepcopy(self.h.api.functions))
                if change == 'source':
                    self.h.api.profiles['cpu']['temp_source']['temp_name'] = 'gpu_temp'
                elif change == 'members':
                    self.h.api.profiles['mix']['member_profile_uids'] = ['cpu', 'cpu']
                elif change == 'mix':
                    self.h.api.profiles['mix']['mix_function_type'] = 'Avg'
                elif change == 'missing':
                    del self.h.api.profiles['gpu']
                elif change == 'sensor':
                    del self.h.api.devices['ec']['info']['temps']['gpu_temp']
                elif change == 'channel':
                    self.h.api.devices['ec']['info']['channels'] = {}
                elif change == 'function':
                    self.h.api.functions['standard']['f_type'] = 'Identity'
                else:
                    self.h.api.poll_rate = 2
                with self.assertRaises(selector.StagedError):
                    self.h.select()
                self.h.api.profiles, self.h.api.devices, self.h.api.functions = profiles, devices, functions
                self.h.api.poll_rate = 1
        self.assertEqual(self.h.api.puts, [])

    def test_invalid_bounds_types_and_duties(self):
        for low, high in ((True, None), (None, False), ('20', None), (30, 20), (20, 20),
                          (-1, None), (None, 121), (float('nan'), None)):
            self.h.api.profiles['cpu'].update(temp_min=low, temp_max=high)
            with self.subTest(low=low, high=high), self.assertRaises(selector.StagedError):
                self.h.select()
        self.h.api.profiles['cpu'].update(temp_min=None, temp_max=None)
        for duty in (True, 20.0, -1, 101):
            self.h.api.profiles['cpu']['speed_profile'][0][1] = duty
            with self.subTest(duty=duty), self.assertRaises(selector.StagedError):
                self.h.select()

    def test_valid_bounds_remain_live(self):
        for low, high in ((None, None), (0, 120), (0.5, 119.5), (None, 0), (120, None)):
            self.h.api.profiles['cpu'].update(temp_min=low, temp_max=high)
            self.assertEqual(self.h.select(), 'mix')

    def test_target_requires_zero_to_hundred_writable_channel(self):
        options = self.h.api.devices['ec']['info']['channels']['both_fans']['speed_options']
        for key, value in (('fixed_enabled', False), ('fixed_enabled', 1), ('min_duty', True),
                           ('max_duty', 100.0), ('min_duty', 5), ('max_duty', 99)):
            old = options[key]
            options[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(selector.StagedError):
                self.h.select()
            options[key] = old

    def test_api_failure_sanitized_full_and_recovers(self):
        self.h.select()
        self.h.api.fail = True
        with self.assertRaises(selector.StagedError) as caught:
            self.h.select()
        self.assertNotIn('SECRET', str(caught.exception))
        self.h.api.fail = False
        self.h.select()
        self.assertEqual(len(self.h.api.puts), 2)

    def test_actual_applied_assignment_is_checked_each_tick(self):
        self.h.select()
        self.h.api.applied = 'foreign-profile'
        self.h.select()
        self.assertEqual(self.h.api.applied, 'mix')
        self.assertEqual(len(self.h.api.puts), 2)

    def test_catalog_edit_during_put_cannot_install(self):
        import provenance_fence as fence
        self.h.api.on_put = lambda: self.h.api.profiles['cpu']['speed_profile'][0].__setitem__(1, 5)
        with self.assertRaises(selector.StagedError):
            self.h.select()
        self.assertEqual(fence.status()['phase'], 'closed')

    def test_failed_handover_confirmation_prevents_put(self):
        self.h.rpc.handover = Mock(return_value={'full_confirmed': False})
        with self.assertRaises(selector.StagedError):
            self.h.select()
        self.assertEqual(self.h.api.puts, [])


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.token = patch.object(selector, 'read_token', return_value=TOKEN['token'])
        self.reader = self.token.start()
        self.addCleanup(self.token.stop)
        self.http = patch.object(selector.http.client, 'HTTPConnection')
        self.factory = self.http.start()
        self.addCleanup(self.http.stop)
        self.conn = self.factory.return_value
        self.response = self.conn.getresponse.return_value
        self.response.status = 200
        self.response.getheader.return_value = None
        self.response.read.return_value = b''
        self.timer_patch = patch.object(selector.threading, 'Timer')
        self.timer = self.timer_patch.start()
        self.addCleanup(self.timer_patch.stop)
        self.obj = selector.LocalTransport('/fake/private/token')

    def test_lazy_bearer_fixed_destination_no_proxy(self):
        self.reader.assert_not_called()
        with patch.dict('os.environ', {'http_proxy': 'http://evil', 'HTTP_PROXY': 'http://evil'}):
            self.obj.request('PUT', '/devices/ec/settings/fan1/profile', {'profile_uid': 'cpu'})
        self.factory.assert_called_once_with('127.0.0.1', 11987, timeout=3.0)
        args, kw = self.conn.request.call_args
        self.assertEqual(args, ('PUT', '/devices/ec/settings/fan1/profile'))
        self.assertEqual(json.loads(kw['body']), {'profile_uid': 'cpu'})
        self.assertEqual(kw['headers']['Authorization'], 'Bearer ' + TOKEN['token'])
        self.response.read.assert_called_once_with(262145)
        self.timer.return_value.start.assert_called_once()
        self.timer.return_value.cancel.assert_called_once()
        self.conn.close.assert_called_once()

    def test_redirect_errors_oversize_and_bad_json_sanitized(self):
        for status, body in [(301, b'SECRET'), (302, b''), (307, b''), (308, b''),
                             (401, b'SECRET'), (500, b''), (200, b'x' * 262145),
                             (200, b'SECRET')]:
            with self.subTest(status=status, size=len(body)):
                self.response.status, self.response.read.return_value = status, body
                with self.assertRaises(selector.StagedError) as caught:
                    self.obj.request('GET', '/profiles')
                self.assertEqual(str(caught.exception), 'local API request failed')

    def test_network_timeout_and_timer_shutdown(self):
        self.conn.getresponse.side_effect = TimeoutError('SECRET')
        with self.assertRaises(selector.StagedError):
            self.obj.request('GET', '/profiles')
        self.timer.call_args.args[1]()
        self.conn.sock.shutdown.assert_called_once()

    def test_canonical_assignment_path(self):
        for segment in ('..', '.', '%2E%2E', '%2F', '%252F', '%', '%41', 'a%3ab'):
            for path in (f'/devices/{segment}/settings/fan1/profile',
                         f'/devices/ec/settings/{segment}/profile'):
                with self.subTest(path=path), self.assertRaises(selector.StagedError):
                    self.obj.request('PUT', path, {'profile_uid': 'cpu'})
        self.reader.assert_not_called()
        self.obj.request('PUT', '/devices/ec%3A1/settings/fan1/profile', {'profile_uid': 'cpu'})
        self.reader.assert_called_once()

    def test_transport_limits_and_encoded_body(self):
        for timeout in (0, -1, True, float('nan'), float('inf'), 8.1):
            with self.subTest(timeout=timeout), self.assertRaises(selector.StagedError):
                selector.LocalTransport('/fake/token', timeout=timeout)
        for limit in (0, True, 1.5, 1048577):
            with self.subTest(limit=limit), self.assertRaises(selector.StagedError):
                selector.LocalTransport('/fake/token', response_limit=limit)
        self.response.getheader.return_value = 'gzip'
        with self.assertRaises(selector.StagedError):
            self.obj.request('GET', '/profiles')
        self.response.read.assert_not_called()

    def test_disallowed_routes_before_credential_read(self):
        for method, path in [('POST', '/profiles'), ('PUT', '/profiles/cpu'),
                             ('GET', 'http://evil'), ('GET', '//evil')]:
            with self.assertRaises(selector.StagedError):
                self.obj.request(method, path)
        self.reader.assert_not_called()


class TokenTests(unittest.TestCase):
    def test_descriptor_security_without_reading_any_real_token(self):
        directory = SimpleNamespace(st_uid=0, st_mode=stat.S_IFDIR | 0o700)
        regular = SimpleNamespace(st_uid=0, st_mode=stat.S_IFREG | 0o600, st_nlink=1)
        for bad in (None, 'owner', 'mode', 'parent', 'symlink', 'hardlink', 'nonroot'):
            d, f = copy.copy(directory), copy.copy(regular)
            if bad == 'owner':
                f.st_uid = 1000
            elif bad == 'mode':
                f.st_mode = stat.S_IFREG | 0o644
            elif bad == 'parent':
                d.st_mode = stat.S_IFDIR | 0o755
            elif bad == 'symlink':
                f.st_mode = stat.S_IFLNK | 0o600
            elif bad == 'hardlink':
                f.st_nlink = 2
            with self.subTest(bad=bad), \
                    patch.object(selector.os, 'geteuid', return_value=1000 if bad == 'nonroot' else 0), \
                    patch.object(selector.os, 'open', side_effect=[10, 11, 12]) as op, \
                    patch.object(selector.os, 'close'), \
                    patch.object(selector.os, 'fstat', side_effect=[d, f]), \
                    patch.object(selector.os, 'read', return_value=json.dumps(TOKEN).encode()) as read:
                if bad:
                    with self.assertRaises(selector.StagedError):
                        selector.read_token('/private/token')
                    read.assert_not_called()
                else:
                    self.assertEqual(selector.read_token('/private/token'), TOKEN['token'])
                    for call in op.call_args_list:
                        self.assertTrue(call.args[1] & selector.os.O_NOFOLLOW)
                    self.assertEqual(op.call_args.kwargs['dir_fd'], 11)

    def test_symlink_open_failure_sanitized(self):
        with patch.object(selector.os, 'geteuid', return_value=0), \
                patch.object(selector.os, 'open', side_effect=OSError('SECRET')):
            with self.assertRaisesRegex(selector.StagedError, '^credential unavailable or unsafe$'):
                selector.read_token('/symlink/private/token')


if __name__ == '__main__':
    unittest.main()

"""Offline authorization tests: no real API, GUI, privilege or hardware access."""
import http.cookiejar
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

import authorize_coolercontrol as auth

TOKEN = {'id': '12345678-1234-1234-1234-123456789abc',
         'token': 'cc_' + 'a' * 32, 'label': auth.LABEL, 'write_access': True}


class FakeSession:
    def __init__(self, failure=None):
        self.calls = []
        self.failure = failure
        self.cookies = http.cookiejar.CookieJar()

    def request(self, method, path, body=None, authorization=None):
        self.calls.append((method, path, body, authorization))
        if path == self.failure:
            raise OSError('simulated transport failure')
        if path == '/tokens':
            return dict(TOKEN)


class AuthorizationTests(unittest.TestCase):
    def test_success_logs_out_and_stores_only_token(self):
        session = FakeSession()
        stored = []
        auth.enroll('test-only-password', session, stored.append)
        self.assertEqual(stored, [TOKEN])
        self.assertEqual([c[1] for c in session.calls], ['/login', '/tokens', '/logout'])
        self.assertIsNone(session.calls[1][3])
        self.assertIsNone(session.calls[2][3])
        self.assertNotIn('password', str(stored))

    def test_login_failure_is_not_retried(self):
        session = FakeSession('/login')
        with self.assertRaises(OSError):
            auth.enroll('test', session, lambda _: self.fail('must not store'))
        self.assertEqual(len(session.calls), 1)

    def test_create_failure_logs_out(self):
        session = FakeSession('/tokens')
        with self.assertRaises(OSError):
            auth.enroll('test', session, lambda _: self.fail('must not store'))
        self.assertEqual(session.calls[-1][1], '/logout')

    def test_storage_failure_revokes_new_token(self):
        session = FakeSession()
        def fail(_):
            raise OSError('cancelled')
        with self.assertRaises(OSError):
            auth.enroll('test', session, fail)
        self.assertEqual(session.calls[-2][:2], ('DELETE', '/tokens/' + TOKEN['id']))
        self.assertEqual(session.calls[-1][1], '/logout')

    def test_redirect_is_refused(self):
        with self.assertRaises(ValueError):
            auth.NoRedirect().redirect_request(None, None, 302, '', {}, 'http://example.org')

    def test_invalid_tokens_rejected(self):
        for key, value in [('id', '../escape'), ('token', 'secret\n'),
                           ('write_access', False), ('label', 'other')]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                auth.validate_token(dict(TOKEN, **{key: value}))

    def test_storage_permissions_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / 'private'
            target = directory / 'token.json'
            with patch.object(auth, 'DEST', directory), patch.object(auth, 'TOKEN_FILE', target), \
                    patch.object(auth.os, 'geteuid', return_value=0):
                # lstat ownership is supplied separately because tests run unprivileged.
                original = Path.lstat
                def root_info(path):
                    info = original(path)
                    if path == directory:
                        from types import SimpleNamespace
                        return SimpleNamespace(st_mode=info.st_mode, st_uid=0)
                    return info
                with patch.object(Path, 'lstat', root_info):
                    auth.store(TOKEN)
                    self.assertEqual(target.stat().st_mode & 0o777, 0o600)
                    with self.assertRaises(FileExistsError):
                        auth.store(TOKEN)
                    target.unlink()
                    target.symlink_to(Path(tmp) / 'outside')
                    with self.assertRaises(FileExistsError):
                        auth.store(TOKEN)
                    self.assertFalse((Path(tmp) / 'outside').exists())

    def test_non_root_storage_rejected(self):
        with patch.object(auth.os, 'geteuid', return_value=1000), self.assertRaises(ValueError):
            auth.store(TOKEN)


if __name__ == '__main__':
    unittest.main()

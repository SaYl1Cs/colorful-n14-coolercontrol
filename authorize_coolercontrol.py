#!/usr/bin/env python3
"""Explicit local CoolerControl login; never log or persist the password.

Run as the desktop user. Only the --store subprocess runs through Polkit.
No hardware access and no changes to CoolerControl authentication settings.
"""
import base64
import http.cookiejar
import json
import os
from pathlib import Path
import re
import resource
import stat
import subprocess
import sys
import urllib.error
import urllib.request

BASE = 'http://127.0.0.1:11987'
DEST = Path('/etc/colorful-n14-control')
TOKEN_FILE = DEST / 'coolercontrol-token.json'
LABEL = 'Colorful N14 EC profile selector'


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('API redirect refused')


class Session:
    def __init__(self):
        self.cookies = http.cookiejar.CookieJar()
        self.http = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirect(),
            urllib.request.HTTPCookieProcessor(self.cookies))

    def request(self, method, path, body=None, authorization=None):
        headers = {'Content-Type': 'application/json'}
        if authorization is not None:
            headers['Authorization'] = authorization
        req = urllib.request.Request(BASE + path,
            data=None if body is None else json.dumps(body).encode(),
            headers=headers, method=method)
        with self.http.open(req, timeout=8) as response:
            if response.status != 200:
                raise ValueError('Unexpected API status')
            data = response.read(65537)
            if len(data) > 65536:
                raise ValueError('API response too large')
            return json.loads(data) if data else None


def validate_token(data):
    if (not isinstance(data, dict)
            or not isinstance(data.get('token'), str)
            or re.fullmatch(r'cc_[0-9a-f]{32}', data['token']) is None
            or not isinstance(data.get('id'), str)
            or re.fullmatch(r'[0-9a-fA-F-]{36}', data['id']) is None
            or data.get('label') != LABEL
            or data.get('write_access') is not True):
        raise ValueError('Invalid token response')
    return {key: data[key] for key in ('id', 'label', 'token', 'write_access')}


def store(data):
    if os.geteuid() != 0:
        raise ValueError('Root required for token storage')
    payload = validate_token(data)
    DEST.mkdir(mode=0o700, exist_ok=True)
    info = DEST.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise ValueError('Unsafe token directory')
    # O_EXCL also refuses symlinks and prevents replacing an existing credential.
    fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'w', encoding='ascii') as stream:
            json.dump(payload, stream)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        TOKEN_FILE.unlink(missing_ok=True)
        raise


def enroll(password, session=None, persist=None):
    session = session or Session()
    token = None
    saved = False
    logged_in = False
    try:
        auth = 'Basic ' + base64.b64encode(('CCAdmin:' + password).encode()).decode('ascii')
        try:
            session.request('POST', '/login', authorization=auth)
            logged_in = True
        finally:
            # Python cannot guarantee memory zeroization; no secret reaches disk,
            # argv, environment, terminal output or exception diagnostics here.
            password = auth = None
        token = validate_token(session.request('POST', '/tokens', {
            'label': LABEL, 'expires_at': None, 'write_access': True}))
        if persist is None:
            result = subprocess.run(
                ['pkexec', '/usr/bin/python3', '-B', str(Path(__file__).resolve()), '--store'],
                input=json.dumps(token).encode(), stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, check=False)
            if result.returncode != 0:
                raise ValueError('Polkit storage failed')
        else:
            persist(token)
        saved = True
    finally:
        if logged_in:
            if token is not None and not saved:
                try:
                    session.request('DELETE', '/tokens/' + token['id'])
                except Exception:
                    print('Не удалось отозвать несохранённый токен. Удалите токен «'
                          + LABEL + '» в интерфейсе CoolerControl.', file=sys.stderr)
            try:
                session.request('POST', '/logout')
            except Exception:
                print('Не удалось завершить временную API-сессию; пароль не сохранён.',
                      file=sys.stderr)
            session.cookies.clear()


def main():
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    os.umask(0o077)
    if sys.argv[1:] == ['--store']:
        try:
            raw = sys.stdin.buffer.read(8193)
            if len(raw) > 8192:
                return 1
            store(json.loads(raw))
            return 0
        except Exception:
            return 1  # Never include credential-bearing exceptions in output.
    if sys.argv[1:] or os.geteuid() == 0:
        print('Run without arguments as the desktop user.', file=sys.stderr)
        return 1
    prompt = subprocess.run([
        'zenity', '--password', '--title=CoolerControl — авторизация EC-переключателя',
        '--text=Введите пароль CoolerControl (не пароль Linux).\n'
                 'Пароль не сохраняется. Будет создан отдельный API-токен с правом записи.\n'
                 'Затем Polkit запросит разрешение сохранить токен только для root.'
    ], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
    if prompt.returncode != 0:
        print('Авторизация отменена; изменений нет.')
        return 1
    try:
        password = prompt.stdout.decode().removesuffix('\n')
        prompt = None
        enroll(password)
        print('Отдельный API-токен сохранён только для root. Пароль не сохранён. '
              'Охлаждение и настройки авторизации не изменены.')
        return 0
    except Exception:
        print('Локальная авторизация или сохранение не выполнены. '
              'Секреты не выводятся; автоматического повторного входа нет.', file=sys.stderr)
        return 1
    finally:
        password = None


if __name__ == '__main__':
    raise SystemExit(main())

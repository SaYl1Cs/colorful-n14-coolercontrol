"""Serial, root-only adapter RPC. No hardware work until serve() is called."""
from contextlib import redirect_stdout, redirect_stderr
import io
import os
import re
import signal
import socket
import stat
import struct
import sys
import time

import coolercontrol_adapter as adapter
from prepare import session_lock
from thermal_state import RUNTIME_DIR, read_snapshot, notify_ready

SOCKET = RUNTIME_DIR / 'adapter.sock'
COMMAND = re.compile(r'(?:get-rpm|get-duty|temp-cpu|temp-gpu|enable|reset|self-check|set-duty (?:100|[0-9]{1,2}))')


def dispatch(request):
    if COMMAND.fullmatch(request) is None:
        return b'ERR invalid command\n'
    output, errors = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(output), redirect_stderr(errors):
            adapter.main(['adapter', *request.split(' ')])
        text = output.getvalue().strip() or 'ok'
        return f'OK {text}\n'.encode('ascii')
    except (SystemExit, OSError, ValueError, TypeError) as exc:
        text = errors.getvalue().strip() or str(exc) or 'adapter failed'
        return ('ERR ' + text.replace('\n', ' ')[:512] + '\n').encode('ascii', 'replace')


class ThermalWatchdog:
    def __init__(self):
        self.next_check = 0.0
        self.next_full = 0.0
        self.failed = False

    def tick(self):
        now = time.monotonic()
        if now < self.next_check:
            return
        self.next_check = now + 1
        try:
            data = read_snapshot()
            if data['cpu_temp'] >= 90000 or data['gpu_input'] >= 80000:
                raise ValueError('critical temperature')
            self.failed = False
        except (OSError, ValueError, TypeError) as exc:
            if not self.failed:
                print(f'Fan watchdog: {exc}; requesting both fans full', file=sys.stderr, flush=True)
                self.failed = True
            if now >= self.next_full:
                self.next_full = now + 5
                result = dispatch('reset')
                if result.startswith(b'ERR'):
                    print(result.decode().strip(), file=sys.stderr, flush=True)


def serve():
    # Lock is shared with the current NBFC launcher: one hardware owner.
    with session_lock(), socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        adapter.guarded(True)
        if SOCKET.exists() or SOCKET.is_symlink():
            info = SOCKET.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != 0:
                raise RuntimeError('refusing to remove untrusted adapter socket')
            SOCKET.unlink()
        listener.bind(str(SOCKET))
        os.chmod(SOCKET, 0o600)
        listener.listen(16)
        listener.settimeout(0.5)
        stopping = False

        def stop(*_):
            nonlocal stopping
            stopping = True

        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, stop)
        watchdog = ThermalWatchdog()
        notify_ready()
        try:
            while not stopping:
                watchdog.tick()
                try:
                    conn, _ = listener.accept()
                except socket.timeout:
                    continue
                with conn:
                    conn.settimeout(1)
                    try:
                        _, uid, _ = struct.unpack('3i', conn.getsockopt(
                            socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i')))
                        if uid != 0:
                            conn.sendall(b'ERR root peer required\n')
                            continue
                        data = bytearray()
                        while len(data) <= 64 and b'\n' not in data:
                            part = conn.recv(65 - len(data))
                            if not part:
                                break
                            data.extend(part)
                        if not data.endswith(b'\n') or len(data) > 64:
                            conn.sendall(b'ERR invalid request framing\n')
                            continue
                        conn.sendall(dispatch(data[:-1].decode('ascii')))
                    except (OSError, UnicodeError):
                        # Broken clients must not terminate the control service.
                        continue
        finally:
            result = dispatch('reset')  # Best effort full, never an unverified auto mode.
            if result.startswith(b'ERR'):
                print(result.decode().strip(), file=sys.stderr, flush=True)
            SOCKET.unlink(missing_ok=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(serve())

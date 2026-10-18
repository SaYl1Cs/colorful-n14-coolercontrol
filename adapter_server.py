"""Root-only bounded Unix RPC, serialized fan writes, one-second watchdog.

Slow socket clients are nonblocking and expire independently; they cannot stall
watchdog scheduling. Blocking firmware/sysfs I/O is still not hard real-time.
"""
from contextlib import redirect_stdout, redirect_stderr
import io
import json
import os
import re
import selectors
import signal
import socket
import stat
import struct
import sys
import subprocess
import time

import coolercontrol_adapter as adapter
from prepare import session_lock
import thermal_state
from thermal_state import RUNTIME_DIR, read_snapshot, notify_ready
import provenance_fence as fence

SOCKET = RUNTIME_DIR / 'adapter.sock'
MAX_PROVENANCE = 65536
MAX_REQUEST = MAX_PROVENANCE + 128
MAX_REPLY = MAX_PROVENANCE + 1024
READS = {'get-rpm', 'get-duty', 'temp-cpu', 'temp-gpu', 'temp-cpu-json', 'temp-gpu-json',
         'temp-cpu/json', 'temp-gpu/json', 'self-check', 'enable', 'keylight-get'}
DUTY = re.compile(r'(?:100|[0-9]{1,2})\Z')
KEYLIGHT = re.compile(r'(?:100|[0-9]{1,2})\Z')


_STOPPING_UNTIL = 0.0


def shutdown_confirmed():
    """At reset/finalization only; <=1s total, no shell or per-loop subprocess.

    Cache positive evidence for at most 10s, never a generic SIGTERM or caller
    flag. A failed/ambiguous systemd query retains manual-stop/full semantics.
    """
    global _STOPPING_UNTIL
    now = thermal_state.boottime()
    if now < _STOPPING_UNTIL:
        return True
    deadline = time.monotonic() + 1.0
    env = {'PATH': '/usr/bin:/bin', 'LC_ALL': 'C', 'SYSTEMD_COLORS': '0'}
    try:
        def query(action):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            return subprocess.run(['/usr/bin/systemctl', '--system', '--no-pager', '--plain', action],
                                  stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, timeout=remaining, env=env, check=False)
        result = query('is-system-running')
        confirmed = result.returncode in (0, 1) and result.stdout.strip() == b'stopping'
        if not confirmed:
            result = query('list-jobs')
            targets = {b'shutdown.target', b'reboot.target', b'poweroff.target', b'halt.target'}
            confirmed = result.returncode == 0 and any(
                len(parts) == 4 and parts[0].isdigit() and parts[1] in targets and
                parts[2] == b'start' and parts[3] in (b'waiting', b'running')
                for parts in (line.split() for line in result.stdout.splitlines()))
        if confirmed:
            _STOPPING_UNTIL = now + fence.MAX_HANDOVER
        return confirmed
    except (OSError, subprocess.TimeoutExpired, TimeoutError):
        return False


def _full_hand_over():
    adapter.forget_safe()
    try:
        fence.close()
    finally:
        # Still attempt full on a state-storage failure; never report a
        # successful hand-over when closing or persisting confirmation failed.
        adapter.fixed_full()
    return fence.mark_full()


def _bounded_hand_over(live_policy=None, *, startup=False, shutdown=False):
    try:
        return adapter.quiet_hand_over(live_policy, startup=startup, shutdown=shutdown)
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, SystemExit):
        return _full_hand_over()


def _lifecycle_stop():
    if shutdown_confirmed():
        return _bounded_hand_over(shutdown=True)
    return _full_hand_over()


def _sensor_check():
    # Never trust a selector's claim of sensor loss as authority to bypass full.
    try:
        data = adapter.current_control_snapshot()
    except thermal_state.SensorUnavailable:
        try:
            previous = adapter.sensor_grace()
            return dict(fence.status(), sensor_grace_until=adapter.sensor_grace_deadline(previous))
        except (OSError, ValueError, TypeError, KeyError, RuntimeError, SystemExit):
            return _full_hand_over()
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, SystemExit):
        return _full_hand_over()
    # A recovery raced the selector: let the next iteration assign, but do not
    # prolong an existing handover or rescue an expired/unknown state.
    return _bounded_hand_over()


def _rpc(request):
    action, sep, payload = request.partition(' ')
    if action == 'fence-close-full' and not sep:
        return _full_hand_over()
    if action in ('reset', 'lifecycle-stop') and not sep:
        return _lifecycle_stop()
    if action == 'sensor-check' and not sep:
        return _sensor_check()
    if action == 'fence-handover' and sep:
        value = thermal_state.strict_json(payload)
        fence.keys(value, ('policy',))
        return _bounded_hand_over(value['policy'])
    if action == 'fence-close' and not sep:
        return fence.close()
    if action in ('fence-status', 'assignment-status') and not sep:
        return fence.status()
    if action in ('install-assignment', 'renew-assignment') and sep:
        if len(payload.encode()) > MAX_PROVENANCE:
            raise ValueError('RPC payload too large')
        value = thermal_state.strict_json(payload)
        current = adapter.current_control_snapshot()
        if action == 'install-assignment':
            fence.keys(value, ('assignment', 'policy', 'epoch', 'close_id'))
            fence.install_assignment(value['assignment'], value['policy'], value['epoch'],
                                     value['close_id'], current)
            if 'hold_until' in fence.status():
                adapter.raise_transition_floor()
            return fence.status()
        fence.keys(value, ('assignment', 'policy_digest', 'epoch'))
        return fence.renew(value['assignment'], value['policy_digest'], value['epoch'], current)
    if action == 'set-duty-provenance' and sep:
        duty, sep, label = payload.partition(' ')
        if not sep or DUTY.fullmatch(duty) is None or len(label.encode()) > MAX_PROVENANCE:
            raise ValueError('invalid provenance arguments')
        adapter.main(['adapter', action, duty, label])
        return None
    if action == 'set-duty' and payload == '100':
        adapter.fixed_full()
        return None
    if action == 'keylight-set' and sep and KEYLIGHT.fullmatch(payload):
        return adapter.set_key_brightness(int(payload))
    if action == 'keylight-save' and not sep:
        return adapter.save_key_brightness()
    if action == 'keylight-restore' and not sep:
        return adapter.restore_key_brightness()
    if action == 'keylight-reapply' and not sep:
        return adapter.reapply_key_brightness()
    if action == 'lid-refresh' and not sep:
        return adapter.refresh_lid()
    if action == 'keylight-get' and not sep:
        return adapter.read_key_brightness()
    if action in READS and not sep:
        adapter.main(['adapter', action])
        return None
    raise ValueError('invalid command')


def dispatch(request):
    output, errors = io.StringIO(), io.StringIO()
    try:
        if (type(request) is not str or len(request.encode()) > MAX_REQUEST - 1
                or any(c in request for c in '\r\n\x00')):
            raise ValueError('invalid framing')
        with redirect_stdout(output), redirect_stderr(errors):
            result = _rpc(request)
        text = json.dumps(result, separators=(',', ':'), allow_nan=False) if result is not None else output.getvalue().strip() or 'ok'
        reply = ('OK ' + text + '\n').encode('ascii')
        if len(reply) > MAX_REPLY:
            raise ValueError('oversize reply')
        return reply
    except adapter.GpuAsleep:
        if request in ('temp-gpu', 'temp-gpu-json', 'temp-gpu/json'):
            # Non-numeric, unsuccessful sensor response: the plugin omits the
            # sleeping GPU. This is expected, not a fan-write failure.
            return b'ERR GPU_ASLEEP: D3cold; temperature unavailable\n'
        # This status must never exempt a control request from protection.
        try:
            _full_hand_over()
        except (OSError, ValueError, RuntimeError, SystemExit):
            pass
        print('Adapter request rejected: unexpected GPU-asleep status', file=sys.stderr, flush=True)
        return b'ERR adapter request rejected\n'
    except adapter.DutyHeld:
        return b'ERR sensor unavailable; bounded last-duty hold\n'
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, SystemExit, RecursionError) as exc:
        if type(request) is str and request.split(' ', 1)[0] in (
                'set-duty-provenance', 'set-duty', 'install-assignment', 'renew-assignment', 'fence-handover'):
            try:
                _full_hand_over()
            except (OSError, ValueError, TypeError, KeyError, RuntimeError, SystemExit):
                pass
        # Log only an allowlisted action and source-owned reason; never payloads.
        action = request.partition(' ')[0] if type(request) is str else 'invalid'
        actions = READS | {'reset', 'lifecycle-stop', 'sensor-check', 'set-duty',
                           'set-duty-provenance', 'install-assignment', 'renew-assignment',
                           'fence-handover', 'fence-close-full', 'fence-close',
                           'fence-status', 'assignment-status', 'keylight-set',
                           'keylight-save', 'keylight-restore', 'keylight-reapply',
                           'lid-refresh'}
        action = action if action in actions else 'invalid'
        print('Adapter request rejected [' + action + ']: ' + fence.rejection_reason(exc),
              file=sys.stderr, flush=True)
        return b'ERR adapter request rejected\n'


class ThermalWatchdog:
    def __init__(self, clock=None):
        self.clock = thermal_state.boottime if clock is None else clock
        self.next_check = 0.0
        self.failed = False
        self.failure_reason = None

    def tick(self):
        now = self.clock()
        if now < self.next_check:
            return
        self.next_check = now + 1.0
        try:
            try:
                data = read_snapshot(require_metadata=True, require_samples=True)
            except thermal_state.SensorUnavailable:
                adapter.sensor_grace()
                return  # No writes, no renewed lease, no refreshed sample timestamp.
            if adapter.read_live_power_state() != (data['gpu_state'], data['gpu_power_state']):
                raise ValueError('current power mode mismatch')
            if thermal_state.critical_temperature(data):
                raise ValueError('critical temperature')
            adapter.check_applied_duty()
            status = fence.status()
            if status['phase'] != 'closed':
                if status['epoch'] != thermal_state.epoch(data):
                    adapter.quiet_hand_over()  # Valid fresh mode change, not a sensor failure.
                elif fence.transition_raise_duty(data, now=now) is not None:
                    adapter.raise_transition_floor()
                adapter.remember_safe(data)
            elif 'hold_until' in status:
                fence.check_hold(status, now)
                if status['hold_epoch'] != thermal_state.epoch(data):
                    adapter.quiet_hand_over(shutdown=status['hold_kind'] == 'shutdown')
                else:
                    adapter.raise_hold_floor(data)
                adapter.remember_safe(data)
            elif status.get('full_confirmed') is not True:
                raise ValueError('fence is closed without full duty')
            self.failed = False
            self.failure_reason = None
        except (OSError, ValueError, TypeError, KeyError, RuntimeError, SystemExit) as exc:
            reason = fence.rejection_reason(exc)
            if not self.failed or reason != self.failure_reason:
                print('Fan watchdog: ' + reason + '; requesting full duty', file=sys.stderr, flush=True)
            self.failed = True
            self.failure_reason = reason
            # Emergency full must never use the planned-shutdown exception.
            # Repeat failed writes once a second; stale callbacks stay fenced.
            if dispatch('fence-close-full').startswith(b'ERR'):
                print('Fan watchdog: full-duty request failed', file=sys.stderr, flush=True)


class SocketLoop:
    """Bounded nonblocking clients; usable with temp sockets in offline tests."""
    def __init__(self, listener, watchdog, peer_uid=0):
        self.listener, self.watchdog, self.peer_uid = listener, watchdog, peer_uid
        self.selector = selectors.DefaultSelector()
        listener.setblocking(False)
        self.selector.register(listener, selectors.EVENT_READ, None)
        self.clients = {}

    def drop(self, conn):
        self.selector.unregister(conn)
        self.clients.pop(conn, None)
        conn.close()

    def step(self, timeout=0.1):
        self.watchdog.tick()
        now = time.monotonic()
        for conn, data in list(self.clients.items()):
            if now >= data['deadline']:
                self.drop(conn)
        for key, mask in self.selector.select(timeout):
            conn = key.fileobj
            if conn is self.listener:
                client, _ = conn.accept()
                client.setblocking(False)
                _, uid, _ = struct.unpack('3i', client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                if uid != self.peer_uid or len(self.clients) >= 32:
                    client.close()
                    continue
                data = {'request': bytearray(), 'reply': b'', 'deadline': time.monotonic() + 1}
                self.clients[client] = data
                self.selector.register(client, selectors.EVENT_READ, data)
                continue
            data = key.data
            try:
                if mask & selectors.EVENT_WRITE:
                    count = conn.send(data['reply'])
                    data['reply'] = data['reply'][count:]
                    if not data['reply']:
                        self.drop(conn)
                    continue
                chunk = conn.recv(min(8192, MAX_REQUEST + 1 - len(data['request'])))
                if not chunk:
                    self.drop(conn)
                    continue
                data['request'].extend(chunk)
                raw = data['request']
                if len(raw) > MAX_REQUEST or b'\0' in raw or b'\r' in raw:
                    data['reply'] = b'ERR invalid framing\n'
                elif b'\n' in raw:
                    if raw.count(b'\n') != 1 or not raw.endswith(b'\n'):
                        data['reply'] = b'ERR invalid framing\n'
                    else:
                        self.watchdog.tick()  # freshness is also checked on each lower write
                        data['reply'] = dispatch(raw[:-1].decode('utf-8'))
                if data['reply']:
                    self.selector.modify(conn, selectors.EVENT_WRITE, data)
            except BlockingIOError:
                continue
            except (OSError, UnicodeError, ValueError):
                self.drop(conn)

    def close(self):
        for conn in list(self.clients):
            self.drop(conn)
        self.selector.close()


def serve():
    if os.geteuid() != 0:
        raise RuntimeError('root required')
    # Acquire sole ownership BEFORE invalidating state or touching hardware.
    with session_lock(), socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        adapter.guarded(True)
        loop = None
        bound = orderly = False
        stopping = False
        def stop(*_):
            nonlocal stopping
            stopping = True
        # Install handlers before any quiet hold; a manual SIGTERM during socket
        # setup must still reach full-duty cleanup rather than leave fixed fans.
        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, stop)
        try:
            _bounded_hand_over(startup=True)
            if SOCKET.exists() or SOCKET.is_symlink():
                info = SOCKET.lstat()
                if not stat.S_ISSOCK(info.st_mode) or info.st_uid != 0:
                    raise RuntimeError('untrusted adapter socket')
                SOCKET.unlink()
            listener.bind(str(SOCKET))
            bound = True
            os.chmod(SOCKET, 0o600)
            listener.listen(16)
            loop = SocketLoop(listener, ThermalWatchdog())
            notify_ready()
            while not stopping:
                loop.step()
            orderly = True
        finally:
            try:
                if loop is not None:
                    loop.close()
            except BaseException:
                orderly = False
                raise
            finally:
                # Setup failures and crashes always full, even during shutdown.
                if dispatch('lifecycle-stop' if orderly else 'fence-close-full').startswith(b'ERR'):
                    print('Adapter shutdown: full-duty request failed', file=sys.stderr, flush=True)
                if bound:
                    SOCKET.unlink(missing_ok=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(serve())

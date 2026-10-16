#!/usr/bin/env python3
"""Authenticated selector daemon; sampling remains in the EC/sysfs sampler."""
import fcntl
import json
import os
from pathlib import Path
import resource
import signal
import socket
import stat
import struct
import sys
import threading
import time

from authorize_coolercontrol import TOKEN_FILE
from profile_selector import LocalTransport, ProfileSelector, StagedError
import control_policy
from thermal_state import SNAPSHOT, RUNTIME_DIR, strict_json

RUNTIME_SOCKET = RUNTIME_DIR / 'adapter.sock'
BINDINGS_PATH = Path('/etc/colorful-n14-control/profile-bindings.json')
POLL_SECONDS = 1.0
MAX_RPC_REPLY = 66560


def read_bindings(path):
    """No symlink in any component; root ownership and no writable ancestors."""
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('unsafe bindings path')
    directory = fd = None
    try:
        directory = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        for part in path.parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
            info = os.fstat(directory)
            if info.st_uid != 0 or info.st_mode & 0o022:
                raise ValueError('unsafe bindings parent')
        info = os.fstat(directory)
        if stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError('bindings parent must be private')
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        info = os.fstat(fd)
        if (info.st_uid != 0 or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600):
            raise ValueError('unsafe bindings file')
        raw = os.read(fd, 16385)
        if len(raw) > 16384:
            raise ValueError('oversize bindings')
        return control_policy.validate_bindings(strict_json(raw))
    finally:
        if fd is not None:
            os.close(fd)
        if directory is not None:
            os.close(directory)


class AdapterFenceRPC:
    def __init__(self, path=RUNTIME_SOCKET, timeout=2.0):
        self.path, self.timeout = Path(path), timeout

    def request(self, command, value=None):
        if value is not None:
            command += ' ' + json.dumps(value, separators=(',', ':'), allow_nan=False)
        encoded = command.encode('utf-8')
        if any(c in encoded for c in (b'\n', b'\r', b'\0')) or len(encoded) > 65663:
            raise StagedError('adapter RPC request rejected')
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(self.timeout)
            conn.connect(str(self.path))
            _, uid, _ = struct.unpack('3i', conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid != 0:
                raise StagedError('adapter peer is not root')
            conn.sendall(encoded + b'\n')
            result = bytearray()
            deadline = time.monotonic() + self.timeout
            while len(result) <= MAX_RPC_REPLY:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise StagedError('adapter RPC timeout')
                conn.settimeout(remaining)
                part = conn.recv(min(4096, MAX_RPC_REPLY + 1 - len(result)))
                if not part:
                    break
                result.extend(part)
                if b'\n' in part:
                    break
        if (not result.endswith(b'\n') or result.count(b'\n') != 1 or len(result) > MAX_RPC_REPLY
                or not result.startswith(b'OK ')):
            raise StagedError('adapter RPC failed')
        return strict_json(result[3:-1])

    def status(self):
        return self.request('assignment-status')

    def close_and_full(self):
        return self.request('fence-close-full')

    def handover(self, policy):
        return self.request('fence-handover', {'policy': policy})

    def sensor_check(self):
        return self.request('sensor-check')

    def lifecycle_stop(self):
        return self.request('lifecycle-stop')

    def install_assignment(self, response, policy, epoch, close_id):
        return self.request('install-assignment', dict(assignment=response, policy=policy,
                                                       epoch=epoch, close_id=close_id))

    def renew(self, response, digest, epoch):
        return self.request('renew-assignment', dict(assignment=response, policy_digest=digest, epoch=epoch))


def serve():
    if os.geteuid() != 0:
        raise RuntimeError('selector requires root')
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    os.umask(0o077)
    rpc = AdapterFenceRPC()
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    fd = os.open(RUNTIME_DIR / 'selector.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    owns_lock = False
    orderly = False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        owns_lock = True
        selector = ProfileSelector(LocalTransport(TOKEN_FILE), read_bindings(BINDINGS_PATH), rpc,
                                   SNAPSHOT, stopping=stop.is_set)
        failed = False
        while not stop.is_set():
            try:
                selector.select()
                if failed:
                    print('Selector recovered; awaiting/using validated producer commands', flush=True)
                failed = False
            except StagedError:
                if not failed:
                    print('Selector unavailable; adapter safety handling requested', file=sys.stderr, flush=True)
                failed = True
            stop.wait(POLL_SECONDS)
        orderly = True
    finally:
        if owns_lock:
            try:
                if orderly:
                    rpc.lifecycle_stop()  # Adapter, not this caller, verifies systemd shutdown.
                else:
                    rpc.close_and_full()
            except Exception:
                pass  # The adapter's independent lease timeout remains in force.
        os.close(fd)


if __name__ == '__main__':
    try:
        serve()
    except Exception:
        print('Selector startup failed; no credentials logged', file=sys.stderr)
        raise SystemExit(1)

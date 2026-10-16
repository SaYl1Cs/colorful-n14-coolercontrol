"""Authenticated live CPU/Max profile selection; no curve or function writes."""
import copy
import http.client
import json
import os
from pathlib import Path
import re
import socket
import stat
import threading
from urllib.parse import quote, unquote

from authorize_coolercontrol import validate_token
import control_policy
import provenance_fence
import thermal_state


class StagedError(ValueError):
    """Fixed diagnostics only; never include upstream bodies or exceptions."""


class APIUnavailable(StagedError):
    """Connection loss/timeout, distinct from an invalid API/policy response."""


def _require(condition):
    if not condition:
        raise StagedError('selector validation rejected')


def _id(value):
    _require(isinstance(value, str) and
             re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', value) is not None and
             value not in ('.', '..'))
    return value


def read_token(path):
    """Descriptor-relative, no symlinks in ANY component; no credential writes."""
    directory = fd = None
    try:
        _require(os.geteuid() == 0)
        path = Path(path)
        _require(path.is_absolute() and '..' not in path.parts)
        directory = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        for part in path.parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=directory)
            os.close(directory)
            directory = child
        info = os.fstat(directory)
        _require(info.st_uid == 0 and stat.S_IMODE(info.st_mode) == 0o700)
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=directory)
        info = os.fstat(fd)
        _require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and
                 stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1)
        raw = os.read(fd, 8193)
        _require(len(raw) <= 8192)
        return validate_token(json.loads(raw))['token']
    except Exception:
        raise StagedError('credential unavailable or unsafe') from None
    finally:
        if fd is not None:
            os.close(fd)
        if directory is not None:
            os.close(directory)


class LocalTransport:
    """Explicit loopback-only bearer transport; no proxies, redirects or retries.

    No generic URL input. http.client does not consult proxy environment variables.
    Socket shutdown bounds the entire connected request, including slow bodies;
    connection establishment has its own bounded timeout. Maximum total: 2*timeout.
    """
    def __init__(self, token_path, timeout=3.0, response_limit=262144):
        _require(type(timeout) in (int, float) and 0 < timeout <= 8)
        _require(type(response_limit) is int and 1 <= response_limit <= 1048576)
        self.token_path = token_path
        self.timeout = timeout
        self.response_limit = response_limit

    def request(self, method, path, body=None):
        connection = timer = None
        try:
            _require((method == 'GET' and (path in ('/profiles', '/functions', '/devices', '/settings', '/health')
                       or re.fullmatch(r'/devices/[A-Za-z0-9_.:%-]+/settings', path))
                      and body is None) or
                     (method == 'PUT' and re.fullmatch(
                         r'/devices/[A-Za-z0-9_.:%-]+/settings/[A-Za-z0-9_.:%-]+/profile',
                         path) is not None and type(body) is dict and
                      set(body) == {'profile_uid'} and _id(body['profile_uid'])))
            if path.startswith('/devices/'):
                segments = (path.split('/')[2], path.split('/')[4]) if method == 'PUT' else (path.split('/')[2],)
                for segment in segments:
                    _require(quote(_id(unquote(segment)), safe='') == segment)
            token = read_token(self.token_path)
            connection = http.client.HTTPConnection('127.0.0.1', 11987,
                                                     timeout=self.timeout)
            connection.connect()
            sock = connection.sock

            def expire():
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

            timer = threading.Timer(self.timeout, expire)
            timer.daemon = True
            timer.start()
            connection.request(method, path,
                               body=None if body is None else json.dumps(body),
                               headers={'Authorization': 'Bearer ' + token,
                                        'Content-Type': 'application/json',
                                        'Accept-Encoding': 'identity'})
            response = connection.getresponse()
            _require(response.status == 200)
            _require(response.getheader('Content-Encoding') in (None, 'identity'))
            raw = response.read(self.response_limit + 1)
            _require(len(raw) <= self.response_limit)
            return thermal_state.strict_json(raw) if raw else None
        except (OSError, http.client.RemoteDisconnected):
            raise APIUnavailable('local API request failed') from None
        except Exception:
            raise StagedError('local API request failed') from None
        finally:
            if timer is not None:
                timer.cancel()
            if connection is not None:
                connection.close()


class ProfileSelector:
    """Pins identities, not curve values. Stable ticks renew without full/PUT.

    A new selector always reschedules; it never adopts an unauthenticated cached
    assignment. Adapter restart, sampler epoch change and live catalog changes
    also require a fresh producer assignment under a closed full/bounded-hold fence.
    """
    def __init__(self, api, bindings, rpc, snapshot_path=thermal_state.SNAPSHOT, stopping=None):
        self.api, self.rpc = api, rpc
        self._stopping = stopping
        self.bindings = copy.deepcopy(control_policy.validate_bindings(bindings))
        self.snapshot_path = snapshot_path
        self._assignment = None
        self._digest = None
        self._epoch = None
        self._health_seen = None
        self._lock = threading.Lock()

    def _health(self):
        # Restart hint only. PID/uptime never authorizes a command; immutable
        # producer session and assignment provenance remain mandatory.
        result = self.api.request('GET', '/health')
        _require(type(result) is dict and type(result.get('details')) is dict)
        details = result['details']
        _require(type(details.get('pid')) is int and details['pid'] > 0)
        uptime = details.get('uptime')
        _require(type(uptime) is str and re.fullmatch(r'[0-9]+:[0-5][0-9]:[0-5][0-9]', uptime))
        hours, minutes, seconds = map(int, uptime.split(':'))
        return details['pid'], hours * 3600 + minutes * 60 + seconds

    def _live(self):
        profiles = control_policy.catalog(self.api.request('GET', '/profiles'), 'profiles')
        functions = control_policy.catalog(self.api.request('GET', '/functions'), 'functions')
        devices = control_policy.catalog(self.api.request('GET', '/devices'), 'devices')
        settings = self.api.request('GET', '/settings')
        _require(type(settings) is dict and type(settings.get('poll_rate')) in (int, float)
                 and settings['poll_rate'] == 1)
        return control_policy.live_policy(self.bindings, profiles, functions, devices)

    def _applied_profile(self):
        target = self.bindings['target']
        response = self.api.request('GET', '/devices/' + quote(target['device_uid'], safe='') + '/settings')
        _require(type(response) is dict and type(response.get('settings')) is list)
        matches = [setting for setting in response['settings']
                   if type(setting) is dict and setting.get('channel_name') == target['channel_name']]
        _require(len(matches) <= 1)
        return matches[0].get('profile_uid') if matches else None

    def select(self, now=None):
        with self._lock:
            try:
                health = self._health()
                same_process = (self._health_seen is not None and health[0] == self._health_seen[0]
                                and health[1] >= self._health_seen[1])
                live = self._live()
                data = thermal_state.read_snapshot(self.snapshot_path, now=now,
                                                   require_metadata=True, require_samples=True)
                _require(not thermal_state.critical_temperature(data))
                epoch = thermal_state.epoch(data)
                role = 'cpu' if thermal_state.requested_curve(data) == 'cpu' else 'mix'
                uid = self.bindings['profiles'][role]
                digest = control_policy.fingerprint(live)
                applied = self._applied_profile()
                state = self.rpc.status()
                stable = (same_process and self._assignment is not None and self._digest == digest
                          and self._epoch == epoch and applied == uid
                          and state.get('assignment') == self._assignment
                          and state.get('phase') in ('awaiting', 'active')
                          and state.get('epoch') == epoch and state.get('policy_digest') == digest)
                if stable:
                    self.rpc.renew(self._assignment, digest, epoch)
                    self._health_seen = health
                    return uid
                closed = self.rpc.handover(live)
                _require(type(closed) is dict and closed.get('phase') == 'closed'
                         and type(closed.get('close_id')) is str)
                if closed.get('full_confirmed') is not True:
                    provenance_fence.check_hold(closed)
                    _require(closed['hold_kind'] == 'handover' and closed['hold_epoch'] == epoch)
                target = self.bindings['target']
                path = '/devices/{}/settings/{}/profile'.format(
                    quote(target['device_uid'], safe=''), quote(target['channel_name'], safe=''))
                response = self.api.request('PUT', path, {'profile_uid': uid})
                provenance_fence.assignment(response)
                _require(response['device_uid'] == target['device_uid']
                         and response['channel_name'] == target['channel_name']
                         and response['assignment']['profile_uid'] == uid)
                # Catch edits during PUT instead of silently accepting a mixed
                # catalog view. This is not an atomic UI/config transaction.
                _require(control_policy.fingerprint(self._live()) == digest)
                _require(self._applied_profile() == uid)
                after = self._health()
                _require(after[0] == health[0] and after[1] >= health[1])
                self.rpc.install_assignment(response, live, epoch, closed['close_id'])
                self._health_seen = after
                self._assignment, self._digest, self._epoch = response, digest, epoch
                return uid
            except thermal_state.SensorUnavailable:
                # Only a thermal read can take this path, never API/policy or
                # provenance failure. The adapter independently checks its own
                # last safe sample, duty and deadlines; it may choose full.
                try:
                    self.rpc.sensor_check()
                except Exception:
                    pass  # Independent adapter watchdog is still authoritative.
                raise StagedError('sensor unavailable; adapter safety check requested') from None
            except Exception as exc:
                self._assignment = self._digest = self._epoch = None
                try:
                    if isinstance(exc, APIUnavailable) and self._stopping is not None and self._stopping():
                        # An in-flight API connection may disappear after SIGTERM.
                        # Signal alone is NOT authority: the adapter must still
                        # confirm systemd shutdown, cool sensors and known duty.
                        self.rpc.lifecycle_stop()
                    else:
                        self.rpc.close_and_full()
                except Exception:
                    pass  # Adapter lease expiry independently fails closed.
                raise StagedError('selection rejected; adapter safety handling requested') from None

"""Private installer staging, explicit snapshots, repeatable restore and health checks.

Only install.sh coordinates services. Snapshot/restore never call hardware or
services. Recovery uses the copies of this file and install.sh in the backup,
not the checkout or the partially installed backend. No token is copied/written.
"""
import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import time
import tomllib

PREFIX = '/usr/local/share/colorful-n14-control'
CLIENT = '/usr/local/libexec/colorful-n14-coolercontrol'
DAEMON = '/usr/local/libexec/colorful-n14-coolercontrold'
PLUGIN = '/usr/local/libexec/colorful-n14-custom-device'
BINDINGS = '/etc/colorful-n14-control/profile-bindings.json'
PREPARE = 'colorful-n14-nbfc-prepare.service'
SENSORS = 'colorful-n14-sensors.service'
ADAPTER = 'colorful-n14-adapter.service'
SELECTOR = 'colorful-n14-profile-selector.service'
COOLER = 'coolercontrold.service'
PLUGIN_UNIT = 'cc-plugin-custom-device.service'
PREREQUISITE = 'colorful-n14-ec-readonly.service'
# The daemon generates the plugin unit; restore/start the daemon first.
START_ORDER = (PREPARE, SENSORS, ADAPTER, COOLER, PLUGIN_UNIT, SELECTOR)
SENSOR_DROPINS = '/etc/systemd/system/' + SENSORS + '.d'
REMOVABLE = SENSOR_DROPINS + '/60-active-gpu-temperature.conf'
PATHS = (
    PREFIX, CLIENT, DAEMON, PLUGIN, BINDINGS,
    *('/etc/systemd/system/' + unit for unit in START_ORDER if unit != COOLER),
    '/etc/systemd/system/' + PREREQUISITE,
    *('/etc/systemd/system/' + unit + '.d' for unit in (*START_ORDER, PREREQUISITE)),
    '/etc/modules-load.d/colorful-n14-control.conf',
)
MODULES = (
    'build_profile.py', 'curves.py', 'prepare.py', 'prepare-autostart.py',
    'sensor_cache.py', 'coolercontrol_adapter.py', 'adapter_server.py',
    'thermal_state.py', 'control_policy.py', 'profile_selector.py',
    'profile_selector_service.py', 'capture_profile_bindings.py',
    'authorize_coolercontrol.py', 'provenance_fence.py',
    'install_prerequisites.py', 'install_transaction.py',
)
SOURCES = (*MODULES, 'install.sh', 'curves.json', 'coolercontrol-config.json',
           'curve_editor.py', 'gtk_editor.py', 'run-control.sh', 'run-read-only.sh',
           'src/adapter_client.c', 'src/transport.lua', 'coolercontrold-n14.conf',
           'configs/Colorful N14 24 - Temperature Curves - GPU Command Readback.json',
           *(unit for unit in (PREPARE, SENSORS, ADAPTER, SELECTOR)))
RELEASES = {
    'build-verification/daemon-target/release/coolercontrold': DAEMON,
    'build-verification/plugin-target/release/custom-device': PLUGIN,
}
PATCHES = ('patches/coolercontrold-producer-provenance.patch',
           'patches/custom-device-producer-provenance.patch')


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def run(argv, **kwargs):
    return subprocess.run(argv, check=True, text=True, capture_output=True,
                          timeout=10, **kwargs).stdout.strip()


def show(unit, field):
    return run(['systemctl', 'show', unit, '--property=' + field, '--value'])


def active(unit):
    return show(unit, 'ActiveState') == 'active'


def absolute(name):
    require(isinstance(name, str) and re.fullmatch(r'/[A-Za-z0-9_./:+ -]+', name)
            and '..' not in Path(name).parts and str(Path(name)) == name,
            'unsafe/unsupported absolute deployment path')
    return Path(name)


def location(root, name):
    path = Path(root) / absolute(name).relative_to('/')
    # Dynamic plugin/config paths are canonicalized during preflight. Never
    # follow a newly substituted parent symlink while applying/restoring.
    require(not any(parent.is_symlink() for parent in path.parents),
            'symlink parent in transaction target')
    return path


def canonical(root, name):
    path = (Path(root) / absolute(name).relative_to('/')).resolve(strict=True)
    return '/' + str(path.relative_to(Path(root).resolve()))


def trusted(path):
    for item in (Path(path), *Path(path).parents):
        info = item.stat()
        require(info.st_uid == 0 and not info.st_mode & 0o022,
                'deployment source/target must have root-owned non-writable ancestors')


def discover_layout(plugin_dir=None, root=Path('/')):
    """Use the running daemon's actual environment, including EnvironmentFiles.

    The patched plugin ALWAYS reads /etc/coolercontrol/plugins/custom-device/
    config.json. That may differ from the daemon's manifest directory. Never
    infer its config path from the relocated executable or WorkingDirectory.
    """
    root = Path(root)
    pid = show(COOLER, 'MainPID')
    require(pid.isdecimal() and int(pid) > 0, 'running CoolerControl daemon required')
    proc = root / 'proc' / pid
    env = dict(item.split('=', 1) for item in (proc / 'environ').read_text().split('\0') if '=' in item)
    argv = [item for item in (proc / 'cmdline').read_text().split('\0') if item]
    require(argv and Path(argv[0]).name in ('coolercontrold', Path(DAEMON).name),
            'unsupported daemon launcher; run the daemon directly in its unit')
    config_dir = canonical(root, env.get('CC_CONFIG_DIR', '/etc/coolercontrol'))
    data_dir = canonical(root, env.get('CC_DATA_DIR', '/var/lib/coolercontrol'))
    plugins = env.get('CC_PLUGINS_DIR', data_dir + '/plugins')
    candidate = plugin_dir or plugins + '/custom-device'
    if not plugin_dir and 'CC_PLUGINS_DIR' not in env:
        # The running generated plugin unit is authoritative for pre-migration
        # layouts. A side-by-side executable no longer identifies its manifest
        # directory, so that case uses the daemon environment/default instead.
        executable = re.search(r'path=([^ ;]+)', show(PLUGIN_UNIT, 'ExecStart'))
        if executable:
            parent = absolute(executable[1]).parent
            if parent.name == 'custom-device' and (root / parent.relative_to('/') / 'manifest.toml').is_file():
                candidate = str(parent)
    manifest_dir = canonical(root, candidate)
    require(Path(manifest_dir).name == 'custom-device', 'plugin directory must identify custom-device')
    conventional = ('/etc/coolercontrol/plugins/custom-device', '/var/lib/coolercontrol/plugins/custom-device')
    if not plugin_dir:
        require(manifest_dir in conventional or 'CC_PLUGINS_DIR' in env or 'CC_DATA_DIR' in env,
                'nonstandard plugin location requires explicit --plugin-dir')
    config_plugin_dir = canonical(root, conventional[0])
    require(Path(config_plugin_dir).name == 'custom-device', 'unexpected plugin config location')
    for folder in (manifest_dir, config_plugin_dir):
        path = location(root, folder)
        require(path.is_dir(), 'plugin directory missing')
        if root == Path('/'):
            trusted(path)
    manifest = location(root, manifest_dir + '/manifest.toml')
    document = tomllib.loads(manifest.read_text())
    require(document.get('id') == 'custom-device' and document.get('type') == 'device'
            and document.get('privileged') is True and document.get('executable')
            and document.get('address') == '/tmp/custom-device.sock',
            'plugin manifest must be managed, privileged and use the compiled plugin socket')
    # Preserve all args/env/metadata, but reject unknown CLI options on this binary.
    require(document.get('args', '') in ('', '--debug') or document.get('args') in ([], ['--debug']),
            'unsupported custom-device arguments')
    require(location(root, config_plugin_dir + '/config.json').is_file(), 'actual plugin config missing')
    require(location(root, config_dir + '/config.toml').is_file(), 'CoolerControl profiles missing')
    if root == Path('/'):
        trusted(manifest)
        trusted(location(root, config_plugin_dir + '/config.json'))
    for unit in START_ORDER[:-1]:
        require(active(unit), 'upgrade requires active ' + unit)
    require(show('sys-kernel-debug.mount', 'LoadState') == 'loaded'
            and active('sys-kernel-debug.mount'), 'systemd debugfs mount must be loaded and active')
    require(not active('nbfc_service.service'), 'nbfc_service must not be active')
    config_targets = []
    for name in ('config.toml', 'config-ui.json'):
        path = location(root, config_dir + '/' + name)
        if path.is_symlink():
            config_targets.append(canonical(root, config_dir + '/' + name))
    return dict(manifest_dir=manifest_dir, config_plugin_dir=config_plugin_dir,
                config_dir=config_dir, data_dir=data_dir, daemon_args=argv[1:],
                config_targets=config_targets,
                active_units=[unit for unit in START_ORDER if active(unit)])


def transaction_paths(layout):
    paths = list(PATHS)
    if layout:
        for key in ('manifest_dir', 'config_plugin_dir'):
            path = str(absolute(layout[key]))
            require(Path(path).name == 'custom-device', 'invalid plugin backup path')
            paths.append(path)
        for name in ('config.toml', 'config-ui.json'):
            paths.append(str(absolute(layout['config_dir'])) + '/' + name)
        paths.extend(str(absolute(name)) for name in layout.get('config_targets', []))
    # De-duplicate canonical aliases; reject overlapping trees, rather than
    # restoring a child and later deleting it through its parent.
    paths = list(dict.fromkeys(paths))
    require(not any(a != b and Path(a) in Path(b).parents for a in paths for b in paths),
            'overlapping backup targets')
    return paths


def digest(path):
    h = hashlib.sha256()
    def visit(item, relative):
        info = item.lstat()
        h.update(json.dumps([relative, stat.S_IMODE(info.st_mode)]).encode())
        if item.is_symlink():
            h.update(b'L' + os.readlink(item).encode())
        elif item.is_dir():
            h.update(b'D')
            for child in sorted(item.iterdir()):
                visit(child, relative + '/' + child.name)
        elif item.is_file():
            h.update(b'F')
            with item.open('rb') as stream:
                while block := stream.read(1024 * 1024):
                    h.update(block)
        else:
            raise RuntimeError('unsupported special file in backup')
    visit(Path(path), '')
    return h.hexdigest()


def copy(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(['cp', '-a', '--no-dereference', '--', str(source), str(target)],
                   check=True, timeout=60)


def save_json(path, data):
    path.write_text(json.dumps(data, indent=2) + '\n')
    path.chmod(0o600)


def snapshot(backup, root=Path('/'), layout=None, source=None):
    backup = Path(backup)
    paths = transaction_paths(layout)
    for name in paths:
        location(root, name)
    backup.mkdir(mode=0o700, parents=True, exist_ok=False)
    manifest = dict(schema=1, layout=layout, files=[])
    for name in paths:
        target = location(root, name)
        present = target.exists() or target.is_symlink()
        checksum = None
        if present:
            saved = backup / 'files' / name.lstrip('/')
            copy(target, saved)
            checksum = digest(saved)
            require(checksum == digest(target), 'configuration changed during snapshot; retry preflight')
        manifest['files'].append(dict(path=name, present=present, digest=checksum))
    if source is not None:
        for name in ('install.sh', 'install_transaction.py'):
            copy(Path(source) / name, backup / 'recovery' / name)
    # A partial backup never has a valid manifest. Flush before arming recovery.
    save_json(backup / 'manifest.json', manifest)
    os.sync()


def validate_backup(backup, root=Path('/')):
    backup = Path(backup)
    manifest = json.loads((backup / 'manifest.json').read_text())
    require(manifest.get('schema') == 1 and
            [row['path'] for row in manifest['files']] == transaction_paths(manifest['layout']),
            'invalid transaction manifest')
    for row in manifest['files']:
        location(root, row['path'])
        require(type(row['present']) is bool, 'invalid presence record')
        saved = backup / 'files' / row['path'].lstrip('/')
        if row['present']:
            require((saved.exists() or saved.is_symlink()) and digest(saved) == row['digest'],
                    'incomplete or damaged backup')
    if manifest['layout']:
        require(set(manifest['layout']['active_units']) <= set(START_ORDER), 'invalid unit list')
    return manifest


def remove(path):
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def restore(backup, root=Path('/')):
    manifest = validate_backup(backup, root)
    for row in manifest['files']:
        target = location(root, row['path'])
        remove(target)
        if row['present']:
            copy(Path(backup) / 'files' / row['path'].lstrip('/'), target)


def verified_releases(source):
    """Verify exact patch/binary set, ELF ABI, dynamic dependencies and build receipts."""
    source = Path(source)
    checksums = {}
    for line in (source / 'patches/BUILD-SHA256SUMS').read_text().splitlines():
        checksum, name = line.split()
        require(re.fullmatch(r'[0-9a-f]{64}', checksum) and name.startswith('nbfc-n14-control/'),
                'invalid release checksum manifest')
        name = name.removeprefix('nbfc-n14-control/')
        require(name not in checksums, 'duplicate release checksum')
        checksums[name] = checksum
    require(set(checksums) == set(RELEASES) | set(PATCHES), 'incomplete patched release manifest')
    for name, checksum in checksums.items():
        path = source / name
        with path.open('rb') as stream:
            require(hashlib.file_digest(stream, 'sha256').hexdigest() == checksum,
                    'patched artifact checksum mismatch: ' + name)
        if name in RELEASES:
            with path.open('rb') as stream:
                header = stream.read(20)
            require(header[:6] == b'\x7fELF\x02\x01' and header[18:20] == b'\x3e\x00'
                    and os.access(path, os.X_OK), 'expected executable x86-64 Linux release')
            require('not found' not in run(['ldd', str(path)]), 'missing release shared-library dependency')
    for name in ('daemon-tests', 'plugin-tests', 'daemon-release', 'plugin-release'):
        require((source / ('build-verification/' + name + '.exit')).read_text().strip() == '0',
                'successful producer build/test receipt missing')
    return checksums


def merge_config(original, ours):
    require(type(original) is dict and type(original.get('devices')) is list, 'invalid plugin configuration')
    ours = ours['devices'][0]
    ids = [device['id'] for device in original['devices']]
    require(len(ids) == len(set(ids)), 'duplicate device IDs')
    require(ours['id'] in ids, 'upgrade must retain the existing N14 device ID')
    # Preserve unknown properties in our device, channels and sensors as well as
    # every unrelated device and global plugin option.
    result = json.loads(json.dumps(original))
    device = next(d for d in result['devices'] if d['id'] == ours['id'])
    device['label'] = ours['label']
    for kind in ('channels', 'temps'):
        for replacement in ours[kind]:
            found = [d for d in device[kind] if d['id'] == replacement['id']]
            require(len(found) == 1, 'missing/duplicate stable channel or temperature ID')
            entry = found[0]
            if kind == 'channels':
                commands = entry['commands']
                commands.pop('set_duty', None)
                commands.update(replacement['commands'])
                entry['label'] = replacement['label']
            else:
                entry.update(replacement)
    return result


def patch_manifest(text):
    before = tomllib.loads(text)
    # Replace just the root executable line, retaining comments, unknown fields,
    # envs, args, address, privilege and all tables verbatim.
    head = re.split(r'(?m)^\s*\[', text, maxsplit=1)[0]
    pattern = r'(?m)^(\s*executable\s*=)[^\n]*$'
    require(len(re.findall(pattern, head)) == 1, 'manifest needs one root executable field')
    updated = re.sub(pattern, lambda m: m[1] + ' ' + json.dumps(PLUGIN), head) + text[len(head):]
    expected = dict(before, executable=PLUGIN)
    require(tomllib.loads(updated) == expected, 'manifest rewrite changed unrelated values')
    return updated


def systemd_word(value):
    require(isinstance(value, str) and not any(ord(c) < 32 or ord(c) == 127 for c in value),
            'invalid systemd argument')
    return json.dumps(value.replace('%', '%%').replace('$', '$$'), ensure_ascii=False)


def stage(source, directory, plugin_dir=None, root=Path('/')):
    source, directory = Path(source), Path(directory)
    checksums = verified_releases(source)
    layout = discover_layout(plugin_dir, root)
    bundle = directory / 'source'
    for name in SOURCES:
        data = (source / name).read_text()
        if name.endswith('.py'):
            ast.parse(data, filename=name)
        copy(source / name, bundle / name)
    copy(source / 'tests', bundle / 'tests')
    # Read these before any stop. A root-private copy is the only input to apply.
    for name in RELEASES:
        target = directory / Path(RELEASES[name]).name
        copy(source / name, target)
        with target.open('rb') as stream:
            require(hashlib.file_digest(stream, 'sha256').hexdigest() == checksums[name],
                    'release changed while staging')
    output = directory / 'tree'
    def asset(name, content, mode=0o644):
        path = output / name.lstrip('/')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        path.chmod(mode)
    from install_prerequisites import ASSETS
    for name, content in ASSETS.items():
        asset(name, content)
    for name in MODULES + ('curves.json', 'src/transport.lua'):
        asset(PREFIX + '/' + name, (bundle / name).read_text())
    profile = 'configs/Colorful N14 24 - Temperature Curves - GPU Command Readback.json'
    asset(PREFIX + '/' + profile, (bundle / profile).read_text())
    for unit in (PREPARE, SENSORS, ADAPTER, SELECTOR):
        asset('/etc/systemd/system/' + unit, (bundle / unit).read_text())
    config_name = layout['config_plugin_dir'] + '/config.json'
    config = location(root, config_name)
    asset(config_name, json.dumps(merge_config(json.loads(config.read_text()),
                                              json.loads((bundle / 'coolercontrol-config.json').read_text())),
                                 ensure_ascii=False, indent=2) + '\n', 0o600)
    manifest_name = layout['manifest_dir'] + '/manifest.toml'
    manifest = location(root, manifest_name)
    asset(manifest_name, patch_manifest(manifest.read_text()), stat.S_IMODE(manifest.stat().st_mode))
    dropin = (bundle / 'coolercontrold-n14.conf').read_text()
    dropin += '\n[Unit]\nWants=' + SELECTOR + '\n\n[Service]\nExecStart=\nExecStart='
    dropin += ' '.join(systemd_word(arg) for arg in [DAEMON, *layout['daemon_args']]) + '\n'
    for key, value in (('CC_CONFIG_DIR', layout['config_dir']), ('CC_DATA_DIR', layout['data_dir']),
                       ('CC_PLUGINS_DIR', str(Path(layout['manifest_dir']).parent))):
        # Pin resolved roots even if upstream defaults migrate in a later release.
        dropin += 'Environment=' + json.dumps(key + '=' + value.replace('%', '%%')) + '\n'
    asset('/etc/systemd/system/' + COOLER + '.d/colorful-n14-control.conf', dropin)
    # Daemon regenerates the plugin unit from its manifest. This specific drop-in
    # also wins over stale generated ExecStart until that regeneration occurs.
    asset('/etc/systemd/system/' + PLUGIN_UNIT + '.d/colorful-n14-control.conf',
          '[Unit]\nRequires=' + ADAPTER + '\nAfter=' + ADAPTER + '\n\n[Service]\nExecStart=\nExecStart=' +
          ' '.join(systemd_word(arg) for arg in [PLUGIN, *(['--debug'] if
                   tomllib.loads(manifest.read_text()).get('args') in ('--debug', ['--debug']) else [])]) + '\n')
    save_json(directory / 'layout.json', layout)
    # Ensure every intended target is covered before any cooling service stops.
    paths = transaction_paths(layout)
    for item in output.rglob('*'):
        if item.is_file():
            name = '/' + str(item.relative_to(output))
            require(any(name == p or name.startswith(p + '/') for p in paths), 'unstaged backup target')
            location(root, name)


def apply(directory, backup, root=Path('/')):
    directory = Path(directory)
    manifest = validate_backup(backup, root)
    layout = json.loads((directory / 'layout.json').read_text())
    require(layout == manifest['layout'], 'staged layout differs from snapshot')
    tree = directory / 'tree'
    for source, target in ((directory / 'adapter-client', CLIENT),
                           (directory / Path(DAEMON).name, DAEMON),
                           (directory / Path(PLUGIN).name, PLUGIN),
                           (directory / 'profile-bindings.json', BINDINGS)):
        staged = tree / target.lstrip('/')
        copy(source, staged)
        staged.chmod(0o600 if target == BINDINGS else 0o755)
    # Never truncate through leaf symlinks or an unrelated .new sidecar.
    import tempfile
    for source in sorted(tree.rglob('*')):
        if not source.is_file():
            continue
        name = '/' + str(source.relative_to(tree))
        require(any(name == p or name.startswith(p + '/') for p in transaction_paths(layout)),
                'apply target outside backup')
        target = location(root, name)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        fd, temporary = tempfile.mkstemp(prefix='.n14-install-', dir=target.parent)
        os.close(fd)
        try:
            shutil.copyfile(source, temporary)
            os.chmod(temporary, stat.S_IMODE(source.stat().st_mode))
            os.replace(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)
    location(root, REMOVABLE).unlink(missing_ok=True)


def check_bindings(path):
    from authorize_coolercontrol import TOKEN_FILE
    from profile_selector import LocalTransport, ProfileSelector
    from profile_selector_service import read_bindings
    # Capture already validated profile identities. Also enforce the live poll
    # rate/policy before stopping anything; this method only performs GETs.
    ProfileSelector(LocalTransport(TOKEN_FILE), read_bindings(path), None)._live()


def health_sample():
    """Read-only: no selector.select(), profile PUT, reset, renewal or fence write."""
    import control_policy
    import provenance_fence as fence
    import thermal_state as thermal
    from authorize_coolercontrol import TOKEN_FILE
    from profile_selector import LocalTransport, ProfileSelector
    from profile_selector_service import AdapterFenceRPC, read_bindings
    for unit in START_ORDER:
        require(active(unit), 'required service not active')
    for unit, binary in ((COOLER, DAEMON), (PLUGIN_UNIT, PLUGIN)):
        pid = show(unit, 'MainPID')
        require(pid.isdecimal() and int(pid) > 0 and
                os.readlink('/proc/' + pid + '/exe') == binary, 'wrong live producer executable')
    bindings = read_bindings(BINDINGS)
    api = LocalTransport(TOKEN_FILE, timeout=0.5)
    rpc = AdapterFenceRPC(timeout=0.5)
    selector = ProfileSelector(api, bindings, rpc)
    policy = selector._live()
    applied = selector._applied_profile()
    current = thermal.read_snapshot(require_metadata=True, require_samples=True)
    state = fence.watchdog_check(current)
    status = rpc.status()
    require(status.get('assignment') == state['assignment'] and status.get('epoch') == state['epoch'],
            'adapter fence changed during health check')
    expected = bindings['profiles']['cpu' if current['gpu_state'] == 'suspended' else 'mix']
    assignment = fence.assignment(state['assignment'])
    require(applied == expected == assignment['assignment']['profile_uid']
            and assignment['device_uid'] == bindings['target']['device_uid']
            and assignment['channel_name'] == bindings['target']['channel_name']
            and state['policy_digest'] == control_policy.fingerprint(policy),
            'selector/live assignment or policy mismatch')
    require(state['full_confirmed'] is True and
            (state['phase'] == 'active' or state['last_duty'] == 100), 'awaiting fence must retain full duty')
    return dict(identity=[thermal.epoch(current), assignment], sample=current['sample_id'],
                lease=state['lease_until'], phase=state['phase'])


def health(timeout=55, sample=health_sample, clock=time.monotonic, sleep=time.sleep):
    require(0 < timeout <= 55, 'health deadline must leave room for the external 60s cap')
    deadline = clock() + timeout
    previous = None
    stable = 0
    mismatches = 0
    while clock() < deadline:
        try:
            current = sample()
            if previous and current['identity'] == previous['identity'] and current['sample'] > previous['sample']:
                stable += 1
            else:
                stable = 0
            # Confirm at least one genuine selector lease renewal, not just one
            # cached valid assignment repeatedly read while the selector is dead.
            if previous and current['lease'] <= previous['lease']:
                stable = 0
            previous = current
            if stable >= 5:
                return current['phase']
        except Exception:
            if previous is not None:
                mismatches += 1
            stable = 0
            require(mismatches < 3, 'repeated post-start health/assignment mismatches')
        sleep(1)
    raise RuntimeError('health deadline exceeded; selector/snapshot/fence never stabilized')


def process_start(pid):
    # comm may contain spaces/parentheses; field 22 follows the final ')'.
    return Path('/proc/' + str(pid) + '/stat').read_text().rsplit(')', 1)[1].split()[19]


def record_owner(backup, pid):
    require(pid > 1, 'invalid installer PID')
    save_json(Path(backup) / 'owner.json', dict(pid=pid, start=process_start(pid)))


def interrupt_owner(backup):
    import signal
    owner = json.loads((Path(backup) / 'owner.json').read_text())
    require(type(owner['pid']) is int and owner['pid'] > 1, 'invalid installer PID')
    try:
        # pidfd pins process identity across comparison and signalling.
        fd = os.pidfd_open(owner['pid'])
    except ProcessLookupError:
        return
    try:
        if process_start(owner['pid']) == owner['start']:
            signal.pidfd_send_signal(fd, signal.SIGTERM)
    except (ProcessLookupError, FileNotFoundError):
        pass
    finally:
        os.close(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('stage', 'snapshot', 'validate', 'restore', 'units', 'apply', 'health', 'owner', 'interrupt', 'check-bindings'))
    parser.add_argument('directory', nargs='?', type=Path)
    parser.add_argument('--source', type=Path)
    parser.add_argument('--backup', type=Path)
    parser.add_argument('--plugin-dir')
    parser.add_argument('--pid', type=int)
    args = parser.parse_args()
    if args.action == 'owner':
        record_owner(args.directory, args.pid)
    elif args.action == 'interrupt':
        interrupt_owner(args.directory)
    elif args.action == 'check-bindings':
        check_bindings(args.directory)
    elif args.action == 'health':
        print('Healthy: fresh snapshots, live CPU/Mix assignment, fence ' + health())
    elif args.action == 'stage':
        stage(args.source, args.directory, args.plugin_dir)
    elif args.action == 'snapshot':
        snapshot(args.directory, layout=json.loads((args.source.parent / 'layout.json').read_text()), source=args.source)
    elif args.action == 'validate':
        validate_backup(args.directory)
    elif args.action == 'units':
        manifest = validate_backup(args.directory)
        for unit in START_ORDER:
            if unit in manifest['layout']['active_units']:
                print(unit)
    elif args.action == 'apply':
        apply(args.directory, args.backup)
    else:
        restore(args.directory)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # Never dump an API body, environment, credential, or traceback.
        print('STOP: installer transaction failed (' + type(exc).__name__ + '): ' +
              (str(exc) if isinstance(exc, RuntimeError) else 'check private staging/backup and prerequisites'),
              file=__import__('sys').stderr)
        raise SystemExit(1)

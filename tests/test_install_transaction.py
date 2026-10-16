"""Offline installer tests. All roots/process/service/API/EC effects are fake."""
from contextlib import ExitStack
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import tomllib
import unittest
from unittest.mock import Mock, patch

import install_prerequisites as prereq
import install_transaction as tx

PROJECT = Path(__file__).resolve().parents[1]
MANIFEST = '''# preserve this comment
id = "custom-device"
type = "device"
version = "0.2.0"
address = "/tmp/custom-device.sock"
executable = "custom-device"
privileged = true
args = "--debug"
[envs]
USER_SETTING = "leave me alone"
'''


class TemporaryRoot(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'root'
        self.root.mkdir()
        self.backup = Path(self.tmp.name) / 'backup'
        self.layout = dict(manifest_dir='/var/lib/coolercontrol/plugins/custom-device',
                           config_plugin_dir='/etc/coolercontrol/plugins/custom-device',
                           config_dir='/etc/coolercontrol', data_dir='/var/lib/coolercontrol',
                           daemon_args=[], active_units=list(tx.START_ORDER[:-1]))

    def put(self, name, value='original', root=None):
        target = (self.root if root is None else root) / name.lstrip('/')
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(value, bytes):
            target.write_bytes(value)
        else:
            target.write_text(value)
        return target

    def plugin_fixture(self, alias=False):
        self.put('/proc/123/cmdline', '/usr/bin/coolercontrold\0--debug\0')
        self.put('/proc/123/environ', 'CC_LOG=INFO\0UNRELATED=keep\0')
        self.put('/etc/coolercontrol/config.toml', 'profiles = []\n')
        self.put('/etc/coolercontrol/config-ui.json', '{"theme":"custom"}')
        self.put(self.layout['manifest_dir'] + '/manifest.toml', MANIFEST)
        self.put(self.layout['manifest_dir'] + '/config.json', '{"unused-copy":true}')
        if alias:
            path = self.root / 'etc/coolercontrol/plugins'
            path.symlink_to(self.root / 'var/lib/coolercontrol/plugins', target_is_directory=True)
            self.layout['config_plugin_dir'] = self.layout['manifest_dir']
        self.put(self.layout['config_plugin_dir'] + '/config.json', (PROJECT / 'coolercontrol-config.json').read_text())
        self.put(self.layout['config_plugin_dir'] + '/administrator-note', 'preserved')

    def show(self, unit, field):
        if field == 'MainPID':
            return '123'
        if field == 'ExecStart':
            return '{ path=' + self.layout['manifest_dir'] + '/custom-device ; argv[]=custom-device ; }'
        if field == 'LoadState':
            return 'loaded'
        if field == 'ActiveState':
            return 'inactive' if unit in (tx.SELECTOR, 'nbfc_service.service') else 'active'
        raise AssertionError((unit, field))


class Prerequisites(TemporaryRoot):
    def setUp(self):
        super().setUp()
        self.put('/sys/module/ec_sys/parameters/write_support', 'Y\n')
        self.put('/proc/cmdline', 'quiet ec_sys.write_support=1')
        self.put('/proc/self/mountinfo', '20 1 0:9 / /sys/kernel/debug rw shared:3 - debugfs debugfs rw\n')
        self.put('/sys/kernel/debug/ec/ec0/io', 'must not be sampled')

    def test_readonly_open_without_sampling_or_flag_change(self):
        original = os.open
        with patch.object(prereq.os, 'open', wraps=original) as opened, patch.object(prereq.os, 'read') as read:
            prereq.check(self.root)
        self.assertEqual(opened.call_args.args[1], os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        read.assert_not_called()
        self.assertEqual((self.root / 'sys/module/ec_sys/parameters/write_support').read_text(), 'Y\n')

    def test_all_global_write_policies_and_boot_options_preserved(self):
        foreign = self.put('/etc/modprobe.d/isw-ec_sys.conf', 'options ec_sys write_support=1\n')
        for flag in ('Y', 'N', '1', '0'):
            for cmdline in ('quiet', 'ec_sys.write_support=1', 'ec_sys.write_support=0 ec_sys.write_support=1'):
                with self.subTest(flag=flag, cmdline=cmdline):
                    parameter = self.put('/sys/module/ec_sys/parameters/write_support', flag)
                    boot = self.put('/proc/cmdline', cmdline)
                    prereq.check(self.root)
                    self.assertEqual(parameter.read_text(), flag)
                    self.assertEqual(boot.read_text(), cmdline)
        self.assertEqual(foreign.read_text(), 'options ec_sys write_support=1\n')

    def test_missing_module(self):
        shutil.rmtree(self.root / 'sys/module/ec_sys')
        with self.assertRaisesRegex(prereq.PrerequisiteError, 'ec_sys'):
            prereq.check(self.root)

    def test_mount_must_be_debugfs_at_exact_mountpoint(self):
        for row in ('20 1 0:9 / /sys/kernel/debug rw - tmpfs tmpfs rw',
                    '20 1 0:9 / /wrong rw - debugfs debugfs rw', 'malformed'):
            self.put('/proc/self/mountinfo', row)
            with self.assertRaises(prereq.PrerequisiteError):
                prereq.check(self.root)

    def test_missing_or_symlink_io(self):
        path = self.root / 'sys/kernel/debug/ec/ec0/io'
        path.unlink()
        with self.assertRaises(prereq.PrerequisiteError):
            prereq.check(self.root)
        path.symlink_to('/dev/null')
        with self.assertRaises(prereq.PrerequisiteError):
            prereq.check(self.root)

    def test_nbfc_process_or_pidfile_refused(self):
        path = self.put('/proc/123/comm', 'nbfc_service\n')
        with self.assertRaisesRegex(prereq.PrerequisiteError, 'nbfc_service'):
            prereq.check(self.root)
        path.unlink()
        self.put('/run/nbfc_service.pid', '123')
        with self.assertRaisesRegex(prereq.PrerequisiteError, 'PID file'):
            prereq.check(self.root)

    def test_persistent_assets_never_override_write_policy(self):
        self.assertEqual(prereq.ASSETS['/etc/modules-load.d/colorful-n14-control.conf'], 'acpi_call\nec_sys\n')
        self.assertFalse(any('modprobe.d' in p for p in prereq.ASSETS))
        self.assertNotIn('write_support=', ''.join(prereq.ASSETS.values()))
        service = prereq.ASSETS['/etc/systemd/system/' + tx.PREREQUISITE]
        self.assertIn('Requires=sys-kernel-debug.mount', service)
        self.assertIn('RemainAfterExit=no', service)
        for name in prereq.ASSETS:
            self.assertTrue(any(name == p or name.startswith(p + '/') for p in tx.PATHS))


class LayoutAndWiring(TemporaryRoot):
    def test_duplicate_plugin_paths_use_manifest_root_and_actual_config(self):
        self.plugin_fixture()
        with patch.object(tx, 'show', side_effect=self.show):
            layout = tx.discover_layout(root=self.root)
        self.assertEqual(layout['manifest_dir'], self.layout['manifest_dir'])
        self.assertEqual(layout['config_plugin_dir'], self.layout['config_plugin_dir'])
        self.assertEqual(layout['daemon_args'], ['--debug'])
        self.assertNotIn(tx.SELECTOR, layout['active_units'])
        paths = tx.transaction_paths(layout)
        self.assertIn(self.layout['manifest_dir'], paths)
        self.assertIn(self.layout['config_plugin_dir'], paths)

    def test_running_plugin_unit_disambiguates_duplicate_manifests(self):
        self.plugin_fixture()
        self.put(self.layout['config_plugin_dir'] + '/manifest.toml', MANIFEST)
        def show(unit, field):
            if field == 'ExecStart':
                return '{ path=/etc/coolercontrol/plugins/custom-device/custom-device ; }'
            return self.show(unit, field)
        with patch.object(tx, 'show', side_effect=show):
            self.assertEqual(tx.discover_layout(root=self.root)['manifest_dir'], self.layout['config_plugin_dir'])

    def test_symlinked_profiles_target_is_backed_up_and_restored(self):
        self.plugin_fixture()
        target = self.put('/private-profiles/config.toml', 'original profiles')
        link = self.root / 'etc/coolercontrol/config.toml'
        link.unlink()
        link.symlink_to(target)
        with patch.object(tx, 'show', side_effect=self.show):
            layout = tx.discover_layout(root=self.root)
        tx.snapshot(self.backup, self.root, layout)
        target.write_text('new assignment')
        tx.restore(self.backup, self.root)
        self.assertTrue(link.is_symlink())
        self.assertEqual(target.read_text(), 'original profiles')

    def test_symlink_aliases_deduplicated_not_replaced(self):
        self.plugin_fixture(alias=True)
        with patch.object(tx, 'show', side_effect=self.show):
            layout = tx.discover_layout(root=self.root)
        self.assertEqual(layout['manifest_dir'], layout['config_plugin_dir'])
        self.assertEqual(tx.transaction_paths(layout).count(layout['manifest_dir']), 1)
        tx.snapshot(self.backup, self.root, layout)
        tx.restore(self.backup, self.root)
        self.assertTrue((self.root / 'etc/coolercontrol/plugins').is_symlink())

    def test_explicit_or_runtime_environment_plugin_root(self):
        self.plugin_fixture()
        self.put('/opt/plugins/custom-device/manifest.toml', MANIFEST)
        with patch.object(tx, 'show', side_effect=self.show):
            layout = tx.discover_layout('/opt/plugins/custom-device', root=self.root)
            self.assertEqual(layout['manifest_dir'], '/opt/plugins/custom-device')
            self.put('/proc/123/environ', 'CC_PLUGINS_DIR=/opt/plugins\0')
            self.assertEqual(tx.discover_layout(root=self.root)['manifest_dir'], '/opt/plugins/custom-device')

    def test_manifest_requires_correct_identity_privilege_socket(self):
        self.plugin_fixture()
        for old, new in (('privileged = true', 'privileged = false'),
                         ('"/tmp/custom-device.sock"', '"/tmp/wrong.sock"'),
                         ('id = "custom-device"', 'id = "other"')):
            self.put(self.layout['manifest_dir'] + '/manifest.toml', MANIFEST.replace(old, new))
            with patch.object(tx, 'show', side_effect=self.show), self.assertRaises(RuntimeError):
                tx.discover_layout(root=self.root)

    def test_manifest_patch_preserves_every_other_value_and_comment(self):
        result = tx.patch_manifest(MANIFEST)
        self.assertEqual(tomllib.loads(result), dict(tomllib.loads(MANIFEST), executable=tx.PLUGIN))
        self.assertIn('# preserve this comment', result)
        with self.assertRaises(RuntimeError):
            tx.patch_manifest('id="custom-device"\n')

    def test_provenance_config_and_stable_ids(self):
        config = json.loads((PROJECT / 'coolercontrol-config.json').read_text())
        device = config['devices'][0]
        self.assertEqual(device['id'], 'colorful-n14-fans')
        channel = device['channels'][0]
        self.assertEqual(channel['id'], 'both_fans')
        self.assertEqual(channel['commands']['set_duty_provenance'], tx.CLIENT + ' set-duty-provenance')
        self.assertNotIn('set_duty', channel['commands'])
        # No fictitious field: actual plugin schema uses presence of command as requirement.
        self.assertNotIn('require_provenance', channel)
        for role, temp in zip(('cpu', 'gpu'), device['temps']):
            self.assertEqual(temp['id'], role + '_temp')
            self.assertIs(temp['provenance'], True)
            self.assertEqual(temp['command'], tx.CLIENT + ' temp-' + role + '/json')
        self.assertIn('unavailable during sleep', device['temps'][1]['label'])

    def test_merge_preserves_unknown_settings_and_other_devices(self):
        ours = json.loads((PROJECT / 'coolercontrol-config.json').read_text())
        old = deepcopy(ours)
        old['global_setting'] = {'keep': True}
        old['devices'].append({'id': 'foreign', 'keep': [1, 2]})
        old['devices'][0]['custom'] = 'keep'
        old['devices'][0]['channels'][0]['commands']['set_duty'] = 'old'
        old['devices'][0]['temps'][0]['custom'] = 'keep'
        result = tx.merge_config(old, ours)
        self.assertEqual(result['global_setting'], old['global_setting'])
        self.assertEqual(result['devices'][1], old['devices'][1])
        self.assertEqual(result['devices'][0]['custom'], 'keep')
        self.assertEqual(result['devices'][0]['temps'][0]['custom'], 'keep')
        self.assertNotIn('set_duty', result['devices'][0]['channels'][0]['commands'])
        self.assertIn('set_duty', old['devices'][0]['channels'][0]['commands'])
        with self.assertRaises(RuntimeError):
            tx.merge_config({'devices': []}, ours)

    def test_systemd_arguments_escape_percent_dollar_and_quotes(self):
        self.assertEqual(tx.systemd_word('a $b %i "c"'), '"a $$b %%i \\"c\\""')
        with self.assertRaises(RuntimeError):
            tx.systemd_word('bad\nExecStart=bad')


class Transactions(TemporaryRoot):
    def is_directory(self, name):
        return name == tx.PREFIX or name.endswith('.d') or name.endswith('/custom-device')

    def test_restore_all_paths_and_absence_twice(self):
        paths = tx.transaction_paths(self.layout)
        for name in paths[:-1]:
            self.put(name + '/user-setting' if self.is_directory(name) else name)
        tx.snapshot(self.backup, self.root, self.layout, PROJECT)
        for name in paths:
            self.put(name + '/user-setting' if self.is_directory(name) else name, 'replacement')
        self.put(tx.REMOVABLE, 'new override')
        tx.restore(self.backup, self.root)
        tx.restore(self.backup, self.root)
        for name in paths[:-1]:
            target = self.root / name.lstrip('/')
            self.assertEqual((target / 'user-setting' if self.is_directory(name) else target).read_text(), 'original')
        self.assertFalse((self.root / paths[-1].lstrip('/')).exists())
        self.assertFalse((self.root / tx.REMOVABLE.lstrip('/')).exists())
        self.assertEqual(self.backup.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.backup / 'recovery/install.sh').read_text(), (PROJECT / 'install.sh').read_text())

    def test_metadata_symlinks_token_foreign_and_unrelated_plugin_preserved(self):
        path = self.put('/etc/coolercontrol/config.toml')
        path.chmod(0o640)
        os.utime(path, ns=(123456789000, 123456789000))
        link = self.root / tx.CLIENT.lstrip('/')
        link.parent.mkdir(parents=True)
        link.symlink_to('old-client')
        keep = [self.put('/etc/modprobe.d/isw-ec_sys.conf'),
                self.put('/etc/modprobe.d/colorful-n14-control.conf'),
                self.put('/etc/colorful-n14-control/coolercontrol-token.json', 'secret'),
                self.put('/var/lib/coolercontrol/plugins/other/config.json')]
        override = self.put(tx.REMOVABLE)
        tx.snapshot(self.backup, self.root, self.layout)
        path.write_text('new')
        link.unlink()
        override.unlink()
        tx.restore(self.backup, self.root)
        self.assertEqual(path.stat().st_mode & 0o777, 0o640)
        self.assertEqual(path.stat().st_mtime_ns, 123456789000)
        self.assertEqual(os.readlink(link), 'old-client')
        self.assertEqual(override.read_text(), 'original')
        for target in keep:
            self.assertTrue(target.exists())
            self.assertFalse((self.backup / 'files' / target.relative_to(self.root)).exists())

    def test_partial_backup_cannot_restore(self):
        target = self.put(tx.CLIENT)
        with patch.object(tx, 'copy', side_effect=OSError('disk full')), self.assertRaises(OSError):
            tx.snapshot(self.backup, self.root, self.layout)
        with self.assertRaises(FileNotFoundError):
            tx.restore(self.backup, self.root)
        self.assertEqual(target.read_text(), 'original')

    def test_existing_backup_not_overwritten(self):
        tx.snapshot(self.backup, self.root, self.layout)
        with self.assertRaises(FileExistsError):
            tx.snapshot(self.backup, self.root, self.layout)

    def test_symlink_parent_rejected(self):
        (self.root / 'etc').symlink_to(self.tmp.name)
        with self.assertRaisesRegex(RuntimeError, 'symlink parent'):
            tx.snapshot(self.backup, self.root, self.layout)

    def test_corrupt_or_incomplete_backup_validated_before_any_mutation(self):
        target = self.put(tx.CLIENT)
        tx.snapshot(self.backup, self.root, self.layout)
        saved = self.backup / 'files' / tx.CLIENT.lstrip('/')
        saved.write_text('corrupt')
        target.write_text('keep')
        with self.assertRaisesRegex(RuntimeError, 'damaged'):
            tx.restore(self.backup, self.root)
        self.assertEqual(target.read_text(), 'keep')
        saved.unlink()
        with self.assertRaisesRegex(RuntimeError, 'incomplete'):
            tx.restore(self.backup, self.root)
        self.assertEqual(target.read_text(), 'keep')

    def test_manifest_cannot_escape_or_add_unrelated_paths(self):
        tx.snapshot(self.backup, self.root, self.layout)
        manifest = json.loads((self.backup / 'manifest.json').read_text())
        manifest['files'][0]['path'] = '/etc/passwd'
        tx.save_json(self.backup / 'manifest.json', manifest)
        with self.assertRaisesRegex(RuntimeError, 'invalid transaction'):
            tx.restore(self.backup, self.root)
        with self.assertRaises(RuntimeError):
            tx.absolute('/etc/../etc/passwd')


class StagingAndApply(TemporaryRoot):
    def fake_release(self, source):
        checksums = {}
        for name in tx.RELEASES:
            path = self.put(name, b'fake binary', source)
            path.chmod(0o755)
            checksums[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        return checksums

    def stage_fixture(self):
        self.plugin_fixture(alias=True)
        source = Path(self.tmp.name) / 'source'
        for name in tx.SOURCES:
            tx.copy(PROJECT / name, source / name)
        tx.copy(PROJECT / 'tests', source / 'tests')
        checksums = self.fake_release(source)
        directory = Path(self.tmp.name) / 'stage'
        directory.mkdir(mode=0o700)
        with patch.object(tx, 'verified_releases', return_value=checksums), patch.object(tx, 'show', side_effect=self.show):
            tx.stage(source, directory, root=self.root)
        return directory

    def test_complete_stage_apply_restore_and_no_token_write(self):
        directory = self.stage_fixture()
        layout = json.loads((directory / 'layout.json').read_text())
        tree = directory / 'tree'
        for name in tx.MODULES:
            self.assertTrue((tree / tx.PREFIX.lstrip('/') / name).is_file())
        dropin = (tree / 'etc/systemd/system/coolercontrold.service.d/colorful-n14-control.conf').read_text()
        self.assertIn('Wants=' + tx.SELECTOR, dropin)
        self.assertIn('ExecStart=\nExecStart="' + tx.DAEMON, dropin)
        self.assertIn('CC_PLUGINS_DIR=', dropin)
        self.put(tx.REMOVABLE)
        token = self.put('/etc/colorful-n14-control/coolercontrol-token.json', 'do not copy or replace')
        tx.snapshot(self.backup, self.root, layout, directory / 'source')
        self.put('adapter-client', 'compiled client', directory)
        self.put('profile-bindings.json', '{"ids":"only"}', directory)
        tx.apply(directory, self.backup, self.root)
        self.assertEqual((self.root / tx.DAEMON.lstrip('/')).stat().st_mode & 0o777, 0o755)
        self.assertEqual((self.root / tx.BINDINGS.lstrip('/')).stat().st_mode & 0o777, 0o600)
        self.assertEqual(token.read_text(), 'do not copy or replace')
        self.assertFalse((self.root / tx.REMOVABLE.lstrip('/')).exists())
        plugin = self.root / layout['manifest_dir'].lstrip('/')
        self.assertEqual(tomllib.loads((plugin / 'manifest.toml').read_text())['executable'], tx.PLUGIN)
        tx.restore(self.backup, self.root)
        self.assertEqual((plugin / 'manifest.toml').read_text(), MANIFEST)
        self.assertFalse((self.root / tx.DAEMON.lstrip('/')).exists())
        self.assertTrue((self.root / tx.REMOVABLE.lstrip('/')).exists())
        self.assertEqual(token.read_text(), 'do not copy or replace')

    def test_apply_failure_is_restorable_and_does_not_touch_symlink_destination(self):
        directory = self.stage_fixture()
        layout = json.loads((directory / 'layout.json').read_text())
        outside = self.put('/outside-client', 'untouched')
        client = self.root / tx.CLIENT.lstrip('/')
        client.parent.mkdir(parents=True)
        client.symlink_to(outside)
        tx.snapshot(self.backup, self.root, layout)
        self.put('adapter-client', 'compiled', directory)
        self.put('profile-bindings.json', '{}', directory)
        replace = os.replace
        count = 0
        def fail_later(source, target):
            nonlocal count
            count += 1
            if count == 6:
                raise OSError('simulated disk failure')
            replace(source, target)
        with patch.object(tx.os, 'replace', side_effect=fail_later), self.assertRaises(OSError):
            tx.apply(directory, self.backup, self.root)
        tx.restore(self.backup, self.root)
        self.assertTrue(client.is_symlink())
        self.assertEqual(outside.read_text(), 'untouched')


class Releases(TemporaryRoot):
    def setUp(self):
        super().setUp()
        lines = []
        for name in (*tx.RELEASES, *tx.PATCHES):
            content = b'\x7fELF\x02\x01' + bytes(12) + b'\x3e\x00' if name in tx.RELEASES else b'patch'
            self.put(name, content).chmod(0o755)
            lines.append(hashlib.sha256(content).hexdigest() + '  nbfc-n14-control/' + name)
        self.put('patches/BUILD-SHA256SUMS', '\n'.join(lines))
        for name in ('daemon-tests', 'plugin-tests', 'daemon-release', 'plugin-release'):
            self.put('build-verification/' + name + '.exit', '0\n')

    def test_exact_hashes_elf_and_link_dependencies(self):
        with patch.object(tx, 'run', return_value='libc => found') as command:
            self.assertEqual(len(tx.verified_releases(self.root)), 4)
        self.assertEqual(command.call_count, 2)

    def test_missing_library_refuses(self):
        with patch.object(tx, 'run', return_value='libfoo => not found'), self.assertRaisesRegex(RuntimeError, 'shared-library'):
            tx.verified_releases(self.root)

    def test_tampered_binary_refuses_before_execution(self):
        self.put(next(iter(tx.RELEASES)), 'tampered')
        with patch.object(tx, 'run') as command, self.assertRaisesRegex(RuntimeError, 'checksum mismatch'):
            tx.verified_releases(self.root)
        command.assert_not_called()

    def test_missing_receipt_or_manifest_entry_refuses(self):
        self.put('build-verification/plugin-tests.exit', '1')
        with patch.object(tx, 'run', return_value='linked'), self.assertRaisesRegex(RuntimeError, 'receipt'):
            tx.verified_releases(self.root)
        self.put('patches/BUILD-SHA256SUMS', '')
        with self.assertRaisesRegex(RuntimeError, 'incomplete'):
            tx.verified_releases(self.root)


class Health(unittest.TestCase):
    def run_health(self, samples, timeout=15):
        now = [0]
        def sleep(seconds):
            now[0] += seconds
        return tx.health(timeout, Mock(side_effect=samples), lambda: now[0], sleep)

    def healthy(self, n=8, phase='awaiting'):
        return [dict(identity=['epoch', 'assignment'], sample=i, lease=i + 15, phase=phase) for i in range(n)]

    def test_stable_awaiting_or_active_allowed_with_renewal_and_fresh_samples(self):
        for phase in ('awaiting', 'active'):
            self.assertEqual(self.run_health([ValueError('starting'), *self.healthy(phase=phase)]), phase)

    def test_closed_stale_or_no_renewals_timeout(self):
        for samples in ([ValueError('closed')] * 20, [self.healthy()[0]] * 20,
                        [dict(s, lease=15) for s in self.healthy(20)]):
            with self.assertRaisesRegex(RuntimeError, 'deadline'):
                self.run_health(samples)

    def test_repeated_mismatches_refused(self):
        with self.assertRaisesRegex(RuntimeError, 'repeated'):
            self.run_health([self.healthy()[0], ValueError('mismatch'), ValueError('mismatch'), ValueError('mismatch')])

    def test_deadline_cannot_exceed_cap(self):
        with self.assertRaises(RuntimeError):
            self.run_health([], timeout=60)

    def test_live_preflight_policy_poll_rate_without_put_or_reset(self):
        from protocol_fixtures import Harness, BINDINGS
        import profile_selector
        import profile_selector_service
        h = Harness()
        self.addCleanup(h.close)
        with patch.object(profile_selector, 'LocalTransport', return_value=h.api), \
                patch.object(profile_selector_service, 'read_bindings', return_value=BINDINGS):
            tx.check_bindings('/offline-bindings')
            h.api.poll_rate = 2
            with self.assertRaises(ValueError):
                tx.check_bindings('/offline-bindings')
        self.assertEqual(h.api.puts, [])
        h.writes.assert_not_called()

    def test_live_health_uses_actual_assignment_and_fence_readonly(self):
        from protocol_fixtures import Harness, BINDINGS
        import profile_selector
        import profile_selector_service
        h = Harness()
        self.addCleanup(h.close)
        h.select()
        old_writes, old_puts = h.writes.call_count, len(h.api.puts)
        with ExitStack() as stack:
            stack.enter_context(patch.object(tx, 'active', return_value=True))
            stack.enter_context(patch.object(tx, 'show', return_value='123'))
            # Explicit two-read sequence per health tick.
            stack.enter_context(patch.object(tx.os, 'readlink', side_effect=[tx.DAEMON, tx.PLUGIN] * 2))
            stack.enter_context(patch.object(profile_selector, 'LocalTransport', return_value=h.api))
            stack.enter_context(patch.object(profile_selector_service, 'AdapterFenceRPC', return_value=h.rpc))
            stack.enter_context(patch.object(profile_selector_service, 'read_bindings', return_value=BINDINGS))
            self.assertEqual(tx.health_sample()['phase'], 'awaiting')
            h.api.applied = 'wrong'
            with self.assertRaisesRegex(RuntimeError, 'mismatch'):
                tx.health_sample()
        self.assertEqual(h.writes.call_count, old_writes)
        self.assertEqual(len(h.api.puts), old_puts)


class RollbackLifecycle(TemporaryRoot):
    def run_recovery(self, failure='', selector_missing=False):
        text = (PROJECT / 'install.sh').read_text()
        function = text[text.index('recover_backup() {'):text.index('# --restore is usable')]
        # Evaluate only the function. All external actions are shell functions;
        # PATH is empty, so a regression cannot contact systemd or hardware.
        script = '''
PATH=/nonexistent
PREPARE_UNIT=prepare
SENSORS_UNIT=sensors
ADAPTER_UNIT=adapter
SELECTOR_UNIT=selector
PLUGIN_UNIT=plugin
PREREQUISITE_UNIT=prereq
CLIENT=/fake-client
ctl() {
  printf 'ctl %s\n' "$*" >&2
  if [[ $1 == show ]]; then
    if [[ $2 == selector && $MISSING == yes ]]; then printf 'not-found\n'; else printf 'loaded\n'; fi
  fi
  [[ "ctl $*" != "$FAILURE" ]]
}
python3() {
  printf '%s\n' "$3" >&2
  [[ "$3" != "$FAILURE" ]] || return 1
  if [[ $3 == units ]]; then printf 'prepare\nsensors\nadapter\nplugin\ncoolercontrold.service\n'; fi
}
timeout() { printf 'reset\n' >&2; [[ $FAILURE != reset ]]; }
''' + function + '\nrecover_backup "$BACKUP"\n'
        self.backup.mkdir(exist_ok=True)
        return subprocess.run(['/bin/bash', '-c', script], capture_output=True, text=True,
                              env={'FAILURE': failure, 'BACKUP': str(self.backup),
                                   'MISSING': 'yes' if selector_missing else 'no'})

    def test_restore_and_restart_order(self):
        result = self.run_recovery()
        self.assertEqual(result.returncode, 0, result.stderr)
        actions = result.stderr.splitlines()
        expected = ['ctl stop selector', 'ctl stop coolercontrold.service', 'ctl stop plugin',
                    'reset', 'ctl stop adapter', 'ctl stop sensors', 'ctl stop prepare',
                    'restore', 'ctl daemon-reload', 'ctl start prepare', 'ctl start sensors',
                    'ctl start adapter', 'ctl start plugin', 'ctl start coolercontrold.service']
        positions = [actions.index(item) for item in expected]
        self.assertEqual(positions, sorted(positions))
        self.assertNotIn('ctl start selector', actions)
        self.assertEqual((self.backup / 'result').read_text(), 'restored\n')

    def test_stop_or_reset_failure_never_restores_under_live_process(self):
        for failure in ('ctl stop selector', 'ctl stop coolercontrold.service', 'ctl stop plugin',
                        'reset', 'ctl stop adapter', 'ctl stop sensors', 'ctl stop prepare'):
            with self.subTest(failure=failure):
                result = self.run_recovery(failure)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn('\nrestore\n', result.stderr)
                self.assertNotIn('ctl start', result.stderr)

    def test_invalid_backup_does_not_stop_services(self):
        result = self.run_recovery('validate')
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('ctl ', result.stderr)

    def test_restore_failure_never_starts_partial_backend(self):
        result = self.run_recovery('restore')
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('ctl start', result.stderr)

    def test_start_failure_never_starts_downstream_consumers(self):
        for unit, forbidden in (('prepare', 'sensors'), ('sensors', 'adapter'),
                                ('adapter', 'plugin'), ('plugin', 'coolercontrold.service')):
            result = self.run_recovery('ctl start ' + unit)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn('ctl start ' + forbidden, result.stderr)

    def test_missing_old_selector_is_supported(self):
        result = self.run_recovery(selector_missing=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('ctl stop selector', result.stderr)


class DeploymentFailureTrap(TemporaryRoot):
    def test_health_failure_runs_backed_up_recovery_not_checkout(self):
        text = (PROJECT / 'install.sh').read_text()
        recovery = text[text.index('recover_backup() {'):text.index('# --restore is usable')]
        deployment = text[text.index('TRANSACTION_STARTED=0'):]
        self.backup.mkdir()
        script = '''
set -Eeuo pipefail
PATH=/nonexistent
PREPARE_UNIT=prepare
SENSORS_UNIT=sensors
ADAPTER_UNIT=adapter
SELECTOR_UNIT=selector
PLUGIN_UNIT=plugin
PREREQUISITE_UNIT=prereq
RECOVERY_UNIT=recovery
CLIENT=/fake-client
PREFIX=/fake-prefix
TX=/staged/install_transaction.py
STAGING=/staged
ctl() {
  printf 'ctl %s\\n' "$*" >&2
  if [[ $1 == show ]]; then printf 'loaded\\n'; fi
  if [[ $1 == stop && $2 == recovery.timer ]]; then printf 'stopped\\n' > "$BACKUP_ROOT/timer-stopped"; fi
  return 0
}
python3() {
  printf 'python %s\\n' "$*" >&2
  if [[ $3 == health ]]; then return 1; fi
  if [[ $3 == units ]]; then printf 'prepare\\nsensors\\nadapter\\nplugin\\ncoolercontrold.service\\n'; fi
  return 0
}
timeout() {
  shift 2
  if [[ $1 == python3 ]]; then "$@"; else printf 'reset\\n' >&2; fi
}
systemd-run() { printf 'timer-armed\\n' >&2; }
''' + recovery + '\n' + deployment
        result = subprocess.run(['/bin/bash', '-c', script], capture_output=True, text=True,
                                env={'BACKUP_ROOT': str(self.backup)})
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('timer-armed', result.stderr)
        self.assertIn('/fake-prefix/install_transaction.py health', result.stderr)
        self.assertIn(str(self.backup) + '/recovery/install_transaction.py restore', result.stderr)
        self.assertNotIn('/staged/install_transaction.py restore', result.stderr)
        self.assertEqual((self.backup / 'result').read_text(), 'restored\n')
        self.assertNotIn('Installed EC release', result.stdout)
        self.assertEqual((self.backup / 'timer-stopped').read_text(), 'stopped\n')


class RecoveryOwner(TemporaryRoot):
    def test_recycled_pid_never_signalled(self):
        import signal
        self.backup.mkdir()
        tx.save_json(self.backup / 'owner.json', dict(pid=123, start='old'))
        for start, count in (('new', 0), ('old', 1)):
            with patch.object(tx.os, 'pidfd_open', return_value=99), patch.object(tx.os, 'close'), \
                    patch.object(tx, 'process_start', return_value=start), patch.object(signal, 'pidfd_send_signal') as send:
                tx.interrupt_owner(self.backup)
                self.assertEqual(send.call_count, count)


class InstallerPreflight(unittest.TestCase):
    def test_incomplete_environment_refuses_before_any_external_command(self):
        result = subprocess.run(['/bin/bash', str(PROJECT / 'install.sh')],
                                env={'PATH': '/nonexistent'}, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn('preflight refusal', result.stderr)
        self.assertIn('installed cooling has not been changed', result.stderr)
        self.assertNotIn('command not found', result.stderr)
        self.assertNotIn('not deployable yet', result.stderr)

    def test_shell_syntax(self):
        subprocess.run(['/bin/bash', '-n', str(PROJECT / 'install.sh')], check=True)

    def test_preflight_backup_recovery_stop_health_order_and_no_global_changes(self):
        text = (PROJECT / 'install.sh').read_text()
        begin = text.index('TRANSACTION_STARTED=1')
        deployment = text[begin:]
        for marker in ('unittest discover', 'prepare.py" check', 'install_prerequisites.py"',
                       'capture_profile_bindings.py"', 'cc -std=c11', 'snapshot "$BACKUP_ROOT"',
                       'systemd-run --quiet', 'is-active --quiet "$RECOVERY_UNIT.timer"'):
            self.assertLess(text.index(marker), begin, marker)
        ordered = ['ctl stop coolercontrold.service', 'ctl stop "$PLUGIN_UNIT"',
                   '"$CLIENT" reset', 'ctl stop "$ADAPTER_UNIT"', 'ctl stop "$SENSORS_UNIT"',
                   '"$TX" apply', 'ctl start "$PREPARE_UNIT"', 'ctl start "$SENSORS_UNIT"',
                   'ctl start "$ADAPTER_UNIT"', 'ctl start coolercontrold.service',
                   'ctl is-active --quiet "$PLUGIN_UNIT"', '"$PREFIX/install_transaction.py" health',
                   "printf 'committed\\n'"]
        indices = [deployment.index(marker) for marker in ordered]
        self.assertEqual(indices, sorted(indices))
        for forbidden in ('systemctl enable', 'systemctl disable', 'modprobe -r', 'write_support=0',
                          'persistent full-fan/sole-owner fence is not integrated'):
            self.assertNotIn(forbidden, text)
        self.assertIn('58s python3', text)
        self.assertIn('trap rollback ERR', text)
        self.assertIn('recovery/install.sh" --recover', text)
        self.assertIn('recovery/install.sh" "$BACKUP_ROOT"', text)


if __name__ == '__main__':
    unittest.main()

"""Fail-closed guards for the explicit disabled-CONFIG Redis recipe."""
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock


spec = importlib.util.spec_from_file_location('redis_state_tests', Path(__file__).parents[1] / 'redis_state.py')
state = importlib.util.module_from_spec(spec)
spec.loader.exec_module(state)


class RedisStateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='farm-redis-state-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / 'dump.rdb'
        self.source.write_bytes(b'fixture')
        self.file = self.root / 'redis.conf'
        self.text = (f'dir {self.root}\ndbfilename dump.rdb\nappendonly no\n'
                     'rename-command CONFIG ""\nrequirepass never-echo-this-secret\n')
        self.file.write_text(self.text)
        self.config = {'persistence_verification': 'disabled-config', 'config_file': str(self.file),
                       'service_unit': 'redis-server.service', 'host': '127.0.0.1', 'port': 6379}
        self.context = {'pid': 123, 'started_ns': self.file.stat().st_ctime_ns + 1_000_000_000,
                        'started_monotonic_us': 10_000_000, 'unit_file': {'fixture': True},
                        'executable': '/usr/bin/redis-server', 'invocation_id': 'a' * 32}
        self.process = {'started_ticks': 10 * os.sysconf('SC_CLK_TCK'), 'directory_dev': 1,
                        'directory_inode': 2, 'executable_dev': 1, 'executable_inode': 3}
        self.server = {'redis_version': '6.0.16', 'redis_mode': 'standalone', 'process_id': '123',
                       'config_file': str(self.file), 'tcp_port': '6379', 'run_id': 'b' * 40,
                       'executable': '/usr/bin/redis-server'}
        self.persistence = {'loading': '0', 'aof_enabled': '0'}
        self.reply = b'ERR unknown command `CONFIG`, with args beginning with: `GET`, `dir`, \n\n'
        self.calls = []

    def redis_run(self, argv, *, timeout):
        self.calls.append(argv)
        self.assertEqual(timeout, 30)
        if argv[-3:] == ['CONFIG', 'GET', 'dir']:
            return self.reply
        if argv[-2:] == ['INFO', 'server']:
            fields = self.server
        elif argv[-2:] == ['INFO', 'persistence']:
            fields = self.persistence
        else:
            raise AssertionError('Unexpected or mutating command')
        return ('# Fixture\r\n' + ''.join(f'{key}:{value}\r\n' for key, value in fields.items())).encode()

    def verify(self, run=None):
        with mock.patch.object(state, '_systemd_context', return_value=self.context), \
                mock.patch.object(state, '_process_context', return_value=self.process):
            return state.verify_disabled_config(self.config, self.source, run or self.redis_run, 30)

    def rejected(self):
        with self.assertRaises(state.RedisVerificationError) as caught:
            self.verify()
        self.assertNotIn('never-echo', str(caught.exception))

    def test_valid_identity_is_stable_and_secret_free(self):
        before = self.verify()
        self.assertEqual(before, self.verify())
        self.assertNotIn('never-echo', str(before))
        self.assertEqual(before['source'], str(self.source))
        self.assertEqual(len(self.calls), 6)

    def test_explicit_selection_and_required_policy(self):
        for key in ('persistence_verification', 'config_file', 'service_unit'):
            with self.subTest(key=key):
                original = self.config.pop(key)
                self.rejected()
                self.config[key] = original

    def test_config_must_be_older_than_running_process(self):
        self.context['started_ns'] = self.file.stat().st_ctime_ns
        self.rejected()
        self.assertEqual(self.calls, [])

    def test_includes_ambiguous_names_and_modules_fail_before_redis_calls(self):
        for line in ('include /private/secret.conf', '"include" /private/secret.conf',
                     '"\\x64ir" /other', 'loadmodule /private/module.so', '# comment\0'):
            with self.subTest(line=line):
                self.file.write_text(self.text + line + '\n')
                self.rejected()
        self.assertEqual(self.calls, [])

    def test_duplicate_path_aof_and_renamed_config_are_rejected(self):
        changes = [self.text + 'dbfilename other.rdb\n',
                   self.text.replace('dbfilename dump.rdb', 'dbfilename other.rdb'),
                   self.text.replace('appendonly no', 'appendonly yes'),
                   self.text.replace('CONFIG ""', 'CONFIG never-echo-this-secret'),
                   self.text.replace('rename-command CONFIG ""\n', ''),
                   self.text.replace(f'dir {self.root}', 'dir /wrong'),
                   self.text.replace('dbfilename dump.rdb', 'dbfilename ../dump.rdb')]
        for text in changes:
            with self.subTest(text=text):
                self.file.write_text(text)
                self.rejected()
        self.assertEqual(self.calls, [])

    def test_only_exact_unknown_config_response_is_accepted(self):
        for response in (b'NOAUTH Authentication required.', b'NOPERM denied', b'ERR unknown command OTHER',
                         b'dir\n/var/lib/redis\n', b'', b'ERR unknown command CONFIG'):
            with self.subTest(response=response):
                self.reply = response
                self.rejected()

    def test_live_identity_mismatches_are_rejected(self):
        for key, value in (('process_id', '456'), ('config_file', '/other'), ('tcp_port', '6380'),
                           ('redis_mode', 'cluster'), ('redis_version', '7.0.0'), ('run_id', 'bad'),
                           ('executable', '/another/redis-server')):
            with self.subTest(key=key):
                original, self.server[key] = self.server[key], value
                self.rejected()
                self.server[key] = original

    def test_live_aof_loading_and_missing_fields_fail(self):
        for values in ({'loading': '0', 'aof_enabled': '1'}, {'loading': '1', 'aof_enabled': '0'},
                       {'aof_enabled': '0'}):
            self.persistence = values
            self.rejected()

    def test_kernel_start_mismatch_is_rejected(self):
        self.process['started_ticks'] += 2 * os.sysconf('SC_CLK_TCK')
        self.rejected()

    def test_mid_verification_config_change_is_rejected(self):
        def changed(argv, **kwargs):
            result = self.redis_run(argv, **kwargs)
            if argv[-2:] == ['INFO', 'persistence']:
                self.file.write_text(self.text + '# changed\n')
            return result
        with self.assertRaises(state.RedisVerificationError):
            self.verify(changed)

    def test_run_id_change_is_visible_in_returned_identity(self):
        before = self.verify()
        self.server['run_id'] = 'c' * 40
        self.assertNotEqual(before, self.verify())

    def test_symlink_config_and_world_writable_config_fail(self):
        original = self.file
        alias = self.root / 'alias.conf'
        alias.symlink_to(original)
        self.config['config_file'] = str(alias)
        self.rejected()
        self.config['config_file'] = str(original)
        original.chmod(0o666)
        self.rejected()

    def test_unexpected_exceptions_are_sanitized(self):
        def secret(*args, **kwargs):
            raise RuntimeError('never-echo-this-secret')
        with self.assertRaises(state.RedisVerificationError) as caught:
            self.verify(secret)
        self.assertNotIn('never-echo', str(caught.exception))

    def test_parser_matches_supported_simple_quoted_values(self):
        self.file.write_text(self.text.replace('dbfilename dump.rdb', 'dbfilename "dump.rdb"')
                             .replace('CONFIG ""', "CONFIG ''"))
        settings, _ = state._configuration(self.file)
        self.assertEqual(settings['dbfilename'], 'dump.rdb')

    def test_unrelated_simple_command_renames_are_allowed_and_not_recorded(self):
        self.file.write_text(self.text + 'rename-command DEBUG "opaque-replacement"\n')
        result = self.verify()
        self.assertNotIn('opaque-replacement', str(result))
        self.file.write_text(self.text + 'rename-command DEBUG "CONFIG"\n')
        self.rejected()


class SystemdContextTests(unittest.TestCase):
    def setUp(self):
        self.path = Path('/etc/redis/redis.conf')
        self.fields = {'Id': 'redis-server.service', 'LoadState': 'loaded', 'ActiveState': 'active',
                       'SubState': 'running', 'ControlPID': '0', 'MainPID': '123',
                       'NeedDaemonReload': 'no', 'DropInPaths': '', 'RootDirectory': '', 'RootImage': '',
                       'Transient': 'no', 'InvocationID': 'a' * 32,
                       'ExecMainStartTimestamp': 'Mon 2026-10-12 05:32:00.940983 UTC',
                       'ExecMainStartTimestampMonotonic': '152154502',
                       'FragmentPath': '/lib/systemd/system/redis-server.service',
                       'ExecStart': '{ path=/usr/bin/redis-server ; argv[]=/usr/bin/redis-server '
                                    '/etc/redis/redis.conf --supervised systemd --daemonize no ; '
                                    'ignore_errors=no ; start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; '
                                    'code=(null) ; status=0/0 }'}
        self.fingerprint = {'mtime_ns': 1, 'ctime_ns': 1, 'uid': 0, 'links': []}

    def context(self):
        def run(argv, **kwargs):
            self.assertEqual(argv[:3], ['/usr/bin/systemctl', 'show', 'redis-server.service'])
            return ''.join(f'{key}={value}\n' for key, value in self.fields.items()).encode()
        with mock.patch.object(state, '_read_file', return_value=(b'', self.fingerprint)):
            return state._systemd_context('redis-server.service', self.path, run, 30)

    def test_installed_format_and_lib_symlink_identity(self):
        result = self.context()
        self.assertEqual(result['pid'], 123)
        self.assertEqual(result['started_ns'], 1791783120940983000)

    def test_cli_overrides_and_wrapper_fail(self):
        original = self.fields['ExecStart']
        for value in (original.replace('--daemonize no', '--daemonize no --dbfilename other.rdb'),
                      original.replace('/usr/bin/redis-server', '/wrapper'),
                      original.replace('--supervised systemd', '--supervised $MODE'),
                      original + ' ' + original):
            self.fields['ExecStart'] = value
            with self.assertRaises(state.RedisVerificationError):
                self.context()

    def test_unloaded_changed_or_namespaced_service_fails(self):
        for key, value in (('NeedDaemonReload', 'yes'), ('DropInPaths', '/some/dropin'),
                           ('RootDirectory', '/chroot'), ('RootImage', '/image'),
                           ('ActiveState', 'inactive'), ('ControlPID', '123'), ('Transient', 'yes')):
            with self.subTest(key=key):
                original, self.fields[key] = self.fields[key], value
                with self.assertRaises(state.RedisVerificationError):
                    self.context()
                self.fields[key] = original

    def test_changed_unit_or_symlink_since_start_fails(self):
        for metadata in (self.fingerprint, {'mtime_ns': 1, 'ctime_ns': 2**63}):
            self.fingerprint['links'] = [] if metadata is self.fingerprint else [metadata]
            if metadata is self.fingerprint:
                metadata['ctime_ns'] = 2**63
            with self.assertRaises(state.RedisVerificationError):
                self.context()
            self.fingerprint['ctime_ns'] = 1


if __name__ == '__main__':
    unittest.main()

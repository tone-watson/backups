"""Verify this host's Redis 6 persistence when CONFIG is deliberately disabled.

This is an explicit, conservative alternative to CONFIG GET, not a fallback for
arbitrary Redis errors. It never changes Redis configuration or service state.
The recipe is pinned to the inspected Redis 6.0.16 behavior. Only fixed
diagnostic errors and stable, non-secret identity metadata escape.
"""
from datetime import datetime, timezone
import hashlib
import ipaddress
import os
from pathlib import Path
import re
import stat


class RedisVerificationError(RuntimeError):
    pass


def _fail(message):
    raise RedisVerificationError(message)


def _plain(value):
    path = Path(value)
    if (not path.is_absolute() or '..' in path.parts or path.resolve() != path
            or not re.fullmatch(r'/[A-Za-z0-9_./-]+', str(path))):
        _fail('Redis verification requires plain absolute paths')
    return path


def _metadata(info):
    return {name: getattr(info, 'st_' + name) for name in
            ('dev', 'ino', 'size', 'mtime_ns', 'ctime_ns', 'uid', 'gid', 'mode')}


def _read_file(path, *, aliases=False):
    """Bounded read with stable metadata; retain neither config text nor secrets."""
    path = Path(path)
    links = []
    if aliases:

        for candidate in reversed((path, *path.parents)):
            info = candidate.lstat()
            if stat.S_ISLNK(info.st_mode):
                links.append({'path': str(candidate), **_metadata(info)})
        path = path.resolve(strict=True)
    else:
        path = _plain(path)
    with path.open('rb') as stream:
        before = os.fstat(stream.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_size > 2 * 1024 * 1024
                or before.st_mode & 0o022):
            _fail('Redis configuration or unit file has unsupported metadata')
        content = stream.read(2 * 1024 * 1024 + 1)
        after = os.fstat(stream.fileno())
    if (_metadata(before) != _metadata(after) or _metadata(after) != _metadata(path.stat())
            or len(content) != after.st_size):
        _fail('Redis configuration or unit file changed while being inspected')
    fingerprint = {'path': str(path), **_metadata(after),
                   'sha256': hashlib.sha256(content).hexdigest(), 'links': links}
    return content, fingerprint


def _configuration(path):
    content, fingerprint = _read_file(path)
    if b'\0' in content:
        _fail('Redis config contains unsupported directive syntax')
    settings = {}
    disabled = False
    for line in content.decode('utf-8', 'strict').split('\n'):
        line = line.strip(' \t\r\n')
        if not line or line.startswith('#'):
            continue


        matched = re.fullmatch(r'([A-Za-z][A-Za-z0-9-]*)(?:[ \t]+(.*))?', line)
        if matched is None:
            _fail('Redis config contains unsupported directive syntax')
        key, value = matched.group(1).lower(), matched.group(2) or ''
        if key in {'include', 'loadmodule'}:
            _fail('Redis disabled-CONFIG verification does not support includes or modules')
        if key == 'rename-command':
            rename = re.fullmatch(r'([A-Za-z][A-Za-z0-9_-]*)[ \t]+(.*)', value)
            if rename is None:
                _fail('Redis command renaming syntax is unsupported')
            replacement = rename.group(2)
            if len(replacement) >= 2 and replacement[0] == replacement[-1] and replacement[0] in {'"', "'"}:
                replacement = replacement[1:-1]
            if replacement and not re.fullmatch(r'[A-Za-z0-9_-]+', replacement):
                _fail('Redis command renaming syntax is unsupported')
            if replacement.lower() == 'config':
                _fail('Redis CONFIG disabling must be unambiguous')
            if rename.group(1).lower() == 'config':
                if disabled or replacement or rename.group(2) not in {'""', "''"}:
                    _fail('Redis CONFIG disabling must be unambiguous')
                disabled = True
        elif key in {'dir', 'dbfilename', 'appendonly'}:
            if key in settings:
                _fail('Redis persistence directives must be unique')
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
                value = value[1:-1]
            if not value or not re.fullmatch(r'[A-Za-z0-9_./-]+', value):
                _fail('Redis persistence directive syntax is unsupported')
            settings[key] = value
    if not disabled or set(settings) != {'dir', 'dbfilename', 'appendonly'}:
        _fail('Redis disabled-CONFIG persistence declarations are incomplete')
    if settings['appendonly'].lower() != 'no':
        _fail('Redis AOF persistence is unsupported by this capture recipe')
    filename = settings['dbfilename']
    if filename in {'.', '..'} or Path(filename).name != filename:
        _fail('Redis RDB filename must be a plain basename')
    settings['dir'] = str(_plain(settings['dir']))
    return settings, fingerprint


def _fields(output, separator):
    fields = {}
    for line in output.decode('utf-8', 'strict').splitlines():
        if not line or line.startswith('#'):
            continue
        key, found, value = line.partition(separator)
        if not found or key in fields:
            _fail('Redis verification returned an invalid diagnostic response')
        fields[key] = value
    return fields


def _systemd_context(service_unit, config_file, run, timeout):
    """Test seam: authenticate the systemd launch; no process or Redis mutation.

    Isolated Redis tests may replace only this helper with an equivalent fixture
    launch record, leaving config, INFO and /proc validation real.
    """
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.@:-]*\.service', service_unit):
        _fail('Redis verification requires an explicit service unit')
    properties = ('Id,LoadState,ActiveState,SubState,MainPID,ControlPID,'
                  'ExecMainStartTimestamp,ExecMainStartTimestampMonotonic,NeedDaemonReload,'
                  'FragmentPath,DropInPaths,ExecStart,RootDirectory,RootImage,Transient,InvocationID')
    fields = _fields(run(['/usr/bin/systemctl', 'show', service_unit, '--no-pager',
                         '--timestamp=us', '--property=' + properties], timeout=timeout), '=')
    expected = {'Id': service_unit, 'LoadState': 'loaded', 'ActiveState': 'active',
                'SubState': 'running', 'ControlPID': '0', 'NeedDaemonReload': 'no',
                'DropInPaths': '', 'RootDirectory': '', 'RootImage': '', 'Transient': 'no'}
    if any(fields.get(key) != value for key, value in expected.items()):
        _fail('Redis service context is unsupported or not stable and running')
    argv = f'/usr/bin/redis-server {config_file} --supervised systemd --daemonize no'
    launch = fields.get('ExecStart', '')
    if not re.fullmatch(r'\{ path=/usr/bin/redis-server ; argv\[\]=' + re.escape(argv)
                        + r' ; ignore_errors=no ; [^{}]* \}', launch):
        _fail('Redis service launch has unsupported command-line overrides')
    timestamp = fields.get('ExecMainStartTimestamp', '')
    started = datetime.strptime(timestamp, '%a %Y-%m-%d %H:%M:%S.%f UTC').replace(tzinfo=timezone.utc)
    started_ns = int(started.timestamp()) * 1_000_000_000 + started.microsecond * 1000
    pid = int(fields['MainPID'])
    monotonic = int(fields['ExecMainStartTimestampMonotonic'])
    invocation = fields.get('InvocationID', '')
    if pid <= 0 or monotonic <= 0 or not re.fullmatch(r'[0-9a-f]{32}', invocation):
        _fail('Redis service launch identity is invalid')
    unit = Path(fields['FragmentPath'])
    if not unit.is_absolute() or unit.name != service_unit:
        _fail('Redis service fragment is unsupported')
    _, unit_file = _read_file(unit, aliases=True)
    if unit_file['uid'] != 0:
        _fail('Redis service fragment must be root-owned')
    _predates_start(unit_file, started_ns)
    return {'pid': pid, 'started_ns': started_ns, 'started_monotonic_us': monotonic,
            'unit_file': unit_file, 'executable': '/usr/bin/redis-server',
            'invocation_id': invocation}


def _predates_start(fingerprint, started_ns):
    for value in (fingerprint, *fingerprint.get('links', [])):
        if max(value['mtime_ns'], value['ctime_ns']) >= started_ns:
            _fail('Redis configuration or unit changed after this process started')


def _same_file(first, second):
    a, b = os.stat(first), os.stat(second)
    return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)


def _process_context(pid, config_file, directory, executable):
    """Read the real process identity, including its mount-namespace view."""
    process = Path('/proc') / str(pid)
    value = (process / 'stat').read_text()
    tail = value[value.rfind(')') + 2:].split()
    started_ticks = int(tail[19])
    real_executable = Path(executable).resolve(strict=True)
    if (os.readlink(process / 'exe') != str(real_executable)
            or not _same_file(process / 'exe', real_executable)):
        _fail('Redis process executable does not match its service launch')
    if os.readlink(process / 'cwd') != str(directory) or not _same_file(process / 'cwd', directory):
        _fail('Redis live working directory does not match its persistence directory')
    if not _same_file(process / 'root' / str(config_file).lstrip('/'), config_file):
        _fail('Redis process sees a different configuration file')
    info = os.stat(process / 'cwd')
    exe = os.stat(process / 'exe')
    return {'started_ticks': started_ticks, 'directory_dev': info.st_dev,
            'directory_inode': info.st_ino, 'executable_dev': exe.st_dev,
            'executable_inode': exe.st_ino}


def verify_disabled_config(config, source: Path, run, timeout) -> dict:
    """Return an equality-comparable identity for checks before and after export.

    ``run(argv, timeout=timeout)`` returns bytes. Exceptions never expose command
    output, config lines, renamed-command replacements, or secret values.
    """
    try:
        if config.get('persistence_verification') != 'disabled-config':
            _fail('Redis disabled-CONFIG verification must be explicitly selected')
        source = _plain(source)
        config_file = _plain(config['config_file'])
        service_unit = config['service_unit']
        host, port = config.get('host', '127.0.0.1'), config.get('port', 6379)
        if not ipaddress.ip_address(host).is_loopback or type(port) is not int or not 0 < port < 65536:
            _fail('Redis verification requires a loopback address and valid port')
        settings, fingerprint = _configuration(config_file)
        if Path(settings['dir']) / settings['dbfilename'] != source:
            _fail('Redis declared RDB path does not match the capture source')
        service = _systemd_context(service_unit, config_file, run, timeout)
        _predates_start(fingerprint, service['started_ns'])
        client = ['/usr/bin/redis-cli', '-h', host, '-p', str(port), '--raw']
        reply = run(client + ['CONFIG', 'GET', 'dir'], timeout=timeout).decode('utf-8', 'strict').strip()
        if reply != 'ERR unknown command `CONFIG`, with args beginning with: `GET`, `dir`,':
            _fail('Redis CONFIG is not confirmed disabled')
        server = _fields(run(client + ['INFO', 'server'], timeout=timeout), ':')
        persistence = _fields(run(client + ['INFO', 'persistence'], timeout=timeout), ':')
        if (server.get('redis_version') != '6.0.16' or server.get('redis_mode') != 'standalone'
                or server.get('process_id') != str(service['pid'])
                or server.get('config_file') != str(config_file)
                or server.get('tcp_port') != str(port)
                or server.get('executable') != service['executable']
                or not re.fullmatch(r'[0-9a-f]{40}', server.get('run_id', ''))):
            _fail('Redis live identity does not match its verified service')
        if persistence.get('aof_enabled') != '0' or persistence.get('loading') != '0':
            _fail('Redis must have AOF disabled and finish loading before capture')
        process = _process_context(service['pid'], config_file, source.parent, service['executable'])


        kernel_us = process['started_ticks'] * 1_000_000 // os.sysconf('SC_CLK_TCK')
        if abs(kernel_us - service['started_monotonic_us']) > 1_000_000:
            _fail('Redis kernel and service process start identities differ')
        if _configuration(config_file) != (settings, fingerprint):
            _fail('Redis configuration changed during verification')
        return {'method': 'disabled-config', 'service': service, 'process': process,
                'config_file': fingerprint, 'run_id': server['run_id'],
                'redis_version': server['redis_version'], 'source': str(source), 'aof_enabled': False}
    except RedisVerificationError:
        raise
    except Exception:
        raise RedisVerificationError('Redis disabled-CONFIG verification could not establish safe live settings') from None

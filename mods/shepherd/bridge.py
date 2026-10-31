#!/usr/bin/env python3
"""Shepherd's local worker. Python standard library; credentials never leave private state."""
import argparse
import contextlib
import datetime
import fcntl
import hashlib
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import ssl
import subprocess
import sys
import time
import urllib.parse
import uuid


class Error(Exception):
    pass


class StaleAttempt(Exception):
    """Raised by a worker whose job attempt has been superseded."""


@contextlib.contextmanager
def execution_lock(root, blocking=True):
    """One worker may own an operation at a time. Yield whether it was acquired."""
    with open(Path(root) / 'worker.lock', 'a') as handle:
        flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        try:
            fcntl.flock(handle, flags)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_name('.' + path.name + '.' + uuid.uuid4().hex)
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as out:
        json.dump(value, out, indent=2)
    os.replace(temp, path)


def load(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return default


class PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, port, fingerprint):
        self.fingerprint = fingerprint.replace(':', '').lower()
        if not re.fullmatch('[0-9a-f]{64}', self.fingerprint):
            raise Error('TLS fingerprint must be a SHA-256 certificate fingerprint.')
        super().__init__(host, port, timeout=15, context=ssl._create_unverified_context())

    def connect(self):
        super().connect()
        actual = hashlib.sha256(self.sock.getpeercert(binary_form=True)).hexdigest()
        if actual != self.fingerprint:
            self.close()
            raise Error('Proxmox TLS certificate fingerprint changed; check the server certificate.')


class API:
    def __init__(self, host):
        self.host = host
        self.url = urllib.parse.urlsplit(host['url'])
        if self.url.scheme != 'https' or not self.url.hostname or self.url.username or self.url.path not in ('', '/') or self.url.query or self.url.fragment:
            raise Error('Use an HTTPS Proxmox origin, e.g. https://server:8006.')

    def call(self, method, path, data=None):
        if method in ('GET', 'DELETE') and data is not None:
            query = urllib.parse.urlencode(data)
            if query:
                path += ('&' if '?' in path else '?') + query
            data = None
        fp = self.host.get('tlsFingerprint', '')
        conn = (PinnedHTTPS(self.url.hostname, self.url.port or 8006, fp) if fp else
                http.client.HTTPSConnection(self.url.hostname, self.url.port or 8006, timeout=15))
        body = json.dumps(data).encode() if data is not None else None
        headers = {'Authorization': 'PVEAPIToken=' + self.host['tokenId'] + '=' + self.host['tokenSecret']}
        if body is not None:
            headers['Content-Type'] = 'application/json'
        try:
            conn.request(method, '/api2/json/' + path, body, headers)
            response = conn.getresponse()
            raw = response.read()
            if response.status >= 400:
                # Never include headers, request body or token in errors.
                try:
                    detail = json.loads(raw).get('errors', {})
                except ValueError:
                    detail = {}
                raise Error('Proxmox HTTP %s on %s: %s %s' % (response.status, path, response.reason, detail))
            return json.loads(raw)['data']
        finally:
            conn.close()

    def get(self, path):
        return self.call('GET', path)

    def post(self, path, **data):
        return self.call('POST', path, data)

    def wait(self, node, task, timeout=900):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = self.get('nodes/%s/tasks/%s/status' % (node, urllib.parse.quote(task, safe='')))
            if status['status'] == 'stopped':
                if status.get('exitstatus') != 'OK':
                    raise Error('Proxmox task failed: ' + status.get('exitstatus', 'unknown'))
                return
            time.sleep(2)
        raise Error('Proxmox task timed out; inspect the VM before retrying.')


def task_status(api, node, task):
    return api.get('nodes/%s/tasks/%s/status' % (node, urllib.parse.quote(task, safe='')))


def settle_task(api, node, task):
    """Wait for a task and always validate exitstatus, including tasks already stopped."""
    status = task_status(api, node, task)
    if status['status'] != 'stopped':
        api.wait(node, task)
        status = task_status(api, node, task)
        if status['status'] != 'stopped':
            raise Error('Proxmox task did not finish; inspect the VM before retrying.')
    if status.get('exitstatus') != 'OK':
        raise Error('Proxmox task failed: ' + status.get('exitstatus', 'unknown'))
    return status


def config(root):
    value = load(root / 'config.json')
    if not value or not value.get('hypervisors'):
        raise Error('Configure a hypervisor first (Settings).')
    os.chmod(root / 'config.json', 0o600)
    ids = [h['id'] for h in value['hypervisors']]
    if len(ids) != len(set(ids)):
        raise Error('Duplicate hypervisor IDs.')
    return value


def host_config(root, host_id):
    for h in config(root)['hypervisors']:
        if h['id'] == host_id:
            return h
    raise Error('Unknown hypervisor.')


def inventory(host):
    api = API(host)
    nodes = api.get('nodes')
    resources = api.get('cluster/resources?type=vm')
    templates = [r for r in resources if r.get('type') == 'qemu' and r.get('template')]
    profiles = []
    for template in sorted(templates, key=lambda t: (t.get('name', ''), int(t['vmid']))):
        preset = next((p for p in host.get('profiles', [])
                       if (str(p.get('templateVmid')) == str(template['vmid'])
                           if p.get('templateVmid') is not None else p.get('template') == template.get('name'))), {})
        profiles.append(dict(preset, id='template-' + str(template['vmid']),
                             name=template.get('name') or str(template['vmid']),
                             template=template.get('name', ''), templateVmid=template['vmid'],
                             available=True, matches=1, node=template['node']))
    return {'nodes': nodes, 'profiles': profiles,
            'warning': '' if templates else 'No templates visible. Check token ACLs / Privilege Separation.'}


def hc_binary():
    for candidate in [shutil.which('hc'), str(Path.home() / '.local/bin/hc'),
                      str(Path.home() / '.local/bin/herdr-core'), '/opt/homebrew/bin/hc', '/usr/local/bin/hc']:
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    raise Error('Install hc on this computer first.')


def command(args, timeout=30):
    if args[0] == 'hc':
        args = [hc_binary()] + args[1:]
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise Error((result.stderr or result.stdout or 'Command failed')[-1600:].strip())
    return result.stdout


def job_known_hosts(root, job):
    """Per-job host key store, so a recycled DHCP address cannot collide. Legacy jobs keep the old path."""
    value = job.get('knownHosts')
    path = Path(value) if value else Path(root) / 'known_hosts'
    if not path.is_absolute():
        path = Path(root) / path
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def ssh_args(host, address, root, known_hosts=None):
    known_hosts = Path(known_hosts) if known_hosts else Path(root) / 'known_hosts'
    return ['ssh', '-i', str(Path(host['sshKey']).expanduser()), '-o', 'BatchMode=yes',
            '-o', 'IdentitiesOnly=yes', '-o', 'StrictHostKeyChecking=accept-new',
            '-o', 'UserKnownHostsFile=' + str(known_hosts),
            '-o', 'ConnectTimeout=5', '-o', 'ConnectionAttempts=1',
            host.get('sshUser', 'shepherd') + '@' + address]


def guest_address(api, base, expected_mac=None):
    result = api.get(base + '/agent/network-get-interfaces')
    candidates = []
    for interface in result.get('result', []):
        if expected_mac and interface.get('hardware-address', '').lower() != expected_mac.lower():
            continue
        for item in interface.get('ip-addresses', []):
            address = ipaddress.ip_address(item['ip-address'])
            if address.version == 4 and not (address.is_loopback or address.is_link_local or address.is_unspecified):
                candidates.append(str(address))
    if not candidates:
        raise Error('Waiting for a guest-agent IPv4 address on net0.')
    return candidates[0]


def guest_exec(api, base, script, timeout=120):
    result = api.post(base + '/agent/exec', command=['/bin/sh', '-c', script])
    pid = result['pid']
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = api.get(base + '/agent/exec-status?pid=' + str(pid))
        if result.get('exited'):
            if result.get('exitcode', -1) != 0:
                raise Error('Guest bootstrap failed: ' + result.get('err-data', result.get('out-data', str(result))))
            return result.get('out-data', '')
        time.sleep(2)
    raise Error('Guest bootstrap timed out.')


HOSTS_UPDATE_PROGRAM = r'''
import pathlib, sys
path, old, new = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3]
text = path.read_text()
old_names = {old, old.split('.')[0]}
lines, found = [], False
for line in text.splitlines():
    body, separator, comment = line.partition('#')
    fields = body.split()
    if len(fields) > 1 and fields[0] == '127.0.1.1':
        # Debian/Ubuntu's local hostname mapping, including the template's short alias.
        obsolete = old_names | {fields[1], fields[1].split('.')[0]}
        aliases = [v for v in fields[1:] if v not in obsolete and v != new]
        body = '127.0.1.1\t' + ' '.join([new] + aliases)
        line = body + ('  #' + comment if separator else '')
        found = True
    elif len(fields) > 1 and fields[0] in ('127.0.0.1', '::1'):
        aliases = [new if v in old_names and v != 'localhost' else v for v in fields[1:]]
        if aliases != fields[1:]:
            line = fields[0] + '\t' + ' '.join(dict.fromkeys(aliases)) + ('  #' + comment if separator else '')
    lines.append(line)
if not found:
    lines.append('127.0.1.1\t' + new)
result = '\n'.join(lines) + '\n'
if result != text:
    backup = path.with_name(path.name + '.before-shepherd')
    if not backup.exists():
        backup.write_text(text)
    path.write_text(result)
'''


def system_setup_script(user, name):
    if not re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}', user):
        raise Error('Invalid SSH username.')
    if not re.fullmatch(r'[a-z][a-z0-9-]{0,47}', name):
        raise Error('Invalid VM hostname.')
    script = [
        'set -eu',
        'command -v python3 >/dev/null || { apt-get update -qq; DEBIAN_FRONTEND=noninteractive apt-get install -y python3; }',
        'old_hostname=$(hostname)',
        'python3 -c ' + shlex.quote(HOSTS_UPDATE_PROGRAM) + ' /etc/hosts "$old_hostname" ' + shlex.quote(name),
        'hostnamectl set-hostname ' + shlex.quote(name),
    ]
    return '\n'.join(script)


def bootstrap_script(user, public_key, name):
    setup = system_setup_script(user, name)
    # Runs as root via authenticated QGA. No password or remote installer needed.
    return '\n'.join([
        'set -eu',
        'command -v sshd >/dev/null || { apt-get update -qq; DEBIAN_FRONTEND=noninteractive apt-get install -y openssh-server; }',
        'id -u ' + shlex.quote(user) + ' >/dev/null 2>&1 || useradd -m -s /bin/bash ' + shlex.quote(user),
        'home=$(getent passwd ' + shlex.quote(user) + " | cut -d: -f6)",
        'install -d -m 700 -o ' + shlex.quote(user) + ' -g "$(id -gn ' + shlex.quote(user) + ')" "$home/.ssh"',
        'touch "$home/.ssh/authorized_keys"',
        'grep -qxF ' + shlex.quote(public_key) + ' "$home/.ssh/authorized_keys" || printf "%s\\n" ' + shlex.quote(public_key) + ' >> "$home/.ssh/authorized_keys"',
        'chown ' + shlex.quote(user) + ':"$(id -gn ' + shlex.quote(user) + ')" "$home/.ssh/authorized_keys"',
        'chmod 600 "$home/.ssh/authorized_keys"',
        setup,
        # Clones must not share their template's SSH host identity.
        'if [ ! -f /var/lib/shepherd-ssh-identity ]; then rm -f /etc/ssh/ssh_host_*; ssh-keygen -A; touch /var/lib/shepherd-ssh-identity; fi',
        'systemctl enable ssh >/dev/null; systemctl restart ssh',
    ])


def local_preflight(host, name):
    if not re.fullmatch(r'[a-z][a-z0-9-]{0,47}', name):
        raise Error('VM name: 1–48 lowercase letters, digits or hyphens, starting with a letter.')
    key = Path(host['sshKey']).expanduser()
    if not key.is_file() or not Path(str(key) + '.pub').is_file():
        raise Error('SSH private key and matching .pub file are required.')
    hc_binary()
    if not shutil.which('ssh'):
        raise Error('Install hc and OpenSSH on this computer first.')
    pub = Path(str(key) + '.pub').read_text().strip()
    if not pub.startswith(('ssh-ed25519 ', 'ssh-rsa ', 'ecdsa-sha2-')):
        raise Error('Unsupported SSH public key.')
    return pub


def template_bridge(host, profile, cfg):
    if 'net0' not in cfg:
        raise Error('VM requires a net0 network interface.')
    bridge_match = re.search(r'(?:^|,)bridge=([^,]+)', cfg['net0'])
    return profile.get('bridge') or host.get('bridge') or (bridge_match.group(1) if bridge_match else '')


def check_bridge(api, node, host, profile, cfg, fallback=False):
    # A resume without an explicit saved override inherits the VM, not today's host default.
    network_bridge = template_bridge({} if fallback else host, profile, cfg)
    networks = api.get('nodes/%s/network' % node)
    active = {n['iface'] for n in networks if n.get('active')}
    if network_bridge in active:
        if fallback:
            profile['bridge'] = network_bridge
        return network_bridge
    if fallback:
        # An existing VM keeps working when the host default changed. Preserve its current bridge
        # instead of blocking a retry on a destination that is no longer configured.
        match = re.search(r'(?:^|,)bridge=([^,]+)', cfg.get('net0', ''))
        current = match.group(1) if match else ''
        if current in active:
            profile['bridge'] = current
            return current
    raise Error('Network bridge is missing or inactive: ' + network_bridge + '. Set bridge in the deployment profile.')


def preflight(host, profile, name, existing_vmid=None):
    pub = local_preflight(host, name)
    api = API(host)
    resources = api.get('cluster/resources?type=vm')
    if any(r.get('name') == name and r.get('vmid') != existing_vmid for r in resources):
        raise Error('A VM with that name already exists.')
    templates = [r for r in resources if r.get('template') and r.get('type') == 'qemu'
                 and (str(r.get('vmid')) == str(profile['templateVmid'])
                      if profile.get('templateVmid') is not None else r.get('name') == profile['template'])]
    if len(templates) != 1:
        raise Error('Template missing, ambiguous, or not visible to this token: ' + profile['template'])
    template = templates[0]
    cfg = api.get('nodes/%s/qemu/%s/config' % (template['node'], template['vmid']))
    mode = profile.get('bootstrap', 'guest-agent')
    if mode not in ('cloud-init', 'guest-agent'):
        raise Error('Unknown bootstrap mode.')
    if mode == 'cloud-init' and not any('cloudinit' in str(v).lower() for k, v in cfg.items() if re.fullmatch(r'(ide|sata|scsi)\d+', k)):
        raise Error('Template needs a cloud-init drive and cloud-init installed.')
    if mode == 'cloud-init' and cfg.get('cicustom'):
        raise Error('Template has cicustom overrides. Use a template with standard cloud-init user/network data.')
    check_bridge(api, template['node'], host, profile, cfg)
    return api, template, pub


def preflight_resume(host, profile, job):
    """Validate key/destination/ownership for an existing VM without requiring its original template."""
    pub = local_preflight(host, job['name'])
    api = API(host)
    resources = api.get('cluster/resources?type=vm')
    if any(r.get('name') == job['name'] and r.get('vmid') != job['vmid'] for r in resources):
        raise Error('A VM with that name already exists.')
    cfg = api.get('nodes/%s/qemu/%s/config' % (job['node'], job['vmid']))
    if cfg.get('template') or cfg.get('name') != job['name'] or ('job ' + job['id']) not in cfg.get('description', ''):
        raise Error('Cannot resume: VM ownership could not be verified. Inspect the VM in Proxmox.')
    mode = profile.get('bootstrap', 'guest-agent')
    if mode not in ('cloud-init', 'guest-agent'):
        raise Error('Unknown bootstrap mode.')
    check_bridge(api, job['node'], host, profile, cfg, fallback=True)
    return api, cfg, pub


def deployment_profile(profile, overrides):
    if not isinstance(overrides, dict) or set(overrides) - {'bridge', 'pool'}:
        raise Error('Unsupported deployment overrides.')
    result = dict(profile)
    for key, value in overrides.items():
        if not isinstance(value, str):
            raise Error('Invalid ' + key)
        value = value.strip()
        if value and not re.fullmatch(r'[a-zA-Z0-9_.-]{1,64}', value):
            raise Error('Invalid ' + key)
        if value:
            result[key] = value
    return result


_TASK_KINDS = {'qmclone': 'clone', 'qmstart': 'start', 'qmstop': 'stop',
               'qmshutdown': 'stop', 'qmdestroy': 'delete'}


def infer_task_kind(task):
    """Recover the operation from a historical Proxmox UPID when older jobs lack taskKind."""
    match = re.search(r'(?:^|:)(qmclone|qmstart|qmstop|qmshutdown|qmdestroy)(?=:)', str(task or ''))
    return _TASK_KINDS.get(match.group(1)) if match else None


def resolved_profile(host, job):
    profile = job.get('profileSnapshot')
    if isinstance(profile, dict) and profile:
        return dict(profile)
    profile = next((p for p in host.get('profiles', []) if p['id'] == job['profile']), None)
    if not profile:
        raise Error('Unknown deployment profile.')
    return deployment_profile(profile, job.get('overrides', {}))


def deploy(root, job):
    path = root / 'jobs' / (job['id'] + '.json')

    def ensure_current():
        if (load(path) or {}).get('attempt') != job.get('attempt'):
            raise StaleAttempt()

    def stage(state, message, **extra):
        ensure_current()
        job.update(state=state, message=message, updated=datetime.datetime.now(datetime.timezone.utc).isoformat(), **extra)
        atomic(path, job)

    try:
        host = host_config(root, job['host'])
        profile = resolved_profile(host, job)
        if job.get('vmid'):
            vmid = job['vmid']
            node = job['node']
            task = job.get('task')
            kind = job.get('taskKind') or infer_task_kind(task)
            # Clone/stop tasks must settle before the VM config can be inspected.
            if task and kind != 'start':
                api = API(host)
                settle_task(api, node, task)
                job.pop('task', None)
                job.pop('taskKind', None)
                stage('working', 'Previous %s task completed' % (kind or 'Proxmox'))
            stage('working', 'Checking SSH configuration and VM destination')
            api, _cfg, pub = preflight_resume(host, profile, job)
            if task and kind == 'start':
                status = api.get('nodes/%s/tasks/%s/status' % (node, urllib.parse.quote(task, safe='')))
                if status['status'] != 'stopped':
                    settle_task(api, node, task)
                elif status.get('exitstatus') != 'OK':
                    job.pop('task', None)
                    job.pop('taskKind', None)
                    stage('working', 'Previous VM start failed; retrying start')
                else:
                    job.pop('task', None)
                    job.pop('taskKind', None)
                    stage('working', 'Previous VM start completed')
        else:
            stage('working', 'Checking template and SSH configuration')
            api, template, pub = preflight(host, profile, job['name'])
            if profile.get('pool'):
                api.get('pools/' + urllib.parse.quote(profile['pool'], safe=''))
            node = template['node']
            vmid = int(api.get('cluster/nextid'))
            stage('working', 'Cloning template', vmid=vmid, node=node)
            linked = job.get('linkedClone', host.get('linkedClone', True))
            args = dict(newid=vmid, name=job['name'], full=0 if linked else 1, description='Managed by Herdr Shepherd; job ' + job['id'])
            if profile.get('storage') and not linked:
                args['storage'] = profile['storage']
            if profile.get('pool'):
                args['pool'] = profile['pool']
            task = api.post('nodes/%s/qemu/%s/clone' % (node, template['vmid']), **args)
            stage('working', 'Waiting for linked clone' if linked else 'Waiting for full clone', task=task, taskKind='clone')
            settle_task(api, node, task)
            stage('working', 'Clone completed', task=None, taskKind=None)
        base = 'nodes/%s/qemu/%s' % (node, vmid)
        stage('working', 'Configuring VM')
        settings = dict(agent='enabled=1', tags='herdr;shepherd')
        for key in ('cores', 'memory'):
            if key in profile:
                settings[key] = int(profile[key])
        network_bridge = profile.get('bridge') or host.get('bridge')
        if network_bridge:
            if not re.fullmatch(r'[a-zA-Z0-9_.-]+', network_bridge):
                raise Error('Invalid bridge name.')
            net = api.get(base + '/config')['net0']
            settings['net0'] = ','.join(part for part in net.split(',') if not part.startswith('bridge=')) + ',bridge=' + network_bridge
        mode = profile.get('bootstrap', 'guest-agent')
        if mode == 'cloud-init':
            settings.update(ciuser=host.get('sshUser', 'shepherd'), sshkeys=urllib.parse.quote(pub, safe=''), ipconfig0=profile.get('ipconfig', 'ip=dhcp'))
        api.call('PUT', base + '/config', settings)
        stage('working', 'Starting VM')
        if api.get(base + '/status/current')['status'] != 'running':
            task = api.post(base + '/status/start')
            stage('working', 'Waiting for VM start', task=task, taskKind='start')
            settle_task(api, node, task)
            stage('working', 'VM started', task=None, taskKind=None)
        stage('working', 'Waiting for guest agent and network (up to 10 minutes)')
        net = api.get(base + '/config').get('net0', '')
        match = re.search(r'(?:^|,)(?:virtio|e1000|rtl8139|vmxnet3)=([0-9a-fA-F:]{17})', net)
        expected_mac = match.group(1) if match else None
        deadline = time.monotonic() + 600
        address = None
        last_error = ''
        while time.monotonic() < deadline:
            try:
                address = profile.get('address') or guest_address(api, base, expected_mac)
                ipaddress.ip_address(address)
                break
            except (Error, OSError, ValueError, subprocess.TimeoutExpired) as exc:
                last_error = str(exc)
                time.sleep(5)
        else:
            raise Error('Guest not ready: ' + last_error)
        if mode == 'guest-agent':
            stage('working', 'Creating SSH access through guest agent', address=address)
            guest_exec(api, base, bootstrap_script(host.get('sshUser', 'shepherd'), pub, job['name']), timeout=300)
        known_hosts = job_known_hosts(root, job)
        ssh = ssh_args(host, address, root, known_hosts=known_hosts)
        old_target = job.get('viaTarget') or job.get('machineTarget')
        if old_target:
            # Keep the registered user's key/options, but use the current guest address
            # consistently for both the SSH probe and the Herdr connection.
            ssh = shlex.split(old_target)
            if not ssh or ssh[0] != 'ssh' or '@' not in ssh[-1]:
                raise Error('Stored SSH target is not a Shepherd SSH command.')
            ssh[-1] = ssh[-1].rsplit('@', 1)[0] + '@' + address
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            try:
                command(ssh + [('cloud-init status --wait' if mode == 'cloud-init' else 'true')], timeout=35)
                break
            except (Error, subprocess.TimeoutExpired):
                time.sleep(3)
        else:
            raise Error('SSH not ready at ' + address + '; verify routing and guest SSH service.')
        stage('working', 'Installing hc and registering machine', address=address)
        target = shlex.join(ssh)
        if old_target and old_target != target:
            # SSH to the rediscovered guest has succeeded. Retarget only our own registration,
            # retaining both intents across a crash between remove and add.
            machines = json.loads(command(['hc', 'machine', 'ls', '--json']))['machines']
            machine = next((m for m in machines if m.get('name') == job['name']), None)
            previous_target = job.get('previousTarget')
            if machine and machine.get('target') not in (old_target, target, previous_target):
                raise Error('A Herdr machine with this name has a different SSH target. Remove it manually.')
            # Retain the actual proven registration across multiple interrupted DHCP moves.
            removal_target = machine['target'] if machine else previous_target or old_target
            stage('working', 'Updating machine SSH destination', previousTarget=removal_target,
                  viaTarget=target, registering=True, registered=False)
            if machine and machine.get('target') != target:
                command(['hc', 'machine', 'rm', job['name'], '--json'])
        if not job.get('registered'):
            recovering = bool(job.get('registering'))
            # Persist registration intent and the exact target before invoking hc.
            stage('working', 'Registering machine', registering=True, machine=job['name'], viaTarget=target)
            if recovering:
                machines = json.loads(command(['hc', 'machine', 'ls', '--json']))['machines']
                machine = next((m for m in machines if m.get('name') == job['name']), None)
                if machine and machine.get('target') != target:
                    if machine.get('target') != job.get('previousTarget'):
                        raise Error('A Herdr machine with this name has a different SSH target. Remove it manually.')
                    command(['hc', 'machine', 'rm', job['name'], '--json'])
                    machine = None
                if not machine:
                    command(['hc', 'machine', 'add', '--via', target, '--name', job['name'], '--install', '--json'], timeout=300)
            else:
                command(['hc', 'machine', 'add', '--via', target, '--name', job['name'], '--install', '--json'], timeout=300)
            stage('working', 'Machine registered', registered=True, registering=False)
        stage('working', 'Checking remote herdr-core')
        command(['hc', 'exec', '--on', job['name'], '--', 'true'], timeout=60)
        stage('done', 'Ready in Herdr Machines', machine=job['name'], task=None, taskKind=None, registering=False)
    except StaleAttempt:
        return
    except Exception as exc:
        try:
            stage('blocked', str(exc), task=job.get('task'))
        except StaleAttempt:
            return


def delete_vm(root, job):
    path = root / 'jobs' / (job['id'] + '.json')
    def ensure_current():
        if (load(path) or {}).get('attempt') != job.get('attempt'):
            raise StaleAttempt()

    def save(message, **values):
        ensure_current()
        job.update(message=message, **values)
        atomic(path, job)

    def clear_task():
        job.pop('task', None)
        job.pop('taskKind', None)
    try:
        host = host_config(root, job['host'])
        api = API(host)
        base = 'nodes/%s/qemu/%s' % (job['node'], job['vmid'])
        task = job.get('task')
        if task:
            # Never report deletion complete while a persisted Proxmox task is pending or failed.
            status = api.get('nodes/%s/tasks/%s/status' % (job['node'], urllib.parse.quote(task, safe='')))
            kind = job.get('taskKind') or infer_task_kind(task)
            if status['status'] != 'stopped':
                try:
                    settle_task(api, job['node'], task)
                except Exception:
                    if kind in ('delete', None):
                        # Keep failed DELETE evidence when the VM is absent; otherwise a retry may try again.
                        job['deleteFailed'] = True
                        resources = api.get('cluster/resources?type=vm')
                        if any(r.get('vmid') == job['vmid'] for r in resources):
                            clear_task()
                    else:
                        clear_task()
                    raise
            elif status.get('exitstatus') != 'OK':
                if kind in ('delete', None):
                    job['deleteFailed'] = True
                    resources = api.get('cluster/resources?type=vm')
                    if any(r.get('vmid') == job['vmid'] for r in resources):
                        clear_task()
                else:
                    clear_task()
                raise Error('Proxmox task failed: ' + status.get('exitstatus', 'unknown'))
            clear_task()
            save('Previous %s task completed' % (kind or 'Proxmox'))
        resources = api.get('cluster/resources?type=vm')
        exists = any(r.get('vmid') == job['vmid'] for r in resources)
        if job.get('deleteFailed'):
            if not exists:
                raise Error('Previous DELETE failed and the VM is absent; verify in Proxmox before retrying.')
            job.pop('deleteFailed', None)
            job.pop('deleteAttempted', None)
        if exists:
            cfg = api.get(base + '/config')
            if cfg.get('template') or cfg.get('name') != job['name'] or ('job ' + job['id']) not in cfg.get('description', ''):
                raise Error('Cannot delete: VM ownership could not be verified.')
            if cfg.get('lock'):
                raise Error('VM is locked by Proxmox; wait for its task to finish.')
            if api.get(base + '/status/current')['status'] != 'stopped':
                save('Stopping VM for deletion')
                task = api.post(base + '/status/stop')
                save('Waiting for VM stop', task=task, taskKind='stop')
                try:
                    settle_task(api, job['node'], task)
                except Exception:
                    clear_task()
                    raise
                save('VM stopped', task=None, taskKind=None)
            save('Deleting VM and its disks', deleteAttempted=True, deleteFailed=False)
            try:
                task = api.call('DELETE', base, {'purge': 1})
            except Exception:
                job['deleteFailed'] = True
                raise
            save('Waiting for VM deletion', task=task, taskKind='delete')
            try:
                settle_task(api, job['node'], task)
            except Exception:
                job['deleteFailed'] = True
                raise
            save('VM deletion task completed', task=None, taskKind=None)
        # Reconcile Herdr even if the process stopped after registration but before the flag was saved.
        if job.get('registered') or job.get('registering') or job.get('viaTarget'):
            save('Removing machine from Herdr')
            machines = json.loads(command(['hc', 'machine', 'ls', '--json']))['machines']
            machine = next((m for m in machines if m['name'] == job['name']), None)
            if machine:
                expected = job.get('viaTarget') or job.get('machineTarget')
                if not expected and job.get('address'):
                    expected = shlex.join(ssh_args(host, job['address'], root, known_hosts=job_known_hosts(root, job)))
                if not expected:
                    raise Error('VM deleted; Herdr machine target could not be verified. Remove that machine manually.')
                if machine.get('target') not in (expected, job.get('previousTarget')):
                    raise Error('VM deleted; Herdr machine target changed. Remove that machine manually.')
                command(['hc', 'machine', 'rm', job['name'], '--json'])
        save('VM deleted', state='deleted', registered=False, registering=False,
             task=None, taskKind=None, deleteAttempted=False, deleteFailed=False)
    except StaleAttempt:
        return
    except Exception as exc:
        try:
            save(str(exc), state='blocked')
        except StaleAttempt:
            return


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def recover_job(root, path, job, message, remains_stale):
    """Re-read under the worker lock so a worker finishing concurrently is never overwritten."""
    with execution_lock(root, blocking=False) as acquired:
        if not acquired:
            return job
        fresh = load(path)
        if not fresh or fresh.get('state') != 'working':
            return fresh or job
        if fresh.get('attempt') != job.get('attempt') or fresh.get('pid') != job.get('pid'):
            return fresh
        if remains_stale(fresh):
            fresh.update(state='blocked', message=message)
            atomic(path, fresh)
        return fresh


def jobs(root):
    result = []
    for path in sorted((root / 'jobs').glob('*.json'), reverse=True):
        job = load(path)
        if not job:
            continue
        if job.get('state') == 'working' and job.get('pid'):
            if not pid_alive(job['pid']):
                job = recover_job(
                    root, path, job,
                    'Worker interrupted. Inspect the recorded VM/task in Proxmox; no automatic re-clone.',
                    lambda fresh: not fresh.get('pid') or not pid_alive(fresh.get('pid')))
        elif job.get('state') == 'working':
            queued = job.get('queuedAt', int(job.get('id', '0-').split('-')[0]) / 1e9)
            if time.time() - queued > 60:
                def stale(fresh):
                    queued_at = fresh.get('queuedAt', int(fresh.get('id', '0-').split('-')[0]) / 1e9)
                    return not fresh.get('pid') and time.time() - queued_at > 60
                job = recover_job(root, path, job, 'Worker did not start. Retry provisioning.', stale)
        result.append(job)
    return result[:50]


def start(root, request, resume_id=None, deleting=False):
    host = host_config(root, request['host'])
    selected_profile = None
    if not resume_id:
        if str(request['profile']).startswith('template-'):
            selected_profile = next((p for p in inventory(host)['profiles'] if p['id'] == request['profile']), None)
        else:
            selected_profile = next((p for p in host.get('profiles', []) if p['id'] == request['profile']), None)
        if not selected_profile:
            raise Error('Unknown profile or template no longer visible.')
    if not re.fullmatch(r'[a-z][a-z0-9-]{0,47}', request['name']):
        raise Error('Invalid VM name.')
    with open(root / 'launch.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        current = jobs(root)
        if any(j.get('state') == 'working' for j in current):
            raise Error('A deployment is already running. Wait for it to finish.')
        with execution_lock(root, blocking=False) as acquired:
            if not acquired:
                raise Error('A deployment is already running. Wait for it to finish.')
            if resume_id:
                job = next((j for j in current if j['id'] == resume_id), None)
                if not job or (job.get('state') != 'blocked' and not (deleting and job.get('state') == 'done')):
                    raise Error('Only a blocked job can be resumed.')
                if deleting:
                    if not job.get('vmid') or not job.get('node'):
                        raise Error('This job has no VM to delete.')
                    job['operation'] = 'delete'
                job.pop('pid', None)
                job['attempt'] = uuid.uuid4().hex
                job.update(state='working', message='Deleting VM' if job.get('operation') == 'delete' else 'Resuming existing VM')
            else:
                profile = selected_profile
                if not profile:
                    raise Error('Unknown profile.')
                overrides = request.get('overrides', {})
                effective = deployment_profile(profile, overrides)
                if not effective.get('bridge') and host.get('bridge'):
                    effective['bridge'] = host['bridge']
                job = {k: request[k] for k in ('host', 'profile', 'name')}
                job['linkedClone'] = host.get('linkedClone', True)
                if not isinstance(job['linkedClone'], bool):
                    raise Error('linkedClone must be true or false.')
                job['overrides'] = overrides
                job['profileSnapshot'] = effective
                job['attempt'] = uuid.uuid4().hex
                job.update(id=str(time.time_ns()) + '-' + uuid.uuid4().hex[:8], state='working', message='Queued')
                job['knownHosts'] = str(root / 'known_hosts.d' / job['id'])
            job['queuedAt'] = time.time()
            path = root / 'jobs' / (job['id'] + '.json')
            atomic(path, job)
        # Release the execution lock before launching: a fast worker must never wait on start.
        try:
            command(['hc', 'run', '--bg', '--no-parent', '--no-notify', '--rm', '--json',
                     '--name', 'shepherd-' + job['id'].split('-')[-1], '--',
                     sys.executable, str(Path(__file__).resolve()), '--root', str(root), 'worker', job['id'], job['attempt']])
        except Exception as exc:
            with execution_lock(root, blocking=False) as acquired:
                if acquired:
                    fresh = load(path) or job
                    if (fresh.get('state') == 'working' and not fresh.get('pid') and
                            fresh.get('attempt') == job.get('attempt')):
                        fresh.update(state='blocked', message='Cannot launch worker: ' + str(exc))
                        atomic(path, fresh)
                        job = fresh
            raise
        # The worker owns subsequent writes, including its PID.
        return job


def run_worker(root, job_id, attempt):
    path = root / 'jobs' / (job_id + '.json')
    with execution_lock(root, blocking=True):
        job = load(path)
        if not job or job.get('state') != 'working':
            return
        if attempt is None:
            # Legacy queued job written before attempts existed.
            if job.get('attempt'):
                return
        elif job.get('attempt') != attempt:
            return
        job['pid'] = os.getpid()
        atomic(path, job)
        if job.get('operation') == 'delete':
            delete_vm(root, job)
        else:
            deploy(root, job)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('action', choices=['init', 'inventory', 'jobs', 'start', 'resume', 'delete', 'worker'])
    parser.add_argument('value', nargs='?')
    parser.add_argument('attempt', nargs='?')
    args = parser.parse_args()
    root = args.root.resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    (root / 'jobs').mkdir(exist_ok=True, mode=0o700)
    try:
        if args.action == 'init':
            result = {'configured': bool(load(root / 'config.json')), 'jobs': jobs(root)}
        elif args.action == 'inventory':
            result = inventory(host_config(root, args.value))
        elif args.action == 'jobs':
            result = jobs(root)
        elif args.action == 'start':
            result = start(root, json.loads(args.value))
        elif args.action in ('resume', 'delete'):
            job = next((j for j in jobs(root) if j['id'] == args.value), None)
            if not job:
                raise Error('Unknown job.')
            result = start(root, job, args.value, deleting=args.action == 'delete')
        else:
            if not re.fullmatch(r'\d+-[0-9a-f]{8}', args.value or ''):
                raise Error('Invalid job ID.')
            run_worker(root, args.value, args.attempt)
            return
        print(json.dumps({'ok': True, 'data': result}))
    except Exception as exc:
        print(json.dumps({'ok': False, 'error': str(exc)}))
        sys.exit(1)


if __name__ == '__main__':
    main()

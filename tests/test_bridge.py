import importlib.util
import json
from pathlib import Path
import tempfile
import subprocess
import sys
import time
import unittest
from unittest.mock import patch, MagicMock

spec = importlib.util.spec_from_file_location('bridge', Path(__file__).parents[1] / 'mods/shepherd/bridge.py')
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)


def successful_command(args, **_kwargs):
    return 'hc 0.1.15' if args == ['hc', '--version'] else ''


class FakeAPI:
    def __init__(self, fail_clone=False):
        self.calls = []
        self.fail_clone = fail_clone

    def get(self, path):
        self.calls.append(('GET', path))
        if path == 'cluster/nextid': return '200'
        if path.startswith('pools/'): return {'poolid': path.split('/')[1]}
        if '/tasks/' in path: return {'status': 'stopped', 'exitstatus': 'OK'}
        if path.endswith('/config'): return {'net0': 'virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0'}
        if path.endswith('/status/current'): return {'status': 'stopped'}
        raise AssertionError(path)

    def post(self, path, **data):
        self.calls.append(('POST', path, data))
        if self.fail_clone and path.endswith('/clone'): raise b.Error('Clone failed')
        return 'UPID:test'

    def call(self, method, path, data):
        self.calls.append((method, path, data))
        return 'UPID:test'

    def wait(self, node, task): pass


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.host = {'id': 'pve', 'sshKey': '~/.ssh/key', 'sshUser': 'shepherd',
                     'profiles': [{'id': 'ubuntu', 'template': 'template'}]}
        b.atomic(self.root / 'config.json', {'hypervisors': [self.host]})
        self.job = dict(id='1-12345678', host='pve', profile='ubuntu', name='test-vm')
        b.atomic(self.root / 'jobs/1-12345678.json', self.job)

    def tearDown(self): self.temp.cleanup()

    def persist(self, **values):
        self.job.update(values)
        b.atomic(self.root / 'jobs/1-12345678.json', self.job)
        return self.job

    def execute(self, api):
        with patch.object(b, 'preflight', return_value=(api, {'node': 'pve', 'vmid': 100}, 'ssh-ed25519 AAAA')), \
             patch.object(b, 'guest_address', return_value='192.168.1.50'), \
             patch.object(b, 'guest_exec') as guest, \
             patch.object(b, 'command', side_effect=successful_command) as command:
            b.deploy(self.root, self.job)
            return guest, command

    def test_full_deployment_registers_permanent_machine(self):
        api = FakeAPI()
        guest, command = self.execute(api)
        saved = b.load(self.root / 'jobs/1-12345678.json')
        self.assertEqual(saved['state'], 'done')
        self.assertEqual(saved['machine'], 'test-vm')
        guest.assert_called_once()
        commands = [c.args[0] for c in command.call_args_list]
        add = next(c for c in commands if c[:3] == ['hc', 'machine', 'add'])
        self.assertIn('--install', add)
        self.assertNotIn('--temp', add)
        self.assertEqual([c[1] for c in api.calls if c[0] == 'POST'],
                         ['nodes/pve/qemu/100/clone', 'nodes/pve/qemu/200/status/start'])

    def test_failed_clone_never_starts_or_registers(self):
        api = FakeAPI(fail_clone=True)
        guest, command = self.execute(api)
        saved = b.load(self.root / 'jobs/1-12345678.json')
        self.assertEqual(saved['state'], 'blocked')
        self.assertEqual(saved['vmid'], 200)
        guest.assert_not_called()
        command.assert_not_called()
        self.assertFalse(any('/status/start' in c[1] for c in api.calls))

    def test_linked_clone_is_default(self):
        self.host['profiles'][0]['storage'] = 'other-storage'
        b.atomic(self.root / 'config.json', {'hypervisors': [self.host]})
        api = FakeAPI()
        self.execute(api)
        clone = next(c for c in api.calls if c[0] == 'POST' and c[1].endswith('/clone'))
        self.assertEqual(clone[2]['full'], 0)
        self.assertNotIn('storage', clone[2])

    def test_unchecked_linked_clone_uses_full_clone(self):
        self.host['linkedClone'] = False
        b.atomic(self.root / 'config.json', {'hypervisors': [self.host]})
        api = FakeAPI()
        self.execute(api)
        clone = next(c for c in api.calls if c[0] == 'POST' and c[1].endswith('/clone'))
        self.assertEqual(clone[2]['full'], 1)

    def test_saved_job_clone_mode_wins_over_changed_host_setting(self):
        self.job['linkedClone'] = False
        api = FakeAPI()
        self.execute(api)
        clone = next(c for c in api.calls if c[0] == 'POST' and c[1].endswith('/clone'))
        self.assertEqual(clone[2]['full'], 1)

    def test_advanced_pool_is_sent_to_clone_and_bridge_to_config(self):
        self.job['overrides'] = {'bridge': 'vmbr42', 'pool': 'development'}
        api = FakeAPI()
        self.execute(api)
        self.assertEqual(self.job['state'], 'done')
        clone = next(c for c in api.calls if c[0] == 'POST' and c[1].endswith('/clone'))
        self.assertEqual(clone[2]['pool'], 'development')
        cfg = next(c for c in api.calls if c[0] == 'PUT')
        self.assertEqual(cfg[2]['net0'], 'virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr42')
        self.assertLess(api.calls.index(('GET', 'pools/development')), api.calls.index(clone))

    def test_invalid_pool_never_clones(self):
        self.job['overrides'] = {'pool': 'missing'}
        api = FakeAPI()
        original_get = api.get
        def get(path):
            if path == 'pools/missing': raise b.Error('Pool missing')
            return original_get(path)
        api.get = get
        self.execute(api)
        self.assertEqual(self.job['state'], 'blocked')
        self.assertFalse(any(c[0] in ('PUT', 'POST') for c in api.calls))

    def test_resume_ignores_new_active_host_bridge(self):
        api = FakeAPI()
        api.get = lambda _: [{'iface': 'vmbr0', 'active': 1}, {'iface': 'vmbr9', 'active': 1}]
        profile = {'cores': 2}
        bridge = b.check_bridge(api, 'pve', {'bridge': 'vmbr9'}, profile,
                                {'net0': 'virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0'}, fallback=True)
        self.assertEqual(bridge, 'vmbr0')
        self.assertEqual(profile['bridge'], 'vmbr0')

    def test_changed_dhcp_target_is_reconciled_before_remote_check(self):
        self.check_dhcp_retarget()

    def test_second_dhcp_change_recovers_interrupted_retarget(self):
        self.check_dhcp_retarget(interrupted=True)

    def check_dhcp_retarget(self, interrupted=False):
        old = 'ssh -i key shepherd@192.168.1.40'
        self.job.update(registering=True, viaTarget=old, address='192.168.1.40')
        if interrupted:
            self.job.update(previousTarget=old, viaTarget='ssh -i key shepherd@192.168.1.45')
        api = FakeAPI()
        machine = {'name': 'test-vm', 'target': old}
        calls = []
        def command(args, timeout=30):
            nonlocal machine
            calls.append(args)
            if args[:3] == ['hc', 'machine', 'ls']:
                return json.dumps({'machines': [machine] if machine else []})
            if args[:3] == ['hc', 'machine', 'rm']: machine = None
            if args[:3] == ['hc', 'machine', 'add']:
                machine = {'name': 'test-vm', 'target': args[args.index('--via') + 1]}
            if args[:2] == ['hc', 'exec']:
                self.assertTrue(machine['target'].endswith('@192.168.1.50'))
            return ''
        with patch.object(b, 'preflight', return_value=(api, {'node': 'pve', 'vmid': 100}, 'ssh-ed25519 AAAA')), \
             patch.object(b, 'guest_address', return_value='192.168.1.50'), \
             patch.object(b, 'guest_exec'), patch.object(b, 'command', side_effect=command):
            b.deploy(self.root, self.job)
        self.assertEqual(self.job['state'], 'done')
        self.assertTrue(self.job['viaTarget'].endswith('@192.168.1.50'))
        probe = next(c for c in calls if c[0] == 'ssh')
        self.assertEqual(probe[-2], 'shepherd@192.168.1.50')

    def test_overrides_preserve_defaults_and_reject_unknown_settings(self):
        profile = {'bridge': 'vmbr0', 'pool': 'default', 'cores': 2}
        self.assertEqual(b.deployment_profile(profile, {'bridge': '', 'pool': ''}), profile)
        self.assertEqual(b.deployment_profile(profile, {'bridge': 'vmbr1'})['bridge'], 'vmbr1')
        self.assertEqual(profile['bridge'], 'vmbr0')
        with self.assertRaises(b.Error): b.deployment_profile(profile, {'bridge': 'vmbr0,tag=2'})
        with self.assertRaises(b.Error): b.deployment_profile(profile, {'tokenSecret': 'x'})

    def test_guest_interface_is_selected_by_mac(self):
        api = FakeAPI()
        api.get = lambda _: {'result': [
            {'hardware-address': '00:00:00:00:00:00', 'ip-addresses': [{'ip-address': '172.17.0.1'}]},
            {'hardware-address': 'aa:bb:cc:dd:ee:ff', 'ip-addresses': [{'ip-address': '192.168.1.50'}]}]}
        self.assertEqual(b.guest_address(api, 'base', 'AA:BB:CC:DD:EE:FF'), '192.168.1.50')

    def test_guest_script_rejects_shell_injection(self):
        with self.assertRaises(b.Error): b.bootstrap_script('user;touch /tmp/x', 'key', 'vm')

    def test_hosts_replaces_template_alias_and_preserves_other_records(self):
        path = self.root / 'hosts'
        original = ('127.0.0.1 localhost\n127.0.1.1 template.example template local-alias # local VM\n'
                    '::1 localhost ip6-localhost ip6-loopback\n192.168.1.2 database\n')
        path.write_text(original)
        args = [sys.executable, '-c', b.HOSTS_UPDATE_PROGRAM, str(path), 'new-vm', 'new-vm']
        subprocess.run(args, check=True)
        updated = path.read_text()
        self.assertIn('127.0.1.1\tnew-vm local-alias  # local VM', updated)
        self.assertNotIn('template', updated)
        self.assertIn('::1 localhost ip6-localhost ip6-loopback', updated)
        self.assertIn('192.168.1.2 database', updated)
        self.assertEqual((self.root / 'hosts.before-shepherd').read_text(), original)
        subprocess.run(args, check=True)
        self.assertEqual(path.read_text(), updated)
        self.assertEqual((self.root / 'hosts.before-shepherd').read_text(), original)

    def test_hosts_adds_mapping_and_updates_old_loopback_name(self):
        path = self.root / 'hosts'
        path.write_text('127.0.0.1 localhost old-host # keep\n::1 localhost\n')
        subprocess.run([sys.executable, '-c', b.HOSTS_UPDATE_PROGRAM, str(path), 'old-host', 'new-host'], check=True)
        self.assertIn('127.0.0.1\tlocalhost new-host  # keep', path.read_text())
        self.assertIn('127.0.1.1\tnew-host', path.read_text())

    def test_bootstrap_has_no_sudo_grants_and_shell_is_valid(self):
        script = b.bootstrap_script('shepherd', 'ssh-ed25519 AAAA', 'test-vm')
        subprocess.run(['sh', '-n'], input=script, text=True, check=True)
        self.assertNotIn('sudo', script)
        with self.assertRaises(b.Error): b.system_setup_script('shepherd', 'bad;hostname')

    def test_plain_http_and_invalid_pin_rejected(self):
        with self.assertRaises(b.Error): b.API({'url': 'http://server:8006'})
        with self.assertRaises(b.Error): b.PinnedHTTPS('server', 8006, 'invalid')

    def test_config_private_and_concurrent_deploy_rejected(self):
        self.assertEqual((self.root / 'config.json').stat().st_mode & 0o777, 0o600)
        b.atomic(self.root / 'jobs/old.json', {'state': 'working', 'queuedAt': time.time()})
        with self.assertRaisesRegex(b.Error, 'already running'):
            b.start(self.root, dict(host='pve', profile='ubuntu', name='new-vm'))

    def test_stale_queue_is_recoverable(self):
        b.atomic(self.root / 'jobs/old.json', {'state': 'working', 'queuedAt': time.time() - 120})
        self.assertEqual(b.jobs(self.root)[0]['state'], 'blocked')

    def test_delete_checks_ownership_before_mutations(self):
        self.job.update(vmid=200, node='pve', registered=True)
        api = FakeAPI()
        api.get = lambda path: ([{'vmid': 200}] if path == 'cluster/resources?type=vm' else {'name': 'someone-else'})
        with patch.object(b, 'API', return_value=api), patch.object(b, 'command') as command:
            b.delete_vm(self.root, self.job)
        self.assertEqual(self.job['state'], 'blocked')
        self.assertIn('ownership', self.job['message'])
        self.assertEqual(api.calls, [])
        command.assert_not_called()

    def test_delete_http_uses_query_without_body(self):
        conn = MagicMock()
        conn.getresponse.return_value.status = 200
        conn.getresponse.return_value.read.return_value = b'{"data":"UPID:delete"}'
        with patch.object(b.http.client, 'HTTPSConnection', return_value=conn):
            api = b.API({'url': 'https://proxmox.example:8006', 'tokenId': 'test@pam!test', 'tokenSecret': 'test'})
            self.assertEqual(api.call('DELETE', 'nodes/pve/qemu/200', {'purge': 1}), 'UPID:delete')
        args = conn.request.call_args.args
        self.assertEqual(args[0], 'DELETE')
        self.assertEqual(args[1], '/api2/json/nodes/pve/qemu/200?purge=1')
        self.assertIsNone(args[2])
        self.assertNotIn('Content-Type', args[3])
        conn.close.assert_called_once()

    def test_delete_stops_then_deletes_then_unregisters(self):
        self.job.update(vmid=200, node='pve', registered=True, address='192.168.1.50')
        api = FakeAPI()
        def get(path):
            if path == 'cluster/resources?type=vm': return [{'vmid': 200}]
            if '/tasks/' in path: return {'status': 'stopped', 'exitstatus': 'OK'}
            if path.endswith('/config'): return {'name': self.job['name'], 'description': 'job ' + self.job['id']}
            if path.endswith('/status/current'): return {'status': 'running'}
            raise AssertionError(path)
        api.get = get
        target = b.shlex.join(b.ssh_args(self.host, self.job['address'], self.root))
        with patch.object(b, 'API', return_value=api), patch.object(b, 'command', side_effect=[json.dumps({'machines': [{'name': 'test-vm', 'target': target}]}), '']) as command:
            b.delete_vm(self.root, self.job)
        self.assertEqual(self.job['state'], 'deleted')
        self.assertEqual([(c[0], c[1]) for c in api.calls], [('POST', 'nodes/pve/qemu/200/status/stop'), ('DELETE', 'nodes/pve/qemu/200')])
        self.assertEqual(command.call_args_list[-1].args[0], ['hc', 'machine', 'rm', 'test-vm', '--json'])

    def test_resume_refuses_foreign_vm(self):
        self.job.update(vmid=200, node='pve')
        api = FakeAPI()
        api.get = lambda path: ([{'vmid': 200, 'name': 'test-vm'}] if path == 'cluster/resources?type=vm'
                                else {'name': 'someone-else'})
        with patch.object(b, 'local_preflight', return_value='ssh-ed25519 AAAA'), \
             patch.object(b, 'API', return_value=api), \
             patch.object(b, 'command') as command:
            b.deploy(self.root, self.job)
        saved = b.load(self.root / 'jobs/1-12345678.json')
        self.assertEqual(saved['state'], 'blocked')
        self.assertIn('ownership', saved['message'])
        self.assertFalse(any(c[0] in ('POST', 'PUT') for c in api.calls))
        command.assert_not_called()

    def execute_existing(self, api, extra=None):
        cfg = {'net0': 'virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0'}
        with patch.object(b, 'preflight_resume', return_value=(api, cfg, 'ssh-ed25519 AAAA')), \
             patch.object(b, 'guest_address', return_value='192.168.1.50'), \
             patch.object(b, 'guest_exec') as guest, \
             patch.object(b, 'command', side_effect=successful_command) as command:
            b.deploy(self.root, self.job)
            return guest, command

    def test_failed_clone_task_blocks_without_followup_mutations(self):
        self.persist(vmid=200, node='pve', task='UPID:clone', taskKind='clone', attempt='a')
        api = FakeAPI()
        api.get = lambda path: ({'status': 'stopped', 'exitstatus': 'clone failed'} if '/tasks/' in path else {})
        with patch.object(b, 'API', return_value=api), \
             patch.object(b, 'preflight_resume') as resume, \
             patch.object(b, 'command') as command:
            b.deploy(self.root, self.job)
        saved = b.load(self.root / 'jobs/1-12345678.json')
        self.assertEqual(saved['state'], 'blocked')
        self.assertIn('task failed', saved['message'])
        self.assertEqual([c for c in api.calls if c[0] in ('POST', 'PUT')], [])
        resume.assert_not_called()
        command.assert_not_called()

    def test_running_task_is_awaited_before_resume(self):
        self.persist(vmid=200, node='pve', task='UPIDstart', taskKind='start', attempt='a')
        api = FakeAPI()
        statuses = {'nodes/pve/tasks/UPIDstart/status': {'status': 'running'}}
        def get(path):
            if path in statuses: return statuses[path]
            if path == 'cluster/nextid': return '200'
            if path.endswith('/config'): return {'net0': 'virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0'}
            if path.endswith('/status/current'): return {'status': 'running'}
            raise AssertionError(path)
        api.get = get
        api.wait = MagicMock(side_effect=lambda node, task: statuses.update(
            {'nodes/pve/tasks/UPIDstart/status': {'status': 'stopped', 'exitstatus': 'OK'}}))
        guest, command = self.execute_existing(api)
        self.assertEqual(b.load(self.root / 'jobs/1-12345678.json')['state'], 'done')
        api.wait.assert_called_once_with('pve', 'UPIDstart')
        self.assertFalse(any(c[0] == 'POST' and c[1].endswith('/status/start') for c in api.calls))

    def test_failed_start_task_can_retry_start(self):
        self.persist(vmid=200, node='pve', task='UPIDstart', taskKind='start', attempt='a')
        api = FakeAPI()
        def get(path):
            if path == 'nodes/pve/tasks/UPIDstart/status':
                return {'status': 'stopped', 'exitstatus': 'start failed'}
            if '/tasks/' in path:
                return {'status': 'stopped', 'exitstatus': 'OK'}
            if path == 'cluster/nextid': return '200'
            if path.endswith('/config'): return {'net0': 'virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0'}
            if path.endswith('/status/current'): return {'status': 'stopped'}
            raise AssertionError(path)
        api.get = get
        guest, command = self.execute_existing(api)
        self.assertEqual(b.load(self.root / 'jobs/1-12345678.json')['state'], 'done')
        self.assertTrue(any(c[0] == 'POST' and c[1].endswith('/status/start') for c in api.calls))

    def test_completed_start_task_does_not_restart_vm(self):
        self.persist(vmid=200, node='pve', task='UPIDstart', taskKind='start', attempt='a')
        api = FakeAPI()
        def get(path):
            if path == 'nodes/pve/tasks/UPIDstart/status':
                return {'status': 'stopped', 'exitstatus': 'OK'}
            if path == 'cluster/nextid': return '200'
            if path.endswith('/config'): return {'net0': 'virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0'}
            if path.endswith('/status/current'): return {'status': 'running'}
            raise AssertionError(path)
        api.get = get
        guest, command = self.execute_existing(api)
        self.assertEqual(b.load(self.root / 'jobs/1-12345678.json')['state'], 'done')
        self.assertFalse(any(c[0] == 'POST' and c[1].endswith('/status/start') for c in api.calls))

    def test_resume_uses_saved_profile_when_original_profile_is_gone(self):
        self.persist(vmid=200, node='pve', profileSnapshot={'cores': 4, 'memory': 4096}, attempt='a')
        self.host['profiles'] = []
        b.atomic(self.root / 'config.json', {'hypervisors': [self.host]})
        api = FakeAPI()
        guest, command = self.execute_existing(api)
        self.assertEqual(b.load(self.root / 'jobs/1-12345678.json')['state'], 'done')
        put = next(c for c in api.calls if c[0] == 'PUT')
        self.assertEqual(put[2]['cores'], 4)
        self.assertEqual(put[2]['memory'], 4096)

    def test_jobs_does_not_overwrite_done_written_during_probe(self):
        path = self.root / 'jobs/win.json'
        b.atomic(path, {'id': 'win', 'state': 'working', 'pid': 123, 'attempt': 'a'})
        def probe(pid):
            b.atomic(path, {'id': 'win', 'state': 'done', 'pid': 123, 'attempt': 'a', 'machine': 'test-vm'})
            return False
        with patch.object(b, 'pid_alive', side_effect=probe):
            result = b.jobs(self.root)
        self.assertEqual(result[0]['state'], 'done')
        self.assertEqual(b.load(path)['state'], 'done')

    def test_dead_worker_is_blocked_only_if_still_working(self):
        path = self.root / 'jobs/dead.json'
        b.atomic(path, {'id': 'dead', 'state': 'working', 'pid': 123, 'attempt': 'a'})
        with patch.object(b, 'pid_alive', return_value=False):
            result = b.jobs(self.root)
        self.assertEqual(result[0]['state'], 'blocked')
        self.assertIn('interrupted', result[0]['message'])

    def test_stale_attempt_worker_exits_without_touching_job(self):
        path = self.root / 'jobs/stale-12345678.json'
        original = {'id': 'stale-12345678', 'state': 'working', 'attempt': 'new', 'queuedAt': time.time()}
        b.atomic(path, original)
        with patch.object(b, 'deploy') as deploy, patch.object(b, 'delete_vm') as delete:
            b.run_worker(self.root, 'stale-12345678', 'old')
        deploy.assert_not_called()
        delete.assert_not_called()
        self.assertEqual(b.load(path), original)

    def test_current_attempt_worker_records_pid_before_dispatch(self):
        path = self.root / 'jobs/current-12345678.json'
        b.atomic(path, {'id': 'current-12345678', 'state': 'working', 'attempt': 'same'})
        with patch.object(b, 'deploy') as deploy:
            b.run_worker(self.root, 'current-12345678', 'same')
        deploy.assert_called_once()
        self.assertEqual(b.load(path)['pid'], __import__('os').getpid())

    def test_start_refuses_while_execution_lock_is_held(self):
        with b.execution_lock(self.root):
            with patch.object(b, 'command') as command:
                with self.assertRaisesRegex(b.Error, 'already running'):
                    b.start(self.root, {'host': 'pve', 'profile': 'ubuntu', 'name': 'new-vm'})
        command.assert_not_called()

    def test_new_job_gets_per_job_known_hosts_and_attempt(self):
        with patch.object(b, 'command', return_value='') as command:
            job = b.start(self.root, {'host': 'pve', 'profile': 'ubuntu', 'name': 'new-vm'})
        saved = b.load(self.root / ('jobs/' + job['id'] + '.json'))
        self.assertNotEqual(saved['knownHosts'], str(self.root / 'known_hosts'))
        self.assertEqual(saved['knownHosts'], str(self.root / 'known_hosts.d' / job['id']))
        self.assertEqual(saved['attempt'], job['attempt'])
        launched = command.call_args.args[0]
        self.assertEqual(launched[-2:], [job['id'], job['attempt']])

    def test_new_discovered_template_can_launch_without_configured_profile(self):
        profile = {'id': 'template-999', 'template': 'custom', 'templateVmid': 999, 'node': 'pve', 'available': True}
        with patch.object(b, 'inventory', return_value={'profiles': [profile]}), patch.object(b, 'command', return_value=''):
            job = b.start(self.root, {'host': 'pve', 'profile': 'template-999', 'name': 'new-vm'})
        self.assertEqual(job['profileSnapshot']['templateVmid'], 999)
        self.assertNotIn('cores', job['profileSnapshot'])

    def test_new_job_ssh_uses_per_job_known_hosts(self):
        self.job['knownHosts'] = str(self.root / 'known_hosts.d' / self.job['id'])
        api = FakeAPI()
        guest, command = self.execute(api)
        add = next(c.args[0] for c in command.call_args_list if c.args[0][:3] == ['hc', 'machine', 'add'])
        target = add[add.index('--via') + 1]
        self.assertIn('UserKnownHostsFile=' + self.job['knownHosts'], target)
        ssh = next(c.args[0] for c in command.call_args_list if c.args[0][0] == 'ssh')
        self.assertIn('UserKnownHostsFile=' + self.job['knownHosts'], ssh)

    def test_legacy_job_keeps_shared_known_hosts(self):
        api = FakeAPI()
        guest, command = self.execute(api)
        add = next(c.args[0] for c in command.call_args_list if c.args[0][:3] == ['hc', 'machine', 'add'])
        target = add[add.index('--via') + 1]
        self.assertIn('UserKnownHostsFile=' + str(self.root / 'known_hosts'), target)

    def test_registration_intent_is_persisted_before_add(self):
        api = FakeAPI()
        def command(args, timeout=30):
            if args[:3] == ['hc', 'machine', 'add']:
                raise b.Error('add failed')
            return ''
        with patch.object(b, 'preflight', return_value=(api, {'node': 'pve', 'vmid': 100}, 'ssh-ed25519 AAAA')), \
             patch.object(b, 'guest_address', return_value='192.168.1.50'), \
             patch.object(b, 'guest_exec'), patch.object(b, 'command', side_effect=command):
            b.deploy(self.root, self.job)
        saved = b.load(self.root / 'jobs/1-12345678.json')
        self.assertEqual(saved['state'], 'blocked')
        self.assertTrue(saved['registering'])
        self.assertIn('shepherd@192.168.1.50', saved['viaTarget'])

    def test_registration_recovery_reconciles_machine_without_adding_again(self):
        target = 'ssh -i key shepherd@192.168.1.50'
        self.job.update(registering=True, viaTarget=target, address='192.168.1.50')
        api = FakeAPI()
        calls = []
        def command(args, timeout=30):
            calls.append(args)
            if args[:3] == ['hc', 'machine', 'ls']:
                return json.dumps({'machines': [{'name': 'test-vm', 'target': target}]})
            return ''
        with patch.object(b, 'preflight', return_value=(api, {'node': 'pve', 'vmid': 100}, 'ssh-ed25519 AAAA')), \
             patch.object(b, 'guest_address', return_value='192.168.1.50'), \
             patch.object(b, 'guest_exec'), patch.object(b, 'command', side_effect=command):
            b.deploy(self.root, self.job)
        saved = b.load(self.root / 'jobs/1-12345678.json')
        self.assertEqual(saved['state'], 'done')
        self.assertTrue(saved['registered'])
        self.assertFalse(any(c[:3] == ['hc', 'machine', 'add'] for c in calls))
        self.assertTrue(any(c[:3] == ['hc', 'machine', 'ls'] for c in calls))

    def test_delete_works_without_profile_or_registration_flag(self):
        self.host['profiles'] = []
        b.atomic(self.root / 'config.json', {'hypervisors': [self.host]})
        self.job.update(vmid=200, node='pve')
        api = FakeAPI()
        def get(path):
            if path == 'cluster/resources?type=vm': return [{'vmid': 200}]
            if '/tasks/' in path: return {'status': 'stopped', 'exitstatus': 'OK'}
            if path.endswith('/config'): return {'name': 'test-vm', 'description': 'job ' + self.job['id']}
            if path.endswith('/status/current'): return {'status': 'stopped'}
            raise AssertionError(path)
        api.get = get
        with patch.object(b, 'API', return_value=api), patch.object(b, 'command') as command:
            b.delete_vm(self.root, self.job)
        self.assertEqual(self.job['state'], 'deleted')
        command.assert_not_called()

    def test_delete_waits_for_pending_task_and_never_claims_deleted_on_failure(self):
        self.persist(vmid=200, node='pve', task='UPID:delete', taskKind='delete', attempt='a')
        api = FakeAPI()
        api.get = lambda path: ({'status': 'stopped', 'exitstatus': 'delete failed'} if '/tasks/' in path else [])
        with patch.object(b, 'API', return_value=api), patch.object(b, 'command') as command:
            b.delete_vm(self.root, self.job)
        saved = b.load(self.root / 'jobs/1-12345678.json')
        self.assertEqual(saved['state'], 'blocked')
        self.assertIn('task failed', saved['message'])
        self.assertEqual(saved['task'], 'UPID:delete')
        self.assertTrue(saved['deleteFailed'])
        command.assert_not_called()

    def test_delete_running_task_is_awaited_before_cleanup(self):
        self.persist(vmid=200, node='pve', task='UPID:delete', taskKind='delete', attempt='a')
        api = FakeAPI()
        status = {'status': 'running'}

        def get(path):
            if '/tasks/' in path: return status
            return []

        def wait(node, task):
            status.update(status='stopped', exitstatus='OK')

        api.get = get
        api.wait = MagicMock(side_effect=wait)
        with patch.object(b, 'API', return_value=api), patch.object(b, 'command') as command:
            b.delete_vm(self.root, self.job)
        api.wait.assert_called_once_with('pve', 'UPID:delete')
        self.assertEqual(self.job['state'], 'deleted')

    def test_legacy_job_without_attempt_is_processed(self):
        path = self.root / 'jobs/legacy-12345678.json'
        b.atomic(path, {'id': 'legacy-12345678', 'state': 'working'})
        with patch.object(b, 'deploy') as deploy:
            b.run_worker(self.root, 'legacy-12345678', None)
        deploy.assert_called_once()
        self.assertIn('pid', b.load(path))

    def test_legacy_task_kinds_are_inferred_from_upids(self):
        upid = 'UPID:pve:00001234:00000000:00000000:%s:200:root@pam:'
        self.assertEqual(b.infer_task_kind(upid % 'qmclone'), 'clone')
        self.assertEqual(b.infer_task_kind(upid % 'qmstart'), 'start')
        self.assertEqual(b.infer_task_kind(upid % 'qmstop'), 'stop')
        self.assertEqual(b.infer_task_kind(upid % 'qmdestroy'), 'delete')
        self.assertIsNone(b.infer_task_kind('UPID:unknown'))

    def test_failed_legacy_qmdestroy_never_reports_deleted_when_vm_is_absent(self):
        task = 'UPID:pve:1:1:1:qmdestroy:200:root@pam:'
        self.persist(vmid=200, node='pve', task=task, attempt='a')
        api = FakeAPI()
        api.get = lambda path: ({'status': 'stopped', 'exitstatus': 'delete failed'}
                                if '/tasks/' in path else [])
        with patch.object(b, 'API', return_value=api), patch.object(b, 'command') as command:
            b.delete_vm(self.root, self.job)
        saved = b.load(self.root / 'jobs/1-12345678.json')
        self.assertEqual(saved['state'], 'blocked')
        self.assertEqual(saved['task'], task)
        self.assertTrue(saved['deleteFailed'])
        command.assert_not_called()

    def test_failed_delete_http_with_vm_absent_stays_blocked_on_retry(self):
        self.persist(vmid=200, node='pve', attempt='a')
        api = FakeAPI()
        def get(path):
            if path == 'cluster/resources?type=vm': return [{'vmid': 200}]
            if path.endswith('/config'): return {'name': 'test-vm', 'description': 'job ' + self.job['id']}
            if path.endswith('/status/current'): return {'status': 'stopped'}
            raise AssertionError(path)
        api.get = get
        api.call = MagicMock(side_effect=b.Error('Proxmox HTTP 500'))
        with patch.object(b, 'API', return_value=api), patch.object(b, 'command') as command:
            b.delete_vm(self.root, self.job)
        saved = b.load(self.root / 'jobs/1-12345678.json')
        self.assertEqual(saved['state'], 'blocked')
        self.assertTrue(saved['deleteFailed'])
        # A retry that now sees no VM must not infer success from a failed DELETE.
        api.get = lambda path: ([] if path == 'cluster/resources?type=vm' else {})
        with patch.object(b, 'API', return_value=api):
            b.delete_vm(self.root, self.job)
        saved = b.load(self.root / 'jobs/1-12345678.json')
        self.assertEqual(saved['state'], 'blocked')
        self.assertIn('DELETE failed', saved['message'])
        command.assert_not_called()

    def test_failed_delete_task_with_vm_present_can_be_retried(self):
        task = 'UPID:pve:1:1:1:qmdestroy:200:root@pam:'
        self.persist(vmid=200, node='pve', task=task, attempt='a')
        api = FakeAPI()
        def get(path):
            if path == 'cluster/resources?type=vm': return [{'vmid': 200}]
            if '/tasks/' in path:
                return ({'status': 'stopped', 'exitstatus': 'delete failed'} if 'qmdestroy' in path
                        else {'status': 'stopped', 'exitstatus': 'OK'})
            if path.endswith('/config'): return {'name': 'test-vm', 'description': 'job ' + self.job['id']}
            if path.endswith('/status/current'): return {'status': 'stopped'}
            raise AssertionError(path)
        api.get = get
        with patch.object(b, 'API', return_value=api):
            b.delete_vm(self.root, self.job)
        saved = b.load(self.root / 'jobs/1-12345678.json')
        self.assertEqual(saved['state'], 'blocked')
        self.assertNotIn('task', saved)
        self.assertTrue(saved['deleteFailed'])
        # A second attempt may retry because the VM is still there.
        with patch.object(b, 'API', return_value=api):
            b.delete_vm(self.root, self.job)
        self.assertEqual(b.load(self.root / 'jobs/1-12345678.json')['state'], 'deleted')

    def test_delete_cleans_registered_intent_without_registered_flag(self):
        target = 'ssh -i old-key shepherd@192.168.1.50'
        self.job.update(vmid=200, node='pve', registering=True, viaTarget=target,
                        address='192.168.1.50')
        api = FakeAPI()
        api.get = lambda path: ([] if path == 'cluster/resources?type=vm' else {})
        with patch.object(b, 'API', return_value=api), \
             patch.object(b, 'command', return_value=json.dumps({'machines': [
                 {'name': 'test-vm', 'target': target}]})) as command:
            b.delete_vm(self.root, self.job)
        self.assertEqual(self.job['state'], 'deleted')
        self.assertIn(['hc', 'machine', 'rm', 'test-vm', '--json'], [c.args[0] for c in command.call_args_list])

    def test_resume_keeps_working_when_host_bridge_changed(self):
        self.persist(vmid=200, node='pve', task='UPIDstart', taskKind='start',
                     attempt='a', profileSnapshot={'cores': 2, 'memory': 2048})
        self.host['bridge'] = 'vmbr9'
        b.atomic(self.root / 'config.json', {'hypervisors': [self.host]})
        api = FakeAPI()
        def get(path):
            if path == 'nodes/pve/tasks/UPIDstart/status':
                return {'status': 'stopped', 'exitstatus': 'start failed'}
            if '/tasks/' in path:
                return {'status': 'stopped', 'exitstatus': 'OK'}
            if path == 'cluster/resources?type=vm': return [{'vmid': 200, 'name': 'test-vm'}]
            if path == 'nodes/pve/network': return [{'iface': 'vmbr0', 'active': 1}, {'iface': 'vmbr9', 'active': 0}]
            if path.endswith('/config'): return {'name': 'test-vm', 'description': 'job ' + self.job['id'],
                                                 'net0': 'virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0'}
            if path.endswith('/status/current'): return {'status': 'stopped'}
            raise AssertionError(path)
        api.get = get
        with patch.object(b, 'API', return_value=api), \
             patch.object(b, 'local_preflight', return_value='ssh-ed25519 AAAA'), \
             patch.object(b, 'guest_address', return_value='192.168.1.50'), \
             patch.object(b, 'guest_exec'), patch.object(b, 'command', return_value=''):
            b.deploy(self.root, self.job)
        saved = b.load(self.root / 'jobs/1-12345678.json')
        self.assertEqual(saved['state'], 'done')
        put = next(c for c in api.calls if c[0] == 'PUT')
        self.assertIn('bridge=vmbr0', put[2]['net0'])
        self.assertTrue(any(c[0] == 'POST' and c[1].endswith('/status/start') for c in api.calls))

    def test_windows_deployment_skips_linux_bootstrap_and_checks_remote_core(self):
        self.host['profiles'][0]['bootstrap'] = 'windows-ssh'
        b.atomic(self.root / 'config.json', {'hypervisors': [self.host]})
        api = FakeAPI()
        guest, command = self.execute(api)
        saved = b.load(self.root / 'jobs/1-12345678.json')
        self.assertEqual(saved['state'], 'done')
        guest.assert_called_once()
        self.assertTrue(guest.call_args.kwargs['windows'])
        self.assertEqual(guest.call_args.kwargs['timeout'], 900)
        self.assertIn('New-LocalUser', guest.call_args.args[2])
        commands = [c.args[0] for c in command.call_args_list]
        ssh = next(c for c in commands if c[0] == 'ssh')
        self.assertEqual(ssh[-1], 'cmd.exe /d /c exit 0')
        register = next(c for c in commands if c[:3] == ['hc', 'machine', 'add'])
        self.assertNotIn('--install', register)
        install = next(c for c in commands if c[0] == 'ssh' and '-EncodedCommand' in c[-1])
        self.assertLess(commands.index(install), commands.index(register))
        self.assertEqual(commands[-1], ['hc', 'exec', '--on', 'test-vm', '--', 'cmd.exe', '/d', '/c', 'exit', '0'])
        settings = next(c[2] for c in api.calls if c[0] == 'PUT')
        self.assertNotIn('ciuser', settings)
        self.assertNotIn('sshkeys', settings)

    def windows_api(self):
        api = FakeAPI()
        original_get = api.get
        def get(path):
            if path == 'cluster/resources?type=vm':
                return [{'vmid': 100, 'type': 'qemu', 'template': 1, 'name': 'template', 'node': 'pve'}]
            if path.endswith('/config'):
                return {'ostype': 'win11', 'net0': 'virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0',
                        'name': 'test-vm', 'description': 'job ' + self.job['id']}
            if path == 'nodes/pve/network':
                return [{'iface': 'vmbr0', 'active': 1}]
            return original_get(path)
        api.get = get
        return api

    def test_windows_template_automatically_switches_bootstrap_and_saves_mode(self):
        # Even a Linux cloud-init profile must yield to the actual template OS.
        self.host['profiles'][0]['bootstrap'] = 'cloud-init'
        b.atomic(self.root / 'config.json', {'hypervisors': [self.host]})
        api = self.windows_api()
        with patch.object(b, 'API', return_value=api), \
             patch.object(b, 'local_preflight', return_value='ssh-ed25519 AAAA'), \
             patch.object(b, 'guest_address', return_value='192.0.2.10'), \
             patch.object(b, 'guest_exec') as guest, patch.object(b, 'command', side_effect=successful_command) as command:
            b.deploy(self.root, self.job)
        saved = b.load(self.root / 'jobs/1-12345678.json')
        self.assertEqual(saved['state'], 'done')
        self.assertEqual(saved['profileSnapshot']['bootstrap'], 'windows-ssh')
        guest.assert_called_once()
        self.assertTrue(guest.call_args.kwargs['windows'])
        self.assertEqual(command.call_args.args[0][-5:], ['cmd.exe', '/d', '/c', 'exit', '0'])
        self.assertNotIn('ciuser', next(c[2] for c in api.calls if c[0] == 'PUT'))

    def test_bootstrap_is_selected_from_proxmox_os_type(self):
        for os_type in ('win11', 'win10', 'w2k22', 'w2k19'):
            self.assertEqual(b.bootstrap_mode({}, {'ostype': os_type}), 'windows-ssh')
        self.assertEqual(b.bootstrap_mode({'bootstrap': 'windows-ssh'}, {'ostype': 'l26'}), 'guest-agent')
        self.assertEqual(b.bootstrap_mode({'bootstrap': 'cloud-init'}, {'ostype': 'l26'}), 'cloud-init')
        self.assertEqual(b.bootstrap_mode({}, {'ostype': 'l26', 'name': 'windows-named-linux'}), 'guest-agent')
        self.assertEqual(b.bootstrap_mode({}, {}), 'guest-agent')

    def test_resume_detects_windows_from_existing_vm_without_original_template(self):
        self.persist(vmid=200, node='pve', profileSnapshot={'bootstrap': 'guest-agent'})
        self.host['profiles'] = []
        b.atomic(self.root / 'config.json', {'hypervisors': [self.host]})
        api = self.windows_api()
        with patch.object(b, 'API', return_value=api), \
             patch.object(b, 'local_preflight', return_value='ssh-ed25519 AAAA'), \
             patch.object(b, 'guest_address', return_value='192.0.2.10'), \
             patch.object(b, 'guest_exec') as guest, patch.object(b, 'command', side_effect=successful_command) as command:
            b.deploy(self.root, self.job)
        saved = b.load(self.root / 'jobs/1-12345678.json')
        self.assertEqual(saved['state'], 'done')
        self.assertEqual(saved['profileSnapshot']['bootstrap'], 'windows-ssh')
        self.assertEqual(command.call_args.args[0][-5:], ['cmd.exe', '/d', '/c', 'exit', '0'])
        self.assertFalse(any(c[1].endswith('/clone') for c in api.calls))
        guest.assert_called_once()
        self.assertTrue(guest.call_args.kwargs['windows'])

    def test_bootstrap_cannot_be_overridden_by_deployment_request(self):
        with self.assertRaisesRegex(b.Error, 'Unsupported deployment overrides'):
            b.deployment_profile({}, {'bootstrap': 'windows-ssh'})

    def test_windows_ssh_failure_blocks_without_registration(self):
        self.host['profiles'][0]['bootstrap'] = 'windows-ssh'
        b.atomic(self.root / 'config.json', {'hypervisors': [self.host]})
        api = FakeAPI()
        # Expire the SSH deadline without waiting; guest IP discovery still runs.
        with patch.object(b, 'preflight', return_value=(api, {'node': 'pve', 'vmid': 100}, 'ssh-ed25519 AAAA')), \
             patch.object(b, 'guest_address', return_value='192.0.2.10'), \
             patch.object(b.time, 'monotonic', side_effect=[0, 1, 0, 181]), \
             patch.object(b, 'guest_exec') as guest, patch.object(b, 'command') as command:
            b.deploy(self.root, self.job)
        saved = b.load(self.root / 'jobs/1-12345678.json')
        self.assertEqual(saved['state'], 'blocked')
        self.assertIn('SSH not ready', saved['message'])
        guest.assert_called_once()
        command.assert_not_called()

    def test_guest_exec_encodes_powershell_and_retains_linux_transport(self):
        for windows in (False, True):
            with self.subTest(windows=windows):
                api = MagicMock()
                api.post.return_value = {'pid': 42}
                api.get.return_value = {'exited': True, 'exitcode': 0, 'out-data': 'completed'}
                script = "Write-Output 'quoted \"text\" and Unicode: \u03bb'"
                self.assertEqual(b.guest_exec(api, 'vm', script, windows=windows), 'completed')
                argv = api.post.call_args.kwargs['command']
                if windows:
                    self.assertEqual(argv[0], 'powershell.exe')
                    self.assertEqual(argv[-2], '-EncodedCommand')
                    self.assertEqual(b.base64.b64decode(argv[-1]).decode('utf-16le'), script)
                else:
                    self.assertEqual(argv, ['/bin/sh', '-c', script])

    def test_guest_exec_failure_includes_phase_output_and_error(self):
        api = MagicMock()
        api.post.return_value = {'pid': 42}
        api.get.return_value = {'exited': True, 'exitcode': 1,
                                'out-data': 'Installing OpenSSH Server', 'err-data': 'Windows Update unavailable'}
        with self.assertRaises(b.Error) as error:
            b.guest_exec(api, 'vm', 'script', windows=True)
        self.assertIn('Installing OpenSSH Server', str(error.exception))
        self.assertIn('Windows Update unavailable', str(error.exception))

    def test_windows_bootstrap_failure_never_attempts_ssh_or_registration(self):
        self.host['profiles'][0]['bootstrap'] = 'windows-ssh'
        b.atomic(self.root / 'config.json', {'hypervisors': [self.host]})
        with patch.object(b, 'preflight', return_value=(FakeAPI(), {'node': 'pve', 'vmid': 100}, 'ssh-ed25519 AAAA')), \
             patch.object(b, 'guest_address', return_value='192.0.2.10'), \
             patch.object(b, 'guest_exec', side_effect=b.Error('OpenSSH installation failed')), \
             patch.object(b, 'command') as command:
            b.deploy(self.root, self.job)
        saved = b.load(self.root / 'jobs/1-12345678.json')
        self.assertEqual(saved['state'], 'blocked')
        self.assertIn('OpenSSH installation failed', saved['message'])
        command.assert_not_called()

    def test_windows_bootstrap_data_cannot_inject_powershell(self):
        for user in ('user;whoami', 'user\nadmin', 'a' * 21, 'user.'):
            with self.assertRaises(b.Error):
                b.windows_bootstrap_script(user, 'ssh-ed25519 AAAA', self.job['id'])
        with self.assertRaises(b.Error):
            b.windows_bootstrap_script('shepherd', 'ssh-ed25519 AAAA\nssh-rsa BBBB', self.job['id'])
        with self.assertRaises(b.Error):
            b.windows_bootstrap_script('shepherd', 'ssh-ed25519 AAAA', "bad';whoami")
        key = "ssh-ed25519 AAAA comment ' $(Invoke-Expression bad)"
        script = b.windows_bootstrap_script('shepherd', key, self.job['id'])
        encoded = script.split("FromBase64String('", 1)[1].split("')", 1)[0]
        payload = json.loads(b.base64.b64decode(encoded))
        self.assertEqual(payload, {'user': 'shepherd', 'publicKey': key, 'job': self.job['id']})
        self.assertNotIn('Invoke-Expression', script)

    def test_windows_core_install_runs_as_ssh_user_with_local_release(self):
        ssh = ['ssh', '-i', '/example/key', 'shepherd@192.0.2.10']
        with patch.object(b, 'command', side_effect=['hc 0.1.15\n', 'verified']) as command:
            self.assertEqual(b.install_windows_core(ssh), 'verified')
        self.assertEqual(command.call_args_list[0].args[0], ['hc', '--version'])
        remote = command.call_args_list[1]
        self.assertEqual(remote.args[0][:-1], ssh)
        self.assertEqual(remote.kwargs['timeout'], 600)
        encoded = remote.args[0][-1].split('-EncodedCommand ', 1)[1]
        script = b.base64.b64decode(encoded).decode('utf-16le')
        self.assertTrue(script.startswith("$version = '0.1.15'\n"))
        self.assertIn('https://releases.herdr.dev/core/install.ps1', script)

    def test_windows_core_install_rejects_unrecognized_local_version(self):
        with patch.object(b, 'command', return_value='unexpected version output') as command:
            with self.assertRaisesRegex(b.Error, 'Cannot determine the local hc release'):
                b.install_windows_core(['ssh', 'guest'])
        self.assertEqual(command.call_count, 1)

    def test_windows_core_install_failure_blocks_before_registration(self):
        self.host['profiles'][0]['bootstrap'] = 'windows-ssh'
        b.atomic(self.root / 'config.json', {'hypervisors': [self.host]})
        with patch.object(b, 'install_windows_core', side_effect=b.Error('Windows download failed')):
            _, command = self.execute(FakeAPI())
        saved = b.load(self.root / 'jobs/1-12345678.json')
        self.assertEqual(saved['state'], 'blocked')
        self.assertIn('Windows download failed', saved['message'])
        self.assertFalse(any(c.args[0][:3] == ['hc', 'machine', 'add'] for c in command.call_args_list))

    def test_linux_never_uses_windows_core_installer(self):
        with patch.object(b, 'install_windows_core') as install:
            _, command = self.execute(FakeAPI())
        install.assert_not_called()
        registration = next(c.args[0] for c in command.call_args_list if c.args[0][:3] == ['hc', 'machine', 'add'])
        self.assertIn('--install', registration)

    def test_windows_registration_recovery_does_not_use_install_flag(self):
        target = 'ssh -i key shepherd@192.0.2.10'
        self.persist(vmid=200, node='pve', profileSnapshot={'bootstrap': 'windows-ssh'},
                     registering=True, viaTarget=target)
        def commands(args, **kwargs):
            if args == ['hc', 'machine', 'ls', '--json']:
                return json.dumps({'machines': []})
            return successful_command(args, **kwargs)
        with patch.object(b, 'API', return_value=self.windows_api()), \
             patch.object(b, 'local_preflight', return_value='ssh-ed25519 AAAA'), \
             patch.object(b, 'guest_address', return_value='192.0.2.10'), \
             patch.object(b, 'guest_exec'), patch.object(b, 'command', side_effect=commands) as command:
            b.deploy(self.root, self.job)
        self.assertEqual(b.load(self.root / 'jobs/1-12345678.json')['state'], 'done')
        registration = next(c.args[0] for c in command.call_args_list if c.args[0][:3] == ['hc', 'machine', 'add'])
        self.assertNotIn('--install', registration)

    def test_inventory_does_not_return_unused_vms(self):
        api = MagicMock()
        api.get.side_effect = lambda path: ([{'node': 'pve', 'status': 'online'}] if path == 'nodes' else
                                            [{'type': 'qemu', 'name': 'template', 'template': 1, 'node': 'pve', 'vmid': 100},
                                             {'type': 'qemu', 'name': 'vm', 'node': 'pve', 'vmid': 101}])
        with patch.object(b, 'API', return_value=api):
            result = b.inventory({'profiles': []})
        self.assertNotIn('vms', result)
        self.assertEqual(result['nodes'], [{'node': 'pve', 'status': 'online'}])
        self.assertEqual([p['templateVmid'] for p in result['profiles']], [100])

    def test_inventory_lists_unconfigured_and_duplicate_named_templates(self):
        api = MagicMock()
        templates = [{'type': 'qemu', 'name': name, 'template': 1, 'node': node, 'vmid': vmid}
                     for name, node, vmid in [('custom', 'a', 100), ('custom', 'b', 101), ('other', 'b', 102)]]
        api.get.side_effect = lambda path: [] if path == 'nodes' else templates
        with patch.object(b, 'API', return_value=api):
            result = b.inventory({'profiles': [{'id': 'legacy', 'template': 'custom', 'cores': 4}]})
        self.assertEqual([p['id'] for p in result['profiles']], ['template-100', 'template-101', 'template-102'])
        self.assertTrue(all(p['available'] for p in result['profiles']))
        self.assertEqual(result['profiles'][0]['cores'], 4)
        self.assertNotIn('cores', result['profiles'][2])
        with patch.object(b, 'API', return_value=api), patch.object(b, 'local_preflight'), patch.object(b, 'check_bridge'):
            api.get.side_effect = lambda path: templates if path == 'cluster/resources?type=vm' else {}
            _, template, _ = b.preflight({}, result['profiles'][1], 'new-vm')
        self.assertEqual(template['vmid'], 101)


if __name__ == '__main__': unittest.main()

import { View, div, svg } from 'gpui-kit';
import { v_flex, h_flex, Input, InputState } from 'gpui-base';
import { appHome, exec, readTextFile, writeTextFile, theme, run, machines, nextCommand } from 'herdr';

const presets = [
  { id: 'ubuntu', name: 'Ubuntu 24.04', template: 'ubuntu-24.04-x64-server-template', cores: 2, memory: 2048 },
  { id: 'debian', name: 'Debian 12', template: 'debian-12-x64-server-template', cores: 2, memory: 2048 },
];

export default class Shepherd extends View {
  init(_props, cx) {
    this.root = appHome() + '/data/shepherd';
    this.script = appHome() + '/mods/shepherd/bridge.py';
    this.config = { hypervisors: [] };
    this.jobs = [];
    this.inventory = null;
    this.selected = '';
    this.profile = 'ubuntu';
    this.error = '';
    this.page = 'home';
    this.loading = false;
    this.launching = false;
    this.polling = false;
    this.ticks = 0;
    this.vmName = InputState.new({ placeholder: 'e.g. ubuntu-dev' });
    this.vmBridge = InputState.new({ placeholder: 'Default from profile / hypervisor' });
    this.vmPool = InputState.new({ placeholder: 'Pool ID · blank = profile default / none' });
    this.advanced = false;
    this.fields = {};
    for (const key of ['name', 'url', 'tokenId', 'tokenSecret', 'sshUser', 'sshKey', 'bridge', 'tlsFingerprint']) {
      this.fields[key] = InputState.new({ placeholder: key });
    }
    this.fields.tokenSecret.set_masked(true);
    cx.spawn(async (cx) => {
      try {
        await this.call('init');
        await this.loadConfig();
        this.jobs = await this.call('jobs');
        cx.notify();
        this.refresh(cx);
      } catch (e) { this.error = String(e); cx.notify(); }
    });
    cx.timer.every(3000, (cx) => {
      this.ticks++;
      if (this.jobs.some(j => j.state === 'working') || this.ticks % 5 === 0) this.poll(cx);
    });
    cx.spawn(async (cx) => {
      for (;;) {
        const command = await nextCommand();
        if (command === 'refresh') this.refresh(cx);
        if (command === 'new') this.page = 'create';
        if (command === 'settings') this.editSettings(false, cx);
        if (command === 'home') this.page = 'home';
        cx.notify();
      }
    });
  }

  async call(action, value) {
    const args = [this.script, '--root', this.root, action];
    if (value !== undefined) args.push(typeof value === 'string' ? value : JSON.stringify(value));
    // Route through the local core, which owns machine access and its environment.
    const result = await exec('hc', ['exec', '--', 'python3', ...args]);
    let payload;
    try { payload = JSON.parse(result.stdout); }
    catch (_) { throw new Error(result.stderr || 'Shepherd worker returned invalid output.'); }
    if (!payload.ok) throw new Error(payload.error);
    return payload.data;
  }

  async loadConfig() {
    this.rawConfig = await readTextFile(this.root + '/config.json');
    this.config = this.rawConfig ? JSON.parse(this.rawConfig) : { hypervisors: [] };
    if (!this.config.hypervisors.some(h => h.id === this.selected)) {
      this.selected = this.config.hypervisors[0]?.id || '';
    }
  }

  refresh(cx) {
    if (this.loading || !this.selected) return;
    const selected = this.selected;
    this.loading = true;
    this.error = '';
    cx.notify();
    cx.spawn(async (cx) => {
      try {
        const inventory = await this.call('inventory', selected);
        if (this.selected === selected) this.inventory = inventory;
      } catch (e) { this.error = String(e); }
      this.loading = false;
      cx.notify();
    });
  }

  poll(cx) {
    if (this.polling) return;
    this.polling = true;
    cx.spawn(async (cx) => {
      try { this.jobs = await this.call('jobs'); }
      catch (e) { this.error = String(e); }
      this.polling = false;
      cx.notify();
    });
  }

  editSettings(isNew, cx) {
    const host = isNew ? {} : this.config.hypervisors.find(h => h.id === this.selected) || {};
    this.editId = host.id || null;
    this.editLinkedClone = host.linkedClone !== false;
    for (const key of Object.keys(this.fields)) this.fields[key].set_value(host[key] || ({ sshUser: 'shepherd', sshKey: '~/.ssh/id_ed25519' }[key] || ''));
    this.page = 'settings';
    cx.notify();
  }

  saveSettings(cx) {
    if (this.saving) return;
    this.saving = true;
    cx.spawn(async (cx) => {
      try {
        const values = {};
        for (const key of Object.keys(this.fields)) values[key] = this.fields[key].value().trim();
        if (!values.name || !values.url.startsWith('https://') || !values.tokenId || !values.tokenSecret) throw new Error('Name, HTTPS URL and token are required.');
        if (values.tlsFingerprint && !/^[a-fA-F0-9]{64}$/.test(values.tlsFingerprint.replace(/:/g, ''))) throw new Error('TLS fingerprint must contain 64 hexadecimal digits.');
        const hosts = this.config.hypervisors.slice();
        const index = hosts.findIndex(h => h.id === this.editId);
        const host = { ...(index >= 0 ? hosts[index] : { id: 'pve-' + Date.now(), profiles: presets }), ...values,
          linkedClone: this.editLinkedClone };
        delete host.passwordlessSudo;
        if (index >= 0) hosts[index] = host; else hosts.push(host);
        const raw = JSON.stringify({ ...this.config, hypervisors: hosts }, null, 2);
        await writeTextFile(this.root + '/config.json', raw, this.rawConfig);
        await this.loadConfig();
        this.selected = host.id;
        this.deletePending = null;
        this.hoverJob = null;
        this.inventory = null;
        this.page = 'home';
        this.refresh(cx);
      } catch (e) { this.error = String(e); }
      this.saving = false;
      cx.notify();
    });
  }

  deploy(cx) {
    if (this.launching || this.jobs.some(j => j.state === 'working')) return;
    this.launching = true;
    this.error = '';
    cx.notify();
    cx.spawn(async (cx) => {
      try {
        const job = await this.call('start', { host: this.selected, profile: this.profile, name: this.vmName.value().trim(),
          overrides: { bridge: this.vmBridge.value().trim(), pool: this.vmPool.value().trim() } });
        this.jobs.unshift(job);
        this.page = 'home';
        this.vmName.set_value('');
        this.vmBridge.set_value('');
        this.vmPool.set_value('');
        this.advanced = false;
      } catch (e) { this.error = String(e); }
      this.launching = false;
      cx.notify();
    });
  }

  jobAction(action, job, cx) {
    if (this.launching || this.jobs.some(j => j.state === 'working')) return;
    if (job.host !== this.selected) {
      this.deletePending = null;
      this.error = 'The selected hypervisor changed. Select the VM again.';
      cx.notify();
      return;
    }
    this.launching = true;
    this.deletePending = null;
    this.error = '';
    cx.notify();
    cx.spawn(async (cx) => {
      try { await this.call(action, job.id); this.jobs = await this.call('jobs'); }
      catch (e) { this.error = String(e); }
      this.launching = false;
      cx.notify();
    });
  }

  button(id, label, action, primary = false, enabled = true) {
    const t = theme();
    return div().id(id).px(10).py(7).rounded(6).text_size(12)
      .bg(primary && enabled ? t.solid : t.raised)
      .text_color(!enabled ? t.dim : primary ? t.solid_foreground : t.foreground)
      .hover(s => s.bg(enabled ? t.selected : t.raised))
      .child(label).on_click((_e, cx) => { if (enabled) action(cx); });
  }

  field(label, state) {
    return v_flex().gap(5).child(div().text_size(11).text_color(theme().muted).child(label))
      .child(Input.new(state).h(32).px(8).bg(theme().canvas).border(1).border_color(theme().input).rounded(5));
  }

  iconButton(id, icon, action, enabled = true) {
    const t = theme();
    return div().id(id).size(28).flex_none().flex().items_center().justify_center().rounded(5)
      .text_color(enabled ? t.accent : t.dim).hover(s => s.bg(t.selected))
      .child(svg('mods/shepherd/' + icon + '.svg').size(16))
      .on_click((_e, cx) => { if (enabled) action(cx); });
  }

  render() {
    const t = theme();
    const host = this.config.hypervisors.find(h => h.id === this.selected);
    const working = this.jobs.some(j => j.state === 'working');
    const localJobs = this.jobs.filter(j => j.host === this.selected && j.state !== 'deleted');
    const hoveredJob = localJobs.find(j => j.id === this.hoverJob);
    const availableMachines = new Set(machines().map(m => m.name));
    const content = v_flex().gap(14);
    if (this.page === 'settings') {
      content.child(div().text_size(16).font_semibold().child(this.editId ? 'Hypervisor settings' : 'Add hypervisor'));
      for (const [key, label] of Object.entries({ name: 'Display name', url: 'Proxmox HTTPS URL', tokenId: 'API token ID', tokenSecret: 'API token secret', sshUser: 'Guest SSH user', sshKey: 'Local SSH private key path', bridge: 'Network bridge · blank = template setting', tlsFingerprint: 'TLS SHA-256 fingerprint · blank = system CA' })) content.child(this.field(label, this.fields[key]));
      content.child(h_flex().id('linked-clone').gap(8).items_center().py(6).cursor_pointer()
        .child(div().text_color(this.editLinkedClone ? t.accent : t.muted).child(this.editLinkedClone ? '☑' : '☐'))
        .child(div().text_size(12).child('Linked clone'))
        .on_click((_e, c) => { this.editLinkedClone = !this.editLinkedClone; c.notify(); }));
      content.child(div().text_size(11).text_color(t.muted).child('Checked: fast clone sharing the template base disk. Unchecked: independent full clone. Linked clones use template storage.'));
      content.child(div().text_size(11).text_color(t.muted).child('SSH key login; no account password is generated. Use the VM local administrator for privileged operations.'));
      content.child(div().text_size(11).text_color(t.muted).child('Saved locally in data/shepherd/config.json. Profiles inherit guest networking; advanced settings live in that file.'));
      content.child(h_flex().gap(8)
        .child(this.button('save', this.saving ? 'Saving…' : 'Save hypervisor', c => this.saveSettings(c), true, !this.saving))
        .child(this.button('cancel-settings', 'Back', c => { this.page = 'home'; c.notify(); })));
    } else {
      content.child(h_flex().justify_between().items_center()
        .child(div().text_size(10).font_semibold().text_color(t.muted).child('HYPERVISORS'))
        .child(this.button('add-host', '+ Add', c => this.editSettings(true, c))));
      const hosts = v_flex().gap(4);
      for (const h of this.config.hypervisors) {
        hosts.child(h_flex().id('host-' + h.id).h(32).px(8).gap(8).items_center().rounded(5)
          .bg(h.id === this.selected ? t.selected : t.background).hover(s => s.bg(t.hover))
          .child(svg('mods/shepherd/server.svg').size(14).flex_none().text_color(h.id === this.selected ? t.accent : t.muted))
          .child(div().flex_1().min_w(0).truncate().text_size(12).font_semibold().child(h.name))
          .child(div().max_w(170).truncate().text_size(10).text_color(t.muted).child(h.url.replace(/^https:\/\//, '')))
          .on_click((_e, c) => {
            if (this.loading) return;
            this.deletePending = null;
            this.hoverJob = null;
            this.vmBridge.set_value('');
            this.vmPool.set_value('');
            this.selected = h.id; this.inventory = null; this.refresh(c);
          }));
      }
      content.child(hosts);
      if (!host) content.child(div().text_color(t.muted).child('Add your Proxmox hypervisor to get started.'));
      if (host) {
        content.child(h_flex().gap(8)
          .child(this.button('new', '+ Deploy VM', c => { this.page = 'create'; c.notify(); }, true, !working))
          .child(this.button('refresh', this.loading ? 'Connecting…' : 'Refresh', c => this.refresh(c), false, !this.loading))
          .child(this.button('settings', 'Settings', c => this.editSettings(false, c))));
        if (this.inventory) {
          content.child(div().text_size(11).text_color(t.muted).child(this.inventory.nodes.map(n => n.node + ' · ' + n.status).join('  /  ')));
          if (this.inventory.warning) content.child(div().text_color(t.blocked).child(this.inventory.warning));
        }
      }
      if (this.page === 'create') {
        const selector = v_flex().gap(4)
          .child(div().text_size(11).text_color(t.muted).child('Choose a VM template'));
        for (const p of this.inventory?.profiles || []) {
          selector.child(h_flex().id('profile-' + p.id).h(32).px(8).gap(8).items_center().rounded(5)
            .bg(this.profile === p.id ? t.selected : t.background)
            .hover(s => s.bg(t.hover))
            .child(div().text_size(12).text_color(this.profile === p.id ? t.accent : t.dim).child(this.profile === p.id ? '●' : '○'))
            .child(div().flex_1().min_w(0).truncate().text_size(12).text_color(p.available ? t.foreground : t.dim).child(p.name))
            .child(div().flex_none().text_size(10).text_color(p.available ? t.muted : t.blocked)
              .child(p.available ? p.node + ' · ' + p.templateVmid : 'Unavailable'))
            .on_click((_e, c) => { if (p.available) { this.profile = p.id; c.notify(); } }));
        }
        content.child(selector);
        content.child(this.field('Machine name', this.vmName));
        content.child(this.button('advanced', this.advanced ? '▾ Advanced settings' : '▸ Advanced settings', c => {
          this.advanced = !this.advanced; c.notify();
        }));
        if (this.advanced) {
          const profile = this.inventory?.profiles.find(p => p.id === this.profile);
          content.child(v_flex().gap(10)
            .child(this.field('Network bridge · default: ' + (profile?.bridge || host?.bridge || 'template'), this.vmBridge))
            .child(this.field('Resource Pool ID · default: ' + (profile?.pool || 'none'), this.vmPool))
            .child(div().text_size(11).text_color(t.muted).child('Overrides apply only to this VM. The pool must already exist; it groups resources and permissions, not VM settings.')));
        }
        content.child(div().text_size(11).text_color(t.muted).child((host?.linkedClone === false ? 'Full clone' : 'Linked clone') + ' → start → install hc → add to Machines. The template’s disk and network settings are inherited.'));
        content.child(h_flex().gap(8)
          .child(this.button('deploy', this.launching ? 'Starting…' : 'Create & start', c => this.deploy(c), true,
            !this.launching && !working && !!this.inventory?.profiles.some(p => p.id === this.profile && p.available)))
          .child(this.button('cancel-create', 'Cancel', c => { this.page = 'home'; c.notify(); })));
      }
      content.child(h_flex().justify_between().items_center()
        .child(div().text_size(10).font_semibold().text_color(t.muted).child('DEPLOYMENTS'))
        .child(div().text_size(11).text_color(t.dim).child(String(localJobs.length))));
      if (this.deletePending?.host === this.selected) {
        const pending = this.deletePending;
        content.child(v_flex().p(10).gap(8).bg(t.raised).rounded(6)
          .child(div().text_size(12).child('Delete ' + pending.name + ' (VM ' + pending.vmid + ')? The VM and its disks will be deleted and the machine removed from Herdr.'))
          .child(h_flex().gap(8)
            .child(this.button('confirm-delete', 'Confirm deletion', c => this.jobAction('delete', pending, c), true, !working && !this.launching))
            .child(this.button('cancel-delete', 'Cancel', c => { this.deletePending = null; c.notify(); }))));
      }
      if (!localJobs.length) content.child(v_flex().p(16).gap(7).rounded(8).bg(t.canvas)
        .child(div().font_semibold().child('Your next machine starts here.'))
        .child(div().text_size(12).text_color(t.muted).child('Choose a compatible VM template. Shepherd handles provisioning and connects the new machine to Herdr.')));
      const table = v_flex().gap(0);
      for (const job of localJobs) {
        const color = t[job.state] || t.idle;
        const row = h_flex().id('job-' + job.id).h(34).gap(8).items_center().min_w(0)
          .hover(s => s.bg(t.hover))
          .on_hover((hovered, c) => { this.hoverJob = hovered ? job.id : null; c.notify(); });
        const status = div().w(67).flex_none().text_size(11).text_color(color);
        if (job.state === 'working') status.frames(['◐ Working', '◓ Working', '◑ Working', '◒ Working'], 250);
        else status.child(job.state === 'done' ? '● Ready' : job.state === 'blocked' ? '● Blocked' : '○ Idle');
        row.child(div().flex_1().min_w(0).truncate().text_size(12).child(job.name))
          .child(div().w(30).flex_none().text_size(10).text_color(t.dim).child(job.vmid ? String(job.vmid) : '—'))
          .child(status);
        if (job.state === 'blocked') row.child(this.iconButton('resume-' + job.id, 'retry', c => {
          if (job.operation === 'delete') { this.deletePending = job; c.notify(); return; }
          this.jobAction('resume', job, c);
        }, !working && !this.launching));
        if (job.state === 'done') {
          row.child(this.iconButton('open-' + job.id, 'play', () => run('space.new', { name: job.name, machine: job.machine }), availableMachines.has(job.machine)));
        }
        if (job.state !== 'done' && job.state !== 'blocked') row.child(div().w(28).flex_none());
        row.child(this.iconButton('delete-' + job.id, 'delete', c => {
          this.deletePending = job;
          c.notify();
        }, !!job.vmid && !working && !this.launching));
        table.child(row);
      }
      content.child(table);
    }
    return v_flex().id('shepherd').size_full().min_h(0).rounded(10).overflow_hidden()
      .bg(t.background).border_1().border_color(t.border).text_color(t.foreground).text_size(13)
      .child(h_flex().h(42).flex_none().px(14).gap(8).items_center().border_b(1).border_color(t.border)
        .child(div().font_semibold().child('Shepherd'))
        .child(div().flex_1().min_w(0).truncate().text_right().text_size(12).text_color(t.dim).child('Proxmox')))
      .child(v_flex().id('shepherd-scroll').flex_1().min_h(0).overflow_y_scrollbar().p(14).gap(12)
        .when(!!this.error, el => el.child(div().p(10).rounded(6).bg(t.canvas).text_size(12).text_color(t.blocked).child(this.error)))
        .child(content))
      .child(div().flex_none().px(14).py(10).border_t(1).border_color(t.border).text_size(10).text_color(hoveredJob?.state === 'blocked' ? t.blocked : t.dim)
        .child(hoveredJob ? hoveredJob.message + (hoveredJob.address ? ' · ' + hoveredJob.address : '') : 'LOCAL CONTROL  ·  API + SSH  ·  ' + (working ? 'Deployment in progress' : 'Ready')));
  }
}

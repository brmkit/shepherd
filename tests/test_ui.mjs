// Behavioral checks only; native rendering is verified with herdr-app check.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const elements = new Map();
function element() {
  const state = {};
  const proxy = new Proxy(state, {
    get(target, name) {
      if (name === 'handler') return target.handler;
      if (name === 'id') return id => { elements.set(id, proxy); return proxy; };
      if (name === 'on_click') return handler => { target.handler = handler; return proxy; };
      if (name === 'when') return (condition, callback) => condition ? callback(proxy) : proxy;
      return () => proxy;
    },
  });
  return proxy;
}
const source = fs.readFileSync(new URL('../mods/shepherd/main.js', import.meta.url), 'utf8')
  .replace(/^import .*;\n/gm, '').replace('export default class Shepherd', 'globalThis.Shepherd = class Shepherd');
const context = vm.createContext({
  View: class {}, div: element, svg: element, h_flex: element, v_flex: element,
  Input: { new: element }, theme: () => ({}), machines: () => [{ name: 'test-vm' }],
});
vm.runInContext(source, context);
const view = new context.Shepherd();
const job = { id: 'job', host: 'pve', name: 'test-vm', machine: 'test-vm', vmid: 200, state: 'done' };
Object.assign(view, {
  page: 'home', selected: 'pve', config: { hypervisors: [{ id: 'pve', name: 'Proxmox', url: 'https://pve' }] },
  jobs: [job], launching: false, inventory: null,
});
const calls = [], pending = [];
view.call = async (action, id) => { calls.push([action, id]); return action === 'jobs' ? [job] : {}; };
const cx = { notify() {}, spawn(fn) { pending.push(fn(cx)); } };
function click(id) { elements.get(id).handler({}, cx); }

view.render();
click('delete-job');
assert.equal(view.deletePending.id, 'job');
assert.equal(calls.length, 0, 'Trash must not delete before approval');
view.render();
click('cancel-delete');
assert.equal(view.deletePending, null);
assert.equal(calls.length, 0, 'Cancel must not execute any action');
view.render();
click('delete-job');
view.render();
click('confirm-delete');
await Promise.all(pending);
assert.equal(calls[0][0], 'delete');
assert.equal(calls[0][1], 'job');
assert.equal(view.launching, false);
calls.length = 0;
job.state = 'blocked';
job.operation = 'delete';
view.render();
click('resume-job');
assert.equal(view.deletePending.id, 'job');
assert.equal(calls.length, 0, 'Deletion retry must require fresh approval');
view.selected = 'other-host';
view.jobAction('delete', job, cx);
assert.equal(calls.length, 0, 'Stale approval cannot operate on a different selected host');
assert.equal(view.deletePending, null);
view.selected = 'pve';
view.deletePending = job;
view.editId = null;
view.editLinkedClone = true;
view.fields = Object.fromEntries(Object.entries({ name: 'New host', url: 'https://new-host', tokenId: 'test', tokenSecret: 'test' })
  .map(([key, value]) => [key, { value: () => value }]));
context.writeTextFile = async (_path, text) => { view.config = JSON.parse(text); };
view.loadConfig = async () => {};
view.refresh = () => {};
view.saveSettings(cx);
await Promise.all(pending);
assert.notEqual(view.selected, 'pve');
assert.equal(view.deletePending, null, 'Saving another host must clear pending approval');
console.log('UI regression checks passed: delete approval, cancel, confirmed action, retry approval, stale-host rejection, add-host reset.');

// Platform detection belongs to the worker, not the creation form.
const input = value => ({ value: () => value, set_value(next) { value = next; } });
Object.assign(view, {
  page: 'create', selected: 'pve', profile: 'template-win', jobs: [],
  config: { hypervisors: [{ id: 'pve', name: 'Proxmox', url: 'https://pve' }] },
  inventory: { nodes: [], profiles: [
    { id: 'template-win', name: 'Windows', available: true },
    { id: 'template-linux', name: 'Linux', available: true },
  ] },
  vmName: input('win-dev'), vmBridge: input(''), vmPool: input(''),
});
elements.clear();
view.render();
assert.equal(elements.has('bootstrap-windows'), false);
assert.equal(elements.has('bootstrap-linux'), false);
click('profile-template-win');
calls.length = 0;
view.call = async (action, value) => { calls.push([action, value]); return { id: 'windows-job', state: 'working' }; };
view.deploy(cx);
await Promise.all(pending);
assert.equal(calls[0][0], 'start');
assert.equal(calls[0][1].profile, 'template-win');
assert.equal('bootstrap' in calls[0][1].overrides, false);
console.log('Windows UI checks passed: ordinary template selection, no manual bootstrap override.');

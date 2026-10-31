#!/usr/bin/env python3
"""Install the local mod; leave an existing installation alone unless --update is given."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess

parser = argparse.ArgumentParser()
parser.add_argument('--update', action='store_true')
args = parser.parse_args()
source = Path(__file__).resolve().parent
home = Path(os.environ.get('HERDR_APP_HOME', Path.home() / '.config/herdr-app'))
target = home / 'mods/shepherd'
layout_path = home / 'layouts/shepherd.json'
new_layout = not layout_path.exists()
if not new_layout and not args.update:
    parser.error('Layout shepherd already exists; inspect it before --update.')
if any((home / 'mods' / name).exists() for name in ('shepherd', 'shepherd-tabs')) and not args.update:
    parser.error('Shepherd already exists. Inspect it, then use --update for your own installation.')
for name in ('shepherd', 'shepherd-tabs'):
    subprocess.run(['herdr-app', 'check', str(source / 'mods' / name)], check=True)
    dest = home / 'mods' / name
    dest.mkdir(parents=True, exist_ok=True)
    for path in (source / 'mods' / name).iterdir():
        if path.is_file() and path.suffix in ('.js', '.json', '.py', '.svg') and path.name != 'jsconfig.json':
            shutil.copy2(path, dest / path.name)
subprocess.run(['python3', str(target / 'bridge.py'), '--root', str(home / 'data/shepherd'), 'init'], check=True)
keys_path = home / 'keys.json'
keys = json.loads(keys_path.read_text()) if keys_path.exists() else {}
snippet = json.loads((source / 'keys.json').read_text())
for section, bindings in snippet.items():
    for key, command in bindings.items():
        if key in keys.get(section, {}) and keys[section][key] != command:
            print('Skipped existing binding: ' + section + '/' + key)
        else:
            keys.setdefault(section, {})[key] = command
if keys_path.exists():
    backup = keys_path.with_suffix('.json.before-shepherd')
    if not backup.exists():
        shutil.copy2(keys_path, backup)
keys_path.write_text(json.dumps(keys, indent=2) + '\n')
subprocess.run(['herdr-app', 'check', str(target)], check=True)
if new_layout:
    subprocess.run(['herdr-app', 'do', 'layout.clone', '{"name":"main","as":"shepherd"}'], check=True)
layout = json.loads(layout_path.read_text())

def find(node, name):
    if node.get('id') == name:
        return node
    for child in node.get('children', []):
        match = find(child, name)
        if match is not None:
            return match

tabs = find(layout, 'tabs')
body = find(layout, 'panel-body')
panel = find(layout, 'panel')
if not tabs or not body or not panel:
    parser.error('This main layout does not have tabs/panel/panel-body; integrate Shepherd manually.')
tabs['content'] = {'mod': 'shepherd-tabs'}
if not find(body, 'shepherd'):
    body['children'].append({'id': 'shepherd', 'type': 'block', 'content': {'mod': 'shepherd'}})
if new_layout:
    body['active'] = 'shepherd'
    panel['hidden'] = False
    panel['size'] = {'px': 390}
layout_path.write_text(json.dumps(layout, indent=2) + '\n')
subprocess.run(['herdr-app', 'check', str(home / 'mods/shepherd-tabs')], check=True)
if new_layout:
    subprocess.run(['herdr-app', 'do', 'layout.switch', '{"name":"shepherd"}'], check=True)
print('Installed: ' + str(target))

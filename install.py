#!/usr/bin/env python3
"""Install the local mod; leave an existing installation alone unless --update is given."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
from integration import prepare, integrate

parser = argparse.ArgumentParser()
parser.add_argument('--update', action='store_true')
parser.add_argument('--layout', help='Saved layout to integrate; defaults to the current layout.')
args = parser.parse_args()
source = Path(__file__).resolve().parent
home = Path(os.environ.get('HERDR_APP_HOME', Path.home() / '.config/herdr-app'))
target = home / 'mods/shepherd'
if target.exists() and not args.update:
    parser.error('Shepherd already exists. Inspect it, then use --update for your own installation.')
try:
    plan = prepare(home, args.layout)
except (ValueError, OSError) as error:
    parser.error(str(error))
runtime = source / 'mods/shepherd'
subprocess.run(['herdr-app', 'check', str(runtime)], check=True)
target.mkdir(parents=True, exist_ok=True)
for path in runtime.iterdir():
    if path.is_file() and path.suffix in ('.js', '.json', '.py', '.ps1', '.svg') and path.name != 'jsconfig.json':
        shutil.copy2(path, target / path.name)
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
integrate(home, plan)
print('Installed: ' + str(target))

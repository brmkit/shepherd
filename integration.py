"""Add Shepherd to the user's existing Files/Changes integration."""
import copy
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time


def find(node, name):
    if node.get('id') == name:
        return node
    for child in node.get('children', []):
        match = find(child, name)
        if match is not None:
            return match


def patch_tabs(text):
    """Keep all existing buttons and code; add only the missing Shepherd hooks."""
    enabled = list(re.finditer(r'^(\s*)const enabled = (.+);$', text, re.M))
    if len(enabled) != 1:
        raise ValueError('Unsupported tab bar: expected one enabled panel list.')
    match = enabled[0]
    expression = match.group(2)
    if not re.search(r'[\"\']shepherd[\"\']', expression):
        text = text[:match.start()] + match.group(1) + 'const enabled = [...(' + expression + '), "shepherd"];' + text[match.end():]
    if not re.search(r'panelButton\(\s*[\"\']shepherd[\"\']', text):
        anchors = list(re.finditer(r'^([ \t]*)\.when\(enabled\.includes\("changes"\).+$', text, re.M))
        if len(anchors) != 1:
            raise ValueError('Unsupported tab bar: cannot locate the Changes button.')
        anchor = anchors[0]
        text = text[:anchor.end()] + '\n' + anchor.group(1) + '.child(panelButton("shepherd", "Shepherd", "mods/shepherd/server.svg"))' + text[anchor.end():]
    return text


def run(command, **kwargs):
    return subprocess.run(['herdr-app'] + command, check=True, **kwargs)


def unused(parent, name):
    candidate = name
    index = 2
    while (parent / candidate).exists():
        candidate = name + '-' + str(index)
        index += 1
    return candidate


def replace(path, text):
    if path.read_text() == text:
        return
    shutil.copy2(path, path.with_name(path.name + '.before-shepherd-' + str(time.time_ns())))
    with tempfile.NamedTemporaryFile(mode='w', prefix='.', dir=path.parent, delete=False) as stream:
        stream.write(text)
        temporary = Path(stream.name)
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def prepare(home, name=None):
    if name is None:
        name = json.loads(run(['status'], capture_output=True, text=True).stdout)['layout']
    if not isinstance(name, str) or Path(name).name != name:
        raise ValueError('Select a saved layout before installing Shepherd.')
    path = home / 'layouts' / (name + '.json')
    installed = json.loads((home / 'installed.json').read_text())
    if 'layout:' + name in installed and (home / 'layouts/Tabs.json').exists():
        raise ValueError('Tabs already exists. Select it or pass --layout Tabs to reuse it.')
    layout = json.loads(path.read_text())
    tabs, body, panel = find(layout, 'tabs'), find(layout, 'panel-body'), find(layout, 'panel')
    if not tabs or not body or not panel or body.get('type') != 'stack':
        raise ValueError('The selected layout needs tabs, panel, and a panel-body stack; integrate Shepherd manually.')
    existing = find(layout, 'shepherd')
    if existing is not None and (existing not in body.get('children', []) or existing.get('content', {}).get('mod') != 'shepherd'):
        raise ValueError('The layout already uses the shepherd block ID for another purpose.')
    mod = tabs.get('content', {}).get('mod')
    if not isinstance(mod, str) or Path(mod).name != mod:
        raise ValueError('The selected layout has no supported tab-bar mod.')
    folder = home / 'mods' / mod
    original = (folder / 'main.js').read_text()
    patched = patch_tabs(original)
    # Check a staged copy before touching the live bar or cloning layouts.
    with tempfile.TemporaryDirectory(prefix='shepherd-tabs-check-') as temporary:
        staged = Path(temporary) / mod
        shutil.copytree(folder, staged)
        (staged / 'main.js').write_text(patched)
        run(['check', str(staged)])
    return name, layout, mod, original, patched, installed


def integrate(home, plan):
    name, layout, mod, original, patched, installed = plan
    path = home / 'layouts' / (name + '.json')
    if json.loads(path.read_text()) != layout or (home / 'mods' / mod / 'main.js').read_text() != original:
        raise ValueError('Layout or tab bar changed during installation; run the installer again.')
    if 'layout:' + name in installed:
        run(['do', 'layout.clone', json.dumps({'name': name, 'as': 'Tabs'})])
        name = 'Tabs'
        path = home / 'layouts/Tabs.json'
        layout = json.loads(path.read_text())
    else:
        layout = copy.deepcopy(layout)
    if mod in installed:
        clone = unused(home / 'mods', mod + '-plus')
        run(['do', 'mod.clone', json.dumps({'name': mod, 'as': clone})])
        mod = clone
        # Native cloning rewrites the clone's asset paths.
        patched = patch_tabs((home / 'mods' / mod / 'main.js').read_text())
    replace(home / 'mods' / mod / 'main.js', patched)
    find(layout, 'tabs')['content']['mod'] = mod
    body = find(layout, 'panel-body')
    if find(layout, 'shepherd') is None:
        body['children'].append({'id': 'shepherd', 'type': 'block', 'content': {'mod': 'shepherd'}})
    replace(path, json.dumps(layout, indent=2) + '\n')
    run(['check', str(home / 'mods' / mod)])
    print('Integrated Shepherd into layout ' + name + ' using tab bar ' + mod + '.')

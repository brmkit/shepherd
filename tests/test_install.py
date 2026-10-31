"""Installer regression checks against an isolated, synthetic Herdr home."""
import contextlib
import io
import json
import os
from pathlib import Path
import runpy
import tempfile
import unittest
from unittest.mock import patch


INSTALLER = Path(__file__).parents[1] / 'install.py'


class InstallerTests(unittest.TestCase):
    def test_first_install_integrates_and_selects_shepherd(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            (home / 'layouts').mkdir()
            main = {'id': 'root', 'children': [
                {'id': 'tabs', 'content': {'mod': 'tabs'}},
                {'id': 'panel', 'hidden': True, 'size': {'px': 368}, 'children': [
                    {'id': 'panel-body', 'active': 'files', 'children': [
                        {'id': 'files'}, {'id': 'changes'}]}]}]}
            def execute(command, **_kwargs):
                if 'layout.clone' in command:
                    (home / 'layouts/shepherd.json').write_text(json.dumps(main))
            with patch.dict(os.environ, {'HERDR_APP_HOME': folder}), \
                 patch('sys.argv', ['install.py']), patch('subprocess.run', side_effect=execute) as run, \
                 contextlib.redirect_stdout(io.StringIO()):
                runpy.run_path(str(INSTALLER), run_name='__main__')
            layout = json.loads((home / 'layouts/shepherd.json').read_text())
            tabs, panel = layout['children']
            self.assertEqual(tabs['content']['mod'], 'shepherd-tabs')
            self.assertFalse(panel['hidden'])
            self.assertEqual(panel['size']['px'], 390)
            body = panel['children'][0]
            self.assertEqual(body['active'], 'shepherd')
            self.assertEqual([child['id'] for child in body['children']], ['files', 'changes', 'shepherd'])
            self.assertTrue(any('layout.switch' in call.args[0] for call in run.call_args_list))
            self.assertEqual((home / 'mods/shepherd/windows-bootstrap.ps1').read_bytes(),
                             (INSTALLER.parent / 'mods/shepherd/windows-bootstrap.ps1').read_bytes())

    def test_update_preserves_panel_preferences_keys_and_current_layout(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            (home / 'layouts').mkdir()
            layout = {'id': 'root', 'children': [
                {'id': 'tabs', 'content': {'mod': 'shepherd-tabs'}},
                {'id': 'panel', 'hidden': True, 'size': {'px': 560}, 'children': [
                    {'id': 'panel-body', 'active': 'changes', 'children': [
                        {'id': 'files'}, {'id': 'changes'},
                        {'id': 'shepherd', 'content': {'mod': 'shepherd'}}]}]}]}
            path = home / 'layouts/shepherd.json'
            path.write_text(json.dumps(layout))
            keys = {'global': {'ctrl-shift-s': 'my-existing-command'}}
            (home / 'keys.json').write_text(json.dumps(keys))
            with patch.dict(os.environ, {'HERDR_APP_HOME': folder}), \
                 patch('sys.argv', ['install.py', '--update']), \
                 patch('subprocess.run') as run, contextlib.redirect_stdout(io.StringIO()):
                runpy.run_path(str(INSTALLER), run_name='__main__')
            self.assertEqual(json.loads(path.read_text()), layout)
            self.assertEqual(json.loads((home / 'keys.json').read_text())['global']['ctrl-shift-s'], 'my-existing-command')
            commands = [call.args[0] for call in run.call_args_list]
            self.assertFalse(any('layout.switch' in cmd for cmd in commands))
            self.assertFalse(any('layout.clone' in cmd for cmd in commands))
            self.assertTrue((home / 'mods/shepherd/bridge.py').exists())
            self.assertTrue((home / 'mods/shepherd/windows-bootstrap.ps1').exists())
            self.assertTrue((home / 'mods/shepherd/windows-install-core.ps1').exists())
            self.assertTrue((home / 'mods/shepherd/server.svg').exists())
            self.assertFalse((home / 'mods/shepherd/gpui-kit.d.ts').exists())
            self.assertFalse((home / 'mods/shepherd/jsconfig.json').exists())

    def test_existing_layout_conflict_is_rejected_before_copying(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            (home / 'layouts').mkdir()
            (home / 'layouts/shepherd.json').write_text('{}')
            with patch.dict(os.environ, {'HERDR_APP_HOME': folder}), \
                 patch('sys.argv', ['install.py']), patch('subprocess.run') as run, \
                 contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                runpy.run_path(str(INSTALLER), run_name='__main__')
            run.assert_not_called()
            self.assertFalse((home / 'mods').exists())


if __name__ == '__main__':
    unittest.main()

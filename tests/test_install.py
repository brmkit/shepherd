"""Exercise additive integration against synthetic personal and shipped layouts."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1]))
from integration import prepare, integrate, patch_tabs, find


BAR = '''const enabled = [...(shared("workspace.panels") ?? ["files", "changes"]), "automations"];
      .when(enabled.includes("changes"), el => el.child(panelButton("changes", "Changes", "mods/changes/changes.svg")))
      .child(panelButton("automations", "Automations", "mods/automations/automations.svg"))
'''


class IntegrationTests(unittest.TestCase):
    def fixture(self, home, shipped=False):
        (home / 'layouts').mkdir()
        (home / 'mods/tabs').mkdir(parents=True)
        (home / 'mods/tabs/main.js').write_text(BAR)
        (home / 'installed.json').write_text(json.dumps({'tabs': {}, 'layout:personal': {}} if shipped else {}))
        layout = {'id': 'root', 'children': [
            {'id': 'tabs', 'content': {'mod': 'tabs', 'props': {'custom': True}}},
            {'id': 'panel', 'hidden': True, 'size': {'px': 512}, 'children': [
                {'id': 'panel-body', 'type': 'stack', 'active': 'automations', 'children': [
                    {'id': 'automations', 'content': {'mod': 'automations'}}]}]}]}
        (home / 'layouts/personal.json').write_text(json.dumps(layout))
        return layout

    def test_patch_preserves_other_panels_and_is_idempotent(self):
        result = patch_tabs(BAR)
        self.assertIn('"automations"]', result)
        self.assertIn('panelButton("automations"', result)
        self.assertEqual(result.count('panelButton("shepherd"'), 1)
        self.assertEqual(patch_tabs(result), result)

    def test_unknown_bar_is_rejected(self):
        with self.assertRaises(ValueError):
            patch_tabs('const enabled = [];\nrenderSomethingElse();')

    def test_personal_layout_and_bar_are_reused(self):
        with tempfile.TemporaryDirectory() as folder, patch('integration.run') as run:
            home = Path(folder)
            before = self.fixture(home)
            integrate(home, prepare(home, 'personal'))
            after = json.loads((home / 'layouts/personal.json').read_text())
            self.assertEqual(find(after, 'tabs'), find(before, 'tabs'))
            self.assertEqual(find(after, 'panel')['size'], {'px': 512})
            self.assertTrue(find(after, 'panel')['hidden'])
            self.assertEqual(find(after, 'panel-body')['active'], 'automations')
            self.assertIsNotNone(find(after, 'automations'))
            self.assertIsNotNone(find(after, 'shepherd'))
            self.assertFalse(any(c.args[0][0] == 'do' for c in run.call_args_list))
            backups = list(home.rglob('*.before-shepherd-*'))
            self.assertEqual(len(backups), 2)
            integrate(home, prepare(home, 'personal'))
            self.assertEqual(len(list(home.rglob('*.before-shepherd-*'))), 2)

    def test_shipped_bar_is_cloned_in_existing_personal_layout(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            self.fixture(home)
            (home / 'installed.json').write_text(json.dumps({'tabs': {}}))
            def command(args, **kwargs):
                self.assertNotEqual(args[:2], ['do', 'layout.clone'])
                if args[:2] == ['do', 'mod.clone']:
                    payload = json.loads(args[2])
                    dest = home / 'mods' / payload['as']
                    dest.mkdir()
                    (dest / 'main.js').write_text(BAR)
            with patch('integration.run', side_effect=command):
                integrate(home, prepare(home, 'personal'))
            self.assertEqual((home / 'mods/tabs/main.js').read_text(), BAR)
            self.assertFalse((home / 'layouts/personal-plus.json').exists())
            after = json.loads((home / 'layouts/personal.json').read_text())
            self.assertEqual(find(after, 'tabs')['content']['mod'], 'tabs-plus')
            self.assertIn('panelButton("shepherd"', (home / 'mods/tabs-plus/main.js').read_text())

    def test_existing_tabs_layout_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as folder, patch('integration.run') as run:
            home = Path(folder)
            before = self.fixture(home, shipped=True)
            (home / 'layouts/Tabs.json').write_text('{"id":"personal"}')
            with self.assertRaisesRegex(ValueError, 'Tabs already exists'):
                prepare(home, 'personal')
            run.assert_not_called()
            self.assertEqual(json.loads((home / 'layouts/personal.json').read_text()), before)
            self.assertEqual((home / 'mods/tabs/main.js').read_text(), BAR)

    def test_shipped_layout_is_cloned_as_tabs(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            before = self.fixture(home)
            (home / 'installed.json').write_text(json.dumps({'layout:personal': {}}))
            def command(args, **kwargs):
                if args[:2] == ['do', 'layout.clone']:
                    self.assertEqual(json.loads(args[2]), {'name': 'personal', 'as': 'Tabs'})
                    (home / 'layouts/Tabs.json').write_text(json.dumps(before))
            with patch('integration.run', side_effect=command):
                integrate(home, prepare(home, 'personal'))
            self.assertEqual(json.loads((home / 'layouts/personal.json').read_text()), before)
            self.assertIsNotNone(find(json.loads((home / 'layouts/Tabs.json').read_text()), 'shepherd'))

    def test_concurrent_edit_is_preserved(self):
        with tempfile.TemporaryDirectory() as folder, patch('integration.run'):
            home = Path(folder)
            self.fixture(home)
            plan = prepare(home, 'personal')
            path = home / 'mods/tabs/main.js'
            path.write_text(BAR + '// Personal edit\n')
            with self.assertRaises(ValueError):
                integrate(home, plan)
            self.assertTrue(path.read_text().endswith('// Personal edit\n'))


if __name__ == '__main__':
    unittest.main()

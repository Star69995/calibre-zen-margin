#!/usr/bin/env python
# License: GPL v3 Copyright: 2026, Nadeem Siddique

"""
Windows "Open with" (calibre.utils.winreg.default_programs): what the fork
registers, that the installer's choice is honoured, and that Settings is
opened once. Nothing here writes to the registry.
"""

import os
import re
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

from calibre_zen.tests.test_identity import repo_root


@unittest.skipUnless(sys.platform == 'win32', 'Default Programs is the Windows registry')
class TestOpenWith(unittest.TestCase):
    def test_entries_are_the_forks(self):
        """
        What Windows lists under "Open with" is the fork's launcher, never
        calibre's frozen executable (which runs calibre's own Python), under
        names that are not calibre's or Calibre Zen's.
        """
        from calibre.constants import __appname__, zen_display_name
        from calibre.linux import PATH_PREFIX
        from calibre.utils.winreg.default_programs import cap_path, default_programs, launcher_name, progid_name

        programs = default_programs()
        self.assertEqual(
            {launcher_name(p) for p in programs},
            {f'{__appname__}.exe', f'{PATH_PREFIX}ebook-viewer.exe', f'{PATH_PREFIX}ebook-edit.exe'},
        )
        ps1 = os.path.join(repo_root(), 'packaging', 'windows', 'package.ps1')
        if os.path.exists(ps1):
            with open(ps1, encoding='utf-8') as f:
                self.assertEqual(set(re.findall(r"Target = '([^']+)'", f.read())), set(programs), 'package.ps1 builds a launcher for each')
        for program, data in programs.items():
            with self.subTest(program=program):
                self.assertTrue(data['name'].startswith(zen_display_name))
                self.assertTrue(cap_path(data).startswith(rf'Software\{__appname__}' + '\\'))
                self.assertTrue(progid_name(data['assoc_name'], 'epub').startswith(f'{__appname__}.'))

    def installer_values(self, values):
        "`installer_declined()` with the .msi's `Installer` key holding `values` (None: no such key)."
        from calibre.utils.winreg import default_programs as dp

        if values is None:
            return mock.patch.object(dp, 'Key', side_effect=FileNotFoundError)
        key = mock.MagicMock()
        key.__enter__.return_value = SimpleNamespace(get=values.get)
        return mock.patch.object(dp, 'Key', return_value=key)

    def test_installer_without_the_choice_declines(self):
        from calibre.utils.winreg.default_programs import installer_declined

        with self.installer_values({'InstallPath': 'somewhere'}):
            self.assertTrue(installer_declined())

    def test_installer_with_the_choice_does_not(self):
        from calibre.utils.winreg.default_programs import installer_declined

        with self.installer_values({'InstallPath': 'somewhere', 'OpenWith': 1}):
            self.assertFalse(installer_declined())

    def test_a_zip_is_not_an_installer_that_declined(self):
        from calibre.utils.winreg.default_programs import installer_declined

        with self.installer_values(None):
            self.assertFalse(installer_declined())

    def test_declining_blocks_registering(self):
        from calibre.utils.winreg import default_programs as dp

        with (
            mock.patch.object(dp, 'isfrozen', True),
            mock.patch.object(dp, 'isportable', False),
            mock.patch.dict(os.environ, clear=False),
            mock.patch.object(dp, 'installer_declined', return_value=True),
        ):
            os.environ.pop('CALIBRE_NO_DEFAULT_PROGRAMS', None)
            self.assertRaises(dp.NotAllowed, dp.check_allowed)

    def test_settings_open_once_on_the_viewers_page(self):
        from calibre.utils.winreg import default_programs as dp

        me = SimpleNamespace(prefs={})
        viewer = {'ebook-viewer.exe': {'name': 'Calibre Zen Margin Viewer'}}
        with (
            mock.patch.object(dp, 'installed_programs', return_value=viewer),
            mock.patch.object(dp, 'open_default_apps_page') as opened,
        ):
            dp.Register.offer_default_apps_page(me)
            dp.Register.offer_default_apps_page(me)
        opened.assert_called_once_with('Calibre Zen Margin Viewer')

    def test_the_settings_link_names_the_program(self):
        from calibre.utils.winreg import default_programs as dp

        with mock.patch.object(dp.os, 'startfile') as startfile:
            dp.open_default_apps_page('Calibre Zen Margin Viewer')
        startfile.assert_called_once_with('ms-settings:defaultapps?registeredAppUser=Calibre%20Zen%20Margin%20Viewer')

#!/usr/bin/env python
# License: GPLv3 Copyright: 2015, Kovid Goyal <kovid at kovidgoyal.net>

import os
import sys
import time
import traceback
from threading import Thread
from typing import TYPE_CHECKING, Self, cast
from urllib.parse import quote

from calibre import guess_type, prints
from calibre.constants import DEBUG, __appname__, __version__, isfrozen, isportable, zen_display_name
from calibre.utils.localization import _
from calibre.utils.lock import singleinstance
from calibre.utils.winreg.lib import HKEY_CURRENT_USER, HKEY_LOCAL_MACHINE, Key
from calibre_extensions import winutil

if TYPE_CHECKING:
    from collections.abc import Iterable
    from ctypes.wintypes import HKEY

    from calibre.utils.config import JSONConfig
    from calibre.utils.winreg.lib import RegistryValue

# See https://msdn.microsoft.com/en-us/library/windows/desktop/cc144154(v=vs.85).aspx


# calibre-zen: the keys are calibre's frozen executables, which `extensions()`
# is keyed on, but what gets registered is the fork's launcher for each
# (`launcher_name()`): the frozen executable alone runs calibre's own bundled
# Python, not this fork's. The registry names derive from the fork's identity,
# not calibre's, so the fork installs beside calibre and Calibre Zen without
# either overwriting the other's entries or unregistering them.
def default_programs() -> dict[str, dict[str, str]]:
    return {
        'calibre.exe': {
            'icon_id': 'main_icon',
            'description': _('The main calibre program, used to manage your collection of e-books'),
            'capability_name': 'calibre64bit',
            'name': zen_display_name,
            'assoc_name': f'{__appname__}.Main',
        },
        'ebook-edit.exe': {
            'icon_id': 'editor_icon',
            'description': _('The calibre E-book editor. It can be used to edit common e-book formats.'),
            'capability_name': 'Editor64bit',
            'name': f'{zen_display_name} Editor',
            'assoc_name': f'{__appname__}.Editor',
        },
        'ebook-viewer.exe': {
            'icon_id': 'viewer_icon',
            'description': _('The calibre E-book viewer. It can view most known e-book formats.'),
            'capability_name': 'Viewer64bit',
            'name': f'{zen_display_name} Viewer',
            'assoc_name': f'{__appname__}.Viewer',
        },
    }


def launcher_name(program: str) -> str:
    "The fork's launcher for one of calibre's frozen executables, as packaging/windows/package.ps1 names them."
    from calibre.linux import path_name

    # The GUI's launcher is named for the app itself; the viewer's and the editor's take the PATH prefix.
    return path_name(f'{__appname__}.exe' if program == 'calibre.exe' else program)


def installed_programs() -> dict[str, dict[str, str]]:
    "`default_programs()` limited to those whose launcher is installed, with the launcher's full path as `exe`."
    base = os.path.dirname(sys.executable)
    ans = {}
    for program, data in default_programs().items():
        exe = os.path.join(base, launcher_name(program))
        if os.path.exists(exe):
            ans[program] = {**data, 'exe': exe}
    return ans


def extensions(basename: str) -> set[str] | None:
    if basename == 'calibre.exe':
        from calibre.ebooks import BOOK_EXTENSIONS

        # We remove rar and zip as they interfere with 7-zip associations
        # https://www.mobileread.com/forums/showthread.php?t=256459
        return set(BOOK_EXTENSIONS) - {'rar', 'zip'}
    if basename == 'ebook-viewer.exe':
        from calibre.customize.ui import all_input_formats

        return set(all_input_formats())
    if basename == 'ebook-edit.exe':
        from calibre.ebooks.oeb.polish.import_book import IMPORTABLE
        from calibre.ebooks.oeb.polish.main import SUPPORTED

        return SUPPORTED | IMPORTABLE


class NotAllowed(ValueError):
    pass


def check_allowed() -> None:
    if not isfrozen:
        raise NotAllowed('Not allowed to create associations for non-frozen installs')
    if isportable:
        raise NotAllowed('Not allowed to create associations for portable installs')
    if sys.getwindowsversion()[:2] < (6, 2):
        raise NotAllowed('Not allowed to create associations for windows versions older than Windows 8')
    if 'CALIBRE_NO_DEFAULT_PROGRAMS' in os.environ:
        raise NotAllowed('Disabled by the CALIBRE_NO_DEFAULT_PROGRAMS environment variable')
    if installer_declined():
        raise NotAllowed('The installer was run without "Open e-book files with" ticked')


def installer_declined() -> bool:
    """
    Whether the .msi was installed with its "Open e-book files with" feature
    unticked. Ticking it writes `OpenWith` beside `InstallPath`; a zip has
    neither, and registers as it always has.
    """
    try:
        with Key(open_at=rf'Software\{__appname__}\Installer', root=HKEY_LOCAL_MACHINE) as key:
            return key.get('InstallPath') is not None and key.get('OpenWith') is None
    except OSError:
        return False


def create_prog_id(ext: str, prog_id: str, ext_map: dict[str, str], exe: str) -> None:
    with Key(rf'Software\Classes\{prog_id}') as key:
        type_name = _('%s Document') % ext.upper()
        key.set(value=type_name)
        key.set('FriendlyTypeName', type_name)
        key.set('PerceivedType', 'Document')
        key.set(sub_key='DefaultIcon', value=exe + ',0')
        key.set_default_value(r'shell\open\command', f'"{exe}" "%1"')
        # contrary to the msdn docs, this key prevents calibre programs
        # from appearing in the initial open with list, see
        # https://www.mobileread.com/forums/showthread.php?t=313668
        # key.set('AllowSilentDefaultTakeOver')

    with Key(rf'Software\Classes\.{ext}\OpenWithProgIDs') as key:
        key.set(prog_id)


def progid_name(assoc_name: str, ext: str) -> str:
    return f'{assoc_name}.AssocFile.{ext.upper()}'


def cap_path(data: dict[str, str]) -> str:
    return rf'Software\{__appname__}\{data["capability_name"]}\Capabilities'


def is_registered() -> bool:
    "Whether each installed program still has its Default Programs entry, which a reinstall or an uninstall removes."
    try:
        with Key(open_at=r'Software\RegisteredApplications') as key:
            return all(key.get(data['name']) == cap_path(data) for data in installed_programs().values())
    except OSError:
        return False


def register() -> None:
    for program, data in installed_programs().items():
        exe = data['exe']
        capabilities_path = cap_path(data)
        ext_map = {ext.lower(): guess_type('file.' + ext.lower())[0] for ext in extensions(program)}  # ty: ignore[not-iterable]
        ext_map = {ext: mt for ext, mt in ext_map.items() if mt}
        prog_id_map = {ext: progid_name(data['assoc_name'], ext) for ext in ext_map}

        with Key(capabilities_path) as key:
            for k, v in {'ApplicationDescription': 'description', 'ApplicationName': 'name'}.items():
                key.set(k, data[v])
            key.set('ApplicationIcon', f'{exe},0')
            key.set_default_value(r'shell\open\command', f'"{exe}" "%1"')

            with Key('FileAssociations', root=key) as fak, Key('MimeAssociations', root=key) as mak:
                # previous_associations = set(fak.values())
                for ext, prog_id in prog_id_map.items():
                    mt = ext_map[ext]
                    fak.set('.' + ext, prog_id)
                    mak.set(mt, prog_id)
        for ext, prog_id in prog_id_map.items():
            create_prog_id(ext, prog_id, ext_map, exe)

        with Key(r'Software\RegisteredApplications') as key:
            key.set(data['name'], capabilities_path)

    winutil.notify_associations_changed()


def open_default_apps_page(name: str) -> None:
    "Windows Settings, on the page that lists what the program registered as `name` can open."
    os.startfile('ms-settings:defaultapps?registeredAppUser=' + quote(name))


def unregister() -> None:
    for program, data in default_programs().items():
        capabilities_path = cap_path(data).rpartition('\\')[0]
        ext_map = {ext.lower(): guess_type('file.' + ext.lower())[0] for ext in extensions(program)}  # ty: ignore[not-iterable]
        ext_map = {ext: mt for ext, mt in ext_map.items() if mt}
        prog_id_map = {ext: progid_name(data['assoc_name'], ext) for ext in ext_map}
        with Key(r'Software\RegisteredApplications') as key:
            key.delete_value(data['name'])
        parent, sk = capabilities_path.rpartition('\\')[0::2]
        with Key(parent) as key:
            key.delete_tree(sk)
        for ext, prog_id in prog_id_map.items():
            with Key(rf'Software\Classes\.{ext}\OpenWithProgIDs') as key:
                key.delete_value(prog_id)
            with Key(r'Software\Classes') as key:
                key.delete_tree(prog_id)


class Register(Thread):
    daemon = True

    def __init__(self, prefs: JSONConfig) -> None:
        Thread.__init__(self, name='RegisterDP')
        self.prefs = prefs
        self.start()

    def run(self) -> None:
        try:
            self.do_register()
        except Exception:
            traceback.print_exc()

    def do_register(self) -> None:
        try:
            check_allowed()
        except NotAllowed:
            return
        if singleinstance('register_default_programs'):
            # calibre-zen: the version alone is not enough. Uninstalling removes the
            # entries but leaves the settings, so a reinstall of the same version
            # would never register again.
            if self.prefs.get('windows_register_default_programs', None) != __version__ or not is_registered():
                self.prefs['windows_register_default_programs'] = __version__
                if DEBUG:
                    st = time.monotonic()
                    prints('Registering with default programs...')
                register()
                if DEBUG:
                    prints(f'Registered with default programs in {time.monotonic() - st:.1f} seconds')
                self.offer_default_apps_page()

    def offer_default_apps_page(self) -> None:
        # calibre-zen: Windows will not let a program make itself the default
        # for a file type, so the first time it is registered the person is taken
        # to the page where they can. Once only: after that it is their call.
        if self.prefs.get('windows_default_apps_shown', False):
            return
        self.prefs['windows_default_apps_shown'] = True
        viewer = installed_programs().get('ebook-viewer.exe')
        if viewer is not None:
            open_default_apps_page(viewer['name'])

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        # Give the thread some time to finish in case the user quit the
        # application very quickly
        self.join(4.0)


def get_prog_id_map(base: HKEY, key_path: str) -> tuple[RegistryValue, dict[str, RegistryValue]]:
    desc, ans = None, {}
    try:
        k = Key(open_at=key_path, root=base)
    except OSError as err:
        if err.winerror == winutil.ERROR_FILE_NOT_FOUND:
            return desc, ans
        raise
    with k:
        desc = k.get_mui_string('ApplicationDescription')
        if desc is None:
            return desc, ans
        for ext, prog_id in k.values(sub_key='FileAssociations', get_data=True):
            ans[ext[1:].lower()] = prog_id
    return desc, ans


def get_open_data(base: HKEY, prog_id: object) -> tuple[str | None, str | None, str | None]:
    try:
        k = Key(open_at=rf'Software\Classes\{prog_id}', root=base)
    except OSError as err:
        if err.winerror == winutil.ERROR_FILE_NOT_FOUND:
            return None, None, None
        raise
    with k:
        cmd = cast('str | None', k.get(sub_key=r'shell\open\command'))
        if cmd:
            parts = cmd.split()
            if parts[-1] == '/dde' and '%1' not in cmd:
                cmd = ' '.join(parts[:-1]) + ' "%1"'
        return (
            cmd,
            cast('str | None', k.get(sub_key='DefaultIcon')),
            cast('str | None', k.get_mui_string('FriendlyTypeName') or k.get()),
        )


def split_commandline(commandline: str) -> list[str]:
    # CommandLineToArgvW returns path to executable if called with empty string.
    if not commandline.strip():
        return []
    return list(winutil.parse_cmdline(commandline))


def friendly_app_name(prog_id: str | None = None, exe: str | None = None) -> str | None:
    try:
        return winutil.friendly_name(prog_id, exe)
    except Exception:
        traceback.print_exc()


def find_programs(extensions: Iterable[str]) -> list[dict[str, RegistryValue]]:
    extensions = frozenset(extensions)
    ans: list[dict[str, RegistryValue]] = []
    seen_prog_ids, seen_cmdlines = set(), set()

    # Search for programs registered using Default Programs that claim they are
    # capable of handling the specified extensions.
    for base in (HKEY_CURRENT_USER, HKEY_LOCAL_MACHINE):
        try:
            k = Key(open_at=r'Software\RegisteredApplications', root=base)
        except OSError as err:
            if err.winerror == winutil.ERROR_FILE_NOT_FOUND:
                continue
            raise
        with k:
            for name, key_path in k.values(get_data=True):
                try:
                    app_desc, prog_id_map = get_prog_id_map(base, cast('str', key_path))
                except Exception:
                    traceback.print_exc()
                    continue
                for ext in extensions:
                    prog_id = prog_id_map.get(ext)
                    if prog_id is not None and prog_id not in seen_prog_ids:
                        seen_prog_ids.add(prog_id)
                        cmdline, icon_resource, friendly_name = get_open_data(base, prog_id)
                        if cmdline and cmdline not in seen_cmdlines:
                            seen_cmdlines.add(cmdline)
                            ans.append({'name': app_desc, 'cmdline': cmdline, 'icon_resource': icon_resource})

    # Now look for programs that only register with Windows Explorer instead of
    # Default Programs (for example, FoxIt PDF reader)
    for ext in extensions:
        try:
            k = Key(
                open_at=rf'Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts\.{ext}\OpenWithProgIDs',
                root=HKEY_CURRENT_USER,
            )
        except OSError as err:
            if err.winerror == winutil.ERROR_FILE_NOT_FOUND:
                continue
            raise
        for prog_id in k.values():
            if prog_id and prog_id not in seen_prog_ids:
                seen_prog_ids.add(prog_id)
                cmdline, icon_resource, friendly_name = get_open_data(HKEY_LOCAL_MACHINE, prog_id)
                if cmdline and cmdline not in seen_cmdlines:
                    seen_cmdlines.add(cmdline)
                    exe_name = None
                    exe = split_commandline(cmdline)
                    if exe:
                        exe_name = friendly_app_name(prog_id) or os.path.splitext(os.path.basename(exe[0]))[0]
                    name = exe_name or friendly_name
                    if name:
                        ans.append({'name': name, 'cmdline': cmdline, 'icon_resource': icon_resource})
    return ans


if __name__ == '__main__':
    from pprint import pprint

    pprint(find_programs('docx'.split()))

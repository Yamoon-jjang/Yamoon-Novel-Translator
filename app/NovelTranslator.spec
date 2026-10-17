# PyInstaller spec for the portable Windows build.
# Default release format is ONEDIR for Qt/plugin reliability and editable config/profiles.
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

hiddenimports = collect_submodules('charset_normalizer') + collect_submodules('pypdf')
datas = [
    ('config', 'config'),
    ('profiles', 'profiles'),
    ('docs', 'docs'),
    ('novel_translator/build_info.json', 'novel_translator'),
]
datas += collect_data_files('certifi')

a = Analysis(
    ['run.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='NovelTranslator',
    version='version_info.txt',
    icon='assets/app.ico',   # same drawing as the About dialog icon (ui/about_dialog.app_pixmap)
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='NovelTranslator',
)

# User-facing files next to NovelTranslator.exe: the manual (docs/*.html) and README.txt.
# (No Korean literals here: the file names come from the docs folder itself.)
import os, shutil
_dist_dir = os.path.join(DISTPATH, 'NovelTranslator')
for _name in os.listdir('docs'):
    if _name.lower().endswith('.html'):
        shutil.copy2(os.path.join('docs', _name), os.path.join(_dist_dir, _name))
shutil.copy2('README.txt', os.path.join(_dist_dir, 'README.txt'))
for _name in ('LICENSE.txt', 'THIRD_PARTY_NOTICES.txt'):
    shutil.copy2(_name, os.path.join(_dist_dir, _name))

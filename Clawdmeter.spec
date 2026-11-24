# PyInstaller spec for Clawdmeter.
# Build with:  pyinstaller Clawdmeter.spec
# Output:      dist/Clawdmeter.exe (single-file, no console)

# -*- mode: python ; coding: utf-8 -*-

import os
import sys

block_cipher = None

# macOS architecture slice. build-macos.sh sets CLAWD_TARGET_ARCH=universal2 when
# the interpreter it is building with carries both slices, so one .app runs
# natively on Apple Silicon AND Intel — Apple's recommended way to ship, and one
# download means a user never has to know their own CPU. Unset (the default) =
# build for whatever this machine is, which keeps Windows/Linux untouched.
_TARGET_ARCH = os.environ.get("CLAWD_TARGET_ARCH") or None

# Platform flags for the size-pruning and packaging logic below. Everything is
# structured so a future macOS build is an additive branch, not a rewrite.
_IS_WIN = sys.platform == 'win32'
_IS_MAC = sys.platform == 'darwin'

# --- Build identity ------------------------------------------------------------
# Stamp the git commit (and whether this is exactly the release tag, and whether
# the tree had local changes) into src/_build_info.py, so the About box -- and
# the Windows ProductVersion below -- can't pass a dev build off as a release.
# See src/build_info.py. Never fails the build: no git just means "unknown".
import re as _re
sys.path.insert(0, os.path.abspath('src'))
import build_info as _build_info_mod

with open('src/app_settings.py', encoding='utf-8') as _f:
    _m0 = _re.search(r'APP_VERSION\s*=\s*["\']([0-9]+(?:\.[0-9]+)*)["\']', _f.read())
_BUILD = _build_info_mod.write_stamp(_m0.group(1) if _m0 else '0.0.0')
_VERSION_LABEL = _build_info_mod.version_label(
    _m0.group(1) if _m0 else '0.0.0', _BUILD)
print(f'Clawdmeter build identity: {_VERSION_LABEL}')

a = Analysis(
    ['src/main.py'],
    pathex=['src'],
    binaries=[],
    datas=[
        ('assets/sprites', 'assets/sprites'),
        ('assets/icon.png', 'assets'),
        ('assets/icon.ico', 'assets'),
        # Font Awesome 6 Free (Solid) — tab/nav icons. Loaded via QFontDatabase
        # at startup; SIL OFL 1.1, license bundled alongside.
        ('assets/fonts/fa-solid-900.ttf', 'assets/fonts'),
        ('assets/fonts/LICENSE.txt', 'assets/fonts'),
        # USD price map for Claude models. Read at runtime by pricing.load_price_map
        # via _MEIPASS/pricing/price_map.json (see src/pricing/__init__.py).
        ('src/pricing/price_map.json', 'pricing'),
    ],
    # macOS: NSColorSampler is reached via a lazy `from AppKit import ...`, so
    # PyInstaller's static analysis misses it -- force-collect AppKit (its pyobjc
    # hook pulls in Foundation + pyobjc-core). No-op on Windows/Linux.
    # ServiceManagement is likewise reached lazily (SMAppService, for the
    # login item), so name it explicitly too.
    # Security belongs here for the same reason: macos_keychain does
    # `from Security import SecItemCopyMatching` inside a try/except, which
    # PyInstaller classes as "delayed, optional" exactly like the other two.
    # It is currently collected anyway by pyobjc's hooks, but nothing pins
    # that — and losing it fails invisibly: the import raises, the Keychain
    # read falls back to the `security` CLI, a token still comes back, and
    # the only symptom is the authorisation dialog naming *security* again
    # and an "Always Allow" grant attaching to that shared binary instead
    # of to us — the two things this was changed to fix.
    # _build_info is imported by name at runtime (build_info.current), which
    # static analysis can't see.
    hiddenimports=['_build_info'] + (['AppKit', 'Security', 'ServiceManagement']
                                     if _IS_MAC else []),
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        # QtNetwork is bundled (NOT excluded): single_instance.py uses
        # QLocalServer/QLocalSocket for the single-instance guard.
        'PySide6.QtQml',
        'PySide6.QtQuick',
        'PySide6.QtWebEngineCore',
        'PySide6.QtMultimedia',
        'PySide6.QtPdf',
        'PySide6.Qt3DCore',
        'PySide6.QtCharts',
        'PySide6.QtDataVisualization',
        'PySide6.QtOpenGL',
        'PySide6.QtSvg',
        'PySide6.QtPrintSupport',
        'PySide6.QtTest',
        'PySide6.QtSql',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

# --- Size pruning -----------------------------------------------------------
# The `excludes` above drop the PySide6 *Python* binding modules, but PyInstaller's
# PySide6 hook still collects the matching native Qt DLLs, every Qt plugin, and all
# translations. The app only uses QtCore/QtGui/QtWidgets/QtNetwork, so we strip the
# rest here. This takes the one-file exe from ~48 MB to ~27 MB. See the size notes
# in README/NOTICE if you change the imported Qt modules.

# 1. Native Qt DLLs the app never imports (QML/Quick/Pdf/OpenGL/Svg/VirtualKeyboard).
#    opengl32sw.dll is Qt's ~20 MB software-OpenGL fallback, unneeded for an opaque
#    Widgets UI. (If you reintroduce a WA_TranslucentBackground window, stop pruning
#    it — translucent compositing fails in the frozen build without this fallback.)
_DROP_QT_DLL = (
    'opengl32sw', 'Qt6Quick', 'Qt6Qml', 'Qt6Pdf', 'Qt6OpenGL',
    'Qt6Svg', 'Qt6VirtualKeyboard', 'Qt6QmlModels', 'Qt6QmlMeta',
    'Qt6QmlWorkerScript',
)
a.binaries = TOC([b for b in a.binaries
                  if not any(x.lower() in b[0].lower() for x in _DROP_QT_DLL)])

# 2. Accidental second OpenSSL build pulled from Git's mingw64\bin via PATH; it
#    duplicates Python's own libcrypto-3.dll/libssl-3.dll which _ssl/httpx use.
a.binaries = TOC([b for b in a.binaries
                  if 'mingw64' not in (b[1] or '').lower()
                  and b[0].split('\\')[-1].lower()
                      not in ('libcrypto-3-x64.dll', 'libssl-3-x64.dll')])

# 3. Image-format plugins the app never decodes. It loads PNG sprites/icons (PNG
#    support is built into Qt6Gui) and uses the .ico window icon, so keep only qico.
_DROP_IMG = (
    'qjpeg', 'qtiff', 'qgif', 'qwebp', 'qwbmp', 'qtga', 'qicns',
    'qsvg', 'qsvgicon', 'qpdf',
)
a.binaries = TOC([b for b in a.binaries
                  if not any(x in b[0].split('\\')[-1].lower() for x in _DROP_IMG)])

# 4. Extra Qt platform plugins; the Windows desktop build only needs qwindows.
_DROP_PLAT = ('qdirect2d.dll', 'qminimal.dll', 'qoffscreen.dll')
a.binaries = TOC([b for b in a.binaries
                  if b[0].split('\\')[-1].lower() not in _DROP_PLAT])

# 5. Qt's own UI translations (~6 MB of .qm). The app installs no QTranslator, so
#    these are never loaded; its UI strings are hardcoded English.
a.datas = [d for d in a.datas if 'translations' not in d[0].lower()]

# 6. Cross-platform prune (Linux/macOS). Filters 1-5 above key off '\'-separated
#    basenames or Windows plugin names, so they largely no-op off Windows — which
#    keeps the Windows build byte-for-byte identical. Re-apply the equivalent
#    prune for the current non-Windows OS here, keeping only the platform
#    plugin(s) it actually uses. macOS-ready: qcocoa is in the keep list, so a
#    future darwin build needs no change here.
if not _IS_WIN:
    def _base(p):
        return os.path.basename(p.replace('\\', '/'))

    def _plugin_stem(p):
        n = _base(p).lower()
        return (n[3:] if n.startswith('lib') else n).split('.')[0]

    # The Linux Wayland platform plugin is libqwayland.so (stem 'qwayland') in
    # the PySide6 6.11.1 wheel -- verified bundled on a real Mint 22 build. (An
    # earlier review guessed the file was 'libqwayland-generic.so'; that name
    # doesn't exist in this wheel, so 'qwayland' is the correct keep entry and
    # no extra one is needed. 'qwayland-egl' is also absent but harmless to list.)
    _KEEP_PLAT = (('qcocoa', 'qoffscreen') if _IS_MAC
                  else ('qxcb', 'qwayland', 'qwayland-egl', 'qoffscreen'))
    # Only PNG/.ico are used and PNG is built into Qt6Gui, so no image-format
    # plugin is needed off Windows (Linux/macOS take their window icon from the
    # .desktop entry / .app bundle, not a Qt .ico plugin).
    _DROP_IMG_X = ('qjpeg', 'qtiff', 'qgif', 'qwebp', 'qwbmp', 'qtga', 'qicns',
                   'qico', 'qsvg', 'qpdf')

    def _keep_binary(b):
        parts = b[0].replace('\\', '/').split('/')
        if 'platforms' in parts:
            return _plugin_stem(b[0]) in _KEEP_PLAT
        if 'imageformats' in parts:
            return _plugin_stem(b[0]) not in _DROP_IMG_X
        return True

    a.binaries = TOC([b for b in a.binaries if _keep_binary(b)])
# ---------------------------------------------------------------------------

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# --- Windows version resource ------------------------------------------------
# Give Explorer's Properties > Details tab (and Task Manager / the SmartScreen
# prompt) a real File/Product version, name, and copyright. The version is
# parsed from APP_VERSION in src/app_settings.py so it always matches the in-app
# About box -- bump it there and rebuild; nothing here needs touching.
import re
import sys

# Single source of truth for the build version: APP_VERSION in app_settings.
# Used by the Windows version resource AND the macOS bundle's CFBundleVersion,
# so parse it unconditionally (bump it in src/app_settings.py; nothing here).
with open('src/app_settings.py', encoding='utf-8') as _f:
    _m = re.search(r'APP_VERSION\s*=\s*["\']([0-9]+(?:\.[0-9]+)*)["\']', _f.read())
_ver_str = _m.group(1) if _m else '0.0.0'

# The version resource is a Windows-only concept (and its builder lives under
# PyInstaller.utils.win32, which only imports on Windows). Off Windows the Linux
# build carries no embedded version resource — the .desktop entry and release
# metadata cover that instead — so leave it None. (macOS carries its version in
# the .app Info.plist, built below.)
version_info = None
if sys.platform == 'win32':
    from PyInstaller.utils.win32.versioninfo import (
        VSVersionInfo, FixedFileInfo, StringFileInfo, StringTable,
        StringStruct, VarFileInfo, VarStruct,
    )

    _vtuple = tuple(([int(p) for p in _ver_str.split('.')] + [0, 0, 0, 0])[:4])

    version_info = VSVersionInfo(
        ffi=FixedFileInfo(filevers=_vtuple, prodvers=_vtuple),
        kids=[
            StringFileInfo([
                StringTable('040904B0', [
                    StringStruct('CompanyName', 'Nick Welter'),
                    StringStruct('FileDescription',
                                 'Clawdmeter — Claude Code usage dashboard'),
                    StringStruct('FileVersion', _ver_str),
                    StringStruct('InternalName', 'Clawdmeter'),
                    StringStruct('LegalCopyright',
                                 '© 2026 Nick Welter · MIT licensed · '
                                 'Clawd mascot © Anthropic PBC'),
                    StringStruct('OriginalFilename', 'Clawdmeter.exe'),
                    StringStruct('ProductName', 'Clawdmeter'),
                    # The label, not the bare number: "3.1.0+dev.1d4a1cc" for
                    # anything but the tagged release, so Explorer's Details
                    # tab tells the builds apart too. FileVersion and the
                    # fixed-info tuples stay numeric, as Windows requires.
                    StringStruct('ProductVersion', _VERSION_LABEL),
                    StringStruct('Comments',
                                 'Unofficial; not affiliated with Anthropic. '
                                 'github.com/weltern/Clawdmeter'),
                ]),
            ]),
            VarFileInfo([VarStruct('Translation', [0x0409, 0x04B0])]),
        ],
    )

# App/window icon per platform. macOS uses an .icns (drop assets/icon.icns in
# when a darwin build is added); Linux takes its icon from the .desktop entry.
if _IS_WIN:
    _ICON = 'assets/icon.ico'
elif _IS_MAC:
    _ICON = 'assets/icon.icns' if os.path.exists('assets/icon.icns') else None
else:
    _ICON = None

# --- Packaging --------------------------------------------------------------
# Windows/Linux ship a single self-contained executable (onefile): binaries,
# zipfiles, and datas are baked into the EXE. macOS instead builds a onedir
# bundle — EXE (bootstrap only, exclude_binaries=True) -> COLLECT (the onedir
# tree) -> BUNDLE (Clawdmeter.app). A onefile EXE wrapped in BUNDLE is deprecated
# and becomes an ERROR in PyInstaller v7, so the .app must be onedir. The
# non-macOS EXE below is unchanged from the Windows-only version, so Windows and
# Linux builds are byte-for-byte identical.
if _IS_MAC:
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,          # onedir: binaries go into COLLECT below
        name='Clawdmeter',
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=False,
        disable_windowed_traceback=False,
        target_arch=_TARGET_ARCH,
        codesign_identity=None,
        entitlements_file=None,
        icon=_ICON,
        version=version_info,           # None off Windows
    )

    coll = COLLECT(
        exe,
        a.binaries,
        a.zipfiles,
        a.datas,
        strip=False,
        upx=False,
        upx_exclude=[],
        name='Clawdmeter',
    )

    # --- macOS .app bundle --------------------------------------------------
    # Wrap the COLLECT'd onedir tree into a Clawdmeter.app so it launches like a
    # native menu-bar app. LSUIElement=1 makes it an "agent" — it lives in the
    # menu bar with NO Dock icon and NO app-switcher entry, which is what a tray
    # utility wants. The version comes from APP_VERSION (parsed above).
    app = BUNDLE(
        coll,
        name='Clawdmeter.app',
        icon=_ICON,
        bundle_identifier='com.clawdmeter.app',
        version=_ver_str,
        info_plist={
            'LSUIElement': True,                     # menu-bar agent, no Dock icon
            'CFBundleName': 'Clawdmeter',
            'CFBundleDisplayName': 'Clawdmeter',
            'CFBundleShortVersionString': _ver_str,
            'CFBundleVersion': _ver_str,
            'NSHighResolutionCapable': True,
            'NSRequiresAquaSystemAppearance': False,  # allow dark appearance for the dark theme
            'LSMinimumSystemVersion': '11.0',
            'NSHumanReadableCopyright': (
                '© 2026 Nick Welter · MIT licensed · '
                'Clawd mascot © Anthropic PBC'
            ),
        },
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.zipfiles,
        a.datas,
        [],
        name='Clawdmeter',
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        runtime_tmpdir=None,
        console=False,
        disable_windowed_traceback=False,
        target_arch=_TARGET_ARCH,
        codesign_identity=None,
        entitlements_file=None,
        icon=_ICON,
        version=version_info,
    )

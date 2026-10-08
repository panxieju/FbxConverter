# -*- mode: python -*-
"""PyInstaller build spec for FbxConverter.

One analysis produces two executables that share the bundled payload:

    FbxConverter.exe   windowed   the PyQt5 wizard
    fbxconv.exe        console    scan / convert / unity-import

Both run ``packaging/entry.py``, which picks a mode from its own executable
name -- that is what allows a single :class:`Analysis` to feed both.

Why the data files matter
-------------------------
``fbxconv/ue_scripts/*.py`` is **not** imported by this application. Unreal's
own bundled Python 3.11 executes those files, so they have to exist on disk as
readable ``.py`` sources. A frozen build must therefore ship them as *data*
(``datas=`` below), never as frozen modules. The same applies to the Unity
``.cs`` / ``.asmdef`` templates. :mod:`fbxconv.resources` resolves both
locations for source checkouts and frozen bundles alike.

Build
-----
::

    pyinstaller --clean --noconfirm FbxConverter.spec

Output goes to ``dist/FbxConverter/`` (onedir) or ``dist/*.exe`` (onefile).
Switch modes with the ``FBXCONV_BUILD_MODE`` environment variable::

    $env:FBXCONV_BUILD_MODE = "onefile"
    pyinstaller --clean --noconfirm FbxConverter.spec

onedir is the default deliberately: it starts instantly and keeps the ~120 MB
Qt payload out of a per-launch extraction, which matters because this tool
spawns Unreal and Unity repeatedly.
"""

import os
from pathlib import Path

# SPECPATH is injected by PyInstaller: the directory holding this spec.
PROJECT_ROOT = Path(SPECPATH).resolve()
SRC_DIR = PROJECT_ROOT / "src"
PACKAGE_DIR = SRC_DIR / "fbxconv"
ENTRY_SCRIPT = PROJECT_ROOT / "packaging" / "entry.py"
VERSION_FILE = PROJECT_ROOT / "packaging" / "version_info.txt"

APP_NAME = "FbxConverter"
CLI_NAME = "fbxconv"
BUILD_MODE = os.environ.get("FBXCONV_BUILD_MODE", "onedir").strip().lower()
if BUILD_MODE not in {"onedir", "onefile"}:
    raise SystemExit(
        f"FBXCONV_BUILD_MODE must be 'onedir' or 'onefile', got {BUILD_MODE!r}"
    )

for required in (ENTRY_SCRIPT, PACKAGE_DIR / "ue_scripts", PACKAGE_DIR / "unity" / "templates"):
    if not required.exists():
        raise SystemExit(f"missing required input: {required}")


# --------------------------------------------------------------------------- #
# Payload
# --------------------------------------------------------------------------- #


def collect_tree(source, destination):
    """List a directory as ``(file, destination-dir)`` pairs, skipping caches.

    PyInstaller's directory form would copy ``__pycache__`` too, which is both
    useless and misleading: those ``.pyc`` files belong to the *packaging*
    interpreter (3.13), whereas Unreal executes the ``.py`` sources with its
    own bundled Python 3.11.
    """
    collected = []
    for path in sorted(Path(source).rglob("*")):
        if not path.is_file():
            continue
        if "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
            continue
        relative = path.parent.relative_to(Path(source)).as_posix()
        target = destination if relative == "." else f"{destination}/{relative}"
        collected.append((str(path), target))
    if not collected:
        raise SystemExit(f"nothing to bundle from {source}")
    return collected


#: Destinations mirror the package layout so fbxconv.resources can find them
#: by relative path in both source checkouts and frozen bundles.
datas = [
    *collect_tree(PACKAGE_DIR / "ue_scripts", "fbxconv/ue_scripts"),
    *collect_tree(PACKAGE_DIR / "unity" / "templates", "fbxconv/unity/templates"),
]

#: PyQt5 and the UI pages are imported inside functions, so name them
#: explicitly rather than trusting static analysis to chase every branch.
hiddenimports = [
    "fbxconv.cli",
    "fbxconv.resources",
    "fbxconv.ui.app",
    "fbxconv.ui.main_window",
    "fbxconv.ui.pages",
    "fbxconv.ui.widgets",
    "fbxconv.ui.worker",
    "PyQt5.QtCore",
    "PyQt5.QtGui",
    "PyQt5.QtWidgets",
]

#: Heavyweight modules this tool never touches. QtWebEngine alone is ~100 MB.
excludes = [
    # Qt subsystems we do not use
    "PyQt5.QtWebEngineWidgets",
    "PyQt5.QtWebEngineCore",
    "PyQt5.QtWebEngine",
    "PyQt5.QtQml",
    "PyQt5.QtQuick",
    "PyQt5.QtQuickWidgets",
    "PyQt5.QtMultimedia",
    "PyQt5.QtMultimediaWidgets",
    "PyQt5.QtBluetooth",
    "PyQt5.QtNfc",
    "PyQt5.QtDesigner",
    "PyQt5.QtHelp",
    "PyQt5.QtSql",
    "PyQt5.QtTest",
    "PyQt5.QtLocation",
    "PyQt5.QtPositioning",
    "PyQt5.QtSensors",
    "PyQt5.QtSerialPort",
    "PyQt5.QtWebSockets",
    "PyQt5.QtXmlPatterns",
    # Scientific / imaging stacks that some hooks pull in transitively
    "numpy",
    "pandas",
    "matplotlib",
    "scipy",
    "PIL",
    "IPython",
    # Development-only
    "pytest",
    "_pytest",
    "ruff",
    "mypy",
    "tkinter",
    "pydoc_data",
]

#: UPX reliably corrupts Qt DLLs; never enable it here.
USE_UPX = False


# --------------------------------------------------------------------------- #
# Analysis
# --------------------------------------------------------------------------- #

a = Analysis(
    [str(ENTRY_SCRIPT)],
    pathex=[str(SRC_DIR)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    upx=USE_UPX,
)

pyz = PYZ(a.pure)


# --------------------------------------------------------------------------- #
# Executables
# --------------------------------------------------------------------------- #

_common = {
    "version": str(VERSION_FILE),
    "debug": False,
    "bootloader_ignore_signals": False,
    "strip": False,
    "upx": USE_UPX,
    "upx_exclude": [],
}

if BUILD_MODE == "onefile":
    # Each executable embeds the whole payload, so this roughly doubles the
    # download size. Only used when a single self-contained file is required.
    exe_gui = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name=APP_NAME,
        console=False,
        runtime_tmpdir=None,
        **_common,
    )
    exe_cli = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name=CLI_NAME,
        console=True,
        runtime_tmpdir=None,
        **_common,
    )
else:
    exe_gui = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name=APP_NAME,
        console=False,
        **_common,
    )
    exe_cli = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name=CLI_NAME,
        console=True,
        **_common,
    )
    coll = COLLECT(
        exe_gui,
        exe_cli,
        a.binaries,
        a.datas,
        strip=False,
        upx=USE_UPX,
        upx_exclude=[],
        name=APP_NAME,
    )


# --------------------------------------------------------------------------- #
# Build summary
# --------------------------------------------------------------------------- #

print()
print("=" * 72)
print(f"FbxConverter build plan ({BUILD_MODE})")
print("=" * 72)
print(f"  entry        : {ENTRY_SCRIPT.relative_to(PROJECT_ROOT)}")
print(f"  gui exe      : {APP_NAME}.exe (windowed)")
print(f"  cli exe      : {CLI_NAME}.exe (console)")
print(f"  version info : {VERSION_FILE.name}")
print("  bundled data :")
for source, destination in datas:
    print(f"      {Path(source).relative_to(PROJECT_ROOT)}  ->  {destination}")
print(f"  excluded     : {len(excludes)} modules (QtWebEngine, numpy, pytest, ...)")
print(f"  upx          : {'on' if USE_UPX else 'off'}")
print("=" * 72)
print()

"""Locate bundled data files in both source checkouts and frozen builds.

Two directories are shipped as *data* rather than imported as modules:

``fbxconv/ue_scripts/``
    Executed by Unreal's own bundled Python 3.11 interpreter, never by this
    process. They must exist on disk as readable ``.py`` files, so a frozen
    build has to place them somewhere the app can copy from.

``fbxconv/unity/templates/``
    ``.cs`` / ``.asmdef`` files copied into the Unity project.

Under PyInstaller both live in the extraction directory (``sys._MEIPASS``).
This module is the single place that knows how to find them.
"""

from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path

UE_SCRIPTS_DIRNAME = "ue_scripts"
TEMPLATES_RELATIVE = ("unity", "templates")


@lru_cache(maxsize=1)
def package_root() -> Path:
    """Directory that holds the package's data folders.

    Resolved once per process: the answer cannot change while running, and the
    frozen path costs a couple of ``stat`` calls to verify.
    """
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        candidate = Path(bundle) / "fbxconv"
        if candidate.is_dir():
            return candidate

    return Path(__file__).resolve().parent


def ue_scripts_dir() -> Path:
    """Directory holding the Python scripts that run *inside* Unreal."""
    return package_root() / UE_SCRIPTS_DIRNAME


def templates_dir() -> Path:
    """Directory holding the Unity Editor C# templates."""
    return package_root().joinpath(*TEMPLATES_RELATIVE)


def is_frozen() -> bool:
    """True when running from a PyInstaller bundle."""
    return bool(getattr(sys, "frozen", False))


def describe() -> str:
    """Human-readable description of where data is being loaded from."""
    location = "frozen bundle" if is_frozen() else "source checkout"
    return f"{location}: {package_root()}"


__all__ = [
    "TEMPLATES_RELATIVE",
    "UE_SCRIPTS_DIRNAME",
    "describe",
    "is_frozen",
    "package_root",
    "templates_dir",
    "ue_scripts_dir",
]

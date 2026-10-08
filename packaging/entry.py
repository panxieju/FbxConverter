"""Single entry point for both packaged executables.

``FbxConverter.exe`` (windowed) and ``fbxconv.exe`` (console) are built from
this same frozen script. Each decides what to do by inspecting its own
executable name, which is what lets one analysis produce both binaries.

Behaviour:

===========================  ==========================================
executable                   action
===========================  ==========================================
``fbxconv.exe``              command line (scan / convert / unity-import)
``FbxConverter.exe``         PyQt5 wizard
``FbxConverter.exe cli ...`` command line, for convenience
===========================  ==========================================

Running this file directly from a source checkout also works.
"""

from __future__ import annotations

import multiprocessing
import os
import sys
from pathlib import Path

CLI_EXE_STEM = "fbxconv"
GUI_EXE_STEM = "FbxConverter"


def _bootstrap_source_tree() -> None:
    """Make ``fbxconv`` importable when running unpackaged."""
    if getattr(sys, "frozen", False):
        return
    src = Path(__file__).resolve().parent.parent / "src"
    if src.is_dir() and str(src) not in sys.path:
        sys.path.insert(0, str(src))


def _wants_cli(argv: list[str]) -> bool:
    """Decide between the CLI and the GUI.

    The executable name is authoritative; ``cli`` as the first argument is an
    escape hatch, and ``FBXCONV_FORCE_CLI=1`` overrides everything (handy for
    debugging a windowed build).
    """
    if os.environ.get("FBXCONV_FORCE_CLI") == "1":
        return True
    if Path(sys.executable).stem.lower() == CLI_EXE_STEM:
        return True
    return bool(argv) and argv[0] in {"cli", "--cli"}


def main(argv: list[str] | None = None) -> int:
    # Harmless here, but required for any frozen build that ends up using
    # multiprocessing transitively through a dependency.
    multiprocessing.freeze_support()
    _bootstrap_source_tree()

    args = list(sys.argv[1:] if argv is None else argv)

    if _wants_cli(args):
        if args and args[0] in {"cli", "--cli"}:
            args = args[1:]
        from fbxconv.cli import main as cli_main

        return cli_main(args)

    gui_args = [a for a in args if a not in {"gui", "--gui"}]
    from fbxconv.ui.app import main as gui_main

    return gui_main([sys.argv[0], *gui_args])


if __name__ == "__main__":
    raise SystemExit(main())

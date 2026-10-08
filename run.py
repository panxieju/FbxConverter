"""Convenience launcher.

    python run.py                 # graphical wizard
    python run.py gui             # same
    python run.py cli scan <dir>  # command line
    python run.py scan <dir>      # anything else is forwarded to the CLI

Installing the package (``pip install -e .``) also provides the ``fbxconv``
and ``fbxconv-gui`` console scripts, which do not need this file.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow running straight from a source checkout.
_SRC = Path(__file__).resolve().parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    if not args or args[0] in {"gui", "--gui"}:
        from fbxconv.ui.app import main as gui_main

        return gui_main([sys.argv[0], *args[1:]])

    if args[0] in {"cli", "--cli"}:
        args = args[1:]

    from fbxconv.cli import main as cli_main

    return cli_main(args)


if __name__ == "__main__":
    raise SystemExit(main())

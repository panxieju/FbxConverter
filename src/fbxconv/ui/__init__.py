"""PyQt5 desktop wizard (spec section 4)."""

from __future__ import annotations

__all__ = ["main"]


def main(argv: list[str] | None = None) -> int:
    """Launch the GUI. Imported lazily so the CLI works without PyQt5."""
    from .app import main as _main

    return _main(argv)

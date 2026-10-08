"""GUI entry point."""

from __future__ import annotations

import logging
import sys

from .. import APP_DISPLAY_NAME, __version__
from ..config import AppConfig
from ..logutil import configure_logging


def main(argv: list[str] | None = None) -> int:
    """Create the Qt application and show the conversion wizard."""
    try:
        from PyQt5.QtWidgets import QApplication
    except ImportError as exc:  # pragma: no cover - environment dependent
        sys.stderr.write(
            "需要 PyQt5 才能启动图形界面：pip install PyQt5\n"
            "也可以改用命令行：python -m fbxconv --help\n"
            f"（{exc}）\n"
        )
        return 1

    configure_logging(logging.INFO)

    from .main_window import MainWindow

    app = QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName(APP_DISPLAY_NAME)
    app.setApplicationVersion(__version__)
    app.setOrganizationName("FbxConverter")

    window = MainWindow(AppConfig.load())
    window.show()
    return int(app.exec_())


__all__ = ["main"]

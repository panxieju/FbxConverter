"""GUI regression tests.

These exist because the original offscreen smoke check never enabled INFO
logging, so no log record ever reached the log pane -- and a broken
``LogView.append_line`` therefore went unnoticed until a real run aborted the
process. Anything on the logging path must be exercised headlessly.

Runs against Qt's ``offscreen`` platform plugin; skipped when PyQt5 is absent.
"""

from __future__ import annotations

import logging
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_LOGGING_RULES", "qt.qpa.*=false")

pytest.importorskip("PyQt5.QtWidgets", reason="PyQt5 not installed")


@pytest.fixture(scope="module")
def qt_app():
    """One QApplication for the whole module (Qt forbids more than one)."""
    from PyQt5.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


class TestLogView:
    def test_appends_every_log_level(self, qt_app):
        from fbxconv.ui.widgets import LogView

        view = LogView()
        levels = (
            logging.DEBUG,
            logging.INFO,
            logging.WARNING,
            logging.ERROR,
            logging.CRITICAL,
        )
        for level in levels:
            view.append_line(level, f"level-{level}")

        text = view.toPlainText()
        for level in levels:
            assert f"level-{level}" in text

    def test_append_plain_and_clear(self, qt_app):
        from fbxconv.ui.widgets import LogView

        view = LogView()
        view.append_plain("plain line")
        view.append_plain("coloured line", color="#ff0000")
        assert "plain line" in view.toPlainText()
        assert "coloured line" in view.toPlainText()

        view.clear_log()
        assert view.toPlainText() == ""

    def test_unknown_level_falls_back(self, qt_app):
        from fbxconv.ui.widgets import LogView

        view = LogView()
        view.append_line(9999, "odd level")
        assert "odd level" in view.toPlainText()


class TestLogBridge:
    """Drive logging through MainWindow exactly as a real run does."""

    def test_main_window_routes_info_logs_into_the_pane(self, qt_app):
        from fbxconv.config import AppConfig
        from fbxconv.logutil import configure_logging
        from fbxconv.ui.main_window import MainWindow

        # The original bug only appeared with INFO enabled.
        configure_logging(logging.INFO, quiet_console=True)

        window = MainWindow(AppConfig())
        try:
            logger = logging.getLogger("fbxconv.test")
            logger.info("hello-from-the-test")
            # Qt delivers the queued signal when the event loop turns.
            qt_app.processEvents()

            assert "hello-from-the-test" in window.run_page.log_view.toPlainText()
        finally:
            window.close()

    def test_append_log_never_raises(self, qt_app):
        """A broken log pane must not be able to abort the process."""
        from fbxconv.config import AppConfig
        from fbxconv.ui.main_window import MainWindow

        window = MainWindow(AppConfig())
        try:
            # Simulate an internal rendering failure.
            window.run_page.log_view = object()  # type: ignore[assignment]
            window.run_page.append_log(logging.INFO, "must not raise")
        finally:
            window.close()

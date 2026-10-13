"""GUI flow regression tests.

This module exists because the wizard's scan button was a **silent no-op** on a
fresh config: ``ScanPage._start_scan`` bailed out when ``config.content_root``
was ``None``, but that field is only filled by ``DirectoryPage.apply_to_config()``
-- which ran *inside* ``MainWindow.start_scan()``, i.e. after the guard. Nothing
on the page-1 -> page-2 transition pushed the pickers into the config, so a
correctly filled-in form produced a button that did nothing at all.

Worse, when ``AppConfig`` *did* carry a remembered path, the GUI silently scanned
that stale directory instead of what was on screen.

Runs against Qt's ``offscreen`` platform plugin; skipped when PyQt5 is absent.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_LOGGING_RULES", "qt.qpa.*=false")

pytest.importorskip("PyQt5.QtWidgets", reason="PyQt5 not installed")


@pytest.fixture(scope="module")
def qt_app():
    from PyQt5.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def content_dir(tmp_path):
    """A standalone asset folder, like a downloaded pack."""
    root = tmp_path / "Rapier Anim Set" / "Rapier_AnimSet"
    (root / "Animation").mkdir(parents=True)
    (root / "Animation" / "attack_01.uasset").write_bytes(b"\x00" * 16)
    return root


@pytest.fixture
def output_dir(tmp_path):
    out = tmp_path / "UnityAnimations"
    out.mkdir()
    return out


def _make_window(app_config=None):
    from fbxconv.config import AppConfig
    from fbxconv.ui.main_window import MainWindow

    return MainWindow(app_config or AppConfig())


def _fill(window, content, output):
    window.directory_page.content_picker.set_path(content)
    window.directory_page.output_picker.set_path(output)


class TestDirectoryStateIsPushedToConfig:
    def test_typed_paths_reach_the_config(self, qt_app, content_dir, output_dir):
        window = _make_window()
        try:
            _fill(window, content_dir, output_dir)
            assert window.config.content_root == content_dir
            assert window.config.output_root == output_dir
        finally:
            window.close()

    def test_stale_config_is_replaced_by_the_picker(
        self, qt_app, content_dir, output_dir, tmp_path
    ):
        """A remembered path must never win over what the user just chose."""
        from fbxconv.config import AppConfig

        stale = tmp_path / "OldPack"
        stale.mkdir()
        window = _make_window(AppConfig(content_root=stale, output_root=output_dir))
        try:
            window.directory_page.on_enter()
            window.directory_page.content_picker.set_path(content_dir)
            assert window.config.content_root == content_dir
            assert window.config.content_root != stale
        finally:
            window.close()

    def test_leaving_page_one_pushes_the_state(
        self, qt_app, content_dir, output_dir
    ):
        window = _make_window()
        try:
            _fill(window, content_dir, output_dir)
            window.config.content_root = None
            window.config.output_root = None

            assert window.directory_page.on_leave() is True
            assert window.config.content_root == content_dir
            assert window.config.output_root == output_dir
        finally:
            window.close()

    def test_status_line_reports_a_ready_form(self, qt_app, content_dir, output_dir):
        window = _make_window()
        try:
            _fill(window, content_dir, output_dir)
            window.refresh_navigation()
            text = window.status_label.text()
            assert "请选择 Unreal 资源目录" not in text
            assert "请选择 Unity 输出目录" not in text
        finally:
            window.close()


class TestScanButton:
    def test_scan_starts_with_a_fresh_config(self, qt_app, content_dir, output_dir, monkeypatch):
        """The reported bug: button clicked, nothing happened."""
        window = _make_window()
        try:
            _fill(window, content_dir, output_dir)
            started: list[str] = []
            monkeypatch.setattr(
                type(window), "start_scan", lambda self: started.append("scan")
            )
            window.scan_page._start_scan()
            assert started == ["scan"], "扫描按钮被静默忽略了"
        finally:
            window.close()

    def test_missing_engine_is_explained_not_ignored(
        self, qt_app, content_dir, output_dir, monkeypatch
    ):
        window = _make_window()
        try:
            _fill(window, content_dir, output_dir)
            window.install = None
            warnings: list[str] = []
            monkeypatch.setattr(type(window), "warn", lambda self, m: warnings.append(m))
            started: list[str] = []
            monkeypatch.setattr(
                type(window), "start_scan", lambda self: started.append("scan")
            )

            window.scan_page._start_scan()

            assert not started
            assert warnings, "没有引擎时必须给出原因，而不是毫无反应"
        finally:
            window.close()

    def test_missing_content_is_explained_not_ignored(
        self, qt_app, output_dir, monkeypatch
    ):
        window = _make_window()
        try:
            window.directory_page.output_picker.set_path(output_dir)
            warnings: list[str] = []
            monkeypatch.setattr(type(window), "warn", lambda self, m: warnings.append(m))
            started: list[str] = []
            monkeypatch.setattr(
                type(window), "start_scan", lambda self: started.append("scan")
            )

            window.scan_page._start_scan()

            assert not started
            assert warnings
        finally:
            window.close()

    def test_nonexistent_content_directory_is_rejected(
        self, qt_app, tmp_path, output_dir, monkeypatch
    ):
        window = _make_window()
        try:
            _fill(window, tmp_path / "does-not-exist", output_dir)
            warnings: list[str] = []
            monkeypatch.setattr(type(window), "warn", lambda self, m: warnings.append(m))
            started: list[str] = []
            monkeypatch.setattr(
                type(window), "start_scan", lambda self: started.append("scan")
            )

            window.scan_page._start_scan()

            assert not started
            assert warnings, "路径不存在时必须说明原因"
        finally:
            window.close()


class TestScanPageDoor:
    def test_next_step_is_blocked_without_content(self, qt_app, output_dir):
        window = _make_window()
        try:
            window.directory_page.output_picker.set_path(output_dir)
            assert window.directory_page.is_complete() is False
        finally:
            window.close()

    def test_next_step_is_allowed_with_content(self, qt_app, content_dir, output_dir):
        window = _make_window()
        try:
            _fill(window, content_dir, output_dir)
            window.directory_page._refresh_state()
            if window.install is not None:
                assert window.directory_page.is_complete() is True
        finally:
            window.close()

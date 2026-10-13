"""GUI flow regression tests.

Two real defects drove this module, both of which killed the process rather than
showing an error:

1. The wizard's scan button was a **silent no-op** on a fresh config.
   ``ScanPage._start_scan`` bailed out when ``config.content_root`` was ``None``,
   but that field is only filled by ``DirectoryPage.apply_to_config()`` -- which
   ran *inside* ``MainWindow.start_scan()``, i.e. after the guard. Nothing on the
   page-1 -> page-2 transition pushed the pickers into the config, so a correctly
   filled-in form produced a button that did nothing. When ``AppConfig`` did
   carry a remembered path, the GUI silently scanned that stale directory
   instead, which is worse.

2. The selection page used ``QTreeWidgetItem``'s two-argument
   ``setCheckState(column, state)`` on ``QListWidgetItem``, whose signature takes
   no column. That ``TypeError`` escaped a Qt slot, so Qt went to ``qFatal`` and
   the process died with ``0xC0000409`` -- the same failure mode as the original
   ``setTextColor`` bug (see project.md 6.13).

Runs against Qt's ``offscreen`` platform plugin; skipped when PyQt5 is absent.
"""

from __future__ import annotations

import os
from pathlib import Path

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


# --------------------------------------------------------------------------- #
# A scan result with both kinds of group
# --------------------------------------------------------------------------- #


def _skeletal_group_assets() -> list:
    from fbxconv.models import AssetKind, AssetRecord

    skeleton = "/Game/Pack/Animation/Skeleton/UE4_Mannequin_Skeleton"
    assets = [
        AssetRecord(skeleton, "UE4_Mannequin_Skeleton", "Skeleton", AssetKind.SKELETON),
        AssetRecord(
            "/Game/Pack/mesh/SK_Mannequin",
            "SK_Mannequin",
            "SkeletalMesh",
            AssetKind.SKELETAL_MESH,
            skeleton=skeleton,
        ),
    ]
    for name in ("attack_01", "attack_02", "idle_wait", "jump_start"):
        assets.append(
            AssetRecord(
                f"/Game/Pack/Animation/{name}",
                name,
                "AnimSequence",
                AssetKind.ANIM_SEQUENCE,
                skeleton=skeleton,
                num_frames=30,
                duration=1.0,
            )
        )
    return assets


def _static_group_assets() -> list:
    from fbxconv.models import AssetKind, AssetRecord

    out = []
    for name, triangles in (("SM_Rock", 120), ("SM_Tree", 340), ("SM_Bush", 12)):
        record = AssetRecord(
            f"/Game/Pack/Env/{name}",
            name,
            "StaticMesh",
            AssetKind.STATIC_MESH,
            materials=["/Game/Pack/Env/M_Env"],
        )
        record.textures = ["/Game/Pack/Env/T_Env"]
        record.extra["material_slots"] = [
            {
                "slot": "Env",
                "material": "/Game/Pack/Env/M_Env",
                "textures": ["/Game/Pack/Env/T_Env"],
            }
        ]
        record.extra["triangle_count"] = triangles
        out.append(record)
    return out


def _scan_result(*, skeletal: bool = True, static: bool = True):
    from fbxconv.models import ScanResult
    from fbxconv.pipeline.scan import group_assets

    assets: list = []
    if skeletal:
        assets.extend(_skeletal_group_assets())
    if static:
        assets.extend(_static_group_assets())
    groups = group_assets(assets, mount_point="/Game", scope_prefix="/Game/Pack")
    return ScanResult(
        content_root=Path("/tmp/Content"),
        mount_point="/Game",
        project_file=None,
        conversion_project=None,
        engine_version="5.4.4",
        scope_prefix="/Game/Pack",
        groups=groups,
        assets=assets,
    )


def _window_with_scan(scan):
    window = _make_window()
    window.scan = scan
    window.select_page.on_enter()
    return window


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

    def test_leaving_page_one_pushes_the_state(self, qt_app, content_dir, output_dir):
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
    def test_scan_starts_with_a_fresh_config(
        self, qt_app, content_dir, output_dir, monkeypatch
    ):
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


# --------------------------------------------------------------------------- #
# Selection page: the checklist widgets
# --------------------------------------------------------------------------- #


def _select_group(window, index: int) -> None:
    window.select_page.group_list.setCurrentItem(
        window.select_page.group_list.topLevelItem(index)
    )


def _static_index(scan) -> int:
    return next(i for i, g in enumerate(scan.groups) if g.is_static)


def _skeletal_index(scan) -> int:
    return next(i for i, g in enumerate(scan.groups) if not g.is_static)


def _list_check_states(window) -> list[bool]:
    from PyQt5.QtCore import Qt

    widget = window.select_page.animation_list
    return [
        widget.item(row).checkState() == Qt.Checked for row in range(widget.count())
    ]


class TestSelectionPageLoadsGroups:
    """``_load_group`` used to raise TypeError and take the process with it."""

    def test_populating_does_not_raise(self, qt_app):
        window = _window_with_scan(_scan_result())
        try:
            assert window.select_page.group_list.topLevelItemCount() == 2
        finally:
            window.close()

    def test_every_group_can_be_opened(self, qt_app):
        scan = _scan_result()
        window = _window_with_scan(scan)
        try:
            for index in range(len(scan.groups)):
                _select_group(window, index)
        finally:
            window.close()

    def test_skeletal_group_lists_its_animations(self, qt_app):
        scan = _scan_result()
        window = _window_with_scan(scan)
        try:
            _select_group(window, _skeletal_index(scan))
            assert window.select_page.animation_list.count() == 4
            assert all(_list_check_states(window))
        finally:
            window.close()

    def test_static_group_lists_its_meshes(self, qt_app):
        scan = _scan_result()
        window = _window_with_scan(scan)
        try:
            _select_group(window, _static_index(scan))
            assert window.select_page.animation_list.count() == 3
            assert all(_list_check_states(window))
        finally:
            window.close()

    def test_toggle_all_and_looping_do_not_raise(self, qt_app):
        scan = _scan_result()
        window = _window_with_scan(scan)
        try:
            for index in range(len(scan.groups)):
                _select_group(window, index)
                window.select_page._set_all(False)
                assert not any(_list_check_states(window))
                window.select_page._set_all(True)
                assert all(_list_check_states(window))
                window.select_page._select_looping()
        finally:
            window.close()

    def test_filter_does_not_raise(self, qt_app):
        window = _window_with_scan(_scan_result())
        try:
            window.select_page.filter_edit.setText("attack")
            window.select_page.filter_edit.setText("")
        finally:
            window.close()


class TestSelectionPageBuildsSelection:
    def test_static_group_selects_every_mesh(self, qt_app):
        scan = _scan_result()
        window = _window_with_scan(scan)
        try:
            selection = window.select_page.build_selection()
            static = next(s for s in selection if s.group.is_static)
            assert len(static.selected_meshes) == 3
            assert len(selection) == 2
        finally:
            window.close()

    def test_unchecking_a_mesh_is_honoured(self, qt_app):
        from PyQt5.QtCore import Qt

        scan = _scan_result()
        window = _window_with_scan(scan)
        try:
            _select_group(window, _static_index(scan))
            window.select_page.animation_list.item(0).setCheckState(Qt.Unchecked)
            selection = window.select_page.build_selection()
            static = next(s for s in selection if s.group.is_static)
            assert len(static.selected_meshes) == 2
        finally:
            window.close()

    def test_skeletal_group_reports_checked_animations(self, qt_app):
        from PyQt5.QtCore import Qt

        scan = _scan_result()
        window = _window_with_scan(scan)
        try:
            _select_group(window, _skeletal_index(scan))
            window.select_page.animation_list.item(0).setCheckState(Qt.Unchecked)
            selection = window.select_page.build_selection()
            skeletal = next(s for s in selection if not s.group.is_static)
            assert len(skeletal.animations) == 3
            assert skeletal.reference_mesh is not None
        finally:
            window.close()

    def test_unchecking_a_group_drops_it(self, qt_app):
        from PyQt5.QtCore import Qt

        scan = _scan_result()
        window = _window_with_scan(scan)
        try:
            item = window.select_page.group_list.topLevelItem(0)
            item.setCheckState(0, Qt.Unchecked)
            assert len(window.select_page.build_selection()) == 1
        finally:
            window.close()


class TestSelectionPageSummaries:
    def test_summary_mentions_both_kinds(self, qt_app):
        scan = _scan_result()
        window = _window_with_scan(scan)
        try:
            text = window.select_page.summary.text()
            assert "转换组" in text
            assert "静态网格" in text
        finally:
            window.close()

    def test_skeletal_only_scan_is_handled(self, qt_app):
        scan = _scan_result(static=False)
        window = _window_with_scan(scan)
        try:
            assert len(window.select_page.build_selection()) == 1
        finally:
            window.close()

    def test_static_only_scan_is_handled(self, qt_app):
        scan = _scan_result(skeletal=False)
        window = _window_with_scan(scan)
        try:
            selection = window.select_page.build_selection()
            assert len(selection) == 1
            assert selection[0].group.is_static
        finally:
            window.close()


# --------------------------------------------------------------------------- #
# Settings and run pages: the paths that were never driven from the UI
# --------------------------------------------------------------------------- #


def _plan_ready_window(scan, output_dir):
    """A window whose config is complete enough for ``build_plan`` to run."""
    window = _make_window()
    window.directory_page.output_picker.set_path(output_dir)
    window.scan = scan
    window.plan = None
    window.select_page.on_enter()
    return window


class TestSettingsPage:
    def test_preview_renders_both_layouts(self, qt_app):
        scan = _scan_result()
        window = _window_with_scan(scan)
        try:
            window.settings_page.on_enter()
            text = window.settings_page.preview.toPlainText()
            assert "Characters/" in text
            assert "StaticMeshes/" in text
            assert "_Textures/" in text
        finally:
            window.close()

    def test_preview_hides_textures_when_disabled(self, qt_app):
        scan = _scan_result()
        window = _window_with_scan(scan)
        try:
            window.settings_page.textures_check.setChecked(False)
            window.settings_page.on_enter()
            assert "_Textures/" not in window.settings_page.preview.toPlainText()
        finally:
            window.close()

    def test_collect_carries_the_texture_flag(self, qt_app):
        window = _make_window()
        try:
            assert window.settings_page.collect().export_textures is True
            window.settings_page.textures_check.setChecked(False)
            settings = window.settings_page.collect()
            assert settings.export_textures is False
            assert settings.to_payload()["export_textures"] is False
        finally:
            window.close()


class TestRunPage:
    def test_plan_preview_lists_meshes_animations_and_textures(
        self, qt_app, output_dir
    ):
        scan = _scan_result()
        window = _plan_ready_window(scan, output_dir)
        try:
            assert window.ensure_plan() is not None
            window.run_page.refresh_plan()

            table = window.run_page.table
            kinds = [table.item(row, 1).text() for row in range(table.rowCount())]
            assert "Static Mesh" in kinds
            assert "Animation Sequence" in kinds
            assert "贴图" in kinds
            assert table.rowCount() == len(window.plan.all_requests) + len(
                window.plan.texture_requests
            )
        finally:
            window.close()

    def test_summary_counts_static_groups(self, qt_app, output_dir):
        scan = _scan_result()
        window = _plan_ready_window(scan, output_dir)
        try:
            window.ensure_plan()
            window.run_page.refresh_plan()
            text = window.run_page.summary_label.text()
            assert "静态组" in text
            assert "贴图" in text
        finally:
            window.close()

    def test_item_updates_do_not_raise(self, qt_app, output_dir):
        from fbxconv.models import ExportOutcome, ExportRequest, ItemStatus

        scan = _scan_result()
        window = _plan_ready_window(scan, output_dir)
        try:
            window.ensure_plan()
            window.run_page.refresh_plan()

            record = scan.static_groups[0].meshes[0]
            request = ExportRequest(
                kind=record.kind,
                package_path=record.package_path,
                output_rel="StaticMeshes/Env/SM_Rock.fbx",
            )
            for status in ItemStatus:
                window.run_page.update_item(
                    ExportOutcome(request=request, status=status, detail="detail")
                )
        finally:
            window.close()

    def test_show_report_does_not_raise(self, qt_app):
        from fbxconv.report import ExportReport

        window = _make_window()
        try:
            window.run_page.show_report(ExportReport())
        finally:
            window.close()

    def test_retry_without_a_report_is_a_no_op(self, qt_app):
        window = _make_window()
        try:
            window.report = None
            window.retry_failed()
        finally:
            window.close()


class TestWizardNavigation:
    def test_walking_forward_and_back_does_not_raise(
        self, qt_app, content_dir, output_dir
    ):
        window = _make_window()
        try:
            _fill(window, content_dir, output_dir)
            window.scan = _scan_result()
            for index in range(len(window.pages)):
                window.go_to(index)
            for index in range(len(window.pages) - 1, -1, -1):
                window.go_to(index)
        finally:
            window.close()


# --------------------------------------------------------------------------- #
# The blunt instrument: touch every widget
#
# All three crashes in this module's history were "an exception escaped a Qt
# slot" -- which is fatal (qFatal -> 0xC0000409), not a printed traceback:
#   * QPlainTextEdit.setTextColor          (a method that does not exist)
#   * QListWidgetItem.setCheckState(0, ..) (the QTreeWidgetItem signature)
#   * start_export(requests=None) receiving clicked's `checked=False`
# None of them are visible to a linter or to a test that only reads state.
# Clicking everything is the only cheap defence that covers all three.
# --------------------------------------------------------------------------- #


@pytest.fixture
def inert_workers(monkeypatch):
    """Keep the workers from launching an engine, without hiding signature bugs.

    ``run()`` is replaced (not ``__init__``), so constructing a worker -- where
    the ``list(False)`` bug lived -- still happens for real.
    """
    from fbxconv.ui import worker as worker_module

    monkeypatch.setattr(worker_module.ScanWorker, "run", lambda self: None)
    monkeypatch.setattr(worker_module.ExportWorker, "run", lambda self: None)


@pytest.fixture
def no_dialogs(monkeypatch):
    """Stop every modal dialog from blocking the headless run."""
    warnings: list[str] = []
    from PyQt5.QtWidgets import QFileDialog

    from fbxconv.ui import main_window as main_window_module

    monkeypatch.setattr(
        main_window_module.MainWindow, "warn", lambda self, message: warnings.append(message)
    )
    # "浏览…" opens a real file dialog, which would wait for a human forever.
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: ""))
    monkeypatch.setattr(QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: ("", "")))
    monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: ("", "")))
    return warnings


class TestEveryWidgetIsSafeToUse:
    def test_clicking_every_button_on_every_page(
        self, qt_app, content_dir, output_dir, inert_workers, no_dialogs, monkeypatch
    ):
        from PyQt5.QtCore import Qt
        from PyQt5.QtWidgets import QAbstractButton

        from fbxconv.ui import main_window as main_window_module

        monkeypatch.setattr(
            main_window_module, "open_in_file_manager", lambda path: None
        )

        window = _make_window()
        try:
            _fill(window, content_dir, output_dir)

            clicked: list[str] = []
            for index, page in enumerate(window.pages):
                if page is window.select_page:
                    # The real scan populates this; here it is injected after the
                    # scan page has been walked, because clicking "开始扫描"
                    # legitimately resets scan/plan/report.
                    window.scan = _scan_result()
                window.go_to(index)
                if page is window.select_page:
                    for row in range(window.select_page.group_list.topLevelItemCount()):
                        item = window.select_page.group_list.topLevelItem(row)
                        if item.flags() & Qt.ItemIsUserCheckable:
                            item.setCheckState(0, Qt.Checked)
                    window.select_page._set_all(True)
                    window.select_page._update_summary()

                for button in page.findChildren(QAbstractButton):
                    if not button.isEnabled():
                        continue
                    clicked.append(f"{type(page).__name__}.{button.text()}")
                    button.click()
                    qt_app.processEvents()

            # The walk really did press the two buttons that mattered.
            assert any("开始扫描" in entry for entry in clicked), clicked
            assert any("开始转换" in entry for entry in clicked), clicked
        finally:
            window.close()

    def test_toggling_every_checkbox(
        self, qt_app, content_dir, output_dir, inert_workers, no_dialogs
    ):
        from PyQt5.QtWidgets import QCheckBox

        window = _make_window()
        try:
            _fill(window, content_dir, output_dir)
            window.scan = _scan_result()
            window.select_page.on_enter()
            for index, page in enumerate(window.pages):
                window.go_to(index)
                for box in page.findChildren(QCheckBox):
                    box.setChecked(not box.isChecked())
                    box.setChecked(not box.isChecked())
                    qt_app.processEvents()
        finally:
            window.close()

    def test_changing_every_combo_box(
        self, qt_app, content_dir, output_dir, inert_workers, no_dialogs
    ):
        from PyQt5.QtWidgets import QComboBox

        window = _make_window()
        try:
            _fill(window, content_dir, output_dir)
            window.scan = _scan_result()
            window.select_page.on_enter()
            for index, page in enumerate(window.pages):
                window.go_to(index)
                for combo in page.findChildren(QComboBox):
                    for row in range(combo.count()):
                        combo.setCurrentIndex(row)
                        qt_app.processEvents()
        finally:
            window.close()

    def test_run_button_does_not_pass_a_bool_as_requests(
        self, qt_app, content_dir, output_dir, inert_workers, no_dialogs
    ):
        """The exact reported crash: clicked's `checked` became ``requests``."""
        window = _make_window()
        try:
            _fill(window, content_dir, output_dir)
            window.scan = _scan_result()
            window.select_page.on_enter()
            window.go_to(4)
            assert window.plan is not None

            window.run_page.start_button.click()
            qt_app.processEvents()

            assert window._export_worker is not None
            assert window._export_worker._requests is None
            assert window._export_worker._previous is None
        finally:
            window.close()

    def test_start_export_rejects_a_positional_argument(self, qt_app):
        """Keyword-only keeps a stray signal argument from being interpreted."""
        window = _make_window()
        try:
            with pytest.raises(TypeError):
                window.start_export(False)  # type: ignore[misc]
        finally:
            window.close()


class TestNoConnectedSlotAcceptsOptionalArguments:
    """Static half of the same guard.

    ``QPushButton.clicked`` emits ``checked``. PyQt truncates the signal's
    arguments to the slot's arity, but a parameter with a default is still
    filled -- measured: a slot ``def slot(self, value=None)`` receives
    ``(False,)``. So connecting a signal directly to such a method hands it a
    bool, which is how ``start_export(requests=None)`` ended up doing
    ``list(False)``.

    Runtime clicking (above) catches the ones that actually break; this catches
    the shape, including in paths no test happens to press.
    """

    def test_no_connect_target_has_a_defaulted_parameter(self):
        import ast
        import pathlib
        import re

        ui = pathlib.Path(__file__).resolve().parents[1] / "src" / "fbxconv" / "ui"
        defaulted: dict[str, str] = {}
        for path in sorted(ui.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.FunctionDef):
                    continue
                positional = node.args.posonlyargs + node.args.args
                if not positional or positional[0].arg != "self":
                    continue
                tail = positional[1:]
                first_default = len(tail) - len(node.args.defaults)
                has_default = any(i >= first_default for i in range(len(tail)))
                has_default = has_default or any(
                    d is not None for d in node.args.kw_defaults
                )
                if has_default:
                    defaulted[node.name] = f"{path.name}:{node.lineno}"
        # The methods we know about, so a rename cannot silently disable this.
        assert "start_export" in defaulted

        offenders: list[str] = []
        for path in sorted(ui.glob("*.py")):
            for lineno, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), 1
            ):
                if ".connect(" not in line:
                    continue
                for name in defaulted:
                    if re.search(r"connect\(\s*(?:[\w.]+\.)?" + name + r"\s*\)", line):
                        offenders.append(f"{path.name}:{lineno}: {line.strip()}")

        assert not offenders, (
            "信号会把参数塞进带默认值的形参：改用 lambda 或把参数设为仅关键字。\n"
            + "\n".join(offenders)
        )


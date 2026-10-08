"""Wizard shell hosting the five conversion steps."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path

from PyQt5.QtCore import QObject, pyqtSignal
from PyQt5.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .. import APP_DISPLAY_NAME, __version__
from ..config import AppConfig
from ..errors import FbxConvError
from ..logutil import CallbackHandler, get_logger
from ..models import ExportOutcome, ExportRequest, ScanResult
from ..report import ExportReport
from ..unreal.locator import UnrealInstall
from .pages import DirectoryPage, RunPage, ScanPage, SelectPage, SettingsPage
from .widgets import COLOR_MUTED, StepList, open_in_file_manager
from .worker import ExportWorker, ScanWorker, make_plan

_log = get_logger("ui")

STEP_TITLES = (
    "选择目录",
    "扫描资源",
    "选择转换内容",
    "设置导出",
    "开始转换",
)


class _LogBridge(QObject):
    """Marries the stdlib logging world to Qt's thread-safe signals."""

    message = pyqtSignal(int, str)


class MainWindow(QMainWindow):
    def __init__(self, config: AppConfig | None = None) -> None:
        super().__init__()
        self.config = config or AppConfig.load()

        # --- shared state ---------------------------------------------------
        self.install: UnrealInstall | None = None
        self.scan: ScanResult | None = None
        self.plan = None
        self.report: ExportReport | None = None
        self._scan_worker: ScanWorker | None = None
        self._export_worker: ExportWorker | None = None

        self.setWindowTitle(f"{APP_DISPLAY_NAME} v{__version__}")
        self.resize(1180, 820)
        self.setMinimumSize(980, 700)

        # --- layout ---------------------------------------------------------
        central = QWidget()
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.step_list = StepList(STEP_TITLES)
        root.addWidget(self.step_list)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)

        self.stack = QStackedWidget()
        self.directory_page = DirectoryPage(self)
        self.scan_page = ScanPage(self)
        self.select_page = SelectPage(self)
        self.settings_page = SettingsPage(self)
        self.run_page = RunPage(self)
        self.pages = [
            self.directory_page,
            self.scan_page,
            self.select_page,
            self.settings_page,
            self.run_page,
        ]
        for page in self.pages:
            self.stack.addWidget(page)
        right_layout.addWidget(self.stack, 1)

        nav = QWidget()
        nav_layout = QHBoxLayout(nav)
        nav_layout.setContentsMargins(24, 10, 24, 14)
        self.status_label = QLabel("")
        self.status_label.setStyleSheet(f"color: {COLOR_MUTED};")
        nav_layout.addWidget(self.status_label, 1)
        self.back_button = QPushButton("上一步")
        self.back_button.clicked.connect(self.go_back)
        self.next_button = QPushButton("下一步")
        self.next_button.setDefault(True)
        self.next_button.clicked.connect(self.go_next)
        nav_layout.addWidget(self.back_button)
        nav_layout.addWidget(self.next_button)
        right_layout.addWidget(nav)

        root.addWidget(right, 1)
        self.setCentralWidget(central)

        # --- logging into the run page --------------------------------------
        self._log_bridge = _LogBridge()
        self._log_bridge.message.connect(self.run_page.append_log)
        handler = CallbackHandler(
            lambda level, text: self._log_bridge.message.emit(level, text)
        )
        handler.setFormatter(logging.Formatter("%(asctime)s  %(message)s", datefmt="%H:%M:%S"))
        logging.getLogger("fbxconv").addHandler(handler)

        self.go_to(0)

    # ------------------------------------------------------------------ paths

    @property
    def export_root(self) -> Path | None:
        return self.config.export_root

    @property
    def work_root(self) -> Path | None:
        return self.config.effective_working_root

    # ------------------------------------------------------------- navigation

    @property
    def current_index(self) -> int:
        return self.stack.currentIndex()

    def go_to(self, index: int) -> None:
        index = max(0, min(index, len(self.pages) - 1))
        if index != self.current_index and not self.pages[self.current_index].on_leave():
            return
        self.stack.setCurrentIndex(index)
        self.step_list.set_current(index)
        self.pages[index].on_enter()
        self.refresh_navigation()

    def go_back(self) -> None:
        self.go_to(self.current_index - 1)

    def go_next(self) -> None:
        self.go_to(self.current_index + 1)

    def refresh_navigation(self) -> None:
        index = self.current_index
        page = self.pages[index]
        self.back_button.setEnabled(index > 0)
        is_last = index == len(self.pages) - 1
        self.next_button.setVisible(not is_last)
        self.next_button.setEnabled(not is_last and page.is_complete())
        self.status_label.setText(self._status_text())

    def _status_text(self) -> str:
        if self.install is None:
            return "请选择 Unreal Editor。"
        if self.config.content_root is None:
            return "请选择 Unreal 资源目录。"
        if self.config.output_root is None:
            return "请选择 Unity 输出目录。"
        if self.scan is None:
            return f"引擎：{self.install.label}    等待扫描。"
        return (
            f"引擎：{self.install.label}    "
            f"资源 {len(self.scan.assets)} 个 / 骨架组 {len(self.scan.groups)} 个 / "
            f"动画 {self.scan.total_animations} 个"
        )

    def checked_group_indices(self) -> list[int]:
        try:
            return self.select_page._checked_groups()
        except Exception:  # pragma: no cover - defensive
            return []

    # -------------------------------------------------------------- scan flow

    def start_scan(self) -> None:
        self.directory_page.apply_to_config()
        if self.install is None:
            self._warn("没有可用的 Unreal Editor。")
            return
        content = self.config.content_root
        work = self.work_root
        if content is None or work is None:
            self._warn("请先设置资源目录和输出目录。")
            return

        self.scan = None
        self.plan = None
        self.report = None

        worker = ScanWorker(self.install, work, self.config.link_mode, content)
        worker.progress.connect(self._on_progress)
        worker.event.connect(self._on_event)
        worker.finished_ok.connect(self._on_scan_finished)
        worker.failed.connect(self._on_worker_failed)
        self._scan_worker = worker
        self.scan_page.set_running(True)
        worker.start()
        self.refresh_navigation()

    def cancel_scan(self) -> None:
        if self._scan_worker is not None:
            self._scan_worker.cancel()

    def _on_scan_finished(self, scan: ScanResult) -> None:
        self.scan = scan
        self.plan = None
        self.report = None
        self.scan_page.set_running(False)
        self.scan_page.progress.set_fraction(1.0)
        self.scan_page.progress.set_caption(
            f"完成：确认 {len(scan.assets)} 个资源，用时 {scan.scan_seconds:.1f}s"
        )
        self.scan_page.show_result(scan)
        self.select_page.on_enter()
        self.run_page.table.setRowCount(0)
        self.refresh_navigation()

    # ------------------------------------------------------------ export flow

    def ensure_plan(self):
        """Build (and cache) the conversion plan from the current UI state."""
        if self.scan is None:
            return None
        try:
            selection = self.select_page.build_selection()
            if not selection:
                return None
            settings = self.settings_page.collect()
            self.plan = make_plan(
                self.scan,
                selection,
                settings,
                export_root=self.export_root,
                work_root=self.work_root,
                unity_project=self.config.unity_project,
            )
        except FbxConvError as exc:
            _log.error("%s", exc)
            return None
        return self.plan

    def start_export(
        self,
        requests: Sequence[ExportRequest] | None = None,
        previous: Sequence[ExportOutcome] | None = None,
    ) -> None:
        if self._export_worker is not None and self._export_worker.isRunning():
            return
        plan = self.plan or self.ensure_plan()
        if plan is None:
            self._warn("无法建立转换计划：请确认已扫描并选择了骨架组。")
            return
        self.plan = plan
        if self.install is None:
            self._warn("没有可用的 Unreal Editor。")
            return

        if requests is None and previous is None:
            self.run_page.refresh_plan()

        worker = ExportWorker(
            self.install, plan, requests=requests, previous=previous
        )
        worker.progress.connect(self._on_progress)
        worker.event.connect(self._on_event)
        worker.item.connect(self.run_page.update_item)
        worker.finished_ok.connect(self._on_export_finished)
        worker.failed.connect(self._on_worker_failed)
        self._export_worker = worker
        self.run_page.set_running(True)
        worker.start()

    def cancel_export(self) -> None:
        if self._export_worker is not None:
            self._export_worker.cancel()

    def retry_failed(self) -> None:
        if self.report is None:
            return
        failed = self.report.retryable
        if not failed:
            return
        requests = [outcome.request for outcome in failed]
        self.start_export(requests=requests, previous=self.report.outcomes)

    def _on_export_finished(self, report: ExportReport) -> None:
        self.report = report
        self.run_page.set_running(False)
        self.run_page.progress.set_fraction(1.0)
        self.run_page.progress.set_caption(report.summary_line())
        self.run_page.show_report(report)
        self.refresh_navigation()

    def open_output(self) -> None:
        root = self.export_root
        if root is None or not root.exists():
            self._warn("输出目录尚未生成。")
            return
        open_in_file_manager(root)

    # -------------------------------------------------------------- callbacks

    def _on_progress(self, message: str, fraction) -> None:
        page = self.current_page
        if hasattr(page, "progress"):
            page.progress.set_caption(message)
            page.progress.set_fraction(fraction)
        _log.info("%s", message)

    def _on_event(self, event: dict) -> None:
        kind = event.get("type")
        if kind == "phase":
            _log.info("%s", event.get("message", ""))
        elif kind == "progress":
            _log.debug("%s", event.get("message", ""))
        elif kind == "log":
            level = logging.ERROR if event.get("level") == "error" else logging.INFO
            _log.log(level, "%s", event.get("message", ""))
        elif kind == "fatal":
            _log.error("%s", event.get("error", ""))

    @property
    def current_page(self):
        return self.pages[self.current_index]

    def _on_worker_failed(self, message: str, detail: str) -> None:
        self.scan_page.set_running(False)
        self.run_page.set_running(False)
        self.scan_page.progress.set_fraction(0.0)
        self.run_page.progress.set_fraction(0.0)
        self.scan_page.progress.set_caption(message)
        self.run_page.progress.set_caption(message)
        _log.error("%s", message)
        if detail:
            _log.debug("%s", detail)

    def _warn(self, message: str) -> None:
        _log.warning("%s", message)
        QMessageBox.warning(self, APP_DISPLAY_NAME, message)

    # ------------------------------------------------------------- lifecycle

    def closeEvent(self, event) -> None:
        for worker in (self._scan_worker, self._export_worker):
            if worker is not None and worker.isRunning():
                worker.cancel()
                worker.wait(5000)
        try:
            self.config.save()
        except OSError as exc:  # pragma: no cover
            _log.warning("保存配置失败：%s", exc)
        super().closeEvent(event)


__all__ = ["STEP_TITLES", "MainWindow"]

"""Background workers so the UI never blocks on Unreal."""

from __future__ import annotations

import threading
import traceback
from collections.abc import Sequence
from pathlib import Path

from PyQt5.QtCore import QThread, pyqtSignal

from ..errors import CancelledError, FbxConvError
from ..logutil import get_logger
from ..models import (
    ConversionPlan,
    ExportOutcome,
    ExportRequest,
    ExportSettings,
    ScanResult,
    SelectedGroup,
)
from ..pipeline.export import ExportSession, build_plan
from ..pipeline.scan import ScanSession
from ..unreal.locator import UnrealInstall
from ..unreal.project import MountMode

_log = get_logger("ui.worker")


class _CancellableThread(QThread):
    """Base class wiring a ``threading.Event`` to a friendly cancel()."""

    progress = pyqtSignal(str, object)  # message, fraction|None
    event = pyqtSignal(dict)
    failed = pyqtSignal(str, str)  # message, detail

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def _report_error(self, exc: BaseException) -> None:
        if isinstance(exc, CancelledError):
            self.failed.emit("已取消", "")
            return
        if isinstance(exc, FbxConvError):
            self.failed.emit(str(exc), exc.detail or "")
            return
        self.failed.emit(f"{type(exc).__name__}: {exc}", traceback.format_exc())


class ScanWorker(_CancellableThread):
    """Runs :class:`ScanSession` off the GUI thread."""

    finished_ok = pyqtSignal(object)  # ScanResult

    def __init__(
        self,
        install: UnrealInstall,
        work_root: Path,
        mount_mode: MountMode | str,
        content_root: Path,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._install = install
        self._work_root = Path(work_root)
        self._mount_mode = mount_mode
        self._content_root = Path(content_root)

    def run(self) -> None:
        try:
            session = ScanSession(
                self._install,
                self._work_root,
                mount_mode=self._mount_mode,
                on_progress=lambda msg, frac: self.progress.emit(msg, frac),
                on_event=lambda ev: self.event.emit(ev),
                cancel_event=self._cancel,
            )
            result = session.run(self._content_root)
        except BaseException as exc:
            self._report_error(exc)
            return
        self.finished_ok.emit(result)


class ExportWorker(_CancellableThread):
    """Runs :class:`ExportSession` off the GUI thread."""

    item = pyqtSignal(object)  # ExportOutcome
    finished_ok = pyqtSignal(object)  # ExportReport

    def __init__(
        self,
        install: UnrealInstall,
        plan: ConversionPlan,
        *,
        requests: Sequence[ExportRequest] | None = None,
        previous: Sequence[ExportOutcome] | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._install = install
        self._plan = plan
        self._requests = list(requests) if requests is not None else None
        self._previous = list(previous) if previous is not None else None

    def run(self) -> None:
        try:
            session = ExportSession(
                self._install,
                self._plan,
                on_progress=lambda msg, frac: self.progress.emit(msg, frac),
                on_event=lambda ev: self.event.emit(ev),
                on_item=lambda outcome: self.item.emit(outcome),
                cancel_event=self._cancel,
            )
            report = session.run(self._requests, previous=self._previous)
        except BaseException as exc:
            self._report_error(exc)
            return
        self.finished_ok.emit(report)


def make_plan(
    scan: ScanResult,
    selection: Sequence[SelectedGroup],
    settings: ExportSettings,
    *,
    export_root: Path,
    work_root: Path,
    unity_project: Path | None = None,
) -> ConversionPlan:
    """Thin wrapper so pages do not need to import the pipeline directly."""
    return build_plan(
        scan,
        selection,
        settings,
        export_root=export_root,
        work_root=work_root,
        unity_project=unity_project,
    )


class PackageWorker(_CancellableThread):
    """Imports the export inside Unity and writes a ``.unitypackage``.

    Animation is applied by the C# importer, so this has to run in the editor;
    ``run_batch_package`` only returns when the editor process exits.
    """

    finished_ok = pyqtSignal(str)  # package path

    def __init__(
        self,
        install,
        project: Path,
        export_root: Path,
        package_path: Path,
        *,
        include_editor_scripts: bool = True,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._install = install
        self._project = Path(project)
        self._export_root = Path(export_root)
        self._package_path = Path(package_path)
        self._include_editor_scripts = include_editor_scripts

    def run(self) -> None:
        from ..unity.runner import run_batch_package

        try:
            result = run_batch_package(
                self._install,
                self._project,
                self._export_root,
                self._package_path,
                include_editor_scripts=self._include_editor_scripts,
                on_output=lambda line: self.progress.emit(line, None),
            )
        except BaseException as exc:
            self._report_error(exc)
            return

        if not result.ok:
            self.failed.emit(
                f"Unity 打包失败（退出码 {result.returncode}）。日志：{result.log_path}",
                result.output_tail,
            )
            return
        if not self._package_path.is_file():
            self.failed.emit(
                f"Unity 退出码为 0，但没有生成 {self._package_path.name}。",
                result.output_tail,
            )
            return
        self.finished_ok.emit(str(self._package_path))


__all__ = ["ExportWorker", "PackageWorker", "ScanWorker", "make_plan"]

"""The five wizard pages (spec section 4)."""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor, QFont
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..discover import find_uproject, resolve_content_root
from ..errors import ContentRootError
from ..models import (
    ExportOutcome,
    GroupStatus,
    ImportType,
    ItemStatus,
    LoopMode,
    RootMotionMode,
    ScanResult,
    SelectedGroup,
)
from ..unreal.locator import find_installs
from ..unreal.project import MountMode
from .widgets import (
    COLOR_ERROR,
    COLOR_MUTED,
    COLOR_OK,
    COLOR_WARN,
    LogView,
    PathPicker,
    ProgressPanel,
    heading,
    muted,
)

if TYPE_CHECKING:  # pragma: no cover
    from .main_window import MainWindow

_STATUS_COLORS = {
    GroupStatus.READY: COLOR_OK,
    GroupStatus.MISSING_DEPS: COLOR_ERROR,
    GroupStatus.EMPTY: COLOR_MUTED,
}


class WizardPage(QWidget):
    """Base class: pages read/write state on the owning window."""

    title = ""
    subtitle = ""

    def __init__(self, window: MainWindow, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.window = window
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(24, 20, 24, 16)
        self.body.setSpacing(10)

        if self.title:
            self.body.addWidget(heading(self.title, size=16))
        if self.subtitle:
            self.body.addWidget(muted(self.subtitle))

    def on_enter(self) -> None:
        """Called every time the page becomes visible."""

    def on_leave(self) -> bool:
        """Return False to veto navigation away from this page."""
        return True

    def is_complete(self) -> bool:
        return True


# --------------------------------------------------------------------------- #
# 1. Directories
# --------------------------------------------------------------------------- #


class DirectoryPage(WizardPage):
    title = "选择目录"
    subtitle = (
        "设置 Unreal Editor、Unreal 资源目录与 Unity 输出目录。"
        "软件会自动查找资源目录附近的 .uproject；找不到时创建独立转换项目。"
    )

    def __init__(self, window: MainWindow, parent=None) -> None:
        super().__init__(window, parent)

        # --- engine ---------------------------------------------------------
        engine_box = QGroupBox("Unreal Editor")
        engine_layout = QVBoxLayout(engine_box)

        self.engine_combo = QComboBox()
        self.engine_combo.currentIndexChanged.connect(self._on_engine_changed)
        engine_layout.addWidget(self.engine_combo)

        self.engine_path = PathPicker(
            mode="open", placeholder="或手动选择 UnrealEditor-Cmd.exe",
            file_filter="UnrealEditor-Cmd.exe (UnrealEditor-Cmd.exe);;可执行文件 (*.exe)",
        )
        self.engine_path.changed.connect(lambda _: self._refresh_state())
        engine_layout.addWidget(self.engine_path)
        self.body.addWidget(engine_box)

        # --- directories ----------------------------------------------------
        dirs_box = QGroupBox("目录")
        form = QFormLayout(dirs_box)
        form.setLabelAlignment(Qt.AlignRight)

        self.content_picker = PathPicker(mode="dir", placeholder="Unreal 资源目录（Content 文件夹）")
        self.content_picker.changed.connect(lambda _: self._on_content_changed())
        form.addRow("Unreal 资源目录", self.content_picker)

        self.output_picker = PathPicker(mode="dir", placeholder="Unity 输出目录")
        self.output_picker.changed.connect(lambda _: self._refresh_state())
        form.addRow("Unity 输出目录", self.output_picker)

        self.mount_combo = QComboBox()
        for mode in MountMode:
            self.mount_combo.addItem(mode.label, mode.value)
        self.mount_combo.currentIndexChanged.connect(lambda _: self._refresh_state())
        form.addRow("独立项目挂载方式", self.mount_combo)
        self.body.addWidget(dirs_box)

        self.project_info = muted("尚未选择资源目录。")
        self.body.addWidget(self.project_info)

        self.body.addStretch(1)

    # ------------------------------------------------------------------ hooks

    def on_enter(self) -> None:
        if self.engine_combo.count() == 0:
            self._populate_engines()
        if not self.content_picker.text() and self.window.config.content_root:
            self.content_picker.set_path(self.window.config.content_root)
        if not self.output_picker.text() and self.window.config.output_root:
            self.output_picker.set_path(self.window.config.output_root)
        self._refresh_state()

    def is_complete(self) -> bool:
        return self.window.install is not None and self._content_ok() and bool(
            self.output_picker.text()
        )

    # --------------------------------------------------------------- internal

    def _populate_engines(self) -> None:
        self.engine_combo.blockSignals(True)
        self.engine_combo.clear()
        installs = find_installs()
        for install in installs:
            self.engine_combo.addItem(
                f"{install.label}  —  {install.root}", install.editor_cmd
            )
        self.engine_combo.addItem("手动指定…", None)
        self.engine_combo.blockSignals(False)

        preferred = self.window.config.unreal_editor
        if preferred is not None:
            for index in range(self.engine_combo.count()):
                if self.engine_combo.itemData(index) == str(preferred):
                    self.engine_combo.setCurrentIndex(index)
                    break
        self._on_engine_changed()

    def _on_engine_changed(self) -> None:
        data = self.engine_combo.currentData()
        if data:
            self.engine_path.set_path(data)
            self.engine_path.setEnabled(False)
        else:
            self.engine_path.setEnabled(True)
        self._refresh_state()

    def _on_content_changed(self) -> None:
        text = self.content_picker.text()
        if not text:
            self.project_info.setText("尚未选择资源目录。")
            self._refresh_state()
            return
        try:
            info = resolve_content_root(text)
        except ContentRootError as exc:
            self.project_info.setText(f"⚠ {exc}")
            self.project_info.setStyleSheet(f"color: {COLOR_ERROR};")
            self._refresh_state()
            return

        project = find_uproject(info.path)
        if project is not None:
            message = f"找到 Unreal 项目：{project.name}（挂载点 /Game）"
        else:
            message = (
                "未找到 .uproject —— 将创建独立转换项目并保留相对于 Content 的资源路径。"
            )
        if info.was_derived:
            message += f"\n资源根目录已上溯到：{info.path}"
        self.project_info.setText(message)
        self.project_info.setStyleSheet(f"color: {COLOR_MUTED};")
        self._refresh_state()

    def _content_ok(self) -> bool:
        text = self.content_picker.text()
        if not text:
            return False
        try:
            return resolve_content_root(text).path.is_dir()
        except ContentRootError:
            return False

    def _refresh_state(self) -> None:
        from ..unreal.locator import resolve_editor

        editor = self.engine_path.text()
        self.window.install = resolve_editor(editor) if editor else None
        if editor and self.window.install is None:
            self.project_info.setText(f"⚠ 无法识别 Unreal 路径：{editor}")
        self.window.refresh_navigation()

    # ------------------------------------------------------------------ state

    def apply_to_config(self) -> None:
        config = self.window.config
        if self.window.install is not None:
            config.unreal_editor = self.window.install.editor_cmd
            config.remember_unreal_editor(self.window.install.editor_cmd)
        content = self.content_picker.path()
        if content is not None:
            config.content_root = content
            config.remember_content_root(content)
        output = self.output_picker.path()
        if output is not None:
            config.output_root = output
            config.remember_output_root(output)
        config.link_mode = self.mount_combo.currentData() or MountMode.JUNCTION.value


# --------------------------------------------------------------------------- #
# 2. Scan
# --------------------------------------------------------------------------- #


class ScanPage(WizardPage):
    title = "扫描资源"
    subtitle = "先进行文件发现，再通过 Unreal 确认资源类型和依赖（不依赖文件名前缀判断）。"

    def __init__(self, window: MainWindow, parent=None) -> None:
        super().__init__(window, parent)

        controls = QHBoxLayout()
        self.scan_button = QPushButton("开始扫描")
        self.scan_button.setMinimumWidth(120)
        self.scan_button.clicked.connect(self._start_scan)
        self.cancel_button = QPushButton("取消")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self._cancel_scan)
        controls.addWidget(self.scan_button)
        controls.addWidget(self.cancel_button)
        controls.addStretch(1)
        self.body.addLayout(controls)

        self.progress = ProgressPanel()
        self.body.addWidget(self.progress)

        summary = QGroupBox("扫描结果")
        grid = QGridLayout(summary)
        self.summary_labels: dict[str, QLabel] = {}
        for column, (key, caption) in enumerate(
            [
                ("files", "资源文件"),
                ("assets", "已确认资源"),
                ("groups", "骨架组"),
                ("animations", "动画数量"),
                ("missing", "缺失依赖"),
            ]
        ):
            grid.addWidget(muted(caption), 0, column)
            value = QLabel("—")
            font = value.font()
            font.setPointSize(15)
            font.setBold(True)
            value.setFont(font)
            grid.addWidget(value, 1, column)
            self.summary_labels[key] = value
        self.body.addWidget(summary)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(5)
        self.tree.setHeaderLabels(["骨架组", "参考网格", "动画", "状态", "输出目录"])
        self.tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.tree.header().setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self.tree.setAlternatingRowColors(True)
        self.body.addWidget(self.tree, 1)

        self.warning_label = muted("")
        self.body.addWidget(self.warning_label)

    # ------------------------------------------------------------------ hooks

    def is_complete(self) -> bool:
        return self.window.scan is not None and any(
            g.meshes and g.status is not GroupStatus.MISSING_DEPS
            for g in self.window.scan.groups
        )

    # ----------------------------------------------------------------- actions

    def _start_scan(self) -> None:
        if self.window.install is None or self.window.config.content_root is None:
            return
        self.window.start_scan()
        self.scan_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.progress.reset()
        self.tree.clear()

    def _cancel_scan(self) -> None:
        self.window.cancel_scan()

    def set_running(self, running: bool) -> None:
        self.scan_button.setEnabled(not running)
        self.cancel_button.setEnabled(running)

    def show_result(self, scan: ScanResult) -> None:
        self.tree.clear()
        self.summary_labels["files"].setText(str(scan.file_count))
        self.summary_labels["assets"].setText(str(len(scan.assets)))
        self.summary_labels["groups"].setText(str(len(scan.groups)))
        self.summary_labels["animations"].setText(str(scan.total_animations))
        missing_total = sum(len(g.missing_dependencies) for g in scan.groups)
        self.summary_labels["missing"].setText(str(missing_total))
        self.summary_labels["missing"].setStyleSheet(
            f"color: {COLOR_ERROR if missing_total else COLOR_OK};"
        )

        from ..pipeline.scan import group_output_name

        for group in scan.groups:
            mesh = group.reference_mesh.name if group.reference_mesh else "—"
            item = QTreeWidgetItem(
                [
                    group.display_name or "—",
                    mesh,
                    str(group.animation_count),
                    group.status.label,
                    group_output_name(group),
                ]
            )
            item.setForeground(3, QColor(_STATUS_COLORS.get(group.status, COLOR_MUTED)))
            self.tree.addTopLevelItem(item)
            for dependency in group.missing_dependencies:
                child = QTreeWidgetItem(["", "", "", "缺失", dependency])
                child.setForeground(3, QColor(COLOR_ERROR))
                item.addChild(child)
            item.setExpanded(bool(group.missing_dependencies))

        self.warning_label.setText("\n".join(scan.warnings) if scan.warnings else "")


# --------------------------------------------------------------------------- #
# 3. Selection
# --------------------------------------------------------------------------- #


class SelectPage(WizardPage):
    title = "选择转换内容"
    subtitle = "选择一个骨架组和参考网格，软件会自动勾选兼容动画，也可以手动取消部分动作。"

    def __init__(self, window: MainWindow, parent=None) -> None:
        super().__init__(window, parent)

        controls = QHBoxLayout()
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("过滤动画名称…")
        self.filter_edit.textChanged.connect(self._apply_filter)
        controls.addWidget(QLabel("筛选"))
        controls.addWidget(self.filter_edit, 1)

        self.all_button = QPushButton("全选")
        self.none_button = QPushButton("全不选")
        self.loop_button = QPushButton("仅循环动作")
        for button in (self.all_button, self.none_button, self.loop_button):
            button.setFixedWidth(96)
            controls.addWidget(button)
        self.all_button.clicked.connect(lambda: self._set_all(True))
        self.none_button.clicked.connect(lambda: self._set_all(False))
        self.loop_button.clicked.connect(self._select_looping)
        self.body.addLayout(controls)

        splitter = QSplitter(Qt.Horizontal)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(muted("骨架组（勾选要转换的组）"))
        self.group_list = QTreeWidget()
        self.group_list.setHeaderLabels(["骨架组", "网格", "动画", "状态"])
        self.group_list.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.group_list.setRootIsDecorated(False)
        self.group_list.itemChanged.connect(self._on_group_changed)
        self.group_list.currentItemChanged.connect(self._on_group_selected)
        left_layout.addWidget(self.group_list, 1)
        splitter.addWidget(left)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)

        mesh_row = QHBoxLayout()
        mesh_row.addWidget(QLabel("参考网格"))
        self.mesh_combo = QComboBox()
        self.mesh_combo.currentIndexChanged.connect(self._on_mesh_changed)
        mesh_row.addWidget(self.mesh_combo, 1)
        right_layout.addLayout(mesh_row)

        right_layout.addWidget(muted("动画列表（取消勾选可排除动作）"))
        self.animation_list = QListWidget()
        self.animation_list.setSelectionMode(QAbstractItemView.NoSelection)
        self.animation_list.itemChanged.connect(self._on_animation_changed)
        right_layout.addWidget(self.animation_list, 1)
        splitter.addWidget(right)

        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)
        self.body.addWidget(splitter, 1)

        self.summary = muted("")
        self.body.addWidget(self.summary)

    # ------------------------------------------------------------------ hooks

    def on_enter(self) -> None:
        self._populate()
        self._update_summary()

    def is_complete(self) -> bool:
        return bool(self._checked_groups())

    # --------------------------------------------------------------- populate

    def _populate(self) -> None:
        scan = self.window.scan
        self.group_list.blockSignals(True)
        self.group_list.clear()
        self.animation_list.blockSignals(True)
        self.animation_list.clear()
        self.animation_list.blockSignals(False)
        if scan is None:
            self.group_list.blockSignals(False)
            return


        for index, group in enumerate(scan.groups):
            selectable = bool(group.meshes) and group.status is not GroupStatus.MISSING_DEPS
            item = QTreeWidgetItem(
                [
                    group.display_name or "—",
                    str(len(group.meshes)),
                    str(group.animation_count),
                    group.status.label,
                ]
            )
            item.setData(0, Qt.UserRole, index)
            item.setForeground(3, QColor(_STATUS_COLORS.get(group.status, COLOR_MUTED)))
            if selectable:
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setCheckState(0, Qt.Checked)
            else:
                item.setFlags(item.flags() & ~Qt.ItemIsUserCheckable)
                item.setToolTip(
                    3,
                    "缺失依赖：" + ", ".join(group.missing_dependencies[:5])
                    if group.missing_dependencies
                    else "该组没有参考网格",
                )
            self.group_list.addTopLevelItem(item)

        self.group_list.blockSignals(False)
        if self.group_list.topLevelItemCount():
            self.group_list.setCurrentItem(self.group_list.topLevelItem(0))

    def _checked_groups(self) -> list[int]:
        out: list[int] = []
        for row in range(self.group_list.topLevelItemCount()):
            item = self.group_list.topLevelItem(row)
            if item.flags() & Qt.ItemIsUserCheckable and item.checkState(0) == Qt.Checked:
                out.append(item.data(0, Qt.UserRole))
        return out

    # ----------------------------------------------------------------- events

    def _on_group_changed(self, item: QTreeWidgetItem, _column: int) -> None:
        if item is self.group_list.currentItem():
            self._load_group(item)
        self._update_summary()

    def _on_group_selected(self, current: QTreeWidgetItem, _previous) -> None:
        if current is not None:
            self._load_group(current)

    def _load_group(self, item: QTreeWidgetItem) -> None:
        scan = self.window.scan
        if scan is None:
            return
        index = item.data(0, Qt.UserRole)
        if index is None or not (0 <= index < len(scan.groups)):
            return
        group = scan.groups[index]

        self.mesh_combo.blockSignals(True)
        self.mesh_combo.clear()
        for mesh in group.meshes:
            label = f"{mesh.name}"
            if mesh.num_frames:
                label += f"  ({mesh.num_frames} 帧)"
            self.mesh_combo.addItem(label, mesh.package_path)
        self.mesh_combo.blockSignals(False)

        from ..unity.manifest import infer_loop

        self.animation_list.blockSignals(True)
        self.animation_list.clear()
        for animation in group.animations:
            label = animation.name
            if animation.duration:
                label += f"   {animation.duration:.2f}s"
            if animation.num_frames:
                label += f" / {animation.num_frames} 帧"
            if infer_loop(animation.name):
                label += "   [循环]"
            entry = QListWidgetItem(label)
            entry.setFlags(entry.flags() | Qt.ItemIsUserCheckable)
            entry.setCheckState(0, Qt.Checked)
            entry.setData(Qt.UserRole, animation.package_path)
            self.animation_list.addItem(entry)
        self.animation_list.blockSignals(False)
        self._apply_filter()
        self._update_summary()

    def _on_mesh_changed(self, _index: int) -> None:
        self._update_summary()

    def _on_animation_changed(self, _item: QListWidgetItem) -> None:
        self._update_summary()

    def _apply_filter(self) -> None:
        needle = self.filter_edit.text().strip().lower()
        for row in range(self.animation_list.count()):
            item = self.animation_list.item(row)
            item.setHidden(bool(needle) and needle not in item.text().lower())

    def _set_all(self, checked: bool) -> None:
        self.animation_list.blockSignals(True)
        for row in range(self.animation_list.count()):
            item = self.animation_list.item(row)
            if not item.isHidden():
                item.setCheckState(0, Qt.Checked if checked else Qt.Unchecked)
        self.animation_list.blockSignals(False)
        self._update_summary()

    def _select_looping(self) -> None:
        from ..unity.manifest import infer_loop

        self.animation_list.blockSignals(True)
        for row in range(self.animation_list.count()):
            item = self.animation_list.item(row)
            package = item.data(Qt.UserRole) or ""
            name = package.rsplit("/", 1)[-1]
            item.setCheckState(0, Qt.Checked if infer_loop(name) else Qt.Unchecked)
        self.animation_list.blockSignals(False)
        self._update_summary()

    # ------------------------------------------------------------------ state

    def _update_summary(self) -> None:
        groups = self._checked_groups()
        animations = sum(
            1
            for row in range(self.animation_list.count())
            if self.animation_list.item(row).checkState(0) == Qt.Checked
        )
        self.summary.setText(
            f"已选择 {len(groups)} 个骨架组；当前组勾选 {animations} 个动画。"
        )
        self.window.refresh_navigation()

    def build_selection(self) -> list[SelectedGroup]:
        """Materialise the current UI state into pipeline objects."""
        scan = self.window.scan
        if scan is None:
            return []

        checked = set(self._checked_groups())
        selection: list[SelectedGroup] = []

        for index, group in enumerate(scan.groups):
            if index not in checked:
                continue

            # Animations checked in the currently displayed group.
            chosen_animations = list(group.animations)
            current = self.group_list.currentItem()
            if current is not None and current.data(0, Qt.UserRole) == index:
                packages = {
                    self.animation_list.item(row).data(Qt.UserRole)
                    for row in range(self.animation_list.count())
                    if self.animation_list.item(row).checkState(0) == Qt.Checked
                }
                chosen_animations = [
                    a for a in group.animations if a.package_path in packages
                ]

            mesh = group.reference_mesh
            if current is not None and current.data(0, Qt.UserRole) == index:
                package = self.mesh_combo.currentData()
                if package:
                    mesh = next(
                        (m for m in group.meshes if m.package_path == package), mesh
                    )

            selection.append(
                SelectedGroup(group=group, mesh=mesh, animations=chosen_animations)
            )
        return selection


# --------------------------------------------------------------------------- #
# 4. Export settings
# --------------------------------------------------------------------------- #


class SettingsPage(WizardPage):
    title = "设置导出"
    subtitle = "配置 Unity 导入类型、采样率、循环与 Root Motion 参数。"

    def __init__(self, window: MainWindow, parent=None) -> None:
        super().__init__(window, parent)

        box = QGroupBox("导入设置")
        form = QFormLayout(box)

        type_row = QHBoxLayout()
        self.humanoid_radio = QRadioButton("Humanoid")
        self.generic_radio = QRadioButton("Generic")
        self.generic_radio.setChecked(True)
        group = QButtonGroup(self)
        group.addButton(self.humanoid_radio)
        group.addButton(self.generic_radio)
        type_row.addWidget(self.humanoid_radio)
        type_row.addWidget(self.generic_radio)
        type_row.addStretch(1)
        form.addRow("导入类型", type_row)

        self.rate_spin = QSpinBox()
        self.rate_spin.setRange(1, 240)
        self.rate_spin.setValue(30)
        self.rate_spin.setSuffix("  fps")
        form.addRow("采样率", self.rate_spin)

        self.loop_combo = QComboBox()
        self.loop_combo.addItem("自动（按名称推断）", LoopMode.AUTO.value)
        self.loop_combo.addItem("全部循环", LoopMode.FORCE_LOOP.value)
        self.loop_combo.addItem("全部单次", LoopMode.FORCE_ONCE.value)
        form.addRow("循环", self.loop_combo)

        self.root_combo = QComboBox()
        self.root_combo.addItem("保留 Root Motion", RootMotionMode.KEEP.value)
        self.root_combo.addItem("锁定水平位移（原地）", RootMotionMode.LOCK_XZ.value)
        self.root_combo.addItem("锁定全部位移", RootMotionMode.LOCK_FULL.value)
        form.addRow("Root Motion", self.root_combo)

        self.scale_spin = QDoubleSpinBox()
        self.scale_spin.setRange(0.001, 1000.0)
        self.scale_spin.setDecimals(4)
        self.scale_spin.setValue(1.0)
        self.scale_spin.setSingleStep(0.01)
        form.addRow("导入缩放", self.scale_spin)

        self.materials_check = QCheckBox("保留材质槽（不还原 Unreal 材质图）")
        self.materials_check.setChecked(True)
        form.addRow("", self.materials_check)

        self.body.addWidget(box)

        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        font = QFont("Consolas")
        font.setStyleHint(QFont.Monospace)
        self.preview.setFont(font)
        self.body.addWidget(heading("输出结构预览", size=12))
        self.body.addWidget(self.preview, 1)

    def on_enter(self) -> None:
        self._refresh_preview()

    def is_complete(self) -> bool:
        return self.window.scan is not None and bool(
            self.window.checked_group_indices()
        )

    def collect(self):
        from ..models import ExportSettings

        return ExportSettings(
            import_type=ImportType.HUMANOID
            if self.humanoid_radio.isChecked()
            else ImportType.GENERIC,
            sampling_rate=int(self.rate_spin.value()),
            loop_mode=LoopMode(self.loop_combo.currentData()),
            root_motion=RootMotionMode(self.root_combo.currentData()),
            scale=float(self.scale_spin.value()),
            export_materials=self.materials_check.isChecked(),
        )

    def _refresh_preview(self) -> None:
        from ..pipeline.scan import group_output_name

        scan = self.window.scan
        lines = ["UnityExport/"]
        if scan is None:
            lines.append("  （先完成扫描）")
        else:
            checked = set(self.window.checked_group_indices())
            lines.append("  Characters/")
            for index, group in enumerate(scan.groups):
                if index not in checked:
                    continue
                name = group_output_name(group)
                lines.append(f"    {name}/")
                lines.append("      Mesh/")
                if group.reference_mesh:
                    lines.append(f"        {group.reference_mesh.name}.fbx")
                lines.append("      Animations/")
                for animation in group.animations[:4]:
                    lines.append(f"        {animation.name}.fbx")
                if len(group.animations) > 4:
                    lines.append(f"        … 共 {len(group.animations)} 个")
                lines.append("      profile.json")
        lines.append("  Editor/")
        lines.append("    UnrealResourceImporter.cs")
        lines.append("    UnrealImportTests.cs")
        lines.append("  Reports/")
        lines.append("    export_report.json")
        lines.append("  manifest.json")
        self.preview.setPlainText("\n".join(lines))


# --------------------------------------------------------------------------- #
# 5. Run
# --------------------------------------------------------------------------- #


class RunPage(WizardPage):
    title = "开始转换"
    subtitle = "转换过程写入独立工作目录，原目录只读。"

    def __init__(self, window: MainWindow, parent=None) -> None:
        super().__init__(window, parent)

        controls = QHBoxLayout()
        self.start_button = QPushButton("开始转换")
        self.start_button.setMinimumWidth(120)
        self.start_button.clicked.connect(self.window.start_export)
        self.cancel_button = QPushButton("取消")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.window.cancel_export)
        self.retry_button = QPushButton("重试失败项")
        self.retry_button.setEnabled(False)
        self.retry_button.clicked.connect(self.window.retry_failed)
        self.open_button = QPushButton("打开输出目录")
        self.open_button.setEnabled(False)
        self.open_button.clicked.connect(self.window.open_output)

        for button in (self.start_button, self.cancel_button, self.retry_button, self.open_button):
            controls.addWidget(button)
        controls.addStretch(1)
        self.body.addLayout(controls)

        self.progress = ProgressPanel()
        self.body.addWidget(self.progress)

        self.summary_label = muted("尚未开始。")
        self.body.addWidget(self.summary_label)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["状态", "类型", "名称", "说明"])
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.verticalHeader().setVisible(False)
        self.body.addWidget(self.table, 1)

        self.body.addWidget(heading("日志", size=12))
        self.log_view = LogView()
        self.log_view.setMinimumHeight(150)
        self.body.addWidget(self.log_view, 1)

    # ------------------------------------------------------------------ hooks

    def on_enter(self) -> None:
        self.refresh_plan()

    def refresh_plan(self) -> None:
        plan = self.window.ensure_plan()
        self.table.setRowCount(0)
        if plan is None:
            self.summary_label.setText("尚无转换计划，请先完成前面的步骤。")
            self.start_button.setEnabled(False)
            return
        self.start_button.setEnabled(True)
        self.open_button.setEnabled(True)
        self.summary_label.setText(
            f"计划：{len(plan.groups)} 个骨架组，"
            f"{sum(len(g.mesh_requests) for g in plan.groups)} 个网格，"
            f"{sum(len(g.animation_requests) for g in plan.groups)} 个动画。"
        )
        for request in plan.all_requests:
            self._append_row(request.kind.label, request.display_name, "")

    def _append_row(self, kind: str, name: str, detail: str) -> int:
        row = self.table.rowCount()
        self.table.insertRow(row)
        status = QTableWidgetItem(ItemStatus.PENDING.label)
        self.table.setItem(row, 0, status)
        self.table.setItem(row, 1, QTableWidgetItem(kind))
        self.table.setItem(row, 2, QTableWidgetItem(name))
        self.table.setItem(row, 3, QTableWidgetItem(detail))
        return row

    # ----------------------------------------------------------------- updates

    def set_running(self, running: bool) -> None:
        self.start_button.setEnabled(not running)
        self.cancel_button.setEnabled(running)
        if running:
            self.retry_button.setEnabled(False)

    def update_item(self, outcome: ExportOutcome) -> None:
        for row in range(self.table.rowCount()):
            name_item = self.table.item(row, 2)
            if name_item is None or name_item.text() != outcome.request.display_name:
                continue
            if self.table.item(row, 1).text() != outcome.request.kind.label:
                continue
            status_item = self.table.item(row, 0)
            status_item.setText(outcome.status.label)
            color = {
                ItemStatus.OK: COLOR_OK,
                ItemStatus.FAILED: COLOR_ERROR,
                ItemStatus.RUNNING: COLOR_WARN,
            }.get(outcome.status, COLOR_MUTED)
            status_item.setForeground(QColor(color))
            self.table.item(row, 3).setText(
                outcome.error or outcome.detail or outcome.output_path or ""
            )
            break

    def show_report(self, report) -> None:
        self.summary_label.setText(
            f"{report.summary_line()}    用时 {report.duration_s:.1f}s    "
            f"输出：{report.unity_export_root}"
        )
        self.retry_button.setEnabled(bool(report.retryable))
        self.open_button.setEnabled(True)

    def append_log(self, level: int, text: str) -> None:
        """Render one log record.

        This runs from a Qt slot, so an escaping exception aborts the whole
        process (PyQt5 calls ``qFatal``). Losing a log line is always
        preferable to losing the session, so failures are swallowed -- the
        record has already reached the console and file handlers anyway.
        """
        with contextlib.suppress(Exception):
            self.log_view.append_line(level, text)


__all__ = [
    "DirectoryPage",
    "RunPage",
    "ScanPage",
    "SelectPage",
    "SettingsPage",
    "WizardPage",
]

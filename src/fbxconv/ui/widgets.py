"""Reusable Qt widgets for the conversion wizard."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from PyQt5.QtCore import QSize, Qt, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QTextCharFormat, QTextCursor
from PyQt5.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

# --------------------------------------------------------------------------- #
# Palette
# --------------------------------------------------------------------------- #

COLOR_OK = "#1b7f3b"
COLOR_WARN = "#b26a00"
COLOR_ERROR = "#b3261e"
COLOR_MUTED = "#6b7280"
COLOR_ACCENT = "#2563eb"

_LEVEL_COLORS = {
    logging.DEBUG: COLOR_MUTED,
    logging.INFO: "#111827",
    logging.WARNING: COLOR_WARN,
    logging.ERROR: COLOR_ERROR,
    logging.CRITICAL: COLOR_ERROR,
}


# --------------------------------------------------------------------------- #
# Path selection
# --------------------------------------------------------------------------- #


class PathPicker(QWidget):
    """A line edit plus a Browse button.

    ``mode`` is one of ``dir``, ``open`` or ``save``.
    """

    changed = pyqtSignal(str)

    def __init__(
        self,
        *,
        mode: str = "dir",
        placeholder: str = "",
        file_filter: str = "All files (*)",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._mode = mode
        self._filter = file_filter

        self.edit = QLineEdit(self)
        self.edit.setPlaceholderText(placeholder)
        self.edit.textChanged.connect(self.changed.emit)

        self.button = QPushButton("浏览…", self)
        self.button.setFixedWidth(84)
        self.button.clicked.connect(self._browse)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(self.edit, 1)
        layout.addWidget(self.button, 0)

    # ------------------------------------------------------------------ api

    def path(self) -> Path | None:
        text = self.edit.text().strip()
        return Path(text) if text else None

    def set_path(self, value: str | os.PathLike[str] | None) -> None:
        self.edit.setText(str(value) if value else "")

    def text(self) -> str:
        return self.edit.text().strip()

    def setEnabled(self, enabled: bool) -> None:
        super().setEnabled(enabled)
        self.edit.setEnabled(enabled)
        self.button.setEnabled(enabled)

    # -------------------------------------------------------------- internal

    def _browse(self) -> None:
        current = self.edit.text().strip()
        start = current if current and Path(current).exists() else str(Path.home())
        if self._mode == "dir":
            chosen = QFileDialog.getExistingDirectory(self, "选择目录", start)
        elif self._mode == "open":
            chosen, _ = QFileDialog.getOpenFileName(self, "选择文件", start, self._filter)
        else:
            chosen, _ = QFileDialog.getSaveFileName(self, "选择保存位置", start, self._filter)
        if chosen:
            self.edit.setText(chosen)


# --------------------------------------------------------------------------- #
# Log view
# --------------------------------------------------------------------------- #


class LogView(QPlainTextEdit):
    """Read-only, colour-coded log pane.

    ``QPlainTextEdit`` does **not** have ``setTextColor`` -- that belongs to
    ``QTextEdit``. Colour therefore has to travel with each insertion, via a
    ``QTextCharFormat`` handed to the cursor.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        self.setMaximumBlockCount(5000)
        self.setLineWrapMode(QPlainTextEdit.NoWrap)
        font = QFont("Consolas")
        font.setStyleHint(QFont.Monospace)
        font.setPointSize(9)
        self.setFont(font)
        self.setPlaceholderText("转换日志将显示在这里…")

    def _append(self, text: str, color: str) -> None:
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(color))
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.End)
        cursor.insertText(f"{text}\n", fmt)
        self.setTextCursor(cursor)
        self.ensureCursorVisible()

    def append_line(self, level: int, text: str) -> None:
        self._append(text, _LEVEL_COLORS.get(level, "#111827"))

    def append_plain(self, text: str, color: str | None = None) -> None:
        self._append(text, color or COLOR_MUTED)

    def clear_log(self) -> None:
        self.clear()


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


class Badge(QLabel):
    """A small coloured status chip."""

    def __init__(self, text: str = "", color: str = COLOR_MUTED, parent=None) -> None:
        super().__init__(text, parent)
        self.setAlignment(Qt.AlignCenter)
        self.setMinimumWidth(64)
        self.set_status(text, color)

    def set_status(self, text: str, color: str = COLOR_MUTED) -> None:
        self.setText(text)
        self.setStyleSheet(
            f"QLabel {{ color: {color}; border: 1px solid {color};"
            f" border-radius: 8px; padding: 1px 8px; font-size: 11px; }}"
        )


def heading(text: str, *, size: int = 13) -> QLabel:
    label = QLabel(text)
    font = label.font()
    font.setPointSize(size)
    font.setBold(True)
    label.setFont(font)
    return label


def muted(text: str) -> QLabel:
    label = QLabel(text)
    label.setStyleSheet(f"color: {COLOR_MUTED};")
    label.setWordWrap(True)
    return label


def separator() -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.HLine)
    line.setFrameShadow(QFrame.Sunken)
    return line


def open_in_file_manager(path: str | os.PathLike[str]) -> bool:
    """Reveal a directory in Explorer / Finder / xdg-open."""
    target = Path(path)
    if not target.exists():
        return False
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(target))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(target)])
        else:
            subprocess.Popen(["xdg-open", str(target)])
    except OSError:
        return False
    return True


# --------------------------------------------------------------------------- #
# Step sidebar
# --------------------------------------------------------------------------- #


class StepList(QListWidget):
    """Non-interactive list of wizard steps with a numbered marker."""

    def __init__(self, titles: Sequence[str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setSelectionMode(QListWidget.NoSelection)
        self.setFocusPolicy(Qt.NoFocus)
        self.setFixedWidth(210)
        self._items: list[QListWidgetItem] = []
        for index, title in enumerate(titles, start=1):
            item = QListWidgetItem(f"     {index}.  {title}")
            item.setSizeHint(QSize(200, 40))
            self.addItem(item)
            self._items.append(item)
        self.setStyleSheet(
            "QListWidget { border: none; background: #f8fafc; }"
            "QListWidget::item { padding: 10px 6px; color: #6b7280; }"
        )

    def set_current(self, index: int) -> None:
        for position, item in enumerate(self._items):
            font = item.font()
            font.setBold(position == index)
            item.setFont(font)
            if position < index:
                marker = "  ✓  "
            elif position == index:
                marker = "  ▶  "
            else:
                marker = "     "
            title = item.text().split(".", 1)[1].strip()
            prefix = f"{position + 1}."
            item.setText(f"{marker}{prefix}  {title}")
            item.setForeground(
                QColor(COLOR_ACCENT if position == index else COLOR_MUTED)
            )


class ProgressPanel(QWidget):
    """A progress bar with an optional indeterminate mode and caption."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.bar = QProgressBar(self)
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self.bar.setTextVisible(True)
        self.caption = QLabel("就绪")
        self.caption.setStyleSheet(f"color: {COLOR_MUTED};")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(self.caption)
        layout.addWidget(self.bar)

    def set_caption(self, text: str) -> None:
        self.caption.setText(text)

    def set_fraction(self, fraction: float | None) -> None:
        if fraction is None:
            self.bar.setRange(0, 0)  # indeterminate
        else:
            self.bar.setRange(0, 100)
            self.bar.setValue(max(0, min(100, round(fraction * 100))))

    def reset(self) -> None:
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self.caption.setText("就绪")


__all__ = [
    "COLOR_ACCENT",
    "COLOR_ERROR",
    "COLOR_MUTED",
    "COLOR_OK",
    "COLOR_WARN",
    "Badge",
    "LogView",
    "PathPicker",
    "ProgressPanel",
    "StepList",
    "heading",
    "muted",
    "open_in_file_manager",
    "separator",
]

"""Persistent application configuration (spec section 1).

The config is stored as JSON under the user's roaming profile so that the
directory choices, import type and recent paths survive restarts.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import APP_NAME
from .errors import ConfigError
from .logutil import get_logger
from .models import ExportSettings

_log = get_logger("config")

MAX_RECENT = 10


def default_config_dir() -> Path:
    """Per-user configuration directory."""
    if os.name == "nt":
        base = os.environ.get("APPDATA")
        if base:
            return Path(base) / APP_NAME
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / APP_NAME
    return Path.home() / ".config" / APP_NAME


def default_config_path() -> Path:
    return default_config_dir() / "config.json"


@dataclass
class AppConfig:
    """Everything the user configures in step 1 of the wizard, plus history."""

    unreal_editor: Path | None = None
    """Full path to ``UnrealEditor-Cmd.exe`` (or ``UnrealEditor.exe``)."""

    content_root: Path | None = None
    """The Unreal resource directory (a ``Content`` folder or a project folder)."""

    output_root: Path | None = None
    """Where ``UnityExport/`` is written."""

    unity_project: Path | None = None
    """Optional Unity/Tuanjie project to install the importer into."""

    unity_editor: Path | None = None
    """Optional path to ``Unity.exe``/``Tuanjie.exe`` for the batch import step."""

    settings: ExportSettings = field(default_factory=ExportSettings)
    link_mode: str = "junction"
    """How an independent conversion project exposes the source content."""

    working_root: Path | None = None
    """Scratch area; defaults to ``<output_root>/.fbxconv``."""

    recent_content_roots: list[Path] = field(default_factory=list)
    recent_output_roots: list[Path] = field(default_factory=list)
    recent_unreal_editors: list[Path] = field(default_factory=list)

    # ---------------------------------------------------------------- derived

    @property
    def export_root(self) -> Path | None:
        """The ``UnityExport`` directory inside the output root."""
        if self.output_root is None:
            return None
        return self.output_root / "UnityExport"

    @property
    def effective_working_root(self) -> Path | None:
        if self.working_root is not None:
            return self.working_root
        if self.output_root is None:
            return None
        return self.output_root / ".fbxconv"

    # ------------------------------------------------------------- validation

    def validate(self, *, require_editor: bool = True) -> None:
        """Raise :class:`ConfigError` describing the first missing requirement."""
        if require_editor:
            if self.unreal_editor is None:
                raise ConfigError("未指定 Unreal Editor 路径。")
            if not Path(self.unreal_editor).is_file():
                raise ConfigError(f"Unreal Editor 不存在：{self.unreal_editor}")
        if self.content_root is None:
            raise ConfigError("未指定 Unreal 资源目录。")
        if not Path(self.content_root).is_dir():
            raise ConfigError(f"Unreal 资源目录不存在：{self.content_root}")
        if self.output_root is None:
            raise ConfigError("未指定 Unity 输出目录。")

    # ------------------------------------------------------------ persistence

    def to_payload(self) -> dict[str, Any]:
        def _p(value: Path | None) -> str | None:
            return str(value) if value is not None else None

        return {
            "schema_version": 1,
            "unreal_editor": _p(self.unreal_editor),
            "content_root": _p(self.content_root),
            "output_root": _p(self.output_root),
            "unity_project": _p(self.unity_project),
            "unity_editor": _p(self.unity_editor),
            "working_root": _p(self.working_root),
            "link_mode": self.link_mode,
            "settings": self.settings.to_payload(),
            "recent_content_roots": [str(p) for p in self.recent_content_roots],
            "recent_output_roots": [str(p) for p in self.recent_output_roots],
            "recent_unreal_editors": [str(p) for p in self.recent_unreal_editors],
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> AppConfig:
        def _path(key: str) -> Path | None:
            raw = payload.get(key)
            return Path(raw) if raw else None

        def _paths(key: str) -> list[Path]:
            return [Path(p) for p in payload.get(key, []) or [] if p]

        settings_payload = payload.get("settings") or {}
        return cls(
            unreal_editor=_path("unreal_editor"),
            content_root=_path("content_root"),
            output_root=_path("output_root"),
            unity_project=_path("unity_project"),
            unity_editor=_path("unity_editor"),
            working_root=_path("working_root"),
            link_mode=str(payload.get("link_mode") or "junction"),
            settings=ExportSettings.from_payload(settings_payload),
            recent_content_roots=_paths("recent_content_roots"),
            recent_output_roots=_paths("recent_output_roots"),
            recent_unreal_editors=_paths("recent_unreal_editors"),
        )

    def save(self, path: Path | None = None) -> Path:
        target = path or default_config_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(self.to_payload(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(target)
        _log.debug("configuration saved to %s", target)
        return target

    @classmethod
    def load(cls, path: Path | None = None) -> AppConfig:
        target = path or default_config_path()
        if not target.is_file():
            return cls()
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            _log.warning("ignoring unreadable config %s: %s", target, exc)
            return cls()
        if not isinstance(payload, dict):
            _log.warning("ignoring malformed config %s", target)
            return cls()
        try:
            return cls.from_payload(payload)
        except (TypeError, ValueError) as exc:
            _log.warning("ignoring malformed config %s: %s", target, exc)
            return cls()

    # ------------------------------------------------------------- mutators

    def remember_content_root(self, path: Path) -> None:
        self.recent_content_roots = _push_unique(self.recent_content_roots, path)

    def remember_output_root(self, path: Path) -> None:
        self.recent_output_roots = _push_unique(self.recent_output_roots, path)

    def remember_unreal_editor(self, path: Path) -> None:
        self.recent_unreal_editors = _push_unique(self.recent_unreal_editors, path)


def _push_unique(items: Iterable[Path], value: Path) -> list[Path]:
    value = Path(value)
    out = [p for p in items if Path(p) != value]
    out.insert(0, value)
    return out[:MAX_RECENT]


__all__ = ["AppConfig", "default_config_dir", "default_config_path"]

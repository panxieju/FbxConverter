"""Export reporting (spec section 5).

Produces ``Reports/export_report.json``: one record per requested FBX, with a
concrete reason and a retry flag for every failure.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import __version__
from .models import ExportOutcome, ExportSettings, ItemStatus


@dataclass
class ExportReport:
    """Outcome of one conversion run."""

    source_content_root: str = ""
    project_file: str | None = None
    conversion_project: str | None = None
    engine_version: str = ""
    unity_export_root: str = ""
    settings: ExportSettings = field(default_factory=ExportSettings)
    outcomes: list[ExportOutcome] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""
    duration_s: float = 0.0

    @property
    def succeeded(self) -> list[ExportOutcome]:
        return [o for o in self.outcomes if o.status is ItemStatus.OK]

    @property
    def failed(self) -> list[ExportOutcome]:
        return [o for o in self.outcomes if o.status is ItemStatus.FAILED]

    @property
    def retryable(self) -> list[ExportOutcome]:
        return [o for o in self.failed if o.retryable]

    @property
    def success_count(self) -> int:
        return len(self.succeeded)

    @property
    def failure_count(self) -> int:
        return len(self.failed)

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "generator": f"fbxconv {__version__}",
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": round(self.duration_s, 3),
            "summary": {
                "total": len(self.outcomes),
                "succeeded": self.success_count,
                "failed": self.failure_count,
                "retryable": len(self.retryable),
            },
            "source": {
                "content_root": self.source_content_root,
                "project_file": self.project_file,
                "conversion_project": self.conversion_project,
                "engine_version": self.engine_version,
            },
            "unity_export_root": self.unity_export_root,
            "settings": self.settings.to_payload(),
            "messages": list(self.messages),
            "items": [o.to_payload() for o in self.outcomes],
        }

    def save(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.to_payload(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return target

    def summary_line(self) -> str:
        return (
            f"共 {len(self.outcomes)} 项：成功 {self.success_count}，"
            f"失败 {self.failure_count}（可重试 {len(self.retryable)}）"
        )

    @staticmethod
    def timestamp() -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%S")


__all__ = ["ExportReport"]

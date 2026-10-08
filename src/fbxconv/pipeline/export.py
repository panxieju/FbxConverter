"""Export orchestration (spec sections 3, 4.5 and 5).

Turns a validated selection into one batched Unreal run, then writes the
Unity-facing artefacts: per-character ``profile.json``, the top-level
``manifest.json``, ``Reports/export_report.json`` and the Editor scripts.
"""

from __future__ import annotations

import posixpath
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..errors import ConfigError, FbxConvError
from ..logutil import get_logger
from ..models import (
    AssetKind,
    AssetRecord,
    ConversionPlan,
    ExportOutcome,
    ExportRequest,
    ExportSettings,
    GroupPlan,
    ItemStatus,
    ScanResult,
    SelectedGroup,
)
from ..report import ExportReport
from ..unity.manifest import (
    build_manifest_payload,
    build_profile_payload,
    install_unity_support,
    write_json,
)
from ..unreal.locator import UnrealInstall
from ..unreal.paths import package_to_object, sanitise_filename, unique_posix
from ..unreal.runner import UnrealRunner

_log = get_logger("pipeline.export")

MESH_DIR = "Mesh"
ANIM_DIR = "Animations"
CHARACTERS_DIR = "Characters"
REPORTS_DIR = "Reports"
REPORT_NAME = "export_report.json"

ProgressFn = Callable[[str, float | None], None]
EventFn = Callable[[dict[str, Any]], None]
ItemFn = Callable[[ExportOutcome], None]


# --------------------------------------------------------------------------- #
# Planning
# --------------------------------------------------------------------------- #


def build_plan(
    scan: ScanResult,
    selection: Sequence[SelectedGroup],
    settings: ExportSettings,
    *,
    export_root: Path,
    work_root: Path,
    unity_project: Path | None = None,
) -> ConversionPlan:
    """Lay out every FBX that needs producing (spec section 5)."""
    if not selection:
        raise ConfigError("没有选择任何要转换的骨架组。")

    groups: list[GroupPlan] = []
    used_group_names: set[str] = set()

    for chosen in selection:
        group = chosen.group
        base_name = sanitise_filename(
            group.output_name or group.display_name, fallback="Group"
        )
        output_name = base_name
        index = 2
        while output_name in used_group_names:
            output_name = f"{base_name}_{index}"
            index += 1
        used_group_names.add(output_name)

        plan_group = GroupPlan(group=group, output_name=output_name)
        taken: set[str] = set()

        # --- reference mesh -------------------------------------------------
        mesh = chosen.reference_mesh
        if mesh is not None:
            mesh_name = sanitise_filename(mesh.name or mesh.display_name, fallback="Mesh")
            relative = posixpath.join(
                CHARACTERS_DIR,
                output_name,
                MESH_DIR,
                unique_posix(f"{mesh_name}.fbx", taken),
            )
            plan_group.mesh_requests.append(
                ExportRequest(
                    kind=AssetKind.SKELETAL_MESH,
                    package_path=mesh.package_path,
                    output_rel=relative,
                    display_name=mesh.name or mesh.display_name,
                )
            )

        # --- animations ------------------------------------------------------
        taken.clear()
        animations: Iterable[AssetRecord] = chosen.animations or group.animations
        for animation in animations:
            name = sanitise_filename(
                animation.name or animation.display_name, fallback="Animation"
            )
            relative = posixpath.join(
                CHARACTERS_DIR,
                output_name,
                ANIM_DIR,
                unique_posix(f"{name}.fbx", taken),
            )
            plan_group.animation_requests.append(
                ExportRequest(
                    kind=AssetKind.ANIM_SEQUENCE,
                    package_path=animation.package_path,
                    output_rel=relative,
                    display_name=animation.name or animation.display_name,
                )
            )

        if not plan_group.all_requests:
            _log.warning("跳过 %s：没有可导出的网格或动画", group.display_name)
            continue

        groups.append(plan_group)

    if not groups:
        raise ConfigError("所选骨架组中没有可导出的网格或动画。")

    plan = ConversionPlan(
        content_root=scan.content_root,
        mount_point=scan.mount_point,
        project_file=scan.project_file,
        conversion_project=scan.conversion_project,
        unreal_project=scan.unreal_project or scan.project_file,
        export_root=Path(export_root),
        work_root=Path(work_root),
        settings=settings,
        groups=groups,
        engine_version=scan.engine_version,
        unity_project=Path(unity_project) if unity_project else None,
    )

    _log.info(
        "转换计划：%d 组，%d 个网格，%d 个动画",
        len(plan.groups),
        sum(len(g.mesh_requests) for g in plan.groups),
        sum(len(g.animation_requests) for g in plan.groups),
    )
    return plan


# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #


@dataclass
class ExportSession:
    """Runs a :class:`ConversionPlan` through Unreal and writes the artefacts."""

    install: UnrealInstall
    plan: ConversionPlan
    on_progress: ProgressFn | None = None
    on_event: EventFn | None = None
    on_item: ItemFn | None = None
    cancel_event: threading.Event | None = None
    null_rhi: bool = False
    """Must stay ``False``: ``USkeletalMeshExporterFBX`` builds a skinned
    component through MeshMergeUtilities, which asserts and crashes the editor
    under ``-nullrhi``. The scan phase has no such dependency and can use it."""

    write_unity_support: bool = True

    outcomes: list[ExportOutcome] = field(default_factory=list, init=False)

    # ------------------------------------------------------------------ helpers

    def _progress(self, message: str, fraction: float | None = None) -> None:
        _log.info("%s", message)
        if self.on_progress is not None:
            self.on_progress(message, fraction)

    def _emit_item(self, outcome: ExportOutcome) -> None:
        if self.on_item is not None:
            self.on_item(outcome)

    @property
    def export_root(self) -> Path:
        return self.plan.export_root

    # --------------------------------------------------------------------- run

    def run(
        self,
        requests: Sequence[ExportRequest] | None = None,
        *,
        previous: Sequence[ExportOutcome] | None = None,
    ) -> ExportReport:
        """Export ``requests`` (default: the whole plan) and write all artefacts.

        ``previous`` lets a retry keep the outcomes of items it is not re-running.
        """
        started = time.monotonic()
        report = ExportReport(
            source_content_root=str(self.plan.content_root),
            project_file=str(self.plan.project_file) if self.plan.project_file else None,
            conversion_project=str(self.plan.conversion_project)
            if self.plan.conversion_project
            else None,
            engine_version=self.plan.engine_version,
            unity_export_root=str(self.export_root),
            settings=self.plan.settings,
            started_at=ExportReport.timestamp(),
        )

        targets = list(requests) if requests is not None else self.plan.all_requests
        if not targets:
            report.finished_at = ExportReport.timestamp()
            report.duration_s = time.monotonic() - started
            self.outcomes = list(previous or [])
            report.outcomes = self.outcomes
            return report

        self.export_root.mkdir(parents=True, exist_ok=True)

        for plan_group in self.plan.groups:
            (self.export_root / CHARACTERS_DIR / plan_group.output_name).mkdir(
                parents=True, exist_ok=True
            )

        payload = [
            {
                "kind": request.kind.value,
                "package_path": request.package_path,
                "object_path": package_to_object(request.package_path),
                "output_rel": request.output_rel,
                "output_path": str(self.export_root / Path(request.output_rel)),
                "display_name": request.display_name,
            }
            for request in targets
        ]

        self._progress(f"正在通过 Unreal 导出 {len(payload)} 个 FBX…")

        batch_outcomes = self._run_unreal(payload, targets)

        # Merge with previous outcomes for retries.
        merged: dict[str, ExportOutcome] = {}
        for outcome in list(previous or []):
            merged[outcome.request.output_rel] = outcome
        for outcome in batch_outcomes:
            merged[outcome.request.output_rel] = outcome
        self.outcomes = [merged[key] for key in sorted(merged)]
        report.outcomes = self.outcomes

        report.finished_at = ExportReport.timestamp()
        report.duration_s = time.monotonic() - started

        self._write_artefacts(report)
        self._progress(report.summary_line(), 1.0)
        return report

    # --------------------------------------------------------------- internals

    def _run_unreal(
        self, payload: list[dict[str, Any]], targets: Sequence[ExportRequest]
    ) -> list[ExportOutcome]:
        runner = UnrealRunner(
            self.install,
            project_file=self.plan.unreal_project or self.plan.project_file,
            work_root=self.plan.work_root,
            null_rhi=self.null_rhi,
        )

        try:
            result = runner.run_script(
                "fbxconv_export.py",
                {
                    "mount_point": self.plan.mount_point,
                    "settings": self.plan.settings.to_payload(),
                    "exports": payload,
                },
                label="export",
                on_event=self._forward_event,
                cancel_event=self.cancel_event,
                timeout=7200.0,
            )
        except FbxConvError as exc:
            # The whole batch failed; every target gets the same reason.
            # Use the short message: str(exc) appends a whole log tail.
            message = getattr(exc, "message", None) or str(exc)
            outcomes = [
                ExportOutcome(
                    request=request,
                    status=ItemStatus.FAILED,
                    error=message,
                    retryable=True,
                )
                for request in targets
            ]
            for outcome in outcomes:
                self._emit_item(outcome)
            return outcomes

        data = result.data
        by_key: dict[tuple[str, str], dict[str, Any]] = {}
        for item in data.get("results", []) or []:
            key = (str(item.get("package_path", "")), str(item.get("output_path", "")))
            by_key[key] = item

        outcomes: list[ExportOutcome] = []
        for request in targets:
            key = (request.package_path, str(self.export_root / Path(request.output_rel)))
            item = by_key.get(key)
            if item is None:
                outcome = ExportOutcome(
                    request=request,
                    status=ItemStatus.FAILED,
                    error="Unreal 未返回该项的结果。",
                    retryable=True,
                )
            elif item.get("ok"):
                outcome = ExportOutcome(
                    request=request,
                    status=ItemStatus.OK,
                    output_path=item.get("output_path"),
                    duration_s=float(item.get("elapsed") or 0.0),
                    detail=f"{int(item.get('size') or 0)} 字节",
                )
            else:
                outcome = ExportOutcome(
                    request=request,
                    status=ItemStatus.FAILED,
                    output_path=None,
                    error=str(item.get("error") or "未知错误"),
                    retryable=True,
                )
            outcomes.append(outcome)
            self._emit_item(outcome)
        return outcomes

    def _forward_event(self, event: dict[str, Any]) -> None:
        if event.get("type") == "log" and event.get("level") == "error":
            _log.error("%s", event.get("message"))
        if self.on_event is not None:
            self.on_event(event)

    def _write_artefacts(self, report: ExportReport) -> None:
        profile_paths: dict[str, str] = {}

        for plan_group in self.plan.groups:
            payload = build_profile_payload(plan_group, self.plan.settings)
            relative = posixpath.join(
                CHARACTERS_DIR, plan_group.output_name, "profile.json"
            )
            write_json(self.export_root / Path(relative), payload)
            profile_paths[plan_group.output_name] = relative
            _log.debug("写入 %s", relative)

        report_path = posixpath.join(REPORTS_DIR, REPORT_NAME)
        report.save(self.export_root / Path(report_path))

        manifest = build_manifest_payload(
            self.plan,
            profile_paths=profile_paths,
            generated_at=report.finished_at or ExportReport.timestamp(),
            report_path=report_path,
        )
        write_json(self.export_root / "manifest.json", manifest)

        if self.write_unity_support:
            install_unity_support(
                self.export_root, unity_project=self.plan.unity_project
            )

        _log.info("已写入 %s", self.export_root)


__all__ = ["ExportSession", "build_plan"]

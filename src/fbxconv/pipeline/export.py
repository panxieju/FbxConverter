"""Export orchestration (spec sections 3, 4.5 and 5).

Turns a validated selection into one batched Unreal run, then writes the
Unity-facing artefacts: per-character ``profile.json``, the top-level
``manifest.json``, ``Reports/export_report.json`` and the Editor scripts.
"""

from __future__ import annotations

import posixpath
import struct
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
    TextureRequest,
)
from ..report import ExportReport
from ..unity.manifest import (
    build_manifest_payload,
    build_profile_payload,
    install_unity_support,
    material_name_collisions,
    write_json,
)
from ..unreal.locator import UnrealInstall
from ..unreal.paths import (
    package_to_object,
    package_to_relative,
    sanitise_filename,
    unique_posix,
)
from ..unreal.runner import UnrealRunner

_log = get_logger("pipeline.export")

MESH_DIR = "Mesh"
ANIM_DIR = "Animations"
CHARACTERS_DIR = "Characters"
STATIC_DIR = "StaticMeshes"
TEXTURE_DIR = "_Textures"
REPORTS_DIR = "Reports"
REPORT_NAME = "export_report.json"

ProgressFn = Callable[[str, float | None], None]
EventFn = Callable[[dict[str, Any]], None]
ItemFn = Callable[[ExportOutcome], None]


# --------------------------------------------------------------------------- #
# Planning
# --------------------------------------------------------------------------- #


def safe_relative_dir(relative: str) -> str:
    """Sanitise each segment of a Content-relative folder."""
    parts = [
        sanitise_filename(part, fallback="Folder")
        for part in (relative or "").split("/")
        if part
    ]
    return posixpath.join(*parts) if parts else ""


PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def png_size(path: str | Path) -> tuple[int, int] | None:
    """``(width, height)`` of a PNG, straight from its IHDR chunk.

    Unity's default ``maxTextureSize`` is 2048, so a 4K source silently gets
    halved on import. Recording the real size lets the importer say so instead
    of letting the user wonder why the model looks soft.
    """
    try:
        with open(path, "rb") as handle:
            header = handle.read(24)
    except OSError:
        return None
    if len(header) < 24 or not header.startswith(PNG_MAGIC):
        return None
    try:
        width, height = struct.unpack(">II", header[16:24])
    except struct.error:  # pragma: no cover - short read
        return None
    return int(width), int(height)


def build_texture_requests(
    scan: ScanResult, groups: Sequence[GroupPlan]
) -> tuple[list[TextureRequest], dict[str, str]]:
    """Every texture the selected static meshes need, as PNG exports.

    ``Textures`` land in one shared pool rather than beside each FBX: a pack
    like PolygonApocalypse has 1900 meshes in a dozen folders all pointing at
    the same atlas, and copying it per folder would multiply the payload.
    """
    packages: list[str] = []
    for plan_group in groups:
        if not plan_group.is_static:
            continue
        for mesh in plan_group.group.meshes:
            for texture in mesh.textures:
                if texture not in packages:
                    packages.append(texture)

    requests: list[TextureRequest] = []
    mapping: dict[str, str] = {}
    for package in sorted(packages):
        relative = package_to_relative(package, scan.mount_point)
        name = sanitise_filename(relative.name, fallback="Texture")
        parent = safe_relative_dir(relative.parent.as_posix())
        output_rel = posixpath.join(STATIC_DIR, TEXTURE_DIR, parent, f"{name}.png")
        mapping[package] = output_rel
        requests.append(
            TextureRequest(
                package_path=package,
                output_rel=output_rel,
                display_name=name,
            )
        )
    return requests, mapping


def build_plan(
    scan: ScanResult,
    selection: Sequence[SelectedGroup],
    settings: ExportSettings,
    *,
    export_root: Path,
    work_root: Path,
    unity_project: Path | None = None,
) -> ConversionPlan:
    """Lay out every FBX (and texture) that needs producing (spec section 5)."""
    if not selection:
        raise ConfigError("没有选择任何要转换的组。")

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

        if group.is_static:
            # Mirror the Content layout, so a 2000-mesh pack stays browsable.
            # Bucketing happens on the same folder, so two static groups can
            # never claim the same directory.
            folder = safe_relative_dir(group.relative_dir)
            _log.debug("静态组 %s -> %s", output_name, folder or ".")
            for mesh in chosen.selected_meshes:
                name = sanitise_filename(
                    mesh.name or mesh.display_name, fallback="StaticMesh"
                )
                relative = posixpath.join(
                    STATIC_DIR, folder, unique_posix(f"{name}.fbx", taken)
                )
                plan_group.mesh_requests.append(
                    ExportRequest(
                        kind=AssetKind.STATIC_MESH,
                        package_path=mesh.package_path,
                        output_rel=relative,
                        display_name=mesh.name or mesh.display_name,
                    )
                )
            if not plan_group.all_requests:
                _log.warning("跳过 %s：没有可导出的静态网格", group.display_name)
                continue
            groups.append(plan_group)
            continue

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
        raise ConfigError("所选组中没有可导出的网格或动画。")

    texture_requests, texture_rel_by_package = build_texture_requests(scan, groups)

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
        texture_requests=texture_requests,
        texture_rel_by_package=texture_rel_by_package,
        engine_version=scan.engine_version,
        unity_project=Path(unity_project) if unity_project else None,
    )

    _log.info(
        "转换计划：%d 组（骨骼 %d / 静态 %d），%d 个网格，%d 个动画，%d 个贴图",
        len(plan.groups),
        len(plan.skeletal_groups),
        len(plan.static_groups),
        sum(len(g.mesh_requests) for g in plan.groups),
        sum(len(g.animation_requests) for g in plan.groups),
        len(plan.texture_requests),
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
    texture_metadata: dict[str, dict[str, Any]] = field(default_factory=dict, init=False)
    """Per texture package: ``{srgb, texture_kind, output_rel, size}``.

    Filled by the texture run and merged into ``manifest.json`` so Unity knows
    how to import each PNG (sRGB or linear, normal map or not).
    """

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

        # Textures come out of a separate editor launch (see _run_textures), so
        # a retry that mixes FBX and PNG failures has to be split rather than
        # handed wholesale to either run.
        if requests is None:
            fbx_targets = list(targets)
            texture_targets = (
                list(self.plan.texture_requests)
                if self.plan.settings.export_textures
                else []
            )
            if self.plan.texture_requests and not texture_targets:
                self._progress(
                    f"已按设置跳过 {len(self.plan.texture_requests)} 个贴图（--no-textures）"
                )
        else:
            fbx_targets = [r for r in targets if r.kind is not AssetKind.TEXTURE]
            texture_targets = [r for r in targets if r.kind is AssetKind.TEXTURE]

        if not fbx_targets and not texture_targets:
            report.finished_at = ExportReport.timestamp()
            report.duration_s = time.monotonic() - started
            self.outcomes = list(previous or [])
            report.outcomes = self.outcomes
            return report

        self.export_root.mkdir(parents=True, exist_ok=True)

        for plan_group in self.plan.groups:
            if plan_group.is_static:
                folder = safe_relative_dir(plan_group.group.relative_dir)
                (self.export_root / STATIC_DIR / Path(folder)).mkdir(
                    parents=True, exist_ok=True
                )
                continue
            (self.export_root / CHARACTERS_DIR / plan_group.output_name).mkdir(
                parents=True, exist_ok=True
            )

        batch_outcomes: list[ExportOutcome] = []
        if fbx_targets:
            payload = [
                {
                    "kind": request.kind.value,
                    "package_path": request.package_path,
                    "object_path": package_to_object(request.package_path),
                    "output_rel": request.output_rel,
                    "output_path": str(self.export_root / Path(request.output_rel)),
                    "display_name": request.display_name,
                }
                for request in fbx_targets
            ]

            self._progress(f"正在通过 Unreal 导出 {len(payload)} 个 FBX…")
            batch_outcomes = self._run_unreal(payload, fbx_targets)

        texture_outcomes: list[ExportOutcome] = []
        if texture_targets:
            texture_outcomes = self._run_textures(texture_targets)

        # Merge with previous outcomes for retries.
        merged: dict[str, ExportOutcome] = {}
        for outcome in list(previous or []):
            merged[outcome.request.output_rel] = outcome
        for outcome in [*batch_outcomes, *texture_outcomes]:
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

    def _run_textures(self, requests: Sequence[TextureRequest]) -> list[ExportOutcome]:
        """Export every texture the selected static meshes need.

        Runs as its own editor launch: ``UTextureExporterPNG`` performs a native
        ``check()`` on the source format and a failed check kills the process, so
        this must not be able to take the (already finished) FBX work with it.
        """
        requests = list(requests)
        payload = [
            {
                "package_path": request.package_path,
                "output_path": str(self.export_root / Path(request.output_rel)),
                "display_name": request.display_name,
            }
            for request in requests
        ]

        self._progress(f"正在通过 Unreal 导出 {len(requests)} 个贴图…")

        runner = UnrealRunner(
            self.install,
            project_file=self.plan.unreal_project or self.plan.project_file,
            work_root=self.plan.work_root,
            null_rhi=self.null_rhi,
        )

        data: dict[str, Any] = {}
        partial = False
        try:
            result = runner.run_script(
                "fbxconv_textures.py",
                {"textures": payload},
                label="textures",
                on_event=self._forward_event,
                cancel_event=self.cancel_event,
                timeout=7200.0,
            )
            data = result.data
            partial = not bool(data.get("complete", False))
        except FbxConvError as exc:
            message = getattr(exc, "message", None) or str(exc)
            self._progress(f"贴图导出未完成：{message}")
            partial = True

        by_package: dict[str, dict[str, Any]] = {}
        for item in data.get("results", []) or []:
            by_package[str(item.get("package_path", ""))] = item

        outcomes: list[ExportOutcome] = []
        for request in requests:
            item = by_package.get(request.package_path)
            if item is None:
                reason = (
                    "编辑器在导出贴图时崩溃，该项未产出（其余贴图已保留）。"
                    if partial
                    else "Unreal 未返回该贴图的结果。"
                )
                outcome = ExportOutcome(
                    request=request, status=ItemStatus.FAILED, error=reason, retryable=True
                )
            elif item.get("ok"):
                size = png_size(self.export_root / Path(request.output_rel))
                self.texture_metadata[request.package_path] = {
                    "srgb": bool(item.get("srgb", True)),
                    "texture_kind": str(item.get("texture_kind") or "baseColor"),
                    "class": str(item.get("texture_class") or "Texture2D"),
                    "compression": str(item.get("compression") or ""),
                    "source_file": str(item.get("source_file") or ""),
                    "output_rel": request.output_rel,
                    "size": int(item.get("size") or 0),
                    "width": size[0] if size else 0,
                    "height": size[1] if size else 0,
                }
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
                    error=str(item.get("error") or "未知错误"),
                    retryable=True,
                )
            outcomes.append(outcome)
            self._emit_item(outcome)

        if partial:
            self._progress("贴图批次提前结束；已完成的部分已写入磁盘。")
        return outcomes

    def _forward_event(self, event: dict[str, Any]) -> None:
        if event.get("type") == "log" and event.get("level") == "error":
            _log.error("%s", event.get("message"))
        if self.on_event is not None:
            self.on_event(event)

    def _write_artefacts(self, report: ExportReport) -> None:
        profile_paths: dict[str, str] = {}

        for plan_group in self.plan.groups:
            if plan_group.is_static:
                # Static folders are described entirely by manifest.json; there
                # is no skeleton, no avatar and no clip set to profile.
                continue
            payload = build_profile_payload(plan_group, self.plan.settings)
            relative = posixpath.join(
                CHARACTERS_DIR, plan_group.output_name, "profile.json"
            )
            write_json(self.export_root / Path(relative), payload)
            profile_paths[plan_group.output_name] = relative
            _log.debug("写入 %s", relative)

        report_path = posixpath.join(REPORTS_DIR, REPORT_NAME)
        report.save(self.export_root / Path(report_path))

        # Unity binds materials to a model by name, so two different Unreal
        # materials sharing an asset name cannot both survive. Say so here
        # rather than letting one of them silently win.
        material_packages = [
            material
            for group in self.plan.static_groups
            for mesh in group.group.meshes
            for material in mesh.materials
        ]
        for message in material_name_collisions(material_packages):
            _log.warning("%s", message)
            if message not in report.messages:
                report.messages.append(message)
        if report.messages:
            report.save(self.export_root / Path(report_path))

        manifest = build_manifest_payload(
            self.plan,
            profile_paths=profile_paths,
            generated_at=report.finished_at or ExportReport.timestamp(),
            report_path=report_path,
            texture_metadata=self.texture_metadata,
        )
        write_json(self.export_root / "manifest.json", manifest)

        if self.write_unity_support:
            install_unity_support(
                self.export_root, unity_project=self.plan.unity_project
            )

        _log.info("已写入 %s", self.export_root)


__all__ = [
    "CHARACTERS_DIR",
    "STATIC_DIR",
    "TEXTURE_DIR",
    "ExportSession",
    "build_plan",
    "build_texture_requests",
    "safe_relative_dir",
]

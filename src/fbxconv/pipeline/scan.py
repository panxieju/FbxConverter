"""Scan orchestration (spec sections 2 and 3).

Ties the three halves of the scan together:

1. :mod:`fbxconv.discover` lists the ``.uasset`` files on disk.
2. Unreal's AssetRegistry says what each one *is* and what it depends on.
3. :func:`group_assets` organises the result by Skeleton.
"""

from __future__ import annotations

import posixpath
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..discover import MOUNT_POINT, DiscoveryResult, discover
from ..logutil import get_logger
from ..models import (
    AssetKind,
    AssetRecord,
    GroupStatus,
    ScanResult,
    SkeletonGroup,
)
from ..unreal.locator import UnrealInstall, resolve_install_for_project
from ..unreal.paths import sanitise_filename
from ..unreal.project import MountMode, ensure_conversion_project
from ..unreal.runner import UnrealRunner

_log = get_logger("pipeline.scan")

#: Folder names that describe *what* an asset is rather than *which* character
#: it belongs to; skipped when deriving a group folder name.
GENERIC_SEGMENTS = frozenset(
    {
        "meshes",
        "mesh",
        "animations",
        "animation",
        "anims",
        "anim",
        "materials",
        "material",
        "textures",
        "texture",
        "rigs",
        "rig",
        "poses",
        "skeleton",
        "skeletons",
        "characters",
        "character",
        "content",
        "assets",
        "data",
    }
)

ProgressFn = Callable[[str, float | None], None]
EventFn = Callable[[dict[str, Any]], None]
LogFn = Callable[[int, str], None]


# --------------------------------------------------------------------------- #
# Grouping
# --------------------------------------------------------------------------- #


def _parent_segments(package_path: str, mount_point: str) -> list[str]:
    text = package_path.rstrip("/")
    prefix = mount_point.rstrip("/") + "/"
    if text.startswith(prefix):
        text = text[len(prefix) :]
    parts = [p for p in text.split("/") if p]
    return parts[:-1] if parts else []


def _short_location(package_path: str, mount_point: str) -> str:
    """Readable package location, trimmed from the middle when deep."""
    segments = _parent_segments(package_path, mount_point)
    if not segments:
        return ""
    if len(segments) > 3:
        segments = [segments[0], "…", *segments[-2:]]
    return "/".join(segments)


def _disambiguate_display_names(
    groups: Sequence[SkeletonGroup], mount_point: str
) -> None:
    """Qualify skeleton names that appear in more than one pack.

    Downloaded packs routinely each ship an asset literally called
    ``UE4_Mannequin_Skeleton``. Four identical rows make the selection UI
    unusable, so colliding names get their package location appended.
    """
    counts: dict[str, int] = {}
    for group in groups:
        counts[group.display_name] = counts.get(group.display_name, 0) + 1

    for group in groups:
        if counts.get(group.display_name, 0) < 2:
            continue
        anchor = group.skeleton_path or (
            group.meshes[0].package_path if group.meshes else ""
        )
        location = _short_location(anchor, mount_point) if anchor else ""
        if location:
            group.display_name = f"{group.display_name}  ({location})"


def derive_group_name(
    skeleton_path: str | None,
    meshes: Sequence[AssetRecord],
    mount_point: str,
    taken: set[str],
) -> str:
    """Human-friendly, filesystem-safe folder name for a skeleton group."""
    anchor = skeleton_path or (meshes[0].package_path if meshes else "")
    segments = _parent_segments(anchor, mount_point) if anchor else []
    meaningful = [s for s in segments if s.lower() not in GENERIC_SEGMENTS]

    if meaningful:
        base = meaningful[-1]
    elif meshes:
        base = meshes[0].name
    elif anchor:
        base = posixpath.basename(anchor)
    else:
        base = "Ungrouped"

    candidate = sanitise_filename(base, fallback="Group")
    if candidate in taken:
        # Disambiguate with the reference mesh, which is what the user sees.
        suffix = sanitise_filename(meshes[0].name) if meshes else "group"
        candidate = f"{candidate}_{suffix}"
    stem = candidate
    index = 2
    while candidate in taken:
        candidate = f"{stem}_{index}"
        index += 1
    taken.add(candidate)
    return candidate


def _missing_entries(record: AssetRecord) -> list[dict[str, str]]:
    raw = record.extra.get("missing") if record.extra else None
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    for item in raw:
        if isinstance(item, dict) and item.get("package"):
            out.append(
                {
                    "package": str(item["package"]),
                    "expected_path": str(item.get("expected_path") or ""),
                }
            )
        elif isinstance(item, str):
            out.append({"package": item, "expected_path": ""})
    return out


def group_assets(
    assets: Iterable[AssetRecord],
    *,
    mount_point: str = MOUNT_POINT,
) -> list[SkeletonGroup]:
    """Organise confirmed assets by the Skeleton they bind to."""
    asset_list = list(assets)
    by_kind: dict[AssetKind, list[AssetRecord]] = {}
    for asset in asset_list:
        by_kind.setdefault(asset.kind, []).append(asset)

    skeleton_assets = {a.package_path: a for a in by_kind.get(AssetKind.SKELETON, [])}
    known_packages = {a.package_path for a in asset_list}

    buckets: dict[str | None, SkeletonGroup] = {}

    def bucket(skeleton: str | None) -> SkeletonGroup:
        if skeleton not in buckets:
            asset = skeleton_assets.get(skeleton) if skeleton else None
            if asset is not None:
                display = asset.name
            elif skeleton:
                display = posixpath.basename(skeleton)
            else:
                display = "未关联骨架"
            buckets[skeleton] = SkeletonGroup(
                skeleton_path=skeleton, display_name=display
            )
        return buckets[skeleton]

    # Skeletons first so empty groups still surface in the UI.
    for skeleton_path, asset in skeleton_assets.items():
        group = bucket(skeleton_path)
        group.notes.append(f"骨架资源：{asset.package_path}")

    for mesh in by_kind.get(AssetKind.SKELETAL_MESH, []):
        bucket(mesh.skeleton).meshes.append(mesh)
    for anim in by_kind.get(AssetKind.ANIM_SEQUENCE, []):
        bucket(anim.skeleton).animations.append(anim)
    for phys in by_kind.get(AssetKind.PHYSICS_ASSET, []):
        bucket(None).physics_assets.append(phys)

    # Track missing dependencies, including skeletons we never found.
    referenced: set[str] = set()
    for group in buckets.values():
        missing: dict[str, str] = {}
        for asset in [*group.meshes, *group.animations]:
            for entry in _missing_entries(asset):
                missing.setdefault(entry["package"], entry["expected_path"])
            if asset.skeleton:
                referenced.add(asset.skeleton)
                if asset.skeleton not in known_packages:
                    missing.setdefault(asset.skeleton, "")
        group.missing_dependencies = sorted(missing)
        group.notes = sorted(set(group.notes))

    # Drop placeholder buckets that received nothing -- an "unlinked skeleton"
    # row with zero meshes and zero animations is pure noise in the UI.
    groups = sorted(
        (g for g in buckets.values() if g.meshes or g.animations),
        key=lambda g: (
            g.skeleton_path is None,
            -len(g.meshes),
            -len(g.animations),
            g.display_name,
        ),
    )

    _disambiguate_display_names(groups, mount_point)

    taken: set[str] = set()
    for group in groups:
        group.output_name = derive_group_name(
            group.skeleton_path, group.meshes, mount_point, taken
        )

    _log.info(
        "按骨架分组完成：%d 组（%d 网格 / %d 动画）",
        len(groups),
        sum(len(g.meshes) for g in groups),
        sum(len(g.animations) for g in groups),
    )
    return groups


def group_output_name(group: SkeletonGroup) -> str:
    """Folder name for a group, falling back to its display name."""
    return group.output_name or sanitise_filename(group.display_name, fallback="Group")


def _clean_notes(group: SkeletonGroup) -> list[str]:
    return [n for n in group.notes if not n.startswith("__output_name__:")]


def _first_segment(package_path: str, mount_point: str) -> str:
    prefix = mount_point.rstrip("/") + "/"
    rest = package_path[len(prefix) :] if package_path.startswith(prefix) else package_path
    return rest.split("/", 1)[0]


def diagnose_dependencies(
    assets: Sequence[AssetRecord],
    groups: Sequence[SkeletonGroup],
    mount_point: str,
) -> list[str]:
    """Explain the "everything is missing" signature.

    Downloaded asset packs are frequently laid out as ``<Pack>/Content/**`` and
    reference ``/Game/<Pack>/...``, because they expect to be copied to
    ``Content/<Pack>/``. Pointing the tool straight at that ``Content`` folder
    mounts its *contents* at ``/Game``, so every internal reference misses.

    That failure mode is invisible in a bare list of missing paths, so detect
    it and say what to do about it.
    """
    missing: set[str] = set()
    for group in groups:
        missing.update(group.missing_dependencies)

    prefix = mount_point.rstrip("/") + "/"
    game_missing = {m for m in missing if m.startswith(prefix)}
    if len(game_missing) < 3:
        return []

    present = {
        _first_segment(a.package_path, mount_point)
        for a in assets
        if a.package_path.startswith(prefix)
    }
    wanted = {_first_segment(m, mount_point) for m in game_missing}
    absent = sorted(wanted - present)
    if not absent:
        return []

    # Only claim a mount mismatch when it dominates the missing set.
    if len(game_missing) / max(1, len(missing)) < 0.5:
        return []

    sample = "; ".join(sorted(game_missing)[:3])
    existing = ", ".join(sorted(present)[:6]) or "无"
    return [
        "疑似挂载点不匹配："
        f"{len(game_missing)} 个依赖指向 {mount_point}/{absent[0]}/…，"
        f"但当前 {mount_point} 下不存在该顶层目录（实际存在：{existing}）。"
        "常见原因：该资源包期望内容位于 Content/<包名>/ 之下，而所选目录本身已经是那个 Content。"
        "处理方式：改选包含该包目录的上一级，或把包内容放到转换项目 Content/<包名>/ 下。"
        f"示例：{sample}"
    ]


# --------------------------------------------------------------------------- #
# Mount point detection
# --------------------------------------------------------------------------- #


def detect_mount_point(content_root: Path, project_file: Path | None) -> str:
    """Determine where Unreal mounts ``content_root``.

    Project content mounts at ``/Game``; content inside
    ``<project>/Plugins/<Name>/Content`` mounts at ``/<Name>``.
    """
    if project_file is None:
        return MOUNT_POINT
    project_dir = Path(project_file).parent
    try:
        relative = Path(content_root).resolve().relative_to(project_dir.resolve())
    except ValueError:
        return MOUNT_POINT
    parts = relative.parts
    if len(parts) >= 3 and parts[0].lower() == "plugins" and parts[-1].lower() == "content":
        return "/" + parts[1]
    return MOUNT_POINT


# --------------------------------------------------------------------------- #
# Session
# --------------------------------------------------------------------------- #


@dataclass
class ScanSession:
    """Runs discovery + Unreal confirmation for one content root."""

    install: UnrealInstall
    work_root: Path
    mount_mode: MountMode | str = MountMode.JUNCTION
    null_rhi: bool = True
    deep_resolve: bool = True
    on_progress: ProgressFn | None = None
    on_event: EventFn | None = None
    on_log: LogFn | None = None
    cancel_event: threading.Event | None = None

    discovery: DiscoveryResult | None = field(default=None, init=False)
    project_file: Path | None = field(default=None, init=False)
    mount_point: str = field(default=MOUNT_POINT, init=False)
    scope_prefix: str = field(default="", init=False)
    conversion_project: Path | None = field(default=None, init=False)

    def _progress(self, message: str, fraction: float | None = None) -> None:
        _log.info("%s", message)
        if self.on_progress is not None:
            self.on_progress(message, fraction)

    def _resolve_scope(self, discovery: DiscoveryResult) -> str:
        """Package path of the user's selection, relative to the mount point.

        Selecting ``Content/CLazyAnimpack`` mounts at ``Content`` (so
        ``/Game/...`` references resolve) but should only *report*
        ``/Game/CLazyAnimpack``. Without this the whole project gets scanned.
        """
        try:
            relative = Path(discovery.original_root).resolve().relative_to(
                Path(discovery.content_root).resolve()
            )
        except ValueError:
            return ""
        parts = relative.parts
        if not parts or parts == (".",):
            return ""
        return self.mount_point.rstrip("/") + "/" + "/".join(parts)

    def prepare(self, content_root: str | Path) -> Path:
        """Discover files and materialise the Unreal project to scan with."""
        started = time.monotonic()

        def _tick(dirs: int, found: int) -> None:
            if self.on_progress is not None:
                self.on_progress(f"已扫描 {dirs} 个目录，发现 {found} 个资源", None)

        discovery = discover(content_root, progress=_tick)
        self.discovery = discovery
        self.project_file = discovery.project_file
        self.mount_point = detect_mount_point(discovery.content_root, discovery.project_file)
        self.scope_prefix = self._resolve_scope(discovery)

        # A project declares the engine it was authored against; honouring it
        # avoids opening (and silently upgrading) a 5.4 project with 5.8.
        if discovery.project_file is not None:
            self.install = resolve_install_for_project(
                discovery.project_file, self.install
            )

        if discovery.project_file is not None:
            self._progress(f"使用现有项目：{discovery.project_file.name}")
            if self.scope_prefix:
                self._progress(
                    f"挂载点 {self.mount_point}（依赖可解析），仅扫描 {self.scope_prefix}"
                )
        else:
            self._progress("未找到 .uproject，正在创建独立转换项目…")
            project = ensure_conversion_project(
                self.work_root,
                discovery.content_root,
                engine_version=self.install.version,
                mount_mode=self.mount_mode,
                progress=lambda msg: self._progress(msg),
            )
            self.project_file = project.uproject
            self.conversion_project = project.root

        _log.info(
            "准备完成：%d 个资源，用时 %.1fs，挂载点 %s",
            len(discovery.files),
            time.monotonic() - started,
            self.mount_point,
        )
        return self.project_file

    def run(self, content_root: str | Path) -> ScanResult:
        """Full scan: disk discovery, Unreal confirmation, skeleton grouping."""
        started = time.monotonic()
        project_file = self.prepare(content_root)
        discovery = self.discovery
        assert discovery is not None  # set by prepare()

        if not discovery.files:
            self._progress("目录中没有 .uasset 文件")
            return ScanResult(
                content_root=discovery.content_root,
                mount_point=self.mount_point,
                project_file=discovery.project_file,
                conversion_project=self.conversion_project,
                engine_version=self.install.version,
                unreal_project=project_file,
                warnings=["选定目录中未发现任何 .uasset 资源。"],
                cooked_archives=discovery.cooked_archives,
                file_count=0,
            )

        self._progress(
            f"正在通过 Unreal 确认 {len(discovery.files)} 个资源的类型与依赖…"
        )
        runner = UnrealRunner(
            self.install,
            project_file=project_file,
            work_root=self.work_root,
            null_rhi=self.null_rhi,
        )
        result = runner.run_script(
            "fbxconv_scan.py",
            {
                "mount_point": self.mount_point,
                "scope_prefix": self.scope_prefix,
                "deep_resolve": self.deep_resolve,
            },
            label="scan",
            on_output=None,
            on_event=self.on_event,
            cancel_event=self.cancel_event,
            timeout=1800.0,
        )
        data = result.require_ok()

        assets = [AssetRecord.from_payload(item) for item in data.get("assets", [])]
        groups = group_assets(assets, mount_point=self.mount_point)

        warnings = list(result.warnings)
        if discovery.project_file is None:
            warnings.append(
                "源目录没有 .uproject，已创建独立转换项目："
                f"{self.conversion_project or '（未生成）'}"
            )
        if discovery.cooked_archives:
            warnings.append(
                f"发现 {len(discovery.cooked_archives)} 个打包文件"
                "（.pak/.utoc/.ucas），第一版不支持，已跳过。"
            )
        unresolved = [g for g in groups if g.status is GroupStatus.MISSING_DEPS]
        if unresolved:
            warnings.append(
                f"{len(unresolved)} 个骨架组存在缺失依赖，详见各组“缺失依赖”列表。"
            )
        warnings.extend(diagnose_dependencies(assets, groups, self.mount_point))

        scan = ScanResult(
            content_root=discovery.content_root,
            mount_point=self.mount_point,
            project_file=discovery.project_file,
            conversion_project=self.conversion_project,
            engine_version=str(data.get("engine_version") or self.install.version),
            unreal_project=project_file,
            scope_prefix=self.scope_prefix,
            unreal_editor=str(self.install.editor_cmd),
            groups=groups,
            assets=assets,
            warnings=warnings,
            cooked_archives=discovery.cooked_archives,
            file_count=len(discovery.files),
            scan_seconds=time.monotonic() - started,
        )

        _log.info(
            "扫描完成：%d 个资源，%d 组，%d 个动画，用时 %.1fs",
            len(assets),
            len(groups),
            scan.total_animations,
            scan.scan_seconds,
        )
        return scan


__all__ = [
    "GENERIC_SEGMENTS",
    "ScanSession",
    "derive_group_name",
    "detect_mount_point",
    "diagnose_dependencies",
    "group_assets",
    "group_output_name",
]

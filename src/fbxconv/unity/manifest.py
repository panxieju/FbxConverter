"""``manifest.json`` / ``profile.json`` generation.

The schema is deliberately restricted to what Unity's built-in ``JsonUtility``
can parse: a root object, ``[Serializable]``-shaped nested objects, arrays of
strings, and no ``null`` numbers. That keeps the Unity importer free of any
third-party JSON dependency (Newtonsoft is not loaded by default).

All FBX paths inside these files are relative to the ``UnityExport`` root.
"""

from __future__ import annotations

import json
import posixpath
import shutil
from pathlib import Path
from typing import Any

from .. import __version__, resources
from ..logutil import get_logger
from ..models import (
    ConversionPlan,
    ExportSettings,
    GroupPlan,
    LoopMode,
    RootMotionMode,
    ScanResult,
)

_log = get_logger("unity.manifest")

SCHEMA_VERSION = 1
IMPORTER_FILENAME = "UnrealResourceImporter.cs"
IMPORTER_ASMDEF = "FbxConverter.Editor.asmdef"
TESTS_DIRNAME = "Tests"
TESTS_FILENAME = "UnrealImportTests.cs"
TESTS_ASMDEF = "FbxConverter.Tests.asmdef"

#: Animation names implying a one-shot (non-looping) clip.
ONCE_TOKENS = (
    "jump",
    "land",
    "start",
    "end",
    "attack",
    "hit",
    "death",
    "die",
    "pose",
    "draw",
    "holster",
    "open",
    "close",
    "equip",
    "spawn",
    "intro",
    "outro",
    "transition",
    "reload",
    "throw",
    "cast",
    "vault",
    "climb",
    "getup",
    "stagger",
    "react",
)

#: Animation names implying a looping clip.
LOOP_TOKENS = (
    "idle",
    "walk",
    "run",
    "jog",
    "sprint",
    "cycle",
    "crouch",
    "strafe",
    "fly",
    "swim",
    "hover",
    "breath",
    "turn",
    "loop",
)


def templates_dir() -> Path:
    return resources.templates_dir()


def infer_loop(name: str) -> bool:
    """Guess whether an animation should loop, from its Unreal asset name."""
    lowered = (name or "").lower()
    if "loop" in lowered:
        return True
    for token in ONCE_TOKENS:
        if token in lowered:
            return False
    return any(token in lowered for token in LOOP_TOKENS)


def resolve_loop(settings: ExportSettings, name: str) -> bool:
    if settings.loop_mode is LoopMode.FORCE_LOOP:
        return True
    if settings.loop_mode is LoopMode.FORCE_ONCE:
        return False
    return infer_loop(name)


def _anim_has_root_motion(settings: ExportSettings) -> bool:
    return settings.root_motion is not RootMotionMode.LOCK_FULL


def _motion_node(settings: ExportSettings) -> str:
    # Unreal's root bone is named "root"; locking horizontal travel still keeps
    # vertical motion available to Unity's retargeting.
    return "root" if settings.root_motion is not RootMotionMode.KEEP else ""


def build_profile_payload(
    plan_group: GroupPlan,
    settings: ExportSettings,
    *,
    scan: ScanResult | None = None,
) -> dict[str, Any]:
    """Per-character import instructions consumed by the Unity importer."""
    group = plan_group.group
    animations_by_package = {a.package_path: a for a in group.animations}

    mesh_entry: dict[str, Any] = {
        "fbx": "",
        "name": "",
        "unrealPackage": "",
        "avatarName": f"{plan_group.output_name}_Avatar",
        "skeletonPackage": group.skeleton_path or "",
    }
    if plan_group.mesh_requests:
        request = plan_group.mesh_requests[0]
        record = next(
            (m for m in group.meshes if m.package_path == request.package_path), None
        )
        mesh_entry.update(
            {
                "fbx": request.output_rel,
                "name": request.display_name,
                "unrealPackage": request.package_path,
            }
        )
        if record is not None:
            mesh_entry["triangleHint"] = int(record.extra.get("triangle_count") or 0)
            mesh_entry["materialCount"] = len(record.materials)

    animation_entries: list[dict[str, Any]] = []
    for request in plan_group.animation_requests:
        record = animations_by_package.get(request.package_path)
        duration = float(record.duration) if record and record.duration else 0.0
        frames = int(record.num_frames) if record and record.num_frames else 0
        rate = 0.0
        if record is not None:
            try:
                rate = float(record.extra.get("frame_rate") or 0.0)
            except (TypeError, ValueError):
                rate = 0.0
        animation_entries.append(
            {
                "name": request.display_name,
                "fbx": request.output_rel,
                "unrealPackage": request.package_path,
                "loop": resolve_loop(settings, request.display_name),
                "rootMotion": _anim_has_root_motion(settings),
                "lengthSeconds": round(duration, 6),
                "frames": frames,
                "frameRate": rate if rate > 0 else float(settings.sampling_rate),
            }
        )

    materials = []
    seen_slots: set[str] = set()
    for mesh in group.meshes:
        for material in mesh.materials:
            if material in seen_slots:
                continue
            seen_slots.add(material)
            materials.append(
                {
                    "unrealMaterial": material,
                    "name": posixpath.basename(material),
                    "note": "材质槽保留；完整 Unreal 材质不自动还原",
                }
            )

    notes = [
        "FBX 路径相对于 UnityExport 根目录。",
        "第一版保留材质槽，不还原完整 Unreal 材质图。",
    ]
    if group.missing_dependencies:
        notes.append(f"该组有 {len(group.missing_dependencies)} 个缺失依赖。")

    return {
        "schemaVersion": SCHEMA_VERSION,
        "generator": f"fbxconv {__version__}",
        "pathsRelativeTo": "UnityExport",
        "group": plan_group.output_name,
        "displayName": group.display_name,
        "skeletonPackage": group.skeleton_path or "",
        "referenceMesh": mesh_entry["unrealPackage"],
        "importType": settings.import_type.value,
        "scale": float(settings.scale),
        "samplingRate": int(settings.sampling_rate),
        "motionNode": _motion_node(settings),
        "mesh": mesh_entry,
        "animations": animation_entries,
        "materials": materials,
        "missingDependencies": list(group.missing_dependencies),
        "notes": notes,
    }


def build_manifest_payload(
    plan: ConversionPlan,
    *,
    profile_paths: dict[str, str],
    generated_at: str,
    report_path: str = "",
) -> dict[str, Any]:
    """Top-level manifest tying every character and report together."""
    characters = []
    for plan_group in plan.groups:
        group = plan_group.group
        characters.append(
            {
                "name": plan_group.output_name,
                "displayName": group.display_name,
                "skeletonPackage": group.skeleton_path or "",
                "profilePath": profile_paths.get(plan_group.output_name, ""),
                "meshPath": plan_group.mesh_requests[0].output_rel
                if plan_group.mesh_requests
                else "",
                "meshName": plan_group.mesh_requests[0].display_name
                if plan_group.mesh_requests
                else "",
                "animationCount": len(plan_group.animation_requests),
                "animationPaths": [r.output_rel for r in plan_group.animation_requests],
                "animationNames": [r.display_name for r in plan_group.animation_requests],
                "missingDependencies": list(group.missing_dependencies),
            }
        )

    return {
        "schemaVersion": SCHEMA_VERSION,
        "generator": f"fbxconv {__version__}",
        "generatedAt": generated_at,
        "pathsRelativeTo": "UnityExport",
        "source": {
            "contentRoot": str(plan.content_root),
            "projectFile": str(plan.project_file) if plan.project_file else "",
            "conversionProject": str(plan.conversion_project)
            if plan.conversion_project
            else "",
            "engineVersion": plan.engine_version,
            "mountPoint": plan.mount_point,
        },
        "settings": {
            "importType": plan.settings.import_type.value,
            "samplingRate": int(plan.settings.sampling_rate),
            "loopMode": plan.settings.loop_mode.value,
            "rootMotion": plan.settings.root_motion.value,
            "scale": float(plan.settings.scale),
            "exportMaterials": bool(plan.settings.export_materials),
        },
        "importerScript": f"Editor/{IMPORTER_FILENAME}",
        "reportPath": report_path,
        "characterCount": len(characters),
        "animationCount": sum(len(g.animation_requests) for g in plan.groups),
        "characters": characters,
    }


def write_json(path: str | Path, payload: dict[str, Any]) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return target


def find_enclosing_project(path: str | Path) -> Path | None:
    """Walk up from ``path`` looking for a Unity project root."""
    current = Path(path).resolve()
    for candidate in [current, *current.parents]:
        if (candidate / "Assets").is_dir() and (candidate / "ProjectSettings").is_dir():
            return candidate
    return None


def project_has_test_framework(project: str | Path) -> bool:
    """Does this Unity project resolve ``com.unity.test-framework``?

    Checked from the project's own package files rather than inferred from an
    asmdef, because Unity compiles an assembly whose types are missing even
    when ``defineConstraints`` looks satisfied -- and a broken test assembly
    blocks Play Mode for the whole project.
    """
    root = Path(project)
    for name in ("Packages/manifest.json", "Packages/packages-lock.json"):
        try:
            payload = json.loads((root / name).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        if "com.unity.test-framework" in (payload.get("dependencies") or {}):
            return True
    return False


def install_unity_support(
    export_root: str | Path,
    *,
    unity_project: str | Path | None = None,
    overwrite: bool = True,
    include_tests: bool | None = None,
) -> list[Path]:
    """Mirror the Editor scripts into the export tree (and optionally a project).

    The export-tree copy is what makes ``UnityExport`` self-contained: drop the
    folder into ``Assets/`` and the importer compiles and appears under
    ``Tools > FbxConverter``.

    When the export tree already lives inside the target project's ``Assets``
    folder the second copy is skipped -- shipping the same C# twice would make
    Unity fail with CS0101 (duplicate definition).

    ``include_tests=None`` auto-detects: the EditMode tests are only installed
    when the destination project actually resolves the Unity Test Framework.
    """
    source_dir = templates_dir()
    export_path = Path(export_root).resolve()
    written: list[Path] = []

    project = Path(unity_project).resolve() if unity_project else find_enclosing_project(export_path)
    if include_tests is None:
        include_tests = project is not None and project_has_test_framework(project)

    targets = [export_path / "Editor"]

    if project is not None:
        project_assets = project / "Assets"
        try:
            export_path.relative_to(project_assets)
            inside_assets = True
        except ValueError:
            inside_assets = False
        if inside_assets:
            _log.debug("导出目录已在项目 Assets 内，跳过重复安装：%s", project_assets)
        else:
            targets.append(project_assets / "Editor" / "FbxConverter")

    for target_dir in targets:
        for source in sorted(source_dir.rglob("*")):
            if not source.is_file():
                continue
            relative = source.relative_to(source_dir)
            if not include_tests and relative.parts[0] == TESTS_DIRNAME:
                continue
            destination = target_dir / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists() and not overwrite:
                continue
            shutil.copy2(source, destination)
            written.append(destination)
            _log.debug("写入 Unity 脚本 %s", destination)

        _write_editor_readme(target_dir, include_tests=include_tests)
        written.append(target_dir / "README.md")

    return written


def _write_editor_readme(target_dir: Path, *, include_tests: bool) -> None:
    """Explain the payload, and how to turn the optional tests on."""
    if include_tests:
        test_note = (
            "`Tests/` 内含 EditMode 校验测试（需要 Unity Test Framework）。\n"
            "  从 Window > General > Test Runner > EditMode 运行 `FbxConverter.Tests`。"
        )
    else:
        test_note = (
            "未安装 `Tests/`：当前项目没有解析到 Unity Test Framework。\n"
            "  安装 com.unity.test-framework 后重新导出，即可获得 EditMode 校验测试。"
        )

    text = (
        "# FbxConverter Unity 支持脚本\n"
        "\n"
        "由 FbxConverter 自动生成，可安全覆盖。\n"
        "\n"
        "- `UnrealResourceImporter.cs` -- 按 manifest.json 配置导入设置并校验结果。\n"
        "  菜单：Tools > FbxConverter > Import Unreal Export...\n"
        "  批处理：`-executeMethod FbxConverter.Editor.UnrealResourceImporter.ImportFromCommandLine`\n"
        f"  {test_note}\n"
        "- `FbxConverter.Editor.asmdef` -- 把它隔离到独立程序集，避免污染 Assembly-CSharp。\n"
    )
    (target_dir / "README.md").write_text(text, encoding="utf-8")


__all__ = [
    "IMPORTER_ASMDEF",
    "IMPORTER_FILENAME",
    "SCHEMA_VERSION",
    "TESTS_ASMDEF",
    "TESTS_DIRNAME",
    "TESTS_FILENAME",
    "build_manifest_payload",
    "build_profile_payload",
    "find_enclosing_project",
    "infer_loop",
    "install_unity_support",
    "project_has_test_framework",
    "resolve_loop",
    "templates_dir",
    "write_json",
]

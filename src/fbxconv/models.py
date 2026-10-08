"""Domain model for the Unreal -> Unity conversion pipeline.

These types are deliberately serialisation-friendly: everything that crosses
the boundary to Unreal (via JSON) or to Unity (via ``manifest.json``) is a
plain ``str``/``int``/``float``/``bool``/list/dict so that no third-party JSON
library is needed on either side.
"""

from __future__ import annotations

import posixpath
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any

# --------------------------------------------------------------------------- #
# Asset taxonomy
# --------------------------------------------------------------------------- #


class AssetKind(str, Enum):
    """Normalised asset category, derived from the Unreal class name."""

    SKELETAL_MESH = "skeletal_mesh"
    SKELETON = "skeleton"
    ANIM_SEQUENCE = "anim_sequence"
    ANIM_MONTAGE = "anim_montage"
    PHYSICS_ASSET = "physics_asset"
    MATERIAL = "material"
    MATERIAL_INSTANCE = "material_instance"
    TEXTURE = "texture"
    ANIM_BLUEPRINT = "anim_blueprint"
    BLUEPRINT = "blueprint"
    RIG = "rig"
    OTHER = "other"

    @property
    def label(self) -> str:
        return _KIND_LABELS[self]

    @property
    def is_exportable(self) -> bool:
        """Only these two kinds produce an FBX in version 1."""
        return self in (AssetKind.SKELETAL_MESH, AssetKind.ANIM_SEQUENCE)


_KIND_LABELS: dict[AssetKind, str] = {
    AssetKind.SKELETAL_MESH: "Skeletal Mesh",
    AssetKind.SKELETON: "Skeleton",
    AssetKind.ANIM_SEQUENCE: "Animation Sequence",
    AssetKind.ANIM_MONTAGE: "Animation Montage",
    AssetKind.PHYSICS_ASSET: "Physics Asset",
    AssetKind.MATERIAL: "Material",
    AssetKind.MATERIAL_INSTANCE: "Material Instance",
    AssetKind.TEXTURE: "Texture",
    AssetKind.ANIM_BLUEPRINT: "Animation Blueprint",
    AssetKind.BLUEPRINT: "Blueprint",
    AssetKind.RIG: "Rig",
    AssetKind.OTHER: "Other",
}

#: Unreal class name (with or without the ``U``/``A`` prefix) -> normalised kind.
_CLASS_TO_KIND: dict[str, AssetKind] = {
    "SkeletalMesh": AssetKind.SKELETAL_MESH,
    "Skeleton": AssetKind.SKELETON,
    "AnimSequence": AssetKind.ANIM_SEQUENCE,
    "AnimSequenceBase": AssetKind.ANIM_SEQUENCE,
    "AnimMontage": AssetKind.ANIM_MONTAGE,
    "PhysicsAsset": AssetKind.PHYSICS_ASSET,
    "Material": AssetKind.MATERIAL,
    "MaterialInstance": AssetKind.MATERIAL_INSTANCE,
    "MaterialInstanceConstant": AssetKind.MATERIAL_INSTANCE,
    "MaterialInstanceDynamic": AssetKind.MATERIAL_INSTANCE,
    "Texture": AssetKind.TEXTURE,
    "Texture2D": AssetKind.TEXTURE,
    "TextureCube": AssetKind.TEXTURE,
    "AnimBlueprint": AssetKind.ANIM_BLUEPRINT,
    "Blueprint": AssetKind.BLUEPRINT,
    "ControlRigBlueprint": AssetKind.RIG,
    "IKRigDefinition": AssetKind.RIG,
    "ControlRig": AssetKind.RIG,
    "SkeletalMeshLODSettings": AssetKind.OTHER,
}


def classify_class(class_name: str) -> AssetKind:
    """Map an Unreal class name onto an :class:`AssetKind`.

    Unreal reports classes both bare (``SkeletalMesh``) and prefixed
    (``USkeletalMesh``); both forms are accepted.
    """
    if not class_name:
        return AssetKind.OTHER
    name = class_name.strip()
    if name in _CLASS_TO_KIND:
        return _CLASS_TO_KIND[name]
    # Strip the reflection prefix and retry.
    if len(name) > 1 and name[0] in "UA" and name[1].isupper():
        stripped = name[1:]
        if stripped in _CLASS_TO_KIND:
            return _CLASS_TO_KIND[stripped]
    return AssetKind.OTHER


# --------------------------------------------------------------------------- #
# On-disk discovery
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SourceFile:
    """A ``.uasset`` located on disk, before Unreal confirms what it is."""

    path: Path
    """Absolute path to the ``.uasset``."""

    rel: PurePosixPath
    """Path relative to the content root, e.g. ``Characters/SK_Mannequin.uasset``."""

    package_path: str
    """Unreal package path derived from :attr:`rel`, e.g. ``/Game/Characters/SK_Mannequin``."""

    size: int
    companions: tuple[Path, ...] = ()
    """Existing sibling bulk-data files (``.uexp``, ``.ubulk``, ``.uptnl``)."""

    @property
    def name(self) -> str:
        return self.path.stem


@dataclass(frozen=True)
class CookedArchive:
    """A packaged artefact that version 1 explicitly does not support."""

    path: Path
    suffix: str


# --------------------------------------------------------------------------- #
# Unreal-confirmed asset records
# --------------------------------------------------------------------------- #


@dataclass
class AssetRecord:
    """What Unreal's asset registry reports about a single asset."""

    package_path: str
    name: str
    class_name: str
    kind: AssetKind
    object_path: str = ""
    disk_path: str | None = None
    dependencies: list[str] = field(default_factory=list)
    skeleton: str | None = None
    mesh: str | None = None
    materials: list[str] = field(default_factory=list)
    textures: list[str] = field(default_factory=list)
    physics_asset: str | None = None
    num_frames: int | None = None
    duration: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> AssetRecord:
        class_name = str(payload.get("class", "") or "")
        kind_raw = payload.get("kind")
        kind = AssetKind(kind_raw) if kind_raw else classify_class(class_name)
        return cls(
            package_path=str(payload.get("package_path", "")),
            name=str(payload.get("name", "")),
            class_name=class_name,
            kind=kind,
            object_path=str(payload.get("object_path", "") or ""),
            disk_path=payload.get("disk_path") or None,
            dependencies=[str(d) for d in payload.get("dependencies", []) or []],
            skeleton=payload.get("skeleton") or None,
            mesh=payload.get("mesh") or None,
            materials=[str(m) for m in payload.get("materials", []) or []],
            textures=[str(t) for t in payload.get("textures", []) or []],
            physics_asset=payload.get("physics_asset") or None,
            num_frames=_as_int(payload.get("num_frames")),
            duration=_as_float(payload.get("duration")),
            extra=dict(payload.get("extra", {}) or {}),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "package_path": self.package_path,
            "name": self.name,
            "class": self.class_name,
            "kind": self.kind.value,
            "object_path": self.object_path,
            "disk_path": self.disk_path,
            "dependencies": list(self.dependencies),
            "skeleton": self.skeleton,
            "mesh": self.mesh,
            "materials": list(self.materials),
            "textures": list(self.textures),
            "physics_asset": self.physics_asset,
            "num_frames": self.num_frames,
            "duration": self.duration,
            "extra": dict(self.extra),
        }

    @property
    def display_name(self) -> str:
        return self.name or posixpath.basename(self.package_path)


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class GroupStatus(str, Enum):
    READY = "ready"
    MISSING_DEPS = "missing-deps"
    EMPTY = "empty"

    @property
    def label(self) -> str:
        return {
            GroupStatus.READY: "可转换",
            GroupStatus.MISSING_DEPS: "缺依赖",
            GroupStatus.EMPTY: "无动画",
        }[self]


@dataclass
class SkeletonGroup:
    """One Skeleton plus every mesh/animation bound to it."""

    skeleton_path: str | None
    display_name: str
    output_name: str = ""
    """Folder name used under ``UnityExport/Characters/``."""

    meshes: list[AssetRecord] = field(default_factory=list)
    animations: list[AssetRecord] = field(default_factory=list)
    physics_assets: list[AssetRecord] = field(default_factory=list)
    materials: list[AssetRecord] = field(default_factory=list)
    textures: list[AssetRecord] = field(default_factory=list)
    missing_dependencies: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def status(self) -> GroupStatus:
        if self.missing_dependencies:
            return GroupStatus.MISSING_DEPS
        if not self.meshes:
            return GroupStatus.EMPTY
        return GroupStatus.READY

    @property
    def animation_count(self) -> int:
        return len(self.animations)

    @property
    def reference_mesh(self) -> AssetRecord | None:
        return self.meshes[0] if self.meshes else None

    def to_payload(self) -> dict[str, Any]:
        return {
            "skeleton_path": self.skeleton_path,
            "display_name": self.display_name,
            "output_name": self.output_name or self.display_name,
            "status": self.status.value,
            "meshes": [m.to_payload() for m in self.meshes],
            "animations": [a.to_payload() for a in self.animations],
            "physics_assets": [p.to_payload() for p in self.physics_assets],
            "missing_dependencies": list(self.missing_dependencies),
            "notes": list(self.notes),
        }


@dataclass
class ScanResult:
    """Everything the scan phase learned, ready for the UI and the exporter."""

    content_root: Path
    mount_point: str
    project_file: Path | None
    conversion_project: Path | None
    engine_version: str
    unreal_project: Path | None = None
    """The ``.uproject`` actually handed to Unreal.

    This is ``project_file`` when the source directory belongs to a real
    project, and the generated conversion project's ``.uproject`` otherwise.
    Export *must* reuse it or Unreal starts with no project at all.
    """

    groups: list[SkeletonGroup] = field(default_factory=list)
    assets: list[AssetRecord] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    cooked_archives: list[CookedArchive] = field(default_factory=list)
    file_count: int = 0
    scan_seconds: float = 0.0

    @property
    def total_animations(self) -> int:
        return sum(g.animation_count for g in self.groups)

    @property
    def total_meshes(self) -> int:
        return sum(len(g.meshes) for g in self.groups)

    def group_by_name(self, name: str) -> SkeletonGroup | None:
        for group in self.groups:
            if group.display_name == name:
                return group
        return None


# --------------------------------------------------------------------------- #
# Export configuration
# --------------------------------------------------------------------------- #


class ImportType(str, Enum):
    HUMANOID = "Humanoid"
    GENERIC = "Generic"


class LoopMode(str, Enum):
    AUTO = "auto"
    FORCE_LOOP = "loop"
    FORCE_ONCE = "once"


class RootMotionMode(str, Enum):
    KEEP = "keep"
    """Leave root bone translation intact; Unity exposes it as root motion."""

    LOCK_XZ = "lock-xz"
    """Zero horizontal root travel (in-place animation)."""

    LOCK_FULL = "lock-full"
    """Zero all root translation."""


@dataclass
class ExportSettings:
    """User-facing export knobs (spec section 4.4)."""

    import_type: ImportType = ImportType.GENERIC
    sampling_rate: int = 30
    loop_mode: LoopMode = LoopMode.AUTO
    root_motion: RootMotionMode = RootMotionMode.KEEP
    scale: float = 1.0
    export_materials: bool = True
    include_physics_asset: bool = False

    def to_payload(self) -> dict[str, Any]:
        return {
            "import_type": self.import_type.value,
            "sampling_rate": int(self.sampling_rate),
            "loop_mode": self.loop_mode.value,
            "root_motion": self.root_motion.value,
            "scale": float(self.scale),
            "export_materials": bool(self.export_materials),
            "include_physics_asset": bool(self.include_physics_asset),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> ExportSettings:
        def _enum(enum_cls: Any, raw: Any, default: Any) -> Any:
            if raw is None:
                return default
            try:
                return enum_cls(raw)
            except ValueError:
                return default

        return cls(
            import_type=_enum(ImportType, payload.get("import_type"), ImportType.GENERIC),
            sampling_rate=int(payload.get("sampling_rate", 30) or 30),
            loop_mode=_enum(LoopMode, payload.get("loop_mode"), LoopMode.AUTO),
            root_motion=_enum(RootMotionMode, payload.get("root_motion"), RootMotionMode.KEEP),
            scale=float(payload.get("scale", 1.0) or 1.0),
            export_materials=bool(payload.get("export_materials", True)),
            include_physics_asset=bool(payload.get("include_physics_asset", False)),
        )


# --------------------------------------------------------------------------- #
# Jobs and outcomes
# --------------------------------------------------------------------------- #


@dataclass
class ExportRequest:
    """One FBX to produce."""

    kind: AssetKind
    package_path: str
    output_rel: str
    """Destination path relative to the Unity export root."""

    display_name: str = ""

    def __post_init__(self) -> None:
        if not self.display_name:
            self.display_name = posixpath.basename(self.package_path)

    def to_payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "package_path": self.package_path,
            "output_rel": self.output_rel,
            "display_name": self.display_name,
        }


class ItemStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    OK = "ok"
    FAILED = "failed"
    SKIPPED = "skipped"

    @property
    def label(self) -> str:
        return {
            ItemStatus.PENDING: "等待",
            ItemStatus.RUNNING: "转换中",
            ItemStatus.OK: "成功",
            ItemStatus.FAILED: "失败",
            ItemStatus.SKIPPED: "跳过",
        }[self]


@dataclass
class ExportOutcome:
    request: ExportRequest
    status: ItemStatus
    output_path: str | None = None
    error: str | None = None
    retryable: bool = True
    duration_s: float = 0.0
    detail: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "kind": self.request.kind.value,
            "package_path": self.request.package_path,
            "display_name": self.request.display_name,
            "output_rel": self.request.output_rel,
            "output_path": self.output_path,
            "status": self.status.value,
            "error": self.error,
            "retryable": bool(self.retryable),
            "duration_s": round(self.duration_s, 3),
            "detail": self.detail,
        }


@dataclass
class SelectedGroup:
    """A skeleton group the user chose to convert, with its checked animations."""

    group: SkeletonGroup
    mesh: AssetRecord | None = None
    animations: list[AssetRecord] = field(default_factory=list)

    @property
    def output_name(self) -> str:
        return self.group.output_name or self.group.display_name

    @property
    def reference_mesh(self) -> AssetRecord | None:
        return self.mesh or self.group.reference_mesh


@dataclass
class GroupPlan:
    """A skeleton group selected for conversion."""

    group: SkeletonGroup
    output_name: str
    mesh_requests: list[ExportRequest] = field(default_factory=list)
    animation_requests: list[ExportRequest] = field(default_factory=list)

    @property
    def all_requests(self) -> list[ExportRequest]:
        return [*self.mesh_requests, *self.animation_requests]


@dataclass
class ConversionPlan:
    """The complete unit of work handed to the export phase."""

    content_root: Path
    mount_point: str
    project_file: Path | None
    conversion_project: Path | None
    export_root: Path
    work_root: Path
    settings: ExportSettings
    unreal_project: Path | None = None
    """The ``.uproject`` to launch Unreal with (see :attr:`ScanResult.unreal_project`)."""

    groups: list[GroupPlan] = field(default_factory=list)
    engine_version: str = ""
    unity_project: Path | None = None

    @property
    def all_requests(self) -> list[ExportRequest]:
        out: list[ExportRequest] = []
        for group in self.groups:
            out.extend(group.all_requests)
        return out


__all__ = [
    "AssetKind",
    "AssetRecord",
    "ConversionPlan",
    "CookedArchive",
    "ExportOutcome",
    "ExportRequest",
    "ExportSettings",
    "GroupPlan",
    "GroupStatus",
    "ImportType",
    "ItemStatus",
    "LoopMode",
    "RootMotionMode",
    "ScanResult",
    "SelectedGroup",
    "SkeletonGroup",
    "SourceFile",
    "classify_class",
]

"""Tests for the dependency-mismatch diagnosis."""

from __future__ import annotations

from fbxconv.models import AssetKind, AssetRecord, SkeletonGroup
from fbxconv.pipeline.scan import diagnose_dependencies


def _mesh(package: str, *, missing: list[str] | None = None) -> AssetRecord:
    record = AssetRecord(
        package_path=package,
        name=package.rsplit("/", 1)[-1],
        class_name="SkeletalMesh",
        kind=AssetKind.SKELETAL_MESH,
    )
    if missing:
        record.extra["missing"] = [{"package": m, "expected_path": ""} for m in missing]
    return record


def _group(missing: list[str], meshes: list[AssetRecord] | None = None) -> SkeletonGroup:
    return SkeletonGroup(
        skeleton_path="/Game/S",
        display_name="G",
        meshes=meshes or [],
        missing_dependencies=missing,
    )


class TestDiagnoseDependencies:
    def test_flags_pack_layout_mismatch(self):
        # Mirrors the real case: <Pack>/Content/** referencing /Game/<Pack>/...
        assets = [
            _mesh("/Game/Mannequins/Meshes/SKM_Manny"),
            _mesh("/Game/Mannequin_UE4/Meshes/SK_Mannequin"),
        ]
        missing = [
            "/Game/Characters/Mannequins/Meshes/SK_Mannequin",
            "/Game/Characters/Mannequins/Materials/MI_Manny_01",
            "/Game/Characters/Mannequin_UE4/Materials/M_Body",
        ]
        groups = [_group(missing)]

        messages = diagnose_dependencies(assets, groups, "/Game")

        assert len(messages) == 1
        assert "挂载点不匹配" in messages[0]
        assert "/Game/Characters" in messages[0]
        assert "Mannequins" in messages[0]  # names what IS present

    def test_silent_when_missing_matches_existing_toplevel(self):
        # A genuinely missing asset under a folder we do have: not a mismatch.
        assets = [_mesh("/Game/Shared/Meshes/SK_A")]
        groups = [_group(["/Game/Shared/Meshes/SK_Gone"])]
        assert diagnose_dependencies(assets, groups, "/Game") == []

    def test_silent_when_too_few_missing(self):
        assets = [_mesh("/Game/Existing/Meshes/SK_A")]
        groups = [_group(["/Game/Absent/B", "/Game/Absent/C"])]
        assert diagnose_dependencies(assets, groups, "/Game") == []

    def test_silent_when_mismatch_is_a_minority(self):
        assets = [_mesh("/Game/Existing/Meshes/SK_A")]
        game = [f"/Game/Absent/A{i}" for i in range(4)]
        engine = [f"/Engine/Thing{i}" for i in range(20)]
        assert diagnose_dependencies(assets, [_group(game + engine)], "/Game") == []

    def test_silent_when_nothing_missing(self):
        assert diagnose_dependencies([_mesh("/Game/A")], [_group([])], "/Game") == []

    def test_respects_non_default_mount_point(self):
        assets = [_mesh("/MyPlugin/A/B")]
        missing = [f"/MyPlugin/Pack/C{i}" for i in range(4)]
        messages = diagnose_dependencies(assets, [_group(missing)], "/MyPlugin")
        assert len(messages) == 1
        assert "/MyPlugin/Pack" in messages[0]

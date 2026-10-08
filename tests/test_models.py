"""Domain-model tests: classification, settings, grouping."""

from __future__ import annotations

from fbxconv.models import (
    AssetKind,
    AssetRecord,
    ExportSettings,
    GroupStatus,
    ImportType,
    LoopMode,
    RootMotionMode,
    SkeletonGroup,
    classify_class,
)
from fbxconv.pipeline.scan import (
    derive_group_name,
    detect_mount_point,
    group_assets,
    group_output_name,
)


class TestClassify:
    def test_known_classes(self):
        assert classify_class("SkeletalMesh") is AssetKind.SKELETAL_MESH
        assert classify_class("AnimSequence") is AssetKind.ANIM_SEQUENCE
        assert classify_class("Skeleton") is AssetKind.SKELETON
        assert classify_class("PhysicsAsset") is AssetKind.PHYSICS_ASSET
        assert classify_class("Texture2D") is AssetKind.TEXTURE

    def test_reflection_prefix_is_tolerated(self):
        assert classify_class("USkeletalMesh") is AssetKind.SKELETAL_MESH
        assert classify_class("UAnimSequence") is AssetKind.ANIM_SEQUENCE

    def test_unknown_and_empty(self):
        assert classify_class("NiagaraSystem") is AssetKind.OTHER
        assert classify_class("") is AssetKind.OTHER

    def test_only_meshes_and_animations_are_exportable(self):
        assert AssetKind.SKELETAL_MESH.is_exportable
        assert AssetKind.ANIM_SEQUENCE.is_exportable
        assert not AssetKind.SKELETON.is_exportable
        assert not AssetKind.TEXTURE.is_exportable
        assert not AssetKind.PHYSICS_ASSET.is_exportable


class TestExportSettings:
    def test_payload_round_trip(self):
        original = ExportSettings(
            import_type=ImportType.HUMANOID,
            sampling_rate=60,
            loop_mode=LoopMode.FORCE_LOOP,
            root_motion=RootMotionMode.LOCK_XZ,
            scale=0.01,
            export_materials=False,
        )
        restored = ExportSettings.from_payload(original.to_payload())
        assert restored == original

    def test_unknown_enum_values_fall_back(self):
        restored = ExportSettings.from_payload(
            {"import_type": "Alien", "loop_mode": "nonsense", "root_motion": "???"}
        )
        assert restored.import_type is ImportType.GENERIC
        assert restored.loop_mode is LoopMode.AUTO
        assert restored.root_motion is RootMotionMode.KEEP


class TestGroupStatus:
    def test_ready_when_all_present(self):
        group = SkeletonGroup(
            skeleton_path="/Game/S",
            display_name="G",
            meshes=[AssetRecord("/Game/M", "M", "SkeletalMesh", AssetKind.SKELETAL_MESH)],
        )
        assert group.status is GroupStatus.READY

    def test_missing_deps_wins(self):
        group = SkeletonGroup(
            skeleton_path="/Game/S",
            display_name="G",
            meshes=[AssetRecord("/Game/M", "M", "SkeletalMesh", AssetKind.SKELETAL_MESH)],
            missing_dependencies=["/Game/Gone"],
        )
        assert group.status is GroupStatus.MISSING_DEPS

    def test_empty_without_meshes(self):
        group = SkeletonGroup(skeleton_path="/Game/S", display_name="G")
        assert group.status is GroupStatus.EMPTY


class TestGrouping:
    def _mesh(self, package: str, skeleton: str | None) -> AssetRecord:
        return AssetRecord(
            package_path=package,
            name=package.rsplit("/", 1)[-1],
            class_name="SkeletalMesh",
            kind=AssetKind.SKELETAL_MESH,
            skeleton=skeleton,
        )

    def _anim(self, package: str, skeleton: str | None) -> AssetRecord:
        return AssetRecord(
            package_path=package,
            name=package.rsplit("/", 1)[-1],
            class_name="AnimSequence",
            kind=AssetKind.ANIM_SEQUENCE,
            skeleton=skeleton,
        )

    def test_groups_by_skeleton(self):
        skeleton_a = "/Game/A/A_Skeleton"
        skeleton_b = "/Game/B/B_Skeleton"
        assets = [
            AssetRecord(skeleton_a, "A_Skeleton", "Skeleton", AssetKind.SKELETON),
            AssetRecord(skeleton_b, "B_Skeleton", "Skeleton", AssetKind.SKELETON),
            self._mesh("/Game/A/Meshes/SK_A", skeleton_a),
            self._anim("/Game/A/Animations/Walk", skeleton_a),
            self._anim("/Game/A/Animations/Run", skeleton_a),
            self._mesh("/Game/B/Meshes/SK_B", skeleton_b),
            self._anim("/Game/B/Animations/Idle", skeleton_b),
        ]

        groups = group_assets(assets)
        by_skeleton = {g.skeleton_path: g for g in groups}

        assert set(by_skeleton) == {skeleton_a, skeleton_b}
        assert by_skeleton[skeleton_a].animation_count == 2
        assert by_skeleton[skeleton_b].animation_count == 1
        assert all(g.status is GroupStatus.READY for g in groups)

    def test_animations_without_skeleton_are_not_lost(self):
        assets = [self._anim("/Game/Orphan/Anim", None)]
        groups = group_assets(assets)
        orphan = next(g for g in groups if g.skeleton_path is None)
        assert orphan.animation_count == 1

    def test_referenced_but_absent_skeleton_is_a_missing_dependency(self):
        assets = [self._mesh("/Game/A/Meshes/SK_A", "/Game/A/A_Skeleton")]
        groups = group_assets(assets)
        assert "/Game/A/A_Skeleton" in groups[0].missing_dependencies
        assert groups[0].status is GroupStatus.MISSING_DEPS

    def test_each_group_gets_a_unique_output_name(self):
        assets = [
            self._mesh("/Game/Shared/Meshes/SK_A", "/Game/Shared/A_Skeleton"),
            self._mesh("/Game/Shared/Meshes/SK_B", "/Game/Shared/B_Skeleton"),
        ]
        groups = group_assets(assets)
        names = [group_output_name(g) for g in groups]
        assert len(set(names)) == len(names)


class TestGroupDisambiguation:
    """Real packs each ship their own ``UE4_Mannequin_Skeleton``."""

    def _skeleton(self, package: str) -> AssetRecord:
        return AssetRecord(
            package_path=package,
            name=package.rsplit("/", 1)[-1],
            class_name="Skeleton",
            kind=AssetKind.SKELETON,
        )

    def _mesh(self, package: str, skeleton: str) -> AssetRecord:
        return AssetRecord(
            package_path=package,
            name=package.rsplit("/", 1)[-1],
            class_name="SkeletalMesh",
            kind=AssetKind.SKELETAL_MESH,
            skeleton=skeleton,
        )

    def _anim(self, package: str, skeleton: str) -> AssetRecord:
        return AssetRecord(
            package_path=package,
            name=package.rsplit("/", 1)[-1],
            class_name="AnimSequence",
            kind=AssetKind.ANIM_SEQUENCE,
            skeleton=skeleton,
        )

    def test_clashing_skeleton_names_are_qualified(self):
        skel_a = "/Game/PackA/Mesh/UE4_Mannequin_Skeleton"
        skel_b = "/Game/PackB/Mesh/UE4_Mannequin_Skeleton"
        assets = [
            self._skeleton(skel_a),
            self._skeleton(skel_b),
            self._mesh("/Game/PackA/Mesh/SK_Mannequin", skel_a),
            self._mesh("/Game/PackB/Mesh/SK_Mannequin", skel_b),
        ]

        groups = group_assets(assets)
        names = [g.display_name for g in groups]

        assert len(groups) == 2
        assert len(set(names)) == 2, names
        assert all("UE4_Mannequin_Skeleton" in n for n in names)
        assert any("PackA" in n for n in names)
        assert any("PackB" in n for n in names)

    def test_unique_names_are_not_decorated(self):
        assets = [
            self._skeleton("/Game/PackA/Mesh/Unique_Skeleton"),
            self._mesh("/Game/PackA/Mesh/SK_A", "/Game/PackA/Mesh/Unique_Skeleton"),
        ]
        groups = group_assets(assets)
        assert groups[0].display_name == "Unique_Skeleton"

    def test_empty_buckets_are_dropped(self):
        # A skeleton with no bound meshes or animations is pure UI noise.
        assets = [self._skeleton("/Game/OnlyAnOrphanSkeleton")]
        assert group_assets(assets) == []

    def test_group_with_animations_but_no_mesh_is_kept(self):
        skel = "/Game/Pack/Skeleton"
        assets = [
            self._skeleton(skel),
            self._anim("/Game/Pack/Animations/Walk", skel),
        ]
        groups = group_assets(assets)
        assert len(groups) == 1
        assert groups[0].animation_count == 1


class TestDeriveGroupName:
    def test_skips_generic_segments(self):
        taken: set[str] = set()
        name = derive_group_name(
            "/Game/Mannequin_UE4/Meshes/SK_Mannequin_Skeleton",
            [AssetRecord("/Game/Mannequin_UE4/Meshes/SK_Mannequin", "SK_Mannequin",
                         "SkeletalMesh", AssetKind.SKELETAL_MESH)],
            "/Game",
            taken,
        )
        assert name == "Mannequin_UE4"

    def test_disambiguates_collisions(self):
        taken: set[str] = set()
        meshes_a = [AssetRecord("/Game/Pack/Meshes/SK_A", "SK_A", "SkeletalMesh",
                                AssetKind.SKELETAL_MESH)]
        meshes_b = [AssetRecord("/Game/Pack/Meshes/SK_B", "SK_B", "SkeletalMesh",
                                AssetKind.SKELETAL_MESH)]
        first = derive_group_name("/Game/Pack/A_Skeleton", meshes_a, "/Game", taken)
        second = derive_group_name("/Game/Pack/B_Skeleton", meshes_b, "/Game", taken)
        assert first != second


class TestDetectMountPoint:
    def test_project_content_mounts_at_game(self, tmp_path):
        project = tmp_path / "P"
        content = project / "Content"
        content.mkdir(parents=True)
        uproject = project / "P.uproject"
        uproject.write_text("{}", encoding="utf-8")
        assert detect_mount_point(content, uproject) == "/Game"

    def test_plugin_content_mounts_under_plugin_name(self, tmp_path):
        project = tmp_path / "P"
        content = project / "Plugins" / "MyPlugin" / "Content"
        content.mkdir(parents=True)
        uproject = project / "P.uproject"
        uproject.write_text("{}", encoding="utf-8")
        assert detect_mount_point(content, uproject) == "/MyPlugin"

    def test_no_project_defaults_to_game(self, tmp_path):
        content = tmp_path / "Content"
        content.mkdir()
        assert detect_mount_point(content, None) == "/Game"

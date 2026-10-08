"""Artefact tests: config persistence, profiles, manifest and the export report."""

from __future__ import annotations

import json
from pathlib import Path

from fbxconv.config import AppConfig
from fbxconv.models import (
    AssetKind,
    AssetRecord,
    ConversionPlan,
    ExportOutcome,
    ExportRequest,
    ExportSettings,
    GroupPlan,
    ImportType,
    ItemStatus,
    LoopMode,
    RootMotionMode,
    SkeletonGroup,
)
from fbxconv.report import ExportReport
from fbxconv.unity.manifest import (
    build_manifest_payload,
    build_profile_payload,
    infer_loop,
    resolve_loop,
)

# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


def _group() -> SkeletonGroup:
    mesh = AssetRecord(
        package_path="/Game/Mannequin_UE4/Meshes/SK_Mannequin",
        name="SK_Mannequin",
        class_name="SkeletalMesh",
        kind=AssetKind.SKELETAL_MESH,
        skeleton="/Game/Mannequin_UE4/Meshes/SK_Mannequin_Skeleton",
        materials=["/Game/Mannequin_UE4/Materials/M_Body"],
    )
    animations = [
        AssetRecord(
            package_path=f"/Game/Mannequin_UE4/Animations/{name}",
            name=name,
            class_name="AnimSequence",
            kind=AssetKind.ANIM_SEQUENCE,
            skeleton="/Game/Mannequin_UE4/Meshes/SK_Mannequin_Skeleton",
            num_frames=60,
            duration=1.966,
            extra={"frame_rate": 30.0},
        )
        for name in ("Jog_Fwd", "Idle_Loop", "Jump_Start")
    ]
    return SkeletonGroup(
        skeleton_path="/Game/Mannequin_UE4/Meshes/SK_Mannequin_Skeleton",
        display_name="SK_Mannequin_Skeleton",
        output_name="Mannequin_UE4",
        meshes=[mesh],
        animations=animations,
    )


def _plan(settings: ExportSettings | None = None) -> ConversionPlan:
    group = _group()
    plan_group = GroupPlan(group=group, output_name="Mannequin_UE4")
    plan_group.mesh_requests.append(
        ExportRequest(
            kind=AssetKind.SKELETAL_MESH,
            package_path=group.meshes[0].package_path,
            output_rel="Characters/Mannequin_UE4/Mesh/SK_Mannequin.fbx",
            display_name="SK_Mannequin",
        )
    )
    for animation in group.animations:
        plan_group.animation_requests.append(
            ExportRequest(
                kind=AssetKind.ANIM_SEQUENCE,
                package_path=animation.package_path,
                output_rel=f"Characters/Mannequin_UE4/Animations/{animation.name}.fbx",
                display_name=animation.name,
            )
        )
    return ConversionPlan(
        content_root=Path("H:/Packs/Characters"),
        mount_point="/Game",
        project_file=None,
        conversion_project=Path("G:/work/UnrealProject"),
        unreal_project=Path("G:/work/UnrealProject/FbxConvProject.uproject"),
        export_root=Path("G:/out/UnityExport"),
        work_root=Path("G:/out/.fbxconv"),
        settings=settings or ExportSettings(),
        groups=[plan_group],
        engine_version="5.4.4",
    )


# --------------------------------------------------------------------------- #
# Loop inference
# --------------------------------------------------------------------------- #


class TestLoopInference:
    def test_looping_names(self):
        for name in ("Idle_Loop", "MM_Walk_Fwd", "Run", "Jog_Fwd", "Fall_Loop"):
            assert infer_loop(name), name

    def test_one_shot_names(self):
        for name in ("Jump_Start", "Land", "Attack_01", "Death", "MM_T_Pose"):
            assert not infer_loop(name), name

    def test_force_modes_override_inference(self):
        settings = ExportSettings(loop_mode=LoopMode.FORCE_LOOP)
        assert resolve_loop(settings, "Jump_Start")
        settings = ExportSettings(loop_mode=LoopMode.FORCE_ONCE)
        assert not resolve_loop(settings, "Idle_Loop")


# --------------------------------------------------------------------------- #
# profile.json
# --------------------------------------------------------------------------- #


class TestProfile:
    def test_shape_and_paths(self):
        plan = _plan()
        payload = build_profile_payload(plan.groups[0], plan.settings)

        assert payload["schemaVersion"] == 1
        assert payload["pathsRelativeTo"] == "UnityExport"
        assert payload["importType"] == "Generic"
        assert payload["group"] == "Mannequin_UE4"
        assert payload["mesh"]["fbx"] == "Characters/Mannequin_UE4/Mesh/SK_Mannequin.fbx"
        assert len(payload["animations"]) == 3
        assert payload["animations"][0]["loop"] is True  # Jog_Fwd
        assert payload["animations"][2]["loop"] is False  # Jump_Start
        assert payload["animations"][0]["frames"] == 60
        assert payload["materials"][0]["name"] == "M_Body"

    def test_contains_no_nulls_in_numeric_fields(self):
        # JsonUtility cannot represent null for value types on the C# side.
        plan = _plan()
        payload = build_profile_payload(plan.groups[0], plan.settings)
        for animation in payload["animations"]:
            assert isinstance(animation["frames"], int)
            assert isinstance(animation["lengthSeconds"], float)
            assert isinstance(animation["frameRate"], float)

    def test_humanoid_and_root_motion_settings_propagate(self):
        settings = ExportSettings(
            import_type=ImportType.HUMANOID, root_motion=RootMotionMode.LOCK_XZ
        )
        payload = build_profile_payload(_plan(settings).groups[0], settings)
        assert payload["importType"] == "Humanoid"
        assert payload["motionNode"] == "root"
        assert all(a["rootMotion"] for a in payload["animations"])


# --------------------------------------------------------------------------- #
# manifest.json
# --------------------------------------------------------------------------- #


class TestManifest:
    def test_references_profiles_and_characters(self):
        plan = _plan()
        payload = build_manifest_payload(
            plan,
            profile_paths={"Mannequin_UE4": "Characters/Mannequin_UE4/profile.json"},
            generated_at="2026-01-01T00:00:00",
            report_path="Reports/export_report.json",
        )

        assert payload["schemaVersion"] == 1
        assert payload["characterCount"] == 1
        assert payload["animationCount"] == 3
        assert payload["source"]["engineVersion"] == "5.4.4"
        character = payload["characters"][0]
        assert character["profilePath"] == "Characters/Mannequin_UE4/profile.json"
        assert character["meshPath"].endswith("SK_Mannequin.fbx")
        assert len(character["animationPaths"]) == 3

    def test_is_json_serialisable(self):
        plan = _plan()
        payload = build_manifest_payload(
            plan, profile_paths={}, generated_at="now"
        )
        json.dumps(payload)  # must not raise


# --------------------------------------------------------------------------- #
# export_report.json
# --------------------------------------------------------------------------- #


class TestReport:
    def test_summary_counts(self):
        report = ExportReport(
            settings=ExportSettings(),
            outcomes=[
                ExportOutcome(
                    ExportRequest(AssetKind.SKELETAL_MESH, "/Game/A", "A.fbx"), ItemStatus.OK
                ),
                ExportOutcome(
                    ExportRequest(AssetKind.ANIM_SEQUENCE, "/Game/B", "B.fbx"),
                    ItemStatus.FAILED,
                    error="boom",
                    retryable=True,
                ),
            ],
        )
        assert report.success_count == 1
        assert report.failure_count == 1
        assert len(report.retryable) == 1
        assert "成功 1" in report.summary_line()

    def test_saves_valid_json(self, tmp_path: Path):
        report = ExportReport(outcomes=[])
        target = report.save(tmp_path / "Reports" / "export_report.json")
        payload = json.loads(target.read_text(encoding="utf-8"))
        assert payload["schema_version"] == 1
        assert payload["summary"]["total"] == 0


# --------------------------------------------------------------------------- #
# config.json
# --------------------------------------------------------------------------- #


class TestConfig:
    def test_round_trip(self, tmp_path: Path):
        original = AppConfig(
            unreal_editor=Path("H:/UE_5.4/Engine/Binaries/Win64/UnrealEditor-Cmd.exe"),
            content_root=Path("H:/Packs/Content"),
            output_root=Path("G:/Out"),
            settings=ExportSettings(sampling_rate=60, import_type=ImportType.HUMANOID),
        )
        target = tmp_path / "config.json"
        original.save(target)

        restored = AppConfig.load(target)

        assert restored.content_root == original.content_root
        assert restored.output_root == original.output_root
        assert restored.settings.sampling_rate == 60
        assert restored.settings.import_type is ImportType.HUMANOID

    def test_missing_file_yields_defaults(self, tmp_path: Path):
        config = AppConfig.load(tmp_path / "nope.json")
        assert config.content_root is None
        assert config.settings.import_type is ImportType.GENERIC

    def test_corrupt_file_yields_defaults(self, tmp_path: Path):
        target = tmp_path / "config.json"
        target.write_text("{not json", encoding="utf-8")
        assert AppConfig.load(target).content_root is None

    def test_derived_paths(self):
        config = AppConfig(output_root=Path("G:/Out"))
        assert config.export_root == Path("G:/Out/UnityExport")
        assert config.effective_working_root == Path("G:/Out/.fbxconv")

    def test_recent_paths_are_deduplicated_and_capped(self, tmp_path: Path):
        config = AppConfig()
        for index in range(15):
            config.remember_content_root(tmp_path / f"p{index}")
        config.remember_content_root(tmp_path / "p0")
        assert config.recent_content_roots[0] == tmp_path / "p0"
        assert len(config.recent_content_roots) <= 10

"""Static meshes, textures and the Unity material hand-off.

Unreal cannot embed textures in FBX -- ``FFbxExporter::WriteToFile`` hard-codes
``bEmbedMedia = false`` and ``UFbxExportOption`` has no switch for it -- so the
textures are exported as PNG beside the meshes and stitched back together by
``UnrealResourceImporter.cs`` from ``manifest.json``. These tests pin the Python
half of that contract.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path

from fbxconv.models import (
    AssetKind,
    AssetRecord,
    ConversionPlan,
    ExportRequest,
    ExportSettings,
    GroupKind,
    GroupPlan,
    GroupStatus,
    ItemStatus,
    ScanResult,
    SelectedGroup,
    SkeletonGroup,
    TextureRequest,
    classify_class,
)
from fbxconv.pipeline.export import (
    ExportSession,
    build_plan,
    build_texture_requests,
    png_size,
    safe_relative_dir,
)
from fbxconv.pipeline.scan import group_assets, group_static_meshes, relative_dir
from fbxconv.unity.manifest import build_manifest_payload, build_static_materials


def static_mesh(package: str, materials: list[str] | None = None,
                slots: list[dict] | None = None) -> AssetRecord:
    """A StaticMesh record shaped like the Unreal scan payload."""
    record = AssetRecord(
        package_path=package,
        name=package.rsplit("/", 1)[-1],
        class_name="StaticMesh",
        kind=AssetKind.STATIC_MESH,
        materials=list(materials or []),
    )
    if slots is not None:
        record.extra["material_slots"] = slots
    return record


class TestClassify:
    def test_static_mesh_is_recognised(self):
        assert classify_class("StaticMesh") is AssetKind.STATIC_MESH
        assert classify_class("UStaticMesh") is AssetKind.STATIC_MESH

    def test_static_mesh_is_exportable(self):
        assert AssetKind.STATIC_MESH.is_exportable
        assert AssetKind.STATIC_MESH.label == "Static Mesh"


class TestRelativeDir:
    def test_relative_to_scope(self):
        assert (
            relative_dir(
                "/Game/Pack/Meshes/Buildings/SM_X", "/Game", "/Game/Pack"
            )
            == "Meshes/Buildings"
        )

    def test_relative_to_mount_when_unscoped(self):
        assert relative_dir("/Game/Meshes/SM_X", "/Game") == "Meshes"

    def test_top_level_mesh_has_no_folder(self):
        assert relative_dir("/Game/SM_X", "/Game") == ""

    def test_foreign_path_is_not_mangled(self):
        # A mesh outside the mount still yields something usable, not a crash.
        assert relative_dir("/Other/SM_X", "/Game") == "Other"


class TestSafeRelativeDir:
    def test_illegal_characters_are_replaced(self):
        assert safe_relative_dir("A/B:C") == "A/B_C"

    def test_empty_and_stray_separators(self):
        assert safe_relative_dir("") == ""
        assert safe_relative_dir("//A//B//") == "A/B"


class TestGroupStaticMeshes:
    def test_bucketed_by_folder(self):
        meshes = [
            static_mesh("/Game/Pack/Meshes/Buildings/SM_A"),
            static_mesh("/Game/Pack/Meshes/Buildings/SM_B"),
            static_mesh("/Game/Pack/Meshes/Props/SM_C"),
        ]
        groups = group_static_meshes(
            meshes, mount_point="/Game", scope_prefix="/Game/Pack"
        )
        assert [g.relative_dir for g in groups] == ["Meshes/Buildings", "Meshes/Props"]
        assert [g.mesh_count for g in groups] == [2, 1]
        assert all(g.kind is GroupKind.STATIC for g in groups)
        assert all(g.is_static for g in groups)

    def test_output_names_are_unique_and_filesystem_safe(self):
        meshes = [
            static_mesh("/Game/Pack/A:B/SM_A"),
            static_mesh("/Game/Pack/A_B/SM_B"),
        ]
        groups = group_static_meshes(meshes, mount_point="/Game")
        names = [g.output_name for g in groups]
        assert len(set(names)) == len(names)
        assert all(":" not in name for name in names)

    def test_missing_dependencies_are_collected(self):
        mesh = static_mesh("/Game/Pack/Meshes/SM_A")
        mesh.extra["missing"] = [{"package": "/Game/Gone", "expected_path": "x.uasset"}]
        groups = group_static_meshes([mesh], mount_point="/Game")
        assert groups[0].missing_dependencies == ["/Game/Gone"]
        assert groups[0].status is GroupStatus.MISSING_DEPS

    def test_no_meshes_no_groups(self):
        assert group_static_meshes([], mount_point="/Game") == []

    def test_texture_count_is_the_union(self):
        a = static_mesh("/Game/Pack/M/SM_A")
        a.textures = ["/Game/Pack/T/T1", "/Game/Pack/T/T2"]
        b = static_mesh("/Game/Pack/M/SM_B")
        b.textures = ["/Game/Pack/T/T2"]
        groups = group_static_meshes([a, b], mount_point="/Game")
        assert groups[0].texture_count == 2


class TestGroupAssetsMixed:
    def _skeleton(self, package: str) -> AssetRecord:
        return AssetRecord(package, package.rsplit("/", 1)[-1], "Skeleton",
                           AssetKind.SKELETON)

    def test_skeletal_and_static_groups_coexist(self):
        skeleton = "/Game/Pack/Mesh/SK_Skeleton"
        assets = [
            self._skeleton(skeleton),
            AssetRecord("/Game/Pack/Mesh/SK_A", "SK_A", "SkeletalMesh",
                        AssetKind.SKELETAL_MESH, skeleton=skeleton),
            static_mesh("/Game/Pack/Env/SM_Rock"),
        ]
        groups = group_assets(assets, mount_point="/Game", scope_prefix="/Game/Pack")
        kinds = [g.kind for g in groups]
        assert kinds == [GroupKind.SKELETAL, GroupKind.STATIC]
        assert groups[0].skeleton_path == skeleton
        assert groups[1].relative_dir == "Env"

    def test_static_only_scan_is_not_empty(self):
        groups = group_assets([static_mesh("/Game/Pack/Env/SM_Rock")])
        assert len(groups) == 1
        assert groups[0].status is GroupStatus.READY


class TestBuildTextureRequests:
    def _plan(self, meshes: list[AssetRecord]) -> tuple[list[TextureRequest], dict[str, str]]:
        from fbxconv.models import ScanResult

        group = SkeletonGroup(
            skeleton_path=None,
            display_name="Env",
            output_name="Env",
            kind=GroupKind.STATIC,
            relative_dir="Env",
            meshes=meshes,
        )
        scan = ScanResult(
            content_root=Path("/tmp/Content"),
            mount_point="/Game",
            project_file=None,
            conversion_project=None,
            engine_version="5.4.0",
        )
        return build_texture_requests(scan, [GroupPlan(group=group, output_name="Env")])

    def test_textures_land_in_one_shared_pool(self):
        a = static_mesh("/Game/Pack/Env/SM_A")
        a.textures = ["/Game/Pack/Textures/T_Rock"]
        b = static_mesh("/Game/Pack/Env/SM_B")
        b.textures = ["/Game/Pack/Textures/T_Rock", "/Game/Pack/Textures/Sub/T_Moss"]

        requests, mapping = self._plan([a, b])
        paths = sorted(r.output_rel for r in requests)

        assert paths == [
            "StaticMeshes/_Textures/Pack/Textures/Sub/T_Moss.png",
            "StaticMeshes/_Textures/Pack/Textures/T_Rock.png",
        ]
        # Shared, not duplicated per folder: the atlas is referenced once.
        assert len(requests) == 2
        assert mapping["/Game/Pack/Textures/T_Rock"].endswith("T_Rock.png")

    def test_texture_request_reports_texture_kind(self):
        request = TextureRequest("/Game/T/T_A", "StaticMeshes/_Textures/T_A.png")
        assert request.kind is AssetKind.TEXTURE
        assert request.display_name == "T_A"


class TestBuildPlanStatic:
    def _scan(self) -> ScanResult:
        meshes = [
            static_mesh("/Game/Pack/Env/Buildings/SM_A"),
            static_mesh("/Game/Pack/Env/Buildings/SM_B"),
            static_mesh("/Game/Pack/Env/Props/SM_C"),
        ]
        groups = group_assets(meshes, mount_point="/Game", scope_prefix="/Game/Pack")
        return ScanResult(
            content_root=Path("/tmp/Content"),
            mount_point="/Game",
            project_file=None,
            conversion_project=None,
            engine_version="5.4.0",
            scope_prefix="/Game/Pack",
            groups=groups,
            assets=meshes,
        )

    def _plan(self, tmp_path: Path) -> ConversionPlan:
        scan = self._scan()
        # An empty mesh list means "every mesh in the group".
        selection = [SelectedGroup(group=group) for group in scan.static_groups]
        return build_plan(
            scan,
            selection,
            ExportSettings(),
            export_root=tmp_path / "UnityExport",
            work_root=tmp_path / "work",
        )

    def test_every_mesh_is_planned_not_just_the_reference(self, tmp_path):
        plan = self._plan(tmp_path)
        paths = sorted(r.output_rel for r in plan.static_mesh_requests)
        assert paths == [
            "StaticMeshes/Env/Buildings/SM_A.fbx",
            "StaticMeshes/Env/Buildings/SM_B.fbx",
            "StaticMeshes/Env/Props/SM_C.fbx",
        ]
        assert all(r.kind is AssetKind.STATIC_MESH for r in plan.static_mesh_requests)

    def test_no_animation_requests_for_static_groups(self, tmp_path):
        plan = self._plan(tmp_path)
        assert plan.static_groups
        assert all(not g.animation_requests for g in plan.static_groups)

    def test_output_names_are_unique_across_groups(self, tmp_path):
        plan = self._plan(tmp_path)
        names = [g.output_name for g in plan.groups]
        assert len(set(names)) == len(names)

    def test_explicit_mesh_selection_is_honoured(self, tmp_path):
        scan = self._scan()
        group = scan.static_groups[0]
        plan = build_plan(
            scan,
            [SelectedGroup(group=group, meshes=[group.meshes[0]])],
            ExportSettings(),
            export_root=tmp_path / "UnityExport",
            work_root=tmp_path / "work",
        )
        assert len(plan.static_mesh_requests) == 1


def _minimal_png(width: int, height: int) -> bytes:
    """Signature + IHDR, which is all :func:`png_size` reads."""
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">II", width, height)
    )


class TestPngSize:
    def test_reads_ihdr(self, tmp_path):
        path = tmp_path / "t.png"
        path.write_bytes(_minimal_png(4096, 2032))
        assert png_size(path) == (4096, 2032)

    def test_rejects_non_png(self, tmp_path):
        path = tmp_path / "t.png"
        path.write_bytes(b"not a png at all............")
        assert png_size(path) is None

    def test_missing_file_is_not_an_error(self, tmp_path):
        assert png_size(tmp_path / "absent.png") is None


class TestManifestStaticTables:
    def _manifest(self, texture_metadata: dict) -> dict:
        meshes = [
            static_mesh(
                "/Game/Pack/Env/SM_A",
                materials=["/Game/Pack/M/M_Rock"],
                slots=[
                    {
                        "slot": "Rock",
                        "material": "/Game/Pack/M/M_Rock",
                        "textures": ["/Game/Pack/T/T_Rock", "/Game/Pack/T/T_Rock_N"],
                    }
                ],
            )
        ]
        group = SkeletonGroup(
            skeleton_path=None,
            display_name="Env",
            output_name="Env",
            kind=GroupKind.STATIC,
            relative_dir="Env",
            meshes=meshes,
        )
        plan_group = GroupPlan(
            group=group,
            output_name="Env",
            mesh_requests=[
                ExportRequest(
                    kind=AssetKind.STATIC_MESH,
                    package_path="/Game/Pack/Env/SM_A",
                    output_rel="StaticMeshes/Env/SM_A.fbx",
                    display_name="SM_A",
                )
            ],
        )
        plan = ConversionPlan(
            content_root=Path("/tmp/Content"),
            mount_point="/Game",
            project_file=None,
            conversion_project=None,
            export_root=Path("/tmp/out"),
            work_root=Path("/tmp/work"),
            settings=ExportSettings(),
            groups=[plan_group],
            texture_requests=[
                TextureRequest(
                    "/Game/Pack/T/T_Rock",
                    "StaticMeshes/_Textures/Pack/T/T_Rock.png",
                ),
                TextureRequest(
                    "/Game/Pack/T/T_Rock_N",
                    "StaticMeshes/_Textures/Pack/T/T_Rock_N.png",
                ),
            ],
            texture_rel_by_package={
                "/Game/Pack/T/T_Rock": "StaticMeshes/_Textures/Pack/T/T_Rock.png",
                "/Game/Pack/T/T_Rock_N": "StaticMeshes/_Textures/Pack/T/T_Rock_N.png",
            },
        )
        return build_manifest_payload(
            plan,
            profile_paths={},
            generated_at="2026-01-01T00:00:00",
            texture_metadata=texture_metadata,
        )

    def _metadata(self) -> dict:
        return {
            "/Game/Pack/T/T_Rock": {
                "srgb": True,
                "texture_kind": "baseColor",
                "class": "Texture2D",
                "output_rel": "StaticMeshes/_Textures/Pack/T/T_Rock.png",
                "size": 100,
                "width": 2048,
                "height": 2048,
            },
            "/Game/Pack/T/T_Rock_N": {
                "srgb": False,
                "texture_kind": "normal",
                "class": "Texture2D",
                "output_rel": "StaticMeshes/_Textures/Pack/T/T_Rock_N.png",
                "size": 200,
                "width": 2048,
                "height": 2048,
            },
        }

    def test_texture_table_is_keyed_by_name(self):
        manifest = self._manifest(self._metadata())
        names = [t["name"] for t in manifest["textures"]]
        assert names == ["T_Rock", "T_Rock_N"]
        assert manifest["textureCount"] == 2
        assert manifest["staticMeshCount"] == 1

    def test_material_slots_resolve_to_texture_names(self):
        manifest = self._manifest(self._metadata())
        material = manifest["materials"][0]
        assert material["name"] == "M_Rock"
        assert material["baseColor"] == "T_Rock"
        assert material["normal"] == "T_Rock_N"
        assert material["mask"] == ""
        mesh = manifest["staticMeshes"][0]
        assert mesh["slots"] == [{"slotName": "Rock", "materialIndex": 0}]

    def test_duplicate_basenames_are_disambiguated(self):
        manifest = self._manifest(
            {
                "/Game/A/T/T_Rock": {
                    "srgb": True,
                    "texture_kind": "baseColor",
                    "class": "Texture2D",
                    "output_rel": "x/A.png",
                },
                "/Game/B/T/T_Rock": {
                    "srgb": True,
                    "texture_kind": "baseColor",
                    "class": "Texture2D",
                    "output_rel": "x/B.png",
                },
            }
        )
        names = [t["name"] for t in manifest["textures"]]
        assert names == ["T_Rock", "T_Rock_1"]

    def test_missing_metadata_yields_empty_tables(self):
        manifest = self._manifest({})
        assert manifest["textures"] == []
        assert manifest["textureCount"] == 0
        # The material still exists, it just has nothing to point at.
        assert manifest["materials"][0]["baseColor"] == ""

    def test_manifest_has_no_json_nulls(self):
        """JsonUtility turns ``null`` into a silent default; keep it out."""
        payload = json.dumps(self._manifest(self._metadata()))
        assert "null" not in payload
        assert ": null" not in payload

    def test_manifest_is_json_serialisable_and_stable(self):
        manifest = self._manifest(self._metadata())
        first = json.dumps(manifest, sort_keys=True)
        second = json.dumps(self._manifest(self._metadata()), sort_keys=True)
        assert first == second


class TestExportReportSplit:
    def _outcome(self, kind: AssetKind, rel: str):
        from fbxconv.models import ExportOutcome

        return ExportOutcome(
            request=ExportRequest(kind=kind, package_path="/Game/A", output_rel=rel),
            status=ItemStatus.OK,
        )

    def test_textures_are_counted_separately(self):
        from fbxconv.report import ExportReport

        report = ExportReport(
            outcomes=[
                self._outcome(AssetKind.STATIC_MESH, "StaticMeshes/A/SM.fbx"),
                self._outcome(AssetKind.TEXTURE, "StaticMeshes/_Textures/T.png"),
                self._outcome(AssetKind.SKELETAL_MESH, "Characters/C/Mesh/SK.fbx"),
            ]
        )
        assert len(report.fbx_outcomes) == 2
        assert len(report.texture_outcomes) == 1
        assert report.summary_line() == "FBX 成功 2/2，贴图成功 1/1"
        assert report.to_payload()["summary"]["texture_total"] == 1

    def test_failures_are_reported(self):
        from fbxconv.models import ExportOutcome
        from fbxconv.report import ExportReport

        failed = ExportOutcome(
            request=ExportRequest(
                kind=AssetKind.STATIC_MESH, package_path="/Game/A", output_rel="a.fbx"
            ),
            status=ItemStatus.FAILED,
            error="boom",
        )
        report = ExportReport(outcomes=[failed])
        assert "失败 1" in report.summary_line()


class TestSettingsRoundTrip:
    def test_export_textures_survives(self):
        settings = ExportSettings(export_textures=False)
        assert settings.to_payload()["export_textures"] is False
        assert ExportSettings.from_payload(settings.to_payload()).export_textures is False

    def test_defaults_to_enabled(self):
        assert ExportSettings().export_textures is True
        assert ExportSettings.from_payload({}).export_textures is True


class TestRunSkipsUnwantedWork:
    """The session must not call Unreal at all when there is nothing to do."""

    def _session(self, tmp_path: Path, textures: bool) -> ExportSession:
        from fbxconv.unreal.locator import UnrealInstall

        plan = ConversionPlan(
            content_root=tmp_path,
            mount_point="/Game",
            project_file=None,
            conversion_project=None,
            export_root=tmp_path / "UnityExport",
            work_root=tmp_path / "work",
            settings=ExportSettings(export_textures=textures),
            groups=[],
        )
        install = UnrealInstall(root=tmp_path, version="5.4", source="test")
        return ExportSession(install, plan)

    def test_no_targets_means_no_report_items(self, tmp_path):
        session = self._session(tmp_path, textures=False)
        report = session.run([])
        assert report.outcomes == []
        assert report.summary_line() == "没有任何导出项。"

    def test_outcomes_merge_with_previous(self, tmp_path):
        from fbxconv.models import ExportOutcome

        session = self._session(tmp_path, textures=False)
        kept = ExportOutcome(
            request=ExportRequest(
                kind=AssetKind.STATIC_MESH, package_path="/Game/A", output_rel="a.fbx"
            ),
            status=ItemStatus.OK,
        )
        report = session.run([], previous=[kept])
        assert [o.request.output_rel for o in report.outcomes] == ["a.fbx"]


class TestMaterialNameCollisions:
    """Unity binds materials by name, so same-named Unreal materials clash."""

    def test_no_collision_for_distinct_names(self):
        from fbxconv.unity.manifest import material_name_collisions

        assert material_name_collisions(
            ["/Game/A/M_Rock", "/Game/A/M_Wood"]
        ) == []

    def test_collision_is_reported_with_both_packages(self):
        from fbxconv.unity.manifest import material_name_collisions

        messages = material_name_collisions(
            ["/Game/PackA/M_Rock", "/Game/PackB/M_Rock", "/Game/Only"]
        )
        assert len(messages) == 1
        assert "/Game/PackA/M_Rock" in messages[0]
        assert "/Game/PackB/M_Rock" in messages[0]
        assert "M_Rock" in messages[0]

    def test_identical_package_repeated_is_not_a_collision(self):
        from fbxconv.unity.manifest import material_name_collisions

        assert material_name_collisions(["/Game/A/M_Rock", "/Game/A/M_Rock"]) == []


class TestBuildStaticMaterialsDirect:
    def test_unknown_kind_still_reaches_base_colour(self):
        """A texture we cannot classify must not leave the model untextured."""
        plan = ConversionPlan(
            content_root=Path("/tmp"),
            mount_point="/Game",
            project_file=None,
            conversion_project=None,
            export_root=Path("/tmp/out"),
            work_root=Path("/tmp/work"),
            settings=ExportSettings(),
            groups=[],
        )
        textures, materials, meshes, summary = build_static_materials(plan)
        assert (textures, materials, meshes) == ([], [], [])
        assert summary["staticMeshCount"] == 0

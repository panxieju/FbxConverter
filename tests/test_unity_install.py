"""Tests for Unity-side artefact installation and project detection."""

from __future__ import annotations

import json
from pathlib import Path

from fbxconv.unity.manifest import (
    TESTS_DIRNAME,
    find_enclosing_project,
    install_unity_support,
    project_has_test_framework,
)


def _make_project(root: Path, *, manifest_deps: dict | None = None) -> Path:
    (root / "Assets").mkdir(parents=True)
    (root / "ProjectSettings").mkdir(parents=True)
    packages = root / "Packages"
    packages.mkdir(parents=True)
    (packages / "manifest.json").write_text(
        json.dumps({"dependencies": manifest_deps or {}}), encoding="utf-8"
    )
    return root


class TestFindEnclosingProject:
    def test_finds_project_root_from_nested_export(self, tmp_path: Path):
        project = _make_project(tmp_path / "MyGame")
        export = project / "Assets" / "UnityExport"
        export.mkdir(parents=True)
        assert find_enclosing_project(export) == project

    def test_returns_none_outside_a_project(self, tmp_path: Path):
        loose = tmp_path / "Loose" / "UnityExport"
        loose.mkdir(parents=True)
        assert find_enclosing_project(loose) is None


class TestProjectHasTestFramework:
    def test_detects_from_manifest(self, tmp_path: Path):
        project = _make_project(
            tmp_path / "P", manifest_deps={"com.unity.test-framework": "1.1.33"}
        )
        assert project_has_test_framework(project) is True

    def test_absent_from_manifest(self, tmp_path: Path):
        project = _make_project(tmp_path / "P")
        assert project_has_test_framework(project) is False

    def test_detects_from_packages_lock(self, tmp_path: Path):
        project = _make_project(tmp_path / "P")
        lock = project / "Packages" / "packages-lock.json"
        lock.write_text(
            json.dumps({"dependencies": {"com.unity.test-framework": {"version": "1.1.33"}}}),
            encoding="utf-8",
        )
        assert project_has_test_framework(project) is True

    def test_tolerates_missing_or_corrupt_files(self, tmp_path: Path):
        root = tmp_path / "Broken"
        (root / "Packages").mkdir(parents=True)
        (root / "Packages" / "manifest.json").write_text("{oops", encoding="utf-8")
        assert project_has_test_framework(root) is False


class TestInstallUnitySupport:
    def test_writes_importer_and_asmdef_into_export_tree(self, tmp_path: Path):
        export = tmp_path / "UnityExport"
        export.mkdir()

        written = install_unity_support(export)

        names = {p.name for p in written}
        assert "UnrealResourceImporter.cs" in names
        assert "FbxConverter.Editor.asmdef" in names
        assert "README.md" in names
        assert (export / "Editor" / "UnrealResourceImporter.cs").is_file()

    def test_skips_tests_when_no_test_framework(self, tmp_path: Path):
        project = _make_project(tmp_path / "MyGame")  # empty dependencies
        export = project / "Assets" / "UnityExport"
        export.mkdir(parents=True)

        install_unity_support(export)

        assert not (export / "Editor" / TESTS_DIRNAME).exists()

    def test_includes_tests_when_test_framework_present(self, tmp_path: Path):
        project = _make_project(
            tmp_path / "MyGame", manifest_deps={"com.unity.test-framework": "1.1.33"}
        )
        export = project / "Assets" / "UnityExport"
        export.mkdir(parents=True)

        install_unity_support(export)

        tests = export / "Editor" / TESTS_DIRNAME
        assert (tests / "UnrealImportTests.cs").is_file()
        assert (tests / "FbxConverter.Tests.asmdef").is_file()

    def test_does_not_double_install_inside_assets(self, tmp_path: Path):
        # Shipping the same C# twice makes Unity fail with CS0101.
        project = _make_project(tmp_path / "MyGame")
        export = project / "Assets" / "UnityExport"
        export.mkdir(parents=True)

        written = install_unity_support(export, unity_project=project)

        outside = [p for p in written if "Editor" / Path("FbxConverter") in p.parents]
        assert outside == []
        assert not (project / "Assets" / "Editor" / "FbxConverter").exists()

    def test_installs_into_project_when_export_is_external(self, tmp_path: Path):
        project = _make_project(tmp_path / "MyGame")
        export = tmp_path / "Elsewhere" / "UnityExport"
        export.mkdir(parents=True)

        written = install_unity_support(export, unity_project=project)

        installed = project / "Assets" / "Editor" / "FbxConverter"
        assert (installed / "UnrealResourceImporter.cs").is_file()
        assert any(installed in p.parents for p in written)

    def test_respects_overwrite_false(self, tmp_path: Path):
        export = tmp_path / "UnityExport"
        export.mkdir()
        install_unity_support(export)
        target = export / "Editor" / "UnrealResourceImporter.cs"
        target.write_text("// customised", encoding="utf-8")

        install_unity_support(export, overwrite=False)

        assert target.read_text(encoding="utf-8") == "// customised"

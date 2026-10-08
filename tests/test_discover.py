"""Filesystem discovery tests (spec section 2, first half)."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

import pytest

from fbxconv.discover import (
    discover,
    find_uproject,
    package_path_for,
    resolve_content_root,
)
from fbxconv.errors import ContentRootError


class TestResolveContentRoot:
    def test_project_folder_resolves_to_its_content_dir(self, tmp_path: Path):
        project = tmp_path / "MyGame"
        content = project / "Content"
        content.mkdir(parents=True)
        (project / "MyGame.uproject").write_text("{}", encoding="utf-8")

        info = resolve_content_root(project)

        assert info.path == content.resolve()
        assert info.was_derived

    def test_uproject_file_resolves_to_its_content_dir(self, tmp_path: Path):
        project = tmp_path / "MyGame"
        (project / "Content").mkdir(parents=True)
        uproject = project / "MyGame.uproject"
        uproject.write_text("{}", encoding="utf-8")

        assert resolve_content_root(uproject).path == (project / "Content").resolve()

    def test_content_dir_is_used_directly(self, tmp_path: Path):
        content = tmp_path / "Content"
        content.mkdir()
        info = resolve_content_root(content)
        assert info.path == content.resolve()
        assert not info.was_derived

    def test_subfolder_walks_up_to_content(self, tmp_path: Path):
        nested = tmp_path / "Content" / "Characters" / "Meshes"
        nested.mkdir(parents=True)

        info = resolve_content_root(nested)

        assert info.path == (tmp_path / "Content").resolve()
        assert info.was_derived

    def test_bare_folder_is_its_own_root(self, tmp_path: Path):
        pack = tmp_path / "MyAssetPack"
        pack.mkdir()
        info = resolve_content_root(pack)
        assert info.path == pack.resolve()
        assert not info.was_derived

    def test_missing_path_raises(self, tmp_path: Path):
        with pytest.raises(ContentRootError):
            resolve_content_root(tmp_path / "nope")

    def test_non_uproject_file_raises(self, tmp_path: Path):
        stray = tmp_path / "notes.txt"
        stray.write_text("hi", encoding="utf-8")
        with pytest.raises(ContentRootError):
            resolve_content_root(stray)


class TestFindUproject:
    def test_walks_up_from_content(self, content_root_with_project: Path):
        found = find_uproject(content_root_with_project)
        assert found is not None
        assert found.name == "MyGame.uproject"

    def test_returns_none_for_standalone_content(self, ue4_mannequin_tree: Path):
        # Guards the "independent conversion project" requirement (spec 6).
        assert find_uproject(ue4_mannequin_tree) is None


class TestPackagePath:
    @pytest.mark.parametrize(
        ("relative", "expected"),
        [
            ("SK_Mannequin.uasset", "/Game/SK_Mannequin"),
            ("Characters/SK_Mannequin.uasset", "/Game/Characters/SK_Mannequin"),
            ("A/B/C/Jog_Fwd.uasset", "/Game/A/B/C/Jog_Fwd"),
        ],
    )
    def test_maps_relative_paths(self, relative: str, expected: str):
        assert package_path_for(PurePosixPath(relative)) == expected

    def test_respects_custom_mount(self):
        assert (
            package_path_for(PurePosixPath("Meshes/SK_A.uasset"), "/MyPlugin")
            == "/MyPlugin/Meshes/SK_A"
        )


class TestDiscover:
    def test_finds_assets_and_companions(self, ue4_mannequin_tree: Path):
        result = discover(ue4_mannequin_tree)

        assert result.project_file is None
        names = {f.path.name for f in result.files}
        assert names == {
            "SK_Mannequin.uasset",
            "SK_Mannequin_Skeleton.uasset",
            "SK_Mannequin_PhysicsAsset.uasset",
            "Jog_Fwd.uasset",
        }

        mannequin = next(f for f in result.files if f.path.stem == "SK_Mannequin")
        assert mannequin.package_path == "/Game/Mannequin_UE4/Meshes/SK_Mannequin"
        assert {c.suffix for c in mannequin.companions} == {".uexp", ".ubulk"}

    def test_ignores_non_uasset_files(self, ue4_mannequin_tree: Path):
        (ue4_mannequin_tree / "readme.txt").write_text("x", encoding="utf-8")
        (ue4_mannequin_tree / "stray.uexp").write_bytes(b"\x00")
        result = discover(ue4_mannequin_tree)
        assert all(f.path.suffix == ".uasset" for f in result.files)

    def test_reports_cooked_archives_as_out_of_scope(self, ue4_mannequin_tree: Path):
        (ue4_mannequin_tree / "pakchunk0.pak").write_bytes(b"\x00")
        (ue4_mannequin_tree / "global.utoc").write_bytes(b"\x00")
        result = discover(ue4_mannequin_tree)
        assert {c.suffix for c in result.cooked_archives} == {".pak", ".utoc"}

    def test_empty_directory_yields_no_files(self, tmp_path: Path):
        empty = tmp_path / "Content"
        empty.mkdir()
        result = discover(empty)
        assert result.files == []

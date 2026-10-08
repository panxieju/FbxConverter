"""Engine selection and scan scoping.

Both behaviours came out of testing a real third-party pack:
the project declared 5.4 while the newest install was 5.8, and selecting
``Content/CLazyAnimpack`` was scanning the entire project.
"""

from __future__ import annotations

import json
from pathlib import Path

from fbxconv.discover import DiscoveryResult
from fbxconv.pipeline.scan import ScanSession
from fbxconv.unreal import locator
from fbxconv.unreal.locator import (
    UnrealInstall,
    find_install_for_version,
    project_engine_association,
    resolve_install_for_project,
)


def _install(root: str, version: str) -> UnrealInstall:
    return UnrealInstall(root=Path(root), version=version, source="test")


UE54 = _install(r"H:\UE_5.4", "5.4.4")
UE58 = _install(r"H:\UE_5.8", "5.8.2")


class TestProjectEngineAssociation:
    def test_reads_association(self, tmp_path: Path):
        uproject = tmp_path / "Escape.uproject"
        uproject.write_text(
            json.dumps({"FileVersion": 3, "EngineAssociation": "5.4"}), encoding="utf-8"
        )
        assert project_engine_association(uproject) == "5.4"

    def test_tolerates_utf8_bom(self, tmp_path: Path):
        uproject = tmp_path / "Escape.uproject"
        uproject.write_text(
            json.dumps({"EngineAssociation": "5.4"}), encoding="utf-8-sig"
        )
        assert project_engine_association(uproject) == "5.4"

    def test_missing_or_corrupt_yields_empty(self, tmp_path: Path):
        assert project_engine_association(tmp_path / "nope.uproject") == ""
        broken = tmp_path / "broken.uproject"
        broken.write_text("{not json", encoding="utf-8")
        assert project_engine_association(broken) == ""

    def test_missing_key_yields_empty(self, tmp_path: Path):
        uproject = tmp_path / "x.uproject"
        uproject.write_text(json.dumps({"FileVersion": 3}), encoding="utf-8")
        assert project_engine_association(uproject) == ""

    def test_source_build_guid_is_returned_verbatim(self, tmp_path: Path):
        uproject = tmp_path / "x.uproject"
        uproject.write_text(
            json.dumps({"EngineAssociation": "{A1B2C3}"}), encoding="utf-8"
        )
        assert project_engine_association(uproject) == "{A1B2C3}"


class TestFindInstallForVersion:
    def test_matches_major_minor(self):
        found = find_install_for_version("5.4", (UE58, UE54))
        assert found is UE54

    def test_matches_full_version_string(self):
        assert find_install_for_version("5.4.4", (UE58, UE54)) is UE54

    def test_none_when_not_installed(self):
        assert find_install_for_version("4.27", (UE58, UE54)) is None

    def test_none_for_guid(self):
        assert find_install_for_version("{A1B2C3}", (UE58, UE54)) is None

    def test_none_for_empty(self):
        assert find_install_for_version("", (UE58, UE54)) is None


class TestResolveInstallForProject:
    def test_prefers_the_projects_declared_engine(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(locator, "find_installs", lambda: (UE58, UE54))
        uproject = tmp_path / "Escape.uproject"
        uproject.write_text(json.dumps({"EngineAssociation": "5.4"}), encoding="utf-8")

        # Default choice is the newest; the project's declaration must win.
        assert resolve_install_for_project(uproject, UE58) is UE54

    def test_keeps_preferred_when_already_matching(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(locator, "find_installs", lambda: (UE58, UE54))
        uproject = tmp_path / "Escape.uproject"
        uproject.write_text(json.dumps({"EngineAssociation": "5.4"}), encoding="utf-8")

        assert resolve_install_for_project(uproject, UE54) is UE54

    def test_falls_back_when_declared_engine_absent(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(locator, "find_installs", lambda: (UE58, UE54))
        uproject = tmp_path / "Old.uproject"
        uproject.write_text(json.dumps({"EngineAssociation": "4.27"}), encoding="utf-8")

        assert resolve_install_for_project(uproject, UE58) is UE58

    def test_no_project_returns_preferred(self):
        assert resolve_install_for_project(None, UE58) is UE58


def _discovery(content_root: Path, original: Path) -> DiscoveryResult:
    return DiscoveryResult(
        content_root=content_root,
        original_root=original,
        project_file=content_root.parent / "Escape.uproject",
        files=[],
        cooked_archives=[],
        directories_scanned=0,
    )


class TestScanScope:
    """Selecting a subfolder must not scan the whole project."""

    def _session(self, tmp_path: Path) -> ScanSession:
        return ScanSession(UE54, tmp_path)

    def test_subfolder_becomes_a_scope_prefix(self, tmp_path: Path):
        content = tmp_path / "Content"
        pack = content / "CLazyAnimpack"
        pack.mkdir(parents=True)
        session = self._session(tmp_path)

        scope = session._resolve_scope(_discovery(content, pack))

        assert scope == "/Game/CLazyAnimpack"

    def test_selecting_the_content_root_scopes_nothing(self, tmp_path: Path):
        content = tmp_path / "Content"
        content.mkdir(parents=True)
        session = self._session(tmp_path)

        assert session._resolve_scope(_discovery(content, content)) == ""

    def test_deeply_nested_selection_keeps_full_relative_path(self, tmp_path: Path):
        content = tmp_path / "Content"
        nested = content / "Pack" / "Sub"
        nested.mkdir(parents=True)
        session = self._session(tmp_path)

        assert session._resolve_scope(_discovery(content, nested)) == "/Game/Pack/Sub"

    def test_unrelated_original_root_scopes_nothing(self, tmp_path: Path):
        content = tmp_path / "Content"
        content.mkdir(parents=True)
        elsewhere = tmp_path / "Other"
        elsewhere.mkdir()
        session = self._session(tmp_path)

        assert session._resolve_scope(_discovery(content, elsewhere)) == ""

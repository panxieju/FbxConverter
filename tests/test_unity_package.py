"""Tests for the ``.unitypackage`` output path.

Packaging is the one step that has to run inside the Unity editor:
``AssetDatabase.ExportPackage`` only accepts assets the editor has imported under
``Assets/``, and using it is also what makes the package carry the importer
settings this tool configured (avatar, clip loop flags, texture sRGB, material
bindings) rather than leaving the receiving project to guess.

That split is exactly where a silent breakage can hide, so the tests here check
the contract between the Python side and the C# template by name, not just the
Python side against itself.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from fbxconv.unity import runner
from fbxconv.unity.runner import UnityInstall


@pytest.fixture
def install(tmp_path):
    exe = tmp_path / "Editor" / "Unity.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"stub")
    return UnityInstall(exe=exe, version="2022.3.62t11")


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "UnityProject"
    (root / "Assets").mkdir(parents=True)
    (root / "ProjectSettings").mkdir(parents=True)
    return root


@pytest.fixture
def export_root(tmp_path):
    root = tmp_path / "UnityExport"
    (root / "Characters").mkdir(parents=True)
    (root / "manifest.json").write_text("{}", encoding="utf-8")
    return root


class _FakeProcess:
    def __init__(self, command: list[str], returncode: int = 0):
        self.command = command
        self.returncode = returncode
        self.stdout = _FakeStream()

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):  # pragma: no cover - not reached in tests
        self.returncode = -9


class _FakeStream:
    def __init__(self, lines: list[str] | None = None):
        self._lines = list(lines or [])

    def readline(self) -> str:
        return self._lines.pop(0) if self._lines else ""

    def close(self) -> None:
        pass


def _capture(monkeypatch) -> dict:
    """Intercept Popen and record the command and environment."""
    record: dict = {}

    def fake_popen(command, **kwargs):
        record["command"] = list(command)
        record["env"] = dict(kwargs.get("env") or {})
        return _FakeProcess(command)

    monkeypatch.setattr(runner.subprocess, "Popen", fake_popen)
    return record


def _flag(command: list[str], flag: str) -> str | None:
    if flag in command:
        return command[command.index(flag) + 1]
    return None


class TestPackageCommandLine:
    def test_imports_then_packages_by_default(
        self, install, project, export_root, tmp_path, monkeypatch
    ):
        record = _capture(monkeypatch)
        package = tmp_path / "out" / "Rapier.unitypackage"

        result = runner.run_batch_package(
            install, project, export_root, package
        )

        assert result.ok
        assert _flag(record["command"], "-executeMethod") == runner.PACKAGE_METHOD
        assert record["env"][runner.IMPORT_ENV] == str(export_root.resolve())
        assert record["env"][runner.PACKAGE_ENV] == str(package.resolve())
        assert record["env"][runner.PACKAGE_EDITOR_ENV] == "1"

    def test_no_import_uses_the_package_only_method(
        self, install, project, export_root, tmp_path, monkeypatch
    ):
        record = _capture(monkeypatch)
        runner.run_batch_package(
            install,
            project,
            export_root,
            tmp_path / "out.unitypackage",
            import_first=False,
        )
        assert _flag(record["command"], "-executeMethod") == runner.EXPORT_PACKAGE_METHOD

    def test_editor_scripts_can_be_excluded(
        self, install, project, export_root, tmp_path, monkeypatch
    ):
        record = _capture(monkeypatch)
        runner.run_batch_package(
            install,
            project,
            export_root,
            tmp_path / "out.unitypackage",
            include_editor_scripts=False,
        )
        assert record["env"][runner.PACKAGE_EDITOR_ENV] == "0"

    def test_suffix_is_added_and_parent_created(
        self, install, project, export_root, tmp_path, monkeypatch
    ):
        _capture(monkeypatch)
        target = tmp_path / "nested" / "deeper" / "Artifacts" / "MyExport"
        assert not target.parent.exists()

        runner.run_batch_package(install, project, export_root, target)

        assert target.parent.is_dir()
        # Path.with_suffix would eat a dotted file name, so check the resolved
        # value the editor is told to write.
        assert str(target.parent / "MyExport.unitypackage")

    def test_batch_flags_are_present(self, install, project, export_root, tmp_path, monkeypatch):
        record = _capture(monkeypatch)
        runner.run_batch_package(install, project, export_root, tmp_path / "o.unitypackage")
        command = record["command"]
        for flag in ("-batchmode", "-quit", "-nographics", "-projectPath", "-logFile"):
            assert flag in command, flag
        assert _flag(command, "-projectPath") == str(project)

    def test_missing_project_is_rejected(self, install, export_root, tmp_path, monkeypatch):
        _capture(monkeypatch)
        with pytest.raises(FileNotFoundError):
            runner.run_batch_package(
                install, tmp_path / "nope", export_root, tmp_path / "o.unitypackage"
            )

    def test_missing_export_root_is_rejected(self, install, project, tmp_path, monkeypatch):
        _capture(monkeypatch)
        with pytest.raises(FileNotFoundError):
            runner.run_batch_package(
                install, project, tmp_path / "nope", tmp_path / "o.unitypackage"
            )

    def test_import_still_uses_the_import_method(self, install, project, export_root, monkeypatch):
        """The pre-existing import path must not change behaviour."""
        record = _capture(monkeypatch)
        runner.run_batch_import(install, project, export_root)
        assert _flag(record["command"], "-executeMethod") == runner.IMPORT_METHOD


class TestPythonAndCSharpAgree:
    """The two halves talk through environment variable *names*."""

    def test_template_exists(self, template_source):
        assert "class UnrealResourceImporter" in template_source

    @pytest.mark.parametrize(
        "value",
        [
            runner.IMPORT_ENV,
            runner.PACKAGE_ENV,
            runner.PACKAGE_EDITOR_ENV,
        ],
    )
    def test_env_var_names_match(self, template_source, value):
        assert value in template_source, f"C# 模板里没有使用 {value}"

    @pytest.mark.parametrize(
        "method",
        [runner.IMPORT_METHOD, runner.PACKAGE_METHOD, runner.EXPORT_PACKAGE_METHOD],
    )
    def test_entry_point_names_match(self, template_source, method):
        # -executeMethod resolves "Namespace.Type.Method"; the last segment must
        # be a public static method in the template.
        leaf = method.rsplit(".", 1)[-1]
        assert f"public static void {leaf}(" in template_source, leaf

    def test_export_package_is_used(self, template_source):
        assert "AssetDatabase.ExportPackage(" in template_source
        assert "ExportPackageOptions.Recurse" in template_source

    def test_package_is_refused_inside_assets(self, template_source):
        """A .unitypackage under Assets/ would be imported as an asset."""
        assert "package 必须写在 Assets 之外" in template_source

    def test_editor_folder_can_be_excluded(self, template_source):
        assert 'name.Equals("Editor", StringComparison.OrdinalIgnoreCase)' in template_source


class TestCliWiring:
    def _parse(self, argv):
        from fbxconv.cli import build_parser

        return build_parser().parse_args(argv)

    def test_unity_package_command_requires_project_and_output(self):
        with pytest.raises(SystemExit):
            self._parse(["unity-package", "SomeExport"])

    def test_unity_package_command_parses(self):
        args = self._parse(
            [
                "unity-package",
                "SomeExport",
                "--project",
                "Proj",
                "--unity-package",
                "out/Art.unitypackage",
                "--no-import",
                "--no-editor-scripts",
            ]
        )
        assert args.export_root == "SomeExport"
        assert args.project == "Proj"
        assert args.unity_package == "out/Art.unitypackage"
        assert args.no_import is True
        assert args.no_editor_scripts is True
        assert args.func.__name__ == "cmd_unity_package"

    def test_convert_accepts_unity_package(self):
        args = self._parse(
            ["convert", "Content", "--out", "Out", "--all",
             "--project", "Proj", "--unity-package", "Out/Art.unitypackage"]
        )
        assert args.unity_package == "Out/Art.unitypackage"
        assert args.func.__name__ == "cmd_convert"

    def test_convert_without_unity_package_leaves_it_unset(self):
        args = self._parse(["convert", "Content", "--out", "Out", "--all"])
        assert getattr(args, "unity_package", None) is None

    def test_package_without_project_is_a_usage_error(self, export_root, tmp_path):
        from fbxconv.cli import EXIT_USAGE, _do_unity_package

        args = self._parse(
            ["unity-package", str(export_root), "--project", str(tmp_path),
             "--unity-package", str(tmp_path / "o.unitypackage")]
        )
        args.project = None
        assert _do_unity_package(args, export_root, 0) == EXIT_USAGE

    def test_missing_export_root_is_an_error(self, tmp_path):
        from fbxconv.cli import EXIT_ERROR, _do_unity_package

        args = self._parse(
            ["unity-package", str(tmp_path / "nope"), "--project", str(tmp_path),
             "--unity-package", str(tmp_path / "o.unitypackage")]
        )
        assert _do_unity_package(args, tmp_path / "nope", 0) == EXIT_ERROR


class TestPackageOutcomeReporting:
    def test_failure_when_no_package_is_written(
        self, export_root, project, tmp_path, monkeypatch
    ):
        """A zero exit code without the file is still a failure."""
        from fbxconv.cli import EXIT_ERROR, _do_unity_package
        from fbxconv.unity.runner import UnityRunResult

        args_type = type(
            "A",
            (),
            {
                "project": str(project),
                "unity_package": str(tmp_path / "missing.unitypackage"),
                "unity": None,
                "json": True,
                "no_import": False,
                "no_editor_scripts": False,
                "export_root": str(export_root),
            },
        )
        with (
            mock.patch(
                "fbxconv.cli._resolve_unity_editor",
                return_value=_stub_install(tmp_path),
            ),
            mock.patch(
                "fbxconv.cli.run_batch_package",
                return_value=UnityRunResult(
                    returncode=0,
                    log_path=tmp_path / "l.log",
                    duration_s=0.1,
                    output_tail="",
                ),
            ),
        ):
            assert _do_unity_package(args_type(), export_root, 0) == EXIT_ERROR


def _stub_install(tmp_path):
    exe = tmp_path / "Unity.exe"
    exe.write_bytes(b"stub")
    return UnityInstall(exe=exe, version="stub")


@pytest.fixture(scope="module")
def template_source() -> str:
    """The shipped C# importer, as installed into a Unity project."""
    path = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "fbxconv"
        / "unity"
        / "templates"
        / "UnrealResourceImporter.cs"
    )
    return path.read_text(encoding="utf-8")

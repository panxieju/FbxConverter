"""Command line interface.

The GUI and the CLI share the same pipeline, so anything the wizard can do is
scriptable -- which is also how the acceptance stages in the spec are verified.

Examples::

    fbxconv unreal-list
    fbxconv scan  H:/Packs/MyPack/Content --json
    fbxconv convert H:/Packs/MyPack/Content --out G:/Out --all
    fbxconv convert H:/Proj/Content --out G:/Out --group Mannequin_UE4 --import-type Humanoid
    fbxconv unity-import G:/Out/UnityExport --project G:/MyUnityProject
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from . import APP_DISPLAY_NAME, __version__
from .config import AppConfig
from .errors import FbxConvError
from .logutil import configure_logging, get_logger
from .models import (
    ExportSettings,
    GroupStatus,
    ImportType,
    LoopMode,
    RootMotionMode,
    ScanResult,
    SelectedGroup,
)
from .pipeline.export import ExportSession, build_plan
from .pipeline.scan import ScanSession, group_output_name
from .unity.runner import find_unity_editors, run_batch_import
from .unreal.locator import UnrealInstall, find_default_install, find_installs, resolve_editor
from .unreal.project import MountMode

_log = get_logger("cli")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _reconfigure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError):
            stream.reconfigure(encoding="utf-8", errors="replace")


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("content_root", help="Unreal 资源目录（Content 文件夹或项目目录）")
    parser.add_argument("--out", required=True, metavar="DIR", help="输出目录（生成 UnityExport）")
    parser.add_argument("--unreal", metavar="PATH", help="UnrealEditor-Cmd.exe 或引擎根目录")
    parser.add_argument("--work", metavar="DIR", help="工作目录（默认 <out>/.fbxconv）")
    parser.add_argument(
        "--link-mode",
        choices=[m.value for m in MountMode],
        default=MountMode.JUNCTION.value,
        help="独立转换项目挂载资源的方式（默认 junction）",
    )
    parser.add_argument("--no-nullrhi", action="store_true", help="禁用 -nullrhi（排查渲染相关问题时使用）")
    parser.add_argument("--verbose", "-v", action="store_true", help="输出调试日志")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果")


def _add_settings(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--import-type",
        choices=[t.value for t in ImportType],
        default=ImportType.GENERIC.value,
        help="Unity 导入类型",
    )
    parser.add_argument("--sampling-rate", type=int, default=30, help="动画采样率")
    parser.add_argument(
        "--loop",
        choices=[m.value for m in LoopMode],
        default=LoopMode.AUTO.value,
        help="循环模式：auto 按名称推断",
    )
    parser.add_argument(
        "--root-motion",
        choices=[m.value for m in RootMotionMode],
        default=RootMotionMode.KEEP.value,
        help="Root Motion 处理方式",
    )
    parser.add_argument("--scale", type=float, default=1.0, help="Unity 导入缩放")
    parser.add_argument("--no-materials", action="store_true", help="不导入材质槽")


def _install_from_args(args: argparse.Namespace) -> UnrealInstall:
    if getattr(args, "unreal", None):
        install = resolve_editor(args.unreal)
        if install is None:
            raise FbxConvError(
                f"无法识别的 Unreal 路径：{args.unreal}（请指向 UnrealEditor-Cmd.exe 或引擎根目录）"
            )
        return install
    config = AppConfig.load()
    if config.unreal_editor is not None:
        install = resolve_editor(config.unreal_editor)
        if install is not None:
            return install
    return find_default_install()


def _settings_from_args(args: argparse.Namespace) -> ExportSettings:
    return ExportSettings(
        import_type=ImportType(args.import_type),
        sampling_rate=int(args.sampling_rate),
        loop_mode=LoopMode(args.loop),
        root_motion=RootMotionMode(args.root_motion),
        scale=float(args.scale),
        export_materials=not args.no_materials,
    )


def _work_root(args: argparse.Namespace) -> Path:
    if getattr(args, "work", None):
        return Path(args.work)
    return Path(args.out) / ".fbxconv"


def _select_groups(
    scan: ScanResult, names: Sequence[str], take_all: bool
) -> list[SelectedGroup]:
    """Resolve ``--group`` filters (substring, case-insensitive) into a selection."""
    selected: list[SelectedGroup] = []
    for group in scan.groups:
        output_name = group_output_name(group)
        if group.status is GroupStatus.MISSING_DEPS:
            _log.warning("跳过 %s：存在缺失依赖", output_name)
            continue
        if not group.meshes:
            _log.warning("跳过 %s：没有参考网格", output_name)
            continue
        if not take_all and names:
            lowered = [n.lower() for n in names]
            haystack = f"{output_name} {group.display_name}".lower()
            if not any(n in haystack for n in lowered):
                continue
        elif not take_all and not names:
            continue
        selected.append(
            SelectedGroup(
                group=group, mesh=group.reference_mesh, animations=list(group.animations)
            )
        )
    return selected


def _print_scan(scan: ScanResult) -> None:
    print(f"内容根目录 : {scan.content_root}")
    print(f"挂载点     : {scan.mount_point}")
    if scan.scope_prefix:
        print(f"扫描范围   : {scan.scope_prefix}（依赖仍在整个挂载点内解析）")
    print(f"Unreal 项目: {scan.project_file or '（无，已创建独立转换项目）'}")
    if scan.conversion_project:
        print(f"转换项目   : {scan.conversion_project}")
    print(f"引擎版本   : {scan.engine_version}")
    print(f"资源文件   : {scan.file_count} 个（确认 {len(scan.assets)} 个）")
    for warning in scan.warnings:
        print(f"  警告: {warning}")
    print()
    print(f"{'骨架组':<40}{'参考网格':<24}{'动画':>5}  {'状态':<10}输出目录")
    print("-" * 100)
    for group in scan.groups:
        mesh = group.reference_mesh.name if group.reference_mesh else "-"
        print(
            f"{(group.display_name or '-')[:39]:<40}{mesh[:23]:<24}"
            f"{group.animation_count:>5}  {group.status.label:<10}{group_output_name(group)}"
        )
        if group.missing_dependencies:
            for dep in group.missing_dependencies[:5]:
                print(f"      缺失: {dep}")


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


def cmd_unreal_list(args: argparse.Namespace) -> int:
    installs = find_installs()
    if args.json:
        print(json.dumps([i.to_payload() for i in installs], indent=2, ensure_ascii=False))
        return EXIT_OK
    if not installs:
        print("未找到可用的 Unreal Engine 安装。")
        print("可设置环境变量 FBXCONV_UNREAL_EDITOR 指向 UnrealEditor-Cmd.exe。")
        return EXIT_ERROR
    print(f"找到 {len(installs)} 个 Unreal 安装：")
    for install in installs:
        print(f"  {install.label:<24} {install.root}")
        print(f"  {'':<24} {install.editor_cmd}")
    return EXIT_OK


def cmd_unity_list(args: argparse.Namespace) -> int:
    editors = find_unity_editors()
    if args.json:
        print(json.dumps([e.to_payload() for e in editors], indent=2, ensure_ascii=False))
        return EXIT_OK
    if not editors:
        print("未找到 Unity / 团结引擎 安装。")
        print("可设置环境变量 FBXCONV_UNITY_EDITOR 指向 Unity.exe 或 Tuanjie.exe。")
        return EXIT_ERROR
    print(f"找到 {len(editors)} 个 Unity/团结引擎 编辑器：")
    for editor in editors:
        print(f"  {editor.label:<28} {editor.exe}")
    return EXIT_OK


def cmd_scan(args: argparse.Namespace) -> int:
    install = _install_from_args(args)
    session = ScanSession(
        install,
        _work_root(args),
        mount_mode=args.link_mode,
        null_rhi=not args.no_nullrhi,
        on_progress=None if args.json else lambda msg, frac: _log.info("%s", msg),
    )
    scan = session.run(args.content_root)

    if args.json:
        print(
            json.dumps(
                {
                    "content_root": str(scan.content_root),
                    "mount_point": scan.mount_point,
                    "project_file": str(scan.project_file) if scan.project_file else None,
                    "conversion_project": str(scan.conversion_project)
                    if scan.conversion_project
                    else None,
                    "engine_version": scan.engine_version,
                    "unreal_editor": scan.unreal_editor,
                    "scope_prefix": scan.scope_prefix,
                    "file_count": scan.file_count,
                    "warnings": scan.warnings,
                    "groups": [
                        {
                            "display_name": g.display_name,
                            "output_name": group_output_name(g),
                            "status": g.status.value,
                            "meshes": [m.name for m in g.meshes],
                            "animation_count": g.animation_count,
                            "missing_dependencies": g.missing_dependencies,
                        }
                        for g in scan.groups
                    ],
                },
                indent=2,
                ensure_ascii=False,
            )
        )
    else:
        _print_scan(scan)
    return EXIT_OK


def cmd_convert(args: argparse.Namespace) -> int:
    install = _install_from_args(args)
    work = _work_root(args)
    settings = _settings_from_args(args)

    scan_session = ScanSession(
        install,
        work,
        mount_mode=args.link_mode,
        null_rhi=not args.no_nullrhi,
        on_progress=None if args.json else lambda msg, frac: _log.info("%s", msg),
    )
    scan = scan_session.run(args.content_root)

    selection = _select_groups(scan, args.group or [], args.all)
    if not selection:
        _log.error("没有选中的骨架组。使用 --all 或 --group <名称>。")
        if not args.json:
            _print_scan(scan)
        return EXIT_ERROR

    plan = build_plan(
        scan,
        selection,
        settings,
        export_root=Path(args.out) / "UnityExport",
        work_root=work,
        unity_project=Path(args.unity_project) if args.unity_project else None,
    )

    report = ExportSession(
        # Reuse the engine the scan settled on: a project declaring 5.4 must
        # not be exported with a newer editor.
        scan_session.install,
        plan,
        on_progress=None if args.json else lambda msg, frac: _log.info("%s", msg),
        on_event=None,
    ).run()

    if args.json:
        print(json.dumps(report.to_payload(), indent=2, ensure_ascii=False))
    else:
        print()
        print(report.summary_line())
        print(f"输出目录: {report.unity_export_root}")
        for outcome in report.failed:
            print(f"  失败 {outcome.request.display_name}: {outcome.error}")
        if report.retryable:
            print(f"  可用相同命令重试 {len(report.retryable)} 个失败项")

    if args.unity_import:
        return _do_unity_import(args, Path(args.out) / "UnityExport", report.failure_count)

    return EXIT_OK if report.failure_count == 0 else EXIT_ERROR


def cmd_unity_import(args: argparse.Namespace) -> int:
    return _do_unity_import(args, Path(args.export_root), 0)


def _do_unity_import(args: argparse.Namespace, export_root: Path, prior_failures: int) -> int:
    if not args.project:
        _log.error("--project 是必须的：请指定 Unity/团结引擎 项目目录。")
        return EXIT_USAGE

    editors = find_unity_editors()
    editor = None
    if getattr(args, "unity", None):
        candidate = Path(args.unity)
        for found in editors:
            if found.exe == candidate:
                editor = found
                break
        if editor is None and candidate.is_file():
            from .unity.runner import UnityInstall

            editor = UnityInstall(exe=candidate, version="")
    elif editors:
        editor = editors[0]

    if editor is None:
        _log.error("未找到 Unity/团结引擎 编辑器，请使用 --unity 指定。")
        return EXIT_ERROR

    result = run_batch_import(
        editor,
        args.project,
        export_root,
        on_output=None if args.json else (lambda line: _log.debug("%s", line)),
    )

    if args.json:
        print(
            json.dumps(
                {
                    "editor": editor.to_payload(),
                    "project": str(args.project),
                    "export_root": str(export_root),
                    "returncode": result.returncode,
                    "log_path": str(result.log_path),
                    "duration_seconds": round(result.duration_s, 2),
                },
                indent=2,
                ensure_ascii=False,
            )
        )
    else:
        print(f"编辑器   : {editor.label}")
        print(f"项目     : {args.project}")
        print(f"退出码   : {result.returncode}")
        print(f"日志     : {result.log_path}")
        report = Path(export_root) / "Reports" / "unity_import_report.json"
        if report.is_file():
            print(f"校验报告 : {report}")

    if not result.ok or prior_failures:
        return EXIT_ERROR
    return EXIT_OK


# --------------------------------------------------------------------------- #
# Parser
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fbxconv",
        description=f"{APP_DISPLAY_NAME} —— 将 Unreal 骨骼网格与动画转换为 Unity 可用的 FBX 包",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"fbxconv {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    p = sub.add_parser("unreal-list", help="列出可用的 Unreal 安装")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_unreal_list)

    p = sub.add_parser("unity-list", help="列出可用的 Unity/团结引擎 编辑器")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_unity_list)

    p = sub.add_parser("scan", help="扫描资源目录并确认类型与依赖")
    _add_common(p)
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("convert", help="扫描并导出 FBX（主命令）")
    _add_common(p)
    _add_settings(p)
    p.add_argument("--group", action="append", metavar="NAME", help="按名称选择骨架组（可重复）")
    p.add_argument("--all", action="store_true", help="转换所有可用的骨架组")
    p.add_argument("--unity-project", metavar="DIR", help="同时把 Editor 脚本安装到该项目")
    p.add_argument("--unity-import", action="store_true", help="导出后立即执行 Unity 批处理导入")
    p.add_argument("--project", metavar="DIR", help="配合 --unity-import 的 Unity 项目目录")
    p.add_argument("--unity", metavar="PATH", help="Unity.exe / Tuanjie.exe 路径")
    p.set_defaults(func=cmd_convert)

    p = sub.add_parser("unity-import", help="在 Unity/团结引擎 中批量应用导入设置并校验")
    p.add_argument("export_root", metavar="EXPORT_ROOT", help="UnityExport 目录")
    p.add_argument("--project", required=True, metavar="DIR", help="Unity/团结引擎 项目目录")
    p.add_argument("--unity", metavar="PATH", help="Unity.exe / Tuanjie.exe 路径")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_unity_import)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    _reconfigure_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)

    if not getattr(args, "command", None):
        parser.print_help()
        return EXIT_USAGE

    configure_logging(logging.DEBUG if getattr(args, "verbose", False) else logging.INFO)

    try:
        return int(args.func(args))
    except FbxConvError as exc:
        _log.error("%s", exc)
        return EXIT_ERROR
    except KeyboardInterrupt:
        _log.error("已取消。")
        return EXIT_ERROR


__all__ = ["build_parser", "main"]

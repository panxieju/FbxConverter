"""Locate and drive the Unity / Tuanjie editor in batch mode."""

from __future__ import annotations

import json
import os
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from ..logutil import get_logger

_log = get_logger("unity.runner")

#: Executable names, newest hub layout first. Tuanjie is Unity China's fork and
#: ships the same editor with a different binary name.
EXE_NAMES = ("Unity.exe", "Tuanjie.exe")
EXE_NAME_SET = {name.lower() for name in EXE_NAMES}

IMPORT_METHOD = "FbxConverter.Editor.UnrealResourceImporter.ImportFromCommandLine"
IMPORT_ENV = "FBXCONV_UNITY_EXPORT"

DEFAULT_TIMEOUT = 3600.0

OutputCallback = Callable[[str], None]


@dataclass(frozen=True)
class UnityInstall:
    """A Unity-family editor installation."""

    exe: Path
    version: str

    @property
    def editor_root(self) -> Path:
        return self.exe.parent

    @property
    def product(self) -> str:
        return "Tuanjie" if self.exe.name.lower().startswith("tuanjie") else "Unity"

    @property
    def label(self) -> str:
        return f"{self.product} {self.version}" if self.version else f"{self.product} ({self.exe})"

    def to_payload(self) -> dict[str, str]:
        return {
            "exe": str(self.exe),
            "version": self.version,
            "product": self.product,
        }


def _hub_roots() -> list[Path]:
    """Install roots reported by the Unity Hub and Tuanjie Hub configs."""
    roots: list[Path] = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        for hub in ("UnityHub", "TuanjieHub"):
            base = Path(appdata) / hub
            config = base / "secondaryInstallPath.json"
            try:
                raw = config.read_text(encoding="utf-8").strip()
            except OSError:
                continue
            # The file holds a bare JSON string, e.g. "G:\\Program Files\\UnityHub"
            try:
                value = json.loads(raw)
            except json.JSONDecodeError:
                value = raw.strip('"')
            if isinstance(value, str) and value:
                roots.append(Path(value))
    roots.extend(
        [
            Path(r"C:\Program Files\Unity\Hub\Editor"),
            Path(r"C:\Program Files\UnityHub"),
            Path(r"C:\Program Files\Unity"),
            Path(r"C:\Program Files\Tuanjie\Hub\Editor"),
            Path(r"C:\Program Files\TuanjieHub"),
        ]
    )
    for letter in "DEFGH":
        roots.extend(
            [
                Path(f"{letter}:/Program Files/Unity/Editor"),
                Path(f"{letter}:/Program Files/UnityHub"),
                Path(f"{letter}:/Program Files/Unity/Hub/Editor"),
                Path(f"{letter}:/Unity/Hub/Editor"),
                Path(f"{letter}:/Program Files/Tuanjie/Hub/Editor"),
            ]
        )
    return roots


def _editor_from_root(root: Path) -> list[UnityInstall]:
    """Collect editors under one install root (``<root>/<version>/Editor/*.exe``)."""
    found: list[UnityInstall] = []
    if not root.is_dir():
        return found
    try:
        candidates = [root] + [p for p in root.iterdir() if p.is_dir()]
    except OSError:  # pragma: no cover
        return found
    for candidate in candidates:
        editor_dir = candidate if candidate.name == "Editor" else candidate / "Editor"
        if not editor_dir.is_dir():
            continue
        for name in EXE_NAMES:
            exe = editor_dir / name
            if exe.is_file():
                version = candidate.name if candidate.name != "Editor" else ""
                found.append(UnityInstall(exe=exe, version=version))
    return found


def find_unity_editors() -> list[UnityInstall]:
    """All discoverable Unity/Tuanjie editors, newest-looking first."""
    override = os.environ.get("FBXCONV_UNITY_EDITOR")
    installs: list[UnityInstall] = []

    if override:
        candidate = Path(override)
        if candidate.is_file() and candidate.name.lower() in EXE_NAME_SET:
            installs.append(UnityInstall(exe=candidate, version=""))
        elif candidate.is_dir():
            installs.extend(_editor_from_root(candidate))

    seen: set[Path] = {i.exe for i in installs}
    for root in _hub_roots():
        for install in _editor_from_root(root):
            if install.exe in seen:
                continue
            seen.add(install.exe)
            installs.append(install)

    installs.sort(key=lambda i: _version_key(i.version), reverse=True)
    _log.debug("找到 %d 个 Unity/Tuanjie 编辑器", len(installs))
    return installs


def _version_key(version: str) -> tuple:
    parts = []
    current = ""
    for char in version or "":
        if char.isdigit():
            current += char
        elif current:
            parts.append(int(current))
            current = ""
    if current:
        parts.append(int(current))
    return tuple(parts) if parts else (0,)


@dataclass
class UnityRunResult:
    returncode: int
    log_path: Path
    duration_s: float
    output_tail: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def run_batch_import(
    install: UnityInstall,
    project_path: str | Path,
    export_root: str | Path,
    *,
    log_path: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    on_output: OutputCallback | None = None,
    extra_args: Sequence[str] = (),
) -> UnityRunResult:
    """Run the C# importer inside ``project_path`` in batch mode."""
    project = Path(project_path)
    if not project.is_dir():
        raise FileNotFoundError(f"Unity 项目不存在：{project}")

    export = Path(export_root).resolve()
    if not export.is_dir():
        raise FileNotFoundError(f"导出目录不存在：{export}")

    if log_path is None:
        log_path = project / "Logs" / "fbxconv-import.log"
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    command = [
        str(install.exe),
        "-batchmode",
        "-quit",
        "-nographics",
        "-projectPath",
        str(project),
        "-executeMethod",
        IMPORT_METHOD,
        "-logFile",
        str(log_path),
        *extra_args,
    ]

    env = os.environ.copy()
    env[IMPORT_ENV] = str(export)

    _log.info("启动 %s 批处理导入：%s", install.label, project.name)
    started = time.monotonic()
    tail: list[str] = []

    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        creationflags=creationflags,
    )
    try:
        assert process.stdout is not None
        for line in iter(process.stdout.readline, ""):
            text = line.rstrip("\r\n")
            if not text:
                continue
            tail.append(text)
            del tail[:-60]
            if on_output is not None:
                on_output(text)
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _log.error("Unity 批处理超时，正在终止")
        process.kill()
        process.wait()
    finally:
        if process.stdout is not None:
            process.stdout.close()

    duration = time.monotonic() - started
    result = UnityRunResult(
        returncode=process.returncode if process.returncode is not None else -1,
        log_path=log_path,
        duration_s=duration,
        output_tail="\n".join(tail),
    )
    _log.info(
        "Unity 批处理结束：退出码 %s，用时 %.1fs", result.returncode, duration
    )
    return result


__all__ = [
    "IMPORT_ENV",
    "IMPORT_METHOD",
    "UnityInstall",
    "UnityRunResult",
    "find_unity_editors",
    "run_batch_import",
]

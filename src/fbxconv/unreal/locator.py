"""Locate usable Unreal Engine installations on this machine.

Sources, in order of trust:

1. ``FBXCONV_UNREAL_EDITOR`` environment variable (an exact executable path)
2. ``HKLM\\SOFTWARE\\EpicGames\\Unreal Engine\\<version>`` -> ``InstalledDirectory``
3. ``HKCU\\SOFTWARE\\Epic Games\\Unreal Engine\\Builds`` (source/custom builds)
4. ``%ProgramData%\\Epic\\UnrealEngineLauncher\\LauncherInstalled.dat``
5. A bounded scan of ``<drive>:\\<dir>`` for ``UE_*`` / ``UnrealEngine*`` folders

Every candidate is validated by checking that ``UnrealEditor-Cmd.exe`` actually
exists, which discards stale registry entries left behind by uninstalled
engines.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from ..errors import UnrealNotFoundError
from ..logutil import get_logger

_log = get_logger("unreal.locator")

_EDITOR_CMD_REL = Path("Engine/Binaries/Win64/UnrealEditor-Cmd.exe")
_EDITOR_REL = Path("Engine/Binaries/Win64/UnrealEditor.exe")
_BUILD_VERSION_REL = Path("Engine/Build/Build.version")

_VERSION_DIR_RE = re.compile(r"^(?:UE_|UnrealEngine)(\d+\.\d+)", re.IGNORECASE)
_DRIVE_GLOB_PATTERNS = ("UE_*", "UnrealEngine*", "Epic Games/UE_*")


@dataclass(frozen=True)
class UnrealInstall:
    """A validated Unreal Engine installation."""

    root: Path
    version: str
    source: str

    @property
    def editor_cmd(self) -> Path:
        return self.root / _EDITOR_CMD_REL

    @property
    def editor_exe(self) -> Path | None:
        candidate = self.root / _EDITOR_REL
        return candidate if candidate.is_file() else None

    @property
    def label(self) -> str:
        return f"Unreal Engine {self.version}" if self.version else str(self.root)

    def to_payload(self) -> dict[str, str]:
        return {
            "root": str(self.root),
            "version": self.version,
            "editor_cmd": str(self.editor_cmd),
            "source": self.source,
        }

    def __str__(self) -> str:  # pragma: no cover - display only
        return f"{self.label} ({self.root})"


def _version_key(version: str) -> tuple[int, ...]:
    parts = re.findall(r"\d+", version)
    return tuple(int(p) for p in parts) if parts else (0,)


def _read_build_version(root: Path) -> str:
    """Read the authoritative version from ``Engine/Build/Build.version``."""
    candidate = root / _BUILD_VERSION_REL
    try:
        payload = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    major = payload.get("MajorVersion")
    minor = payload.get("MinorVersion")
    patch = payload.get("PatchVersion")
    if major is None or minor is None:
        return ""
    version = f"{major}.{minor}"
    if patch is not None:
        version += f".{patch}"
    return version


def _install_from_root(root: Path, source: str) -> UnrealInstall | None:
    """Build a validated install, or ``None`` if the editor binary is absent."""
    try:
        root = root.expanduser().resolve()
    except OSError:  # pragma: no cover
        return None
    if not (root / _EDITOR_CMD_REL).is_file():
        _log.debug("忽略候选（缺少 UnrealEditor-Cmd.exe）：%s", root)
        return None
    version = _read_build_version(root)
    if not version:
        match = _VERSION_DIR_RE.match(root.name)
        version = match.group(1) if match else ""
    return UnrealInstall(root=root, version=version, source=source)


def _registry_candidates() -> list[tuple[Path, str]]:
    out: list[tuple[Path, str]] = []
    if os.name != "nt":
        return out
    try:
        import winreg
    except ImportError:  # pragma: no cover - non-Windows
        return out

    def _open(root: int, path: str):
        try:
            return winreg.OpenKey(root, path)
        except OSError:
            return None

    # Installed launcher builds.
    key = _open(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\EpicGames\Unreal Engine")
    if key is not None:
        with key:
            index = 0
            while True:
                try:
                    sub_name = winreg.EnumKey(key, index)
                except OSError:
                    break
                index += 1
                sub = _open(
                    winreg.HKEY_LOCAL_MACHINE,
                    rf"SOFTWARE\EpicGames\Unreal Engine\{sub_name}",
                )
                if sub is None:
                    continue
                with sub:
                    try:
                        installed, _ = winreg.QueryValueEx(sub, "InstalledDirectory")
                    except OSError:
                        continue
                    if installed:
                        out.append((Path(installed), f"registry:{sub_name}"))

    # Custom / source builds registered by the user.
    builds = _open(winreg.HKEY_CURRENT_USER, r"SOFTWARE\Epic Games\Unreal Engine\Builds")
    if builds is not None:
        with builds:
            index = 0
            while True:
                try:
                    name, value, _ = winreg.EnumValue(builds, index)
                except OSError:
                    break
                index += 1
                if not value:
                    continue
                path = Path(value)
                # Custom builds may register the .uproject or the engine root.
                if path.suffix.lower() == ".uproject":
                    path = path.parent
                out.append((path, f"registry-build:{name}"))
    return out


def _launcher_candidates() -> list[tuple[Path, str]]:
    out: list[tuple[Path, str]] = []
    program_data = os.environ.get("PROGRAMDATA")
    if not program_data:
        return out
    manifest = Path(program_data) / "Epic" / "UnrealEngineLauncher" / "LauncherInstalled.dat"
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return out
    for entry in payload.get("InstallationList", []) or []:
        location = entry.get("InstallLocation")
        app_name = entry.get("AppName", "")
        if not location:
            continue
        # Only engine installs, not plugin/marketplace payloads.
        if not str(app_name).startswith("UE_"):
            continue
        out.append((Path(location), f"launcher:{app_name}"))
    return out


def _drive_candidates() -> list[tuple[Path, str]]:
    """Bounded scan for engine folders under each fixed drive root."""
    out: list[tuple[Path, str]] = []
    if os.name != "nt":
        return out
    for letter in "CDEFGHIJ":
        drive = Path(f"{letter}:/")
        if not drive.exists():
            continue
        for pattern in _DRIVE_GLOB_PATTERNS:
            try:
                for match in drive.glob(pattern):
                    if match.is_dir():
                        out.append((match, f"scan:{letter}:"))
            except OSError:  # pragma: no cover
                continue
        # Also cover "<drive>:/Program Files/Epic Games/UE_*"
        for base in ("Program Files/Epic Games", "Epic Games", "Program Files"):
            directory = drive / base
            if not directory.is_dir():
                continue
            try:
                for match in directory.glob("UE_*"):
                    if match.is_dir():
                        out.append((match, f"scan:{letter}:/{base}"))
            except OSError:  # pragma: no cover
                continue
    return out


@lru_cache(maxsize=1)
def find_installs() -> tuple[UnrealInstall, ...]:
    """All validated installations, newest version first."""
    candidates: list[tuple[Path, str]] = []

    env_path = os.environ.get("FBXCONV_UNREAL_EDITOR")
    if env_path:
        candidate = Path(env_path)
        if candidate.name.lower() == "unrealeditor-cmd.exe":
            # <root>/Engine/Binaries/Win64/UnrealEditor-Cmd.exe -> <root>
            candidates.append((candidate.parents[3], "env:FBXCONV_UNREAL_EDITOR"))
        else:
            candidates.append((candidate, "env:FBXCONV_UNREAL_EDITOR"))

    candidates.extend(_registry_candidates())
    candidates.extend(_launcher_candidates())
    candidates.extend(_drive_candidates())

    seen: set[Path] = set()
    installs: list[UnrealInstall] = []
    for root, source in candidates:
        install = _install_from_root(Path(root), source)
        if install is None or install.root in seen:
            continue
        seen.add(install.root)
        installs.append(install)

    installs.sort(key=lambda i: _version_key(i.version), reverse=True)
    _log.info("找到 %d 个可用的 Unreal 安装", len(installs))
    for install in installs:
        _log.debug("  %s", install)
    return tuple(installs)


def find_default_install() -> UnrealInstall:
    """The newest installation, or raise :class:`UnrealNotFoundError`."""
    installs = find_installs()
    if not installs:
        raise UnrealNotFoundError(
            "未找到可用的 Unreal Engine 安装。请手动指定 UnrealEditor-Cmd.exe，"
            "或设置环境变量 FBXCONV_UNREAL_EDITOR。",
            searched=[str(p) for p in _drive_candidates()[:10]],
        )
    return installs[0]


def resolve_editor(path: str | os.PathLike[str]) -> UnrealInstall | None:
    """Validate a user-supplied editor path (exe, or engine root)."""
    candidate = Path(path).expanduser()
    if candidate.is_file():
        if candidate.name.lower() in {"unrealeditor-cmd.exe", "unrealeditor.exe"}:
            # .../Engine/Binaries/Win64/<exe> -> engine root
            try:
                root = candidate.parents[3]
            except IndexError:  # pragma: no cover
                return None
            return _install_from_root(root, "manual")
        return None
    if candidate.is_dir():
        return _install_from_root(candidate, "manual")
    return None


__all__ = [
    "UnrealInstall",
    "find_default_install",
    "find_installs",
    "resolve_editor",
]

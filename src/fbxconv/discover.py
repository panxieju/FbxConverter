"""Filesystem discovery (spec section 2, first half).

This module only answers *what files exist* and *where the owning project is*.
It deliberately does **not** guess asset types from filename prefixes -- that
is Unreal's job (see :mod:`fbxconv.unreal`).
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .errors import ContentRootError
from .logutil import get_logger
from .models import CookedArchive, SourceFile

_log = get_logger("discover")

UASSET_SUFFIX = ".uasset"
COMPANION_SUFFIXES = (".uexp", ".ubulk", ".uptnl")
COOKED_SUFFIXES = (".pak", ".utoc", ".ucas")
MOUNT_POINT = "/Game"

#: Directories inside a Content tree that never contain convertible assets.
SKIP_DIR_NAMES = frozenset(
    {
        "__ExternalActors__",
        "__ExternalObjects__",
        "__ExternalPackages__",
        "Developers",
        "Collections",
        "Intermediate",
        "Saved",
        "Binaries",
        "DerivedDataCache",
        "node_modules",
    }
)

#: ``(directories_scanned, uassets_found)``
ProgressCallback = Callable[[int, int], None]


@dataclass(frozen=True)
class ContentRootInfo:
    """How the user's chosen directory was interpreted."""

    path: Path
    """The directory treated as the ``/Game`` mount point."""

    original: Path
    """What the user actually selected."""

    @property
    def was_derived(self) -> bool:
        return self.path != self.original


def resolve_content_root(path: str | os.PathLike[str]) -> ContentRootInfo:
    """Interpret a user-selected directory as a content root.

    Accepts, in order of preference:

    * a ``.uproject`` file, or a project folder containing one -> its ``Content``
    * a directory literally named ``Content``
    * any directory nested inside a ``Content`` folder -> that ``Content`` folder
    * anything else -> used as-is, mounted at ``/Game`` (asset-pack layout)
    """
    original = Path(path).expanduser()
    try:
        original = original.resolve()
    except OSError as exc:  # pragma: no cover - defensive
        raise ContentRootError(f"无法解析路径：{original}（{exc}）") from exc

    if not original.exists():
        raise ContentRootError(f"目录不存在：{original}")

    if original.is_file():
        if original.suffix.lower() != ".uproject":
            raise ContentRootError(f"这不是目录，也不是 .uproject：{original}")
        original = original.parent

    # A project folder: prefer its Content directory.
    if any(original.glob("*.uproject")):
        content = original / "Content"
        if content.is_dir():
            return ContentRootInfo(path=content, original=original)
        raise ContentRootError(f"项目没有 Content 目录：{original}")

    if original.name.lower() == "content":
        return ContentRootInfo(path=original, original=original)

    # Nested somewhere under a Content folder: mount relative to that Content.
    for parent in original.parents:
        if parent.name.lower() == "content":
            _log.info("将资源根目录上溯到 %s", parent)
            return ContentRootInfo(path=parent, original=original)

    # Bare folder: treat as the mount root itself.
    return ContentRootInfo(path=original, original=original)


def find_uproject(content_root: Path, *, max_levels: int = 6) -> Path | None:
    """Search the resource directory's neighbourhood for a ``.uproject``.

    Walks upward from ``content_root``, so ``<proj>/Content`` finds
    ``<proj>/<proj>.uproject`` on the first step. Returns ``None`` when the
    directory is standalone, which triggers the independent conversion project.
    """
    current = Path(content_root)
    for _ in range(max_levels):
        if not current.is_dir():
            break
        try:
            found = sorted(current.glob("*.uproject"))
        except OSError:  # pragma: no cover - permission edge case
            found = []
        if found:
            return found[0]
        if current.parent == current:
            break
        current = current.parent
    return None


def package_path_for(rel: PurePosixPath, mount_point: str = MOUNT_POINT) -> str:
    """``Characters/SK_Mannequin.uasset`` -> ``/Game/Characters/SK_Mannequin``."""
    parts = list(rel.parts)
    if not parts:
        return mount_point
    parts[-1] = PurePosixPath(parts[-1]).stem
    return mount_point.rstrip("/") + "/" + "/".join(parts)


def companion_files(uasset: Path) -> tuple[Path, ...]:
    """Existing bulk-data siblings (``.uexp``, ``.ubulk``, ``.uptnl``)."""
    stem = uasset.with_suffix("")
    out: list[Path] = []
    for suffix in COMPANION_SUFFIXES:
        candidate = Path(str(stem) + suffix)
        if candidate.is_file():
            out.append(candidate)
    return tuple(out)


def _relative_posix(path: Path, root: Path) -> PurePosixPath:
    try:
        rel = path.relative_to(root)
    except ValueError:  # pragma: no cover - junction / case-mismatch fallback
        rel = Path(os.path.relpath(path, root))
    return PurePosixPath(rel.as_posix())


def iter_source_files(
    content_root: Path,
    *,
    progress: ProgressCallback | None = None,
    follow_links: bool = True,
) -> Iterator[SourceFile]:
    """Walk ``content_root`` yielding every ``.uasset`` it contains.

    Directory symlinks and junctions are followed, with a visited-set guard so
    a self-referential layout cannot loop forever.
    """
    root = Path(content_root)
    stack: list[Path] = [root]
    visited: set[str] = set()
    dirs_scanned = 0
    found = 0

    while stack:
        current = stack.pop()
        try:
            key = os.path.realpath(current)
        except OSError:  # pragma: no cover
            key = str(current)
        if key in visited:
            continue
        visited.add(key)

        try:
            entries = sorted(os.scandir(current), key=lambda e: e.name)
        except OSError as exc:
            _log.warning("跳过无法读取的目录 %s（%s）", current, exc)
            continue

        dirs_scanned += 1

        # Group this directory's files by stem so companions can be attached.
        by_stem: dict[str, dict[str, Path]] = {}
        subdirs: list[Path] = []

        for entry in entries:
            name = entry.name
            if name.startswith("."):
                continue
            try:
                if entry.is_dir():
                    if name in SKIP_DIR_NAMES:
                        continue
                    if not follow_links and entry.is_symlink():
                        continue
                    subdirs.append(Path(entry.path))
                    continue
                if not entry.is_file():
                    continue
            except OSError:  # pragma: no cover - broken link etc.
                continue

            suffix = os.path.splitext(name)[1].lower()
            if suffix == UASSET_SUFFIX:
                stem = name[: -len(UASSET_SUFFIX)]
                by_stem.setdefault(stem, {})["asset"] = Path(entry.path)
            elif suffix in COMPANION_SUFFIXES:
                stem = name[: -len(suffix)]
                by_stem.setdefault(stem, {})[suffix] = Path(entry.path)

        for stem in sorted(by_stem):
            group = by_stem[stem]
            asset = group.get("asset")
            if asset is None:
                continue  # orphan companion; nothing to convert
            try:
                size = asset.stat().st_size
            except OSError:  # pragma: no cover
                size = 0
            rel = _relative_posix(asset, root)
            companions = tuple(
                group[s] for s in COMPANION_SUFFIXES if s in group
            )
            found += 1
            yield SourceFile(
                path=asset,
                rel=rel,
                package_path=package_path_for(rel),
                size=size,
                companions=companions,
            )

        if progress is not None:
            progress(dirs_scanned, found)

        stack.extend(reversed(subdirs))


def find_cooked_archives(content_root: Path) -> list[CookedArchive]:
    """Locate packaged artefacts that version 1 explicitly does not support."""
    out: list[CookedArchive] = []
    root = Path(content_root)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            suffix = os.path.splitext(name)[1].lower()
            if suffix in COOKED_SUFFIXES:
                out.append(CookedArchive(path=Path(dirpath) / name, suffix=suffix))
    return out


@dataclass
class DiscoveryResult:
    """Output of the pure-filesystem pass."""

    content_root: Path
    original_root: Path
    project_file: Path | None
    files: list[SourceFile]
    cooked_archives: list[CookedArchive]
    directories_scanned: int

    @property
    def total_bytes(self) -> int:
        return sum(f.size for f in self.files)


def discover(
    path: str | os.PathLike[str],
    *,
    progress: ProgressCallback | None = None,
) -> DiscoveryResult:
    """Resolve a directory, find its project, and enumerate ``.uasset`` files."""
    info = resolve_content_root(path)
    project_file = find_uproject(info.path)
    if project_file is not None:
        _log.info("找到 Unreal 项目：%s", project_file)
    else:
        _log.info("未找到 .uproject，将创建独立转换项目")

    dirs_scanned = 0
    found = 0
    files: list[SourceFile] = []

    def _tick(dirs: int, count: int) -> None:
        nonlocal dirs_scanned, found
        dirs_scanned, found = dirs, count
        if progress is not None:
            progress(dirs, count)

    files = list(iter_source_files(info.path, progress=_tick))

    return DiscoveryResult(
        content_root=info.path,
        original_root=info.original,
        project_file=project_file,
        files=files,
        cooked_archives=find_cooked_archives(info.path),
        directories_scanned=dirs_scanned,
    )


__all__ = [
    "COMPANION_SUFFIXES",
    "COOKED_SUFFIXES",
    "MOUNT_POINT",
    "SKIP_DIR_NAMES",
    "UASSET_SUFFIX",
    "ContentRootInfo",
    "DiscoveryResult",
    "companion_files",
    "discover",
    "find_cooked_archives",
    "find_uproject",
    "iter_source_files",
    "package_path_for",
    "resolve_content_root",
]

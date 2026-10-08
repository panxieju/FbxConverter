"""Conversions between Unreal package paths and filesystem paths."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

MOUNT_GAME = "/Game"


def normalise_mount(mount_point: str) -> str:
    """``Game``, ``/Game/`` -> ``/Game``."""
    text = (mount_point or MOUNT_GAME).strip().replace("\\", "/")
    if not text.startswith("/"):
        text = "/" + text
    return text.rstrip("/") or MOUNT_GAME


def to_package_path(rel: str | PurePosixPath, mount_point: str = MOUNT_GAME) -> str:
    """``Characters/SK_Mannequin.uasset`` -> ``/Game/Characters/SK_Mannequin``."""
    parts = list(PurePosixPath(rel).parts)
    if not parts:
        return normalise_mount(mount_point)
    parts[-1] = PurePosixPath(parts[-1]).stem
    return normalise_mount(mount_point) + "/" + "/".join(parts)


def package_to_object(package_path: str) -> str:
    """``/Game/A/B`` -> ``/Game/A/B.B`` (the object path Unreal needs)."""
    package = package_path.rstrip("/")
    return f"{package}.{package.rsplit('/', 1)[-1]}"


def object_to_package(object_path: str) -> str:
    """``/Game/A/B.B`` -> ``/Game/A/B``."""
    text = object_path.rstrip("/")
    head, _, tail = text.rpartition("/")
    if "." in tail:
        tail = tail.split(".", 1)[0]
    return f"{head}/{tail}" if head else tail


def is_under(package_path: str, mount_point: str) -> bool:
    mount = normalise_mount(mount_point)
    return package_path == mount or package_path.startswith(mount + "/")


def package_to_relative(package_path: str, mount_point: str = MOUNT_GAME) -> PurePosixPath:
    """``/Game/Characters/SK_Mannequin`` -> ``Characters/SK_Mannequin``."""
    mount = normalise_mount(mount_point)
    text = package_path.rstrip("/")
    if text == mount:
        return PurePosixPath()
    if text.startswith(mount + "/"):
        return PurePosixPath(text[len(mount) + 1 :])
    return PurePosixPath(text.lstrip("/"))


def to_filesystem(package_path: str, content_root: Path, mount_point: str = MOUNT_GAME) -> Path:
    """Map a package path onto an on-disk ``.uasset``."""
    rel = package_to_relative(package_path, mount_point)
    return Path(content_root) / Path(rel.as_posix()).with_suffix(".uasset")


def sanitise_filename(name: str, *, fallback: str = "Unnamed") -> str:
    """Make an asset name safe for use as a filename."""
    cleaned = "".join("_" if ch in '<>:"/\\|?*' else ch for ch in (name or "")).strip(" .")
    return cleaned or fallback


def unique_posix(candidate: str, taken: set[str]) -> str:
    """Return ``candidate``, suffixed with ``_1``/``_2``/... until unused."""
    if candidate not in taken:
        taken.add(candidate)
        return candidate
    stem, _, suffix = candidate.rpartition(".")
    if not stem:  # no extension
        stem, suffix = candidate, ""
    index = 1
    while True:
        probe = f"{stem}_{index}" + (f".{suffix}" if suffix else "")
        if probe not in taken:
            taken.add(probe)
            return probe
        index += 1


__all__ = [
    "MOUNT_GAME",
    "is_under",
    "normalise_mount",
    "object_to_package",
    "package_to_object",
    "package_to_relative",
    "sanitise_filename",
    "to_filesystem",
    "to_package_path",
    "unique_posix",
]

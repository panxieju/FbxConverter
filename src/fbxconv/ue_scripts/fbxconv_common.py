"""Shared helpers for the Python scripts that run *inside* Unreal Editor.

This module is executed by Unreal's bundled Python 3.11 interpreter, not by the
desktop app. It must therefore only use the standard library plus ``unreal``.

Communication contract (see :mod:`fbxconv.unreal.runner`):

* Job input  -> the JSON file named by ``$FBXCONV_JOB``
* Result out -> the JSON file named by ``$FBXCONV_RESULT``
* Progress   -> stdout lines beginning with ``FBXCONV:`` followed by JSON
"""

import json
import os
import sys
import traceback

EVENT_PREFIX = "FBXCONV:"
JOB_ENV = "FBXCONV_JOB"
RESULT_ENV = "FBXCONV_RESULT"


# --------------------------------------------------------------------------- #
# Host <-> Unreal transport
# --------------------------------------------------------------------------- #


def emit(event_type, **fields):
    """Print a progress event that the host app parses while Unreal runs."""
    payload = {"type": event_type}
    payload.update(fields)
    try:
        sys.stdout.write(EVENT_PREFIX + json.dumps(payload, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    except Exception:
        pass


def _job_file():
    path = os.environ.get(JOB_ENV)
    if not path:
        raise RuntimeError("%s is not set" % JOB_ENV)
    if not os.path.isfile(path):
        raise RuntimeError("%s points at a missing file: %s" % (JOB_ENV, path))
    return path


def read_job():
    with open(_job_file(), encoding="utf-8") as handle:
        return json.load(handle)


def _result_file():
    path = os.environ.get(RESULT_ENV)
    if not path:
        raise RuntimeError("%s is not set" % RESULT_ENV)
    return path


def write_result(ok, data=None, error=None, traceback_text=None, warnings=None):
    payload = {"ok": bool(ok)}
    if ok:
        payload["data"] = data if data is not None else {}
    else:
        payload["error"] = error or "unknown error"
        if traceback_text:
            payload["traceback"] = traceback_text
    if warnings:
        payload["warnings"] = list(warnings)
    destination = _result_file()
    with open(destination, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)


def main(body):
    """Run ``body(warnings)`` and always leave a well-formed result behind."""
    warnings = []
    try:
        data = body(warnings)
    except Exception as exc:
        text = traceback.format_exc()
        sys.stderr.write(text)
        sys.stderr.flush()
        emit("fatal", error="%s: %s" % (type(exc).__name__, exc))
        write_result(False, error="%s: %s" % (type(exc).__name__, exc),
                     traceback_text=text, warnings=warnings)
        return
    write_result(True, data=data, warnings=warnings)


# --------------------------------------------------------------------------- #
# Unreal helpers
# --------------------------------------------------------------------------- #


def get_asset_registry():
    import unreal

    return unreal.AssetRegistryHelpers.get_asset_registry()


def dependency_options(include_soft=True, include_hard=True):
    """Build ``AssetRegistryDependencyOptions`` tolerantly across UE versions."""
    import unreal

    options = unreal.AssetRegistryDependencyOptions()
    for name, value in (
        ("include_soft_package_references", include_soft),
        ("include_hard_package_references", include_hard),
        ("include_searchable_names", False),
        ("include_soft_management_references", False),
        ("include_hard_management_references", False),
    ):
        try:
            options.set_editor_property(name, value)
        except Exception:
            pass
    return options


def asset_class_name(asset_data):
    """Class name of an ``AssetData`` across UE4/UE5 API shapes."""
    for attribute in ("asset_class_path", "asset_class"):
        try:
            value = getattr(asset_data, attribute)
        except Exception:
            continue
        if value is None:
            continue
        # UE5 TopLevelAssetPath exposes .asset_name; older builds give a Name.
        inner = getattr(value, "asset_name", None)
        text = str(inner) if inner else str(value)
        if text and text != "None":
            return text
    return ""


def package_name_of(asset_data):
    try:
        return str(asset_data.package_name)
    except Exception:
        return ""


def asset_name_of(asset_data):
    try:
        return str(asset_data.asset_name)
    except Exception:
        return ""


def object_path_of(asset_data):
    try:
        return str(asset_data.object_path)
    except Exception:
        return ""


def tags_of(asset_data):
    """All registry tags as plain strings, best effort."""
    tags = {}
    try:
        raw = asset_data.get_tags_and_values()  # not always exposed
    except Exception:
        raw = None
    if raw:
        try:
            items = raw.items() if hasattr(raw, "items") else raw
            for key, value in items:
                tags[str(key)] = str(value)
        except Exception:
            pass
    if tags:
        return tags
    # Fall back to probing the tags we care about.
    for key in (
        "Skeleton",
        "TargetSkeleton",
        "PreviewMesh",
        "NumberOfFrames",
        "NumberOfKeys",
        "Length",
        "SequenceLength",
        "FrameRate",
        "Materials",
        "Textures",
        "PhysicsAsset",
        "Group",
        "ImportedOptionalCurves",
        "bHasLocationCurve",
        "bHasRotationCurve",
        "bHasScaleCurve",
        "bEnableRootMotion",
        "ParentClass",
        "NativeParentClass",
        "GeneratedClass",
        "BlueprintType",
    ):
        try:
            value = asset_data.get_tag_value(key)
        except Exception:
            continue
        text = str(value) if value is not None else ""
        if text and text != "None":
            tags[key] = text
    return tags


def dependencies_of(registry, package_name, options):
    """Package-level dependencies recorded in the asset registry."""
    if not package_name:
        return []
    try:
        raw = registry.get_dependencies(package_name, options)
    except Exception:
        try:
            raw = registry.get_dependencies(package_name)
        except Exception:
            return []
    out = []
    for item in raw or []:
        text = str(item)
        if text and text != "None":
            out.append(text)
    return out


def package_exists(registry, package_name, content_dir_game=None):
    """Is ``package_name`` resolvable?"""
    import unreal

    if not package_name:
        return False
    # /Game packages map onto the project's Content directory.
    if package_name.startswith("/Game/") and content_dir_game:
        relative = package_name[len("/Game/") :]
        candidate = os.path.join(content_dir_game, *relative.split("/")) + ".uasset"
        if os.path.isfile(candidate):
            return True
    try:
        assets = registry.get_assets_by_package_name(package_name)
        if assets:
            return True
    except Exception:
        pass
    try:
        # Engine and plugin content resolve through the package file system.
        if unreal.Paths.file_exists(package_name):
            return True
    except Exception:
        pass
    return False


def expected_package_path(package_name, content_dir_game=None):
    """Where we would expect to find a missing /Game package on disk."""
    if package_name.startswith("/Game/") and content_dir_game:
        relative = package_name[len("/Game/") :]
        return os.path.join(content_dir_game, *relative.split("/")) + ".uasset"
    return None


def scan_paths(registry, paths, verbose=True):
    """Force the asset registry to pick up ``paths`` before querying."""
    if not paths:
        return
    attempts = (
        (tuple(paths), True, True),
        (tuple(paths), True),
        (tuple(paths),),
    )
    for args in attempts:
        try:
            registry.scan_paths_synchronous(*args)
            return
        except Exception as exc:
            if verbose:
                emit("log", message="scan_paths_synchronous%s failed: %s" % (args[1:], exc))
    raise RuntimeError("AssetRegistry.scan_paths_synchronous is unavailable")


def assets_under(registry, path):
    """All assets at or below ``path``."""
    try:
        return list(registry.get_assets_by_path(path, True))
    except TypeError:
        return list(registry.get_assets_by_path(path))


def to_posix(path):
    return str(path).replace("\\", "/")


def frame_rate_struct(numerator):
    """Best-effort ``unreal.FrameRate`` construction."""
    import unreal

    for kwargs in (
        {"numerator": int(numerator), "denominator": 1},
        {"numerator": int(numerator)},
    ):
        try:
            return unreal.FrameRate(**kwargs)
        except Exception:
            continue
    return None

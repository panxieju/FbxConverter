"""Scan phase -- runs inside Unreal Editor.

Answers "what *is* each .uasset?" using the AssetRegistry, which reads package
headers without fully loading them. That keeps the scan fast and, more
importantly, lets us report missing dependencies instead of crashing on them.

Skeleton linkage is resolved authoritatively by loading each skeletal mesh and
animation sequence. The registry's ``Skeleton`` tag proved unreliable (it can
come back as ``/Script/Engine``), and a wrong skeleton silently mis-groups the
whole conversion, so we pay the load cost for exactly those asset kinds.

Input (``$FBXCONV_JOB``)::

    {
      "mount_point": "/Game",
      "deep_resolve": true,      # load meshes/anims to read real properties
      "max_assets": 200000
    }

Output (``$FBXCONV_RESULT``) -> ``data``::

    {"engine_version": ..., "content_dir": ..., "assets": [ {...}, ... ]}
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fbxconv_common as common

PROGRESS_EVERY = 50
SKELETON_KINDS = ("SkeletalMesh", "AnimSequence", "AnimSequenceBase")
MESH_KIND = "SkeletalMesh"


def _load(package_name, cache):
    """Load a package, caching both hits and misses."""
    import unreal

    if package_name in cache:
        return cache[package_name]
    obj = None
    try:
        obj = unreal.load_asset(package_name)
    except Exception:
        obj = None
    cache[package_name] = obj
    return obj


def _is_package_path(value):
    """True for ``/Game/...``-style package paths (not ``/Script/Module``)."""
    if not value:
        return False
    text = str(value).strip()
    if not text.startswith("/"):
        return False
    return not text.startswith("/Script")


def _package_of(value):
    """Package path of a UObject reference, or ``None``.

    Tries the API getters first, then falls back to parsing the Python repr
    (``<Object '/Game/X.Y' (0x...) Class 'Skeleton'>``), which is what an
    ``unreal.Skeleton`` prints as.
    """
    if value is None:
        return None
    for getter in ("get_path_name", "get_name"):
        try:
            text = str(getattr(value, getter)())
        except Exception:
            continue
        if text.startswith("/"):
            return text.split(".")[0] or None
    text = str(value)
    if text.startswith("/"):
        return text.split(".")[0] or None
    if "'" in text:
        parts = text.split("'")
        if len(parts) > 1 and parts[1].startswith("/"):
            return parts[1].split(".")[0] or None
    return None


def _skeleton_of(obj):
    """Resolve the Skeleton of a mesh or animation.

    ``AnimSequence`` does **not** expose a ``skeleton`` attribute -- only the
    ``get_editor_property("skeleton")`` accessor and a ``get_skeleton()``
    method -- so every route has to be attempted.
    """
    accessors = (
        lambda target: target.get_editor_property("skeleton"),
        lambda target: target.skeleton,
        lambda target: target.get_skeleton(),
    )
    for accessor in accessors:
        try:
            value = accessor(obj)
        except Exception:
            continue
        package = _package_of(value)
        if package:
            return package
    return None


def _data_model(obj):
    """UE5 animation data model, which owns frame rate and frame count."""
    try:
        return obj.get_editor_property("data_model_interface")
    except Exception:
        return None


def _property(obj, name, default=None):
    try:
        return obj.get_editor_property(name)
    except Exception:
        return default


def _object_packages(obj, attribute):
    """Package paths of an array-valued object property."""
    out = []
    try:
        values = obj.get_editor_property(attribute)
    except Exception:
        return out
    if not values:
        return out
    try:
        items = list(values)
    except TypeError:
        return out
    for item in items:
        package = _package_of(item)
        if package:
            out.append(package)
    return out


def _numeric(value):
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number


def _frame_rate_of(obj):
    """Sampling rate of an animation, as a float."""
    model = _data_model(obj)
    if model is not None:
        try:
            rate = model.get_frame_rate()
            numerator = _property(rate, "numerator")
            denominator = _property(rate, "denominator")
            if numerator:
                return float(numerator) / float(denominator or 1)
        except Exception:
            pass
    value = _property(obj, "sampling_frame_rate")
    numerator = _property(value, "numerator") if value is not None else None
    denominator = _property(value, "denominator") if value is not None else None
    if numerator:
        return float(numerator) / float(denominator or 1)
    return None


def _duration_of(obj):
    for attribute in ("sequence_length", "play_length"):
        number = _numeric(_property(obj, attribute))
        if number and number > 0:
            return number
    for method in ("get_play_length",):
        try:
            number = _numeric(getattr(obj, method)())
        except Exception:
            number = None
        if number and number > 0:
            return number
    return None


def _frame_count_of(obj, duration, rate):
    model = _data_model(obj)
    if model is not None:
        try:
            number = _numeric(model.get_number_of_frames())
            if number and number > 0:
                return int(round(number))
        except Exception:
            pass
    try:
        number = _numeric(obj.get_number_of_frames())
    except Exception:
        number = None
    if number and number > 0:
        return int(round(number))
    if duration and rate:
        return int(round(duration * rate)) + 1
    return None


def body(warnings):
    import unreal

    job = common.read_job()
    mount_point = str(job.get("mount_point") or "/Game").rstrip("/")
    # The selection defines the *scope*; the mount point defines where
    # dependencies resolve from. They differ when the user picks a subfolder of
    # Content: mounting has to stay at Content so /Game/... references still
    # resolve, but only the selected subtree should be reported.
    scope_prefix = str(job.get("scope_prefix") or "").strip().rstrip("/")
    query_path = scope_prefix or mount_point
    deep_resolve = bool(job.get("deep_resolve", True))
    max_assets = int(job.get("max_assets") or 200000)

    registry = common.get_asset_registry()
    content_dir = unreal.Paths.project_content_dir()
    engine_version = str(unreal.SystemLibrary.get_engine_version())

    common.emit(
        "phase",
        message="正在扫描资源注册表",
        mount_point=mount_point,
        scope=scope_prefix,
    )
    common.scan_paths(registry, [mount_point])

    asset_data_list = common.assets_under(registry, query_path)
    total = min(len(asset_data_list), max_assets)
    common.emit("phase", message="正在解析资源类型与依赖", total=total)
    if len(asset_data_list) > max_assets:
        warnings.append(
            "资源数量 %d 超过上限 %d，仅解析前 %d 个。"
            % (len(asset_data_list), max_assets, max_assets)
        )

    options = common.dependency_options(include_soft=True, include_hard=True)
    exists_cache = {}
    load_cache = {}
    records = []
    load_failures = []

    def package_exists(package_name):
        if package_name not in exists_cache:
            exists_cache[package_name] = common.package_exists(
                registry, package_name, content_dir
            )
        return exists_cache[package_name]

    def expected_path(package_name):
        path = common.expected_package_path(package_name, content_dir)
        return common.to_posix(path) if path else None

    for index, asset_data in enumerate(asset_data_list[:max_assets]):
        package_name = common.package_name_of(asset_data)
        if not package_name:
            continue
        # Defensive: get_assets_by_path should already be scoped.
        if scope_prefix and not (
            package_name == scope_prefix or package_name.startswith(scope_prefix + "/")
        ):
            continue
        class_name = common.asset_class_name(asset_data)
        tags = common.tags_of(asset_data)

        record = {
            "package_path": package_name,
            "name": common.asset_name_of(asset_data),
            "class": class_name,
            "object_path": common.object_path_of(asset_data),
            "dependencies": [],
            "skeleton": None,
            "mesh": None,
            "materials": [],
            "textures": [],
            "physics_asset": None,
            "num_frames": None,
            "duration": None,
            "disk_path": None,
            "extra": {"tags": tags},
        }

        if package_name.startswith(mount_point + "/"):
            relative = package_name[len(mount_point) + 1 :]
            candidate = os.path.join(content_dir, *relative.split("/")) + ".uasset"
            record["disk_path"] = common.to_posix(candidate)

        record["dependencies"] = common.dependencies_of(registry, package_name, options)

        # --- Skeleton linkage (authoritative: read it off the loaded object) ---
        tag_skeleton = tags.get("Skeleton") or tags.get("TargetSkeleton")
        if _is_package_path(tag_skeleton):
            record["skeleton"] = str(tag_skeleton).split(".")[0]

        obj = None
        if class_name in SKELETON_KINDS and (deep_resolve or not record["skeleton"]):
            obj = _load(package_name, load_cache)
            if obj is None:
                load_failures.append(package_name)
            else:
                record["skeleton"] = _skeleton_of(obj) or record["skeleton"]

        if class_name in ("AnimSequence", "AnimSequenceBase"):
            record["extra"]["deep_resolved"] = obj is not None
            # Duration: registry tags are unreliable, prefer the live object.
            for key in ("SequenceLength", "Length", "PlayLength"):
                number = _numeric(tags.get(key))
                if number and number > 0:
                    record["duration"] = number
                    break
            rate_tag = _numeric(tags.get("FrameRate")) or _numeric(tags.get("SampleRate"))
            if obj is not None:
                rate = _frame_rate_of(obj) or rate_tag
                duration = _duration_of(obj) or record["duration"]
                record["duration"] = duration
                record["num_frames"] = _frame_count_of(obj, duration, rate)
                if rate:
                    record["extra"]["frame_rate"] = rate
                try:
                    record["extra"]["has_root_motion"] = bool(
                        obj.get_editor_property("enable_root_motion")
                    )
                except Exception:
                    pass
            else:
                record["num_frames"] = (
                    int(round(record["duration"] * rate_tag)) + 1
                    if record["duration"] and rate_tag
                    else None
                )
                if rate_tag:
                    record["extra"]["frame_rate"] = rate_tag

        elif class_name == MESH_KIND:
            if obj is None and (deep_resolve or not record["skeleton"]):
                obj = _load(package_name, load_cache)
                if obj is None:
                    load_failures.append(package_name)
            record["extra"]["deep_resolved"] = obj is not None
            if obj is not None:
                record["skeleton"] = _skeleton_of(obj) or record["skeleton"]
                record["materials"] = _object_packages(obj, "materials")
                record["physics_asset"] = _package_of(_property(obj, "physics_asset"))
                try:
                    sockets = obj.get_editor_property("sockets")
                    record["extra"]["socket_count"] = len(sockets) if sockets else 0
                except Exception:
                    pass
            if not record["physics_asset"] and tags.get("PhysicsAsset"):
                value = tags["PhysicsAsset"]
                if _is_package_path(value):
                    record["physics_asset"] = value.split(".")[0]

        elif class_name == "Skeleton":
            record["extra"]["is_group_root"] = True

        # --- missing dependency detection ------------------------------------
        missing = []
        for dependency in record["dependencies"]:
            if dependency == package_name:
                continue
            # /Script/* are native modules; they are always present.
            if dependency.startswith("/Script"):
                continue
            if not package_exists(dependency):
                missing.append(
                    {"package": dependency, "expected_path": expected_path(dependency)}
                )
        if missing:
            record["extra"]["missing"] = missing

        records.append(record)

        if (index + 1) % PROGRESS_EVERY == 0 or index + 1 == total:
            common.emit(
                "progress",
                done=index + 1,
                total=total,
                message="已解析 %d/%d" % (index + 1, total),
            )

    if load_failures:
        warnings.append(
            "%d 个资源无法加载（依赖缺失）：%s"
            % (len(load_failures), ", ".join(load_failures[:10]))
        )

    common.emit("phase", message="扫描完成", assets=len(records))

    return {
        "engine_version": engine_version,
        "content_dir": common.to_posix(content_dir),
        "mount_point": mount_point,
        "scope_prefix": scope_prefix,
        "asset_count": len(records),
        "load_failures": load_failures,
        "assets": records,
    }


common.main(body)

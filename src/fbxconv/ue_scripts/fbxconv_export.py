"""Export phase -- runs inside Unreal Editor.

Batch-exports skeletal meshes and animation sequences to FBX in a single
editor launch, reporting a per-item outcome so individual failures stay
retryable (spec sections 4.5 and 5).

This script deliberately never saves a package: in the independent-project
case the assets may be the user's originals reached through a junction.

Input (``$FBXCONV_JOB``)::

    {
      "exports": [
        {"kind": "skeletal_mesh",
         "package_path": "/Game/.../SK_Mannequin",
         "output_path": "G:/out/.../SK_Mannequin.fbx"}
      ]
    }

Output -> ``data.results``: one entry per requested export.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fbxconv_common as common

FBX_BINARY_MAGIC = b"Kaydara FBX Binary"
FBX_ASCII_MAGIC = b"; FBX"

#: Set from the job; used to force an AssetRegistry scan before loading.
_MOUNT_POINT = "/Game"

#: The editor's initial AssetRegistry gather finishes several seconds after
#: startup, and -ExecutePythonScript runs right in that window. Loading before
#: it completes makes load_asset return None, so we wait it out first.
_READY_TIMEOUT = 90.0
_LOAD_ATTEMPTS = 6


def _ensure_registry_ready():
    """Block until the AssetRegistry has finished indexing the mount point."""
    registry = common.get_asset_registry()

    # UE5 exposes an explicit barrier; UE4 does not.
    try:
        registry.wait_for_completion()
    except Exception:
        pass

    # Enumerating the mount is what makes the gather complete in practice.
    deadline = time.time() + _READY_TIMEOUT
    count = 0
    last_error = None
    while time.time() < deadline:
        try:
            common.scan_paths(registry, [_MOUNT_POINT])
            count = len(common.assets_under(registry, _MOUNT_POINT))
        except Exception as exc:
            last_error = exc
        if count > 0:
            common.emit("log", message="资源注册表就绪：%d 个资源" % count)
            return count
        time.sleep(0.5)

    detail = "：%s" % last_error if last_error else ""
    common.emit(
        "log",
        level="error",
        message="等待资源注册表超时（挂载点 %s%s），仍将尝试加载" % (_MOUNT_POINT, detail),
    )
    return count


def _robust_load(entry):
    """Load an asset, tolerating a registry that is still warming up."""
    import unreal

    registry = common.get_asset_registry()
    candidates = [entry["package_path"]]
    object_path = entry.get("object_path")
    if object_path:
        candidates.append(object_path)

    for attempt in range(_LOAD_ATTEMPTS):
        for candidate in candidates:
            try:
                obj = unreal.load_asset(candidate)
            except Exception:
                obj = None
            if obj is not None:
                return obj

        # Give the gatherer more time and nudge it along.
        try:
            registry.wait_for_completion()
        except Exception:
            pass
        try:
            common.scan_paths(registry, [_MOUNT_POINT])
        except Exception:
            pass
        time.sleep(0.5 * (attempt + 1))

    return None


def _make_exporter(kind):
    """Instantiate the right FBX exporter for an asset kind."""
    import unreal

    if kind == "skeletal_mesh":
        exporter_class = unreal.SkeletalMeshExporterFBX
    elif kind == "anim_sequence":
        exporter_class = unreal.AnimSequenceExporterFBX
    else:
        raise RuntimeError("unsupported export kind: %s" % kind)

    try:
        return exporter_class()
    except Exception:
        pass
    try:
        return unreal.new_object(exporter_class)
    except Exception as exc:
        raise RuntimeError("cannot instantiate %s: %s" % (exporter_class, exc)) from exc


def _fbx_options(settings, applied):
    """Configure FbxExportOption where the engine exposes it."""
    import unreal

    try:
        options = unreal.FbxExportOption()
    except Exception:
        return None

    # Binary FBX keeps the file small and is what Unity expects.
    wanted = {
        "ascii": False,
        "collision": False,
        "level_of_detail": False,
        "vertex_color": settings.get("vertex_color", False),
        "export_preview_mesh": False,
        "force_front_x_axis": False,
    }
    for name, value in wanted.items():
        try:
            options.set_editor_property(name, value)
            applied.append(name)
        except Exception:
            continue
    return options


def _set_property(target, name, value):
    try:
        target.set_editor_property(name, value)
        return True
    except Exception:
        return False


def _looks_like_fbx(path):
    try:
        with open(path, "rb") as handle:
            head = handle.read(32)
    except OSError:
        return False
    return head.startswith(FBX_BINARY_MAGIC) or head.startswith(FBX_ASCII_MAGIC)


def _export_one(unreal, entry, settings, applied_options):
    package_path = entry["package_path"]
    output_path = entry["output_path"]

    obj = _robust_load(entry)
    if obj is None:
        raise RuntimeError("无法加载资源（依赖缺失或未挂载）：%s" % package_path)

    directory = os.path.dirname(output_path)
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)

    exporter = _make_exporter(entry["kind"])
    task = unreal.AssetExportTask()

    if not _set_property(task, "object", obj):
        raise RuntimeError("AssetExportTask.object 设置失败")
    _set_property(task, "exporter", exporter)
    if not _set_property(task, "filename", output_path):
        raise RuntimeError("AssetExportTask.filename 设置失败")
    _set_property(task, "automated", True)
    _set_property(task, "prompt", False)
    _set_property(task, "replace_identical", True)
    _set_property(task, "selected", False)
    _set_property(task, "use_file_archive", False)
    _set_property(task, "write_empty_files", False)

    options = _fbx_options(settings, applied_options)
    if options is not None:
        _set_property(task, "options", options)

    started = time.time()
    try:
        success = unreal.Exporter.run_asset_export_task(task)
    except Exception as exc:
        raise RuntimeError("导出调用异常：%s" % exc) from exc
    elapsed = time.time() - started

    if not os.path.isfile(output_path):
        raise RuntimeError(
            "导出器未生成文件（run_asset_export_task 返回 %s）：%s"
            % (success, output_path)
        )
    if os.path.getsize(output_path) <= 0:
        raise RuntimeError("导出文件为空：%s" % output_path)
    if not _looks_like_fbx(output_path):
        raise RuntimeError("导出文件不是有效的 FBX：%s" % output_path)

    return {
        "success": bool(success),
        "elapsed": elapsed,
        "size": os.path.getsize(output_path),
        "object_class": obj.get_class().get_name(),
    }


def body(warnings):
    import unreal

    global _MOUNT_POINT

    job = common.read_job()
    exports = job.get("exports") or []
    settings = job.get("settings") or {}
    _MOUNT_POINT = str(job.get("mount_point") or "/Game").rstrip("/")
    total = len(exports)

    common.emit("phase", message="开始导出", total=total)

    # Force discovery up front: a just-started editor may not have indexed the
    # content yet, and load_asset would silently return None.
    ready = _ensure_registry_ready()
    if not ready:
        warnings.append("资源注册表未在超时前就绪，导出可能失败。")

    results = []
    for index, entry in enumerate(exports):
        label = entry.get("display_name") or os.path.basename(entry["package_path"])
        common.emit(
            "item", done=index, total=total, name=label, state="running"
        )
        record = {
            "package_path": entry["package_path"],
            "kind": entry["kind"],
            "output_path": entry.get("output_path"),
            "ok": False,
            "error": None,
            "size": 0,
            "elapsed": 0.0,
        }
        try:
            detail = _export_one(unreal, entry, settings, [])
            record.update(detail)
            record["ok"] = True
        except Exception as exc:
            record["error"] = "%s: %s" % (type(exc).__name__, exc)
            common.emit(
                "log",
                level="error",
                message="导出失败 %s -> %s" % (entry["package_path"], record["error"]),
            )
        results.append(record)
        common.emit(
            "item",
            done=index + 1,
            total=total,
            name=label,
            state="ok" if record["ok"] else "failed",
            error=record["error"],
        )

    succeeded = sum(1 for r in results if r["ok"])
    common.emit(
        "phase",
        message="导出完成：%d/%d 成功" % (succeeded, total),
        succeeded=succeeded,
        failed=total - succeeded,
    )

    return {
        "engine_version": str(unreal.SystemLibrary.get_engine_version()),
        "total": total,
        "succeeded": succeeded,
        "failed": total - succeeded,
        "results": results,
    }


common.main(body)

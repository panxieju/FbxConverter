"""Texture export phase -- runs inside Unreal Editor.

Unreal's FBX exporter cannot embed textures: ``FFbxExporter::WriteToFile``
hard-codes ``bEmbedMedia = false`` and ``UFbxExportOption`` exposes no switch
for it. What it writes instead is the *original authoring path* recorded in the
texture's asset import data (``U:/Dropbox/...``), which does not exist on the
machine doing the conversion. On top of that, a material only gets a texture
node at all when the slot is driven by a texture *parameter*; a plain
``TextureSample`` exports as a bare colour.

So the texture bytes are exported here, as PNG, and handed to Unity next to the
FBX. Unity then builds the materials from ``manifest.json``.

Input (``$FBXCONV_JOB``)::

    {
      "textures": [
        {"package_path": "/Game/.../T_Rock",
         "output_path": "G:/out/.../T_Rock.png",
         "display_name": "T_Rock"}
      ]
    }

Output -> ``data``::

    {"complete": true, "total": 2, "succeeded": 2, "failed": 0,
     "results": [ {...}, ... ]}

The result file is rewritten after **every** texture. ``UTextureExporterPNG``
runs a native ``check()`` on the source format, and a failed ``check`` kills the
editor outright rather than raising into Python -- so a partially written result
is the only way the host can learn how far the run got.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fbxconv_common as common

FBXCONV_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

#: Source formats ``UTextureExporterPNG::SupportsTexture`` rejects. Its
#: predicate is ``!ERawImageFormat::IsHDR(ConvertToRawImageFormat(Source.GetFormat()))``,
#: and ``Source`` is not readable from Python, so the closest proxy available is
#: the texture's compression setting.
HDR_COMPRESSION_HINTS = ("HDR", "HALFFLOAT", "HALF_FLOAT")

#: Name fragments that mark a normal map. Unreal's own signal is
#: ``TC_NORMALMAP``; the name is the fallback for textures left on TC_DEFAULT.
NORMAL_TOKENS = ("_normal", "_nrm", "_norm", "normalmap")


def _make_exporter():
    """Instantiate ``TextureExporterPNG`` defensively."""
    import unreal

    exporter_class = getattr(unreal, "TextureExporterPNG", None)
    if exporter_class is None:
        raise RuntimeError("该引擎版本没有 TextureExporterPNG")
    try:
        return exporter_class()
    except Exception:
        pass
    try:
        return unreal.new_object(exporter_class)
    except Exception as exc:
        raise RuntimeError("无法创建 TextureExporterPNG：%s" % exc) from exc


def _set(target, name, value):
    try:
        target.set_editor_property(name, value)
        return True
    except Exception:
        return False


def _enum_name(value):
    """``<TextureCompressionSettings.TC_DEFAULT: 0>`` -> ``TC_DEFAULT``."""
    text = str(value or "")
    if ":" in text:
        text = text.split(":", 1)[0]
    if "." in text:
        text = text.split(".", 1)[1]
    return text.strip("<> ").upper()


def classify_texture(name, srgb, compression_name):
    """Best-effort role of a texture for the Unity material.

    sRGB is the only *authoritative* signal Unreal exposes to Python: colour
    data is sRGB, roughness/metallic/AO/mask data is linear. Normal maps are
    identified by their compression setting first, then by name.
    """
    lowered = (name or "").lower()
    if compression_name == "TC_NORMALMAP" or any(t in lowered for t in NORMAL_TOKENS):
        return "normal"
    if srgb is False:
        return "mask"
    return "baseColor"


def _flush(data, results, total, complete):
    """Rewrite the result file so a native crash still leaves usable data."""
    succeeded = sum(1 for r in results if r.get("ok"))
    data.update(
        {
            "complete": bool(complete),
            "total": total,
            "succeeded": succeeded,
            "failed": len(results) - succeeded,
            "results": results,
        }
    )
    try:
        common.write_result(True, data=data)
    except Exception:
        pass


def _describe(tex):
    """Python-visible facts about a texture, plus the pre-flight verdict."""
    import unreal

    info = {
        "class": tex.get_class().get_name(),
        "srgb": True,
        "compression": "",
        "virtual": False,
        "source_file": "",
        "unsupported": None,
    }
    try:
        info["srgb"] = bool(tex.get_editor_property("srgb"))
    except Exception:
        pass
    try:
        info["compression"] = _enum_name(tex.get_editor_property("compression_settings"))
    except Exception:
        pass
    try:
        info["virtual"] = bool(tex.get_editor_property("virtual_texture_streaming"))
    except Exception:
        pass
    try:
        import_data = tex.get_editor_property("asset_import_data")
        if import_data is not None:
            info["source_file"] = str(import_data.get_first_filename() or "")
    except Exception:
        pass

    if not isinstance(tex, unreal.Texture2D):
        info["unsupported"] = "只有 Texture2D 能导出为 PNG（实际为 %s）" % info["class"]
    elif info["virtual"]:
        info["unsupported"] = "虚拟贴图（Virtual Texture）不支持 PNG 导出"
    elif any(hint in info["compression"] for hint in HDR_COMPRESSION_HINTS):
        info["unsupported"] = (
            "HDR 贴图（%s）会让 TextureExporterPNG 的原生断言崩溃编辑器，已跳过"
            % info["compression"]
        )
    return info


def _export_one(unreal, entry, exporter):
    package_path = entry["package_path"]
    output_path = entry["output_path"]

    tex = unreal.load_asset(package_path)
    if tex is None:
        raise RuntimeError("无法加载贴图：%s" % package_path)

    info = _describe(tex)
    if info["unsupported"]:
        raise RuntimeError(info["unsupported"])

    directory = os.path.dirname(output_path)
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)

    task = unreal.AssetExportTask()
    _set(task, "object", tex)
    _set(task, "exporter", exporter)
    if not _set(task, "filename", output_path):
        raise RuntimeError("AssetExportTask.filename 设置失败")
    _set(task, "automated", True)
    _set(task, "prompt", False)
    _set(task, "replace_identical", True)
    _set(task, "selected", False)
    _set(task, "use_file_archive", False)
    _set(task, "write_empty_files", False)

    started = time.time()
    success = unreal.Exporter.run_asset_export_task(task)
    elapsed = time.time() - started

    if not os.path.isfile(output_path):
        raise RuntimeError(
            "导出器未生成文件（run_asset_export_task 返回 %s）：%s" % (success, output_path)
        )
    size = os.path.getsize(output_path)
    if size <= 0:
        raise RuntimeError("导出文件为空：%s" % output_path)
    try:
        with open(output_path, "rb") as handle:
            head = handle.read(8)
    except OSError as exc:
        raise RuntimeError("无法读取导出文件：%s" % exc) from exc
    if not head.startswith(FBXCONV_PNG_MAGIC):
        raise RuntimeError("导出文件不是有效的 PNG：%s" % output_path)

    return {
        "elapsed": elapsed,
        "size": size,
        "srgb": info["srgb"],
        "compression": info["compression"],
        "texture_class": info["class"],
        "source_file": info["source_file"],
        "texture_kind": classify_texture(
            entry.get("display_name") or package_path.rsplit("/", 1)[-1],
            info["srgb"],
            info["compression"],
        ),
    }


def body(warnings):
    import unreal

    job = common.read_job()
    entries = job.get("textures") or []
    total = len(entries)
    data = {"engine_version": str(unreal.SystemLibrary.get_engine_version())}

    common.emit("phase", message="开始导出贴图", total=total)

    exporter = _make_exporter()
    results = []
    _flush(data, results, total, complete=False)

    for index, entry in enumerate(entries):
        label = entry.get("display_name") or os.path.basename(entry["package_path"])
        common.emit("item", done=index, total=total, name=label, state="running")
        record = {
            "package_path": entry["package_path"],
            "output_path": entry.get("output_path"),
            "display_name": label,
            "ok": False,
            "error": None,
            "size": 0,
            "elapsed": 0.0,
        }
        try:
            record.update(_export_one(unreal, entry, exporter))
            record["ok"] = True
        except Exception as exc:
            record["error"] = "%s: %s" % (type(exc).__name__, exc)
            common.emit(
                "log",
                level="error",
                message="贴图导出失败 %s -> %s" % (entry["package_path"], record["error"]),
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
        # A native check() failure in the exporter kills the editor without
        # unwinding, so persist after every item rather than at the end.
        _flush(data, results, total, complete=False)

    succeeded = sum(1 for r in results if r["ok"])
    common.emit(
        "phase",
        message="贴图导出完成：%d/%d 成功" % (succeeded, total),
        succeeded=succeeded,
        failed=total - succeeded,
    )

    data.update(
        {
            "complete": True,
            "total": total,
            "succeeded": succeeded,
            "failed": total - succeeded,
            "results": results,
        }
    )
    if total and succeeded < total:
        warnings.append("%d 个贴图未能导出，详见报告。" % (total - succeeded))
    return data


common.main(body)

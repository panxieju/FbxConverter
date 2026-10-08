"""Verify the CLazyAnimpack export inside Tuanjie and measure axis/units."""

import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, r"G:\Project\FbxConverter\src")

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from fbxconv.unity.runner import find_unity_editors, run_batch_import  # noqa: E402

EXPORT = Path(r"G:\Project\FbxConverter\.verify-escape\UnityExport")
PROJECT = Path(r"G:\Project\FbxConverter\.verify-unity")

# ---------------------------------------------------------------- project
if PROJECT.exists():
    shutil.rmtree(PROJECT)
(PROJECT / "Assets").mkdir(parents=True)
(PROJECT / "Packages").mkdir(parents=True)
(PROJECT / "ProjectSettings").mkdir(parents=True)
(PROJECT / "ProjectSettings" / "ProjectVersion.txt").write_text(
    "m_EditorVersion: 2022.3.62t11\n"
    "m_EditorVersionWithRevision: 2022.3.62t11 (b99029341900)\n",
    encoding="utf-8",
)
(PROJECT / "Packages" / "manifest.json").write_text(
    '{ "dependencies": {} }', encoding="utf-8"
)

shutil.copytree(EXPORT, PROJECT / "Assets" / "UnityExport")
fbx_count = len(list((PROJECT / "Assets" / "UnityExport").rglob("*.fbx")))
size_mb = sum(f.stat().st_size for f in (PROJECT / "Assets" / "UnityExport").rglob("*") if f.is_file()) / 1048576
print(f"copied {fbx_count} FBX ({size_mb:,.0f} MB) into the Tuanjie project")
print("-" * 78)

# ------------------------------------------------------------------ import
editors = find_unity_editors()
if not editors:
    raise SystemExit("no Unity/Tuanjie editor found")
editor = editors[0]
print(f"editor: {editor.label}")

report = PROJECT / "Assets" / "UnityExport" / "Reports" / "unity_import_report.json"
if report.exists():
    report.unlink()

result = run_batch_import(editor, PROJECT, PROJECT / "Assets" / "UnityExport")
print(f"returncode={result.returncode}  duration={result.duration_s:.1f}s")
print(f"report exists: {report.exists()}")
print("-" * 78)

if not report.exists():
    print("last output:")
    print(result.output_tail)
    raise SystemExit(1)

data = json.loads(report.read_text(encoding="utf-8"))
print("summary:", data["summary"])
for c in data["characters"]:
    print()
    print(f"== {c['character']}  ->  {c['status']}")
    print(f"   importType        : {c['importType']}")
    print(f"   avatarValid       : {c['avatarValid']}")
    print(f"   avatarHuman       : {c['avatarHuman']}   name={c['avatarName']}")
    print(f"   height (metres)   : {c['heightMetres']:.4f}")
    print(f"   width  / depth    : {c['widthMetres']:.4f} / {c['depthMetres']:.4f}")
    print(f"   dominant axis     : {c['dominantAxis']}")
    print(f"   animations        : {c['animationCount']} (failures {c['animationFailures']})")
    print(f"   with root motion  : {c['animationsWithRootMotion']} {c['rootMotionPaths']}")
    print(f"   curve bindings    : {c['totalCurveBindings']:,}")
    print(f"   clips w/ pos curve: {c['clipsWithPositionCurves']}")
    print(f"   problems          : {len(c['problems'])}")
    for p in c["problems"][:5]:
        print(f"      - {p}")
    print(f"   sample curves     : {c['sampleCurves'][:4]}")

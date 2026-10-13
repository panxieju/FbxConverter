# FbxConverter 项目文档

> 面向开发者与维护者。安装、使用与验收方式见 [README.md](README.md)。

---

## 1. 项目定位

把 Unreal Engine 工程里的**骨骼网格 + 动画序列**与**静态网格 + 材质 + 贴图**
批量转换成 Unity 可直接使用的 FBX 包，并把 Unity 侧的导入配置与校验一并自动化。

### 目标

| # | 目标 | 落地方式 |
|---|---|---|
| G1 | 类型判断必须权威 | 只做文件发现，类型/依赖一律由 Unreal AssetRegistry + 实际加载确认 |
| G2 | 按内容组织 | 骨骼内容以 Skeleton 为键分组；静态网格以 `Content` 目录为键分组 |
| G3 | 路径可追溯 | 输出保留相对于 `Content` 的资源路径 |
| G4 | 源目录只读 | 转换产物写入独立工作目录；跨骨架不自动重定向 |
| G5 | 无工程也能用 | 找不到 `.uproject` 时生成独立转换项目，零拷贝挂载源目录 |
| G6 | 失败可定位 | 逐项结果 + 具体缺失路径 + 可重试 |
| G7 | 贴图跟着模型走 | 静态网格所用的材质与贴图**一并导出**，并由 Unity 侧脚本自动装配 |

### 非目标（第一版明确不做）

- `.pak` / `.utoc` / `.ucas` 打包产物（会识别并跳过，且在警告中列出）。
- 完整 Unreal 材质图还原；骨骼网格仅保留材质槽。
- 静态网格的**任意**材质图还原：只还原「基色 / 法线 / 遮罩」三类贴图槽
  （判定依据见 §6.16），材质节点里的算术与混合不还原。
- Physics Asset 与动画蓝图还原。
- 跨 Skeleton 自动重定向。
- 导出时改写 Unreal 资源（见 §6.5）。
- 把贴图二进制**嵌入** FBX。引擎源码层面做不到（见 §6.14），
  替代方案是「PNG 随包 + Unity 侧自动装配」，语义上等价于“贴图和材质一起打包”。

---

## 2. 架构总览

```
┌──────────────────────────────────────────────────────────────┐
│  表现层   cli.py            PyQt5: ui/{app,main_window,pages,widgets,worker}
├──────────────────────────────────────────────────────────────┤
│  编排层   pipeline/scan.py            pipeline/export.py
├──────────────────────────────────────────────────────────────┤
│  领域层   models.py   discover.py   report.py   unity/manifest.py
├──────────────────────────────────────────────────────────────┤
│  引擎适配 unreal/{locator,project,paths,runner}.py
│           unity/runner.py            resources.py
├──────────────────────────────────────────────────────────────┤
│  引擎侧   ue_scripts/*.py  (Unreal 内置 Python 3.11 执行)
│           unity/templates/*.cs  (Unity Editor 执行)
└──────────────────────────────────────────────────────────────┘
```

编排层与表现层**不直接调用引擎**，一律经适配层；引擎适配层不包含业务规则。
这样 CLI 与 GUI 共享同一条流水线，也便于单测在无引擎环境下运行。

---

## 3. 模块清单

| 模块 | 职责 | 关键类型 |
|---|---|---|
| `errors.py` | 异常层级，每个错误带 `code` 与可读细节 | `FbxConvError` → `UnrealRunError` / `UnrealScriptError` / `ContentRootError` … |
| `models.py` | 领域模型；全部可 JSON 序列化 | `AssetKind` `AssetRecord` `SkeletonGroup` `GroupKind` `TextureRequest` `ConversionPlan` `ExportOutcome` |
| `discover.py` | 纯文件系统发现：`.uasset`、伴随文件、`.uproject`、打包产物 | `SourceFile` `DiscoveryResult` |
| `resources.py` | 定位随包数据（源码树 / PyInstaller 冻结） | `package_root()` `ue_scripts_dir()` `templates_dir()` |
| `config.py` | 用户配置持久化（`%APPDATA%/FbxConverter/config.json`） | `AppConfig` |
| `logutil.py` | 统一日志 + 回调 Handler（喂给 GUI 日志面板） | `configure_logging` `CallbackHandler` |
| `report.py` | 导出报告（FBX 与贴图分别计数） | `ExportReport` |
| `unreal/locator.py` | 注册表 / Launcher / 目录扫描定位引擎，并**校验可执行文件存在**；按 `.uproject` 的 `EngineAssociation` 选引擎 | `UnrealInstall` `resolve_install_for_project` `project_engine_association` |
| `unreal/project.py` | 独立转换项目：生成 `.uproject` + 挂载 `Content` | `ConversionProject` `MountMode` |
| `unreal/paths.py` | 文件系统 ↔ Unreal 包路径换算 | `to_package_path` `package_to_object` … |
| `unreal/runner.py` | 无头启动编辑器、传递 job、回收 result、流式事件、取消 | `UnrealRunner` `UnrealScriptResult` |
| `pipeline/scan.py` | 发现 → 定范围 → Unreal 确认 → 分组（骨架 / 静态目录）→ 依赖诊断 | `ScanSession` `group_assets` `group_static_meshes` |
| `pipeline/export.py` | 计划 → 批量导出 FBX → 批量导出贴图 → 写产物 | `ExportSession` `build_plan` `build_texture_requests` `png_size` |
| `unity/manifest.py` | 生成 `manifest.json` / `profile.json`（含贴图与材质表），投放 Editor 脚本 | `build_static_materials` `build_manifest_payload` `install_unity_support` |
| `unity/runner.py` | 定位并批处理驱动 Unity/团结引擎 | `UnityInstall` `run_batch_import` |
| `ue_scripts/` | **在 Unreal 内执行**的脚本（详见 §5） | `fbxconv_common/scan/export/textures.py` |
| `unity/templates/` | **在 Unity 内执行**的 C# 与 asmdef | `UnrealResourceImporter.cs` |

---

## 4. 端到端数据流

### 4.1 扫描（`ScanSession.run`）

```
资源目录
  │ discover()                     纯 Python，只列文件，不猜类型
  ▼
DiscoveryResult{files, project_file, cooked_archives}
  │ project_file is None ?
  ├─ 是 → ensure_conversion_project()   生成 .uproject，Content → 源目录（联接）
  └─ 否 → 直接使用现有 .uproject
  ▼
UnrealRunner.run_script("fbxconv_scan.py", {mount_point, deep_resolve})
  │      rescan 注册表 → 读类型/依赖 → 加载网格与动画读真实 Skeleton
  ▼
AssetRecord[]  ──group_assets()──▶  SkeletonGroup[]  ──▶  ScanResult
```

`ScanResult.unreal_project` 记录**本次真正使用的 `.uproject`**。它是导出阶段的输入，
漏传会导致 Unreal 以“无项目”状态启动（见 §6.3）。

### 4.2 导出（`ExportSession.run`）

```
用户选择 SelectedGroup[] + ExportSettings
  │ build_plan()          展平为 ExportRequest[]，分配 rel 路径并去重
  ▼
ConversionPlan{groups[], texture_requests[]}
  │ 第一次 UnrealRunner.run_script("fbxconv_export.py", {exports[]})
  │      等待注册表就绪 → 逐个 AssetExportTask → 逐项回传
  │ 第二次 UnrealRunner.run_script("fbxconv_textures.py", {textures[]})   ← 仅静态网格
  ▼
ExportOutcome[]  ──▶  profile.json × N  ──▶  manifest.json
                 ──▶  export_report.json
                 ──▶  Editor/ 脚本投放
```

两次启动是刻意分开的：`UTextureExporterPNG` 会对源格式执行**原生 `check()`**，
断言失败直接杀死编辑器进程（不是抛 Python 异常），因此贴图批次绝不能让
已经完成的 FBX 工作陪葬（见 §6.15）。

### 4.3 静态网格与贴图（`fbxconv_textures.py` 侧）

```
静态网格组（按 Content 目录分组）
  │ build_texture_requests()   取所有网格引用贴图的并集，落到共享贴图池
  ▼
StaticMeshes/_Textures/<包的 Content 相对路径>.png
  │ fbxconv_textures.py  逐个 load → 安全检查 → TextureExporterPNG
  │                     每导出一张就重写一次 result.json（崩溃可续）
  ▼
texture_metadata{package → srgb / texture_kind / width / height}
  │ png_size() 读 IHDR 记下真实尺寸（Unity 默认 maxTextureSize=2048，会静默降采样）
  ▼
manifest.json 的 textures[] / materials[] / staticMeshes[]
```

### 4.4 Unity 导入

```
Unity/Tuanjie.exe -batchmode -executeMethod ...ImportFromCommandLine
  │ 环境变量 FBXCONV_UNITY_EXPORT 或回退到 Assets/UnityExport
  ▼
骨骼内容：配置 ModelImporter → 生成/复用 Avatar → 配置每个 clip → 校验
静态内容：导入贴图（sRGB / NormalMap）→ 建共享 Material 资产
          → ModelImporter.SearchAndRemapMaterials 把模型指到这些材质上 → 校验
  ▼
Reports/unity_import_report.json
```

### 4.5 重试语义

`ExportSession.run(requests, previous=...)` 把本次结果与 `previous` 按
`output_rel` 合并，因此“重试失败项”只重启必要的编辑器，且报告仍覆盖全量。

失败项先按 `AssetKind.TEXTURE` 拆成两批：FBX 批次与贴图批次各自只跑自己那部分，
避免把贴图请求塞给 FBX 导出器（那会以 “unsupported export kind: texture” 失败）。

---

## 5. 接口契约

### 5.1 主机 → Unreal

已验证：`-ExecutePythonScript` 执行时 `sys.argv` **只含脚本路径**，因此配置与结果
一律走文件 + 环境变量。

| 环境变量 | 方向 | 含义 |
|---|---|---|
| `FBXCONV_JOB` | 主机 → Unreal | 输入 JSON 绝对路径 |
| `FBXCONV_RESULT` | 主机 ← Unreal | 输出 JSON 绝对路径 |
| `FBXCONV_RUN_DIR` | 主机 → Unreal | 本次运行目录（日志/临时文件） |

调用形式：

```
UnrealEditor-Cmd.exe <project> -ExecutePythonScript=<script> \
  -unattended -nosplash -nopause -stdout -FullStdOutLogOutput \
  -NoSourceControl -abslog=<log> [-nullrhi]
```

脚本执行完编辑器会自行退出。**`-nullrhi` 仅用于扫描**（见 §6.4）。

#### 结果信封

```jsonc
// 成功
{ "ok": true, "data": { /* 各脚本自定义 */ }, "warnings": ["..."] }
// 失败（脚本内任何异常都会落到这里，绝不会静默）
{ "ok": false, "error": "TypeError: ...", "traceback": "..." }
```

#### 事件协议（stdout）

Unreal 侧 `print("FBXCONV:" + json)`，主机按前缀解析并转发给 UI：

```jsonc
{"type":"phase","message":"开始导出","total":42}
{"type":"progress","done":50,"total":145,"message":"已解析 50/145"}
{"type":"item","done":3,"total":42,"name":"MM_Idle","state":"ok","error":null}
{"type":"log","level":"error","message":"..."}
{"type":"fatal","error":"..."}
```

### 5.2 扫描 job / result

```jsonc
// job
{ "mount_point": "/Game",
  "scope_prefix": "/Game/CLazyAnimpack",   // 只报告该子树；空 = 整个挂载点
  "deep_resolve": true, "max_assets": 200000 }
```

```jsonc
// data
{
  "engine_version": "5.8.2-...",
  "content_dir": "G:/proj/Content/",
  "mount_point": "/Game",
  "asset_count": 145,
  "load_failures": [],
  "assets": [{
    "package_path": "/Game/Characters/Mannequin_UE4/Meshes/SK_Mannequin",
    "name": "SK_Mannequin",
    "class": "SkeletalMesh",
    "object_path": "/Game/.../SK_Mannequin.SK_Mannequin",
    "dependencies": ["/Game/.../SK_Mannequin_Skeleton", "/Script/Engine"],
    "skeleton": "/Game/.../SK_Mannequin_Skeleton",
    "materials": ["/Game/.../M_MannequinUE4_Body"],
    "physics_asset": "/Game/.../SK_Mannequin_PhysicsAsset",
    "num_frames": null, "duration": null,
    "disk_path": "G:/proj/Content/Characters/.../SK_Mannequin.uasset",
    "extra": { "tags": {}, "deep_resolved": true, "socket_count": 0 }
  }]
}
```

**类型字段是权威来源**：`class` 直接取自 AssetRegistry，主机用
`classify_class()` 映射为 `AssetKind`。`skeleton` 一律以**加载后的对象**为准。

### 5.3 导出 job / result

```jsonc
// job
{
  "mount_point": "/Game",
  "settings": { "import_type": "Generic", "sampling_rate": 30,
                "loop_mode": "auto", "root_motion": "keep", "scale": 1.0,
                "export_materials": true, "export_textures": true },
  "exports": [{
    "kind": "skeletal_mesh",              // skeletal_mesh | static_mesh | anim_sequence
    "package_path": "/Game/.../SK_Mannequin",
    "object_path":  "/Game/.../SK_Mannequin.SK_Mannequin",
    "output_rel":   "Characters/Mannequin_UE4/Mesh/SK_Mannequin.fbx",
    "output_path":  "G:/out/UnityExport/Characters/.../SK_Mannequin.fbx",
    "display_name": "SK_Mannequin"
  }]
}
```

```jsonc
// data
{ "engine_version": "5.8.2-...", "total": 42, "succeeded": 42, "failed": 0,
  "results": [{
    "package_path": "/Game/.../SK_Mannequin", "kind": "skeletal_mesh",
    "output_path": "G:/out/.../SK_Mannequin.fbx",
    "ok": true, "error": null, "size": 1023312, "elapsed": 0.42 }] }
```

**单项失败不影响整批**：每项独立 `try/except`，错误文本进 `error`，主机标记
`retryable=True`。

### 5.3.1 贴图导出 job / result（`fbxconv_textures.py`）

```jsonc
// job
{ "textures": [{
    "package_path": "/Game/PolygonApocalypse/Textures/T_X",
    "output_path":  "G:/out/UnityExport/StaticMeshes/_Textures/PolygonApocalypse/Textures/T_X.png",
    "display_name": "T_X" }] }
```

```jsonc
// data
{ "complete": true, "total": 7, "succeeded": 7, "failed": 0,
  "engine_version": "5.4.4-...",
  "results": [{
    "package_path": "/Game/.../T_X", "output_path": "G:/out/.../T_X.png",
    "ok": true, "error": null, "size": 2185513, "elapsed": 0.05,
    "srgb": true, "compression": "TC_DEFAULT", "texture_class": "Texture2D",
    "source_file": "U:/Dropbox/SyntyStudios/.../T_X.png",
    "texture_kind": "baseColor" }] }
```

`complete` 是**崩溃探针**：脚本在循环前和每导出一张后都重写一次 result，
所以即使编辑器在导出中途被原生断言杀死，主机仍能读到 `complete: false`
以及已完成的部分，并把剩下的项标成可重试（见 §6.15）。

### 5.4 主机 → Unity

`manifest.json` 与 `profile.json` 的字段形状**受 Unity `JsonUtility` 约束**：
根必须是对象、只用嵌套可序列化类与字符串数组、**数值字段不得为 `null`**。
因此 Unity 侧零第三方 JSON 依赖（实测编辑器默认不加载 Newtonsoft）。

命名约定：面向 Unity 的两个文件用 camelCase；Python 侧自用的
`export_report.json` 用 snake_case。

```jsonc
// manifest.json（节选）
{
  "schemaVersion": 1,
  "pathsRelativeTo": "UnityExport",
  "settings": { "importType": "Generic", "samplingRate": 30, "rootMotion": "keep" },
  "importerScript": "Editor/UnrealResourceImporter.cs",
  "reportPath": "Reports/export_report.json",
  "characterCount": 2, "animationCount": 40,
  "characters": [{
    "name": "Mannequin_UE4", "meshPath": "Characters/Mannequin_UE4/Mesh/SK_Mannequin.fbx",
    "profilePath": "Characters/Mannequin_UE4/profile.json",
    "animationCount": 1, "animationPaths": ["..."], "missingDependencies": []
  }]
}
```

```jsonc
// profile.json（节选）
{
  "schemaVersion": 1, "group": "Mannequin_UE4",
  "importType": "Generic", "scale": 1.0, "samplingRate": 30, "motionNode": "",
  "mesh": { "fbx": "Characters/.../SK_Mannequin.fbx", "name": "SK_Mannequin",
            "avatarName": "Mannequin_UE4_Avatar" },
  "animations": [{
    "name": "Jog_Fwd", "fbx": "Characters/.../Jog_Fwd.fbx",
    "loop": true, "rootMotion": true,
    "lengthSeconds": 1.966667, "frames": 60, "frameRate": 30.0
  }],
  "materials": [{ "name": "M_MannequinUE4_Body", "unrealMaterial": "/Game/..." }],
  "missingDependencies": [], "notes": []
}
```

静态网格**没有** `profile.json`：它没有骨架、Avatar 或剪辑集可描述，
全部信息都在 `manifest.json` 的三张表里。三张表互相用**名字与下标**引用，
而不是嵌套展开——实测 1926 个网格的包若把材质表复制到每个网格，
JSON 会膨胀一个数量级，而 `JsonUtility` 要整体解析它。

```jsonc
// manifest.json（静态网格部分，节选）
{
  "staticMeshCount": 5, "textureCount": 7, "materialCount": 4,

  // 贴图表：name 是表内唯一键（不同包同名贴图会得到 _1 后缀）
  "textures": [{
    "name": "T_PolygonApocalypse_01_A",
    "path": "StaticMeshes/_Textures/PolygonApocalypse/Textures/T_PolygonApocalypse_01_A.png",
    "unrealPackage": "/Game/PolygonApocalypse/Textures/T_PolygonApocalypse_01_A",
    "srgb": true, "textureType": "baseColor", "unrealClass": "Texture2D",
    "bytes": 2185513, "width": 2048, "height": 2048
  }],

  // 材质表：Unity 侧据此建 Material 资产并按名回填到模型
  "materials": [{
    "name": "Mat_PolygonApocalypse_01_A",
    "unrealMaterial": "/Game/PolygonApocalypse/Materials/Mat_PolygonApocalypse_01_A",
    "baseColor": "T_PolygonApocalypse_01_A",
    "normal": "", "mask": "",
    "allTextures": ["T_PolygonApocalypse_01_A"]
  }],

  // 网格表：slots 用 materialIndex 指回材质表
  "staticMeshes": [{
    "name": "SM_Bld_Apartment_01",
    "fbx": "StaticMeshes/Meshes/Buildings/SM_Bld_Apartment_01.fbx",
    "unrealPackage": "/Game/PolygonApocalypse/Meshes/Buildings/SM_Bld_Apartment_01",
    "group": "Meshes_Buildings", "relativeDir": "Meshes/Buildings",
    "triangleHint": 128, "slotCount": 1,
    "slots": [{ "slotName": "PolygonApocalypse_Mat_01_A", "materialIndex": 0 }]
  }]
}
```

### 5.5 环境变量总表

| 变量 | 使用方 | 作用 |
|---|---|---|
| `FBXCONV_UNREAL_EDITOR` | `unreal/locator` | 直接指定 `UnrealEditor-Cmd.exe` 或引擎根目录 |
| `FBXCONV_UNITY_EDITOR` | `unity/runner` | 直接指定 `Unity.exe` / `Tuanjie.exe` |
| `FBXCONV_UNITY_EXPORT` | C# 导入器 | 指定 `UnityExport` 目录 |
| `FBXCONV_UNITY_PACKAGE` | C# 打包 | 指定输出的 `.unitypackage` 路径 |
| `FBXCONV_UNITY_PACKAGE_INCLUDE_EDITOR` | C# 打包 | `0`/`false` 时包内不含 `Editor/` 脚本 |
| `FBXCONV_FORCE_CLI` | `packaging/entry.py` | 让窗口版 exe 走 CLI（调试用） |
| `FBXCONV_BUILD_MODE` | `FbxConverter.spec` | `onedir`（默认）/ `onefile` |
| `FBXCONV_JOB` / `FBXCONV_RESULT` / `FBXCONV_RUN_DIR` | `ue_scripts` | 见 §5.1 |

### 5.6 打包 `.unitypackage`

多了一条 Unity 侧命令：

```
fbxconv unity-package <UnityExport> --project <Unity工程> --unity-package <文件> [--no-import] [--no-editor-scripts]
```

C# 侧两个批处理入口：

| `-executeMethod` 后缀 | 行为 |
|---|---|
| `ImportAndPackFromCommandLine` | 先导入，再打包（默认） |
| `ExportPackageFromCommandLine` | 只打包工程里已导入的内容（`--no-import`） |

**为什么必须在编辑器里做**：`.unitypackage` 是 gzip tar，条目为
`<guid>/asset` + `<guid>/asset.meta` + `<guid>/pathname`。`asset.meta` 就是
Unity 的导入设置 YAML，而 `AssetDatabase.ExportPackage` 只接受编辑器**已经导入**
且位于 `Assets/` 下的资源。走这条路，包里带的正是本工具配置好的设置
（Avatar、每个剪辑的 `loopTime`、贴图 `textureType`/`sRGBTexture`、材质与贴图的
GUID 绑定）；手写 tar 就只能自己编 YAML，一旦 meta 不完整，接收方工程会按默认值
重新导入——Avatar 丢失、循环标记归零、法线贴图当基色用。

两条硬约束：

- 包必须写在**目标工程的 `Assets/` 之外**（否则 Unity 会把它当资源导入），
  C# 侧 `IsInsideAssets()` 会直接拒绝。
- 打包前必须先导入：`ExportUnityPackage` 会用 `AssetDatabase.IsValidFolder`
  与 `LoadAssetAtPath` 逐个过滤，跳过尚未导入的路径，避免 Unity 为未知路径
  报错并吐出一个**残缺**的包。

实测（Rapier_AnimSet，117 FBX + 4 贴图 + Editor 脚本，团结引擎 2022.3.62t11）：
73.7 MB 导出目录 → **11.0 MB** `.unitypackage`，45 秒。
随后用一个**全新工程** `-importPackage` 导入并独立核对：
117 模型 / 113 剪辑 / 4 贴图 / 3 静态网格，Avatar `SK_MannequinAvatar` 有效，
角色包围盒 1.4075×1.8329×0.4352 m，68 骨骼，循环标记 **18 循环 / 95 非循环**
全部保留，贴图类型与 sRGB 全部正确。

---

## 6. 关键设计决策

### 6.1 为什么用环境变量而不是命令行参数

实测（UE 5.4）：脚本内 `sys.argv == ['<script.py>']`，`-ExecutePythonScript` 之后
追加的参数不会进入脚本。环境变量是唯一稳定通道，且不受含空格路径影响。

### 6.2 为什么一次启动导出整批

单次冷启动约 17–70 秒（含注册表收集），交换成本远高于导出本身。
42 个 FBX 一次导出仅 26 秒；逐个启动将变成数十分钟。

### 6.3 为什么独立转换项目必须传给导出阶段

`ConversionPlan.unreal_project` 保存扫描时**实际使用**的 `.uproject`。
若误用源工程的 `.uproject`（独立目录下为 `None`），Unreal 会在无项目状态启动：
挂载点消失、注册表缓存退回 `%LOCALAPPDATA%\UnrealEngine\<ver>\...`，
表现为**资源明明存在却 `load_asset` 返回 None**。这是最难察觉的一类回归，
诊断方法是核对日志中 `Asset registry cache read as ... from <路径>`。

### 6.4 为什么导出不能用 `-nullrhi`

`USkeletalMeshExporterFBX` 经 MeshMergeUtilities 构造蒙皮组件，
在无 RHI 时触发 `Assertion failed: MeshObject`（`SkinnedMeshComponent.cpp:4987`）
并让编辑器崩溃（退出码 3）。扫描阶段无此依赖，仍用 `-nullrhi` 提速。

### 6.5 为什么采样率/循环/Root Motion 在 Unity 侧生效

独立转换项目通过 directory junction 暴露**用户的原文件**。若在导出时改写
`AnimSequence.sampling_frame_rate`，即使不保存，也把用户资源置于被误写的风险中。
因此这三项写入 `profile.json`，由 C# 导入器应用——既安全，也确实作用在
Unity 真正读取的参数上。

### 6.6 为什么默认目录联接而非复制

源目录常常数十 GB。junction 零拷贝零等待，且天然满足“源目录只读”。
失败（如跨卷）时自动回退整卷复制；`--link-mode` 可显式选择。

### 6.7 为什么 manifest 迁就 `JsonUtility`

Unity 编辑器默认不加载 Newtonsoft（已实测 `newtonsoftJsonLoaded: false`）。
受限于 `JsonUtility` 不支持字典、不支持根数组、值类型不可为 `null`，
于是 schema 被刻意设计成嵌套类 + 字符串数组 + 全零默认值。
代价是字段稍显冗余，收益是 Unity 侧零依赖、零额外安装步骤。

### 6.8 为什么 Editor 脚本随导出目录走

`UnityExport/Editor/` 让导出结果**自包含**：整个目录丢进 `Assets/` 即可用。
同时 `install_unity_support()` 会检测导出目录是否已在目标工程 `Assets` 内，
在内部时**跳过第二次投放**——否则同一份 C# 被编译两次会触发 CS0101。

EditMode 测试的投放由 Python 决策（检查 `Packages/manifest.json` /
`packages-lock.json` 是否解析到 `com.unity.test-framework`），而不是靠 asmdef
的 `defineConstraints`。原因：`UNITY_INCLUDE_TESTS` 在编辑器下**始终已定义**，
实测无法用它跳过；而一个编译失败的测试程序集会**阻断整个工程进入 Play 模式**。

### 6.9 挂载点 ≠ 扫描范围

用户选择 `Content/CLazyAnimpack` 时，两件事必须分开：

- **挂载点**必须是 `Content`，否则资源内部的 `/Game/CLazyAnimpack/...` 引用无法解析；
- **扫描范围**必须是 `/Game/CLazyAnimpack`，否则会把整个工程（实测 7596 个资源）都扫进来。

早期实现把两者混为一谈，结果是“我只选了一个文件夹，它扫了我整个项目”。
现在 `ScanSession._resolve_scope()` 从「选择目录」相对「挂载点」推导出包前缀，
作为 `scope_prefix` 传给 Unreal 侧：注册表按该前缀查询，而**依赖仍在整个挂载点内解析**。

### 6.10 引擎版本必须跟随项目声明

`.uproject` 里的 `EngineAssociation`（如 `"5.4"`）是权威信息。默认取「最新安装」
会让 5.4 的工程被 5.8 编辑器打开，触发资源升级并可能改变行为。
`resolve_install_for_project()` 因此让声明优先于「最新」，未安装该版本时才回退并告警。

`ScanResult.unreal_editor` 记录实际使用的编辑器，导出阶段复用它——否则扫描用 5.4、
导出用 5.8，两边看到的资源版本可能不一致。

### 6.11 Humanoid 与 Generic 的 Root Motion 存法不同

这是实测第三方动作包时踩到的：**Humanoid 剪辑根本不产生 `m_LocalPosition` 曲线**，
根位移以肌肉曲线 `RootT.x/y/z` 的形式存在。只按 `m_LocalPosition` 检测，
会把 212 个动画里的根运动全部误报为 0。

现在 `AnalyseRootMotion()` 同时识别两种约定，并区分：

- `animationsWithRootMotion` —— 根骨骼是否发生位移（含垂直起伏）
- `animationsWithRootTravel` —— 是否发生**水平**位移，即真正会“走开”的动画

后者才是有用的信号：原地循环动画通常有垂直起伏但没有水平位移。

### 6.12 `manifest.json` 不能带 BOM

实测：用 PowerShell 的 `Set-Content -Encoding UTF8` 改写 Unity 的
`Packages/manifest.json` 会写入 BOM（`EF BB BF`），**Unity 会直接忽略该文件**——
不报错、不提示，只是什么都不做，看起来像“改了没用”。

用无 BOM 的 UTF-8 重写后同一份内容立刻生效。任何自动化改写
`manifest.json` / `packages-lock.json` 的代码都必须显式写 UTF-8 without BOM。

### 6.13 GUI 槽函数异常会终止整个进程

PyQt5 从 C++ 调用 Python 槽函数时，逃逸的异常不会只是打印堆栈，而是走到
`qFatal` → 进程以 `0xC0000409` 结束。曾因 `QPlainTextEdit` 误用
`setTextColor`（那是 `QTextEdit` 的方法）导致一开界面就崩。

规则：**槽函数内部不假设任何东西都正常**。日志面板这类“尽力而为”的 UI
用 `contextlib.suppress` 包住，丢一行日志远好过丢整个会话。

另：该 bug 逃过了最初的离屏冒烟测试，因为那个测试没有调用
`configure_logging()`，日志级别停在 WARNING，记录从未进到面板——
`tests/test_ui_logging.py` 现在专门驱动这条链路。

### 6.14 贴图**无法**嵌入 FBX：这是引擎硬编码，不是配置项

需求原话是“贴图和材质一起打包进 fbx”。查了 UE 5.4 的引擎源码后确认做不到：

```cpp
// Engine/Source/Editor/UnrealEd/Private/Fbx/FbxMainExport.cpp:303,326
void FFbxExporter::WriteToFile(const TCHAR* Filename)
{
    bool bEmbedMedia = false;            // ← 写死
    ...
    IOS_REF.SetBoolProp(EXP_FBX_EMBEDDED, bEmbedMedia);
```

`UFbxExportOption` 也**没有任何**相关开关。实测（UE 5.4）逐个尝试
`export_textures` / `embed_textures` / `export_materials` / `media` / `texture_format`
全部被 `set_editor_property` 拒绝；该类的完整属性只有 13 个：
`ascii / bake_actor_animation / bake_camera_and_light_animation / collision /
export_local_time / export_morph_targets / export_preview_mesh / export_source_mesh /
fbx_export_compatibility / force_front_x_axis / level_of_detail /
map_skeletal_motion_to_root / vertex_color`。

导出结果也印证了这一点：对一个真实静态网格导出的 FBX 里
`Content` 节点数 = 0、无 PNG/JPEG 字节，但 `Video` 节点 6 个、`Texture` 节点 25 个，
并且记录的贴图路径是**原作者机器上的绝对路径**：

```
U:/Dropbox/SyntyStudios/PolygonApocalypse/_Working/Textures/Alts/PolygonApocalypse_Texture_01_A.png
```

该路径在本机不存在（`AssetImportData->GetFirstFilename()` 指向的就是它）。
即便 FbxExportOption 能嵌，路径也是错的。

更糟的是 `FFbxExporter::ExportMaterial()` 只在**槽位由贴图参数节点驱动**时才写贴图
节点（要求 `Expression->GetParameterName() != NAME_None` 且
`Value->AssetImportData` 非空）；普通 `TextureSample` 节点的材质在 FBX 里
连一张贴图都没有，只剩一个颜色。

**结论与取舍**：既然 FBX 里注定只有（甚至没有）失效引用，就把贴图**单独导出成 PNG**
与 FBX 并排放进同一个交付目录，再由我们自己投放的 `UnrealResourceImporter.cs`
按 `manifest.json` 建材质并回填。对用户而言结果一致——“模型进 Unity 就带着贴图”，
而且绕开了上面两个引擎缺陷。

### 6.15 贴图导出必须独立于 FBX 导出

`UTextureExporterGeneric::ExportBinary` 里有一句原生断言：

```cpp
// EditorFactories.cpp:4974,4981
UTexture* Texture = GetExportTexture(Object);
UTexture2D* Texture2D = Cast<UTexture2D>(Texture);
check( Texture2D != nullptr );
check( SupportsTexture(Texture) );
```

`check` 失败**不会**抛 Python 异常，而是直接终止编辑器进程（实测退出码 3，
日志里是 `Assertion failed: SupportsTexture(Texture)`）。
`SupportsTexture` 对 PNG 的实现是 `!IsHDR(ConvertToRawImageFormat(Source.GetFormat()))`，
而 `Source` 在 Python 里读不到——也就是说**无法在调用前完全判定安全性**。

于是采取三层防护：

1. **分两次启动编辑器**：贴图批次崩溃不会带走已经完成的 FBX。
2. **崩溃前先落盘**：`fbxconv_textures.py` 在循环前和每张之后重写 `result.json`，
   主机据 `complete: false` 判定“崩溃于第 N 张之后”，其余项标为可重试。
3. **能查的先查**：跳过非 `Texture2D`、跳过 `virtual_texture_streaming`、
   跳过 `TC_HDR/HalfFloat`（这些是已知会让断言失败的输入）。
   绝不再实例化抽象基类 `TextureExporterGeneric`——最初那次崩溃正是它。

### 6.16 贴图角色靠 sRGB + 压缩设置推断，而不是靠材质图

`MaterialEditingLibrary.get_used_textures()` 看起来是标准答案，但实测对
`/Game/PolygonApocalypse/Materials/Mat_PolygonApocalypse_01_A` 返回**空数组**
（`get_texture_parameter_names` 同样为空），而 `Material.expressions` 又被
Python 侧保护：`Property 'Expressions' is protected and cannot be read`。
也就是说“哪个贴图接在 BaseColor 上”在 Python 里拿不到。

改用 AssetRegistry 依赖图：`material → (MaterialInstance 父链, 最多 4 层) → Texture*`。
实测能正确解析出 `Mat_PolygonApocalypse_01_A → T_PolygonApocalypse_01_A`，
以及 `M_UE4Man_Body1_Inst1 → 7 张贴图`。

角色归类只用 Unreal 能给出的**权威信号**：

| 判定 | 依据 |
|---|---|
| `normal` | `compression_settings == TC_NORMALMAP`，或名字含 `_normal/_nrm/_norm` |
| `mask` | `srgb == False`（线性数据：粗糙度/金属度/AO/遮罩） |
| `baseColor` | 其余（sRGB 颜色数据） |

这是**启发式**，所以在文档与报告里都如实标注；一个材质解析出多张同角色贴图时
取第一张，并在 `allTextures` 里保留全部，方便人工核对。

### 6.17 用 `SearchAndRemapMaterials` 而不是 `AddRemap`

Unity 的模型导入器默认会在每个 FBX 旁生成自己的一套材质，同一个 Unreal 材质
被 1900 个网格引用就会产生 1900 份副本，后续改一次材质要改 1900 次。

做法是：**先**在 `StaticMeshes/Materials/` 下建好共享 Material 资产（名字就用
Unreal 材质名，因为 `FFbxExporter::ExportMaterial` 正是用
`MaterialInterface->GetName()` 命名 FBX 里的材质节点），**再**导入模型并让
导入器按名回填：

```csharp
importer.materialImportMode = ModelImporterMaterialImportMode.ImportStandard;
importer.materialName   = ModelImporterMaterialName.BasedOnMaterialName;
importer.materialSearch = ModelImporterMaterialSearch.RecursiveUp;
importer.SaveAndReimport();
importer.SearchAndRemapMaterials(ModelImporterMaterialName.BasedOnMaterialName,
                                 ModelImporterMaterialSearch.RecursiveUp);
```

`AddRemap(SourceAssetIdentifier, Object)` 也可行，但它要求我们自己拼出 FBX 内部
材质名；`SearchAndRemapMaterials` 用同样的名字规则让 Unity 自己找，
少一处可能对不上的假设。实测回填成功：模型的 `sharedMaterials` 指向
`Assets/UnityExport/StaticMeshes/Materials/*.mat`，而不是 Unity 默认材质或子资产。

Unity 版本差异已实测确认（团结引擎 2022.3.62t11）：
`ModelImporterMaterialLocation{External,InPrefab}`、
`ModelImporterMaterialImportMode{None,ImportStandard,LegacyImport,ImportViaMaterialDescription,Import}`、
`SearchAndRemapMaterials`、`AddRemap` 均存在；`TextureImporter` 的
`sRGBTexture/textureType/mipmapEnabled/wrapMode/maxTextureSize` 均存在。

### 6.18 静态网格按 Content 目录分组，贴图共用一个池

静态网格没有骨架，唯一的天然组织信息就是它所在的目录。于是分组键 =
「选择目录」相对「挂载点」的父路径：选 `Content/PolygonApocalypse` 时，
`.../Meshes/Buildings/SM_X` 落到 `StaticMeshes/Meshes/Buildings/`。
实测 1926 个网格分到若干目录，而不是在一个扁平目录里堆 1926 个文件。

贴图**不**跟着每个网格目录走：PolygonApocalypse 有 1926 个网格、十几个目录，
共用的图集只有几张，按目录复制会让交付体积翻倍。
贴图统一落到 `StaticMeshes/_Textures/<包的 Content 相对路径>/`，
既保持无重名、又保留可读的来源路径。

坐标与单位沿用骨骼链路已验证的默认值（不设 `useFileScale=false`）：
实测 `1M_Cube` 导入后正好 1.000×1.000×1.000 m，说明 UE 的厘米 → Unity 的米
换算在静态网格上同样正确。

### 6.19 向导状态必须即时发布，且拒绝要给出理由

用户实测反馈：“第二步点击扫描没反应”。

根因是向导页之间的一条**单向状态链断了**：

```python
# 修复前
def _start_scan(self):
    if self.window.install is None or self.window.config.content_root is None:
        return                      # ← 静默返回，界面上什么都不会发生
    self.window.start_scan()        # 它内部才调用 apply_to_config()
```

`config.content_root` 只由 `DirectoryPage.apply_to_config()` 写入，而首次配置时
该字段是 `None`，且**没有任何地方**在第 1 步 → 第 2 步的切换中把输入框内容发布出去。
于是表单填得完全正确，按钮就是不动。

两个连带问题：

- 如果 `AppConfig` 里**恰好记住了**上一次的路径，界面会**静默扫描那个旧目录**，
  比没反应更危险——用户以为扫的是新选的目录。
- 底部状态栏读的也是 `config`，于是持续显示“请选择 Unreal 资源目录”，
  与输入框内容自相矛盾。这是当时界面上唯一的线索。

修法：

1. `DirectoryPage.apply_to_config(remember=False)` 在**每次编辑**时发布，
   `on_leave()` 再发布一次（`remember=True`）；
2. `content_root` 只在**真正解析为已存在目录**时才写入，
   半截路径与过期路径都进不了 config；
3. `content_problem() -> str | None` 返回具体原因，`_start_scan()` 用它替换静默返回，
   并由 `window.warn()` 弹给用户；
4. 输出目录与挂载方式原来只连着 `_refresh_state`，同样没发布 config，
   现在一起走 `_sync_and_refresh()`。

教训写成规则：**任何按钮都不许“什么都不做”**。要么干活，要么说清楚为什么不能干。

### 6.20 `QListWidgetItem` 与 `QTreeWidgetItem` 的勾选接口不是同一个形状

修好上一个问题后，用户点进第 3 步立刻以 `0xC0000409` 崩溃：

```
File ".../ui/pages.py", line 674, in _load_group
    entry.setCheckState(0, Qt.Checked)
TypeError: setCheckState(self, state: Qt.CheckState): too many arguments
```

两者签名不同：

| 类型 | 写 | 读 |
|---|---|---|
| `QTreeWidgetItem` | `setCheckState(column, state)` | `checkState(column)` |
| `QListWidgetItem` | `setCheckState(state)` | `checkState()` |

选择页把**树**的签名用在了**列表**上（动画清单与静态网格清单各一处 `set`，
另有 5 处 `checkState(0)` 读取）。这不是静态网格功能引入的：
动画清单从一开始就是错的，只是此前没有人走到过第 3 步。

失败方式与 §6.13 完全一致——`TypeError` 从 Qt 槽函数逃逸 → `qFatal` →
进程以 `0xC0000409` 结束。**PyQt 槽函数里的 API 误用不会只打印堆栈，它会杀掉进程。**

之所以漏掉，是因为最初只覆盖了第 1、2 步。现在 `tests/test_ui_flow.py` 用
**真实的 `Rapier_AnimSet` 扫描结果**（167 资源 / 2 组）驱动整条向导：
逐组打开、全选/全不选/仅循环、过滤、`build_selection`、设置页预览、
`ensure_plan`、运行页表格与条目更新。这类“把每个控件都摸一遍”的测试是这里唯一
有效的防线——静态检查看不出 `Qt` 重载签名，只有真的把控件建出来才会报错。

### 6.21 信号会把参数塞进**带默认值**的形参

第三次同族崩溃，这次在“开始转换”：

```
File ".../ui/worker.py", line 112, in __init__
    self._requests = list(requests) if requests is not None else None
TypeError: 'bool' object is not iterable
```

`QPushButton.clicked` 会发出 `checked`（普通按钮永远是 `False`）。
而 `RunPage` 是直连的：

```python
self.start_button.clicked.connect(self.window.start_export)   # requests=False ！
```

PyQt 会把信号参数**按槽函数的形参个数截断**——但**带默认值的形参照样会被填**。
实测（`QPushButton.clicked`）：

| 槽函数形态 | 实际收到 |
|---|---|
| `def slot(self)` | `()` |
| `def slot(self, value)` | `(False,)` |
| `def slot(self, value=None)` | `(False,)` |
| `def slot(self, value=None, other=None)` | `(False, None)` |

所以 `start_export(self, requests=None, previous=None)` 收到的是
`requests=False, previous=None` → `list(False)` → 槽函数抛异常 → `qFatal` → `0xC0000409`。
**这也解释了为什么前面 10 个直连的按钮都没事**：它们的槽函数除 `self` 外没有形参，
PyQt 一个参数都不传。

修法两处，互为保险：

1. `start_export` 的两个参数改为**仅关键字**（`*`）——`co_argcount` 不含仅关键字参数，
   PyQt 便不再传参；
2. `RunPage` 的四个按钮改为 `lambda: ...` 显式丢弃 `checked`，
   不再依赖“槽函数签名恰好没有形参”这种脆弱前提。

审计命令（`ast` 遍历 + 正则匹配 `.connect(`）已固化成
`TestNoConnectedSlotAcceptsOptionalArguments`：它断言 UI 包里没有任何
`connect(...)` 直连带默认值形参的方法，报错时给出文件名与行号。
再配上“把每一页每个按钮/复选框/下拉框都动一遍”的运行时测试，这类问题
既有静态拦截也有动态兜底。

> 三次崩溃（§6.13 `setTextColor`、§6.20 `setCheckState`、§6.21 信号参数）
> 都是同一个模式：**异常从 Qt 槽函数逃逸 = 进程死亡**。
> 因此这里的策略不是“再写一个用例”，而是“把每个控件都动一遍 +
> 用 `ast` 拦掉已知的接口误用形状”。

---

## 7. 目录结构

```
FbxConverter/
  FbxConverter.spec            PyInstaller 打包描述
  run.py                       源码运行入口（GUI / CLI 分发）
  pyproject.toml               依赖、ruff、pytest、mypy 配置
  README.md                    使用与验收
  project.md                   本文档
  packaging/
    entry.py                   打包后两个 exe 共用入口（按 exe 名分发）
    version_info.txt           Windows 版本资源
  src/fbxconv/
    __main__.py                 python -m fbxconv
    cli.py  config.py  discover.py  errors.py  logutil.py
    models.py  report.py  resources.py
    pipeline/{scan,export}.py
    unreal/{locator,paths,project,runner}.py
    unity/{manifest,runner}.py
    unity/templates/           → 投放到 Unity
      UnrealResourceImporter.cs  FbxConverter.Editor.asmdef
      Tests/{UnrealImportTests.cs, FbxConverter.Tests.asmdef}
    ue_scripts/                → 在 Unreal 内执行
      fbxconv_common.py  fbxconv_scan.py  fbxconv_export.py  fbxconv_textures.py
    ui/{app,main_window,pages,widgets,worker,__init__}.py
  tests/                       217 个单元测试
    test_discover.py           目录解析、.uproject 查找、伴随文件
    test_paths.py              包路径换算、文件名净化与去重
    test_models.py             资源分类、骨架分组、同名消歧、空组过滤
    test_static_meshes.py      静态网格分组、贴图计划、manifest 三张表、
                               PNG 尺寸探测、贴图/FBX 重试拆分
    test_engine_selection.py   引擎版本跟随声明、扫描范围推导
    test_artefacts.py          profile/manifest/report 生成、配置持久化
    test_diagnostics.py        挂载点不匹配诊断
    test_unity_install.py      Editor 脚本投放与 Test Framework 门控
    test_ui_logging.py         GUI 日志链路（曾在真实运行时崩过）
    test_ui_flow.py            向导状态发布、扫描按钮、选择页勾选清单、\n                               设置/运行页（曾在真实运行时崩过两次）
```

---

## 8. 开发环境与工作流

### 依赖

```powershell
& "G:\Program Files\Python313\python.exe" -m pip install -e ".[gui,dev]"
& "G:\Program Files\Python313\python.exe" -m pip install pyinstaller   # 仅打包需要
```

### 日常命令

```powershell
python run.py                       # GUI
python run.py scan <dir> --json     # 扫描
python run.py convert <dir> --out <out> --all
python -m pytest tests -q           # 测试
python -m ruff check src tests run.py packaging
```

### 约定

- **`ue_scripts/` 与 `unity/templates/` 是数据，不是模块。** 不要 import 它们，
  也不要给 `ue_scripts/` 加 `__init__.py`——那会让 PyInstaller 把它当包冻结，
  破坏 Unreal 侧执行。
- 面向 Unity 的 JSON 字段保持 camelCase 且不得为 `null`。
- 新增随包数据时，同时更新 `resources.py`、`FbxConverter.spec` 的 `datas`
  与 `pyproject.toml` 的 `package-data`。
- `ue_scripts/` 内使用 `%` 格式化与显式 `try/except` 探测引擎 API，
  ruff 已按目录豁免 `UP031` / `SIM105` / `RUF046`。

### 修改引擎侧脚本后的验证节奏

引擎脚本改动无法靠单测覆盖，必须实跑。一次完整回归约 2 分钟：

```
scan(17s) → export(26s) → unity-import(13s)
```

### 8.4 用 Unity MCP 在**运行中的**编辑器里验证

批处理导入（§4.3）需要独占工程，且看不到界面。若编辑器已经开着，
装一次 [MCP for Unity](https://github.com/CoplayDev/unity-mcp) 就能直接在活的
编辑器里跑导入、读 API、截图，省掉反复重启：

```powershell
# 作为 embedded package 投放（比 file: 引用更自包含）
Copy-Item H:\Downloads\unity-mcp-main\MCPForUnity `
          <UnityProject>\Packages\com.coplaydev.unity-mcp -Recurse
```

Unity 需要重新解析包才会编译。若编辑器在后台，它**不会主动刷新**；
用 MCP 的 `refresh_unity(mode="force", compile="request")` 推一把即可。
桥接起来后监听 `127.0.0.1:6400`。

验证时**不要只读自己写的报告**，用 Unity 自己的 API 交叉核对：

```csharp
// 尺寸与轴向：直接读 SkinnedMeshRenderer.bounds
var smr = inst.GetComponentInChildren<SkinnedMeshRenderer>();
smr.bounds.size;                    // 应为 (1.41, 1.83, 0.44) 米，Y 最大

// Avatar
avatar.isValid / avatar.isHuman / avatar.humanDescription.human.Length

// 根运动：用引擎算好的平均速度，而不是自己扫曲线
clip.averageSpeed;                  // Vector3，单位 m/s
```

`AnimationClip.averageSpeed` 是这里的关键——它是 Unity 对根运动的权威解释，
比自己解析 `RootT`/`m_LocalPosition` 曲线可靠得多（见 §6.11）。

在场景里临时实例化 → `manage_scene scene_view_frame` → 截图 → 删除，
可以既拿到目视确认，又不留改动。

---

## 9. 打包与发布

```powershell
pyinstaller --clean --noconfirm FbxConverter.spec
```

产出 `dist/FbxConverter/`，内含：

| 文件 | 说明 |
|---|---|
| `FbxConverter.exe` | 窗口版（GUI） |
| `fbxconv.exe` | 控制台版（CLI） |
| `_internal/fbxconv/ue_scripts/*.py` | **必须存在**，Unreal 直接读取 |
| `_internal/fbxconv/unity/templates/**` | C# 与 asmdef 模板 |

`onedir` 为默认：启动即时，且不必每次运行都解压约 120 MB 的 Qt。
需要单文件时：

```powershell
$env:FBXCONV_BUILD_MODE = "onefile"
pyinstaller --clean --noconfirm FbxConverter.spec
```

单文件模式下两个 exe 会各自内嵌完整负载，体积约翻倍。

### 发布前检查清单

```powershell
# 1. 两个 exe 都能识别引擎
dist\FbxConverter\fbxconv.exe unreal-list
dist\FbxConverter\fbxconv.exe unity-list

# 2. 随包数据存在且完整
Test-Path dist\FbxConverter\_internal\fbxconv\ue_scripts\fbxconv_export.py
Get-ChildItem dist\FbxConverter\_internal\fbxconv\unity\templates -Recurse

# 3. 冻结态确实走 _MEIPASS
dist\FbxConverter\fbxconv.exe scan <资源目录> --out <输出目录> --json
#    并用 -v 确认 resources.describe() 报 "frozen bundle"

# 4. GUI 能起
dist\FbxConverter\FbxConverter.exe
```

> `FbxConverter.exe` 是窗口子系统程序，PowerShell 的 `&` **不会等待**它结束。
> 需要等待时用 `Start-Process -Wait`，或直接用 `fbxconv.exe`。

---

## 10. 已知限制与后续方向

| 限制 | 影响 | 可选方向 |
|---|---|---|
| 每次扫描/导出各需一次引擎冷启动 | 交互式迭代偏慢 | 常驻编辑器 + 文件/套接字命令队列（§6.2 的延伸） |
| 静态网格扫描必须**加载**网格，UE 会顺带构建它 | 实测 0.2–0.4 秒/个：1926 个网格的包扫描 371 秒（导出本身约 5 分钟） | 把材质/贴图发现整体挪到导出阶段（导出器本来就要加载网格），扫描只留文件+注册表；代价是导出前看不到贴图数量 |
| 采样率不写回 Unreal | UE 侧帧率仍为原值 | 在显式勾选后于副本上改写 |
| 骨骼网格的材质仅保留槽位 | 需在 Unity 手工重连材质 | 复用静态网格那套贴图导出 + 材质表生成（§6.16 同样适用于骨骼网格） |
| 静态网格材质只还原三类贴图槽 | 复杂材质图（叠加、遮罩运算、顶点色混合）不还原 | 解析 `MaterialExpression` 子集并映射到 Shader Graph / HLSL |
| 贴图角色是启发式判定 | 名字与压缩设置都不典型的贴图可能归错槽 | 从材质图取权威连接（需先解决 Python 侧 `expressions` 被保护的问题） |
| 不嵌入 FBX，PNG 随包输出 | 交付物是「FBX + PNG + manifest」，不是一个自包含 FBX | 自写二进制 FBX 读写器注入 `Video.Content`；但需先实测 Unity 是否读取嵌入媒体 |
| 大于 2048 的贴图会被 Unity 默认上限降采样 | 4K 图集进 Unity 变 2K | 已在导入报告里显式警告；可加“保持原始尺寸”选项 |
| 无跨骨架重定向 | 不同骨架需分别导出 | 接入 IK Retargeter |
| `Rigs/Poses/*_anim` 会被当作普通动画列出 | 选择列表偏长 | 依据 ControlRig 归属自动分组或默认折叠 |
| `.pak/.utoc/.ucas` 不支持 | 打包资源需先解包 | 集成 UnrealPak 解包前置步骤 |
| 输出把动作平铺在 `Animations/` 下 | 源目录的 `Root_motion/` 等分组会丢失（同名靠后缀消解） | 提供“保留源子目录结构”选项 |

---

## 11. 验证记录

真实素材上的端到端结果，作为回归基线：

| 场景 | 结果 |
|---|---|
| UE 模板 Mannequin（UE 5.8，独立目录） | 145 资源 / 3 组；42 个 FBX 一次导出 25.8s；2/2 角色通过，高度 1.813 / 1.833 m，Y(up) |
| `Escape` 工程 `CLazyAnimpack`（UE 5.4，真实工程） | 声明 5.4 自动改用 5.4（默认会选 5.8）；范围收敛到 `/Game/CLazyAnimpack` → 300 资源；**213/213** 导出（1 网格 + 212 动画，44.9s） |
| 同上，导入编辑器内的 Tuanjie 工程 | 1/1 通过；`SkinnedMeshRenderer.bounds` = (1.4075, **1.8329**, 0.4352) m，最高轴 Y；Avatar 有效且为人形（52 骨）；212 个剪辑 0 失败；根运动经 `averageSpeed` 确认为非零 m/s |
| `CLazyAnimpack/Demo` 静态网格（UE 5.4） | 2 组 / 5 网格 / 7 贴图；**5/5 FBX + 7/7 PNG** 全部成功；目录布局 `StaticMeshes/{Materials,Meshes}/`，贴图 `StaticMeshes/_Textures/CLazyAnimpack/...` |
| 同上，Unity 批处理导入（团结引擎 2022.3.62t11，Built-in 管线） | 5/5 网格 0 问题；`1M_Cube` 恰好 1.000×1.000×1.000 m；7 张贴图全部导入（`_N` → `NormalMap`/sRGB off，基色 → sRGB on，遮罩 → sRGB off） |
| 同上，**独立脚本**交叉核对实际 Unity 资产 | 模型 `sharedMaterials` 指向 `Assets/UnityExport/StaticMeshes/Materials/*.mat`（不是默认材质、也不是子资产）；`M_UE4Man_Body1_Inst1` 的 `_MainTex`/`_BumpMap`/`_MetallicGlossMap` 分别指向导出的 PNG |
| `PolygonApocalypse` 整包（UE 5.4，1926 网格） | 2612 资源 → 10 个静态组 / 1926 网格 / 14 贴图；**1926/1926 FBX + 14/14 PNG**，0 失败，约 7 分钟；交付 126 MB；1999 个材质槽 0 缺失；13 个材质无同名冲突 |
| `PolygonApocalypse/Meshes/Buildings` 301 网格导入 Unity | 301/301 通过，0 问题；**325 处材质引用全部指向我们的材质资产**，Unity 默认材质 0 个；`SM_Bld_Apartment_01` = 9.570×14.879×10.313 m；4096² 贴图降采样到 2048² 并被如实告警 |
| 扫描开销对照实验 | 301 个建筑网格：`-nullrhi` 131.4s vs 普通 RHI 119.4s；再加 `r.GenerateMeshDistanceFields 0` 在 102 个道具上 21.8s vs 19.3s。差异都在 10% 左右，说明开销来自加载构建本身 |

前两项中的骨骼部分是早期版本用 §8.4 的 MCP 方式在**用户已打开的编辑器**里验证的，
不是批处理——两种方式结论一致。静态网格的独立核对同样绕开了我们自己写的报告：
用一个一次性 Editor 脚本（跑完即删）直接读 `ModelImporter`、`TextureImporter`、
`Renderer.sharedMaterials` 与 `Material.GetTexture()`，再把结果与导入报告对照。

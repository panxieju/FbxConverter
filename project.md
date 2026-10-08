# FbxConverter 项目文档

> 面向开发者与维护者。安装、使用与验收方式见 [README.md](README.md)。

---

## 1. 项目定位

把 Unreal Engine 工程里的**骨骼网格**与**动画序列**批量转换成 Unity 可直接使用的
FBX 包，并把 Unity 侧的导入配置与校验一并自动化。

### 目标

| # | 目标 | 落地方式 |
|---|---|---|
| G1 | 类型判断必须权威 | 只做文件发现，类型/依赖一律由 Unreal AssetRegistry + 实际加载确认 |
| G2 | 按骨架组织 | 以 Skeleton 为键分组网格与动画，逐组选择与导出 |
| G3 | 路径可追溯 | 输出保留相对于 `Content` 的资源路径 |
| G4 | 源目录只读 | 转换产物写入独立工作目录；跨骨架不自动重定向 |
| G5 | 无工程也能用 | 找不到 `.uproject` 时生成独立转换项目，零拷贝挂载源目录 |
| G6 | 失败可定位 | 逐项结果 + 具体缺失路径 + 可重试 |

### 非目标（第一版明确不做）

- `.pak` / `.utoc` / `.ucas` 打包产物（会识别并跳过，且在警告中列出）。
- 完整 Unreal 材质图还原；仅保留材质槽。
- Physics Asset 与动画蓝图还原。
- 跨 Skeleton 自动重定向。
- 导出时改写 Unreal 资源（见 §6.5）。

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
| `models.py` | 领域模型；全部可 JSON 序列化 | `AssetKind` `AssetRecord` `SkeletonGroup` `ConversionPlan` `ExportOutcome` |
| `discover.py` | 纯文件系统发现：`.uasset`、伴随文件、`.uproject`、打包产物 | `SourceFile` `DiscoveryResult` |
| `resources.py` | 定位随包数据（源码树 / PyInstaller 冻结） | `package_root()` `ue_scripts_dir()` `templates_dir()` |
| `config.py` | 用户配置持久化（`%APPDATA%/FbxConverter/config.json`） | `AppConfig` |
| `logutil.py` | 统一日志 + 回调 Handler（喂给 GUI 日志面板） | `configure_logging` `CallbackHandler` |
| `report.py` | 导出报告 | `ExportReport` |
| `unreal/locator.py` | 注册表 / Launcher / 目录扫描定位引擎，并**校验可执行文件存在** | `UnrealInstall` |
| `unreal/project.py` | 独立转换项目：生成 `.uproject` + 挂载 `Content` | `ConversionProject` `MountMode` |
| `unreal/paths.py` | 文件系统 ↔ Unreal 包路径换算 | `to_package_path` `package_to_object` … |
| `unreal/runner.py` | 无头启动编辑器、传递 job、回收 result、流式事件、取消 | `UnrealRunner` `UnrealScriptResult` |
| `pipeline/scan.py` | 发现 → Unreal 确认 → 按骨架分组 | `ScanSession` `group_assets` |
| `pipeline/export.py` | 计划 → 单次批量导出 → 写产物 | `ExportSession` `build_plan` |
| `unity/manifest.py` | 生成 `manifest.json` / `profile.json`，投放 Editor 脚本 | `build_profile_payload` `install_unity_support` |
| `unity/runner.py` | 定位并批处理驱动 Unity/团结引擎 | `UnityInstall` `run_batch_import` |
| `ue_scripts/` | **在 Unreal 内执行**的脚本（详见 §5） | `fbxconv_common/scan/export.py` |
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
ConversionPlan
  │ 单次 UnrealRunner.run_script("fbxconv_export.py", {exports[]})
  │      等待注册表就绪 → 逐个 AssetExportTask → 逐项回传
  ▼
ExportOutcome[]  ──▶  profile.json × N  ──▶  manifest.json
                 ──▶  export_report.json
                 ──▶  Editor/ 脚本投放
```

### 4.3 Unity 导入

```
Unity/Tuanjie.exe -batchmode -executeMethod ...ImportFromCommandLine
  │ 环境变量 FBXCONV_UNITY_EXPORT 或 fbxs 回退到 Assets/UnityExport
  ▼
逐角色：配置 ModelImporter → 生成/复用 Avatar → 配置每个 clip → 校验
  ▼
Reports/unity_import_report.json
```

### 4.4 重试语义

`ExportSession.run(requests, previous=...)` 把本次结果与 `previous` 按
`output_rel` 合并，因此“重试失败项”只重启一次 Unreal，且报告仍覆盖全量。

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
{ "mount_point": "/Game", "deep_resolve": true, "max_assets": 200000 }
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
                "loop_mode": "auto", "root_motion": "keep", "scale": 1.0 },
  "exports": [{
    "kind": "skeletal_mesh",              // skeletal_mesh | anim_sequence
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

### 5.5 环境变量总表

| 变量 | 使用方 | 作用 |
|---|---|---|
| `FBXCONV_UNREAL_EDITOR` | `unreal/locator` | 直接指定 `UnrealEditor-Cmd.exe` 或引擎根目录 |
| `FBXCONV_UNITY_EDITOR` | `unity/runner` | 直接指定 `Unity.exe` / `Tuanjie.exe` |
| `FBXCONV_UNITY_EXPORT` | C# 导入器 | 指定 `UnityExport` 目录 |
| `FBXCONV_FORCE_CLI` | `packaging/entry.py` | 让窗口版 exe 走 CLI（调试用） |
| `FBXCONV_BUILD_MODE` | `FbxConverter.spec` | `onedir`（默认）/ `onefile` |
| `FBXCONV_JOB` / `FBXCONV_RESULT` / `FBXCONV_RUN_DIR` | `ue_scripts` | 见 §5.1 |

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
      fbxconv_common.py  fbxconv_scan.py  fbxconv_export.py
    ui/{app,main_window,pages,widgets,worker,__init__}.py
  tests/                       87 个单元测试
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
| 采样率不写回 Unreal | UE 侧帧率仍为原值 | 在显式勾选后于副本上改写 |
| 材质仅保留槽位 | 需在 Unity 手工重连材质 | 生成材质映射 CSV，供 Unity 侧批量重绑 |
| 无跨骨架重定向 | 不同骨架需分别导出 | 接入 IK Retargeter |
| `Rigs/Poses/*_anim` 会被当作普通动画列出 | 选择列表偏长 | 依据 ControlRig 归属自动分组或默认折叠 |
| `.pak/.utoc/.ucas` 不支持 | 打包资源需先解包 | 集成 UnrealPak 解包前置步骤 |

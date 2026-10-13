# FbxConverter — Unreal → Unity FBX 转换流水线

把 Unreal Engine 的**骨骼网格**与**动画序列**批量导出为 Unity 可直接使用的 FBX 包，
并由 Unity Editor 脚本自动完成导入设置与校验。

- 不靠文件名猜类型 —— 先做文件发现，再由 Unreal 的资源注册表确认**类型与依赖**。
- 按 **Skeleton 分组**，自动统计每个骨架组的网格、动画数量与缺失依赖。
- 找不到 `.uproject` 时自动创建**独立转换项目**，保留相对于 `Content` 的资源路径。
- 一次 Unreal 启动完成**整批导出**，逐项报告成功/失败原因，失败项可**单独重试**。
- 输出 `manifest.json` / `profile.json` / `export_report.json`，供 Unity 侧直接消费。

---

## 1. 环境要求

| 组件 | 要求 | 本机已验证 |
|---|---|---|
| Python | 3.10+（开发用 3.13.0） | `G:\Program Files\Python313` |
| PyQt5 | 5.15+（仅图形界面需要） | 5.15.11 |
| Unreal Engine | 5.x，需带 `PythonScriptPlugin` | UE 5.4 (`H:\UE_5.4`)、UE 5.8 (`H:\UE_5.8`) |
| Unity / 团结引擎 | 2022.3 LTS 或更新 | 团结引擎 1.9.3 (`2022.3.62t11`) |

> 团结引擎是 Unity 中国版，编辑器可执行文件名为 `Tuanjie.exe`；程序会自动识别两者。

### 安装

```powershell
cd G:\Project\FbxConverter
& "G:\Program Files\Python313\python.exe" -m pip install -e ".[gui,dev]"
```

只做无界面转换可以不装 PyQt5：

```powershell
pip install -e .
```

---

## 2. 快速开始

### 图形界面

```powershell
python run.py gui
# 或
python -m fbxconv.ui.app
```

五步流程与规格一致：

1. **选择目录** —— Unreal Editor、Unreal 资源目录、Unity 输出目录、挂载方式
2. **扫描资源** —— 进度、资源数量、转换组、静态网格数、贴图数、缺失依赖
3. **选择转换内容** —— 骨骼组：勾选组、指定参考网格、逐个取消不需要的动作；
   静态网格组：按目录成组，逐个勾选要导出的网格
4. **设置导出** —— Humanoid／Generic、采样率、循环、Root Motion、缩放、是否导出贴图
5. **开始转换** —— 总进度、逐项状态、日志、取消、重试失败项、打开输出目录

### 命令行

```powershell
# 列出可用的引擎/编辑器
python -m fbxconv unreal-list
python -m fbxconv unity-list

# 只扫描，输出 JSON
python -m fbxconv scan H:\Packs\MyPack\Content --out G:\Out --json

# 扫描 + 导出全部骨架组
python -m fbxconv convert H:\Packs\MyPack\Content --out G:\Out --all

# 只导出某一个骨架组，按 Humanoid 导入
python -m fbxconv convert H:\Proj\Content --out G:\Out --group Mannequin_UE4 --import-type Humanoid

# 只导出静态网格（连同材质与贴图）
python -m fbxconv convert H:\Proj\Content\EnvPack --out G:\Out --all --mode static

# 只要网格，不要贴图
python -m fbxconv convert H:\Proj\Content\EnvPack --out G:\Out --all --mode static --no-textures

# 导出后直接在 Unity/团结引擎 中批处理导入 + 校验
python -m fbxconv convert H:\Proj\Content --out G:\Out --all `
  --unity-import --project G:\MyUnityProject

# 只跑 Unity 侧导入（对已有导出结果）
python -m fbxconv unity-import G:\Out\UnityExport --project G:\MyUnityProject

# 打成 .unitypackage（先导入再打包，含 Editor 导入脚本）
python -m fbxconv unity-package G:\Out\UnityExport `
  --project G:\MyUnityProject `
  --unity-package G:\MyUnityProject\Artifacts\MyPack.unitypackage

# 导出后一步到位：转换 → 导入 → 打包
python -m fbxconv convert H:\Proj\Content --out G:\Out --all `
  --project G:\MyUnityProject --unity-package G:\Out\MyPack.unitypackage
```

> `--out` 请用**绝对路径**：Unreal 进程的工作目录与当前终端不同，
> 相对路径会被解析到引擎目录下。

### `.unitypackage` 说明

图形界面同样支持：第 1 步填 **Unity 工程目录**，第 5 步转换完成后点
**「导出 Unity 包（.unitypackage）」**，选个保存位置即可（旁边的复选框控制包内
是否含 Editor 脚本）。

命令行 `unity-package` 会先在目标工程里**导入**一次，再用
`AssetDatabase.ExportPackage` 写出包。这一步必须在编辑器内做，也正是它的价值：

- 包里带的是**已经配好的导入设置**——Avatar、每个动画的循环标记、贴图类型与
  sRGB、材质与贴图的绑定关系。换到别的工程 Import Package 之后不需要重配。
- 用 `--no-editor-scripts` 可以让包内不含 `Editor/` 脚本，避免目标工程
  已有同名 asmdef 时报 `CS0101`。
- 用 `--no-import` 跳过导入步骤，直接把工程里已导入的内容打包（更快，
  但设置以工程当前状态为准）。
- **包必须写在目标工程的 `Assets/` 之外**，否则 Unity 会把它当成一个资源导入。

实测（Rapier_AnimSet，117 FBX + 4 贴图 + Editor 脚本）：73.7 MB 的导出目录
打成 **11.0 MB** 的 `.unitypackage`，导入一个全新工程后核对：113 个剪辑、
Avatar 有效、角色高 1.8329 m、18 个循环标记全部保留。

---

## 3. 工作原理

```
                 ┌──────────────────────────────────────────┐
  资源目录  ───▶ │ 1. 文件发现（纯 Python，只列 .uasset）    │
                 │    附带 .uexp/.ubulk/.uptnl 伴随文件      │
                 └──────────────────┬───────────────────────┘
                                    │ 找不到 .uproject？
                                    ▼
                 ┌──────────────────────────────────────────┐
                 │ 2. 独立转换项目（Content → 原目录联接）   │
                 │    目录联接零拷贝；失败则回退完整复制      │
                 └──────────────────┬───────────────────────┘
                                    ▼
                 ┌──────────────────────────────────────────┐
                 │ 3. Unreal 确认（headless）                │
                 │    AssetRegistry 判定类型与依赖           │
                 │    加载网格读真实 Skeleton / 材质槽        │
                 │    再由材质依赖图解析出所用贴图            │
                 └──────────────────┬───────────────────────┘
                                    ▼
                 ┌──────────────────────────────────────────┐
                 │ 4. 分组 → 用户选择                        │
                 │    骨骼：按 Skeleton；静态：按 Content 目录│
                 └──────────────────┬───────────────────────┘
                                    ▼
                 ┌──────────────────────────────────────────┐
                 │ 5. Unreal 批量导出                        │
                 │    SkeletalMeshExporterFBX                │
                 │    AnimSequenceExporterFBX                │
                 │    StaticMeshExporterFBX                  │
                 │    TextureExporterPNG（另一次启动，见下）  │
                 └──────────────────┬───────────────────────┘
                                    ▼
                 ┌──────────────────────────────────────────┐
                 │ 6. 写 manifest/profile/report + C# 脚本   │
                 └──────────────────┬───────────────────────┘
                                    ▼
                 ┌──────────────────────────────────────────┐
                 │ 7. Unity 批处理导入 + 校验                │
                 │    贴图导入设置 + 材质资产 + 回填到模型    │
                 └──────────────────────────────────────────┘
```

### 与 Unreal 的通信方式

`UnrealEditor-Cmd.exe <project> -ExecutePythonScript=<script> -unattended ...`。
已实测确认：脚本的 `sys.argv` **只包含脚本路径**，因此配置通过
`FBXCONV_JOB`（输入 JSON）与 `FBXCONV_RESULT`（输出 JSON）两个环境变量传递；
脚本执行完毕后编辑器会自行退出。运行中的进度通过 stdout 上的
`FBXCONV:{...}` 行实时回传。

### 关键实现细节

- **Skeleton 解析**：`AnimSequence` 没有 `skeleton` 属性，只能通过
  `get_editor_property("skeleton")` 或 `get_skeleton()` 读取；而 AssetRegistry 的
  `Skeleton` 标签可能返回 `/Script/Engine` 这类无效值。因此对网格与动画一律
  以**加载后的对象**为准。
- **独立转换项目必须传给导出阶段**：`ConversionPlan.unreal_project` 记录扫描时
  真正使用的 `.uproject`。漏传会让 Unreal 以“无项目”状态启动（挂载点消失、
  资源注册表为空），表现为“资源明明存在却加载失败”。
- **导出阶段不能用 `-nullrhi`**：`USkeletalMeshExporterFBX` 会经
  MeshMergeUtilities 构造蒙皮组件，在无 RHI 下触发断言并让编辑器崩溃。
  扫描阶段无此依赖，仍可使用 `-nullrhi` 加速。
- **注册表就绪**：编辑器启动后约 6 秒 AssetRegistry 才完成首次收集，而
  `-ExecutePythonScript` 正好落在这个窗口内。导出前会显式等待注册表就绪。

### 静态网格的贴图是怎么“打包”进去的

**先说结论：Unreal 的 FBX 导出器无法把贴图写进 FBX，这是引擎源码里写死的。**
（`FbxMainExport.cpp:303` 的 `bool bEmbedMedia = false;`，以及
`IOS_REF.SetBoolProp(EXP_FBX_EMBEDDED, bEmbedMedia)`；`FbxExportOption`
也没有任何相关开关——实测逐项尝试 `export_textures` / `embed_textures` /
`export_materials` 全被拒绝。）它写进 FBX 的只是**原作者机器上的绝对路径**，
例如 `U:/Dropbox/SyntyStudios/.../Texture_01_A.png`，在本机并不存在。

所以本工具的做法是：

1. Unreal 侧导出静态网格 FBX（材质名会被写进 FBX 的材质节点）；
2. 从**材质依赖图**解析出该网格用到的贴图，用 `TextureExporterPNG` 导出成 PNG，
   放到 `StaticMeshes/_Textures/<Content 相对路径>/`；
3. `manifest.json` 记录「网格 → 材质槽 → 贴图」的完整映射；
4. Unity 侧的 `UnrealResourceImporter.cs` 按映射设置贴图导入参数
   （法线图设 `NormalMap` 且关闭 sRGB）、创建共享 Material 资产、
   并用 `SearchAndRemapMaterials` 把模型指到这些材质上。

结果是：**把 `UnityExport` 放进 Unity，模型就是带贴图的**，不需要手工拖拽。

> 贴图导出单独占用一次编辑器启动。原因是 `UTextureExporterPNG` 会对源格式执行
> 原生 `check()`，失败时**直接杀死编辑器进程**而不是抛异常；分开跑可以保证
> 贴图崩溃不会带走已经完成的数千个 FBX。脚本每导出一张就落盘一次结果，
> 因此崩溃后仍能知道进度并只重试剩下的部分。

---

## 4. 输出结构

```
UnityExport/
  Characters/
    Mannequin_UE4/
      Mesh/
        SK_Mannequin.fbx
      Animations/
        Jog_Fwd.fbx
      profile.json            # 该角色的 Unity 导入设置
    Mannequins/
      Mesh/
        SKM_Manny.fbx
      Animations/
        MM_Idle.fbx
        ...
      profile.json
  Editor/
    FbxConverter.Editor.asmdef
    UnrealResourceImporter.cs # 导入设置 + 校验
    README.md
    Tests/                    # 仅在项目已安装 Test Framework 时生成
      FbxConverter.Tests.asmdef
      UnrealImportTests.cs
  Reports/
    export_report.json        # 逐项成功/失败与原因
    unity_import_report.json  # Unity 侧校验结果（导入后生成）
  manifest.json
```

- 角色 FBX 含网格、蒙皮与骨骼；**每个动作一份**动画 FBX。
- 所有 FBX 路径均相对于 `UnityExport` 根目录。
- `profile.json` / `manifest.json` 的字段刻意限制为 Unity `JsonUtility`
  可解析的形状（无字典、数值字段无 `null`），因此 Unity 侧**不需要任何第三方
  JSON 库**。

### 在 Unity 中启用

把整个 `UnityExport` 目录放进项目的 `Assets/` 即可。脚本会自动出现在
`Tools > FbxConverter > Import Unreal Export...`。

批处理：

```powershell
& "G:\Program Files\UnityHub\2022.3.62t11\Editor\Tuanjie.exe" `
  -batchmode -quit -nographics `
  -projectPath G:\MyUnityProject `
  -executeMethod FbxConverter.Editor.UnrealResourceImporter.ImportFromCommandLine `
  -logFile G:\MyUnityProject\Logs\import.log
# 需先设置环境变量 FBXCONV_UNITY_EXPORT=<UnityExport 绝对路径>
```

EditMode 校验测试（需要 `com.unity.test-framework`）：

```
Window > General > Test Runner > EditMode > FbxConverter.Tests
```

---

## 5. 验收顺序与验证方式

| 阶段 | 验收要求 | 状态 | 验证方式 |
|---|---|---|---|
| 目录扫描 | 从目录正确发现网格、Skeleton 和动画并建立关联 | ✅ | `python -m fbxconv scan <dir> --json` |
| 单组转换 | UE4 角色和一个动画导出后可在 Unity 正常播放 | ✅ | `--group Mannequin_UE4` |
| 批量转换 | 同骨架动画批量导出，失败项有原因且可重试 | ✅ | `--all`，见 `export_report.json` |
| Unity 导入 | 自动配置 Humanoid／Generic，验证尺寸、朝向、Avatar 和根骨运动 | ✅ | `Reports/unity_import_report.json` |
| UE5 支持 | Manny／Quinn 独立预设通过实际网格及动画验证 | ✅ | `--group Mannequins` |
| 独立目录支持 | 没有 `.uproject` 时保留资源路径并在转换项目中成功加载 | ✅ | 扫描输出中的“转换项目” |
| 静态网格输出 | 指定资产目录，遍历并导出其中的网格 FBX | ✅ | `--mode static --all` |
| 材质与贴图随包 | 网格在 Unity 中直接带材质与贴图，不需要手工拖拽 | ✅ | `Reports/unity_import_report.json` + 独立脚本核对 |

### 实测结果

**A. UE 模板 Mannequin 内容（UE 5.8 + 团结引擎 1.9.3）**

```
扫描：145 个 .uasset → 145 个确认；3 组（5 网格 / 40 动画）；缺失依赖 0
导出：42/42 成功，单次 Unreal 启动，25.8 秒
Unity：2/2 角色通过
       Mannequins    : Avatar 有效，高度 1.813 m，主轴 Y(up)，39 个动画，0 失败
       Mannequin_UE4 : Avatar 有效，高度 1.833 m，主轴 Y(up)， 1 个动画，0 失败
```

**B. 第三方动作包 `CLazyAnimpack`（UE 5.4 + 团结引擎 1.9.3）**

真实工程 `G:\UnrealEngineProjects\Escape`，工程声明 `EngineAssociation: "5.4"`。

```
引擎：项目声明 5.4 → 自动改用 UE 5.4.4（默认会选到 5.8）
范围：挂载点 /Game（依赖可解析），仅扫描 /Game/CLazyAnimpack → 确认 300 个资源
导出：213/213 成功（1 网格 + 212 动画），单次启动 70.3 秒
Unity：1/1 角色通过
       高度         : 1.8329 m      ← UE4 Mannequin 标准身高，单位正确
       主轴         : Y(up)          ← 宽 1.4075 / 深 0.4352，标准 T-Pose
       Avatar       : 有效且为人形（Humanoid）
       动画         : 212 个全部导入，0 失败，共 27,560 条曲线
       根运动       : 210 个动画有根位移，其中 210 个有水平位移
```

### 两点行为说明

- **扫描范围跟随你选的目录。** 选择 `Content/CLazyAnimpack` 时，挂载点仍是 `Content`
  （这样 `/Game/...` 依赖才能解析），但只扫描并列出该子目录，不会把整个工程拖进来。
- **引擎版本跟随工程声明。** `.uproject` 里的 `EngineAssociation` 优先于“本机最新版本”，
  避免 5.4 工程被 5.8 编辑器打开而触发资源升级；未安装该版本时会告警并回退。

### C. 静态网格 + 材质 + 贴图（UE 5.4 + 团结引擎 1.9.3）

**完整素材包 `PolygonApocalypse`（1926 个网格）**

```
扫描：2612 个资源 → 10 个静态网格组 / 1926 个网格 / 14 张贴图，用时 371 秒
导出：1926/1926 FBX + 14/14 PNG 全部成功，0 失败，约 7 分钟
交付：126 MB（FBX 114.2 MB + PNG 9.8 MB），manifest.json 704 KB
布局：StaticMeshes/Meshes/{Buildings,Props,Weapons,Vehicles,Items,Environments,...}
      StaticMeshes/_Textures/PolygonApocalypse/...
槽位：1999 个材质槽，0 个网格缺槽；13 个材质，无同名冲突
```

**子集 `Meshes/Buildings`（301 个网格）在 Unity 中的独立核对**

用一次性 Editor 脚本（跑完即删）直接读 Unity 资产，不依赖我们自己写的报告：

```
模型        : 301 个，共 325 处材质引用
             325/325 指向 StaticMeshes/Materials/*.mat
             Unity 默认材质 0 个，子资产兜底 0 个
贴图        : 3 张全部导入，sRGB 与类型正确
材质        : 4 个 Standard 材质，_MainTex 指向导出的 PNG
尺寸        : SM_Bld_Apartment_01 9.570 x 14.879 x 10.313 m（厘米→米换算正确）
降采样告警  : T_PolygonApocalypse_01_A 源 4096² → Unity 导入 2048²
             （Unity 默认 maxTextureSize=2048；导入报告里会明确列出，见 §8）
```

**已知代价**：静态网格的扫描明显慢于骨骼内容——UE 在 `load_asset` 时就要**构建**网格
（实测约 0.4 秒/个复杂建筑网格、0.2 秒/个简单道具）。
实测 `-nullrhi` 与 `r.GenerateMeshDistanceFields 0` 各只影响约 9% / 12%，
所以这是加载本身的开销，不是可以关掉的开关。

---

## 6. 测试

```powershell
python -m pytest tests -q
```

单元测试覆盖：目录解析与 `.uproject` 查找、伴随文件识别、包路径换算、
资源分类、骨架分组与命名去重、静态网格按目录分组、贴图计划与共享贴图池、
`manifest.json` 三张表（贴图/材质/网格）、PNG 尺寸探测、FBX 与贴图重试拆分、
挂载点推断、循环推断、`profile.json`/`manifest.json`/`export_report.json` 生成、配置持久化。

---

## 7. 打包发布

```powershell
pyinstaller --clean --noconfirm FbxConverter.spec
```

产出 `dist/FbxConverter/`，约 **97 MB**，含两个可执行文件：

| 文件 | 说明 |
|---|---|
| `FbxConverter.exe` | 窗口版（GUI 向导） |
| `fbxconv.exe` | 控制台版（CLI） |

两者共用同一份冻结负载，靠**自身文件名**决定走 GUI 还是 CLI。
`ue_scripts/*.py` 与 Unity 模板作为**数据**打包在
`_internal/fbxconv/` 下——Unreal 会直接读取这些 `.py` 源文件，
所以它们不能被冻结成模块。

需要单文件时：

```powershell
$env:FBXCONV_BUILD_MODE = "onefile"
pyinstaller --clean --noconfirm FbxConverter.spec
```

> `FbxConverter.exe` 是窗口子系统程序，PowerShell 的 `&` **不会等待**它结束。
> 自动化脚本里请用 `fbxconv.exe`，或 `Start-Process -Wait`。

完整的架构、接口契约与设计取舍见 [project.md](project.md)。

---

## 8. 第一版范围与限制

- **仅支持普通编辑器资源**；打包后的 `.pak` / `.utoc` / `.ucas` 会被识别并跳过，
  并在扫描警告中列出。
- **骨骼网格保留材质槽，但不还原完整 Unreal 材质图**；Physics Asset 与动画蓝图不承诺自动还原。
- **静态网格还原三类贴图槽**（基色 / 法线 / 遮罩）。判定依据是 Unreal 能给出的权威信号
  （法线看 `TC_NORMALMAP` 与命名、线性数据看 sRGB 关闭），**不是**从材质图读取的真实连接——
  因为 `MaterialEditingLibrary.get_used_textures()` 实测返回空、而 `Material.expressions`
  在 Python 侧被保护。复杂材质图（叠加、遮罩运算、顶点色混合）不还原。
- **贴图不会嵌入 FBX**（引擎源码写死，见 §3 末尾），而是作为 PNG 与 FBX 并排输出，
  由 Unity 侧脚本自动装配。交付物是「FBX + PNG + manifest.json」，不是一个自包含 FBX。
- **大于 2048 的贴图会被 Unity 默认上限降采样**（例如 4096² → 2048²）。
  导入报告会明确列出每一张被降采样的贴图。本工具不改 Unity 的默认值，
  因为那会让显存占用翻两番——需要原始尺寸请在 TextureImporter 上调高 `maxTextureSize`。
- **静态网格的扫描比骨骼内容慢**：UE 在加载时就要构建网格，实测约 0.2–0.4 秒/个。
  1926 个网格的包扫描约 6 分钟（导出本身约 5 分钟）。
- **不同 Skeleton 分别导出**，不做跨骨架自动重定向。
- **采样率、循环、Root Motion 在 Unity 侧生效**（写入 `profile.json` 由 C# 导入器应用），
  刻意不在导出时修改 Unreal 资源——独立转换项目下那些资源可能是用户的原文件。
- Unreal 与 Unity 的启动开销是主要耗时。扫描、FBX 导出、贴图导出各需一次引擎启动，
  因此 GUI 的“重试失败项”只会重新启动必要的那几次。

## 9. 排错

| 现象 | 原因与处理 |
|---|---|
| `未找到可用的 Unreal Engine 安装` | 设置 `FBXCONV_UNREAL_EDITOR=<...>\UnrealEditor-Cmd.exe` |
| 扫描报“缺失依赖” | 该组引用了所选目录之外、且无法解析的资源，按提示补齐或扩大资源目录 |
| 扫描报“疑似挂载点不匹配” | 资源包期望位于 `Content/<包名>/` 之下；改选包含该包的上一级目录 |
| 只选了一个子目录，却扫了整个工程 | 挂载点会取其上溯到的 `Content`，但**扫描范围**仍是你选的目录；若列表里出现别的包，说明选的就是 `Content` 本身 |
| 扫描后整组显示“缺依赖”，并提示“疑似挂载点不匹配” | 该资源包期望自己位于 `Content/<包名>/` 之下，而你把**包目录本身**当成了资源根。**改选它的上一级目录**即可（例如把 `…\Rapier Anim Set\Rapier_AnimSet` 换成 `…\Rapier Anim Set`） |
| 点“扫描资源”没有任何反应 | 0.1.0 之后不会再有静默无反应：会弹出具体原因（缺引擎 / 缺资源目录 / 目录不存在）。若仍无反应请贴日志面板内容 |
| 工程是 5.4 却用 5.8 打开 | 现在会自动读取 `.uproject` 的 `EngineAssociation`；若本机没装该版本会告警并回退 |
| 导出全部失败：`无法加载资源` | 确认资源目录与 `.uproject` 是否匹配；转换项目应在 `<输出目录>/.fbxconv/UnrealProject` |
| Unreal 崩溃于 `SkinnedMeshComponent` 断言 | 导出阶段被加了 `-nullrhi`；移除该参数 |
| 贴图批次中途结束，日志无异常 | `TextureExporterPNG` 对源格式的原生 `check()` 失败会直接杀进程。已完成的贴图都在磁盘上，用“重试失败项”续跑即可 |
| 某张贴图报“虚拟贴图不支持 PNG 导出” | 该贴图是 Virtual Texture；需要先转成普通 Texture2D |
| 某些网格仍是 Unity 默认材质 | 该网格在 Unreal 里没有材质槽，或 FBX 里没有可绑定的材质节点；报告里会逐条列出 |
| 模型有贴图但看起来偏软 | 贴图被 `maxTextureSize` 降采样了；报告里有具体尺寸，调高该值后重新导入 |
| Unity 报 `CS0101` 重复定义 | 同一份 C# 被放进了两个目录；只保留 `Assets/UnityExport/Editor/` 一处 |
| Unity 报找不到 `NUnit` | 项目未安装 Test Framework；删除 `Editor/Tests/` 即可（导入器本身不受影响） |
| 报告里根运动全为 0 | 早期版本只识别 Generic 的 `m_LocalPosition`；Humanoid 走 `RootT.*` 肌肉曲线，现已同时支持 |

调试时可加 `-v` 查看 DEBUG 日志，或在 GUI 第 5 步查看完整日志面板。

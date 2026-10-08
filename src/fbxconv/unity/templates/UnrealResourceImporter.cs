// UnrealResourceImporter.cs
//
// Applies Unreal -> Unity FBX import settings from the manifest that
// FbxConverter writes, then validates the result (size, orientation, Avatar
// and root motion) as required by stage 4 of the specification.
//
// Targets Unity 2022.3 LTS and later (verified against Tuanjie 1.9.3 /
// 2022.3.62t11). Only UnityEngine + UnityEditor APIs are used -- no third-party
// JSON library, because Newtonsoft is not loaded in the editor by default.

using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Text;
using UnityEditor;
using UnityEngine;

namespace FbxConverter.Editor
{
    // -----------------------------------------------------------------------
    // manifest.json
    // -----------------------------------------------------------------------

    [Serializable]
    public class ManifestSource
    {
        public string contentRoot;
        public string projectFile;
        public string conversionProject;
        public string engineVersion;
        public string mountPoint;
    }

    [Serializable]
    public class ManifestSettings
    {
        public string importType;
        public int samplingRate;
        public string loopMode;
        public string rootMotion;
        public float scale;
        public bool exportMaterials;
    }

    [Serializable]
    public class ManifestCharacter
    {
        public string name;
        public string displayName;
        public string skeletonPackage;
        public string profilePath;
        public string meshPath;
        public string meshName;
        public int animationCount;
        public string[] animationPaths;
        public string[] animationNames;
        public string[] missingDependencies;
    }

    [Serializable]
    public class Manifest
    {
        public int schemaVersion;
        public string generator;
        public string generatedAt;
        public string pathsRelativeTo;
        public ManifestSource source;
        public ManifestSettings settings;
        public string importerScript;
        public string reportPath;
        public int characterCount;
        public int animationCount;
        public ManifestCharacter[] characters;
    }

    // -----------------------------------------------------------------------
    // profile.json
    // -----------------------------------------------------------------------

    [Serializable]
    public class ProfileMesh
    {
        public string fbx;
        public string name;
        public string unrealPackage;
        public string avatarName;
        public string skeletonPackage;
        public int triangleHint;
        public int materialCount;
    }

    [Serializable]
    public class ProfileMaterial
    {
        public string unrealMaterial;
        public string name;
        public string note;
    }

    [Serializable]
    public class ProfileAnimation
    {
        public string name;
        public string fbx;
        public string unrealPackage;
        public bool loop;
        public bool rootMotion;
        public float lengthSeconds;
        public int frames;
        public float frameRate;
    }

    [Serializable]
    public class Profile
    {
        public int schemaVersion;
        public string generator;
        public string pathsRelativeTo;
        public string group;
        public string displayName;
        public string skeletonPackage;
        public string referenceMesh;
        public string importType;
        public float scale;
        public int samplingRate;
        public string motionNode;
        public ProfileMesh mesh;
        public ProfileAnimation[] animations;
        public ProfileMaterial[] materials;
        public string[] missingDependencies;
        public string[] notes;
    }

    // -----------------------------------------------------------------------
    // Validation report
    // -----------------------------------------------------------------------

    public class ValidationEntry
    {
        public string character;
        public string meshAsset;
        public string status = "ok";
        public string importType;
        public bool avatarValid;
        public bool avatarHuman;
        public string avatarName;
        public float heightMetres;
        public float widthMetres;
        public float depthMetres;
        public string dominantAxis;
        public int animationCount;
        public int animationsWithRootMotion;
        public int animationFailures;
        public int clipsWithPositionCurves;
        public int totalCurveBindings;
        public readonly List<string> sampleCurves = new List<string>();
        public readonly List<string> rootMotionPaths = new List<string>();
        public readonly List<string> problems = new List<string>();
        public readonly List<string> notices = new List<string>();
    }

    // -----------------------------------------------------------------------
    // Importer
    // -----------------------------------------------------------------------

    public static class UnrealResourceImporter
    {
        public const string DefaultExportFolderName = "UnityExport";
        public const string ExportRootEnvVar = "FBXCONV_UNITY_EXPORT";
        public const string ExportRootArg = "-fbxconvExport";

        //: A humanoid should measure roughly this tall, in metres.
        private const float MinHumanoidHeight = 0.3f;
        private const float MaxHumanoidHeight = 6.0f;

        // -------------------------------------------------------------------
        // Entry points
        // -------------------------------------------------------------------

        [MenuItem("Tools/FbxConverter/Import Unreal Export...")]
        public static void ImportFromDialog()
        {
            string start = Path.Combine(Application.dataPath, DefaultExportFolderName);
            if (!Directory.Exists(start))
            {
                start = Application.dataPath;
            }
            string chosen = EditorUtility.OpenFolderPanel("Select UnityExport folder", start, string.Empty);
            if (string.IsNullOrEmpty(chosen))
            {
                return;
            }
            Import(chosen);
        }

        [MenuItem("Tools/FbxConverter/Re-import Last Export")]
        public static void ReimportLastExport()
        {
            string root = ResolveExportRoot();
            if (string.IsNullOrEmpty(root))
            {
                EditorUtility.DisplayDialog(
                    "FbxConverter",
                    "找不到 UnityExport 目录。请先执行一次“Import Unreal Export...”。",
                    "OK");
                return;
            }
            Import(root);
        }

        /// <summary>Batch mode entry point (see -executeMethod).</summary>
        public static void ImportFromCommandLine()
        {
            string root = ResolveExportRoot();
            if (string.IsNullOrEmpty(root))
            {
                Fail("未提供导出目录。请设置环境变量 " + ExportRootEnvVar +
                     " 或传入 " + ExportRootArg + " <path>。");
                return;
            }
            try
            {
                Import(root);
            }
            catch (Exception exception)
            {
                Debug.LogError("[FbxConverter] 导入失败: " + exception);
                EditorApplication.Exit(1);
            }
        }

        private static void Fail(string message)
        {
            Debug.LogError("[FbxConverter] " + message);
            if (Application.isBatchMode)
            {
                EditorApplication.Exit(1);
            }
        }

        private static string ResolveExportRoot()
        {
            string fromEnv = Environment.GetEnvironmentVariable(ExportRootEnvVar);
            if (!string.IsNullOrEmpty(fromEnv) && Directory.Exists(fromEnv))
            {
                return Path.GetFullPath(fromEnv);
            }
            string[] args = Environment.GetCommandLineArgs();
            for (int i = 0; i < args.Length - 1; i++)
            {
                if (string.Equals(args[i], ExportRootArg, StringComparison.OrdinalIgnoreCase))
                {
                    if (Directory.Exists(args[i + 1]))
                    {
                        return Path.GetFullPath(args[i + 1]);
                    }
                }
            }
            string fallback = Path.Combine(Application.dataPath, DefaultExportFolderName);
            return Directory.Exists(fallback) ? Path.GetFullPath(fallback) : null;
        }

        // -------------------------------------------------------------------
        // Core
        // -------------------------------------------------------------------

        public static void Import(string exportRoot)
        {
            exportRoot = EnsureInsideAssets(exportRoot);
            string manifestPath = Path.Combine(exportRoot, "manifest.json");
            if (!File.Exists(manifestPath))
            {
                Fail("找不到 manifest.json: " + manifestPath);
                return;
            }

            Manifest manifest = JsonUtility.FromJson<Manifest>(File.ReadAllText(manifestPath, Encoding.UTF8));
            if (manifest == null || manifest.characters == null || manifest.characters.Length == 0)
            {
                Fail("manifest.json 中没有角色条目。");
                return;
            }

            Debug.Log(string.Format(
                CultureInfo.InvariantCulture,
                "[FbxConverter] 导入 {0} 个角色 / {1} 个动画（引擎 {2}）",
                manifest.characterCount,
                manifest.animationCount,
                manifest.source != null ? manifest.source.engineVersion : "?"));

            List<ValidationEntry> results = new List<ValidationEntry>();
            foreach (ManifestCharacter character in manifest.characters)
            {
                results.Add(ImportCharacter(exportRoot, character, manifest));
            }

            AssetDatabase.SaveAssets();
            AssetDatabase.Refresh();

            string reportPath = WriteReport(exportRoot, manifest, results);
            ReportSummary(results, reportPath);
        }

        private static ValidationEntry ImportCharacter(
            string exportRoot, ManifestCharacter character, Manifest manifest)
        {
            ValidationEntry entry = new ValidationEntry
            {
                character = character.name,
                animationCount = 0,
                animationsWithRootMotion = 0,
                animationFailures = 0,
            };

            if (string.IsNullOrEmpty(character.profilePath))
            {
                entry.status = "failed";
                entry.problems.Add("manifest 未提供 profilePath。");
                return entry;
            }

            string profileFullPath = Path.Combine(exportRoot, Normalise(character.profilePath));
            if (!File.Exists(profileFullPath))
            {
                entry.status = "failed";
                entry.problems.Add("找不到 profile.json: " + profileFullPath);
                return entry;
            }

            Profile profile = JsonUtility.FromJson<Profile>(File.ReadAllText(profileFullPath, Encoding.UTF8));
            if (profile == null)
            {
                entry.status = "failed";
                entry.problems.Add("profile.json 解析失败。");
                return entry;
            }

            entry.importType = profile.importType;

            // --- mesh -------------------------------------------------------
            string meshAssetPath = ToAssetPath(exportRoot, profile.mesh != null ? profile.mesh.fbx : null);
            if (string.IsNullOrEmpty(meshAssetPath))
            {
                entry.status = "failed";
                entry.problems.Add("角色网格 FBX 缺失或不在 Assets 下。");
                return entry;
            }
            entry.meshAsset = meshAssetPath;

            ModelImporter meshImporter = AssetImporter.GetAtPath(meshAssetPath) as ModelImporter;
            if (meshImporter == null)
            {
                entry.status = "failed";
                entry.problems.Add("无法获取 ModelImporter: " + meshAssetPath);
                return entry;
            }

            ConfigureMeshImporter(meshImporter, profile, manifest);
            AssetDatabase.ImportAsset(meshAssetPath, ImportAssetOptions.ForceUpdate);

            // --- avatar -----------------------------------------------------
            Avatar avatar = LoadAvatar(meshAssetPath);
            if (avatar == null)
            {
                entry.problems.Add("导入后没有生成 Avatar。");
            }
            else
            {
                entry.avatarValid = avatar.isValid;
                entry.avatarHuman = avatar.isHuman;
                entry.avatarName = avatar.name;
                if (!avatar.isValid)
                {
                    entry.problems.Add("Avatar 校验失败（isValid = false）。");
                }
                if (IsHumanoid(profile) && !avatar.isHuman)
                {
                    entry.problems.Add("请求 Humanoid，但 Avatar 不是人形。");
                }
            }

            // --- bounds -----------------------------------------------------
            MeasureMesh(meshAssetPath, entry);

            // --- animations -------------------------------------------------
            if (profile.animations != null)
            {
                foreach (ProfileAnimation animation in profile.animations)
                {
                    string animationAssetPath = ToAssetPath(exportRoot, animation.fbx);
                    if (string.IsNullOrEmpty(animationAssetPath))
                    {
                        entry.animationFailures++;
                        entry.problems.Add("动画 FBX 缺失: " + animation.fbx);
                        continue;
                    }

                    ModelImporter animationImporter =
                        AssetImporter.GetAtPath(animationAssetPath) as ModelImporter;
                    if (animationImporter == null)
                    {
                        entry.animationFailures++;
                        entry.problems.Add("无法获取动画 ModelImporter: " + animationAssetPath);
                        continue;
                    }

                    ConfigureAnimationImporter(animationImporter, profile, animation, avatar, manifest);
                    AssetDatabase.ImportAsset(animationAssetPath, ImportAssetOptions.ForceUpdate);

                    entry.animationCount++;
                    string rootPath;
                    int positionBindings;
                    int totalBindings;
                    bool hasRootMotion = FindRootMotion(
                        animationAssetPath, out rootPath, out positionBindings, out totalBindings);
                    entry.totalCurveBindings += totalBindings;
                    if (totalBindings == 0)
                    {
                        entry.animationFailures++;
                        entry.problems.Add(animation.name +
                            "：导入后的 AnimationClip 没有任何曲线（检查 FBX 是否包含动画数据）。");
                    }
                    if (positionBindings > 0)
                    {
                        entry.clipsWithPositionCurves++;
                    }
                    if (entry.sampleCurves.Count < 12)
                    {
                        CollectSampleCurves(animationAssetPath, entry.sampleCurves);
                    }
                    if (hasRootMotion)
                    {
                        entry.animationsWithRootMotion++;
                        if (!string.IsNullOrEmpty(rootPath) &&
                            !entry.rootMotionPaths.Contains(rootPath))
                        {
                            entry.rootMotionPaths.Add(rootPath);
                        }
                    }
                    else if (animation.rootMotion)
                    {
                        // Informational: plenty of animations are legitimately
                        // in place, so this must not mark the character as bad.
                        entry.notices.Add(animation.name +
                            "：未检测到根骨骼位移（原地动画）。");
                    }
                }
            }

            if (entry.problems.Count > 0 && entry.status == "ok")
            {
                entry.status = "warnings";
            }
            return entry;
        }

        private static bool IsHumanoid(Profile profile)
        {
            return !string.IsNullOrEmpty(profile.importType) &&
                   profile.importType.Equals("Humanoid", StringComparison.OrdinalIgnoreCase);
        }

        // -------------------------------------------------------------------
        // Importer configuration
        // -------------------------------------------------------------------

        private static void ConfigureMeshImporter(
            ModelImporter importer, Profile profile, Manifest manifest)
        {
            bool humanoid = IsHumanoid(profile);

            importer.animationType = humanoid
                ? ModelImporterAnimationType.Human
                : ModelImporterAnimationType.Generic;
            importer.avatarSetup = ModelImporterAvatarSetup.CreateFromThisModel;
            importer.importAnimation = false;
            importer.importBlendShapes = true;
            importer.importCameras = false;
            importer.importLights = false;
            importer.importVisibility = false;
            importer.resampleCurves = true;
            importer.isReadable = false;

            // Unreal exports centimetre units; let Unity apply the file scale.
            importer.useFileScale = true;
            importer.globalScale = profile.scale > 0f ? profile.scale : 1f;

            // Keep material *slots* (spec 5) without pretending to rebuild
            // Unreal's material graph.
            bool wantMaterials = manifest.settings == null || manifest.settings.exportMaterials;
            importer.materialImportMode = wantMaterials
                ? ModelImporterMaterialImportMode.ImportStandard
                : ModelImporterMaterialImportMode.None;
            importer.materialLocation = ModelImporterMaterialLocation.External;

            importer.addCollider = false;
            importer.optimizeGameObjects = false;
        }

        private static void ConfigureAnimationImporter(
            ModelImporter importer,
            Profile profile,
            ProfileAnimation animation,
            Avatar avatar,
            Manifest manifest)
        {
            bool humanoid = IsHumanoid(profile);

            importer.animationType = humanoid
                ? ModelImporterAnimationType.Human
                : ModelImporterAnimationType.Generic;
            importer.importAnimation = true;
            importer.resampleCurves = true;
            importer.importBlendShapes = false;
            importer.importCameras = false;
            importer.importLights = false;
            importer.importVisibility = false;
            importer.materialImportMode = ModelImporterMaterialImportMode.None;
            importer.addCollider = false;
            importer.useFileScale = true;
            importer.globalScale = profile.scale > 0f ? profile.scale : 1f;

            // Bind the clip to the mesh's Avatar so both share one rig.
            if (avatar != null)
            {
                importer.avatarSetup = ModelImporterAvatarSetup.CopyFromOther;
                importer.sourceAvatar = avatar;
            }

            if (!string.IsNullOrEmpty(profile.motionNode))
            {
                importer.motionNodeName = profile.motionNode;
            }

            ModelImporterClipAnimation[] clips = importer.defaultClipAnimations;
            if (clips == null || clips.Length == 0)
            {
                clips = new ModelImporterClipAnimation[1];
                clips[0] = new ModelImporterClipAnimation();
            }

            if (clips.Length == 1)
            {
                ApplyClipSettings(clips[0], animation, profile, manifest);
                clips[0].name = string.IsNullOrEmpty(animation.name)
                    ? clips[0].name
                    : animation.name;
            }
            else
            {
                // Multiple takes in one FBX: name them after the profile entry.
                for (int i = 0; i < clips.Length; i++)
                {
                    ApplyClipSettings(clips[i], animation, profile, manifest);
                }
            }

            importer.clipAnimations = clips;
        }

        private static void ApplyClipSettings(
            ModelImporterClipAnimation clip,
            ProfileAnimation animation,
            Profile profile,
            Manifest manifest)
        {
            clip.loopTime = animation.loop;
            clip.loopPose = animation.loop;

            // Deliberately NOT touching firstFrame/lastFrame. Overwriting them
            // with 0 collapses the take to a zero-length range and Unity then
            // imports a clip that contains no curves at all. The values that
            // defaultClipAnimations supplies already cover the whole take.

            // Root motion handling (spec 4.4 / 5).
            string mode = animation.rootMotion ? "keep" : "lock-full";
            if (manifest.settings != null && !string.IsNullOrEmpty(manifest.settings.rootMotion))
            {
                mode = manifest.settings.rootMotion;
            }

            switch (mode)
            {
                case "keep":
                    clip.lockRootPositionXZ = false;
                    clip.lockRootHeightY = false;
                    clip.lockRootRotation = false;
                    clip.keepOriginalPositionY = true;
                    break;
                case "lock-xz":
                    clip.lockRootPositionXZ = true;
                    clip.lockRootHeightY = false;
                    clip.lockRootRotation = false;
                    clip.keepOriginalPositionY = true;
                    break;
                default: // lock-full
                    clip.lockRootPositionXZ = true;
                    clip.lockRootHeightY = true;
                    clip.lockRootRotation = false;
                    clip.keepOriginalPositionY = true;
                    break;
            }

            clip.keepOriginalOrientation = false;
            clip.keepOriginalPositionXZ = false;
            clip.heightFromFeet = false;
        }

        // -------------------------------------------------------------------
        // Validation helpers
        // -------------------------------------------------------------------

        private static Avatar LoadAvatar(string meshAssetPath)
        {
            UnityEngine.Object[] assets = AssetDatabase.LoadAllAssetsAtPath(meshAssetPath);
            if (assets == null)
            {
                return null;
            }
            foreach (UnityEngine.Object asset in assets)
            {
                Avatar avatar = asset as Avatar;
                if (avatar != null)
                {
                    return avatar;
                }
            }
            return null;
        }

        private static void MeasureMesh(string meshAssetPath, ValidationEntry entry)
        {
            GameObject prefab = AssetDatabase.LoadAssetAtPath<GameObject>(meshAssetPath);
            if (prefab == null)
            {
                entry.problems.Add("无法加载网格 GameObject。");
                return;
            }

            GameObject instance = null;
            try
            {
                instance = UnityEngine.Object.Instantiate(prefab);
                Renderer[] renderers = instance.GetComponentsInChildren<Renderer>();
                if (renderers.Length == 0)
                {
                    entry.problems.Add("网格没有任何 Renderer。");
                    return;
                }

                Bounds bounds = renderers[0].bounds;
                for (int i = 1; i < renderers.Length; i++)
                {
                    bounds.Encapsulate(renderers[i].bounds);
                }

                Vector3 size = bounds.size;
                entry.heightMetres = size.y;
                entry.widthMetres = size.x;
                entry.depthMetres = size.z;
                entry.dominantAxis = DominantAxis(size);

                if (IsHumanoid(new Profile { importType = entry.importType }) ||
                    string.Equals(entry.importType, "Humanoid", StringComparison.OrdinalIgnoreCase))
                {
                    if (size.y < MinHumanoidHeight || size.y > MaxHumanoidHeight)
                    {
                        entry.problems.Add(string.Format(
                            CultureInfo.InvariantCulture,
                            "尺寸异常：高度 {0:F3} m 不在 {1:F1}~{2:F1} m 范围（检查缩放/单位）。",
                            size.y, MinHumanoidHeight, MaxHumanoidHeight));
                    }
                    if (size.y <= size.x || size.y <= size.z)
                    {
                        entry.problems.Add(string.Format(
                            CultureInfo.InvariantCulture,
                            "朝向可疑：Y(高)={0:F3} 未占主导（X={1:F3}, Z={2:F3}），可能轴向错误。",
                            size.y, size.x, size.z));
                    }
                }
            }
            catch (Exception exception)
            {
                entry.problems.Add("测量尺寸失败: " + exception.Message);
            }
            finally
            {
                if (instance != null)
                {
                    UnityEngine.Object.DestroyImmediate(instance);
                }
            }
        }

        private static string DominantAxis(Vector3 size)
        {
            if (size.y >= size.x && size.y >= size.z) return "Y(up)";
            if (size.x >= size.y && size.x >= size.z) return "X(right)";
            return "Z(forward)";
        }

        private static AnimationClip LoadPrimaryClip(string animationAssetPath)
        {
            return AssetDatabase
                .LoadAllAssetsAtPath(animationAssetPath)
                .OfType<AnimationClip>()
                .FirstOrDefault(c => !c.name.StartsWith("__preview__", StringComparison.Ordinal));
        }

        /// <summary>Record a few binding paths/properties so the report can explain itself.</summary>
        private static void CollectSampleCurves(string animationAssetPath, List<string> sink)
        {
            AnimationClip clip = LoadPrimaryClip(animationAssetPath);
            if (clip == null)
            {
                return;
            }
            foreach (EditorCurveBinding binding in AnimationUtility.GetCurveBindings(clip))
            {
                if (sink.Count >= 12)
                {
                    return;
                }
                string entry = (string.IsNullOrEmpty(binding.path) ? "<root>" : binding.path) +
                               " :: " + binding.propertyName;
                if (!sink.Contains(entry))
                {
                    sink.Add(entry);
                }
            }
        }

        /// <summary>
        /// Does this clip translate a root bone?
        ///
        /// Unreal's root bone is called "root"; after FBX import the binding
        /// path is either "" (the model root) or "root". Anything deeper is a
        /// limb, not root motion. Also reports how many position curves the
        /// clip has at all, which distinguishes "in-place animation" from
        /// "we looked in the wrong place".
        /// </summary>
        private static bool FindRootMotion(
            string animationAssetPath,
            out string rootPath,
            out int positionBindings,
            out int totalBindings)
        {
            rootPath = null;
            positionBindings = 0;
            totalBindings = 0;

            AnimationClip clip = LoadPrimaryClip(animationAssetPath);
            if (clip == null)
            {
                return false;
            }

            foreach (EditorCurveBinding binding in AnimationUtility.GetCurveBindings(clip))
            {
                totalBindings++;
                if (binding.propertyName == null ||
                    !binding.propertyName.StartsWith("m_LocalPosition", StringComparison.Ordinal))
                {
                    continue;
                }
                positionBindings++;

                string path = binding.path ?? string.Empty;
                bool atRoot = path.Length == 0 ||
                              path.Equals("root", StringComparison.OrdinalIgnoreCase) ||
                              path.EndsWith("/root", StringComparison.OrdinalIgnoreCase);
                if (!atRoot)
                {
                    continue;
                }

                AnimationCurve curve = AnimationUtility.GetEditorCurve(clip, binding);
                if (curve != null && CurveVaries(curve))
                {
                    rootPath = path.Length == 0 ? "<model root>" : path;
                    return true;
                }
            }
            return false;
        }

        private static bool CurveVaries(AnimationCurve curve)
        {
            Keyframe[] keys = curve.keys;
            if (keys.Length < 2)
            {
                return false;
            }
            float first = keys[0].value;
            for (int i = 1; i < keys.Length; i++)
            {
                if (Mathf.Abs(keys[i].value - first) > 1e-4f)
                {
                    return true;
                }
            }
            return false;
        }

        // -------------------------------------------------------------------
        // Path helpers
        // -------------------------------------------------------------------

        private static string Normalise(string relative)
        {
            return relative.Replace('/', Path.DirectorySeparatorChar);
        }

        private static string ToAssetPath(string exportRoot, string relative)
        {
            if (string.IsNullOrEmpty(relative))
            {
                return null;
            }
            string full = Path.GetFullPath(Path.Combine(exportRoot, Normalise(relative)));
            string dataPath = Path.GetFullPath(Application.dataPath);
            if (!full.StartsWith(dataPath, StringComparison.OrdinalIgnoreCase))
            {
                return null;
            }
            string stripped = full.Substring(dataPath.Length).TrimStart('/', '\\');
            return "Assets/" + stripped.Replace('\\', '/');
        }

        /// <summary>Copy the export tree into Assets when it lives outside the project.</summary>
        private static string EnsureInsideAssets(string exportRoot)
        {
            string full = Path.GetFullPath(exportRoot);
            string dataPath = Path.GetFullPath(Application.dataPath);
            if (full.StartsWith(dataPath, StringComparison.OrdinalIgnoreCase))
            {
                return full;
            }

            string destination = Path.Combine(dataPath, DefaultExportFolderName);
            Debug.Log("[FbxConverter] 导出目录不在 Assets 下，正在复制到 " + destination);
            CopyDirectory(full, destination);
            AssetDatabase.Refresh();
            return Path.GetFullPath(destination);
        }

        private static void CopyDirectory(string source, string destination)
        {
            Directory.CreateDirectory(destination);
            foreach (string directory in Directory.GetDirectories(source, "*", SearchOption.AllDirectories))
            {
                Directory.CreateDirectory(directory.Replace(source, destination));
            }
            foreach (string file in Directory.GetFiles(source, "*", SearchOption.AllDirectories))
            {
                string target = file.Replace(source, destination);
                File.Copy(file, target, true);
            }
        }

        // -------------------------------------------------------------------
        // Reporting
        // -------------------------------------------------------------------

        private static string WriteReport(
            string exportRoot, Manifest manifest, List<ValidationEntry> entries)
        {
            StringBuilder builder = new StringBuilder();
            builder.Append("{\n");
            builder.Append("  \"schemaVersion\": 1,\n");
            builder.Append("  \"generatedAt\": \"").Append(Escape(DateTime.Now.ToString("yyyy-MM-ddTHH:mm:ss"))).Append("\",\n");
            builder.Append("  \"unityVersion\": \"").Append(Escape(Application.unityVersion)).Append("\",\n");
            builder.Append("  \"manifest\": \"manifest.json\",\n");
            builder.Append("  \"source\": {\n");
            builder.Append("    \"contentRoot\": \"").Append(Escape(manifest.source != null ? manifest.source.contentRoot : "")).Append("\",\n");
            builder.Append("    \"engineVersion\": \"").Append(Escape(manifest.source != null ? manifest.source.engineVersion : "")).Append("\"\n");
            builder.Append("  },\n");

            int warnings = entries.Count(e => e.status == "warnings");
            int failed = entries.Count(e => e.status == "failed");
            builder.Append("  \"summary\": {\n");
            builder.Append("    \"characters\": ").Append(entries.Count).Append(",\n");
            builder.Append("    \"ok\": ").Append(entries.Count(e => e.status == "ok")).Append(",\n");
            builder.Append("    \"warnings\": ").Append(warnings).Append(",\n");
            builder.Append("    \"failed\": ").Append(failed).Append("\n");
            builder.Append("  },\n");

            builder.Append("  \"characters\": [\n");
            for (int i = 0; i < entries.Count; i++)
            {
                ValidationEntry entry = entries[i];
                builder.Append("    {\n");
                builder.Append("      \"character\": \"").Append(Escape(entry.character)).Append("\",\n");
                builder.Append("      \"status\": \"").Append(Escape(entry.status)).Append("\",\n");
                builder.Append("      \"meshAsset\": \"").Append(Escape(entry.meshAsset)).Append("\",\n");
                builder.Append("      \"importType\": \"").Append(Escape(entry.importType)).Append("\",\n");
                builder.Append("      \"avatarValid\": ").Append(Bool(entry.avatarValid)).Append(",\n");
                builder.Append("      \"avatarHuman\": ").Append(Bool(entry.avatarHuman)).Append(",\n");
                builder.Append("      \"avatarName\": \"").Append(Escape(entry.avatarName)).Append("\",\n");
                builder.Append("      \"heightMetres\": ").Append(Float(entry.heightMetres)).Append(",\n");
                builder.Append("      \"widthMetres\": ").Append(Float(entry.widthMetres)).Append(",\n");
                builder.Append("      \"depthMetres\": ").Append(Float(entry.depthMetres)).Append(",\n");
                builder.Append("      \"dominantAxis\": \"").Append(Escape(entry.dominantAxis)).Append("\",\n");
                builder.Append("      \"animationCount\": ").Append(entry.animationCount).Append(",\n");
                builder.Append("      \"animationsWithRootMotion\": ").Append(entry.animationsWithRootMotion).Append(",\n");
                builder.Append("      \"clipsWithPositionCurves\": ").Append(entry.clipsWithPositionCurves).Append(",\n");
                builder.Append("      \"totalCurveBindings\": ").Append(entry.totalCurveBindings).Append(",\n");
                builder.Append("      \"sampleCurves\": [");
                for (int s = 0; s < entry.sampleCurves.Count; s++)
                {
                    if (s > 0) builder.Append(", ");
                    builder.Append('"').Append(Escape(entry.sampleCurves[s])).Append('"');
                }
                builder.Append("],\n");
                builder.Append("      \"rootMotionPaths\": [");
                for (int r = 0; r < entry.rootMotionPaths.Count; r++)
                {
                    if (r > 0) builder.Append(", ");
                    builder.Append('"').Append(Escape(entry.rootMotionPaths[r])).Append('"');
                }
                builder.Append("],\n");
                builder.Append("      \"animationFailures\": ").Append(entry.animationFailures).Append(",\n");
                builder.Append("      \"notices\": [");
                for (int n = 0; n < entry.notices.Count; n++)
                {
                    if (n > 0) builder.Append(", ");
                    builder.Append('"').Append(Escape(entry.notices[n])).Append('"');
                }
                builder.Append("],\n");
                builder.Append("      \"problems\": [");
                for (int p = 0; p < entry.problems.Count; p++)
                {
                    if (p > 0) builder.Append(", ");
                    builder.Append('"').Append(Escape(entry.problems[p])).Append('"');
                }
                builder.Append("]\n");
                builder.Append(i == entries.Count - 1 ? "    }\n" : "    },\n");
            }
            builder.Append("  ]\n");
            builder.Append("}\n");

            string reportsDirectory = Path.Combine(exportRoot, "Reports");
            Directory.CreateDirectory(reportsDirectory);
            string reportPath = Path.Combine(reportsDirectory, "unity_import_report.json");
            File.WriteAllText(reportPath, builder.ToString(), new UTF8Encoding(false));
            return reportPath;
        }

        private static void ReportSummary(List<ValidationEntry> entries, string reportPath)
        {
            foreach (ValidationEntry entry in entries)
            {
                if (entry.problems.Count == 0)
                {
                    Debug.Log(string.Format(
                        CultureInfo.InvariantCulture,
                        "[FbxConverter] {0}: OK（{1} 个动画，高度 {2:F3} m，主轴 {3}）",
                        entry.character, entry.animationCount, entry.heightMetres, entry.dominantAxis));
                }
                else
                {
                    foreach (string problem in entry.problems)
                    {
                        Debug.LogWarning(string.Format(
                            "[FbxConverter] {0}: {1}", entry.character, problem));
                    }
                }
            }

            int problems = entries.Sum(e => e.problems.Count);
            int notices = entries.Sum(e => e.notices.Count);
            Debug.Log(string.Format(
                CultureInfo.InvariantCulture,
                "[FbxConverter] 导入完成：{0} 个角色，{1} 条问题，{2} 条提示。报告：{3}",
                entries.Count, problems, notices, reportPath));
        }

        private static string Bool(bool value)
        {
            return value ? "true" : "false";
        }

        private static string Float(float value)
        {
            return value.ToString("0.######", CultureInfo.InvariantCulture);
        }

        private static string Escape(string value)
        {
            if (string.IsNullOrEmpty(value))
            {
                return string.Empty;
            }
            StringBuilder builder = new StringBuilder(value.Length + 8);
            foreach (char character in value)
            {
                switch (character)
                {
                    case '"': builder.Append("\\\""); break;
                    case '\\': builder.Append("\\\\"); break;
                    case '\n': builder.Append("\\n"); break;
                    case '\r': builder.Append("\\r"); break;
                    case '\t': builder.Append("\\t"); break;
                    default:
                        if (character < ' ')
                        {
                            builder.Append("\\u").Append(((int)character).ToString("x4"));
                        }
                        else
                        {
                            builder.Append(character);
                        }
                        break;
                }
            }
            return builder.ToString();
        }
    }
}

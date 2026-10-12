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
using UnityEngine.Rendering;

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
    public class ManifestTexture
    {
        public string name;
        public string path;
        public string unrealPackage;
        public string textureType;
        public string unrealClass;
        public bool srgb;
        public int bytes;
        public int width;
        public int height;
    }

    [Serializable]
    public class ManifestMaterial
    {
        public string name;
        public string unrealMaterial;
        public string baseColor;
        public string normal;
        public string mask;
        public string[] allTextures;
    }

    [Serializable]
    public class ManifestSlot
    {
        public string slotName;
        public int materialIndex;
    }

    [Serializable]
    public class ManifestStaticMesh
    {
        public string name;
        public string fbx;
        public string unrealPackage;
        public string group;
        public string relativeDir;
        public int triangleHint;
        public int slotCount;
        public ManifestSlot[] slots;
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
        public ManifestTexture[] textures;
        public ManifestMaterial[] materials;
        public ManifestStaticMesh[] staticMeshes;
        public int staticMeshCount;
        public int textureCount;
        public int materialCount;
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
        public int animationsWithRootTravel;
        public int animationFailures;
        public int clipsWithTranslationCurves;
        public int totalCurveBindings;
        public readonly List<string> sampleCurves = new List<string>();
        public readonly List<string> rootMotionPaths = new List<string>();
        public readonly List<string> problems = new List<string>();
        public readonly List<string> notices = new List<string>();
    }

    public class TextureEntry
    {
        public string package;
        public string assetPath;
        public string textureType;
        public bool srgb;
        public int bytes;
        public string status = "ok";
        public readonly List<string> problems = new List<string>();
    }

    public class StaticMeshEntry
    {
        public string name;
        public string group;
        public string fbx;
        public string meshAsset;
        public int slotCount;
        public int rendererCount;
        public int triangleCount;
        public float heightMetres;
        public float widthMetres;
        public float depthMetres;
        public string dominantAxis;
        public string status = "ok";
        public readonly List<string> materialNames = new List<string>();
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
            bool hasCharacters = manifest != null
                                 && manifest.characters != null
                                 && manifest.characters.Length > 0;
            bool hasStatics = manifest != null
                              && manifest.staticMeshes != null
                              && manifest.staticMeshes.Length > 0;
            if (!hasCharacters && !hasStatics)
            {
                Fail("manifest.json 中没有角色或静态网格条目。");
                return;
            }

            Debug.Log(string.Format(
                CultureInfo.InvariantCulture,
                "[FbxConverter] 导入 {0} 个角色 / {1} 个动画 / {2} 个静态网格 / {3} 个贴图（引擎 {4}）",
                manifest.characterCount,
                manifest.animationCount,
                manifest.staticMeshCount,
                manifest.textureCount,
                manifest.source != null ? manifest.source.engineVersion : "?"));

            List<ValidationEntry> results = new List<ValidationEntry>();
            if (hasCharacters)
            {
                foreach (ManifestCharacter character in manifest.characters)
                {
                    results.Add(ImportCharacter(exportRoot, character, manifest));
                }
            }

            List<TextureEntry> textureResults = new List<TextureEntry>();
            List<StaticMeshEntry> staticResults = new List<StaticMeshEntry>();
            if (hasStatics)
            {
                ImportStatics(exportRoot, manifest, textureResults, staticResults);
            }

            AssetDatabase.SaveAssets();
            AssetDatabase.Refresh();

            string reportPath = WriteReport(
                exportRoot, manifest, results, textureResults, staticResults);
            ReportSummary(results, textureResults, staticResults, reportPath);
        }

        private static ValidationEntry ImportCharacter(
            string exportRoot, ManifestCharacter character, Manifest manifest)
        {
            ValidationEntry entry = new ValidationEntry
            {
                character = character.name,
                animationCount = 0,
                animationsWithRootMotion = 0,
                animationsWithRootTravel = 0,
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
                    bool translates;
                    bool travels;
                    string rootSample;
                    int translationBindings;
                    int totalBindings;
                    AnalyseRootMotion(
                        animationAssetPath,
                        out translates,
                        out travels,
                        out rootSample,
                        out translationBindings,
                        out totalBindings);
                    entry.totalCurveBindings += totalBindings;
                    if (totalBindings == 0)
                    {
                        entry.animationFailures++;
                        entry.problems.Add(animation.name +
                            "：导入后的 AnimationClip 没有任何曲线（检查 FBX 是否包含动画数据）。");
                    }
                    if (translationBindings > 0)
                    {
                        entry.clipsWithTranslationCurves++;
                    }
                    if (entry.sampleCurves.Count < 12)
                    {
                        CollectSampleCurves(animationAssetPath, entry.sampleCurves);
                    }
                    if (translates)
                    {
                        entry.animationsWithRootMotion++;
                        if (!string.IsNullOrEmpty(rootSample) &&
                            !entry.rootMotionPaths.Contains(rootSample))
                        {
                            entry.rootMotionPaths.Add(rootSample);
                        }
                    }
                    else if (animation.rootMotion)
                    {
                        // Informational: plenty of animations are legitimately
                        // in place, so this must not mark the character as bad.
                        entry.notices.Add(animation.name +
                            "：未检测到根骨骼位移（原地动画）。");
                    }
                    if (travels)
                    {
                        entry.animationsWithRootTravel++;
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
        // Static meshes, materials and textures
        //
        // Unreal's FBX exporter cannot embed textures (FbxMainExport.cpp
        // hard-codes bEmbedMedia = false, and UFbxExportOption has no switch
        // for it), and what it writes instead is the *original authoring path*
        // from the texture's import data. So the PNGs are exported separately
        // and stitched back together here, from manifest.json.
        // -------------------------------------------------------------------

        private static void ImportStatics(
            string exportRoot,
            Manifest manifest,
            List<TextureEntry> textureResults,
            List<StaticMeshEntry> staticResults)
        {
            // --- 1. textures -------------------------------------------------
            Dictionary<string, Texture2D> loaded = new Dictionary<string, Texture2D>();
            if (manifest.textures != null)
            {
                foreach (ManifestTexture texture in manifest.textures)
                {
                    TextureEntry entry = ImportTexture(exportRoot, texture, loaded);
                    textureResults.Add(entry);
                }
            }
            // --- 2. one shared Material asset per Unreal material -------------
            // Created *before* the models are imported so the importer's
            // SearchAndRemapMaterials can find them by name and wire the model
            // prefab straight at them.
            Dictionary<int, Material> materials = new Dictionary<int, Material>();
            Dictionary<string, string> claimed = new Dictionary<string, string>();
            List<string> materialProblems = new List<string>();
            if (manifest.materials != null)
            {
                for (int index = 0; index < manifest.materials.Length; index++)
                {
                    Material material = BuildMaterial(
                        exportRoot, manifest.materials[index], loaded,
                        claimed, materialProblems);
                    if (material != null)
                    {
                        materials[index] = material;
                    }
                }
            }

            AssetDatabase.SaveAssets();

            // --- 3. the meshes themselves ------------------------------------
            if (manifest.staticMeshes == null)
            {
                return;
            }
            foreach (ManifestStaticMesh mesh in manifest.staticMeshes)
            {
                StaticMeshEntry entry = ImportStaticMesh(exportRoot, mesh, manifest, materials);
                // A name clash affects every mesh that uses the losing material.
                if (materialProblems.Count > 0 && mesh.slots != null)
                {
                    foreach (string problem in materialProblems)
                    {
                        if (!entry.problems.Contains(problem))
                        {
                            entry.problems.Add(problem);
                        }
                    }
                    if (entry.status == "ok")
                    {
                        entry.status = "warnings";
                    }
                }
                staticResults.Add(entry);
            }
        }

        private static TextureEntry ImportTexture(
            string exportRoot, ManifestTexture texture, Dictionary<string, Texture2D> loaded)
        {
            TextureEntry entry = new TextureEntry
            {
                package = texture.unrealPackage,
                textureType = texture.textureType,
                srgb = texture.srgb,
                bytes = texture.bytes,
            };

            string assetPath = ToAssetPath(exportRoot, texture.path);
            entry.assetPath = assetPath;
            if (string.IsNullOrEmpty(assetPath))
            {
                entry.status = "failed";
                entry.problems.Add("贴图不在 Assets 下或文件缺失: " + texture.path);
                return entry;
            }

            TextureImporter importer = AssetImporter.GetAtPath(assetPath) as TextureImporter;
            if (importer == null)
            {
                entry.status = "failed";
                entry.problems.Add("无法获取 TextureImporter: " + assetPath);
                return entry;
            }

            bool isNormal = string.Equals(texture.textureType, "normal", StringComparison.OrdinalIgnoreCase);
            try
            {
                importer.textureType = isNormal ? TextureImporterType.NormalMap : TextureImporterType.Default;
                importer.sRGBTexture = isNormal ? false : texture.srgb;
                importer.mipmapEnabled = true;
                importer.wrapMode = TextureWrapMode.Repeat;
                importer.filterMode = FilterMode.Bilinear;
            }
            catch (Exception exception)
            {
                entry.status = "failed";
                entry.problems.Add("设置贴图导入参数失败: " + exception.Message);
                return entry;
            }

            AssetDatabase.ImportAsset(assetPath, ImportAssetOptions.ForceUpdate);

            Texture2D asset = AssetDatabase.LoadAssetAtPath<Texture2D>(assetPath);
            if (asset == null)
            {
                entry.status = "failed";
                entry.problems.Add("导入后无法加载贴图: " + assetPath);
                return entry;
            }
            loaded[texture.name] = asset;
            if (asset.width <= 1 && asset.height <= 1)
            {
                entry.problems.Add("贴图尺寸异常（" + asset.width + "x" + asset.height + "）。");
            }
            else if (texture.width > 0 && asset.width < texture.width)
            {
                // Unity's default maxTextureSize is 2048; silent downscaling is
                // the usual reason an imported model looks softer than Unreal.
                entry.problems.Add("贴图被降采样：" + texture.width + "x" + texture.height +
                    " -> " + asset.width + "x" + asset.height +
                    "（TextureImporter.maxTextureSize 上限）。需要原始尺寸请调高该值后重新导入。");
            }
            if (entry.problems.Count > 0)
            {
                entry.status = "warnings";
            }
            return entry;
        }

        private static Shader ResolveLitShader()
        {
            // The project's own pipeline decides which Lit shader exists; asking
            // for the other one silently yields null and leaves materials with
            // the FBX-imported shader.
            bool scriptable = GraphicsSettings.currentRenderPipeline != null ||
                              GraphicsSettings.defaultRenderPipeline != null;
            if (scriptable)
            {
                Shader urp = Shader.Find("Universal Render Pipeline/Lit");
                if (urp != null)
                {
                    return urp;
                }
                Shader hdrp = Shader.Find("HDRP/Lit");
                if (hdrp != null)
                {
                    return hdrp;
                }
            }
            return Shader.Find("Standard");
        }

        private static void SetFirstProperty(Material material, string[] names, Texture texture)
        {
            if (material == null || texture == null)
            {
                return;
            }
            foreach (string name in names)
            {
                if (material.HasProperty(name))
                {
                    material.SetTexture(name, texture);
                    return;
                }
            }
        }

        private static Material BuildMaterial(
            string exportRoot,
            ManifestMaterial definition,
            Dictionary<string, Texture2D> loaded,
            Dictionary<string, string> claimed,
            List<string> problems)
        {
            if (definition == null || string.IsNullOrEmpty(definition.name))
            {
                return null;
            }

            string materialDirectory = Path.Combine(exportRoot, "StaticMeshes", "Materials");
            Directory.CreateDirectory(materialDirectory);

            // Material assets live in one folder next to the meshes, so the
            // FBX importer's RecursiveUp search finds exactly one candidate per
            // Unreal material instead of a per-FBX duplicate.
            //
            // The asset name has to stay equal to the Unreal material name,
            // because that is what Unity matches on. Two different Unreal
            // materials sharing a name therefore cannot both bind; keep them as
            // separate assets so one is not silently overwritten, and report it.
            string assetName = definition.name;
            if (claimed.TryGetValue(assetName, out string owner) &&
                !string.Equals(owner, definition.unrealMaterial, StringComparison.Ordinal))
            {
                int suffix = 2;
                string alternative;
                do
                {
                    alternative = assetName + "__" + suffix;
                    suffix++;
                }
                while (claimed.ContainsKey(alternative));
                problems.Add("材质同名冲突：" + owner + " 与 " + definition.unrealMaterial +
                    " 都叫 " + assetName + "；后者存为 " + alternative +
                    ".mat，但 Unity 按名绑定只能命中前者。");
                claimed[alternative] = definition.unrealMaterial;
                assetName = alternative;
            }
            else
            {
                claimed[assetName] = definition.unrealMaterial;
            }

            string relative = "StaticMeshes/Materials/" + assetName + ".mat";
            string assetPath = ToAssetPath(exportRoot, relative);
            if (string.IsNullOrEmpty(assetPath))
            {
                return null;
            }

            Material material = AssetDatabase.LoadAssetAtPath<Material>(assetPath);
            if (material == null)
            {
                Shader shader = ResolveLitShader();
                material = shader != null ? new Material(shader) : new Material(Shader.Find("Standard"));
                material.name = assetName;
                AssetDatabase.CreateAsset(material, assetPath);
            }
            else
            {
                Shader shader = ResolveLitShader();
                if (shader != null && material.shader != shader)
                {
                    material.shader = shader;
                }
            }

            AssignTexture(material, definition.baseColor, loaded,
                new[] { "_BaseMap", "_MainTex" }, new[] { "_BaseColor", "_Color" });
            AssignTexture(material, definition.normal, loaded,
                new[] { "_BumpMap", "_NormalMap" }, null);
            AssignTexture(material, definition.mask, loaded,
                new[] { "_MetallicGlossMap", "_OcclusionMap" }, null);

            // An Unreal material with no texture parameters is a flat colour;
            // there is nothing to reconstruct, so use a neutral tint rather
            // than leaving the default magenta-ish placeholder.
            if (string.IsNullOrEmpty(definition.baseColor) &&
                string.IsNullOrEmpty(definition.normal) &&
                string.IsNullOrEmpty(definition.mask))
            {
                Color neutral = new Color(0.75f, 0.75f, 0.75f, 1f);
                if (material.HasProperty("_BaseColor"))
                {
                    material.SetColor("_BaseColor", neutral);
                }
                if (material.HasProperty("_Color"))
                {
                    material.SetColor("_Color", neutral);
                }
            }

            EditorUtility.SetDirty(material);
            return material;
        }

        private static void AssignTexture(
            Material material,
            string textureName,
            Dictionary<string, Texture2D> loaded,
            string[] textureProperties,
            string[] tintProperties)
        {
            if (string.IsNullOrEmpty(textureName) || !loaded.ContainsKey(textureName))
            {
                return;
            }
            SetFirstProperty(material, textureProperties, loaded[textureName]);
            if (tintProperties != null)
            {
                foreach (string name in tintProperties)
                {
                    if (material.HasProperty(name))
                    {
                        material.SetColor(name, Color.white);
                        break;
                    }
                }
            }
        }

        private static StaticMeshEntry ImportStaticMesh(
            string exportRoot,
            ManifestStaticMesh mesh,
            Manifest manifest,
            Dictionary<int, Material> materials)
        {
            StaticMeshEntry entry = new StaticMeshEntry
            {
                name = mesh.name,
                group = mesh.group,
                fbx = mesh.fbx,
                slotCount = mesh.slotCount,
            };

            string assetPath = ToAssetPath(exportRoot, mesh.fbx);
            entry.meshAsset = assetPath;
            if (string.IsNullOrEmpty(assetPath))
            {
                entry.status = "failed";
                entry.problems.Add("网格 FBX 缺失或不在 Assets 下: " + mesh.fbx);
                return entry;
            }

            ModelImporter importer = AssetImporter.GetAtPath(assetPath) as ModelImporter;
            if (importer == null)
            {
                entry.status = "failed";
                entry.problems.Add("无法获取 ModelImporter: " + assetPath);
                return entry;
            }

            // Import the FBX materials (Unreal wrote the material *names* into
            // it) and let Unity attach the Material assets we just created,
            // instead of leaving per-FBX duplicates behind.
            importer.materialImportMode = ModelImporterMaterialImportMode.ImportStandard;
            importer.materialName = ModelImporterMaterialName.BasedOnMaterialName;
            importer.materialSearch = ModelImporterMaterialSearch.RecursiveUp;
            importer.globalScale = manifest.settings != null ? manifest.settings.scale : 1f;
            importer.importAnimation = false;
            importer.importBlendShapes = false;
            importer.importCameras = false;
            importer.importLights = false;
            importer.isReadable = false;
            importer.SaveAndReimport();

            importer.SearchAndRemapMaterials(
                ModelImporterMaterialName.BasedOnMaterialName,
                ModelImporterMaterialSearch.RecursiveUp);

            AssetDatabase.ImportAsset(assetPath, ImportAssetOptions.ForceUpdate);

            GameObject prefab = AssetDatabase.LoadAssetAtPath<GameObject>(assetPath);
            if (prefab == null)
            {
                entry.status = "failed";
                entry.problems.Add("导入后无法加载模型: " + assetPath);
                return entry;
            }

            Renderer[] renderers = prefab.GetComponentsInChildren<Renderer>(true);
            entry.rendererCount = renderers.Length;
            foreach (Renderer renderer in renderers)
            {
                foreach (Material material in renderer.sharedMaterials)
                {
                    string name = material == null ? "(null)" : material.name;
                    if (!entry.materialNames.Contains(name))
                    {
                        entry.materialNames.Add(name);
                    }
                }
                MeshFilter filter = renderer.GetComponent<MeshFilter>();
                if (filter != null && filter.sharedMesh != null)
                {
                    entry.triangleCount += filter.sharedMesh.triangles.Length / 3;
                }
            }

            // Did the remap actually take? A material still named after Unity's
            // default means the FBX carried no usable material node for that
            // slot, which is a real (reportable) limitation, not a success.
            for (int index = 0; index < entry.materialNames.Count; index++)
            {
                string name = entry.materialNames[index];
                if (name == "Default-Material" || name == "Default-Material (Instance)" ||
                    name == "WorldGridMaterial" || name == "(null)")
                {
                    entry.problems.Add("材质槽 " + index + " 仍在使用 Unity 默认材质（" + name +
                        "）：该网格的材质没有随 FBX 导出。");
                }
            }

            if (materials.Count > 0 && mesh.slots != null)
            {
                foreach (ManifestSlot slot in mesh.slots)
                {
                    if (!materials.ContainsKey(slot.materialIndex))
                    {
                        entry.problems.Add("材质表缺少索引 " + slot.materialIndex +
                            "（槽 " + slot.slotName + "）。");
                    }
                }
            }

            MeasureMesh(assetPath, entry);
            if (entry.problems.Count > 0 && entry.status == "ok")
            {
                entry.status = "warnings";
            }
            return entry;
        }

        private static void MeasureMesh(string assetPath, StaticMeshEntry entry)
        {
            GameObject prefab = AssetDatabase.LoadAssetAtPath<GameObject>(assetPath);
            if (prefab == null)
            {
                return;
            }
            Renderer[] renderers = prefab.GetComponentsInChildren<Renderer>(true);
            if (renderers.Length == 0)
            {
                return;
            }
            Bounds bounds = renderers[0].bounds;
            for (int index = 1; index < renderers.Length; index++)
            {
                bounds.Encapsulate(renderers[index].bounds);
            }
            // UE exports centimetres and Unity's file scale turns them into
            // metres, so the imported bounds are directly comparable.
            entry.widthMetres = bounds.size.x;
            entry.heightMetres = bounds.size.y;
            entry.depthMetres = bounds.size.z;
            entry.dominantAxis = DominantAxis(bounds.size);
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
        private static void AnalyseRootMotion(
            string animationAssetPath,
            out bool translates,
            out bool travels,
            out string sample,
            out int translationBindings,
            out int totalBindings)
        {
            translates = false;
            travels = false;
            sample = null;
            translationBindings = 0;
            totalBindings = 0;

            AnimationClip clip = LoadPrimaryClip(animationAssetPath);
            if (clip == null)
            {
                return;
            }

            foreach (EditorCurveBinding binding in AnimationUtility.GetCurveBindings(clip))
            {
                totalBindings++;

                string property = binding.propertyName ?? string.Empty;
                string path = binding.path ?? string.Empty;

                // Humanoid rigs do not emit m_LocalPosition at all: root
                // translation arrives as muscle curves named RootT.x/y/z.
                // Checking only for m_LocalPosition silently reports zero root
                // motion for every Humanoid clip.
                bool humanoidRoot = property.StartsWith("RootT.", StringComparison.Ordinal);
                bool genericRoot = property.StartsWith("m_LocalPosition", StringComparison.Ordinal);

                if (!humanoidRoot && !genericRoot)
                {
                    continue;
                }

                if (genericRoot)
                {
                    bool atRoot = path.Length == 0 ||
                                  path.Equals("root", StringComparison.OrdinalIgnoreCase) ||
                                  path.EndsWith("/root", StringComparison.OrdinalIgnoreCase);
                    if (!atRoot)
                    {
                        continue;
                    }
                }

                translationBindings++;

                AnimationCurve curve = AnimationUtility.GetEditorCurve(clip, binding);
                if (curve == null || !CurveVaries(curve))
                {
                    continue;
                }

                translates = true;
                if (sample == null)
                {
                    sample = humanoidRoot
                        ? property
                        : (path.Length == 0 ? "<model root>" : path);
                }

                // Horizontal channels decide whether the character actually
                // travels, as opposed to just bobbing vertically in place.
                char channel = property[property.Length - 1];
                if (channel == 'x' || channel == 'z')
                {
                    travels = true;
                }
            }
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
            string exportRoot,
            Manifest manifest,
            List<ValidationEntry> entries,
            List<TextureEntry> textures,
            List<StaticMeshEntry> statics)
        {
            StringBuilder builder = new StringBuilder();
            builder.Append("{\n");
            builder.Append("  \"schemaVersion\": 2,\n");
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
            builder.Append("    \"failed\": ").Append(failed).Append(",\n");
            builder.Append("    \"textures\": ").Append(textures.Count).Append(",\n");
            builder.Append("    \"texturesOk\": ").Append(textures.Count(t => t.status == "ok")).Append(",\n");
            builder.Append("    \"staticMeshes\": ").Append(statics.Count).Append(",\n");
            builder.Append("    \"staticMeshesOk\": ").Append(statics.Count(s => s.status == "ok")).Append(",\n");
            builder.Append("    \"materials\": ").Append(manifest.materials != null ? manifest.materials.Length : 0).Append("\n");
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
                builder.Append("      \"animationsWithRootTravel\": ").Append(entry.animationsWithRootTravel).Append(",\n");
                builder.Append("      \"clipsWithTranslationCurves\": ").Append(entry.clipsWithTranslationCurves).Append(",\n");
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
            builder.Append("  ],\n");

            builder.Append("  \"textures\": [\n");
            for (int i = 0; i < textures.Count; i++)
            {
                TextureEntry texture = textures[i];
                builder.Append("    {\n");
                builder.Append("      \"unrealPackage\": \"").Append(Escape(texture.package)).Append("\",\n");
                builder.Append("      \"assetPath\": \"").Append(Escape(texture.assetPath)).Append("\",\n");
                builder.Append("      \"textureType\": \"").Append(Escape(texture.textureType)).Append("\",\n");
                builder.Append("      \"srgb\": ").Append(Bool(texture.srgb)).Append(",\n");
                builder.Append("      \"bytes\": ").Append(texture.bytes).Append(",\n");
                builder.Append("      \"status\": \"").Append(Escape(texture.status)).Append("\",\n");
                // Must stay the last field of the object: the helper's
                // trailingComma argument is the only thing keeping the report
                // valid JSON.
                AppendStringArray(builder, "problems", texture.problems, false);
                builder.Append(i == textures.Count - 1 ? "    }\n" : "    },\n");
            }
            builder.Append("  ],\n");

            builder.Append("  \"staticMeshes\": [\n");
            for (int i = 0; i < statics.Count; i++)
            {
                StaticMeshEntry mesh = statics[i];
                builder.Append("    {\n");
                builder.Append("      \"name\": \"").Append(Escape(mesh.name)).Append("\",\n");
                builder.Append("      \"group\": \"").Append(Escape(mesh.group)).Append("\",\n");
                builder.Append("      \"fbx\": \"").Append(Escape(mesh.fbx)).Append("\",\n");
                builder.Append("      \"meshAsset\": \"").Append(Escape(mesh.meshAsset)).Append("\",\n");
                builder.Append("      \"status\": \"").Append(Escape(mesh.status)).Append("\",\n");
                builder.Append("      \"slotCount\": ").Append(mesh.slotCount).Append(",\n");
                builder.Append("      \"rendererCount\": ").Append(mesh.rendererCount).Append(",\n");
                builder.Append("      \"triangleCount\": ").Append(mesh.triangleCount).Append(",\n");
                builder.Append("      \"widthMetres\": ").Append(Float(mesh.widthMetres)).Append(",\n");
                builder.Append("      \"heightMetres\": ").Append(Float(mesh.heightMetres)).Append(",\n");
                builder.Append("      \"depthMetres\": ").Append(Float(mesh.depthMetres)).Append(",\n");
                builder.Append("      \"dominantAxis\": \"").Append(Escape(mesh.dominantAxis)).Append("\",\n");
                AppendStringArray(builder, "materialNames", mesh.materialNames, true);
                AppendStringArray(builder, "problems", mesh.problems, true);
                AppendStringArray(builder, "notices", mesh.notices, false);
                builder.Append(i == statics.Count - 1 ? "    }\n" : "    },\n");
            }
            builder.Append("  ]\n");
            builder.Append("}\n");

            string reportsDirectory = Path.Combine(exportRoot, "Reports");
            Directory.CreateDirectory(reportsDirectory);
            string reportPath = Path.Combine(reportsDirectory, "unity_import_report.json");
            File.WriteAllText(reportPath, builder.ToString(), new UTF8Encoding(false));
            return reportPath;
        }

        /// <summary>
        /// Append a JSON string array. <paramref name="trailingComma"/> must be
        /// false for the last field of the enclosing object -- JSON has no
        /// trailing commas, and a single wrong flag makes the whole report
        /// unparsable.
        /// </summary>
        private static void AppendStringArray(
            StringBuilder builder, string name, List<string> values, bool trailingComma)
        {
            builder.Append("      \"").Append(name).Append("\": [");
            for (int i = 0; i < values.Count; i++)
            {
                if (i > 0)
                {
                    builder.Append(", ");
                }
                builder.Append('"').Append(Escape(values[i])).Append('"');
            }
            builder.Append("]").Append(trailingComma ? ",\n" : "\n");
        }

        private static void ReportSummary(
            List<ValidationEntry> entries,
            List<TextureEntry> textures,
            List<StaticMeshEntry> statics,
            string reportPath)
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

            foreach (TextureEntry texture in textures)
            {
                foreach (string problem in texture.problems)
                {
                    Debug.LogWarning(string.Format(
                        "[FbxConverter] 贴图 {0}: {1}", texture.assetPath, problem));
                }
            }

            foreach (StaticMeshEntry mesh in statics)
            {
                if (mesh.problems.Count == 0)
                {
                    Debug.Log(string.Format(
                        CultureInfo.InvariantCulture,
                        "[FbxConverter] 静态网格 {0}: OK（{1} 个渲染器，{2} 三角形，{3} 个材质，"
                        + "尺寸 {4:F3}x{5:F3}x{6:F3} m）",
                        mesh.name, mesh.rendererCount, mesh.triangleCount,
                        mesh.materialNames.Count, mesh.widthMetres, mesh.heightMetres,
                        mesh.depthMetres));
                }
                else
                {
                    foreach (string problem in mesh.problems)
                    {
                        Debug.LogWarning(string.Format(
                            "[FbxConverter] 静态网格 {0}: {1}", mesh.name, problem));
                    }
                }
            }

            int problems = entries.Sum(e => e.problems.Count)
                           + textures.Sum(t => t.problems.Count)
                           + statics.Sum(s => s.problems.Count);
            int notices = entries.Sum(e => e.notices.Count)
                          + statics.Sum(s => s.notices.Count);
            Debug.Log(string.Format(
                CultureInfo.InvariantCulture,
                "[FbxConverter] 导入完成：{0} 个角色，{1} 个静态网格，{2} 个贴图，"
                + "{3} 条问题，{4} 条提示。报告：{5}",
                entries.Count, statics.Count, textures.Count, problems, notices, reportPath));
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

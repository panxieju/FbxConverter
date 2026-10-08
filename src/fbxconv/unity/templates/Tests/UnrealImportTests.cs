// UnrealImportTests.cs
//
// EditMode tests for the FbxConverter import pipeline (spec stage 4:
// "Unity 导入 -- 自动配置 Humanoid／Generic，验证尺寸、朝向、Avatar 和根骨运动").
//
// Requires the Unity Test Framework (com.unity.test-framework), which ships
// with every 2022.3+ editor. Run from
//   Window > General > Test Runner > EditMode > FbxConverter
// or headlessly:
//   Unity.exe -batchmode -projectPath <p> -runTests -testPlatform EditMode \
//             -testFilter FbxConverter.Tests -logFile -
//
// This file lives in its own assembly (FbxConverter.Tests.asmdef) guarded by
// defineConstraints: ["UNITY_INCLUDE_TESTS"], so Unity skips it entirely --
// rather than failing to compile -- when the Test Framework is not installed.

using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using FbxConverter.Editor;
using NUnit.Framework;
using UnityEditor;
using UnityEngine;

namespace FbxConverter.Tests
{
    public class UnrealImportTests
    {
        private const string ExportRootEnvVar = "FBXCONV_UNITY_EXPORT";
        private const string DefaultFolder = "UnityExport";

        private string _exportRoot;

        private static string ResolveExportRoot()
        {
            string fromEnv = Environment.GetEnvironmentVariable(ExportRootEnvVar);
            if (!string.IsNullOrEmpty(fromEnv) && Directory.Exists(fromEnv))
            {
                return Path.GetFullPath(fromEnv);
            }
            string fallback = Path.Combine(Application.dataPath, DefaultFolder);
            return Directory.Exists(fallback) ? Path.GetFullPath(fallback) : null;
        }

        [SetUp]
        public void SetUp()
        {
            _exportRoot = ResolveExportRoot();
            if (string.IsNullOrEmpty(_exportRoot))
            {
                Assert.Ignore(
                    "找不到 UnityExport 目录。请设置 " + ExportRootEnvVar +
                    " 或把导出结果放到 Assets/" + DefaultFolder + "。");
            }
        }

        private Manifest LoadManifest()
        {
            string path = Path.Combine(_exportRoot, "manifest.json");
            Assert.That(File.Exists(path), Is.True, "缺少 manifest.json: " + path);
            Manifest manifest = JsonUtility.FromJson<Manifest>(File.ReadAllText(path));
            Assert.That(manifest, Is.Not.Null, "manifest.json 解析失败");
            return manifest;
        }

        private Profile LoadProfile(ManifestCharacter character)
        {
            string path = Path.Combine(
                _exportRoot, character.profilePath.Replace('/', Path.DirectorySeparatorChar));
            Assert.That(File.Exists(path), Is.True, "缺少 profile.json: " + path);
            return JsonUtility.FromJson<Profile>(File.ReadAllText(path));
        }

        private static string ToAssetPath(string exportRoot, string relative)
        {
            Assert.That(string.IsNullOrEmpty(relative), Is.False, "相对路径为空");
            string full = Path.GetFullPath(Path.Combine(
                exportRoot, relative.Replace('/', Path.DirectorySeparatorChar)));
            string dataPath = Path.GetFullPath(Application.dataPath);
            Assert.That(
                full.StartsWith(dataPath, StringComparison.OrdinalIgnoreCase),
                Is.True,
                "FBX 不在 Assets 下: " + full);
            return "Assets/" + full.Substring(dataPath.Length).TrimStart('/', '\\')
                .Replace('\\', '/');
        }

        // -------------------------------------------------------------------
        // Manifest / schema
        // -------------------------------------------------------------------

        [Test]
        public void Manifest_IsParseable_AndHasCharacters()
        {
            Manifest manifest = LoadManifest();
            Assert.That(manifest.schemaVersion, Is.EqualTo(1));
            Assert.That(manifest.characters, Is.Not.Null.And.Not.Empty,
                "manifest 中没有角色");
            Assert.That(manifest.characterCount, Is.EqualTo(manifest.characters.Length));
        }

        [Test]
        public void EveryCharacter_HasExistingMeshAndAnimations()
        {
            Manifest manifest = LoadManifest();
            foreach (ManifestCharacter character in manifest.characters)
            {
                string meshAsset = ToAssetPath(_exportRoot, character.meshPath);
                Assert.That(AssetImporter.GetAtPath(meshAsset), Is.Not.Null,
                    character.name + " 的网格 FBX 未导入: " + meshAsset);

                Profile profile = LoadProfile(character);
                if (profile.animations == null)
                {
                    continue;
                }
                foreach (ProfileAnimation animation in profile.animations)
                {
                    string clipAsset = ToAssetPath(_exportRoot, animation.fbx);
                    Assert.That(AssetImporter.GetAtPath(clipAsset), Is.Not.Null,
                        character.name + " 的动画 FBX 未导入: " + clipAsset);
                }
            }
        }

        // -------------------------------------------------------------------
        // Stage 4 acceptance: size, orientation, Avatar, root motion
        // -------------------------------------------------------------------

        [Test]
        public void Character_AvatarIsValid_AndScaleIsSane()
        {
            Manifest manifest = LoadManifest();

            foreach (ManifestCharacter character in manifest.characters)
            {
                string meshAsset = ToAssetPath(_exportRoot, character.meshPath);

                Avatar avatar = AssetDatabase
                    .LoadAllAssetsAtPath(meshAsset)
                    .OfType<Avatar>()
                    .FirstOrDefault();
                Assert.That(avatar, Is.Not.Null, character.name + " 没有生成 Avatar");
                Assert.That(avatar.isValid, Is.True, character.name + " 的 Avatar 无效");

                Profile profile = LoadProfile(character);
                bool humanoid = string.Equals(
                    profile.importType, "Humanoid", StringComparison.OrdinalIgnoreCase);
                if (humanoid)
                {
                    Assert.That(avatar.isHuman, Is.True,
                        character.name + " 请求 Humanoid 但 Avatar 不是人形");
                }

                Vector3 size = MeasureBounds(meshAsset);
                Assert.That(size.y, Is.GreaterThan(0.01f),
                    character.name + " 高度接近 0，可能是单位/缩放错误");
                Assert.That(size.y, Is.LessThan(50f),
                    character.name + " 高度异常巨大，检查 globalScale/useFileScale");

                if (humanoid)
                {
                    Assert.That(size.y, Is.GreaterThan(size.x),
                        character.name + " 朝向可疑：Y 轴不是最长轴（可能轴向错误）");
                    Assert.That(size.y, Is.GreaterThan(size.z),
                        character.name + " 朝向可疑：Y 轴不是最长轴（可能轴向错误）");
                }
            }
        }

        [Test]
        public void Animations_AreBoundToTheCharacterAvatar()
        {
            Manifest manifest = LoadManifest();

            foreach (ManifestCharacter character in manifest.characters)
            {
                string meshAsset = ToAssetPath(_exportRoot, character.meshPath);
                Avatar avatar = AssetDatabase
                    .LoadAllAssetsAtPath(meshAsset).OfType<Avatar>().FirstOrDefault();
                Assert.That(avatar, Is.Not.Null);

                Profile profile = LoadProfile(character);
                if (profile.animations == null)
                {
                    continue;
                }

                foreach (ProfileAnimation animation in profile.animations)
                {
                    string clipAsset = ToAssetPath(_exportRoot, animation.fbx);
                    ModelImporter importer = AssetImporter.GetAtPath(clipAsset) as ModelImporter;
                    Assert.That(importer, Is.Not.Null);
                    Assert.That(importer.sourceAvatar, Is.EqualTo(avatar),
                        animation.name + " 未绑定到角色的 Avatar");

                    AnimationClip clip = AssetDatabase
                        .LoadAllAssetsAtPath(clipAsset)
                        .OfType<AnimationClip>()
                        .FirstOrDefault(c => !c.name.StartsWith("__preview__", StringComparison.Ordinal));
                    Assert.That(clip, Is.Not.Null, animation.name + " 没有生成 AnimationClip");
                    Assert.That(clip.length, Is.GreaterThan(0f),
                        animation.name + " 的时长是 0");
                }
            }
        }

        [Test]
        public void LoopingAnimations_HaveLoopTimeEnabled()
        {
            Manifest manifest = LoadManifest();

            foreach (ManifestCharacter character in manifest.characters)
            {
                Profile profile = LoadProfile(character);
                if (profile.animations == null)
                {
                    continue;
                }

                foreach (ProfileAnimation animation in profile.animations)
                {
                    string clipAsset = ToAssetPath(_exportRoot, animation.fbx);
                    AnimationClip clip = AssetDatabase
                        .LoadAllAssetsAtPath(clipAsset)
                        .OfType<AnimationClip>()
                        .FirstOrDefault(c => !c.name.StartsWith("__preview__", StringComparison.Ordinal));
                    if (clip == null)
                    {
                        continue;
                    }

                    AnimationClipSettings settings = AnimationUtility.GetAnimationClipSettings(clip);
                    if (animation.loop)
                    {
                        Assert.That(settings.loopTime, Is.True,
                            animation.name + " 应循环但 loopTime 为 false");
                    }
                }
            }
        }

        private static Vector3 MeasureBounds(string meshAssetPath)
        {
            GameObject prefab = AssetDatabase.LoadAssetAtPath<GameObject>(meshAssetPath);
            Assert.That(prefab, Is.Not.Null, "无法加载网格: " + meshAssetPath);

            GameObject instance = UnityEngine.Object.Instantiate(prefab);
            try
            {
                Renderer[] renderers = instance.GetComponentsInChildren<Renderer>();
                Assert.That(renderers.Length, Is.GreaterThan(0), "网格没有 Renderer");

                Bounds bounds = renderers[0].bounds;
                for (int i = 1; i < renderers.Length; i++)
                {
                    bounds.Encapsulate(renderers[i].bounds);
                }
                return bounds.size;
            }
            finally
            {
                UnityEngine.Object.DestroyImmediate(instance);
            }
        }
    }
}

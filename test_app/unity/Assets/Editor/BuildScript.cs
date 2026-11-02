using System.IO;
using UnityEditor;
using UnityEditor.Build;
using UnityEditor.Build.Reporting;
using UnityEditor.SceneManagement;
using UnityEngine;

namespace HermesTestRpg.Editor
{
    // Release builds with the Mono backend, like a shipped game: no development
    // build, no script debugging. From the command line:
    //     Unity -batchmode -quit -projectPath test_app/unity \
    //           -executeMethod HermesTestRpg.Editor.BuildScript.BuildAll
    public static class BuildScript
    {
        const string ScenePath = "Assets/Scenes/Main.unity";
        const string ProductName = "HermesTestRpg";

        [MenuItem("Hermes Test/Build Linux")]
        public static void BuildLinux()
        {
            Build(BuildTarget.StandaloneLinux64, "Build/Linux/" + ProductName + ".x86_64");
        }

        [MenuItem("Hermes Test/Build Windows")]
        public static void BuildWindows()
        {
            Build(BuildTarget.StandaloneWindows64, "Build/Windows/" + ProductName + ".exe");
        }

        [MenuItem("Hermes Test/Build All")]
        public static void BuildAll()
        {
            BuildLinux();
            BuildWindows();
        }

        // The game starts from code (Bootstrap), but a player needs one scene.
        static void EnsureScene()
        {
            if (File.Exists(ScenePath))
                return;
            Directory.CreateDirectory(Path.GetDirectoryName(ScenePath));
            var scene = EditorSceneManager.NewScene(NewSceneSetup.DefaultGameObjects, NewSceneMode.Single);
            EditorSceneManager.SaveScene(scene, ScenePath);
            EditorBuildSettings.scenes = new[] { new EditorBuildSettingsScene(ScenePath, true) };
        }

        static void ConfigurePlayer()
        {
            PlayerSettings.companyName = "Hermes";
            PlayerSettings.productName = ProductName;
            PlayerSettings.SetScriptingBackend(NamedBuildTarget.Standalone, ScriptingImplementation.Mono2x);
            PlayerSettings.fullScreenMode = FullScreenMode.Windowed;
            PlayerSettings.defaultScreenWidth = 1280;
            PlayerSettings.defaultScreenHeight = 800;
            PlayerSettings.resizableWindow = true;
            PlayerSettings.runInBackground = true;
        }

        static void Build(BuildTarget target, string path)
        {
            EnsureScene();
            ConfigurePlayer();
            var options = new BuildPlayerOptions
            {
                scenes = new[] { ScenePath },
                locationPathName = path,
                target = target,
                options = BuildOptions.None,
            };
            BuildReport report = BuildPipeline.BuildPlayer(options);
            Debug.Log(string.Format("Build {0}: {1} -> {2}", target, report.summary.result, path));
            if (report.summary.result != BuildResult.Succeeded && Application.isBatchMode)
                EditorApplication.Exit(1);
        }
    }
}

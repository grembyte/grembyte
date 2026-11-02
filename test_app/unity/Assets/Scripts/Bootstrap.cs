using UnityEngine;

namespace HermesTestRpg
{
    // Starts the game from code, so the scene can stay empty and the project
    // needs no hand-made assets.
    static class Bootstrap
    {
        [RuntimeInitializeOnLoadMethod(RuntimeInitializeLoadType.AfterSceneLoad)]
        static void Start()
        {
            if (GameRunner.Instance != null)
                return;

            // Without a camera clearing the screen, IMGUI draws over old frames.
            Camera camera = Camera.main;
            if (camera == null)
                camera = new GameObject("Camera").AddComponent<Camera>();
            camera.clearFlags = CameraClearFlags.SolidColor;
            camera.backgroundColor = Color.black;
            camera.cullingMask = 0;

            var game = new GameObject("Game");
            Object.DontDestroyOnLoad(game);
            game.AddComponent<GameRunner>();
        }
    }
}

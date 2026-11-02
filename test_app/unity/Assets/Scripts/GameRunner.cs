using System;
using System.Globalization;
using UnityEngine;

namespace HermesTestRpg
{
    // The only MonoBehaviour: real-time mana regeneration in Update, keyboard
    // input and drawing in OnGUI. Input comes from IMGUI events, so the game
    // works with both the old Input Manager and the Input System package.
    public class GameRunner : MonoBehaviour
    {
        public static GameRunner Instance;

        readonly AsciiTerminal terminal = new AsciiTerminal(GameView.Columns, GameView.Rows);
        ViewMode mode = ViewMode.Map;
        int? fixedSeed;

        void Awake()
        {
            Instance = this;
            // The same number format on every system (1.5, not 1,5), as typed into a scan.
            CultureInfo.CurrentCulture = CultureInfo.InvariantCulture;
            Application.runInBackground = true;
            Application.targetFrameRate = 60;
            fixedSeed = ReadSeedArgument();
            StartNewGame();
            Debug.Log(string.Format("Hermes Test RPG, PID {0}",
                                    System.Diagnostics.Process.GetCurrentProcess().Id));
        }

        // "-seed <n>" on the command line gives the same map and rolls on every
        // new game; without it each game gets a new seed.
        static int? ReadSeedArgument()
        {
            string[] args = Environment.GetCommandLineArgs();
            for (int i = 0; i < args.Length - 1; i++)
            {
                int seed;
                if (args[i] == "-seed" && int.TryParse(args[i + 1], out seed))
                    return seed;
            }
            return null;
        }

        void StartNewGame()
        {
            int seed = fixedSeed ?? (Environment.TickCount & int.MaxValue);
            GameState.NewGame(seed);
            mode = ViewMode.Map;
        }

        void Update()
        {
            var game = GameState.Current;
            if (game != null && !game.GameOver)
                game.Player.RegenerateMana(Time.deltaTime);
        }

        void OnGUI()
        {
            Event e = Event.current;
            if (e.type == EventType.KeyDown && e.keyCode != KeyCode.None)
            {
                HandleKey(e.keyCode);
                e.Use();
            }
            else if (e.type == EventType.Repaint)
            {
                GameView.Render(terminal, GameState.Current, mode);
                terminal.Draw();
            }
        }

        void ToggleView(ViewMode view)
        {
            mode = mode == view ? ViewMode.Map : view;
        }

        void HandleKey(KeyCode key)
        {
            var game = GameState.Current;
            switch (key)
            {
                case KeyCode.N: StartNewGame(); return;
                case KeyCode.C: ToggleView(ViewMode.Character); return;
                case KeyCode.F1: ToggleView(ViewMode.Debug); return;
                case KeyCode.H: ToggleView(ViewMode.Help); return;
                case KeyCode.Escape: mode = ViewMode.Map; return;
                case KeyCode.Delete: game.ClearEnemies(); return;
            }

            int spawn = SpawnIndex(key);
            if (spawn >= 0)
            {
                game.SpawnEnemy(EnemyType.All[spawn]);
                return;
            }
            if (key == KeyCode.E)
            {
                game.SpawnEnemy(EnemyType.All[game.Rng.Next(EnemyType.All.Length)]);
                return;
            }
            if (game.GameOver)
                return;

            int dx, dy;
            bool tookTurn;
            if (Direction(key, out dx, out dy))
                tookTurn = game.PlayerMove(dx, dy);
            else if (key == KeyCode.F)
                tookTurn = Combat.CastFireBolt(game, game.Player);
            else if (key == KeyCode.R)
            {
                game.PlayerRest();
                tookTurn = true;
            }
            else
                tookTurn = key == KeyCode.Space;
            if (tookTurn)
                game.EndTurn();
        }

        static int SpawnIndex(KeyCode key)
        {
            if (key >= KeyCode.Alpha1 && key <= KeyCode.Alpha5)
                return key - KeyCode.Alpha1;
            return -1;
        }

        static bool Direction(KeyCode key, out int dx, out int dy)
        {
            dx = 0;
            dy = 0;
            switch (key)
            {
                case KeyCode.UpArrow: case KeyCode.W: case KeyCode.Keypad8: dy = -1; break;
                case KeyCode.DownArrow: case KeyCode.S: case KeyCode.Keypad2: dy = 1; break;
                case KeyCode.LeftArrow: case KeyCode.A: case KeyCode.Keypad4: dx = -1; break;
                case KeyCode.RightArrow: case KeyCode.D: case KeyCode.Keypad6: dx = 1; break;
                case KeyCode.Keypad7: dx = -1; dy = -1; break;
                case KeyCode.Keypad9: dx = 1; dy = -1; break;
                case KeyCode.Keypad1: dx = -1; dy = 1; break;
                case KeyCode.Keypad3: dx = 1; dy = 1; break;
                default: return false;
            }
            return true;
        }
    }
}

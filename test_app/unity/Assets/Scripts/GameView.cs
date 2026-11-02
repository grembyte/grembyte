using System.Diagnostics;
using System.Globalization;
using UnityEngine;

namespace HermesTestRpg
{
    public enum ViewMode
    {
        Map,
        Character,
        Debug,
        Help,
    }

    // Draws a GameState onto the 80 x 30 terminal:
    //     rows 0-21  map (60 columns) or another view, and the side panel
    //     rows 23-28 message log
    //     row  29    key hints
    public static class GameView
    {
        public const int Columns = 80;
        public const int Rows = 30;
        const int PanelX = Map.Width + 1;
        const int LogY = Map.Height + 1;

        static readonly Color Text = new Color(0.85f, 0.85f, 0.85f);
        static readonly Color Dim = new Color(0.5f, 0.5f, 0.5f);
        static readonly Color Title = new Color(1f, 0.8f, 0.3f);
        static readonly Color Bad = new Color(1f, 0.35f, 0.35f);

        public static void Render(AsciiTerminal terminal, GameState game, ViewMode mode)
        {
            terminal.Clear();
            switch (mode)
            {
                case ViewMode.Character: RenderCharacterSheet(terminal, game); break;
                case ViewMode.Debug: RenderDebug(terminal, game); break;
                case ViewMode.Help: RenderHelp(terminal); break;
                default: RenderMap(terminal, game); break;
            }
            // The debug view needs the full width.
            if (mode != ViewMode.Debug)
                RenderPanel(terminal, game);
            RenderLog(terminal, game);
            terminal.Print(0, Rows - 1,
                           "Arrows/WASD move  F bolt  R rest  E/1-5 spawn  C sheet  F1 debug  H help",
                           Dim);
        }

        static string Exact(float value)
        {
            return value.ToString("R", CultureInfo.InvariantCulture);
        }

        static string Exact(double value)
        {
            return value.ToString("R", CultureInfo.InvariantCulture);
        }

        static void RenderMap(AsciiTerminal terminal, GameState game)
        {
            var map = game.Map;
            for (int x = 0; x < Map.Width; x++)
                for (int y = 0; y < Map.Height; y++)
                {
                    char tile = map.Tiles[x, y];
                    terminal.Put(x, y, tile, Map.ColorOf(tile));
                }
            foreach (var enemy in game.Enemies)
                terminal.Put(enemy.X, enemy.Y, enemy.Glyph, enemy.Color);
            var player = game.Player;
            terminal.Put(player.X, player.Y, game.GameOver ? '%' : player.Glyph,
                         game.GameOver ? Bad : player.Color);
        }

        static void RenderPanel(AsciiTerminal terminal, GameState game)
        {
            var player = game.Player;
            var stats = player.Stats;
            int y = 0;
            terminal.Print(PanelX, y++, player.Name + "  Lv " + stats.Level, Title);
            y++;
            Color health = stats.Health < stats.MaxHealth * 0.3f ? Bad : Text;
            terminal.Print(PanelX, y++, string.Format("HP  {0:0.0}/{1:0}", stats.Health, stats.MaxHealth), health);
            terminal.Print(PanelX, y++, string.Format("MP  {0:0.0}/{1:0}", stats.Mana, stats.MaxMana), Text);
            terminal.Print(PanelX, y++, string.Format("XP  {0:0}/{1:0}", stats.Experience, player.ExperienceForNextLevel), Text);
            terminal.Print(PanelX, y++, string.Format("Gold {0}", stats.Gold), Text);
            y++;
            terminal.Print(PanelX, y++, string.Format("STR {0}", stats.Strength), Text);
            terminal.Print(PanelX, y++, string.Format("DEX {0}", stats.Dexterity), Text);
            terminal.Print(PanelX, y++, string.Format("INT {0}", stats.Intelligence), Text);
            y++;
            terminal.Print(PanelX, y++, string.Format("Dmg out  x{0:0.##}", player.DamageOutgoingMultiplier), Text);
            terminal.Print(PanelX, y++, string.Format("Dmg in   x{0:0.##}", player.DamageIncomingMultiplier), Text);
            y++;
            terminal.Print(PanelX, y++, string.Format("Turn {0}", game.Turn), Dim);
            terminal.Print(PanelX, y++, string.Format("Enemies {0}", game.Enemies.Count), Dim);
            terminal.Print(PanelX, y++, string.Format("Kills {0}", game.Kills), Dim);
            terminal.Print(PanelX, y++, string.Format("Seed {0}", game.Seed), Dim);
        }

        static void RenderLog(AsciiTerminal terminal, GameState game)
        {
            for (int x = 0; x < Columns; x++)
                terminal.Put(x, LogY - 1, '-', Dim);
            var lines = game.Log.Lines;
            for (int i = 0; i < lines.Count; i++)
                terminal.Print(0, LogY + i, lines[i], i == lines.Count - 1 ? Text : Dim);
        }

        static void RenderCharacterSheet(AsciiTerminal terminal, GameState game)
        {
            var player = game.Player;
            var stats = player.Stats;
            int y = 0;
            terminal.Print(2, y++, "CHARACTER                                   C / Esc: back", Title);
            y++;
            terminal.Print(2, y++, string.Format("Name          {0}", player.Name), Text);
            terminal.Print(2, y++, string.Format("Level         {0}", stats.Level), Text);
            terminal.Print(2, y++, string.Format("Experience    {0:0.00} / {1:0}", stats.Experience, player.ExperienceForNextLevel), Text);
            y++;
            terminal.Print(2, y++, string.Format("Strength      {0,3}   melee damage +{1:0.##}", stats.Strength, stats.Strength * 0.25f), Text);
            terminal.Print(2, y++, string.Format("Dexterity     {0,3}   hit chance and dodge", stats.Dexterity), Text);
            terminal.Print(2, y++, string.Format("Intelligence  {0,3}   Fire Bolt {1:0.##} damage", stats.Intelligence, Combat.FireBoltDamage(player)), Text);
            y++;
            terminal.Print(2, y++, string.Format("Health        {0:0.00} / {1:0.00}", stats.Health, stats.MaxHealth), Text);
            terminal.Print(2, y++, string.Format("Mana          {0:0.00} / {1:0.00}  (+{2}/s)", stats.Mana, stats.MaxMana, Character.ManaRegenPerSecond), Text);
            terminal.Print(2, y++, string.Format("Gold          {0}", stats.Gold), Text);
            y++;
            terminal.Print(2, y++, string.Format("Weapon        {0}-{1} + STR/4", player.DamageMin, player.DamageMax), Text);
            terminal.Print(2, y++, string.Format("Damage out    x{0}", Exact(player.DamageOutgoingMultiplier)), Text);
            terminal.Print(2, y++, string.Format("Damage in     x{0}", Exact(player.DamageIncomingMultiplier)), Text);
            terminal.Print(2, y++, string.Format("Kills         {0}", game.Kills), Text);
        }

        static void RenderDebug(AsciiTerminal terminal, GameState game)
        {
            var player = game.Player;
            var stats = player.Stats;
            int y = 0;
            terminal.Print(1, y++, "DEBUG  exact values                        F1 / Esc: back", Title);
            terminal.Print(1, y++, string.Format("PID {0}   seed {1}   turn {2}",
                                                 Process.GetCurrentProcess().Id, game.Seed, game.Turn), Text);
            terminal.Print(1, y++, "Chain: GameState.Current -> Player -> Stats", Dim);
            y++;
            terminal.Print(1, y++, string.Format("Health      {0} / {1}", Exact(stats.Health), Exact(stats.MaxHealth)), Text);
            terminal.Print(1, y++, string.Format("Mana        {0} / {1}", Exact(stats.Mana), Exact(stats.MaxMana)), Text);
            terminal.Print(1, y++, string.Format("Experience  {0}", Exact(stats.Experience)), Text);
            terminal.Print(1, y++, string.Format("Gold {0}  STR {1}  DEX {2}  INT {3}  Lv {4}",
                                                 stats.Gold, stats.Strength, stats.Dexterity, stats.Intelligence, stats.Level), Text);
            terminal.Print(1, y++, string.Format("Mult out {0}  in {1}",
                                                 Exact(player.DamageOutgoingMultiplier), Exact(player.DamageIncomingMultiplier)), Text);
            y++;
            var report = game.LastDamage;
            terminal.Print(1, y++, "Last ApplyDamage", Title);
            if (report == null)
                terminal.Print(1, y++, "none yet", Dim);
            else
            {
                terminal.Print(1, y++, string.Format("{0} {1} {2}", report.Attacker, report.Source, report.Target), Text);
                terminal.Print(1, y++, string.Format("base {0}  x out {1}  x in {2}",
                                                     Exact(report.BaseDamage), Exact(report.OutgoingMultiplier), Exact(report.IncomingMultiplier)), Text);
                float expected = report.BaseDamage * report.OutgoingMultiplier * report.IncomingMultiplier;
                bool changed = !Mathf.Approximately(expected, report.Calculated);
                terminal.Print(1, y++, string.Format("calculated {0}  dealt {1}{2}",
                                                     Exact(report.Calculated), Exact(report.Dealt),
                                                     changed ? "  (CalculateDamage changed)" : ""), changed ? Title : Text);
            }
            y++;
            terminal.Print(1, y++, string.Format("Enemies ({0})", game.Enemies.Count), Title);
            foreach (var enemy in game.Enemies)
            {
                if (y >= Map.Height)
                    break;
                terminal.Print(1, y++, string.Format("#{0,-3} {1} {2,-8} HP {3,-8} at {4},{5}  out {6} in {7}",
                                                     enemy.Id, enemy.Glyph, enemy.Name, Exact(enemy.Stats.Health),
                                                     enemy.X, enemy.Y, Exact(enemy.DamageOutgoingMultiplier),
                                                     Exact(enemy.DamageIncomingMultiplier)), Text);
            }
        }

        static void RenderHelp(AsciiTerminal terminal)
        {
            string[] lines =
            {
                "HELP                                        H / Esc: back",
                "",
                "Arrows, WASD, numpad   move; walk into an enemy to attack",
                "Space                  wait a turn",
                "R                      rest a turn and recover 2 health",
                "F                      Fire Bolt at the nearest enemy (7.5 mana)",
                "E                      spawn a random enemy",
                "1 2 3 4 5              spawn rat, goblin, skeleton, orc, ogre",
                "Delete                 remove all enemies",
                "C                      character sheet",
                "F1                     debug view with exact values",
                "N                      new game (moves player and stats",
                "                       to new addresses)",
                "",
                "Mana regenerates in real time, even while the window",
                "is in the background. Start with -seed <n> for a fixed map.",
            };
            for (int i = 0; i < lines.Length; i++)
                terminal.Print(2, i, lines[i], i == 0 ? Title : Text);
        }
    }
}

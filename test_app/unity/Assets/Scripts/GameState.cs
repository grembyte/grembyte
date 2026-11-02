using System;
using System.Collections.Generic;

namespace HermesTestRpg
{
    // One running game. "New game" builds a new GameState, so the player and
    // its stats move to new heap addresses, as after loading a save in a real game.
    // The static field Current is the root of every pointer chain.
    public class GameState
    {
        public static GameState Current;

        public const int ChaseDistance = 10;

        public readonly int Seed;
        public readonly System.Random Rng;
        public readonly Map Map;
        public readonly Character Player;
        public readonly List<Character> Enemies = new List<Character>();
        public readonly MessageLog Log = new MessageLog(6);
        public DamageReport LastDamage;
        public int Turn;
        public int Kills;
        public bool GameOver;
        int nextId = 1;

        GameState(int seed)
        {
            Seed = seed;
            Rng = new System.Random(seed);
            Map = Map.Generate(Rng);
            Player = Character.CreatePlayer(nextId++);
            Player.X = Map.StartX;
            Player.Y = Map.StartY;
        }

        public static GameState NewGame(int seed)
        {
            Current = new GameState(seed);
            Current.Log.Add(string.Format("New game, seed {0}. Press H for help.", seed));
            return Current;
        }

        public Character EnemyAt(int x, int y)
        {
            foreach (var enemy in Enemies)
                if (enemy.X == x && enemy.Y == y && !enemy.IsDead)
                    return enemy;
            return null;
        }

        bool IsFree(int x, int y)
        {
            return Map.IsWalkable(x, y) && EnemyAt(x, y) == null && !(Player.X == x && Player.Y == y);
        }

        public static int Distance(Character a, Character b)
        {
            return Math.Max(Math.Abs(a.X - b.X), Math.Abs(a.Y - b.Y));
        }

        public Character NearestEnemy(Character from, int range)
        {
            Character nearest = null;
            foreach (var enemy in Enemies)
            {
                if (enemy.IsDead || Distance(from, enemy) > range)
                    continue;
                if (nearest == null || Distance(from, enemy) < Distance(from, nearest))
                    nearest = enemy;
            }
            return nearest;
        }

        // Places the enemy 3 to 8 cells away from the player; null if there is no room.
        public Character SpawnEnemy(EnemyType type)
        {
            for (int attempt = 0; attempt < 200; attempt++)
            {
                int x = Player.X + Rng.Next(-8, 9);
                int y = Player.Y + Rng.Next(-8, 9);
                int distance = Math.Max(Math.Abs(x - Player.X), Math.Abs(y - Player.Y));
                if (distance < 3 || !IsFree(x, y))
                    continue;
                var enemy = Character.CreateEnemy(nextId++, type);
                enemy.X = x;
                enemy.Y = y;
                Enemies.Add(enemy);
                Log.Add(string.Format("A {0} appears.", type.Name));
                return enemy;
            }
            Log.Add(string.Format("No room for a {0}.", type.Name));
            return null;
        }

        public void ClearEnemies()
        {
            Enemies.Clear();
            Log.Add("All enemies removed.");
        }

        public void OnKilled(Character killer, Character victim)
        {
            Log.Add(string.Format("{0} dies.", victim.Name));
            if (victim.IsPlayer)
            {
                GameOver = true;
                Log.Add("You are dead. Press N for a new game.");
                return;
            }
            Enemies.Remove(victim);
            if (!killer.IsPlayer)
                return;
            Kills++;
            int gold = Rng.Next(victim.Type.GoldMin, victim.Type.GoldMax + 1);
            killer.AddGold(gold);
            int levels = killer.AddExperience(victim.Type.Experience);
            Log.Add(string.Format("You gain {0:0.##} experience and {1} gold.",
                                  victim.Type.Experience, gold));
            if (levels > 0)
                Log.Add(string.Format("You reach level {0}!", killer.Stats.Level));
        }

        // Moves the player or attacks the enemy in the way; returns whether
        // that took a turn.
        public bool PlayerMove(int dx, int dy)
        {
            int x = Player.X + dx;
            int y = Player.Y + dy;
            Character enemy = EnemyAt(x, y);
            if (enemy != null)
            {
                Combat.MeleeAttack(this, Player, enemy);
                return true;
            }
            if (!Map.IsWalkable(x, y))
                return false;
            Player.X = x;
            Player.Y = y;
            return true;
        }

        public void PlayerRest()
        {
            float healed = Player.Heal(2f);
            if (healed > 0f)
                Log.Add(string.Format("You rest and recover {0:0.##} health.", healed));
        }

        public void EndTurn()
        {
            Turn++;
            // Copy: an enemy may die during the loop and leave the list.
            foreach (var enemy in Enemies.ToArray())
            {
                if (GameOver)
                    return;
                if (enemy.IsDead)
                    continue;
                int distance = Distance(enemy, Player);
                if (distance <= 1)
                    Combat.MeleeAttack(this, enemy, Player);
                else if (distance <= ChaseDistance)
                    StepToward(enemy, Player.X, Player.Y);
                else if (Rng.NextDouble() < 0.25)
                    StepToward(enemy, enemy.X + Rng.Next(-1, 2), enemy.Y + Rng.Next(-1, 2));
            }
        }

        void StepToward(Character enemy, int targetX, int targetY)
        {
            int dx = Math.Sign(targetX - enemy.X);
            int dy = Math.Sign(targetY - enemy.Y);
            if (TryStep(enemy, dx, dy))
                return;
            if (dx != 0 && TryStep(enemy, dx, 0))
                return;
            if (dy != 0)
                TryStep(enemy, 0, dy);
        }

        bool TryStep(Character enemy, int dx, int dy)
        {
            if ((dx == 0 && dy == 0) || !IsFree(enemy.X + dx, enemy.Y + dy))
                return false;
            enemy.X += dx;
            enemy.Y += dy;
            return true;
        }
    }
}

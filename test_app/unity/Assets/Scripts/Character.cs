using System.Runtime.CompilerServices;
using UnityEngine;

namespace HermesQuest
{
    // Player and enemies share this class, as in many games: a hook on
    // TakeDamage hits both and has to tell them apart by IsPlayer.
    public class Character
    {
        public const float ManaRegenPerSecond = 0.13f;

        public readonly int Id;
        public readonly string Name;
        public readonly char Glyph;
        public readonly Color Color;
        public readonly bool IsPlayer;
        public readonly EnemyType Type;  // null for the player
        public readonly CharacterStats Stats = new CharacterStats();
        public int X;
        public int Y;
        public int DamageMin;
        public int DamageMax;

        // Targets for scripts: Combat.CalculateDamage multiplies by both.
        public float DamageOutgoingMultiplier = 1f;
        public float DamageIncomingMultiplier = 1f;

        public Character(int id, string name, char glyph, Color color, bool isPlayer, EnemyType type)
        {
            Id = id;
            Name = name;
            Glyph = glyph;
            Color = color;
            IsPlayer = isPlayer;
            Type = type;
        }

        public bool IsDead
        {
            get { return Stats.Health <= 0f; }
        }

        public static Character CreatePlayer(int id)
        {
            var player = new Character(id, "Hero", '@', new Color(1f, 0.95f, 0.4f), true, null);
            var stats = player.Stats;
            stats.Strength = 10;
            stats.Dexterity = 10;
            stats.Intelligence = 10;
            stats.MaxHealth = 50f;
            stats.Health = stats.MaxHealth;
            stats.MaxMana = 50f;
            stats.Mana = stats.MaxMana;
            player.DamageMin = 2;
            player.DamageMax = 6;
            return player;
        }

        public static Character CreateEnemy(int id, EnemyType type)
        {
            var enemy = new Character(id, type.Name, type.Glyph, type.Color, false, type);
            var stats = enemy.Stats;
            stats.Strength = type.Strength;
            stats.Dexterity = type.Dexterity;
            stats.MaxHealth = type.Health;
            stats.Health = type.Health;
            enemy.DamageMin = type.DamageMin;
            enemy.DamageMax = type.DamageMax;
            return enemy;
        }

        // NoInlining keeps each write to a stat in its own, easy-to-find method
        // that the Mono JIT does not fold into its callers.
        // Returns the health actually lost.
        [MethodImpl(MethodImplOptions.NoInlining)]
        public float TakeDamage(float amount)
        {
            if (amount < 0f)
                amount = 0f;
            float before = Stats.Health;
            Stats.Health = Mathf.Max(0f, before - amount);
            return before - Stats.Health;
        }

        [MethodImpl(MethodImplOptions.NoInlining)]
        public float Heal(float amount)
        {
            float before = Stats.Health;
            Stats.Health = Mathf.Min(Stats.MaxHealth, before + amount);
            return Stats.Health - before;
        }

        [MethodImpl(MethodImplOptions.NoInlining)]
        public void RegenerateMana(float seconds)
        {
            Stats.Mana = Mathf.Min(Stats.MaxMana, Stats.Mana + ManaRegenPerSecond * seconds);
        }

        [MethodImpl(MethodImplOptions.NoInlining)]
        public bool SpendMana(float cost)
        {
            if (Stats.Mana < cost)
                return false;
            Stats.Mana -= cost;
            return true;
        }

        [MethodImpl(MethodImplOptions.NoInlining)]
        public void AddGold(int amount)
        {
            Stats.Gold += amount;
        }

        public double ExperienceForNextLevel
        {
            get { return Stats.Level * 50.0; }
        }

        // Returns the number of levels gained.
        [MethodImpl(MethodImplOptions.NoInlining)]
        public int AddExperience(double amount)
        {
            Stats.Experience += amount;
            int gained = 0;
            while (Stats.Experience >= ExperienceForNextLevel)
            {
                LevelUp();
                gained++;
            }
            return gained;
        }

        void LevelUp()
        {
            Stats.Level++;
            // Raise one attribute per level in turn: Strength, Dexterity, Intelligence.
            switch (Stats.Level % 3)
            {
                case 2: Stats.Strength++; break;
                case 0: Stats.Dexterity++; break;
                default: Stats.Intelligence++; break;
            }
            Stats.MaxHealth += 8f;
            Stats.Health = Stats.MaxHealth;
            Stats.MaxMana += 5f;
        }
    }
}

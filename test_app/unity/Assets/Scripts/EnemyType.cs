using UnityEngine;

namespace HermesTestRpg
{
    // Template for an enemy; Spawn keys 1-5 pick All[0] .. All[4].
    public class EnemyType
    {
        public readonly string Name;
        public readonly char Glyph;
        public readonly Color Color;
        public readonly float Health;
        public readonly int Strength;
        public readonly int Dexterity;
        public readonly int DamageMin;
        public readonly int DamageMax;
        public readonly double Experience;
        public readonly int GoldMin;
        public readonly int GoldMax;

        public EnemyType(string name, char glyph, Color color, float health, int strength,
                         int dexterity, int damageMin, int damageMax, double experience,
                         int goldMin, int goldMax)
        {
            Name = name;
            Glyph = glyph;
            Color = color;
            Health = health;
            Strength = strength;
            Dexterity = dexterity;
            DamageMin = damageMin;
            DamageMax = damageMax;
            Experience = experience;
            GoldMin = goldMin;
            GoldMax = goldMax;
        }

        public static readonly EnemyType[] All =
        {
            new EnemyType("Rat", 'r', new Color(0.75f, 0.6f, 0.45f), 6f, 2, 12, 1, 3, 4.5, 0, 2),
            new EnemyType("Goblin", 'g', new Color(0.4f, 0.85f, 0.3f), 12f, 4, 10, 2, 4, 10.25, 3, 8),
            new EnemyType("Skeleton", 's', new Color(0.9f, 0.9f, 0.85f), 18f, 6, 8, 3, 6, 15.5, 2, 10),
            new EnemyType("Orc", 'o', new Color(0.85f, 0.45f, 0.2f), 28f, 9, 8, 4, 8, 24.75, 8, 20),
            new EnemyType("Ogre", 'O', new Color(0.9f, 0.25f, 0.25f), 60f, 14, 5, 6, 12, 60.0, 20, 45),
        };
    }
}

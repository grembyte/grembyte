namespace HermesQuest
{
    // The values a memory scan looks for. A class rather than a struct, so the
    // stats sit in their own heap object behind a pointer:
    //     GameState.Current -> Player -> Stats -> Health
    // Health and Mana are floats and Experience a double that change by uneven
    // amounts, to test scans with tolerance and rounded values.
    public class CharacterStats
    {
        public int Level = 1;
        public int Strength;
        public int Dexterity;
        public int Intelligence;
        public float MaxHealth;
        public float Health;
        public float MaxMana;
        public float Mana;
        public double Experience;
        public int Gold;
    }
}

using System;
using System.Globalization;
using System.Runtime.CompilerServices;

namespace HermesQuest
{
    // Once per second the town hears a rumor, a line "<days>:<text>". Some
    // lines are garbled: Parse throws for them and Tick catches it, as games
    // catch what their own code throws. A target for hooks on a method an
    // exception leaves: the exception has to find its way back to Tick past
    // the hook (Frida's onEnter and onLeave broke that and ended the game).
    public static class Rumors
    {
        public const float SecondsPerRumor = 1f;

        static readonly string[] Lines =
        {
            "3:An ogre was seen near the old mill.",
            "seven:The well is cursed.",
            "1:The smith pays well for goblin ears.",
            "12:A skeleton guards gold in the crypt.",
            "?:Someone saw lights in the swamp.",
        };

        public static int Heard;
        public static int Garbled;
        public static string Last = "";
        static float elapsed;
        static int next;

        [MethodImpl(MethodImplOptions.NoInlining)]
        public static void Tick(float seconds)
        {
            elapsed += seconds;
            if (elapsed < SecondsPerRumor)
                return;
            elapsed -= SecondsPerRumor;
            string line = Lines[next];
            next = (next + 1) % Lines.Length;
            try
            {
                int days = Parse(line);
                Heard++;
                Last = string.Format("{0} ({1} days ago)", line.Substring(line.IndexOf(':') + 1), days);
            }
            catch (FormatException)
            {
                Garbled++;
            }
        }

        // The days since the rumor began; throws FormatException for a garbled line.
        [MethodImpl(MethodImplOptions.NoInlining)]
        public static int Parse(string line)
        {
            return int.Parse(line.Substring(0, line.IndexOf(':')), CultureInfo.InvariantCulture);
        }
    }
}

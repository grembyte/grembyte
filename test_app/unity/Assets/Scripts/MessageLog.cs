using System.Collections.Generic;
using UnityEngine;

namespace HermesTestRpg
{
    // The last few messages on screen; every message also goes to Player.log.
    public class MessageLog
    {
        readonly int capacity;
        readonly List<string> lines = new List<string>();

        public MessageLog(int capacity)
        {
            this.capacity = capacity;
        }

        public IList<string> Lines
        {
            get { return lines; }
        }

        public void Add(string message)
        {
            Debug.Log(message);
            lines.Add(message);
            if (lines.Count > capacity)
                lines.RemoveAt(0);
        }
    }
}

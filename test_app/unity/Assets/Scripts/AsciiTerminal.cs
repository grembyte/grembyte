using System;
using System.Collections.Generic;
using UnityEngine;

namespace HermesTestRpg
{
    // A grid of character cells drawn with IMGUI, so the game needs no assets.
    // Put places single glyphs, one per cell, so the map lines up without a
    // monospace font. Print places text, which is drawn as whole runs so it
    // reads normally; a gap of two or more spaces starts a new run at its own
    // column, which keeps tables aligned.
    public class AsciiTerminal
    {
        struct TextRun
        {
            public int X;
            public int Y;
            public string Text;
            public Color Color;
        }

        public readonly int Columns;
        public readonly int Rows;
        readonly char[] glyphs;
        readonly Color[] colors;
        readonly List<TextRun> runs = new List<TextRun>();
        static readonly string[] glyphStrings = new string[128];
        GUIStyle glyphStyle;
        GUIStyle textStyle;

        public AsciiTerminal(int columns, int rows)
        {
            Columns = columns;
            Rows = rows;
            glyphs = new char[columns * rows];
            colors = new Color[columns * rows];
            Clear();
        }

        public void Clear()
        {
            for (int i = 0; i < glyphs.Length; i++)
                glyphs[i] = ' ';
            runs.Clear();
        }

        public void Put(int x, int y, char glyph, Color color)
        {
            if (x < 0 || y < 0 || x >= Columns || y >= Rows)
                return;
            glyphs[y * Columns + x] = glyph;
            colors[y * Columns + x] = color;
        }

        public void Print(int x, int y, string text, Color color)
        {
            if (y < 0 || y >= Rows)
                return;
            int start = 0;
            while (start < text.Length)
            {
                while (start < text.Length && text[start] == ' ')
                    start++;
                if (start == text.Length)
                    break;
                int end = text.IndexOf("  ", start, StringComparison.Ordinal);
                if (end < 0)
                    end = text.Length;
                runs.Add(new TextRun
                {
                    X = x + start,
                    Y = y,
                    Text = text.Substring(start, end - start),
                    Color = color,
                });
                start = end;
            }
        }

        static string GlyphString(char glyph)
        {
            if (glyph >= glyphStrings.Length)
                return glyph.ToString();
            return glyphStrings[glyph] ?? (glyphStrings[glyph] = glyph.ToString());
        }

        static GUIStyle CreateStyle(TextAnchor alignment)
        {
            var style = new GUIStyle(GUI.skin.label);
            style.alignment = alignment;
            style.padding = new RectOffset(0, 0, 0, 0);
            style.margin = new RectOffset(0, 0, 0, 0);
            style.clipping = TextClipping.Overflow;
            style.wordWrap = false;
            return style;
        }

        // Call from OnGUI during the repaint event.
        public void Draw()
        {
            float cellWidth = Screen.width / (float)Columns;
            float cellHeight = Screen.height / (float)Rows;
            int fontSize = Mathf.Max(6, Mathf.RoundToInt(Mathf.Min(cellHeight * 0.8f, cellWidth * 1.4f)));
            if (glyphStyle == null)
            {
                glyphStyle = CreateStyle(TextAnchor.MiddleCenter);
                textStyle = CreateStyle(TextAnchor.MiddleLeft);
            }
            glyphStyle.fontSize = fontSize;
            textStyle.fontSize = fontSize;

            for (int y = 0; y < Rows; y++)
                for (int x = 0; x < Columns; x++)
                {
                    char glyph = glyphs[y * Columns + x];
                    if (glyph == ' ')
                        continue;
                    glyphStyle.normal.textColor = colors[y * Columns + x];
                    var cell = new Rect(x * cellWidth, y * cellHeight, cellWidth, cellHeight);
                    GUI.Label(cell, GlyphString(glyph), glyphStyle);
                }

            foreach (var run in runs)
            {
                textStyle.normal.textColor = run.Color;
                var line = new Rect(run.X * cellWidth, run.Y * cellHeight,
                                    (Columns - run.X) * cellWidth, cellHeight);
                GUI.Label(line, run.Text, textStyle);
            }
        }
    }
}

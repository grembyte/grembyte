using System;
using System.Collections.Generic;
using UnityEngine;

namespace HermesQuest
{
    public class Map
    {
        public const int Width = 60;
        public const int Height = 22;

        public const char Wall = '#';
        public const char Floor = '.';
        public const char Grass = ',';
        public const char Tree = 'T';
        public const char Water = '~';

        public readonly char[,] Tiles = new char[Width, Height];
        public int StartX;
        public int StartY;

        public bool InBounds(int x, int y)
        {
            return x >= 0 && y >= 0 && x < Width && y < Height;
        }

        public bool IsWalkable(int x, int y)
        {
            if (!InBounds(x, y))
                return false;
            char tile = Tiles[x, y];
            return tile == Floor || tile == Grass;
        }

        public static Color ColorOf(char tile)
        {
            switch (tile)
            {
                case Wall: return new Color(0.6f, 0.6f, 0.6f);
                case Floor: return new Color(0.35f, 0.35f, 0.35f);
                case Grass: return new Color(0.2f, 0.55f, 0.2f);
                case Tree: return new Color(0.1f, 0.7f, 0.25f);
                case Water: return new Color(0.25f, 0.45f, 0.95f);
                default: return Color.white;
            }
        }

        // Open land with a few ruins, lakes and trees, framed by a wall.
        // The same seed gives the same map.
        public static Map Generate(System.Random rng)
        {
            var map = new Map();
            for (int x = 0; x < Width; x++)
                for (int y = 0; y < Height; y++)
                    map.Tiles[x, y] = rng.NextDouble() < 0.25 ? Grass : Floor;

            int lakes = rng.Next(1, 3);
            for (int i = 0; i < lakes; i++)
                map.AddLake(rng);
            int ruins = rng.Next(3, 6);
            for (int i = 0; i < ruins; i++)
                map.AddRuin(rng);
            for (int x = 0; x < Width; x++)
                for (int y = 0; y < Height; y++)
                    if (map.IsWalkable(x, y) && rng.NextDouble() < 0.06)
                        map.Tiles[x, y] = Tree;

            for (int x = 0; x < Width; x++)
            {
                map.Tiles[x, 0] = Wall;
                map.Tiles[x, Height - 1] = Wall;
            }
            for (int y = 0; y < Height; y++)
            {
                map.Tiles[0, y] = Wall;
                map.Tiles[Width - 1, y] = Wall;
            }

            map.FindWalkableNear(Width / 2, Height / 2, out map.StartX, out map.StartY);
            map.ConnectAll();
            return map;
        }

        // Ruins, trees and lakes can close off an area, and the player or an
        // enemy spawned there could not get out. Breaks a straight way from
        // each such area to the part reachable from the start.
        void ConnectAll()
        {
            while (true)
            {
                bool[,] reached = Reachable(StartX, StartY);
                int x, y;
                if (!FindUnreached(reached, out x, out y))
                    return;
                // Horizontally first, then vertically, until the reached part.
                while (!reached[x, y])
                {
                    if (x != StartX)
                        x += Math.Sign(StartX - x);
                    else
                        y += Math.Sign(StartY - y);
                    if (!IsWalkable(x, y))
                        Tiles[x, y] = Floor;
                }
            }
        }

        // Flood fill with the four straight steps, as the arrow keys move.
        bool[,] Reachable(int startX, int startY)
        {
            var reached = new bool[Width, Height];
            var open = new Stack<int>();
            reached[startX, startY] = true;
            open.Push(startY * Width + startX);
            while (open.Count > 0)
            {
                int cell = open.Pop();
                int cx = cell % Width;
                int cy = cell / Width;
                for (int i = 0; i < 4; i++)
                {
                    int nx = cx + (i == 0 ? 1 : i == 1 ? -1 : 0);
                    int ny = cy + (i == 2 ? 1 : i == 3 ? -1 : 0);
                    if (IsWalkable(nx, ny) && !reached[nx, ny])
                    {
                        reached[nx, ny] = true;
                        open.Push(ny * Width + nx);
                    }
                }
            }
            return reached;
        }

        bool FindUnreached(bool[,] reached, out int foundX, out int foundY)
        {
            for (int x = 0; x < Width; x++)
                for (int y = 0; y < Height; y++)
                    if (IsWalkable(x, y) && !reached[x, y])
                    {
                        foundX = x;
                        foundY = y;
                        return true;
                    }
            foundX = 0;
            foundY = 0;
            return false;
        }

        void AddLake(System.Random rng)
        {
            int cx = rng.Next(8, Width - 8);
            int cy = rng.Next(4, Height - 4);
            int rx = rng.Next(3, 7);
            int ry = rng.Next(2, 4);
            for (int x = cx - rx; x <= cx + rx; x++)
                for (int y = cy - ry; y <= cy + ry; y++)
                {
                    float dx = (x - cx) / (float)rx;
                    float dy = (y - cy) / (float)ry;
                    if (InBounds(x, y) && dx * dx + dy * dy <= 1f)
                        Tiles[x, y] = Water;
                }
        }

        // The outline of a building with one gap as door.
        void AddRuin(System.Random rng)
        {
            int w = rng.Next(5, 11);
            int h = rng.Next(4, 7);
            int left = rng.Next(2, Width - w - 2);
            int top = rng.Next(2, Height - h - 2);
            int right = left + w - 1;
            int bottom = top + h - 1;
            for (int x = left; x <= right; x++)
                for (int y = top; y <= bottom; y++)
                {
                    bool edge = x == left || x == right || y == top || y == bottom;
                    Tiles[x, y] = edge ? Wall : Floor;
                }
            int door = rng.Next(1, w - 1);
            Tiles[left + door, rng.Next(2) == 0 ? top : bottom] = Floor;
        }

        // The walkable cell nearest to (x, y), searched in growing squares.
        public bool FindWalkableNear(int x, int y, out int foundX, out int foundY)
        {
            for (int radius = 0; radius < Math.Max(Width, Height); radius++)
                for (int dx = -radius; dx <= radius; dx++)
                    for (int dy = -radius; dy <= radius; dy++)
                    {
                        if (Math.Max(Math.Abs(dx), Math.Abs(dy)) != radius)
                            continue;
                        if (IsWalkable(x + dx, y + dy))
                        {
                            foundX = x + dx;
                            foundY = y + dy;
                            return true;
                        }
                    }
            foundX = x;
            foundY = y;
            return false;
        }
    }
}

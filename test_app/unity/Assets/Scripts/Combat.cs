using System.Runtime.CompilerServices;
using UnityEngine;

namespace HermesQuest
{
    // What the last call of ApplyDamage did; shown in the log and the debug view.
    // The multipliers are read from the characters, Calculated is what
    // CalculateDamage returned. A hook on CalculateDamage shows up as
    // Calculated != BaseDamage * Outgoing * Incoming.
    public class DamageReport
    {
        public string Attacker;
        public string Target;
        public string Source;
        public float BaseDamage;
        public float OutgoingMultiplier;
        public float IncomingMultiplier;
        public float Calculated;
        public float Dealt;
    }

    // Every damage goes through one pipeline, so a single hook covers
    // melee and spells:
    //     MeleeAttack / CastFireBolt -> ApplyDamage -> CalculateDamage
    //                                               -> Character.TakeDamage
    public static class Combat
    {
        public const float FireBoltCost = 7.5f;
        public const int FireBoltRange = 8;

        [MethodImpl(MethodImplOptions.NoInlining)]
        public static float CalculateDamage(Character attacker, Character target, float baseDamage)
        {
            return baseDamage * attacker.DamageOutgoingMultiplier * target.DamageIncomingMultiplier;
        }

        // Returns the health the target actually lost.
        [MethodImpl(MethodImplOptions.NoInlining)]
        public static float ApplyDamage(GameState game, Character attacker, Character target,
                                        float baseDamage, string source)
        {
            float calculated = CalculateDamage(attacker, target, baseDamage);
            float dealt = target.TakeDamage(calculated);

            var report = new DamageReport();
            report.Attacker = attacker.Name;
            report.Target = target.Name;
            report.Source = source;
            report.BaseDamage = baseDamage;
            report.OutgoingMultiplier = attacker.DamageOutgoingMultiplier;
            report.IncomingMultiplier = target.DamageIncomingMultiplier;
            report.Calculated = calculated;
            report.Dealt = dealt;
            game.LastDamage = report;

            game.Log.Add(string.Format("{0} {1} {2}: {3:0.##} (base {4:0.##} x{5:0.##} x{6:0.##})",
                                       attacker.Name, source, target.Name, dealt, baseDamage,
                                       report.OutgoingMultiplier, report.IncomingMultiplier));
            if (target.IsDead)
                game.OnKilled(attacker, target);
            return dealt;
        }

        public static float HitChance(Character attacker, Character target)
        {
            return Mathf.Clamp(0.7f + (attacker.Stats.Dexterity - target.Stats.Dexterity) * 0.02f,
                               0.2f, 0.95f);
        }

        [MethodImpl(MethodImplOptions.NoInlining)]
        public static float RollMeleeDamage(GameState game, Character attacker)
        {
            return game.Rng.Next(attacker.DamageMin, attacker.DamageMax + 1)
                   + attacker.Stats.Strength * 0.25f;
        }

        public static void MeleeAttack(GameState game, Character attacker, Character target)
        {
            if (game.Rng.NextDouble() >= HitChance(attacker, target))
            {
                game.Log.Add(string.Format("{0} misses {1}.", attacker.Name, target.Name));
                return;
            }
            ApplyDamage(game, attacker, target, RollMeleeDamage(game, attacker), "hits");
        }

        public static float FireBoltDamage(Character caster)
        {
            return 6f + caster.Stats.Intelligence * 0.8f;
        }

        // Hits the nearest enemy in range; returns false if nothing was cast.
        public static bool CastFireBolt(GameState game, Character caster)
        {
            Character target = game.NearestEnemy(caster, FireBoltRange);
            if (target == null)
            {
                game.Log.Add("No enemy in range for Fire Bolt.");
                return false;
            }
            if (!caster.SpendMana(FireBoltCost))
            {
                game.Log.Add(string.Format("Not enough mana ({0:0.0} of {1:0.0}).",
                                           caster.Stats.Mana, FireBoltCost));
                return false;
            }
            ApplyDamage(game, caster, target, FireBoltDamage(caster), "burns");
            return true;
        }
    }
}

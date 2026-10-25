"""Filters that pick the methods worth tracing from a game's catalog.

Tracing every method of a game is slow and, under Wine, unreliable: tens of
thousands of methods must be compiled and hooked. A filter picks a small set
from the catalog instead, deterministically, before anything is compiled:

``words``
    Any of them must match one of the name parts of the class or the method.
    Names are split at case changes, underscores, dots and digits (``HPBar``
    is ``hp`` + ``bar``). A word of up to four letters matches a part that is
    the word or its plural (``item``: ``Item``, ``Items``; not ``Itemize``),
    a longer word also the start of a part (``inventory``: ``InventorySlot``).
    ``gold`` matches ``AddGold`` and ``gold_amount`` but not ``Marigold``.
``exclude``
    None of them may match a name part (same rule).
``numbers``
    A parameter or the result must be a number (``int``, ``single``, ...).
``changes``
    The method must look like it changes a value: a property setter
    (``set_Gold``) or a name starting with a verb like ``Add``, ``Set`` or
    ``Spend``.

Several filters combine with OR. Compiler-generated code (lambdas, closures,
iterators: names with ``<``) is left out unless asked for: it is rarely what
one looks for and the source of most noise.
"""

import re
from dataclasses import dataclass, field, replace
from functools import lru_cache

_NUMBERS = frozenset({
    "int", "uint", "long", "ulong", "short", "ushort", "byte", "sbyte",
    "single", "double", "float", "decimal",
})

CHANGE_VERBS = (
    "add", "remove", "set", "change", "modify", "increase", "decrease", "apply",
    "spend", "gain", "take", "give", "heal", "damage", "use", "consume", "reduce",
    "restore", "earn", "lose", "pay", "buy", "sell", "grant", "reward", "deduct",
    "subtract", "update", "inc", "dec",
)

# Camel case and acronyms: "HPBar" -> HP, Bar; "addGold2" -> add, Gold, 2
_PARTS = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")


def name_parts(text):
    """The lower-case parts of an identifier or a dotted name."""
    return list(_cached_parts(text))


# The names of a game repeat (classes have many methods, methods share names):
# what is known of a name or a part is worked out once. Filtering a catalog of
# 170,000 methods took seconds per filter without it.
@lru_cache(maxsize=None)
def _cached_parts(text):
    return tuple(part.lower() for part in _PARTS.findall(text))


@lru_cache(maxsize=None)
def _method_parts(class_name, name):
    return _cached_parts(class_name) + _cached_parts(name)


def _parse_words(text):
    """Words of a user's filter: separated by spaces or commas, lower case."""
    return tuple(dict.fromkeys(word.lower() for word in re.split(r"[\s,;]+", text) if word))


def is_generated(method):
    """Whether the compiler made the method (lambda, closure, iterator, ...)."""
    return "<" in method.class_name or "<" in method.name


def has_number(method):
    """Whether a parameter or the result is a number."""
    return _has_number(method.parameters, method.return_type)


@lru_cache(maxsize=None)
def _has_number(parameters, return_type):
    types = re.split(r"[(),&\s]+", f"{parameters} {return_type}")
    return any(name in _NUMBERS for name in types)


def looks_like_change(method):
    """Whether the method's name says it changes a value."""
    return _looks_like_change(method.name)


@lru_cache(maxsize=None)
def _looks_like_change(name):
    if name.startswith("set_"):
        return True
    parts = _cached_parts(name)
    return bool(parts) and parts[0] in CHANGE_VERBS


SHORT_WORD = 4


def word_matches(word, part):
    """Whether a filter word matches one name part (see the module documentation)."""
    if part in (word, word + "s", word + "es"):
        return True
    return len(word) > SHORT_WORD and part.startswith(word)


@lru_cache(maxsize=None)
def _matches_part(words, part):
    return any(word_matches(word, part) for word in words)


def _matches_a_part(words, parts):
    return any(_matches_part(words, part) for part in parts)


@dataclass(frozen=True)
class TraceFilter:
    """One filter; see the module documentation."""

    name: str
    words: tuple = ()
    exclude: tuple = ()
    numbers: bool = False
    changes: bool = False
    # set for the filters hermes ships; they cannot be changed, only copied
    builtin: str = ""
    # database id of a user's filter
    filter_id: int = None
    description: str = field(default="", compare=False)

    def matches(self, method):
        """Whether ``method`` (a CatalogMethod) passes the filter."""
        parts = _method_parts(method.class_name, method.name)
        if self.words and not _matches_a_part(self.words, parts):
            return False
        if self.exclude and _matches_a_part(self.exclude, parts):
            return False
        if self.numbers and not has_number(method):
            return False
        if self.changes and not looks_like_change(method):
            return False
        return bool(self.words or self.numbers or self.changes)

    def to_json(self):
        """What the database keeps of a user's filter."""
        return {"words": list(self.words), "exclude": list(self.exclude),
                "numbers": self.numbers, "changes": self.changes}

    @classmethod
    def from_json(cls, name, data, filter_id=None, description=""):
        """A user's filter as the database keeps it."""
        return cls(name, tuple(data.get("words", ())), tuple(data.get("exclude", ())),
                   bool(data.get("numbers")), bool(data.get("changes")),
                   filter_id=filter_id, description=description)

    @classmethod
    def from_text(cls, name, words, exclude="", numbers=False, changes=False, **kwargs):
        """A filter from what a user typed (words separated by spaces or commas)."""
        return cls(name, _parse_words(words), _parse_words(exclude), numbers, changes,
                   **kwargs)

    def copy_as(self, name):
        """An editable copy (e.g. of a built-in filter)."""
        return replace(self, name=name, builtin="", filter_id=None)


# Platform and store code (achievements, leaderboards, cloud saves) uses
# the same words as game logic; the shipped filters leave it out.
PLATFORM_WORDS = ("sdk", "steam", "leaderboard", "galaxy", "achievement")

# Shipped filters; the names are translation keys ("trace.filter_<builtin>").
BUILTIN_FILTERS = (
    TraceFilter("money", ("gold", "money", "coin", "currency", "cash", "wallet", "price",
                          "cost", "wealth", "funds", "buy", "sell", "shop", "merchant"),
                PLATFORM_WORDS, builtin="money"),
    TraceFilter("inventory", ("inventory", "item", "slot", "stack", "pickup", "loot", "equip",
                              "bag", "stash", "quantity", "amount"),
                PLATFORM_WORDS, builtin="inventory"),
    TraceFilter("stats", ("health", "hp", "mana", "mp", "stamina", "endurance", "stat",
                          "attribute", "level", "xp", "experience", "skill", "ability",
                          "strength", "might", "resolve", "perception", "dexterity",
                          "intellect", "constitution", "luck"),
                PLATFORM_WORDS, builtin="stats"),
    TraceFilter("combat", ("damage", "attack", "hit", "heal", "armor", "armour", "defense",
                           "defence", "crit", "weapon", "kill", "death", "die", "wound",
                           "hurt", "resist"), PLATFORM_WORDS, builtin="combat"),
    TraceFilter("progress", ("quest", "variable", "flag", "progress", "objective", "journal",
                             "reputation"), PLATFORM_WORDS, builtin="progress"),
    TraceFilter("changes", exclude=PLATFORM_WORDS, numbers=True, changes=True,
                builtin="changes"),
)


def select_methods(methods, filters, words=(), generated=False):
    """The methods passing any of ``filters`` and all ``words``.

    Without filters every method passes them; ``words`` (lower case, as in
    the free-text filter) must all occur in ``Class:Method assembly``.
    Compiler-generated methods are left out unless ``generated``.
    """
    chosen = []
    for method in methods:
        if not generated and is_generated(method):
            continue
        if filters and not any(item.matches(method) for item in filters):
            continue
        if words:
            text = f"{method.class_name}:{method.name} {method.assembly}".lower()
            if not all(word in text for word in words):
                continue
        chosen.append(method)
    return chosen

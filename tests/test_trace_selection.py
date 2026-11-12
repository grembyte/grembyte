"""Which catalog methods a Mono trace hooks (see select_methods)."""

from grembyte.features.catalog import CatalogMethod
from grembyte.features.trace import (
    IMPL_INTERNAL_CALL,
    METHOD_ABSTRACT,
    METHOD_PINVOKE,
    select_methods,
    untraceable,
)

_STATIC = 0x10
# The catalog's ``classes`` answer with the flags, as the agent sends it
_CLASSES = [
    ["Game.Loot", [
        ["Take", "(int&,int)", "System.Void", True, 0x06000002, "Game.Loot:Take (int&,int)",
         _STATIC, 0],
        ["Roll", "()", "System.Int32", False, 0x06000003, "Game.Loot:Roll ()",
         METHOD_ABSTRACT, 0],
    ]],
    ["Game.Native", [
        ["Beep", "()", "System.Void", True, 0x06000004, "Game.Native:Beep ()",
         _STATIC | METHOD_PINVOKE, 0],
        ["Now", "()", "System.Single", True, 0x06000005, "Game.Native:Now ()",
         _STATIC, IMPL_INTERNAL_CALL],
    ]],
    ["R3.ReactiveProperty<T>", [
        ["set_Value", "(T)", "System.Void", False, 0x0600000e,
         "R3.ReactiveProperty`1:set_Value (T)", 0, 0],
    ]],
]


def test_only_methods_with_code_of_their_own_are_traced():
    methods, skipped = select_methods({"Assembly-CSharp": _CLASSES}, [])

    assert methods == [CatalogMethod("Assembly-CSharp", "Game.Loot", "Take", "(int&,int)",
                                     "System.Void", True, 0x06000002,
                                     "Game.Loot:Take (int&,int)")]
    assert skipped == {"abstract": 1, "native": 2, "generic": 1}


def test_words_choose_by_class_method_and_assembly():
    def chosen(*words):
        methods, skipped = select_methods({"Assembly-CSharp": _CLASSES}, words)
        return [method.name for method in methods], sum(skipped.values())

    assert chosen("LOOT", "take") == (["Take"], 0)
    # Methods that cannot be traced count only when they match too.
    assert chosen("loot") == (["Take"], 1)
    assert chosen("native") == ([], 2)
    assert chosen("csharp") == (["Take"], 4)
    assert chosen("nothing") == ([], 0)


def test_generic_methods_of_a_generic_definition_are_untraceable():
    assert untraceable("Ns.List<T>", "Add", "Ns.List`1:Add (T)", 0, 0) == "generic"
    # Method<T>: Mono names it without its type parameters in the signature.
    assert untraceable("Ns.Util", "Find", "Ns.Util:Find<T> (T)", 0, 0) == "generic"
    assert untraceable("Ns.Util", "Find", "Ns.Util:Find (int)", 0, 0) is None

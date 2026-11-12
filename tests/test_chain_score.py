"""Scores of pointer chains (grembyte/core/chain_score.py)."""

from grembyte.core import memory_regions
from grembyte.core.chain_score import ranked, score
from grembyte.core.pointer import PointerChain


def test_short_chains_with_small_offsets_from_the_game_score_best():
    small = PointerChain("Game.exe", 0x123456, (0x20, 0x18))
    large = PointerChain("Game.exe", 0x123456, (0xF80, 0xE40))

    assert score(small, memory_regions.MAIN).total == 40 + 10 + 10 + 10
    assert score(large, memory_regions.MAIN).total == 40 + 10 + 0 + 10
    assert score(small, memory_regions.SYSTEM).total == 0 + 10 + 10 + 10
    assert score(PointerChain("Game.exe", 0x10, (0x300,)), memory_regions.GAME).parts == (
        ("start", 30, "game"), ("length", 20, 1), ("offsets", 5, 0x400), ("aligned", 10, None))


def test_length_points_fall_with_every_step():
    totals = [score(PointerChain("m", 0, (0x8,) * steps), None).total for steps in range(1, 8)]

    assert totals == [t + 20 for t in (20, 10, 0, -5, -10, -15, -15)]


def test_a_pointer_into_the_middle_of_an_object_is_not_aligned():
    # the offset after a pointer tells where in the object the next pointer is
    inner = PointerChain("m", 0, (0x14, 0x8))
    last = PointerChain("m", 0, (0x10, 0x4))  # the last offset leads to the value itself

    assert "aligned" not in [part[0] for part in score(inner, None).parts]
    assert "aligned" in [part[0] for part in score(last, None).parts]
    # 32-bit programs align to 4 bytes
    assert "aligned" in [part[0] for part in score(inner, None, pointer_size=4).parts]


def test_ranked_puts_the_best_first_and_keeps_ties_in_order():
    system = PointerChain("libc.so.6", 0x10, (0x8,))
    first = PointerChain("game", 0x10, (0x8, 0x10))
    second = PointerChain("game", 0x18, (0x8, 0x10))
    unknown = PointerChain("gone.dll", 0x10, (0x8,))
    categories = {"game": memory_regions.MAIN, "libc.so.6": memory_regions.SYSTEM}

    order = [chain for chain, _score in ranked([system, first, unknown, second], categories)]

    assert order == [first, second, system, unknown]

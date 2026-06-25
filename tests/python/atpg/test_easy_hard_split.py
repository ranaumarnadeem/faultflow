"""Tests for the easy/hard parallel worker split (atpg.easy_fault_reserve).

Verifies:
  - interleave_easy_hard degrades to identity when workers < 4 or reserve <= 0
  - interleave_easy_hard covers every item exactly once across all inputs
  - each chunk of workers items has easy_reserve items from the front and
    workers-easy_reserve from the back of the sorted list
  - config field easy_fault_reserve loads from config.ofs and defaults to 2
  - session.set_option accepts valid positive integers and rejects non-int/zero
"""

from __future__ import annotations

from pathlib import Path

import pytest

from faultflow.config import interleave_easy_hard

# ---------------------------------------------------------------------------
# interleave_easy_hard — degenerate / passthrough cases
# ---------------------------------------------------------------------------


def test_empty_list_returns_empty() -> None:
    assert interleave_easy_hard([], 10, 2) == []


def test_workers_below_4_returns_original_order() -> None:
    items = list(range(10))
    for w in (1, 2, 3):
        assert interleave_easy_hard(items, w, 2) == items


def test_easy_reserve_zero_returns_original_order() -> None:
    items = list(range(10))
    assert interleave_easy_hard(items, 8, 0) == items


def test_easy_reserve_equals_workers_returns_original_order() -> None:
    items = list(range(10))
    assert interleave_easy_hard(items, 4, 4) == items


def test_easy_reserve_greater_than_workers_returns_original_order() -> None:
    items = list(range(10))
    assert interleave_easy_hard(items, 4, 5) == items


# ---------------------------------------------------------------------------
# interleave_easy_hard — correctness: every item appears exactly once
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "n,workers,easy_reserve",
    [
        (1, 4, 2),
        (3, 4, 2),
        (4, 4, 2),
        (5, 4, 2),
        (10, 5, 2),
        (15, 10, 2),
        (20, 10, 3),
        (100, 8, 2),
    ],
)
def test_all_items_present_exactly_once(
    n: int, workers: int, easy_reserve: int
) -> None:
    items = list(range(n))
    result = interleave_easy_hard(items, workers, easy_reserve)
    assert sorted(result) == items, "missing or duplicate items"


# ---------------------------------------------------------------------------
# interleave_easy_hard — structure: first chunk has easy from front, hard from back
# ---------------------------------------------------------------------------


def test_first_chunk_has_correct_easy_and_hard_items() -> None:
    # 20 items sorted easy→hard; workers=5, easy_reserve=2
    # First chunk: items[0,1] (easy front) + items[19,18,17] (hard back)
    items = list(range(20))
    result = interleave_easy_hard(items, 5, 2)
    first_chunk = set(result[:5])
    assert 0 in first_chunk and 1 in first_chunk, "easy items missing from first chunk"
    assert (
        19 in first_chunk and 18 in first_chunk and 17 in first_chunk
    ), "hard items missing from first chunk"


def test_easy_items_come_before_hard_within_chunk() -> None:
    # Easy items (indices 0,1) should appear before hard items in the chunk
    items = list(range(20))
    result = interleave_easy_hard(items, 5, 2)
    easy_positions = [result.index(i) for i in (0, 1)]
    hard_positions = [result.index(i) for i in (19, 18, 17)]
    assert max(easy_positions) < min(hard_positions)


def test_second_chunk_advances_both_cursors() -> None:
    # 20 items, workers=4, easy_reserve=2 → chunks of [front, front+1, back, back-1]
    # chunk 0: items[0,1,19,18], chunk 1: items[2,3,17,16], ...
    items = list(range(20))
    result = interleave_easy_hard(items, 4, 2)
    assert result[0] == 0 and result[1] == 1
    assert result[2] == 19 and result[3] == 18
    assert result[4] == 2 and result[5] == 3
    assert result[6] == 17 and result[7] == 16


def test_small_list_shorter_than_one_chunk() -> None:
    # Only 3 items, workers=5, easy_reserve=2; should get all 3 without crash
    items = [10, 20, 30]
    result = interleave_easy_hard(items, 5, 2)
    assert sorted(result) == items


# ---------------------------------------------------------------------------
# AtpgConfig.easy_fault_reserve default + load_config
# ---------------------------------------------------------------------------


def test_atpg_config_easy_fault_reserve_default() -> None:
    from faultflow.config import AtpgConfig

    assert AtpgConfig().easy_fault_reserve == 2


def test_load_config_easy_fault_reserve(tmp_path: Path) -> None:
    from faultflow.config import load_config

    cfg_text = """
[design]
netlist   = tests/benchmarks/iscas85/synth_sky130/c17.json
top       = c17
cell_lib  = cells/sky130/sky130_fd_sc_hd.json
yosys_ver = 0.61

[fault_model]
model = stuck_at

[simulation]
unsupported_cells = fail

[atpg]
tool                = native
mode                = comb
easy_fault_reserve  = 0

[report]
threshold = 95.0
"""
    f = tmp_path / "t.ofs"
    f.write_text(cfg_text, encoding="utf-8")
    cfg = load_config(f, "c17")
    assert cfg.atpg.easy_fault_reserve == 0


# ---------------------------------------------------------------------------
# session.set_option for atpg.easy_fault_reserve
# ---------------------------------------------------------------------------


def test_set_option_easy_fault_reserve_valid() -> None:
    from faultflow.shell.session import ProjectSession

    session = ProjectSession()
    # 0 = disable split; positive integers = reserve N slots for easy faults
    for val in ("0", "1", "2", "10"):
        session.set_option("atpg.easy_fault_reserve", val)
        assert session.options["atpg.easy_fault_reserve"] == val


def test_set_option_easy_fault_reserve_invalid_negative() -> None:
    from faultflow.shell.errors import ShellError
    from faultflow.shell.session import ProjectSession

    session = ProjectSession()
    with pytest.raises(ShellError):
        session.set_option("atpg.easy_fault_reserve", "-1")


def test_set_option_easy_fault_reserve_invalid_string() -> None:
    from faultflow.shell.errors import ShellError
    from faultflow.shell.session import ProjectSession

    session = ProjectSession()
    with pytest.raises(ShellError):
        session.set_option("atpg.easy_fault_reserve", "two")

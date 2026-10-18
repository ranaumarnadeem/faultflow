"""The Liberty reader FaultFlow derives cell maps with (faultflow.pdk.liberty) and the
Liberty boolean expressions in cells' functions (faultflow.pdk.boolexpr). Python only:
the C++ core never reads Liberty."""

from __future__ import annotations

import itertools

import pytest

from faultflow.pdk.boolexpr import parse_expr, truth_table
from faultflow.pdk.liberty import LibertyError, parse_liberty

pytestmark = pytest.mark.unit

TEXT = r"""
/* a comment */
library ("demo_lib") {
    delay_model : "table_lookup";
    // another comment
    time_unit : "1ns";
    cell ("demo__nand2_1") {
        area : 3.75;
        pg_pin ("VPWR") { pg_type : "primary_power"; }
        pin ("A") { direction : "input"; capacitance : 0.002; }
        pin ("B") { direction : input; }
        pin ("Y") {
            direction : "output";
            function : "(!A) | (!B)";
            timing () {
                related_pin : "A";
                cell_rise ("del_1_7_7") {
                    index_1 ("0.01, 0.02");
                    values ("0.1, 0.2", \
                            "0.3, 0.4");
                }
            }
        }
    }
    cell ("demo__dff_1") {
        ff ("IQ","IQ_N") {
            clocked_on : "CLK";
            next_state : "D";
        }
        pin ("Q") { direction : "output"; function : "IQ"; }
    }
}
"""


def test_groups_attributes_and_arguments():
    lib = parse_liberty(TEXT)
    assert (lib.name, lib.args) == ("library", ["demo_lib"])
    assert lib.attrs["delay_model"] == "table_lookup"
    cells = lib.all("cell")
    assert [c.args[0] for c in cells] == ["demo__nand2_1", "demo__dff_1"]
    nand = cells[0]
    assert nand.attrs["area"] == "3.75"
    pins = {p.args[0]: p for p in nand.all("pin")}
    assert set(pins) == {"A", "B", "Y"}
    assert pins["B"].attrs["direction"] == "input"  # unquoted values too
    assert pins["Y"].attrs["function"] == "(!A) | (!B)"
    assert [g.args[0] for g in nand.all("pg_pin")] == ["VPWR"]
    # A complex attribute (values ("..."); continued with a backslash) parses.
    rise = pins["Y"].find("timing").find("cell_rise")
    assert rise is not None and rise.args == ["del_1_7_7"]
    ff = cells[1].find("ff")
    assert ff is not None and ff.args == ["IQ", "IQ_N"]
    assert ff.attrs["next_state"] == "D"
    assert cells[1].find("latch") is None


def test_an_unbalanced_file_is_refused():
    with pytest.raises(LibertyError):
        parse_liberty('library ("x") { cell ("c") { pin ("A") { direction : input; }')


@pytest.mark.parametrize(
    ("expr", "fn"),
    [
        ("!A", lambda a, b, c: not a),
        ("A'", lambda a, b, c: not a),
        ("A&B", lambda a, b, c: a and b),
        ("A B", lambda a, b, c: a and b),
        ("A*B", lambda a, b, c: a and b),
        ("A|B", lambda a, b, c: a or b),
        ("A+B", lambda a, b, c: a or b),
        ("A^B", lambda a, b, c: a != b),
        ("A|B&C", lambda a, b, c: a or (b and c)),
        ("!(A|B)&C", lambda a, b, c: (not (a or b)) and c),
        ("(A&!B) | (C&B)", lambda a, b, c: (a and not b) or (c and b)),
        ("A ^ B & C", lambda a, b, c: (a != b) and c),
        ("(A)", lambda a, b, c: a),
        ("1", lambda a, b, c: True),
        ("0", lambda a, b, c: False),
        ("!A'", lambda a, b, c: a),
    ],
)
def test_boolean_expressions(expr, fn):
    parsed = parse_expr(expr)
    for a, b, c in itertools.product([False, True], repeat=3):
        assert parsed.eval({"A": a, "B": b, "C": c}) == fn(a, b, c), (expr, a, b, c)


def test_variables_and_truth_table():
    parsed = parse_expr("(D&!SCE) | (SCD&SCE)")
    assert parsed.variables() == {"D", "SCE", "SCD"}
    # Bit i of the table is the value at the assignment whose bit k is order[k].
    table = truth_table(parsed, ["D", "SCD", "SCE"])
    for i in range(8):
        d, scd, sce = bool(i & 1), bool(i & 2), bool(i & 4)
        assert bool((table >> i) & 1) == ((d and not sce) or (scd and sce))


@pytest.mark.parametrize("bad", ["A &", "(A|B", "A |) B", ""])
def test_malformed_expressions_are_refused(bad):
    with pytest.raises(LibertyError):
        parse_expr(bad)

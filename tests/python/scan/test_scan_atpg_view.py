from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from faultflow.scan import ScanError, stitch_scan_json
from faultflow.scan.atpg_view import (
    ATPG_VIEW_SCHEMA_VER,
    D_BRANCH_BUF_CELL,
    OBSERVE_BUF_CELL,
    PPI_PREFIX,
    PPO_PREFIX,
    _BitIndex,
    _bit_to_single_net_name,
    build_scan_atpg_view,
)
from faultflow.scan.reports import manifest_from_result

ROOT = Path(__file__).resolve().parents[3]
CELL_MAP = ROOT / "cells/sky130/sky130_fd_sc_hd.json"
MULTICHAIN_FIXTURE = ROOT / "tests/cpp/fixtures/tiny_scan_multichain.json"


def _manifest_for_fixture() -> tuple[dict[str, Any], dict[str, Any]]:
    data = json.loads(MULTICHAIN_FIXTURE.read_text(encoding="utf-8"))
    top = "tiny_scan_multichain"
    manifest = {
        "top": top,
        "clock_net": 2,
        "scan_enable": "scan_en",
        "scan_inputs": ["scan_in_0", "scan_in_1"],
        "scan_outputs": ["scan_out_0", "scan_out_1"],
        "cells": [
            {
                "instance": "ff0",
                "chain_index": 0,
                "chain_position": 0,
                "q_net": 10,
                "data_net": 6,
            },
            {
                "instance": "ff1",
                "chain_index": 0,
                "chain_position": 1,
                "q_net": 11,
                "data_net": 7,
            },
            {
                "instance": "ff2",
                "chain_index": 1,
                "chain_position": 0,
                "q_net": 12,
                "data_net": 8,
            },
            {
                "instance": "ff3",
                "chain_index": 1,
                "chain_position": 1,
                "q_net": 13,
                "data_net": 9,
            },
        ],
    }
    return data, manifest


def _big_scan_chain(n: int) -> tuple[dict[str, Any], dict[str, Any]]:
    """A shift register of ``n`` scan FFs (each FF's Q drives the next FF's D).

    Every FF's Q net has a downstream consumer, so building the ATPG view must
    rewire each Q net and resolve its consumers -- the operations that were
    O(FFs * netlist_size) each (a per-FF full-module scan) before the bit-index
    fix. ``n`` scan cells + ``n`` nets, so one-time O(N) passes (deepcopy, id
    allocation) stay cheap while the per-FF work dominates the quadratic version.
    """
    top = "bigchain"
    clk, se, sdi0, pi = 2, 3, 4, 5
    cells: dict[str, Any] = {}
    netnames: dict[str, Any] = {"clk": {"bits": [clk]}, "se": {"bits": [se]}}
    records: list[dict[str, Any]] = []
    for i in range(n):
        q = 100 + i
        d = pi if i == 0 else 100 + (i - 1)
        sdi = sdi0 if i == 0 else 100 + (i - 1)
        cells[f"ff{i}"] = {
            "type": "$scanff_faultflow",
            "attributes": {
                "faultflow_scan": "1",
                "faultflow_scan_chain": "0",
                "faultflow_scan_index": str(i),
            },
            "connections": {
                "CLK": [clk],
                "D": [d],
                "SDI": [sdi],
                "SE": [se],
                "Q": [q],
            },
        }
        netnames[f"q{i}"] = {"bits": [q]}
        records.append(
            {
                "instance": f"ff{i}",
                "chain_index": 0,
                "chain_position": i,
                "q_net": q,
                "data_net": d,
            }
        )
    ports = {
        "clk": {"direction": "input", "bits": [clk]},
        "se": {"direction": "input", "bits": [se]},
        "sdi": {"direction": "input", "bits": [sdi0]},
        "pi": {"direction": "input", "bits": [pi]},
        "sdo": {"direction": "output", "bits": [100 + n - 1]},
    }
    generic = {
        "modules": {
            top: {
                "attributes": {"top": "0" * 31 + "1"},
                "ports": ports,
                "cells": cells,
                "netnames": netnames,
            }
        }
    }
    manifest = {
        "top": top,
        "clock_net": clk,
        "scan_enable": "se",
        "scan_inputs": ["sdi"],
        "scan_outputs": ["sdo"],
        "cells": records,
    }
    return generic, manifest


def test_build_scan_atpg_view_scales_with_ff_count() -> None:
    # Rewiring each scan FF's Q net and resolving its consumers used to be a
    # per-FF full-module scan -> O(FFs * netlist_size), which spins for tens of
    # minutes on a 12k-FF design. An incrementally-maintained bit->location index
    # makes each per-FF operation touch only the net's actual locations. Require
    # a 1000-FF chain to build well under a bound the quadratic version blows past.
    generic, manifest = _big_scan_chain(1000)
    t0 = time.perf_counter()
    _, port_map = build_scan_atpg_view(generic, manifest)
    secs = time.perf_counter() - t0
    assert len(port_map) == 1000
    assert secs < 3.0


def test_bit_index_rewire_consumers_and_removal() -> None:
    module = {
        "cells": {
            "g0": {"connections": {"A": [5], "Y": [6]}},  # reads net 5
            "g1": {"connections": {"A": [5], "B": [7], "Y": [8]}},  # also reads 5
            "drv": {"connections": {"A": [9], "Y": [5]}},  # drives net 5
        },
        "ports": {"po": {"direction": "output", "bits": [8]}, "scan_in": {"bits": [5]}},
        "netnames": {"n5": {"bits": [5]}, "n8": {"bits": [8]}},
    }
    idx = _BitIndex(module)
    # net 5 appears on 3 cell sites: (g0,A), (g1,A), (drv,Y)
    assert idx.consumer_count(5) == 3
    assert idx.consumer_count(999) == 0

    # Rewire 5 -> 42 everywhere except the excluded scan_in port.
    idx.rewire(5, 42, {"scan_in"})
    assert module["cells"]["g0"]["connections"]["A"] == [42]
    assert module["cells"]["g1"]["connections"]["A"] == [42]
    assert module["cells"]["drv"]["connections"]["Y"] == [42]
    assert module["netnames"]["n5"]["bits"] == [42]  # non-excluded net rewired
    assert module["ports"]["scan_in"]["bits"] == [5]  # excluded port untouched
    assert idx.consumer_count(42) == 3
    assert idx.consumer_count(5) == 0

    # A newly-inserted cell reading 42 bumps the count; removing a cell drops it.
    idx.add_cell("g2", {"connections": {"A": [42], "Y": [50]}})
    assert idx.consumer_count(42) == 4
    idx.remove_cell("g0", module["cells"]["g0"])
    assert idx.consumer_count(42) == 3


def test_bit_to_single_net_name_maps_single_bit_nets() -> None:
    module = {
        "netnames": {
            "a": {"bits": [5]},
            "b": {"bits": [7]},
            "bus": {"bits": [8, 9]},  # multi-bit: skipped
            "alias_of_a": {"bits": [5]},  # duplicate bit: first name wins
            "bad": {"bits": ["0"]},  # constant literal: no int bit -> skipped
        }
    }
    mapping = _bit_to_single_net_name(module)
    assert mapping == {5: "a", 7: "b"}


def test_pseudo_port_naming_and_direction() -> None:
    generic, manifest = _manifest_for_fixture()
    view, port_map = build_scan_atpg_view(generic, manifest)
    module = view["modules"]["tiny_scan_multichain"]

    assert module["ports"]["__ppi_ff0"]["direction"] == "input"
    assert module["ports"]["__ppo_ff0"]["direction"] == "output"
    assert "ff0" not in module["cells"]
    assert port_map["ff0"]["ppi_port"] == "__ppi_ff0"
    assert port_map["ff0"]["ppo_port"] == "__ppo_ff0"


def test_q_primary_output_is_rewired_to_ppi() -> None:
    generic, manifest = _manifest_for_fixture()
    view, port_map = build_scan_atpg_view(generic, manifest)
    module = view["modules"]["tiny_scan_multichain"]
    ppi_bit = module["ports"][port_map["ff0"]["ppi_port"]]["bits"][0]
    assert module["ports"]["Q0"]["bits"] == [ppi_bit]


def test_collision_detection_aborts() -> None:
    generic, manifest = _manifest_for_fixture()
    module = generic["modules"]["tiny_scan_multichain"]
    module["ports"]["__ppi_ff0"] = {"direction": "input", "bits": [99]}

    with pytest.raises(ScanError, match="pseudo-port name already exists"):
        build_scan_atpg_view(generic, manifest)


def test_dangling_scan_ports_removed() -> None:
    generic, manifest = _manifest_for_fixture()
    view, _ = build_scan_atpg_view(generic, manifest)
    module = view["modules"]["tiny_scan_multichain"]
    for name in (
        "scan_in_0",
        "scan_in_1",
        "scan_out_0",
        "scan_out_1",
        "scan_en",
        "CLK",
    ):
        assert name not in module["ports"]


def test_pseudo_port_ordering_matches_sorted_ff_instances() -> None:
    generic, manifest = _manifest_for_fixture()
    _, port_map = build_scan_atpg_view(generic, manifest)
    assert list(port_map.keys()) == ["ff0", "ff1", "ff2", "ff3"]


def test_pseudo_port_map_round_trips_manifest_cells() -> None:
    generic, manifest = _manifest_for_fixture()
    _, port_map = build_scan_atpg_view(generic, manifest)
    assert set(port_map) == {str(cell["instance"]) for cell in manifest["cells"]}
    for instance, entry in port_map.items():
        assert entry["chain_id"] in {0, 1}
        assert entry["position_in_chain"] in {0, 1}
        assert entry["ppi_port"] == f"{PPI_PREFIX}{instance}"
        assert entry["ppo_port"] == f"{PPO_PREFIX}{instance}"


def test_stitched_single_chain_view_is_valid(tmp_path: Path) -> None:
    tiny_dff = {
        "modules": {
            "tiny_dff": {
                "attributes": {"top": "1"},
                "ports": {
                    "CLK": {"direction": "input", "bits": [2]},
                    "D": {"direction": "input", "bits": [3]},
                    "Q": {"direction": "output", "bits": [4]},
                },
                "cells": {
                    "u0": {
                        "hide_name": 0,
                        "type": "sky130_fd_sc_hd__dfxtp_1",
                        "parameters": {},
                        "attributes": {},
                        "port_directions": {
                            "CLK": "input",
                            "D": "input",
                            "Q": "output",
                        },
                        "connections": {"CLK": [2], "D": [3], "Q": [4]},
                    }
                },
                "netnames": {
                    "CLK": {"hide_name": 0, "bits": [2], "attributes": {}},
                    "D": {"hide_name": 0, "bits": [3], "attributes": {}},
                    "Q": {"hide_name": 0, "bits": [4], "attributes": {}},
                },
            }
        }
    }
    source = tmp_path / "tiny_dff.json"
    source.write_text(json.dumps(tiny_dff, indent=2) + "\n", encoding="utf-8")
    output = tmp_path / "tiny_dff_scan.json"
    result = stitch_scan_json(source, CELL_MAP, "tiny_dff", output)
    manifest = manifest_from_result(result, source, tmp_path / "map.v", None)
    generic = json.loads(output.read_text(encoding="utf-8"))
    view, port_map = build_scan_atpg_view(generic, manifest)
    module = view["modules"]["tiny_dff"]
    assert len(port_map) == 1
    assert "scan_in" not in module["ports"]
    assert port_map["u0"]["ppi_port"] == "__ppi_u0"


def _d_fanout_fixture() -> tuple[dict[str, Any], dict[str, Any]]:
    """D net fans out to scan FF D pin and a combinational sink."""
    generic = {
        "modules": {
            "tiny_d_fanout": {
                "attributes": {"top": "1"},
                "ports": {
                    "CLK": {"direction": "input", "bits": [2]},
                    "scan_in": {"direction": "input", "bits": [3]},
                    "scan_en": {"direction": "input", "bits": [4]},
                    "D": {"direction": "input", "bits": [5]},
                    "B": {"direction": "input", "bits": [6]},
                    "Q": {"direction": "output", "bits": [7]},
                    "scan_out": {"direction": "output", "bits": [8]},
                    "Y": {"direction": "output", "bits": [9]},
                },
                "cells": {
                    "u0": {
                        "type": "$scanff_faultflow",
                        "parameters": {},
                        "attributes": {},
                        "connections": {
                            "CLK": [2],
                            "D": [5],
                            "SDI": [3],
                            "SE": [4],
                            "Q": [7],
                        },
                    },
                    "u_and": {
                        "type": "AND2X1",
                        "parameters": {},
                        "attributes": {},
                        "connections": {"A": [5], "B": [6], "Y": [9]},
                    },
                },
                "netnames": {
                    "CLK": {"hide_name": 0, "bits": [2], "attributes": {}},
                    "D": {"hide_name": 0, "bits": [5], "attributes": {}},
                    "B": {"hide_name": 0, "bits": [6], "attributes": {}},
                    "Q": {"hide_name": 0, "bits": [7], "attributes": {}},
                    "Y": {"hide_name": 0, "bits": [9], "attributes": {}},
                },
            }
        }
    }
    manifest = {
        "top": "tiny_d_fanout",
        "clock_net": 2,
        "scan_enable": "scan_en",
        "scan_inputs": ["scan_in"],
        "scan_outputs": ["scan_out"],
        "cells": [
            {
                "instance": "u0",
                "chain_index": 0,
                "chain_position": 0,
                "q_net": 7,
                "data_net": 5,
            }
        ],
    }
    return generic, manifest


def test_observe_buf_cell_present_per_scan_ff() -> None:
    generic, manifest = _manifest_for_fixture()
    view, _ = build_scan_atpg_view(generic, manifest)
    cells = view["modules"]["tiny_scan_multichain"]["cells"]
    for instance in ("ff0", "ff1", "ff2", "ff3"):
        assert f"$ffobserve_{instance}" in cells
        assert cells[f"$ffobserve_{instance}"]["type"] == OBSERVE_BUF_CELL


def test_boundary_sidecar_single_fanout_d_stem() -> None:
    generic, manifest = _manifest_for_fixture()
    _, port_map = build_scan_atpg_view(generic, manifest)
    boundary = port_map["ff0"]["boundary"]
    assert boundary["atpg_view_schema_ver"] == ATPG_VIEW_SCHEMA_VER
    assert boundary["d_boundary_site_key"] == "net:6:stem"
    assert boundary["d_observe_net_id"] == 6
    assert boundary["q_stem_site_key"] == "net:10:stem"
    assert boundary["unload_capable"] is True


def test_boundary_sidecar_multi_fanout_d_branch() -> None:
    generic, manifest = _d_fanout_fixture()
    view, port_map = build_scan_atpg_view(generic, manifest)
    module = view["modules"]["tiny_d_fanout"]
    cells = module["cells"]
    boundary = port_map["u0"]["boundary"]
    assert boundary["d_boundary_site_key"] == "net:5:branch:u0:D"
    assert boundary["d_observe_net_id"] != 5
    assert "$ffbranch_u0" in cells
    assert cells["$ffbranch_u0"]["type"] == D_BRANCH_BUF_CELL
    assert cells["u_and"]["connections"]["A"] == [5]


def test_schema_version_on_module_attributes() -> None:
    generic, manifest = _manifest_for_fixture()
    view, _ = build_scan_atpg_view(generic, manifest)
    attrs = view["modules"]["tiny_scan_multichain"]["attributes"]
    assert attrs["faultflow_atpg_view_schema_ver"] == ATPG_VIEW_SCHEMA_VER


def _ff_to_ff_fixture() -> tuple[dict[str, Any], dict[str, Any]]:
    """FF A's Q directly drives FF B's D (a shift-register / pipeline stage).

    `ff_a` sorts before `ff_b`, so A's Q->PPI rewrite rewires B's D pin BEFORE B
    is processed.  The manifest's data_net for B (net 10 = A's Q) is then stale.
    """
    generic = {
        "modules": {
            "tiny_ff_to_ff": {
                "attributes": {"top": "1"},
                "ports": {
                    "CLK": {"direction": "input", "bits": [2]},
                    "scan_in": {"direction": "input", "bits": [3]},
                    "scan_en": {"direction": "input", "bits": [4]},
                    "D": {"direction": "input", "bits": [5]},
                    "scan_out": {"direction": "output", "bits": [8]},
                    "Q": {"direction": "output", "bits": [12]},
                },
                "cells": {
                    "ff_a": {
                        "type": "$scanff_faultflow",
                        "parameters": {},
                        "attributes": {},
                        "connections": {
                            "CLK": [2],
                            "D": [5],
                            "SDI": [3],
                            "SE": [4],
                            "Q": [10],
                        },
                    },
                    "ff_b": {
                        "type": "$scanff_faultflow",
                        "parameters": {},
                        "attributes": {},
                        # D = ff_a.Q (net 10); SDI from the scan_in port so net 10
                        # has a single (functional) consumer -> no D-branch.
                        "connections": {
                            "CLK": [2],
                            "D": [10],
                            "SDI": [3],
                            "SE": [4],
                            "Q": [12],
                        },
                    },
                },
                "netnames": {
                    "CLK": {"hide_name": 0, "bits": [2], "attributes": {}},
                    "D": {"hide_name": 0, "bits": [5], "attributes": {}},
                    "n10": {"hide_name": 0, "bits": [10], "attributes": {}},
                    "Q": {"hide_name": 0, "bits": [12], "attributes": {}},
                },
            }
        }
    }
    manifest = {
        "top": "tiny_ff_to_ff",
        "clock_net": 2,
        "scan_enable": "scan_en",
        "scan_inputs": ["scan_in"],
        "scan_outputs": ["scan_out"],
        "cells": [
            {
                "instance": "ff_a",
                "chain_index": 0,
                "chain_position": 0,
                "q_net": 10,
                "data_net": 5,
            },
            {
                "instance": "ff_b",
                "chain_index": 0,
                "chain_position": 1,
                "q_net": 12,
                "data_net": 10,
            },
        ],
    }
    return generic, manifest


def test_ff_to_ff_d_observe_follows_rewired_pin() -> None:
    """Regression: FF A.Q -> FF B.D with A processed first.

    B's observe buffer must follow the rewired D pin (A's PPI net), not the stale
    manifest data_net (10), which dangles after A is popped and its Q rewired.
    Before the fix, __ppo_ff_b observed a driverless net and read constant 0,
    diverging from the real netlist's captured value (the PicoRV32a chain-3
    golden mismatch).
    """
    generic, manifest = _ff_to_ff_fixture()
    view, port_map = build_scan_atpg_view(generic, manifest)
    module = view["modules"]["tiny_ff_to_ff"]
    ppi_a_bit = module["ports"][port_map["ff_a"]["ppi_port"]]["bits"][0]

    observe_b = module["cells"]["$ffobserve_ff_b"]
    assert observe_b["connections"]["A"] == [ppi_a_bit]
    assert port_map["ff_b"]["boundary"]["d_observe_net_id"] == ppi_a_bit
    # The stale data_net (10) must not be what B observes.
    assert port_map["ff_b"]["boundary"]["d_observe_net_id"] != 10


def _self_loop_fixture() -> tuple[dict[str, Any], dict[str, Any]]:
    """A permanently-held FF: D wired directly to its own Q net (stitch.py's
    constant-tied-enable-inactive case -- e.g. edfxtp with DE tied to a Yosys
    constant "0", so stitch_scan_json wires D straight to the FF's own Q with
    no mux, per _add_enable_hold_mux's sibling branch in stitch_scan_json).
    """
    generic = {
        "modules": {
            "tiny_self_loop": {
                "attributes": {"top": "1"},
                "ports": {
                    "CLK": {"direction": "input", "bits": [2]},
                    "scan_in": {"direction": "input", "bits": [3]},
                    "scan_en": {"direction": "input", "bits": [4]},
                    "scan_out": {"direction": "output", "bits": [10]},
                },
                "cells": {
                    "ff_self": {
                        "type": "$scanff_faultflow",
                        "parameters": {},
                        "attributes": {},
                        # D wired directly to its own Q (net 10) -- permanent hold.
                        "connections": {
                            "CLK": [2],
                            "D": [10],
                            "SDI": [3],
                            "SE": [4],
                            "Q": [10],
                        },
                    },
                },
                "netnames": {
                    "CLK": {"hide_name": 0, "bits": [2], "attributes": {}},
                    "n10": {"hide_name": 0, "bits": [10], "attributes": {}},
                },
            }
        }
    }
    manifest = {
        "top": "tiny_self_loop",
        "clock_net": 2,
        "scan_enable": "scan_en",
        "scan_inputs": ["scan_in"],
        "scan_outputs": ["scan_out"],
        "cells": [
            {
                "instance": "ff_self",
                "chain_index": 0,
                "chain_position": 0,
                "q_net": 10,
                "data_net": 10,
            },
        ],
    }
    return generic, manifest


def test_self_referencing_ff_d_observe_follows_own_rewired_pin() -> None:
    """Regression: an FF whose D pin is wired to its own Q net (permanent hold)
    must have its D-observe PPO track its OWN PPI, not the stale pre-rewire net.

    d_net was captured once at the top of the per-record loop, BEFORE this same
    record's own Q->PPI rewrite -- for a self-referencing FF (D net == Q net on
    the SAME instance) that rewrite invalidates its own just-captured d_net
    within the same iteration, so the D-observe boundary tapped an orphaned net
    (the scan cell gets popped moments later) instead of the PPI. Confirmed root
    cause of a real PicoRV32a golden-sequence mismatch: chain 54 position 6,
    instance $auto$ff.cc:337:slice$8660 (DE tied to constant "0", D wired to
    its own Q) -- PPI was True, PPO read False (the orphaned net's default).
    """
    generic, manifest = _self_loop_fixture()
    view, port_map = build_scan_atpg_view(generic, manifest)
    module = view["modules"]["tiny_self_loop"]
    ppi_bit = module["ports"][port_map["ff_self"]["ppi_port"]]["bits"][0]
    cells = module["cells"]

    # net 11 (PPI) has >1 consumer here (the port declaration + the scan cell's
    # own rewritten D pin), so a $ffbranch buffer is inserted -- observe reads
    # PPI through that one hop, not directly. Either way, the stale net 10 must
    # not appear anywhere in the chain.
    branch = cells["$ffbranch_ff_self"]
    assert branch["connections"]["A"] == [ppi_bit]
    observe = cells["$ffobserve_ff_self"]
    assert observe["connections"]["A"] == branch["connections"]["Y"]
    assert (
        port_map["ff_self"]["boundary"]["d_observe_net_id"]
        == branch["connections"]["Y"][0]
    )
    assert port_map["ff_self"]["boundary"]["d_observe_net_id"] != 10
    assert not any(
        10 in c.get("connections", {}).get(pin, [])
        for c in cells.values()
        for pin in c.get("connections", {})
    ), "stale net 10 must not be referenced anywhere in the reduced view"

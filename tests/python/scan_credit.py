"""Does the full scan protocol reproduce what a scan campaign credits?

The reduced ATPG view is a shortcut (CLAUDE.md #24): every fault a scan
campaign credits must be one its own exported patterns detect in the full
scan-protocol fault simulator on the scanned netlist -- with the campaign's X
mask applied, since a masked output earns no credit. With scan compression,
also on the compressed chip itself, the decompressor loading the chains
(compressed_credit_not_reproduced).
"""

from __future__ import annotations

import itertools
import json
import sqlite3
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from faultflow.runner.runner import (
    _atpg_pi_names,
    _load_core,
    _port_name_for_net,
    _positional_bus_bits,
)
from faultflow.scan.cell_map import resolve_scan_cell_map
from faultflow.scan.manifest import manifest_clock_net_ids
from faultflow.scan.pattern_export import scan_pattern_from_dict
from faultflow.scan.site_resolution import build_site_key_index, fault_type_to_sa_code
from faultflow.scan.x_mask import _net_of_name, recorded_x_mask


@dataclass(frozen=True)
class _Grader:
    """The faults the latest scan campaign credits, its exported patterns, and
    `detect(i, inputs)`: the indices of the credited faults pattern i detects in
    the full protocol, `inputs` the values of inputs it leaves unset (0
    otherwise)."""

    credited: list[tuple[str, str]]
    patterns: list[Any]
    detect: Callable[..., set[int]]


def credit_not_reproduced(
    cfg: Any,
    patterns_path: Path,
    *,
    loc: bool = False,
    preamble: int | None = None,
    campaign_type: str = "scan",
) -> list[tuple[str, str]]:
    """The (site key, fault type) of each fault the latest scan campaign
    (`campaign_type`: "scan_extest" for the real wrapper's EXTEST) credits that
    none of the exported patterns in `patterns_path` detects in the full protocol
    on the scanned netlist. `loc` replays launch-on-capture patterns (two capture
    pulses); launch-on-shift patterns don't carry their launch shift and can't be
    replayed from the file. Each pattern is replayed after its own preamble, or
    `preamble` clock pulses when given."""
    grader = _grader(
        cfg, patterns_path, loc=loc, preamble=preamble, campaign_type=campaign_type
    )
    reproduced: set[int] = set()
    for number in range(len(grader.patterns)):
        reproduced |= grader.detect(number, {})
    return [
        grader.credited[i] for i in range(len(grader.credited)) if i not in reproduced
    ]


def environment_dependent(
    cfg: Any, patterns_path: Path, environment: Collection[str], *, loc: bool = False
) -> list[tuple[str, str]]:
    """For a wrapped block's INTEST, whose `environment` inputs (bit names) its
    patterns leave unknown: the credited faults no exported pattern detects for
    every value of the environment -- each pattern tried with all of them. Empty
    when no credit depends on what drives the block."""
    names = sorted(environment)
    assert len(names) <= 10, "every value of the environment is tried"
    grader = _grader(cfg, patterns_path, loc=loc, preamble=None)
    robust: set[int] = set()
    for number in range(len(grader.patterns)):
        always: set[int] | None = None
        for values in itertools.product((False, True), repeat=len(names)):
            found = grader.detect(number, dict(zip(names, values)))
            always = found if always is None else always & found
            if not always:
                break
        robust |= always or set()
    return [grader.credited[i] for i in range(len(grader.credited)) if i not in robust]


def core_dependent(
    cfg: Any, patterns_path: Path, *, loc: bool = False
) -> list[tuple[str, str]]:
    """For a wrapped block's EXTEST, whose patterns load the wrapper chains alone
    and leave the core's state unknown: the credited faults no exported pattern
    detects in every state of the core, on the whole block -- each pattern
    tried with every load of the core chains."""
    manifest = json.loads(cfg.scan_manifest_path.read_text(encoding="utf-8"))
    longest = int(manifest["max_chain_length"])
    core_chains = {
        int(c["index"]): int(c["length"])
        for c in manifest["chains"]
        if c.get("kind", "core") == "core"
    }
    states = sum(core_chains.values())
    assert states <= 10, "every state of the core is tried"
    grader = _grader(
        cfg, patterns_path, loc=loc, preamble=None, campaign_type="scan_extest"
    )
    robust: set[int] = set()
    for number, pattern in enumerate(grader.patterns):
        shifts = pattern.shift_length or longest
        # The whole chip's shift: the wrapper chains' loads behind as many
        # zeros as it shifts more, so they end where their own shift puts them.
        loads = {
            chain: [False] * (longest - shifts) + list(bits)
            for chain, bits in pattern.load_seqs.items()
        }
        always: set[int] | None = None
        for state in itertools.product((False, True), repeat=states):
            bits = iter(state)
            for chain, length in core_chains.items():
                own = [next(bits) for _ in range(length)]
                loads[chain] = [False] * (longest - length) + own
            found = grader.detect(number, {}, loads=loads, shifts=longest)
            always = found if always is None else always & found
            if not always:
                break
        robust |= always or set()
    return [grader.credited[i] for i in range(len(grader.credited)) if i not in robust]


def _grader(
    cfg: Any,
    patterns_path: Path,
    *,
    loc: bool,
    preamble: int | None,
    campaign_type: str = "scan",
) -> _Grader:
    core = _load_core()
    assert core is not None
    top = cfg.top
    manifest = json.loads(cfg.scan_manifest_path.read_text(encoding="utf-8"))
    generic = Path(str(manifest["generic_json"]))
    view = json.loads(
        (cfg.intermediate_dir / "scan_atpg_view.json").read_text(encoding="utf-8")
    )
    view_module = view["modules"][top]
    masked = recorded_x_mask(view, top) or frozenset()
    outputs = [
        bit_name
        for name, port in view_module["ports"].items()
        if port["direction"] == "output" and not name.startswith("__")
        for bit_name in _positional_bus_bits(name, len(port["bits"]))
        if _net_of_name(view_module, bit_name) not in masked
    ]
    cell_map = str(resolve_scan_cell_map(cfg))
    unsupported = cfg.simulation.unsupported_cells
    blackboxes = list(cfg.blackbox_instances)
    conn = sqlite3.connect(cfg.db_path)
    (campaign,) = conn.execute(
        "SELECT MAX(id) FROM campaigns WHERE campaign_type = ?", (campaign_type,)
    ).fetchone()
    credited = conn.execute(
        "SELECT fault_site_key, fault_type FROM faults "
        "WHERE campaign_id = ? AND status = 'detected'",
        (campaign,),
    ).fetchall()
    conn.close()
    assert credited, "the campaign credits nothing"
    index = build_site_key_index(core, generic, cell_map, unsupported, blackboxes)
    specs = [(index[key], fault_type_to_sa_code(ft)) for key, ft in credited]
    clock_ports = [
        _port_name_for_net(generic, top, net, "input")
        for net in manifest_clock_net_ids(manifest)
    ]
    patterns = [
        scan_pattern_from_dict(raw)
        for raw in json.loads(patterns_path.read_text(encoding="utf-8"))
    ]

    def detect(
        number: int,
        given: Mapping[str, bool],
        *,
        loads: Mapping[int, list[bool]] | None = None,
        shifts: int | None = None,
    ) -> set[int]:
        """Pattern `number`'s detections; `loads` and `shifts` instead of its own
        load and shift length. A chain it doesn't unload isn't compared."""
        pattern = patterns[number]
        assert not set(given) & set(pattern.capture_pi_values), "the pattern sets it"
        length = shifts or pattern.shift_length or int(manifest["max_chain_length"])
        mask = {
            chain: (list(bits) + [False] * length)[:length]
            for chain, bits in (pattern.unload_mask or {}).items()
        }
        for chain in range(len(manifest["scan_outputs"])):
            if chain not in pattern.expected_unload:
                mask[chain] = [False] * length
            elif chain not in mask:
                own = len(pattern.expected_unload[chain])
                mask[chain] = [t < own for t in range(length)]
        result = core.simulate_scan_protocol_faults(
            str(generic),
            cell_map,
            clock_ports,
            scan_enable_port=str(manifest["scan_enable"]),
            scan_input_ports=[str(p) for p in manifest["scan_inputs"]],
            scan_output_ports=[str(p) for p in manifest["scan_outputs"]],
            functional_output_ports=outputs,
            max_chain_length=length,
            load_seqs=dict(loads) if loads is not None else pattern.load_seqs,
            capture_pi_values={**pattern.capture_pi_values, **given},
            faults=specs,
            unsupported_policy=unsupported,
            loc_two_capture=loc,
            blackbox_instances=blackboxes,
            unload_mask=mask,
            preamble_cycles=(pattern.preamble_cycles if preamble is None else preamble),
            shift_pi_values=pattern.shift_pi_values,
        )
        return {
            int(dict(lane)["fault_index"])
            for batch in result["batches"]
            for lane in batch["lanes"]
            if dict(lane)["outcome"] == "pass"
        }

    return _Grader([(str(k), str(t)) for k, t in credited], patterns, detect)


# insert_compression's instance of the core in the composed netlist: compose_soc
# names the core's cells `<instance>__<cell>`.
CORE_INSTANCE = "core_inst"


def _core_to_composed_nets(
    core_module: dict[str, Any], composed_module: dict[str, Any]
) -> dict[int, int]:
    """Core net id -> composed net id, through every cell pin and port bit the
    two share."""
    nets: dict[int, int] = {}

    def pair(core_bits: list[Any], composed_bits: list[Any]) -> None:
        for core_bit, composed_bit in zip(core_bits, composed_bits):
            if isinstance(core_bit, int) and isinstance(composed_bit, int):
                nets.setdefault(core_bit, composed_bit)

    for name, cell in core_module["cells"].items():
        composed = composed_module["cells"][f"{CORE_INSTANCE}__{name}"]
        for pin, bits in cell["connections"].items():
            pair(bits, composed["connections"][pin])
    for name, port in core_module["ports"].items():
        if name in composed_module["ports"]:
            pair(port["bits"], composed_module["ports"][name]["bits"])
    return nets


def _composed_site_key(key: str, nets: dict[int, int]) -> str:
    """A core fault site's key in the composed netlist."""
    prefix, net, rest = key.split(":", 2)
    assert prefix == "net", key
    if rest == "stem":
        return f"net:{nets[int(net)]}:stem"
    kind, consumer = rest.split(":", 1)
    assert kind == "branch", key
    cell, pin = consumer.rsplit(":", 1)
    return f"net:{nets[int(net)]}:branch:{CORE_INSTANCE}__{cell}:{pin}"


def compressed_credit_not_reproduced(
    cfg: Any, patterns_path: Path, seed_of: Callable[[int, dict[str, Any]], int]
) -> dict[str, list[Any]]:
    """Every fault the latest (stuck-at) scan campaign credits must be detected
    by its exported patterns on the COMPRESSED chip itself: the composed
    netlist scan-compress wrote, the decompressor's synthesized gates
    included. Each pattern's seed (`seed_of(index, exported pattern dict)`: a
    tester's solve, e.g. warptap's) is held on the channel bus and the chip's
    own protocol is run cycle by cycle (simulate_sequence_faults) as the core
    grade runs it -- preamble, load, capture, unload, the faults kept out of
    the preamble and the load -- and compared where the core grade compares:
    the unmasked functional outputs at capture and each chain's unmasked
    unload bits.

    Returns {"not_reproduced": [(site key, fault type), ...], "golden": [...]};
    the second lists (pattern index, observation) where the chip's fault-free
    response contradicts the pattern's expected one -- at a functional output,
    or an unload bit the chain itself holds (the bits a shorter chain shifts
    in during the unload are the decompressor's on the chip, 0s in the core
    grade, and not part of the expected response)."""
    core = _load_core()
    assert core is not None
    top = cfg.top
    manifest = json.loads(cfg.scan_manifest_path.read_text(encoding="utf-8"))
    compression = manifest["compression"]
    composed = Path(str(compression["composed_json"]))
    composed_top = str(compression["composed_top"])
    channel = str(compression.get("channel_port", "tdi"))
    width = int(compression["num_channels"])
    generic = Path(str(manifest["generic_json"]))
    core_module = json.loads(generic.read_text(encoding="utf-8"))["modules"][
        str(manifest["top"])
    ]
    composed_module = json.loads(composed.read_text(encoding="utf-8"))["modules"][
        composed_top
    ]
    view = json.loads(
        (cfg.intermediate_dir / "scan_atpg_view.json").read_text(encoding="utf-8")
    )
    view_module = view["modules"][top]
    masked = recorded_x_mask(view, top) or frozenset()
    outputs = [
        bit_name
        for name, port in view_module["ports"].items()
        if port["direction"] == "output" and not name.startswith("__")
        for bit_name in _positional_bus_bits(name, len(port["bits"]))
        if _net_of_name(view_module, bit_name) not in masked
    ]
    cell_map = str(resolve_scan_cell_map(cfg))
    unsupported = cfg.simulation.unsupported_cells
    blackboxes = [f"{CORE_INSTANCE}__{name}" for name in cfg.blackbox_instances]
    conn = sqlite3.connect(cfg.db_path)
    (campaign,) = conn.execute(
        "SELECT MAX(id) FROM campaigns WHERE campaign_type = 'scan'"
    ).fetchone()
    credited = conn.execute(
        "SELECT fault_site_key, fault_type FROM faults "
        "WHERE campaign_id = ? AND status = 'detected'",
        (campaign,),
    ).fetchall()
    conn.close()
    assert credited, "the campaign credits nothing"
    nets = _core_to_composed_nets(core_module, composed_module)
    index = build_site_key_index(core, composed, cell_map, unsupported, blackboxes)
    specs = [
        (index[_composed_site_key(key, nets)], fault_type_to_sa_code(ft))
        for key, ft in credited
    ]
    clock_ports = [
        str(_port_name_for_net(generic, str(manifest["top"]), net, "input"))
        for net in manifest_clock_net_ids(manifest)
    ]
    scan_enable = str(manifest["scan_enable"])
    scan_outputs = [str(port) for port in manifest["scan_outputs"]]
    max_chain_length = int(manifest["max_chain_length"])
    lengths = {int(c["index"]): int(c["length"]) for c in manifest["chains"]}
    input_order = _atpg_pi_names(composed, composed_top)
    inputs = set(input_order)

    detected: set[int] = set()
    golden_mismatches: list[Any] = []
    for number, raw in enumerate(json.loads(patterns_path.read_text(encoding="utf-8"))):
        pattern = scan_pattern_from_dict(raw)
        seed = seed_of(number, raw)
        base = {k: v for k, v in pattern.capture_pi_values.items() if k in inputs}
        base.update({f"{channel}[{k}]": bool((seed >> k) & 1) for k in range(width)})
        cycles: list[list[bool]] = []
        active: list[bool] = []

        def pulse(values: dict[str, bool], fault_active: bool) -> int:
            """One clock period, its low half (where outputs are sampled)
            first; returns that cycle's index."""
            for level in (False, True):
                cycle = {**values, **{clock: level for clock in clock_ports}}
                cycles.append([bool(cycle.get(name, False)) for name in input_order])
                active.append(fault_active)
            return len(cycles) - 2

        shift = {
            **base,
            **{k: v for k, v in pattern.shift_pi_values.items() if k in inputs},
        }
        for _ in range(pattern.preamble_cycles):
            pulse({**shift, scan_enable: False}, False)
        for _ in range(max_chain_length):
            pulse({**shift, scan_enable: True}, False)
        capture = pulse({**base, scan_enable: False}, True)
        unloads = [
            pulse({**shift, scan_enable: True}, True) for _ in range(max_chain_length)
        ]

        # One observation per set of compared samples: the functional outputs
        # at capture, then the scan outputs of the chains sharing an unload
        # mask at the unload cycles it compares.
        mask: dict[int, list[bool]] = pattern.unload_mask or {}
        by_mask: dict[tuple[bool, ...], list[int]] = {}
        for chain in range(len(scan_outputs)):
            bits = tuple(mask.get(chain, [True] * max_chain_length))
            by_mask.setdefault(bits, []).append(chain)
        observations: list[tuple[list[int], list[str], list[int]]] = [
            ([], outputs, [capture])
        ]
        for bits, chain_ids in by_mask.items():
            compared = [unloads[t] for t in range(max_chain_length) if bits[t]]
            ports = [scan_outputs[chain] for chain in chain_ids]
            observations.append((chain_ids, ports, compared))
        for chain_ids, observe, sampled in observations:
            if not observe or not sampled:
                continue
            # Fault dropping: only what no earlier observation detected.
            remaining = [i for i in range(len(specs)) if i not in detected]
            result = core.simulate_sequence_faults(
                str(composed),
                cell_map,
                input_order,
                cycles,
                [i in sampled for i in range(len(cycles))],
                observe,
                [specs[i] for i in remaining],
                unsupported,
                blackboxes,
                0,
                fault_active=active,
            )
            detected.update(
                remaining[i]
                for i, first in enumerate(result["first_sample"])
                if first >= 0
            )
            if not chain_ids:
                for port, value in zip(observe, result["golden"][0]):
                    expected = pattern.capture_pi_values.get(port)
                    if expected is not None and bool(expected) != bool(value):
                        golden_mismatches.append((number, port))
                continue
            compared_cycles = [
                t for t in range(max_chain_length) if unloads[t] in sampled
            ]
            for t, row in zip(compared_cycles, result["golden"]):
                for chain, value in zip(chain_ids, row):
                    if t < lengths[chain] and bool(value) != bool(
                        pattern.expected_unload[chain][t]
                    ):
                        golden_mismatches.append((number, f"chain {chain} bit {t}"))
    return {
        "not_reproduced": [
            tuple(credited[i]) for i in range(len(credited)) if i not in detected
        ],
        "golden": golden_mismatches,
    }

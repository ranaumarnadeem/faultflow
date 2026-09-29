"""Does the full scan protocol reproduce what a scan campaign credits?

The reduced ATPG view is a shortcut (CLAUDE.md #24): every fault a scan
campaign credits must be one its own exported patterns detect in the full
scan-protocol fault simulator on the scanned netlist -- with the campaign's X
mask applied, since a masked output earns no credit.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from faultflow.runner.runner import (
    _load_core,
    _port_name_for_net,
    _positional_bus_bits,
)
from faultflow.scan.cell_map import resolve_scan_cell_map
from faultflow.scan.manifest import manifest_clock_net_ids
from faultflow.scan.pattern_export import scan_pattern_from_dict
from faultflow.scan.site_resolution import build_site_key_index, fault_type_to_sa_code
from faultflow.scan.x_mask import _net_of_name, recorded_x_mask


def credit_not_reproduced(
    cfg: Any, patterns_path: Path, *, loc: bool = False
) -> list[tuple[str, str]]:
    """The (site key, fault type) of each fault the latest scan campaign
    credits that none of the exported patterns in `patterns_path` detects in
    the full protocol. `loc` replays launch-on-capture patterns (two capture
    pulses); launch-on-shift patterns don't carry their launch shift and
    can't be replayed from the file."""
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
        "SELECT MAX(id) FROM campaigns WHERE campaign_type = 'scan'"
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
    reproduced: set[int] = set()
    for raw in json.loads(patterns_path.read_text(encoding="utf-8")):
        pattern = scan_pattern_from_dict(raw)
        result = core.simulate_scan_protocol_faults(
            str(generic),
            cell_map,
            clock_ports,
            scan_enable_port=str(manifest["scan_enable"]),
            scan_input_ports=[str(p) for p in manifest["scan_inputs"]],
            scan_output_ports=[str(p) for p in manifest["scan_outputs"]],
            functional_output_ports=outputs,
            max_chain_length=int(manifest["max_chain_length"]),
            load_seqs=pattern.load_seqs,
            capture_pi_values=pattern.capture_pi_values,
            faults=specs,
            unsupported_policy=unsupported,
            loc_two_capture=loc,
            blackbox_instances=blackboxes,
            unload_mask=pattern.unload_mask or {},
        )
        for batch in result["batches"]:
            for lane in batch["lanes"]:
                if dict(lane)["outcome"] == "pass":
                    reproduced.add(int(dict(lane)["fault_index"]))
    return [tuple(credited[i]) for i in range(len(credited)) if i not in reproduced]

"""The manifest mbist-insert writes for the inserted chip, in autoMBIST's
instance-manifest format, so FaultFlow reads it as it reads a generated
design's.

Every instance the inserted DFT consists of is listed by the path its cells
carry once the DFT is synthesized frozen (synth.py): each shell's leaves as
`<shell path>__<leaf>` -- the collar's controller and repair logic under the
category autoMBIST gave them, the shell's synchronizers and done delay as
mbist_shell -- and each memory as `<shell path>__u_collar.<its instance>`.

With JTAG, the test_access block describes the TAP and the network: every
instance above plus warptap's (jtag_tap, ijtag_sib, ijtag_tdr; the control
TDRs' clear synchronizer counts with the TDRs), the instruments in network
order, the instruction that selects the network (IJTAG_ACCESS), and the chip
reset the control TDRs clear on.
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from faultflow.mbist.jtag import TAP_PORTS, JtagInsertion
from faultflow.mbist.netlist import InsertError
from faultflow.mbist.spec import ResetSpec

MANIFEST_FORMAT = "autombist_instance_manifest"
# 1.1: test_access names the network's instruction and the chip reset.
SCHEMA_VERSION = "1.1.0"
SHELL_CATEGORY = "mbist_shell"

_JTAG_CATEGORY = {
    "tap_core": "jtag_tap",
    "sib_cell": "ijtag_sib",
    "tck_reset_sync": "ijtag_tdr",
}


@dataclass(frozen=True)
class ShellRecord:
    """One inserted shell, as the manifest lists it."""

    path: str  # the shell's instance path
    leaves: dict[str, str]  # leaf path in the shell -> module (synth.shell_leaves)
    collar_categories: dict[str, str]  # instance in the collar -> autoMBIST category
    memory_instance: str  # the macro's instance in the collar
    macro: str
    macro_sources: tuple[Path, ...]


def _relative(path: Path, root: Path) -> str:
    try:
        return os.path.relpath(path, root)
    except ValueError:  # another drive
        return str(path)


def macro_sources(macro: str, files: Sequence[Path]) -> tuple[Path, ...]:
    """The files among `files` that define module `macro`: its stub."""
    pattern = re.compile(rf"\bmodule\s+{re.escape(macro)}\b")
    found = tuple(
        f
        for f in files
        if pattern.search(f.read_text(encoding="utf-8", errors="replace"))
    )
    if not found:
        raise InsertError(f"no design file defines the memory macro {macro}")
    return found


def _instances(
    shells: Sequence[ShellRecord], rtl: Path, root: Path
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for shell in shells:
        for leaf, module in sorted(shell.leaves.items()):
            inner = leaf.split(".", 1)[1] if leaf.startswith("u_collar.") else None
            if inner is None:
                category = SHELL_CATEGORY
            elif inner in shell.collar_categories:
                category = shell.collar_categories[inner]
            else:
                raise InsertError(
                    f"autoMBIST's manifest has no category for the collar's {inner}"
                )
            entries.append(
                {
                    "category": category,
                    "hierarchical_path": f"{shell.path}__{leaf}",
                    "hierarchy_hint": "separate",
                    "instance_name": leaf.rsplit(".", 1)[-1],
                    "module_type": module,
                    "sources": [_relative(rtl, root)],
                }
            )
        entries.append(
            {
                "category": "memory",
                "hierarchical_path": f"{shell.path}__u_collar.{shell.memory_instance}",
                "hierarchy_hint": "blackbox",
                "instance_name": shell.memory_instance,
                "module_type": shell.macro,
                "sources": [_relative(p, root) for p in shell.macro_sources],
            }
        )
    return entries


def _network(
    jtag: JtagInsertion, top_cells: dict[str, dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """test_access's warptap instances, and its instruments in network order."""
    entries: list[dict[str, Any]] = []
    sib_of: dict[str, str] = {}  # instrument -> SIB cell
    bits_of: dict[str, list[tuple[int, str]]] = {}  # SIB name -> (bit, TDR cell)
    for cell, module in sorted(jtag.instances().items()):
        attributes = top_cells[cell].get("attributes", {})
        sib_name = attributes.get("warptap_sib_name")
        entry: dict[str, Any] = {
            "hierarchical_path": cell,
            "hierarchy_hint": "separate",
            "instance_name": cell,
            "module_type": module,
        }
        if module == "sib_cell":
            instrument = str(attributes["warptap_instrument_name"])
            sib_of[instrument] = cell
            entry.update(category="ijtag_sib", sib_name=sib_name, instrument=instrument)
        elif sib_name is not None:
            raw_bit = attributes["warptap_instrument_bit"]
            # An int as warptap set it, or Yosys's binary string after a round trip.
            bit = raw_bit if isinstance(raw_bit, int) else int(str(raw_bit), 2)
            bits_of.setdefault(str(sib_name), []).append((bit, cell))
            entry.update(category="ijtag_tdr", sib_name=sib_name, bit=bit)
        elif module in _JTAG_CATEGORY:
            entry["category"] = _JTAG_CATEGORY[module]
        else:
            raise InsertError(f"warptap added a {module} the manifest can't place")
        entries.append(entry)
    sib_names = {
        cell: str(top_cells[cell]["attributes"]["warptap_sib_name"])
        for cell in sib_of.values()
    }
    instruments = []
    for port in jtag.ports:
        sib = sib_of[port.name]
        tdr_bits = [cell for _, cell in sorted(bits_of[sib_names[sib]])]
        instruments.append(
            {
                "name": port.name,
                "role": port.role,
                "width": port.width,
                "sib": sib,
                "tdr_bits": tdr_bits,
                "capture_sync": port.capture_sync,
            }
        )
    return entries, instruments


def chip_manifest(
    top: str,
    rtl: Path,
    shells: Sequence[ShellRecord],
    *,
    root: Path,
    jtag: JtagInsertion | None = None,
    top_cells: dict[str, dict[str, Any]] | None = None,
    reset: ResetSpec | None = None,
    icl: Path | None = None,
    bsdl: Path | None = None,
) -> dict[str, Any]:
    """The manifest for the inserted chip, its paths relative to `root` (the
    directory it is written to). With JTAG, `top_cells` are the top module's
    cells and `reset` the chip reset."""
    instances = _instances(shells, rtl, root)
    data: dict[str, Any] = {
        "format": MANIFEST_FORMAT,
        "schema_version": SCHEMA_VERSION,
        "generator": {"command": "faultflow mbist-insert"},
        "top_module": top,
        "sources": {"wrapper": _relative(rtl, root)},
        "instances": instances,
    }
    if jtag is not None:
        if top_cells is None or reset is None:
            raise ValueError("a JTAG manifest needs the top's cells and the reset")
        network, instruments = _network(jtag, top_cells)
        data["test_access"] = {
            "wrapped": True,
            "memory_blackboxed": True,
            "top_module": top,
            "output_verilog": _relative(rtl, root),
            "icl_path": None if icl is None else _relative(icl, root),
            "bsdl_path": None if bsdl is None else _relative(bsdl, root),
            "boundary_ports": list(TAP_PORTS),
            "network_instruction": "IJTAG_ACCESS",
            "network_opcode": jtag.opcode,
            "idcode_value": jtag.idcode,
            "chip_reset": {
                "port": reset.port,
                "active": "low" if reset.active_low else "high",
            },
            "instances": instances + network,
            "instruments": instruments,
        }
    return data

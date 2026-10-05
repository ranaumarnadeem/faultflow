"""What is around a wrapped block while its INTEST runs, as FaultFlow models it: an
unknown blackbox.

In INTEST the block sits in its SoC, so whatever drives its wrapped inputs is
another block's logic, and its wrapped outputs go nowhere a test observes. Its input
cells hold, so nothing of the inputs should reach the core; its output cells drive
their ports safe. A pattern must not depend on either.

FaultFlow models that environment as a blackbox instance, :data:`ENVIRONMENT`, on a
harness of the scanned netlist (:func:`intest_harness`): every wrapped port bit is
cut from its port and wired to the instance instead -- an input bit to one of its
outputs, an output bit to one of its inputs. The scan test then treats it as it
treats a memory whose output is unknown (an X source, faultflow.scan.x_mask):
- the scan ATPG view ties its outputs and leaves its inputs unread;
- a fault only the environment could test is ``blackbox_unresolved``;
- what the held mode pins block from it costs nothing (x_mask's blocked sources).

The harness has the same nets, and so the same fault sites, as the scanned netlist.
A bus port whose bits are wrapped only in part keeps its other bits as one-bit
ports named ``port[bit]``, the name a pattern already gives them.
"""

from __future__ import annotations

import copy
import dataclasses
from typing import Any, Mapping

from faultflow.config import FaultflowConfig
from faultflow.wrap.cell import INPUT, OUTPUT
from faultflow.wrap.errors import WrapError

ENVIRONMENT = "__environment__"
ENVIRONMENT_TYPE = "$faultflow_environment"


def with_environment(cfg: FaultflowConfig) -> FaultflowConfig:
    """``cfg`` for a run on the harness: the environment one more blackbox, its
    outputs unknown."""
    return dataclasses.replace(
        cfg,
        blackbox_instances=(*cfg.blackbox_instances, ENVIRONMENT),
        blackbox_output_values=(*cfg.blackbox_output_values, (ENVIRONMENT, "x")),
    )


def environment_ports(wrapper: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    """The wrapped input bits the environment drives, and the wrapped output bits
    it reads, by the names a pattern gives them: ``port``, or ``port[bit]`` for a
    bus bit."""
    inputs: list[str] = []
    outputs: list[str] = []
    for cell in wrapper["cells"]:
        (inputs if cell["side"] == INPUT else outputs).append(str(cell["label"]))
    return inputs, outputs


def intest_harness(
    netlist: Mapping[str, Any], top: str, wrapper: Mapping[str, Any]
) -> dict[str, Any]:
    """``netlist`` (Yosys JSON of the scanned, wrapped block) with its wrapped port
    bits wired to the environment instance instead of their ports."""
    harness = copy.deepcopy(dict(netlist))
    module = harness["modules"][top]
    cells = module.setdefault("cells", {})
    if ENVIRONMENT in cells:
        raise WrapError(f"the netlist already has a cell named {ENVIRONMENT}")
    wrapped = {
        (str(c["port"]), int(c["bit"])): str(c["side"]) for c in wrapper["cells"]
    }
    directions: dict[str, str] = {}
    connections: dict[str, list[Any]] = {}
    ports: dict[str, Any] = {}
    for name, port in module.get("ports", {}).items():
        bits = list(port.get("bits", []))
        if not any((name, i) in wrapped for i in range(len(bits))):
            ports[name] = port
            continue
        for i, net in enumerate(bits):
            side = wrapped.get((name, i))
            if side is None:
                label = name if len(bits) == 1 else f"{name}[{i}]"
                ports[label] = {"direction": port["direction"], "bits": [net]}
                continue
            if side != port.get("direction") or side not in (INPUT, OUTPUT):
                raise WrapError(
                    f"the wrapper's {side} cell on {name}[{i}] sits on a "
                    f"{port.get('direction')} port"
                )
            pin = f"{'drive' if side == INPUT else 'sense'}{len(connections)}"
            # The environment drives what reaches an input bit and reads an output.
            directions[pin] = "output" if side == INPUT else "input"
            connections[pin] = [net]
    missing = sorted(f"{p}[{b}]" for p, b in wrapped if p not in module["ports"])
    if missing:
        raise WrapError(f"the wrapper's cells name ports the netlist lacks: {missing}")
    module["ports"] = ports
    cells[ENVIRONMENT] = {
        "hide_name": 0,
        "type": ENVIRONMENT_TYPE,
        "parameters": {},
        "attributes": {"faultflow_internal": "1"},
        "port_directions": directions,
        "connections": connections,
    }
    return harness

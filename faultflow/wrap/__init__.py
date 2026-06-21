"""IEEE 1500 wrapper insertion for faultflow.

`wrap_ports` injects boundary cells onto a synthesized Yosys-JSON netlist's ports.
Two models, selected by ``wbr_model``:

* ``buffer`` (default) — transparent ``$wbc_in/out_faultflow`` cells (Stages 1-3).
* ``scan`` — native shiftable ``$wbc_in/out_scan_faultflow`` cells (Stage 4): each
  is a scan FF whose state q drives the core/interconnect through a mode-mux, with
  the wrapper boundary register stitched into a dedicated scan chain (its own
  ``scan_in``/``scan_out``, sharing the design ``CLK``/``SE``). This is what makes
  the boundary stimulus/observe retargetable at the SoC level (Stage 5).
"""

from __future__ import annotations

from faultflow.wrap.ports import WrapError, wrap_ports

__all__ = ["wrap_ports", "WrapError"]

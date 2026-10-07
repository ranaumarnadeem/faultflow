"""The IEEE 1500 wrapper FaultFlow puts on a block (ff.py wrap, [wrap] enabled).

Each wrapped port bit gets a boundary cell of three sky130 cells (cell): a mux
choosing the functional value or the cell's flop, a gate making the core or the
port safe, and the flop. Two mode pins set functional, INTEST and EXTEST
(record.MODES). classify decides which ports are wrapped, block puts the cells
on, record writes them into the scan manifest; sides, environment, graybox and
report serve the INTEST and EXTEST scan tests.
"""

from __future__ import annotations

from faultflow.wrap.errors import WrapError

__all__ = ["WrapError"]

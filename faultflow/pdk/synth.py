"""What a Yosys synthesis run maps onto, and the cells it reads first.

Every synthesis script FaultFlow writes -- the design's (runner.YOSYS_TEMPLATE), the
SoC glue's (project.assemble), mbist-insert's and autoMBIST's, the decompressor's and
the compactor's -- reads every Liberty file of the design's libraries as cell modules
before elaborating, so a design may instantiate library cells (of [design]
extra_libraries too), deletes those modules again before writing its netlist, and
maps the design's own logic and flops onto the primary library alone, never onto a
cell the library's map does not model (``dont_use``)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Union

if TYPE_CHECKING:
    from faultflow.config import FaultflowConfig

# The attribute the Liberty cell modules are read with, to delete them by.
LIBERTY_CELL_ATTR = "faultflow_liberty_cell"
DELETE_LIBERTY_CELLS = f"delete =A:{LIBERTY_CELL_ATTR}"


def _quote(path: Path) -> str:
    text = str(path)
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


@dataclass(frozen=True)
class SynthLibrary:
    # The Liberty dfflibmap and abc map onto.
    liberty: Path
    # Every Liberty file read as cells: empty for `liberty` alone.
    cells: tuple[Path, ...] = ()
    # Cells mapping must not use (FaultflowConfig.dont_use).
    dont_use: tuple[str, ...] = ()

    @classmethod
    def of(cls, library: "SynthLibrary | Path") -> "SynthLibrary":
        """``library``, or a bare Liberty path as one."""
        return library if isinstance(library, SynthLibrary) else cls(Path(library))

    @classmethod
    def from_config(cls, cfg: "FaultflowConfig") -> "SynthLibrary":
        if cfg.liberty is None:
            raise ValueError("synthesis needs [design] liberty")
        return cls(cfg.liberty, cfg.synthesis_libraries, cfg.dont_use)

    @property
    def cell_files(self) -> tuple[Path, ...]:
        return self.cells or (self.liberty,)

    def read_lines(self) -> list[str]:
        """Every library's cells, as modules a design's instances resolve to."""
        return [
            f"read_liberty -lib -setattr {LIBERTY_CELL_ATTR} {_quote(path)}"
            for path in self.cell_files
        ]

    def map_lines(self) -> list[str]:
        """dfflibmap and abc onto `liberty`, its unmodelled cells excluded."""
        dont = "".join(f" -dont_use {cell}" for cell in self.dont_use)
        return [
            f"dfflibmap -liberty {_quote(self.liberty)}{dont}",
            f"abc -liberty {_quote(self.liberty)}{dont}",
        ]


LibraryArg = Union[SynthLibrary, Path]

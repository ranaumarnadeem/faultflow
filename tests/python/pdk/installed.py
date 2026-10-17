"""The PDKs installed for the tests: each in the open_pdks layout, found in $PDK_ROOT
(else ~/.ciel) or in ciel's store of every version fetched
(``<root>/ciel/<family>/versions/<hash>``)."""

from __future__ import annotations

import os
from pathlib import Path

# The standard-cell libraries the per-library tests run on, by PDK.
LIBRARIES: dict[str, tuple[str, ...]] = {
    "sky130A": (
        "sky130_fd_sc_hd",
        "sky130_fd_sc_hdll",
        "sky130_fd_sc_hs",
        "sky130_fd_sc_ms",
        "sky130_fd_sc_ls",
        "sky130_fd_sc_lp",
        "sky130_fd_sc_hvl",
    ),
    "gf180mcuD": ("gf180mcu_fd_sc_mcu7t5v0", "gf180mcu_fd_sc_mcu9t5v0"),
    "ihp-sg13g2": ("sg13g2_stdcell",),
}


def pdk_root_with(pdk: str, library: str) -> Path | None:
    """A PDK root that has ``pdk``'s ``library``, or None."""
    base = Path(os.environ.get("PDK_ROOT", Path.home() / ".ciel"))
    for root in [base, *sorted((base / "ciel").glob("*/versions/*"))]:
        if (root / pdk / "libs.ref" / library / "lib").is_dir():
            return root
    return None


def installed() -> list[tuple[str, str]]:
    """Every (pdk, library) above, installed or not (a test skips the rest)."""
    return [(pdk, lib) for pdk, libs in LIBRARIES.items() for lib in libs]

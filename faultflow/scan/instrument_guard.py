"""Test instruments are never scan flops by accident.

A JTAG TAP and the IJTAG network behind it are test instruments: ``ff.py jtag`` tests
them through TCK with an integrity program, as industrial flows do, and scan ATPG
leaves them out (``[scan] nonscan_cells``, which ``autombist-generate --test-access``
and ``mbist-insert`` write by default). A flop that would land on a scan chain and is
one of them is refused here:

- clocked from the TAP's TCK (``[jtag] tck``): found by structure, whatever it is
  named;
- an instance of a JTAG category of the autoMBIST manifest (``jtag_tap``,
  ``ijtag_sib``, ``ijtag_tdr``, ``ijtag_scan_mux``), as
  ``autombist.access_nonscan_settings`` names them;
- a cell warptap inserted (``warptap_*``, which survives synthesis of the cells it
  names).

``[scan] scan_test_access = true`` (``--scan-tap``) says they are scan flops on
purpose; ``ff.py scan`` then records them in the scan manifest
(``test_access_scanned``) instead of refusing.
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from faultflow.control_trace import port_bits
from faultflow.scan.errors import ScanError

WARPTAP_CELL = re.compile(r"(^|__)warptap_")
LISTED = 5  # how many instruments a refusal names


def manifest_instrument_globs(autombist_manifest: Path | None) -> tuple[str, ...]:
    """The instance globs of the JTAG-category instances of an autoMBIST manifest
    (``<instance path>__*``, the path matched literally): none without a manifest, or
    one of a design that isn't wrapped for JTAG access."""
    if autombist_manifest is None:
        return ()
    from faultflow.integrations.autombist import (
        AutombistManifestError,
        load_autombist_manifest,
        access_nonscan_settings,
    )

    try:
        globs, _ = access_nonscan_settings(load_autombist_manifest(autombist_manifest))
    except (AutombistManifestError, OSError, ValueError):
        return ()
    return globs


def instrument_flops(
    cells: Iterable[tuple[str, int]],
    module: Mapping[str, Any],
    *,
    tck: str,
    globs: Sequence[str] = (),
) -> list[str]:
    """The test instruments among scan flops ``cells`` -- (instance, clock net) --
    of ``module``: those clocked from the input port ``tck``, matching one of
    ``globs``, or named like a warptap cell. Sorted by name."""
    tck_nets = {net for name, net in port_bits(module, "input") if name == tck}
    found = {
        instance
        for instance, clock in cells
        if clock in tck_nets
        or WARPTAP_CELL.search(instance)
        or any(fnmatch.fnmatchcase(instance, glob) for glob in globs)
    }
    return sorted(found)


def check_test_instruments(
    cells: Iterable[tuple[str, int]],
    module: Mapping[str, Any],
    *,
    tck: str,
    globs: Sequence[str] = (),
    allow: bool = False,
) -> list[str]:
    """Refuse (:class:`ScanError`) the test instruments among scan flops ``cells``
    (see :func:`instrument_flops`), unless ``allow``: ``[scan] scan_test_access``,
    which returns them instead. Returns the instruments found (none: ``[]``)."""
    found = instrument_flops(cells, module, tck=tck, globs=globs)
    if found and not allow:
        listed = ", ".join(found[:LISTED]) + (
            f" and {len(found) - LISTED} more" if len(found) > LISTED else ""
        )
        raise ScanError(
            f"test instruments on a scan chain: {listed}. A JTAG TAP and the IJTAG "
            "network are tested through TCK by ff.py jtag, not by scan ATPG: keep "
            "them out of scan with [scan] nonscan_cells (autombist-generate "
            "--test-access and mbist-insert write it) and hold trst_n and tck, or "
            "set [scan] scan_test_access = true (--scan-tap) to scan them on purpose"
        )
    return found

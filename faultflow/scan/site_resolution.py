from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Mapping, Sequence

from faultflow.coverage.site_key import SiteProvenance, canonical_site_key


def build_site_key_index(
    core: Any,
    json_path: str | Path,
    cell_map_path: str | Path,
    unsupported: str,
    blackbox_instances: Sequence[str] = (),
) -> dict[str, int]:
    rows = core.list_site_keys(
        str(json_path), str(cell_map_path), unsupported, list(blackbox_instances)
    )
    return {str(row["site_key"]): int(row["compiled_net_index"]) for row in rows}


def fault_type_to_sa_code(fault_type: str) -> int:
    if fault_type == "sa0":
        return 0
    if fault_type == "sa1":
        return 1
    raise ValueError(f"unexpected fault_type {fault_type!r}; expected 'sa0' or 'sa1'")


def _rows_by_key(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(row["site_key"]): dict(row) for row in rows}


def _compiled_by_stem_yid(rows: list[dict[str, Any]]) -> dict[int, int]:
    return {
        int(row["yosys_net_id"]): int(row["compiled_net_index"])
        for row in rows
        if str(row["kind"]) == "stem"
    }


def _wbr_observe_sites(
    view_cells: Mapping[str, Any],
    reduced_by_key: Mapping[str, Mapping[str, Any]],
    reduced_stems: Mapping[int, int],
    exact_stems: set[int],
) -> dict[str, int]:
    """Wrapper output cell instance -> the view site its INTEST capture reads:
    the branch into its `$wbobserve_` buffer, or that buffer's input stem when
    the buffer is the net's only observer (`exact_stems`)."""
    sites: dict[str, int] = {}
    prefix = "$wbobserve_"
    for name, cell in view_cells.items():
        if not name.startswith(prefix) or not isinstance(cell, dict):
            continue
        bits = cell.get("connections", {}).get("A", [])
        if len(bits) != 1 or not isinstance(bits[0], int):
            continue
        net = bits[0]
        branch = reduced_by_key.get(
            canonical_site_key(
                SiteProvenance(
                    yosys_net_id=net,
                    kind="branch",
                    consumer_instance=name,
                    input_pin="A",
                )
            )
        )
        if branch is not None:
            sites[name[len(prefix) :]] = int(branch["compiled_net_index"])
        elif net in exact_stems and net in reduced_stems:
            sites[name[len(prefix) :]] = reduced_stems[net]
    return sites


def build_scan_execution_map(
    core: Any,
    generic_json_path: str | Path,
    reduced_json_path: str | Path,
    generic_cell_map: str | Path,
    reduced_cell_map: str | Path,
    unsupported: str,
    pseudo_port_map: dict[str, dict[str, Any]],
    manifest: Mapping[str, Any],
    wbr_decoupled_bits: frozenset[int] = frozenset(),
    blackbox_instances: Sequence[str] = (),
) -> tuple[dict[str, int], dict[str, str]]:
    # `blackbox_instances` are the GENERIC netlist's: the reduced view models
    # them opaque (atpg_view._model_blackboxes_opaque) and contains none.
    generic_rows: list[dict[str, Any]] = list(
        core.list_site_keys(
            str(generic_json_path),
            str(generic_cell_map),
            unsupported,
            list(blackbox_instances),
        )
    )
    reduced_rows: list[dict[str, Any]] = list(
        core.list_site_keys(str(reduced_json_path), str(reduced_cell_map), unsupported)
    )
    reduced_by_key = _rows_by_key(reduced_rows)
    reduced_stems = _compiled_by_stem_yid(reduced_rows)
    # View nets that fan out (their readers get branch sites), and view nets an
    # output port observes: a stem stands for one reader's branch only when
    # its net is neither.
    reduced_fanout_yids = {
        int(row["yosys_net_id"]) for row in reduced_rows if str(row["kind"]) == "branch"
    }
    reduced_data: dict[str, Any] = json.loads(
        Path(reduced_json_path).read_text(encoding="utf-8")
    )
    reduced_output_yids = {
        bit
        for port in reduced_data["modules"][str(manifest["top"])]
        .get("ports", {})
        .values()
        if isinstance(port, dict) and port.get("direction") == "output"
        for bit in port.get("bits", [])
        if isinstance(bit, int)
    }
    execution: dict[str, int] = {}
    exclusions: dict[str, str] = {}

    q_boundaries: dict[int, dict[str, Any]] = {}
    d_boundaries: dict[str, int] = {}
    # Async control (RESET_B/SET_B) branch site -> reduced-view observe net.
    # The scan FF cell is removed from the reduced view (replaced by the capture
    # mux), so its control-pin branch fault has no direct reduced consumer; it is
    # graded at the dedicated control branch-buffer net the mux reads.
    control_boundaries: dict[str, int] = {}
    scan_instances: set[str] = set()
    for instance, entry in pseudo_port_map.items():
        scan_instances.add(instance)
        boundary = dict(entry["boundary"])
        q_yid = int(str(boundary["q_stem_site_key"]).split(":")[1])
        q_boundaries[q_yid] = entry
        d_boundaries[str(boundary["d_boundary_site_key"])] = int(
            boundary["d_observe_net_id"]
        )
        if boundary.get("control_boundary_site_key") is not None:
            control_boundaries[str(boundary["control_boundary_site_key"])] = int(
                boundary["control_observe_net_id"]
            )

    generic_data: dict[str, Any] = json.loads(
        Path(generic_json_path).read_text(encoding="utf-8")
    )
    module = generic_data["modules"][str(manifest["top"])]
    scan_chain_yids: set[int] = set()
    for name in [str(value) for value in manifest.get("scan_inputs", [])]:
        port = module.get("ports", {}).get(name, {})
        scan_chain_yids.update(
            bit for bit in port.get("bits", []) if isinstance(bit, int)
        )
    scan_internal_yids: set[int] = set()
    for name in [str(manifest["scan_enable"])]:
        port = module.get("ports", {}).get(name, {})
        scan_internal_yids.update(
            bit for bit in port.get("bits", []) if isinstance(bit, int)
        )

    # IEEE 1500 wrapper boundary cells (buffer- and scan-model). A scan-model cell
    # lowers (in the C++ normalizer) to an FF half + a `$wbrmux` half, so its core
    # net fans out to both and its branch sites have no consumer in the fused view
    # (the cell is removed). Grade those branches at the core net itself -- the
    # fused view preserves it as a stem (PPI-driven for inputs, observe-tapped for
    # outputs). The cell's scan-chain nets (CTI/SE/CTO) are wrapper infrastructure
    # and leave the denominator (tagged scan_chain). Buffer cells carry no chain
    # nets, so this is a no-op for them.
    from faultflow.scan.wbr_view import extract_wbr_cells, extract_wbr_chain_bits

    wbr_records = extract_wbr_cells(module)
    wbr_core_bits = {rec.core_net for rec in wbr_records}
    wbr_instances = {rec.instance for rec in wbr_records}
    view_cells = reduced_data["modules"][str(manifest["top"])].get("cells", {})
    wbr_observe = _wbr_observe_sites(
        view_cells,
        reduced_by_key,
        reduced_stems,
        {
            yid
            for yid in reduced_stems
            if yid not in reduced_fanout_yids and yid not in reduced_output_yids
        },
    )
    # Use extract_wbr_chain_bits (not just rec.chain_nets) so that chain nets
    # from constant-FROM_CORE WBR out cells are included: those cells are
    # skipped by extract_wbr_cells but the C++ still lowers them and emits
    # their CTO/CTI/SE fault sites in the generic site-key listing.
    wbr_chain_bits: set[int] = extract_wbr_chain_bits(module)

    for raw_row in generic_rows:
        row = dict(raw_row)
        key = str(row["site_key"])
        yid = int(row["yosys_net_id"])
        kind = str(row["kind"])
        # INTEST/EXTEST: system-side nets decoupled from the fused view have no
        # reduced mapping and must be excluded rather than raising boundary_map_missing.
        if yid in wbr_decoupled_bits:
            exclusions[key] = "wbr_decoupled"
            continue
        if yid in scan_chain_yids:
            exclusions[key] = "scan_chain"
            continue
        if yid in scan_internal_yids:
            exclusions[key] = "scan_internal"
            continue
        if yid in wbr_chain_bits:
            # Wrapper scan-chain infrastructure (wbr_si/se/so + inter-cell chain).
            # The combinational INTEST view makes the boundary directly
            # controllable/observable and abstracts the shift path away, so these
            # leave the denominator -- exactly like the main chain's SDI nets.
            exclusions[key] = "scan_chain"
            continue
        if (
            kind == "branch"
            and str(row.get("consumer_instance", "")) in scan_instances
            and str(row.get("input_pin", "")) == "SDI"
        ):
            exclusions[key] = "scan_internal"
            continue
        direct = reduced_by_key.get(key)
        if direct is not None:
            execution[key] = int(direct["compiled_net_index"])
            continue
        # A branch into a removed scan flop's D or async control: graded where
        # that flop's capture reads it. Before the Q block -- the branch's net
        # may be another scan flop's Q -- and before the WBR rules, which would
        # otherwise take a core net's D branch for the core net.
        if kind == "branch":
            observe_yid = d_boundaries.get(key, control_boundaries.get(key))
            if observe_yid is not None:
                if observe_yid in reduced_stems:
                    execution[key] = reduced_stems[observe_yid]
                continue
        # A branch into a removed wrapper cell (either half of its two-node
        # lowering): graded where the fused view's INTEST capture reads the core
        # net for that cell -- for a scan flop's Q, a view net with another id.
        # Without an observe site, at the core net if the view keeps it.
        if kind == "branch" and yid in wbr_core_bits:
            wbr_instance = str(row.get("consumer_instance", "")).removesuffix("$wbrmux")
            if wbr_instance in wbr_instances:
                if wbr_instance in wbr_observe:
                    execution[key] = wbr_observe[wbr_instance]
                elif yid in reduced_stems:
                    execution[key] = reduced_stems[yid]
                continue
        # Any other unmatched site on a WBR core net: the core net, which the
        # fused view keeps as a stem.
        if yid in wbr_core_bits and yid in reduced_stems:
            execution[key] = reduced_stems[yid]
            continue
        q_entry = q_boundaries.get(yid)
        if q_entry is not None:
            if kind == "stem":
                ppi_yid = int(q_entry["ppi_net_id"])
                if ppi_yid in reduced_stems:
                    execution[key] = reduced_stems[ppi_yid]
                continue
            # The flop's readers read its PPI, or an async flop's control mux.
            q_drive_yid = int(q_entry["q_drive_net_id"])
            translated = canonical_site_key(
                SiteProvenance(
                    yosys_net_id=q_drive_yid,
                    kind="branch",
                    consumer_instance=str(row["consumer_instance"]),
                    input_pin=str(row["input_pin"]),
                )
            )
            translated_row = reduced_by_key.get(translated)
            if translated_row is not None:
                execution[key] = int(translated_row["compiled_net_index"])
            elif (
                q_drive_yid in reduced_stems
                and q_drive_yid not in reduced_fanout_yids
                and q_drive_yid not in reduced_output_yids
            ):
                # Without the scan-in the view dropped, this reader is the
                # net's only observer: its stem is the branch. Otherwise the
                # site stays unmapped, and apply_scan_execution_map says so.
                execution[key] = reduced_stems[q_drive_yid]
            continue

    return execution, exclusions


def apply_scan_execution_map(
    conn: sqlite3.Connection,
    campaign_id: int,
    execution: dict[str, int],
    exclusions: dict[str, str],
    *,
    jtag_sites: set[str] | frozenset[str] = frozenset(),
    jtag_faults: frozenset[tuple[str, str]] = frozenset(),
) -> None:
    """Write the view site of every fault ATPG grades and the exclusion of every
    fault it doesn't. ``jtag_sites`` (both faults of each site) and ``jtag_faults``
    ((site key, "sa0"/"sa1") pairs) are left to JTAG (scan/nonscan.py): only an
    uncollapsed fault nothing else excludes is tagged, so it is counted once."""
    conn.execute(
        "UPDATE faults SET atpg_compiled_net_index = NULL WHERE campaign_id = ?",
        (campaign_id,),
    )
    conn.executemany(
        """
        UPDATE faults
        SET atpg_compiled_net_index = ?
        WHERE campaign_id = ? AND fault_site_key = ?
        """,
        [
            (compiled_index, campaign_id, key)
            for key, compiled_index in execution.items()
        ],
    )
    conn.executemany(
        """
        UPDATE faults
        SET exclusion = ?, excluded = ?, status = 'excluded',
            atpg_compiled_net_index = NULL
        WHERE campaign_id = ? AND fault_site_key = ?
        """,
        [
            (exclusion, exclusion, campaign_id, key)
            for key, exclusion in exclusions.items()
        ],
    )
    jtag_update = """
        UPDATE faults
        SET exclusion = 'jtag', excluded = 'jtag', status = 'excluded',
            atpg_compiled_net_index = NULL
        WHERE campaign_id = ? AND fault_site_key = ?
          AND exclusion = 'none' AND collapsed_into IS NULL
        """
    conn.executemany(jtag_update, [(campaign_id, key) for key in sorted(jtag_sites)])
    conn.executemany(
        jtag_update + " AND lower(fault_type) = ?",
        [(campaign_id, key, fault_type) for key, fault_type in sorted(jtag_faults)],
    )
    missing = conn.execute(
        """
        SELECT fault_site_key
        FROM faults
        WHERE campaign_id = ?
          AND exclusion = 'none'
          AND collapsed_into IS NULL
          AND atpg_compiled_net_index IS NULL
        ORDER BY fault_site_key
        LIMIT 1
        """,
        (campaign_id,),
    ).fetchone()
    if missing is not None:
        raise RuntimeError(
            "boundary_map_missing: scan ATPG execution mapping missing for "
            f"canonical site {missing['fault_site_key']}"
        )
    conn.commit()

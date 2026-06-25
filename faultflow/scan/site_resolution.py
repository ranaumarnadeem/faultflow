from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Mapping

from faultflow.coverage.site_key import SiteProvenance, canonical_site_key


def build_site_key_index(
    core: Any,
    json_path: str | Path,
    cell_map_path: str | Path,
    unsupported: str,
) -> dict[str, int]:
    rows = core.list_site_keys(str(json_path), str(cell_map_path), unsupported)
    return {str(row["site_key"]): int(row["compiled_net_index"]) for row in rows}


def fault_type_to_sa_code(fault_type: str) -> int:
    return 0 if fault_type == "sa0" else 1


def _rows_by_key(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(row["site_key"]): dict(row) for row in rows}


def _compiled_by_stem_yid(rows: list[dict[str, Any]]) -> dict[int, int]:
    return {
        int(row["yosys_net_id"]): int(row["compiled_net_index"])
        for row in rows
        if str(row["kind"]) == "stem"
    }


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
) -> tuple[dict[str, int], dict[str, str]]:
    generic_rows: list[dict[str, Any]] = list(
        core.list_site_keys(str(generic_json_path), str(generic_cell_map), unsupported)
    )
    reduced_rows: list[dict[str, Any]] = list(
        core.list_site_keys(str(reduced_json_path), str(reduced_cell_map), unsupported)
    )
    reduced_by_key = _rows_by_key(reduced_rows)
    reduced_stems = _compiled_by_stem_yid(reduced_rows)
    execution: dict[str, int] = {}
    exclusions: dict[str, str] = {}

    q_boundaries: dict[int, dict[str, Any]] = {}
    d_boundaries: dict[str, int] = {}
    scan_instances: set[str] = set()
    for instance, entry in pseudo_port_map.items():
        scan_instances.add(instance)
        boundary = dict(entry["boundary"])
        q_yid = int(str(boundary["q_stem_site_key"]).split(":")[1])
        q_boundaries[q_yid] = entry
        d_boundaries[str(boundary["d_boundary_site_key"])] = int(
            boundary["d_observe_net_id"]
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
        # WBR boundary core net: branches into the removed wrapper cell (and its
        # two-node lowering) have no reduced consumer; grade them at the core net,
        # which the fused view keeps as a stem.
        if yid in wbr_core_bits and yid in reduced_stems:
            execution[key] = reduced_stems[yid]
            continue
        q_entry = q_boundaries.get(yid)
        if q_entry is not None:
            ppi_yid = int(q_entry["ppi_net_id"])
            if kind == "stem":
                if ppi_yid in reduced_stems:
                    execution[key] = reduced_stems[ppi_yid]
                continue
            translated = canonical_site_key(
                SiteProvenance(
                    yosys_net_id=ppi_yid,
                    kind="branch",
                    consumer_instance=str(row["consumer_instance"]),
                    input_pin=str(row["input_pin"]),
                )
            )
            translated_row = reduced_by_key.get(translated)
            if translated_row is not None:
                execution[key] = int(translated_row["compiled_net_index"])
            elif ppi_yid in reduced_stems:
                # Removing the scan FF can reduce the PPI fanout to one. The
                # original functional branch then compiles as the PPI stem.
                execution[key] = reduced_stems[ppi_yid]
            continue
        d_observe_yid = d_boundaries.get(key)
        if d_observe_yid is not None and d_observe_yid in reduced_stems:
            execution[key] = reduced_stems[d_observe_yid]

    return execution, exclusions


def apply_scan_execution_map(
    conn: sqlite3.Connection,
    campaign_id: int,
    execution: dict[str, int],
    exclusions: dict[str, str],
) -> None:
    conn.execute(
        "UPDATE faults SET atpg_compiled_net_index = NULL WHERE campaign_id = ?",
        (campaign_id,),
    )
    for key, compiled_index in execution.items():
        conn.execute(
            """
            UPDATE faults
            SET atpg_compiled_net_index = ?
            WHERE campaign_id = ? AND fault_site_key = ?
            """,
            (compiled_index, campaign_id, key),
        )
    for key, exclusion in exclusions.items():
        conn.execute(
            """
            UPDATE faults
            SET exclusion = ?, excluded = ?, status = 'excluded',
                atpg_compiled_net_index = NULL
            WHERE campaign_id = ? AND fault_site_key = ?
            """,
            (exclusion, exclusion, campaign_id, key),
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

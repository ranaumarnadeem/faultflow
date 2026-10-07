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


def build_scan_execution_map(
    core: Any,
    generic_json_path: str | Path,
    reduced_json_path: str | Path,
    generic_cell_map: str | Path,
    reduced_cell_map: str | Path,
    unsupported: str,
    pseudo_port_map: dict[str, dict[str, Any]],
    manifest: Mapping[str, Any],
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

    for raw_row in generic_rows:
        row = dict(raw_row)
        key = str(row["site_key"])
        yid = int(row["yosys_net_id"])
        kind = str(row["kind"])
        if yid in scan_chain_yids:
            exclusions[key] = "scan_chain"
            continue
        if yid in scan_internal_yids:
            exclusions[key] = "scan_internal"
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
        # that flop's capture reads it. Before the Q block: the branch's net may
        # be another scan flop's Q.
        if kind == "branch":
            observe_yid = d_boundaries.get(key, control_boundaries.get(key))
            if observe_yid is not None:
                if observe_yid in reduced_stems:
                    execution[key] = reduced_stems[observe_yid]
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
    reset_sites: set[str] | frozenset[str] = frozenset(),
    decoupled_faults: frozenset[tuple[str, str]] = frozenset(),
) -> None:
    """Write the view site of every fault ATPG grades and the exclusion of every
    fault it doesn't. ``reset_sites`` (both faults of each site: a settled non-scan
    flop's, scan/nonscan.py) are reset faults; ``jtag_sites`` (both faults of each
    site) and ``jtag_faults`` ((site key, "sa0"/"sa1") pairs) are left to JTAG;
    ``decoupled_faults`` (pairs too) to the other IEEE 1500 wrapper test mode
    (faultflow.wrap.sides). Each tags only an uncollapsed fault nothing else
    excludes, so it is counted once."""
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
    conn.executemany(
        """
        UPDATE faults
        SET exclusion = 'reset', excluded = 'reset', status = 'excluded',
            atpg_compiled_net_index = NULL
        WHERE campaign_id = ? AND fault_site_key = ?
          AND exclusion = 'none' AND collapsed_into IS NULL
        """,
        [(campaign_id, key) for key in sorted(reset_sites)],
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
    conn.executemany(
        """
        UPDATE faults
        SET exclusion = 'wbr_decoupled', excluded = 'wbr_decoupled',
            status = 'excluded', atpg_compiled_net_index = NULL
        WHERE campaign_id = ? AND fault_site_key = ? AND lower(fault_type) = ?
          AND exclusion = 'none' AND collapsed_into IS NULL
        """,
        [(campaign_id, key, ftype) for key, ftype in sorted(decoupled_faults)],
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


def mark_blackbox_unresolved(
    conn: sqlite3.Connection, campaign_id: int, faults: frozenset[tuple[str, str]]
) -> int:
    """Mark each (site key, "sa0"/"sa1") of ``faults`` blackbox_unresolved before
    any test is graded: no test may credit it, since its effect lets an unknown
    value in. Only a fault ATPG would grade -- uncollapsed, excluded by nothing,
    not detected. How many it marked."""
    before = conn.total_changes
    conn.executemany(
        """
        UPDATE faults SET blackbox_unresolved = 1
        WHERE campaign_id = ? AND fault_site_key = ? AND lower(fault_type) = ?
          AND exclusion = 'none' AND collapsed_into IS NULL
          AND status != 'detected'
        """,
        [(campaign_id, key, ftype) for key, ftype in sorted(faults)],
    )
    conn.commit()
    return conn.total_changes - before

"""Wave-based parallel SAT-ATPG worker.

A single ``solve_fault_worker`` call maps to exactly one C++ solver call.
Workers write nothing to the database — the coordinator owns all mutations.

The C++ extension is imported inside the function body (not at module level)
so it is only loaded in the forked child, not serialized through the pipe.
This also avoids triggering the graph-cache load in the coordinator before
it has had a chance to warm the cache explicitly.

Solve kinds
-----------
"scan_stuck_at"         core.solve_fault_atpg (fused-view, no extras)
"broadside_transition"  core.solve_scan_transition_fault_atpg
"los_transition"        core.solve_scan_los_transition_fault_atpg
"native_stuck_at"       core.solve_fault_atpg (full signature with bb/test_mode)
"native_transition"     core.solve_transition_fault_atpg (combinational broadside)
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


def solve_fault_worker(args: tuple) -> tuple[int, str, dict[str, Any]]:
    """Solve one fault; return (fault_id, result_str, solved_dict).

    ``args`` is a 15-element tuple so multiprocessing can pickle it without
    any process-local state.  All values must be plain Python scalars or
    lists — no Path objects, no dataclasses.

    The returned ``solved_dict`` is the raw dict from the C++ solver.  For
    SAT results it contains 'vector' (stuck-at), 'launch'+'capture' (LOS),
    or 'launch' (broadside).  UNSAT/TIMEOUT/UNKNOWN dicts have no extra keys.
    On an unexpected exception the result is UNKNOWN with a '_exc' entry.
    """
    (
        solve_kind,
        json_path,
        cell_map_path,
        db_path,
        fault_id,
        blocked,
        conflict_limit,
        timeout,
        unsupported,
        cone_restrict,
        los_couple_ports,
        los_head_ports,
        bb_instances,
        test_mode,
        incremental,
    ) = args

    # The C extension lives in build/src/core/, not on the default sys.path.
    # Mirror the path search from runner._load_core() so forked workers can
    # find _faultflow_core regardless of whether fork() inherited a warm cache.
    _repo_root = Path(__file__).resolve().parents[2]
    for _candidate in [
        _repo_root / "build/src/core",
        _repo_root / "build",
        _repo_root,
        Path("build/src/core"),
        Path("build"),
    ]:
        if _candidate.exists():
            _p = str(_candidate.resolve())
            if _p not in sys.path:
                sys.path.insert(0, _p)
    import _faultflow_core as _core  # type: ignore[import-not-found]

    try:
        if solve_kind == "los_transition":
            solved: dict[str, Any] = dict(
                _core.solve_scan_los_transition_fault_atpg(
                    json_path,
                    cell_map_path,
                    db_path,
                    fault_id,
                    los_couple_ports,
                    los_head_ports,
                    blocked,
                    conflict_limit,
                    timeout,
                    unsupported,
                    cone_restrict=cone_restrict,
                )
            )
        elif solve_kind == "broadside_transition":
            solved = dict(
                _core.solve_scan_transition_fault_atpg(
                    json_path,
                    cell_map_path,
                    db_path,
                    fault_id,
                    blocked,
                    conflict_limit,
                    timeout,
                    unsupported,
                    cone_restrict=cone_restrict,
                )
            )
        elif solve_kind == "native_stuck_at":
            solved = dict(
                _core.solve_fault_atpg(
                    json_path,
                    cell_map_path,
                    db_path,
                    fault_id,
                    blocked,
                    conflict_limit,
                    timeout,
                    unsupported,
                    bb_instances,
                    test_mode,
                    cone_restrict,
                    incremental,
                )
            )
        elif solve_kind == "native_transition":
            solved = dict(
                _core.solve_transition_fault_atpg(
                    json_path,
                    cell_map_path,
                    db_path,
                    fault_id,
                    blocked,
                    conflict_limit,
                    timeout,
                    unsupported,
                    bb_instances,
                    cone_restrict,
                )
            )
        else:  # "scan_stuck_at" (fused view; stays on the baseline solver for now)
            solved = dict(
                _core.solve_fault_atpg(
                    json_path,
                    cell_map_path,
                    db_path,
                    fault_id,
                    blocked,
                    conflict_limit,
                    timeout,
                    unsupported,
                )
            )
    except Exception as exc:
        return (fault_id, "UNKNOWN", {"result": "UNKNOWN", "_exc": str(exc)})

    return (fault_id, str(solved.get("result", "UNKNOWN")), solved)

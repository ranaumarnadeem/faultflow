"""Run combinational INTEST and EXTEST fault simulation on the REAL wrapped
PicoRV32a (102 wbc_in + 307 wbc_out IEEE 1500 shadow/boundary cells) and show
the mode role-flip at scale.

This exercises the mode-aware engine (build_mode_config + simulate_batch(mc)) on
the 12.9k-cell wrapped design:
  * INTEST: stimulus = wbc_in core nets (__core_*); observe = wbc_out core nets.
  * EXTEST: stimulus = wbc_out sys nets (__sys_*) + top PIs; observe = wbc_in sys
    nets + top POs.

PicoRV32a is sequential; this is a COMBINATIONAL pass (flip-flops seeded 0), so
the coverage numbers are illustrative of the mechanism, not a full campaign --
that needs the scan-integrated fused view wired through the CLI. The point here
is: the modes load + reconfigure control/observe + grade faults on the real
shadow-cell-wrapped design, and INTEST vs EXTEST detect DIFFERENT fault sets.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, ".")
sys.path.insert(0, "tests/python")

from faultflow.db import connect, init_schema  # noqa: E402
from faultflow.runner.runner import _load_core, _port_names  # noqa: E402
from db_v3_helpers import insert_campaign  # type: ignore  # noqa: E402

WRAPPED = "examples/picorv32_synth/picorv32a_sky130_wrapped.json"
CELL_MAP = "cells/sky130/sky130_fd_sc_hd.json"
TOP = "picorv32a"
SEED = 0xC0FFEE
N_VECTORS = 16


def _detected(db: str, campaign_id: int) -> set[tuple[str, str]]:
    conn = connect(db)
    init_schema(conn)
    rows = conn.execute(
        "SELECT net_name, fault_type FROM faults "
        "WHERE campaign_id = ? AND status = 'detected'",
        (campaign_id,),
    ).fetchall()
    conn.close()
    return {(str(r["net_name"]), str(r["fault_type"])) for r in rows}


def run_mode(core, mode: str, input_order: list[str], db: str) -> tuple[dict, set]:
    conn: sqlite3.Connection = connect(db)
    init_schema(conn)
    campaign_id = insert_campaign(conn, top=TOP)
    conn.commit()
    conn.close()

    vectors = list(core.atpg_random_vectors(input_order, N_VECTORS, SEED))
    started = time.perf_counter()
    summary = dict(
        core.simulate_to_db(
            WRAPPED,
            CELL_MAP,
            db,
            campaign_id,
            vectors,
            input_order,
            f"{mode}_demo",
            unsupported_policy="blackbox",
            test_mode=mode,
        )
    )
    elapsed = time.perf_counter() - started
    summary["_seconds"] = round(elapsed, 1)
    summary["_stimulus_nets"] = len(input_order)
    return summary, _detected(db, campaign_id)


def main() -> None:
    core = _load_core()
    if core is None:
        print("ERROR: C++ core not available")
        sys.exit(1)

    net = json.loads(Path(WRAPPED).read_text())
    mod = net["modules"][TOP]
    netnames = mod.get("netnames", {})
    core_nets = sorted(n for n in netnames if n.startswith("__core_"))
    sys_nets = sorted(n for n in netnames if n.startswith("__sys_"))
    top_pis = _port_names(Path(WRAPPED), TOP, "input")

    print(f"wrapped {TOP}: {len(mod.get('cells', {}))} cells")
    print(f"  INTEST stimulus (__core_*): {len(core_nets)} nets")
    print(f"  EXTEST stimulus (__sys_* + top PIs): {len(sys_nets)} + {len(top_pis)}")
    print()

    with tempfile.TemporaryDirectory() as tmp:
        intest_summary, intest_det = run_mode(
            core, "intest", core_nets, str(Path(tmp) / "intest.db")
        )
        extest_summary, extest_det = run_mode(
            core, "extest", sys_nets + top_pis, str(Path(tmp) / "extest.db")
        )

    def show(label: str, s: dict) -> None:
        print(
            f"{label}: denominator={s.get('denominator')} "
            f"detected={s.get('detected')} "
            f"coverage={s.get('coverage_percent')}% "
            f"stimulus_nets={s.get('_stimulus_nets')} "
            f"({s.get('_seconds')}s)"
        )

    show("INTEST", intest_summary)
    show("EXTEST", extest_summary)
    print()
    print(f"INTEST-only detected faults : {len(intest_det - extest_det)}")
    print(f"EXTEST-only detected faults : {len(extest_det - intest_det)}")
    print(f"shared detected faults      : {len(intest_det & extest_det)}")
    role_flip = bool(intest_det - extest_det) and bool(extest_det - intest_det)
    print(f"\nROLE-FLIP (modes detect different fault sets): {role_flip}")


if __name__ == "__main__":
    main()

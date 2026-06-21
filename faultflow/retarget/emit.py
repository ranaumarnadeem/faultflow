"""Portable emit/reload of retargeted SoC scan patterns.

Schema ``faultflow_retargeted_v1``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from faultflow.retarget.transform import SocScanPattern

RETARGETED_SCHEMA = "faultflow_retargeted_v1"


def pattern_to_dict(
    pattern: SocScanPattern,
    *,
    assembly_top: str,
    soc_access_ref: str | None = None,
    credit: dict[str, Any] | None = None,
    provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema": RETARGETED_SCHEMA,
        "assembly_top": assembly_top,
        "source_block": pattern.source_block,
        "soc_access_ref": soc_access_ref,
        "block_chains": list(pattern.block_chains),
        "soc_pattern": {
            "load_seqs": {str(k): v for k, v in pattern.load_seqs.items()},
            "capture_pi_values": dict(pattern.capture_pi_values),
            "expected_unload": {str(k): v for k, v in pattern.expected_unload.items()},
            "unload_mask": {str(k): v for k, v in pattern.unload_mask.items()},
        },
        "credit": credit or {},
        "provenance": provenance or {},
    }


def dict_to_pattern(data: dict[str, Any]) -> SocScanPattern:
    if data.get("schema") != RETARGETED_SCHEMA:
        raise ValueError(
            f"unsupported schema {data.get('schema')!r}; expected {RETARGETED_SCHEMA!r}"
        )
    sp = data["soc_pattern"]

    def _ints(d: dict[str, Any]) -> dict[int, list[bool]]:
        return {int(k): [bool(b) for b in v] for k, v in d.items()}

    return SocScanPattern(
        load_seqs=_ints(sp["load_seqs"]),
        capture_pi_values={k: bool(v) for k, v in sp["capture_pi_values"].items()},
        expected_unload=_ints(sp["expected_unload"]),
        unload_mask=_ints(sp["unload_mask"]),
        source_block=str(data["source_block"]),
        block_chains=tuple(int(c) for c in data.get("block_chains", [])),
    )


def write_retargeted(path: str | Path, payload: dict[str, Any]) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return p


def read_retargeted(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))

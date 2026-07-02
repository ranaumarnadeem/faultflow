"""Path B oracle: translate an OT-shaped ofs, run ATPG, write oracle_response.json.

OT calls: faultflow run --config <run_dir>/faultflow.ofs
faultflow reads the manifest, runs init+sim, writes the flat oracle_response.json
that OT's bridge.read_oracle_response() consumes.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from configparser import ConfigParser
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_CELL_LIB = _REPO_ROOT / "cells/sky130/sky130_fd_sc_hd.json"
_SCHEMA_PATH = _REPO_ROOT / "schemas/oracle_response.schema.json"


class OracleResponseError(RuntimeError):
    """Raised when an oracle_response payload fails schema validation."""


_TERMINAL_MAP: dict[str, str] = {
    "target_reached": "target_reached",
    "exhausted": "exhausted",
    "stalled": "exhausted",
    "timeout": "timeout",
    "unknown": "timeout",
}


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def translate_oracle_ofs(ofs_path: Path) -> tuple[Any, str]:
    """Parse an OT-format ofs and return a (FaultflowConfig, top) pair.

    OT ofs sections consumed:
      [input]  netlist      - path to the post-TPI Yosys JSON
      [design] top_module   - design top name (optional fallback: 'top')
      [design] cell_lib     - cell library JSON (optional; defaults to sky130)
      [output] dir          - output directory for workspace + oracle_response
    """
    from faultflow.config import load_config

    cp = ConfigParser()
    cp.read(ofs_path, encoding="utf-8")

    netlist = Path(cp.get("input", "netlist"))
    top = cp.get("design", "top_module", fallback="top")
    output_dir = Path(cp.get("output", "dir", fallback=str(ofs_path.parent)))
    cell_lib = Path(cp.get("design", "cell_lib", fallback=str(_DEFAULT_CELL_LIB)))

    output_dir.mkdir(parents=True, exist_ok=True)
    native_ofs = output_dir / ".faultflow_native.ofs"
    native_ofs.write_text(
        f"[design]\nnetlist = {netlist}\ncell_lib = {cell_lib}\n\n"
        "[fault_model]\nmodel = stuck_at\n\n"
        "[report]\nthreshold = 95.0\n",
        encoding="utf-8",
    )

    cfg = load_config(native_ofs, top)
    cfg = dataclasses.replace(cfg, output_root=output_dir)
    return cfg, top


def discover_manifest(ofs_path: Path) -> dict[str, Any] | None:
    """Return the tpi_manifest.json beside ofs_path, or None if absent."""
    p = ofs_path.parent / "tpi_manifest.json"
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def map_terminal_reason(reason: str) -> str:
    """Map faultflow terminal reason to OT oracle enum value."""
    return _TERMINAL_MAP.get(reason, "exhausted")


def build_oracle_response(
    report: dict[str, Any],
    *,
    manifest: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build a flat oracle_response dict from a faultflow coverage report."""
    summary = report.get("summary", {})
    run = report.get("run", {})

    terminal_raw = str(run.get("atpg_terminal_reason") or "exhausted")
    fault_cov = float(summary.get("fault_coverage_percent", 0.0))

    resp: dict[str, Any] = {
        "backend": "faultflow",
        "coverage_percent": fault_cov,
        "fault_coverage_percent": fault_cov,
        "vector_count": int(run.get("vector_count", 0) or 0),
        "terminal_reason": map_terminal_reason(terminal_raw),
        "undetected_faults": report.get("undetected_faults", []),
        "per_node": report.get("per_node", []),
    }
    if manifest is not None:
        resp["session_id"] = manifest.get("session_id", "")
        resp["iteration"] = manifest.get("iteration", 0)
    return resp


def write_oracle_response(response: dict[str, Any], path: Path) -> None:
    """Validate against schema and write oracle_response.json.

    A schema validation failure is fatal: writing a malformed oracle_response.json
    to disk would silently hand OT's bridge a payload it cannot trust. Only the
    jsonschema-not-installed case (a genuinely optional dependency) is a soft skip.
    """
    try:
        import jsonschema  # type: ignore[import-untyped]
    except ImportError:
        log.debug("jsonschema not installed; skipping oracle response validation")
    else:
        if _SCHEMA_PATH.exists():
            schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
            try:
                jsonschema.validate(response, schema)
            except jsonschema.ValidationError as exc:
                raise OracleResponseError(
                    f"oracle response failed schema validation: {exc.message}"
                ) from exc

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(response, indent=2) + "\n", encoding="utf-8")
    log.info("oracle  response written: %s", path)


def run_oracle(ofs_path: Path) -> int:
    """Translate OT ofs → run ATPG → write oracle_response.json."""
    from faultflow.runner import Runner
    from faultflow.service.flow import FlowService

    cfg, _top = translate_oracle_ofs(ofs_path)
    manifest = discover_manifest(ofs_path)

    service = FlowService(runner_factory=Runner)
    log.info("oracle  init  top=%s", cfg.top)
    service.initialize(cfg)
    log.info("oracle  atpg  top=%s", cfg.top)
    service.run_atpg(cfg)

    report_path = cfg.coverage_json_path
    if not report_path.exists():
        log.error("oracle  coverage report not found: %s", report_path)
        return 1

    report = json.loads(report_path.read_text(encoding="utf-8"))
    response = build_oracle_response(report, manifest=manifest)

    if manifest and manifest.get("response_path"):
        response_path = Path(manifest["response_path"])
    else:
        response_path = cfg.output_root / "oracle_response.json"

    write_oracle_response(response, response_path)
    return 0

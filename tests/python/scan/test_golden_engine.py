"""The scan golden gate's fault-free replay of the full scan protocol
(faultflow.scan.verify) runs on the bit-parallel engine by default, and on the
scalar oracle with ``[scan] golden_engine = scalar``. The two agree bit for bit
(Catch2: "scan pattern on the bit-parallel engine == the scalar oracle")."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import faultflow.runner.runner as runner_mod
from faultflow.config import ConfigError, FaultflowConfig, load_config
from faultflow.runner.runner import RunnerError
from faultflow.scan.protocol import ScanPattern
from faultflow.scan.verify import verify_golden_scan_protocol

ROOT = Path(__file__).resolve().parents[3]
CELL_MAP = ROOT / "cells/sky130/sky130_fd_sc_hd.json"
TOP = "t"
MANIFEST: dict[str, Any] = {
    "top": TOP,
    "clock_net": 2,
    "clock_ports": ["clk"],
    "scan_enable": "se",
    "scan_inputs": ["sdi"],
    "scan_outputs": ["sdo"],
    "chains": [{"index": 0, "length": 2}],
    "max_chain_length": 2,
}


class _Core:
    """Either replay unloads ``unload`` on chain 0; ``calls`` names each one run."""

    def __init__(self, unload: list[bool]) -> None:
        self.unload = unload
        self.calls: list[str] = []

    def _replay(self, engine: str) -> dict[str, Any]:
        self.calls.append(engine)
        return {"unload_seqs": {0: self.unload}, "real_po_values": {}}

    def simulate_scan_pattern(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self._replay("scalar")

    def simulate_scan_pattern_bit_parallel(
        self, *args: Any, **kwargs: Any
    ) -> dict[str, Any]:
        return self._replay("bit_parallel")


def _config(tmp_path: Path, scan_text: str = "") -> FaultflowConfig:
    ofs = tmp_path / "config.ofs"
    ofs.write_text(
        f"[design]\ncell_lib = {CELL_MAP}\n\n[scan]\n{scan_text}", encoding="utf-8"
    )
    return load_config(ofs, TOP)


def _gate(
    cfg: FaultflowConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, core: _Core
) -> None:
    monkeypatch.setattr(runner_mod, "_load_core", lambda: core)
    verify_golden_scan_protocol(
        cfg,
        MANIFEST,
        tmp_path / "generic.json",
        ScanPattern(
            load_seqs={0: [False, False]},
            capture_pi_values={},
            expected_unload={0: [True, False]},
        ),
        vector_index=0,
        fault_id=None,
        reduced_vector={},
        functional_output_order=[],
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "scan_text, engine",
    [
        ("", "bit_parallel"),
        ("golden_engine = bit_parallel\n", "bit_parallel"),
        ("golden_engine = scalar\n", "scalar"),
    ],
)
def test_the_gate_replays_on_the_configured_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scan_text: str, engine: str
) -> None:
    cfg = _config(tmp_path, scan_text)
    assert cfg.scan.golden_engine == engine
    core = _Core([True, False])
    _gate(cfg, tmp_path, monkeypatch, core)
    assert core.calls == [engine]


@pytest.mark.unit
@pytest.mark.parametrize("scan_text", ["", "golden_engine = scalar\n"])
def test_a_mismatch_fails_the_gate_on_either_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scan_text: str
) -> None:
    with pytest.raises(RunnerError, match="unload"):
        _gate(_config(tmp_path, scan_text), tmp_path, monkeypatch, _Core([False, True]))


@pytest.mark.unit
def test_golden_engine_is_bit_parallel_or_scalar(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="golden_engine"):
        _config(tmp_path, "golden_engine = fast\n")

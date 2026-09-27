"""Real-Yosys integration test for the autoMBIST synthesis pipeline.

Loads a checked-in, real (captured from a live `autombist generate
--emit-manifest` run, then trimmed/scrubbed of machine-specific paths)
manifest + RTL fixture and drives `synthesize_from_manifest` (not
`run_autombist_generate` -- no live autoMBIST invocation is available or
needed against an already-captured manifest).

Departs from `tests/python/project/test_assemble.py`'s own inline-RTL local
convention deliberately: faithfully hand-writing a realistic
`manifest.json` (the real `hierarchy_hint`/`category`/`parameters`/`sources`/
`geometry` shape) as an inline Python dict literal would be more error-prone
and less representative than reusing a real, tiny, already-captured example.
`tests/fixtures/scan_protocol/single_chain/` is the direct precedent for a
checked-in manifest+companion-data fixture directory in this codebase.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from faultflow.config import load_config
from faultflow.integrations.autombist import (
    load_autombist_manifest,
    plan_blocks,
    synthesize_from_manifest,
)

ROOT = Path(__file__).resolve().parents[3]
FIXTURE = ROOT / "tests/fixtures/autombist/input_demo_8x16_scn4m"
SKY130_LIBERTY = ROOT / "cells/sky130/sky130_fd_sc_hd__tt_025C_1v80.lib"
SKY130_CELL_MAP = ROOT / "cells/sky130/sky130_fd_sc_hd.json"


@pytest.mark.integration
def test_synthesize_from_manifest_end_to_end(
    tmp_path: Path, require_cpp_core: None
) -> None:
    if shutil.which("yosys") is None:
        pytest.skip("yosys is not available")

    manifest = load_autombist_manifest(FIXTURE / "manifest.json")
    assert manifest.top_module == "input_demo_8x16_scn4m_mbist"
    assert len(manifest.instances) == 2

    blocks = plan_blocks(manifest)
    assert len(blocks) == 1
    assert blocks[0].module == "march_c_top"
    assert blocks[0].instance_paths == ("u_algo_top",)

    result = synthesize_from_manifest(
        manifest, out=tmp_path, liberty=SKY130_LIBERTY, cell_lib=SKY130_CELL_MAP
    )

    assert result.top_module == "input_demo_8x16_scn4m_mbist"
    assert result.blackbox_instances == ("u_sram",)
    assert result.block_count == 1
    assert result.instance_counts == {"memory": 1, "mbist_controller": 1}
    assert result.ofs_path.exists()
    assert result.composed_json_path.exists()

    # The .ofs round-trips through load_config with the memory blackboxed.
    cfg = load_config(result.ofs_path, "input_demo_8x16_scn4m_mbist")
    assert str(cfg.netlist) == str(result.composed_json_path)
    assert list(cfg.blackbox_instances) == ["u_sram"]

    # Composed JSON: both instances spliced in (u_sram's real blackbox cell
    # survives as its own cell since memories are never synthesized/spliced --
    # only "separate" blocks get spliced via compose_soc), no leftover glue
    # instance cell for the block, no $scopeinfo debug cells.
    composed = json.loads(result.composed_json_path.read_text(encoding="utf-8"))
    top = composed["modules"][result.top_module]
    cell_names = set(top["cells"].keys())
    assert "u_algo_top" not in cell_names  # instance cell removed by compose_soc
    assert any(
        name.startswith("u_algo_top__") for name in cell_names
    ), "expected u_algo_top's spliced cells"
    assert "u_sram" in cell_names  # memory blackbox cell untouched
    assert not any("scopeinfo" in name for name in cell_names)

    # The composed netlist loads through the C++ core (proves it's not just
    # well-formed JSON but a genuinely simulatable netlist).
    from faultflow.runner.runner import _load_core

    core = _load_core()
    assert core is not None
    # "blackbox" (not "fail"): u_sram's cell type is the memory's own module
    # name, not a sky130 cell -- Policy 1's blackbox path, matching the
    # written .ofs's [blackbox] section.
    rows = core.list_site_keys(
        str(result.composed_json_path), str(SKY130_CELL_MAP), "blackbox"
    )
    assert rows, "expected at least one fault site in the composed netlist"

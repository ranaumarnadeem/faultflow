"""Tests for check_compression_structure, using hand-built fixtures shaped
exactly like real Yosys/abc output (confirmed by direct inspection of real
insert_compression runs at widths 8 and 16 -- see compression_checks.py's
module docstring): Sky130 cell types with an EMPTY `port_directions` dict
(confirmed true for every real Sky130 library cell in this project's
synthesized JSON -- only the synthetic $scanff_faultflow splice cells
populate it), a single-tap chain optimized to a pure net alias (no gate at
all), and -- since check_compression_structure now also verifies the ring
generator's OWN feedback-tap/reseed-mux cone, not just the phase-shifter --
every enabled-compression fixture in this file needs a real (if simplified)
register cone attached, or the check has nothing to walk and every test
would fail on "declared 'lfsr_reg' net not found in netlist".

_REGISTER_CONE() builds that cone using ONE mux2 per bit (never ABC's
ghost-bit/XOR-cancellation optimization -- that specific fused shape is
exercised separately, against real Yosys output, in
test_compression_end_to_end.py) so each test here stays focused on ONE
thing at a time: either the phase-shifter's own cone (existing tests,
unchanged in intent) or the register cone's own recognition logic (new
tests below)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from faultflow.scan.compression_checks import check_compression_structure

# Fixed net-id layout for the register cone, shared by every test in this
# file: effective_state uses the SAME low net ids [1..8] the existing
# phase-shifter tests already reference (bit k -> net k+1), so a test that
# only cares about the phase-shifter can keep declaring
# effective_state/scan_in_N exactly as before; everything else (tdi,
# lfsr_reg, prev_scan_en, scan_en, reseed-select, next_state XOR outputs)
# lives in clearly out-of-the-way ranges.
_WIDTH = 8
_TAPS = {4, 5, 6}  # PRIMITIVE_POLYNOMIALS[8] -- see ring_generator.py
_EFFECTIVE_STATE_BITS = [1, 2, 3, 4, 5, 6, 7, 8]  # bit k -> net k+1
_TDI_BITS = [910, 911, 912, 913, 914, 915, 916, 917]
_LFSR_REG_BITS = [920, 921, 922, 923, 924, 925, 926, 927]
_CLK_NET = 900
_SCAN_EN_NET = 901
_PREV_SCAN_EN_NET = 902
_RESEED_SELECT_NET = 903
# next_state[0] = fb (net 8, alias); next_state[i] = effective_state[i-1]
# (alias) for i not in _TAPS; a fresh XOR-output net for i in _TAPS.
_NEXT_STATE_BITS = [8, 1, 2, 3, 60, 61, 62, 7]


def _register_cone_cells() -> dict:
    cells = {
        "u_prev_scan_en": {
            "type": "sky130_fd_sc_hd__dfxtp_1",
            "port_directions": {},
            "connections": {
                "CLK": [_CLK_NET],
                "D": [_SCAN_EN_NET],
                "Q": [_PREV_SCAN_EN_NET],
            },
        },
        "u_reseed_nand": {
            "type": "sky130_fd_sc_hd__nand2b_1",
            "port_directions": {},
            "connections": {
                "A_N": [_PREV_SCAN_EN_NET],
                "B": [_SCAN_EN_NET],
                "Y": [_RESEED_SELECT_NET],
            },
        },
    }
    for k in range(_WIDTH):
        cells[f"u_mux_{k}"] = {
            "type": "sky130_fd_sc_hd__mux2_1",
            "port_directions": {},
            "connections": {
                "A0": [_TDI_BITS[k]],
                "A1": [_LFSR_REG_BITS[k]],
                "S": [_RESEED_SELECT_NET],
                "X": [_EFFECTIVE_STATE_BITS[k]],
            },
        }
    fb_bit = _EFFECTIVE_STATE_BITS[_WIDTH - 1]
    for i in range(1, _WIDTH):
        if i in _TAPS:
            cells[f"u_tap_xor_{i}"] = {
                "type": "sky130_fd_sc_hd__xor2_1",
                "port_directions": {},
                "connections": {
                    "A": [_EFFECTIVE_STATE_BITS[i - 1]],
                    "B": [fb_bit],
                    "X": [_NEXT_STATE_BITS[i]],
                },
            }
    return cells


def _register_cone_netnames() -> dict:
    return {
        "effective_state": {"bits": _EFFECTIVE_STATE_BITS},
        "tdi": {"bits": _TDI_BITS},
        "lfsr_reg": {"bits": _LFSR_REG_BITS},
        "next_state": {"bits": _NEXT_STATE_BITS},
        "prev_scan_en": {"bits": [_PREV_SCAN_EN_NET]},
        "scan_en": {"bits": [_SCAN_EN_NET]},
    }


def _with_register_cone(module: dict) -> dict:
    """Merge a valid register cone into a phase-shifter-focused module dict
    -- `module` must already declare its own "effective_state" netnames
    entry using `_EFFECTIVE_STATE_BITS` (bit k -> net k+1) and any
    phase-shifter cells/netnames it needs; this adds everything else."""
    module = dict(module)
    module["cells"] = {**_register_cone_cells(), **module.get("cells", {})}
    module["netnames"] = {**_register_cone_netnames(), **module.get("netnames", {})}
    return module


def _base_compression(**overrides: object) -> dict:
    compression = {
        "enabled": True,
        "num_channels": _WIDTH,
        "tap_source_net": "effective_state",
        "scan_enable_port": "scan_en",
    }
    compression.update(overrides)
    return compression


def _xor2_cell(a: int, b: int, y: int, name: str = "u_xor") -> dict:
    return {
        name: {
            "type": "sky130_fd_sc_hd__xor2_1",
            "port_directions": {},
            "connections": {"A": [a], "B": [b], "X": [y]},
        }
    }


def _inv_cell(a: int, y: int, name: str = "u_inv") -> dict:
    return {
        name: {
            "type": "sky130_fd_sc_hd__inv_1",
            "port_directions": {},
            "connections": {"A": [a], "Y": [y]},
        }
    }


def _mux2_cell(a0: int, a1: int, s: int, y: int, name: str = "u_mux") -> dict:
    return {
        name: {
            "type": "sky130_fd_sc_hd__mux2_1",
            "port_directions": {},
            "connections": {"A0": [a0], "A1": [a1], "S": [s], "X": [y]},
        }
    }


def _manifest(tmp_path: Path, module: dict, compression: dict) -> dict:
    composed_json = tmp_path / "composed.json"
    composed_json.write_text(
        json.dumps({"modules": {"core_top_compressed": module}}), encoding="utf-8"
    )
    compression = dict(compression)
    if compression.get("enabled"):
        compression.setdefault("composed_json", str(composed_json))
        compression.setdefault("composed_top", "core_top_compressed")
    return {
        # The manifest's own "top" is the PRE-compression design name --
        # check_compression_structure never reads generic_json/this file at
        # all, only compression["composed_json"]/["composed_top"] (see the
        # compression CLI-wiring plan's "required fix" section).
        "top": "core_top",
        "compression": compression,
    }


@pytest.mark.unit
def test_check_compression_structure_noop_when_disabled(tmp_path: Path) -> None:
    manifest = _manifest(
        tmp_path,
        {"ports": {}, "cells": {}, "netnames": {}},
        {"enabled": False},
    )
    result = check_compression_structure(manifest)
    assert result.passed
    assert result.errors == []
    assert result.warnings == []


@pytest.mark.unit
def test_check_compression_structure_accepts_alias_and_xor_taps(
    tmp_path: Path,
) -> None:
    # effective_state = bits [1..8] (8 channels). chain0's tap is [0] --
    # Yosys optimizes a single-tap phase-shifter assign to a pure net alias
    # (net 1 IS scan_in_0, no gate at all). chain1's tap is [0, 1] -- a real
    # XOR2 gate combines bits 1 and 2 into a new net (50) that becomes
    # scan_in_1.
    module = {
        "ports": {},
        "netnames": {
            "effective_state": {"bits": _EFFECTIVE_STATE_BITS},
            "scan_in_0": {"bits": [1]},
            "scan_in_1": {"bits": [50]},
        },
        "cells": _xor2_cell(a=1, b=2, y=50),
    }
    compression = _base_compression(
        scan_in_ports=["scan_in_0", "scan_in_1"],
        phase_shifter_taps=[[0], [0, 1]],
    )
    manifest = _manifest(tmp_path, _with_register_cone(module), compression)

    result = check_compression_structure(manifest)

    assert result.passed, result.errors


@pytest.mark.unit
def test_check_compression_structure_detects_mismatched_taps(tmp_path: Path) -> None:
    module = {
        "ports": {},
        "netnames": {
            "effective_state": {"bits": _EFFECTIVE_STATE_BITS},
            "scan_in_0": {"bits": [50]},
        },
        "cells": _xor2_cell(a=1, b=2, y=50),
    }
    compression = _base_compression(
        scan_in_ports=["scan_in_0"],
        # Manifest claims scan_in_0 = bit 0 only, but the netlist actually
        # XORs bits 0 and 1 together.
        phase_shifter_taps=[[0]],
    )
    manifest = _manifest(tmp_path, _with_register_cone(module), compression)

    result = check_compression_structure(manifest)

    assert not result.passed
    assert any("do not match" in err for err in result.errors)


@pytest.mark.unit
def test_check_compression_structure_rejects_non_linear_gate_in_cone(
    tmp_path: Path,
) -> None:
    module = {
        "ports": {},
        "netnames": {
            "effective_state": {"bits": _EFFECTIVE_STATE_BITS},
            "scan_in_0": {"bits": [50]},
        },
        # A mux (non-linear, and NOT the reseed-select net) sits in
        # scan_in_0's cone instead of an XOR -- this must be flagged, not
        # silently accepted as "close enough".
        "cells": _mux2_cell(a0=1, a1=2, s=1, y=50),
    }
    compression = _base_compression(
        scan_in_ports=["scan_in_0"],
        phase_shifter_taps=[[0, 1]],
    )
    manifest = _manifest(tmp_path, _with_register_cone(module), compression)

    result = check_compression_structure(manifest)

    assert not result.passed
    assert any("non-linear gate" in err for err in result.errors)


@pytest.mark.unit
def test_check_compression_structure_rejects_inverted_cone(tmp_path: Path) -> None:
    module = {
        "ports": {},
        "netnames": {
            "effective_state": {"bits": _EFFECTIVE_STATE_BITS},
            "scan_in_0": {"bits": [50]},
        },
        "cells": _inv_cell(a=1, y=50),
    }
    compression = _base_compression(
        scan_in_ports=["scan_in_0"],
        phase_shifter_taps=[[0]],
    )
    manifest = _manifest(tmp_path, _with_register_cone(module), compression)

    result = check_compression_structure(manifest)

    assert not result.passed
    assert any("inverted" in err for err in result.errors)


@pytest.mark.unit
def test_check_compression_structure_rejects_undeclared_leaf(tmp_path: Path) -> None:
    module = {
        "ports": {},
        "netnames": {
            "effective_state": {"bits": _EFFECTIVE_STATE_BITS},
            "scan_in_0": {"bits": [50]},
        },
        # net 50 is driven by an XOR of net 1 and net 99 -- net 99 is not an
        # effective_state bit and has no driver of its own -- undeclared leaf.
        "cells": _xor2_cell(a=1, b=99, y=50),
    }
    compression = _base_compression(
        scan_in_ports=["scan_in_0"],
        phase_shifter_taps=[[0, 1]],
    )
    manifest = _manifest(tmp_path, _with_register_cone(module), compression)

    result = check_compression_structure(manifest)

    assert not result.passed
    assert any("no driver" in err for err in result.errors)


# --- register feedback-tap/reseed-mux cone (the OTHER half of the decompressor) ---


def _passing_module() -> dict:
    """A module with NO phase-shifter ports at all -- these tests only care
    about the register cone; check_compression_structure runs the
    phase-shifter loop over an empty scan_in_ports list (a no-op) before
    reaching the register-cone checks below."""
    return _with_register_cone({"ports": {}, "netnames": {}, "cells": {}})


@pytest.mark.unit
def test_check_compression_structure_accepts_valid_register_cone(
    tmp_path: Path,
) -> None:
    compression = _base_compression(scan_in_ports=[], phase_shifter_taps=[])
    manifest = _manifest(tmp_path, _passing_module(), compression)

    result = check_compression_structure(manifest)

    assert result.passed, result.errors


@pytest.mark.unit
def test_check_compression_structure_accepts_custom_channel_port_from_manifest(
    tmp_path: Path,
) -> None:
    """channel_port is read from the manifest (falling back to "tdi" only
    when absent), not hardcoded -- confirmed real: ring_generator_wrapper_
    verilog's own channel_port parameter defaults to "tdi" but is genuinely
    overridable, unlike lfsr_reg/next_state/prev_scan_en, which never are."""
    module = _passing_module()
    module["netnames"]["custom_tdi"] = module["netnames"].pop("tdi")
    compression = _base_compression(
        scan_in_ports=[], phase_shifter_taps=[], channel_port="custom_tdi"
    )
    manifest = _manifest(tmp_path, module, compression)

    result = check_compression_structure(manifest)

    assert result.passed, result.errors


@pytest.mark.unit
def test_check_compression_structure_rejects_declared_channel_port_not_in_netlist(
    tmp_path: Path,
) -> None:
    """A manifest declaring channel_port="custom_tdi" must make the check
    look for THAT name -- not silently fall back to "tdi" (which is still
    present here, unrenamed) and pass anyway."""
    module = _passing_module()
    compression = _base_compression(
        scan_in_ports=[], phase_shifter_taps=[], channel_port="custom_tdi"
    )
    manifest = _manifest(tmp_path, module, compression)

    result = check_compression_structure(manifest)

    assert not result.passed
    assert any("custom_tdi" in err for err in result.errors)


@pytest.mark.unit
def test_check_compression_structure_rejects_missing_lfsr_reg_net(
    tmp_path: Path,
) -> None:
    module = _passing_module()
    del module["netnames"]["lfsr_reg"]
    compression = _base_compression(scan_in_ports=[], phase_shifter_taps=[])
    manifest = _manifest(tmp_path, module, compression)

    result = check_compression_structure(manifest)

    assert not result.passed
    assert any("lfsr_reg" in err for err in result.errors)


@pytest.mark.unit
def test_check_compression_structure_rejects_wrong_reseed_select_wiring(
    tmp_path: Path,
) -> None:
    # The nand2b's A_N/B operands are swapped (B=prev_scan_en, A_N=scan_en
    # instead of the other way round) -- no cell in the netlist computes the
    # real reseed-select signal at all.
    module = _passing_module()
    module["cells"]["u_reseed_nand"]["connections"] = {
        "A_N": [_SCAN_EN_NET],
        "B": [_PREV_SCAN_EN_NET],
        "Y": [_RESEED_SELECT_NET],
    }
    compression = _base_compression(scan_in_ports=[], phase_shifter_taps=[])
    manifest = _manifest(tmp_path, module, compression)

    result = check_compression_structure(manifest)

    assert not result.passed
    assert any("reseed-select" in err for err in result.errors)


@pytest.mark.unit
def test_check_compression_structure_rejects_enabled_prev_scan_en_register(
    tmp_path: Path,
) -> None:
    # prev_scan_en's own FF must be un-enabled and fed directly by scan_en --
    # here its D input is wrong (fed by lfsr_reg[0] instead of scan_en).
    module = _passing_module()
    module["cells"]["u_prev_scan_en"]["connections"]["D"] = [_LFSR_REG_BITS[0]]
    compression = _base_compression(scan_in_ports=[], phase_shifter_taps=[])
    manifest = _manifest(tmp_path, module, compression)

    result = check_compression_structure(manifest)

    assert not result.passed
    assert any("prev_scan_en" in err for err in result.errors)


@pytest.mark.unit
def test_check_compression_structure_rejects_swapped_clean_bit_mux_operands(
    tmp_path: Path,
) -> None:
    # Bit 0's mux2 has A0/A1 swapped (A0=lfsr_reg[0], A1=tdi[0] instead of
    # the reverse) -- effective_state[0] would select the WRONG operand on
    # reseed. This exact corruption was empirically confirmed (against real
    # Yosys output) to slip past a naive design that only checks mux wiring
    # for "ghost" bits -- see compression_checks.py's dedicated
    # effective_state[k]-construction verification loop.
    module = _passing_module()
    module["cells"]["u_mux_0"]["connections"]["A0"] = [_LFSR_REG_BITS[0]]
    module["cells"]["u_mux_0"]["connections"]["A1"] = [_TDI_BITS[0]]
    compression = _base_compression(scan_in_ports=[], phase_shifter_taps=[])
    manifest = _manifest(tmp_path, module, compression)

    result = check_compression_structure(manifest)

    assert not result.passed
    assert any("effective_state[0]" in err for err in result.errors)


@pytest.mark.unit
def test_check_compression_structure_rejects_wrong_tap_set(tmp_path: Path) -> None:
    # next_state[1] should be a pure alias of effective_state[0] (1 not in
    # _TAPS={4,5,6}), but this netlist wires it as an XOR with fb instead --
    # as if bit 1 were also a tap.
    module = _passing_module()
    module["cells"]["u_bad_tap"] = {
        "type": "sky130_fd_sc_hd__xor2_1",
        "port_directions": {},
        "connections": {
            "A": [_EFFECTIVE_STATE_BITS[0]],
            "B": [_EFFECTIVE_STATE_BITS[_WIDTH - 1]],
            "X": [999],
        },
    }
    module["netnames"]["next_state"]["bits"] = [
        999 if b == 1 else b for b in _NEXT_STATE_BITS
    ]
    compression = _base_compression(scan_in_ports=[], phase_shifter_taps=[])
    manifest = _manifest(tmp_path, module, compression)

    result = check_compression_structure(manifest)

    assert not result.passed
    assert any("next_state[1]" in err for err in result.errors)


@pytest.mark.unit
def test_check_compression_structure_rejects_missing_scan_enable_port(
    tmp_path: Path,
) -> None:
    module = _passing_module()
    compression = _base_compression(
        scan_in_ports=[], phase_shifter_taps=[], scan_enable_port="does_not_exist"
    )
    manifest = _manifest(tmp_path, module, compression)

    result = check_compression_structure(manifest)

    assert not result.passed
    assert any("does_not_exist" in err for err in result.errors)

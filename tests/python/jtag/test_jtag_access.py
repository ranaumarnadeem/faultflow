"""How ff.py jtag reaches a manifest's network: the instruction and the IDCODE it
programs, the chip reset it pulses, and the refusals."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from faultflow.config import JtagConfig
from faultflow.integrations.autombist import AutombistTestAccess
from faultflow.jtag.command import (
    JtagError,
    _program,
    chip_reset_of,
    network_access_opcode,
    tap_idcode,
)
from faultflow.jtag.program import JtagProgramError, TckProgram, program_from_warptap
from faultflow.jtag.xcheck import ChipReset
from warptap_helpers import skip_unless_warptap

ROOT = Path(__file__).resolve().parents[3]
PROGRAM = ROOT / "tests/fixtures/autombist/input_demo_8x16_scn4m_jtag_program.json"
IJTAG_ACCESS = 0b1100

pytestmark = pytest.mark.unit


def _access(**fields: Any) -> AutombistTestAccess:
    return AutombistTestAccess(
        top_module="chip",
        output_verilog=Path("chip.v"),
        boundary_ports=(),
        instances=(),
        instruments=(),
        **fields,
    )


def test_a_manifest_without_the_fields_means_extest_and_no_pulse() -> None:
    access = _access()
    assert network_access_opcode(access) is None
    assert chip_reset_of(access) is None
    assert chip_reset_of(None) is None


def test_ijtag_access_and_the_chip_reset_come_from_the_manifest() -> None:
    access = _access(
        network_instruction="IJTAG_ACCESS",
        network_opcode=IJTAG_ACCESS,
        chip_reset="rst_n",
        chip_reset_active_low=True,
    )
    assert network_access_opcode(access) == IJTAG_ACCESS
    assert chip_reset_of(access) == ChipReset("rst_n", 0)
    high = dataclasses.replace(access, chip_reset="rst", chip_reset_active_low=False)
    assert chip_reset_of(high) == ChipReset("rst", 1)


@pytest.mark.parametrize(
    ("fields", "match"),
    [
        ({"network_instruction": "EXTEST", "network_opcode": 5}, "EXTEST is all zeros"),
        ({"network_instruction": "PRIVATE", "network_opcode": 9}, "behind PRIVATE"),
    ],
)
def test_an_instruction_ff_jtag_cant_program_is_refused(
    fields: dict[str, Any], match: str
) -> None:
    with pytest.raises(JtagError, match=match):
        network_access_opcode(_access(**fields))


def test_the_idcode_is_the_manifests_unless_the_ofs_says_otherwise() -> None:
    access = _access(idcode_value=0x12345679)
    assert tap_idcode(JtagConfig(), access) == 0x12345679
    assert tap_idcode(JtagConfig(), _access()) == JtagConfig().idcode
    agreeing = JtagConfig(idcode=0x12345679, idcode_given=True)
    assert tap_idcode(agreeing, access) == 0x12345679
    for given in (0x0000_0003, None):
        with pytest.raises(JtagError, match="contradicts the manifest"):
            tap_idcode(JtagConfig(idcode=given, idcode_given=True), access)


def test_a_program_file_reaching_the_network_another_way_is_refused(
    tmp_path: Path,
) -> None:
    """The fixture program reaches the network with EXTEST; a TAP behind
    IJTAG_ACCESS needs a program for that."""
    cfg: Any = SimpleNamespace(jtag=JtagConfig(), autombist_manifest=None)
    ijtag = _access(network_instruction="IJTAG_ACCESS", network_opcode=IJTAG_ACCESS)
    with pytest.raises(JtagError, match=r"with EXTEST, the manifest's TAP with "):
        _program(cfg, PROGRAM, ijtag)
    assert len(_program(cfg, PROGRAM, _access())) == 1483

    data = json.loads(PROGRAM.read_text(encoding="utf-8"))
    data["tap"]["ijtag_access_opcode"] = IJTAG_ACCESS
    own = tmp_path / "ijtag.json"
    own.write_text(json.dumps(data), encoding="utf-8")
    assert _program(cfg, own, ijtag).tap.ijtag_access_opcode == IJTAG_ACCESS
    with pytest.raises(JtagError, match=r"with IJTAG_ACCESS \(1100\), the manifest"):
        _program(cfg, own, _access())


def test_the_instruction_survives_the_program_format() -> None:
    """Only a program with IJTAG_ACCESS names it, so an EXTEST program's JSON -- and
    digest, which keys its grades -- is what it always was."""
    data = json.loads(PROGRAM.read_text(encoding="utf-8"))
    assert TckProgram.from_json(data).to_json() == data
    data["tap"]["ijtag_access_opcode"] = IJTAG_ACCESS
    program = TckProgram.from_json(data)
    assert program.tap.ijtag_access_opcode == IJTAG_ACCESS
    assert program.to_json() == data
    with pytest.raises(JtagProgramError, match="malformed"):
        TckProgram.from_json({**data, "tap": [4]})


def _network() -> tuple[Any, Any]:
    from warptap.sib_plan import build_sib_plan  # type: ignore[import-not-found]

    from faultflow.integrations.autombist_jtag import instrument_specs
    from faultflow.mbist.jtag import TestPort

    specs = instrument_specs(
        [TestPort("test_mode", "control", 1), TestPort("bist_done", "status", 1)]
    )
    return build_sib_plan(specs, top_name="chip")


def _built(**extra: Any) -> TckProgram:
    graph, root = _network()
    return program_from_warptap(
        graph,
        root,
        ir_width=4,
        has_idcode=True,
        idcode_value=JtagConfig().idcode or 1,
        margin=8,
        exhaustive_opcodes=False,
        **extra,
    )


def test_warptap_builds_the_program_for_a_network_behind_ijtag_access() -> None:
    skip_unless_warptap("warptap.tap_integrity")
    extest, ijtag = _built(), _built(ijtag_access_opcode=IJTAG_ACCESS)
    assert extest.tap.ijtag_access_opcode is None
    assert ijtag.tap.ijtag_access_opcode == IJTAG_ACCESS
    assert ijtag.tms != extest.tms  # the network's scans load another opcode
    with pytest.raises(JtagProgramError, match="warptap can't build"):
        _built(ijtag_access_opcode=0)  # EXTEST's own opcode


def test_a_warptap_without_ijtag_access_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skip_unless_warptap("warptap.tap_integrity")
    import warptap.tap_integrity as integrity  # type: ignore[import-not-found]

    @dataclasses.dataclass(frozen=True)
    class OldTapConfig:
        ir_width: int = 4
        has_idcode: bool = True
        idcode_value: int = 1

    monkeypatch.setattr(integrity, "TapConfig", OldTapConfig)
    with pytest.raises(JtagProgramError, match="predates ijtag_access_opcode"):
        _built(ijtag_access_opcode=IJTAG_ACCESS)

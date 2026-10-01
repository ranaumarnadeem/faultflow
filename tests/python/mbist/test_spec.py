"""The MBIST insertion file: what it accepts, and what it refuses."""

from __future__ import annotations

import builtins
import json
from pathlib import Path
from typing import Any

import pytest

from faultflow.mbist.spec import MbistSpecError, load_mbist_spec

ROOT = Path(__file__).resolve().parents[3]
CHIP = ROOT / "tests/fixtures/mbist_chip"

pytestmark = pytest.mark.unit


def _write(tmp_path: Path, data: dict[str, Any], name: str = "spec.json") -> Path:
    """A JSON insertion file next to the fixture's sources (copied in)."""
    for rel in ("rtl/core.v", "rtl/chip_top.v", "macros/input_demo_8x16_scn4m.v"):
        dst = tmp_path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes((CHIP / rel).read_bytes())
    (tmp_path / "sram.yml").write_bytes((CHIP / "mbist/sram_demo.yml").read_bytes())
    path = tmp_path / name
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _base(**memory: Any) -> dict[str, Any]:
    entry = {
        "name": "ram0",
        "instance": "u_core0.u_mem",
        "autombist_config": "sram.yml",
    }
    entry.update(memory)
    return {
        "design": {
            "sources": ["rtl/core.v", "rtl/chip_top.v"],
            "libs": ["macros/input_demo_8x16_scn4m.v"],
        },
        "memories": [entry],
    }


def test_the_fixture_insertion_file_loads() -> None:
    spec = load_mbist_spec(CHIP / "mbist.yml")
    assert spec.design.sources == (
        (CHIP / "rtl/core.v").resolve(),
        (CHIP / "rtl/chip_top.v").resolve(),
    )
    assert spec.design.libs == ((CHIP / "macros/input_demo_8x16_scn4m.v").resolve(),)
    assert spec.reset is not None and spec.reset.port == "rst_n"
    assert spec.reset.active_low
    assert spec.jtag is False
    assert spec.autombist_cmd == ("autombist",)
    assert spec.memory_patterns == ("input_demo_*",)
    assert [m.name for m in spec.memories] == ["core0_ram", "bank0_ram", "top_ram"]
    core0 = spec.memories[0]
    assert core0.instance == "u_core0.u_mem"
    assert core0.algo == "march-c"
    assert core0.tie == (("wmask0", 1), ("csb1", 1), ("addr1", 0))
    assert core0.share_clock == ("clk1",)
    assert core0.unused_outputs == ("dout1",)
    assert spec.memories[1].instance == "g_bank[0].u_mem"
    # Sequential by default: one memory per step.
    assert spec.schedule == (("core0_ram",), ("bank0_ram",), ("top_ram",))
    assert spec.memory_at("u_mem_top") is spec.memories[2]
    assert spec.memory_at("u_core1.u_mem") is None


def test_json_is_read_and_defaults_apply(tmp_path: Path) -> None:
    spec = load_mbist_spec(_write(tmp_path, _base()))
    assert spec.reset is None and spec.jtag is False
    assert spec.memory_patterns == ("*",)
    assert spec.memories[0].tie == ()
    assert spec.memories[0].autombist_config == (tmp_path / "sram.yml").resolve()


@pytest.mark.parametrize(
    ("value", "expected"), [(15, 15), ("0xF", 15), ("0b101", 5), ("12", 12)]
)
def test_tie_values_are_integers_in_any_base(
    tmp_path: Path, value: Any, expected: int
) -> None:
    spec = load_mbist_spec(_write(tmp_path, _base(tie={"wmask0": value})))
    assert spec.memories[0].tie == (("wmask0", expected),)


def test_concurrent_schedule_is_one_step(tmp_path: Path) -> None:
    data = _base()
    data["memories"].append(
        {"name": "ram1", "instance": "u_mem_top", "autombist_config": "sram.yml"}
    )
    data["schedule"] = "concurrent"
    assert load_mbist_spec(_write(tmp_path, data)).schedule == (("ram0", "ram1"),)
    data["schedule"] = [["ram1"], ["ram0"]]
    assert load_mbist_spec(_write(tmp_path, data)).schedule == (("ram1",), ("ram0",))


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda d: d.update(colour="blue"), "unknown key"),
        (lambda d: d.pop("design"), "design is required"),
        (lambda d: d["design"].update(sources=[]), "at least one file"),
        (lambda d: d["design"].update(sources=["rtl/nope.v"]), "not found"),
        (lambda d: d["design"].update(include_dirs=["nodir"]), "directory not found"),
        (
            lambda d: d.update(reset={"port": "rst_n", "active": "lo"}),
            "'low' or 'high'",
        ),
        (lambda d: d.update(jtag="yes"), "true or false"),
        (lambda d: d["memories"][0].update(name="0ram"), "identifier"),
        (lambda d: d["memories"][0].update(name="ram-0"), "identifier"),
        (lambda d: d["memories"][0].pop("instance"), "instance path"),
        (lambda d: d["memories"][0].update(autombist_config="x.yml"), "not found"),
        (lambda d: d["memories"][0].update(tie={"a": -1}), "negative"),
        (lambda d: d["memories"][0].update(tie={"a": "lots"}), "integer"),
        (lambda d: d["memories"][0].update(tie={"a": True}), "integer"),
        (
            lambda d: d["memories"][0].update(tie={"clk1": 0}, share_clock=["clk1"]),
            "more than once",
        ),
        (lambda d: d["memories"].append(dict(d["memories"][0])), "share the name"),
        (
            lambda d: d["memories"].append(dict(d["memories"][0], name="ram1")),
            "share the instance",
        ),
        (lambda d: d.update(schedule=[["ram0"], ["ghost"]]), r"unknown: \['ghost'\]"),
        (lambda d: d.update(schedule=[["ram0", "ram0"]]), "listed twice"),
        (lambda d: d.update(schedule=[]), r"missing: \['ram0'\]"),
        (lambda d: d.update(schedule="parallel"), "list of steps"),
    ],
)
def test_a_wrong_insertion_file_is_refused(
    tmp_path: Path, mutate: Any, message: str
) -> None:
    data = _base()
    mutate(data)
    with pytest.raises(MbistSpecError, match=message):
        load_mbist_spec(_write(tmp_path, data))


def test_an_unknown_file_type_is_refused(tmp_path: Path) -> None:
    with pytest.raises(MbistSpecError, match=".yml, .yaml or .json"):
        load_mbist_spec(_write(tmp_path, _base(), name="spec.toml"))


def test_yaml_without_pyyaml_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_import = builtins.__import__

    def no_yaml(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "yaml":
            raise ImportError("no yaml")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_yaml)
    path = _write(tmp_path, _base(), name="spec.yml")
    with pytest.raises(MbistSpecError, match="needs PyYAML"):
        load_mbist_spec(path)

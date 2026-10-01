"""The .ofs mbist-insert writes for the chip it synthesized: the user's .ofs, with the
inserted design's netlist, an output root of its own, its blackboxes, clocks and
scan settings, and the manifest.

Scan has to hold the chip reset inactive: each shell's reset synchronizer settles
with it inactive (faultflow.scan.nonscan), and the collar resets from it. So the
.ofs holds it, the synchronizers run non-scan, and a user .ofs that contradicts
that -- holds the reset active, or names it a scan port or a clock -- is refused
before anything is inserted. With --tap-nonscan, the TAP and the IJTAG network
run non-scan too, trst_n and tck held at 0, for ff.py jtag to test.
"""

from __future__ import annotations

import configparser
from pathlib import Path
from typing import Iterable, Sequence

from faultflow.integrations.autombist import literal_glob
from faultflow.mbist.netlist import InsertError
from faultflow.mbist.spec import ResetSpec

# The reset synchronizer's instance in every shell (faultflow/mbist/shell.py).
RESET_SYNC = "u_rst_sync"
TAP_HOLDS = (("trst_n", 0), ("tck", 0))
_PATH_KEYS = (
    ("design", "cell_lib"),
    ("design", "liberty"),
    ("design", "verilog_models"),
)


def read_base(ofs: Path) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None)
    if not parser.read(ofs, encoding="utf-8"):
        raise InsertError(f"cannot read {ofs}")
    return parser


def _holds(parser: configparser.ConfigParser) -> dict[str, int]:
    holds: dict[str, int] = {}
    for item in parser.get("scan", "hold", fallback="").split(","):
        port, sep, value = item.strip().rpartition(":")
        if sep and value.strip() in ("0", "1"):
            holds[port.strip()] = int(value)
    return holds


def _names(text: str) -> list[str]:
    return [name.strip() for name in text.split(",") if name.strip()]


def scan_holds(reset: ResetSpec, *, tap_nonscan: bool) -> tuple[tuple[str, int], ...]:
    """The inputs the inserted chip's scan test holds: the chip reset inactive, and
    with the TAP non-scan, trst_n and tck at 0."""
    holds = ((reset.port, int(reset.active_low)),)
    return holds + (TAP_HOLDS if tap_nonscan else ())


def check_base(
    parser: configparser.ConfigParser, reset: ResetSpec, *, tap_nonscan: bool
) -> None:
    """Refuse a user .ofs that contradicts the scan holds the inserted chip needs."""
    base = _holds(parser)
    for port, value in scan_holds(reset, tap_nonscan=tap_nonscan):
        if base.get(port, value) != value:
            why = (
                "the shells' reset synchronizers settle, and the collars leave "
                "reset, with the chip reset inactive"
                if port == reset.port
                else "the TAP and the IJTAG network run non-scan held in reset"
            )
            raise InsertError(
                f"the .ofs holds {port} at {base[port]} in scan; it must be {value}: "
                + why
            )
    scan_ports = {
        parser.get("scan", "scan_enable", fallback="scan_en").strip(),
        parser.get("scan", "scan_in", fallback="scan_in").strip(),
        parser.get("scan", "scan_out", fallback="scan_out").strip(),
    }
    if reset.port in scan_ports:
        raise InsertError(
            f"the .ofs names the chip reset {reset.port} as a scan port; scan holds "
            "it inactive"
        )
    if reset.port in _names(parser.get("clocks", "ports", fallback="")):
        raise InsertError(
            f"the .ofs names the chip reset {reset.port} as a clock; scan holds it "
            "inactive"
        )


def nonscan_globs(
    shells: Iterable[str], jtag_globs: Sequence[str] = ()
) -> tuple[str, ...]:
    """Each shell's reset synchronizer (settled in scan), and the network's
    instances when the TAP runs non-scan."""
    own = [f"{literal_glob(path)}__{RESET_SYNC}__*" for path in shells]
    return tuple(sorted(own)) + tuple(jtag_globs)


def write_inserted_ofs(
    path: Path,
    base: configparser.ConfigParser,
    *,
    netlist: Path,
    output_root: Path,
    blackboxes: Sequence[str],
    renamed: Sequence[str],
    clock_ports: Sequence[str],
    chains: int,
    nonscan_cells: Sequence[str],
    holds: Sequence[tuple[str, int]],
    manifest: Path,
) -> Path:
    """The user's .ofs (`base`) for the synthesized, inserted chip. Paths are
    absolute (an .ofs's resolve against the current directory). Its blackboxes are
    the chip's -- the user's own that `renamed` doesn't name (memories now inside
    a shell) and the composed netlist's memories; [clocks] are the scan clocks,
    each with the user's off state; [scan] keeps the user's settings, with at least
    one chain per clock domain and the non-scan cells and holds added."""
    out = configparser.ConfigParser(interpolation=None)
    out.read_dict({name: dict(base[name]) for name in base.sections()})

    def put(section: str, key: str, value: str) -> None:
        if not out.has_section(section):
            out.add_section(section)
        out.set(section, key, value)

    for section, key in _PATH_KEYS:
        value = base.get(section, key, fallback="").strip()
        if value:
            put(section, key, str(Path(value).resolve()))
    put("design", "netlist", str(netlist.resolve()))
    put("design", "output_root", str(output_root.resolve()))

    gone = set(renamed)
    kept = [
        i
        for i in _names(base.get("blackbox", "instances", fallback=""))
        if i not in gone
    ]
    instances = list(dict.fromkeys([*kept, *blackboxes]))
    put("blackbox", "instances", ", ".join(instances))
    if base.has_option("blackbox", "output_value"):
        values = [
            item
            for item in _names(base.get("blackbox", "output_value"))
            if ":" not in item or item.rpartition(":")[0].strip() in instances
        ]
        if values:
            put("blackbox", "output_value", ", ".join(values))
        else:
            out.remove_option("blackbox", "output_value")

    off = {
        port.strip(): value.strip()
        for port, _, value in (
            item.rpartition(":")
            for item in _names(base.get("clocks", "off", fallback=""))
        )
    }
    if clock_ports:
        put("clocks", "ports", ", ".join(clock_ports))
        offs = [f"{p}:{off[p]}" for p in clock_ports if p in off]
        if offs:
            put("clocks", "off", ", ".join(offs))
        elif out.has_option("clocks", "off"):
            out.remove_option("clocks", "off")

    chains = max(chains, base.getint("scan", "chains", fallback=0))
    put("scan", "chains", str(chains))
    cells = _names(base.get("scan", "nonscan_cells", fallback="")) + list(nonscan_cells)
    put("scan", "nonscan_cells", ", ".join(dict.fromkeys(cells)))
    merged = {**_holds(base), **dict(holds)}
    put("scan", "hold", ", ".join(f"{port}:{value}" for port, value in merged.items()))
    put("autombist", "manifest", str(manifest.resolve()))

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        out.write(handle)
    return path

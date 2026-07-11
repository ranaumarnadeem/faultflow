from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from faultflow.scan.errors import ScanError


def _quote(path: Path) -> str:
    text = str(path)
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def run_scan_techmap(
    generic_json: Path,
    techmap_verilog: Path,
    output_verilog: Path,
    top: str,
    log_path: Path,
    script_path: Path,
) -> Path:
    yosys = shutil.which("yosys")
    if yosys is None:
        raise ScanError("scan techmap requires yosys on PATH")
    if not generic_json.exists():
        raise ScanError(f"missing generic scanned JSON: {generic_json}")
    if not techmap_verilog.exists():
        raise ScanError(f"missing scan techmap file: {techmap_verilog}")

    output_verilog.parent.mkdir(parents=True, exist_ok=True)
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text(
        "\n".join(
            [
                f"read_json {_quote(generic_json)}",
                f"techmap -map {_quote(techmap_verilog)}",
                "clean",
                f"write_verilog {_quote(output_verilog)}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    proc = subprocess.run(
        [yosys, "-Q", "-s", str(script_path)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(proc.stdout, encoding="utf-8")
    if proc.returncode != 0:
        raise ScanError(f"scan techmap failed; see {log_path}")
    return output_verilog


def run_scan_techmap_json(
    generic_json: Path,
    techmap_verilog: Path,
    output_json: Path,
    log_path: Path,
    script_path: Path,
) -> Path:
    """Techmap generic scan JSON to Sky130 cells and emit JSON for equivalence sim."""
    yosys = shutil.which("yosys")
    if yosys is None:
        raise ScanError("scan techmap requires yosys on PATH")
    if not generic_json.exists():
        raise ScanError(f"missing generic scanned JSON: {generic_json}")
    if not techmap_verilog.exists():
        raise ScanError(f"missing scan techmap file: {techmap_verilog}")

    output_json.parent.mkdir(parents=True, exist_ok=True)
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text(
        "\n".join(
            [
                f"read_json {_quote(generic_json)}",
                f"techmap -map {_quote(techmap_verilog)}",
                "clean",
                f"write_json {_quote(output_json)}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    proc = subprocess.run(
        [yosys, "-Q", "-s", str(script_path)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(proc.stdout, encoding="utf-8")
    if proc.returncode != 0:
        raise ScanError(f"scan techmap json failed; see {log_path}")
    return output_json


def verilog_to_json(
    verilog: Path,
    top: str,
    output_json: Path,
    log_path: Path,
    script_path: Path,
) -> Path:
    """Import gate-level Verilog for C++ JSON simulation (optional fallback path).

    Deliberately does NOT read cell library behavioral models and does NOT run
    `hierarchy -check`: the C++ core resolves cell semantics from the selected
    cell-map JSON at simulation time, not from Yosys elaboration, so referenced
    library cells only need to survive as opaque (type-name-only) instances --
    `hierarchy` without `-check` does exactly that. Reading the real Sky130
    behavioral models here was tried and reliably fails: they use full UDP
    `primitive`/`table` blocks that Yosys's Verilog-2005 frontend cannot parse
    (a real frontend limitation, not a flag to work around), and even with those
    primitive declarations skipped (`-DNO_PRIMITIVES`), `-DFUNCTIONAL` still ends
    up selected for the cell body regardless of macro state, referencing a UDP
    type that was never defined.
    """
    yosys = shutil.which("yosys")
    if yosys is None:
        raise ScanError("verilog import requires yosys on PATH")
    if not verilog.exists():
        raise ScanError(f"missing verilog netlist: {verilog}")

    output_json.parent.mkdir(parents=True, exist_ok=True)
    script_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"read_verilog {_quote(verilog)}",
        f"hierarchy -top {top}",
        f"write_json {_quote(output_json)}",
    ]
    script_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    proc = subprocess.run(
        [yosys, "-Q", "-s", str(script_path)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(proc.stdout, encoding="utf-8")
    if proc.returncode != 0:
        raise ScanError(f"verilog import failed; see {log_path}")
    return output_json

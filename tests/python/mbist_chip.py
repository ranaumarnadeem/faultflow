"""The MBIST-insertion fixture chip (tests/fixtures/mbist_chip), copied for a
test with a stand-in for autoMBIST that copies the collar fixture
(tests/fixtures/autombist/input_demo_8x16_scn4m) generated from the same config."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[2]
CHIP = ROOT / "tests/fixtures/mbist_chip"
COLLAR = ROOT / "tests/fixtures/autombist/input_demo_8x16_scn4m"
MODEL = CHIP / "sim/input_demo_8x16_scn4m_model.v"


def chip_copy(
    tmp: Path,
    spec_edit: Callable[[str], str] = lambda text: text,
    rtl_edit: Callable[[Path], None] | None = None,
) -> Path:
    """The fixture chip in `tmp`, its insertion file running the stand-in for
    autoMBIST; `spec_edit` and `rtl_edit` change the copy. Returns the
    insertion file."""
    for rel in ("rtl", "macros", "mbist", "sim"):
        shutil.copytree(CHIP / rel, tmp / rel)
    fake = tmp / "fake_autombist.py"
    fake.write_text(
        "import shutil, sys\n"
        "from pathlib import Path\n"
        "args = sys.argv[1:]\n"
        "out = Path(args[args.index('--out') + 1])\n"
        f"shutil.copytree({str(COLLAR)!r}, out / 'input_demo_8x16_scn4m')\n",
        encoding="utf-8",
    )
    text = (CHIP / "mbist.yml").read_text(encoding="utf-8")
    text = text.replace("jtag: false", f"jtag: false\nautombist_cmd: python3 {fake}")
    spec = tmp / "mbist.yml"
    spec.write_text(spec_edit(text), encoding="utf-8")
    if rtl_edit is not None:
        rtl_edit(tmp)
    return spec

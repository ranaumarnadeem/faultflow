from __future__ import annotations

import traceback
from pathlib import Path


def log_exception(output_root: Path, top: str | None) -> Path:
    base = output_root / top if top else output_root
    path = base / ".faultflow" / "logs" / "shell.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(traceback.format_exc())
        handle.write("\n")
    return path

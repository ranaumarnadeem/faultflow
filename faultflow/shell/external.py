from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class ExternalCommandResult:
    returncode: int
    stdout: str
    stderr: str


def run_external(words: Sequence[str]) -> ExternalCommandResult | None:
    if not words:
        return None
    executable = shutil.which(words[0])
    if executable is None:
        return None
    process = subprocess.run(
        [executable, *words[1:]],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    return ExternalCommandResult(
        returncode=process.returncode,
        stdout=process.stdout,
        stderr=process.stderr,
    )

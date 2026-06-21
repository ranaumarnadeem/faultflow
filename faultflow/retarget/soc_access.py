"""SoC scan-access manifest: how block chains map onto the SoC scan chains.

Schema ``faultflow_soc_access_v1``:

```json
{ "schema": "faultflow_soc_access_v1", "assembly_top": "soc",
  "soc_scan": {"scan_inputs": ["si0"], "scan_outputs": ["so0"],
               "scan_enable": "se", "clock_ports": ["CLK"], "max_chain_length": 4},
  "soc_chains": [
    {"index": 0, "scan_in": "si0", "scan_out": "so0", "length": 4, "segments": [
      {"block": "blkB", "block_chain": 0, "soc_offset": 0, "length": 3},
      {"block": "__bypass", "block_chain": null, "soc_offset": 3, "length": 1}]}],
  "wrapper_instruction": {"blkB": "INTEST", "blkA": "BYPASS"} }
```

``soc_offset`` is the segment's position from the SoC chain HEAD (scan-in side):
the segment occupies SoC chain positions ``[soc_offset, soc_offset + length)``.
A segment with ``block_chain == null`` is BYPASS / sibling fill (don't-care bits).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from faultflow.config import ConfigError

SOC_ACCESS_SCHEMA = "faultflow_soc_access_v1"


class SocAccessError(ConfigError):
    """A malformed or inconsistent SoC scan-access manifest."""


@dataclass(frozen=True)
class SocSegment:
    block: str
    block_chain: int | None  # None => BYPASS / sibling fill
    soc_offset: int
    length: int


@dataclass(frozen=True)
class SocChain:
    index: int
    scan_in: str
    scan_out: str
    length: int
    segments: tuple[SocSegment, ...]


@dataclass(frozen=True)
class SocAccess:
    assembly_top: str
    scan_inputs: tuple[str, ...]
    scan_outputs: tuple[str, ...]
    scan_enable: str
    clock_ports: tuple[str, ...]
    max_chain_length: int
    soc_chains: tuple[SocChain, ...]
    wrapper_instruction: dict[str, str] = field(default_factory=dict)

    def chain_for_block(self, block: str) -> SocChain:
        """The SoC chain that carries an INTEST segment for `block`."""
        for chain in self.soc_chains:
            if any(
                seg.block == block and seg.block_chain is not None
                for seg in chain.segments
            ):
                return chain
        raise SocAccessError(f"no SoC chain carries an INTEST segment for {block!r}")


def _req(obj: dict[str, Any], key: str, where: str) -> Any:
    if key not in obj:
        raise SocAccessError(f"{where}: missing required field {key!r}")
    return obj[key]


def _parse_chain(raw: dict[str, Any], i: int) -> SocChain:
    where = f"soc_chains[{i}]"
    raw_segs = _req(raw, "segments", where)
    if not isinstance(raw_segs, list) or not raw_segs:
        raise SocAccessError(f"{where}.segments must be a non-empty list")
    length = int(_req(raw, "length", where))
    segments: list[SocSegment] = []
    covered = 0
    for j, rs in enumerate(sorted(raw_segs, key=lambda s: int(s.get("soc_offset", 0)))):
        sw = f"{where}.segments[{j}]"
        offset = int(_req(rs, "soc_offset", sw))
        seg_len = int(_req(rs, "length", sw))
        if offset != covered:
            raise SocAccessError(
                f"{sw}: soc_offset {offset} leaves a gap/overlap "
                f"(expected {covered}); segments must tile the chain"
            )
        bc = rs.get("block_chain")
        segments.append(
            SocSegment(
                block=str(_req(rs, "block", sw)),
                block_chain=None if bc is None else int(bc),
                soc_offset=offset,
                length=seg_len,
            )
        )
        covered += seg_len
    if covered != length:
        raise SocAccessError(
            f"{where}: segments cover {covered} bits but chain length is {length}"
        )
    return SocChain(
        index=int(_req(raw, "index", where)),
        scan_in=str(_req(raw, "scan_in", where)),
        scan_out=str(_req(raw, "scan_out", where)),
        length=length,
        segments=tuple(segments),
    )


def parse_soc_access(data: dict[str, Any]) -> SocAccess:
    """Validate + build a SocAccess from a parsed manifest object."""
    if data.get("schema") != SOC_ACCESS_SCHEMA:
        raise SocAccessError(
            f"unsupported schema {data.get('schema')!r}; expected {SOC_ACCESS_SCHEMA!r}"
        )
    soc_scan = _req(data, "soc_scan", "soc_access")
    raw_chains = _req(data, "soc_chains", "soc_access")
    if not isinstance(raw_chains, list) or not raw_chains:
        raise SocAccessError("soc_chains must be a non-empty list")
    chains = tuple(_parse_chain(rc, i) for i, rc in enumerate(raw_chains))
    instr = data.get("wrapper_instruction", {})
    if not isinstance(instr, dict):
        raise SocAccessError("wrapper_instruction must be an object")
    return SocAccess(
        assembly_top=str(_req(data, "assembly_top", "soc_access")),
        scan_inputs=tuple(_req(soc_scan, "scan_inputs", "soc_scan")),
        scan_outputs=tuple(_req(soc_scan, "scan_outputs", "soc_scan")),
        scan_enable=str(_req(soc_scan, "scan_enable", "soc_scan")),
        clock_ports=tuple(soc_scan.get("clock_ports", [])),
        max_chain_length=int(_req(soc_scan, "max_chain_length", "soc_scan")),
        soc_chains=chains,
        wrapper_instruction={str(k): str(v) for k, v in instr.items()},
    )


def load_soc_access(path: str | Path) -> SocAccess:
    p = Path(path)
    if not p.exists():
        raise SocAccessError(f"SoC access manifest not found: {p}")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SocAccessError(f"SoC access manifest is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise SocAccessError("SoC access manifest root must be a JSON object")
    return parse_soc_access(data)

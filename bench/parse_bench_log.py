#!/usr/bin/env python3
"""Parse bench_iscas85.log / bench_iscas89.log into a markdown table.

Usage:
    python3 bench/parse_bench_log.py bench_iscas85.log
    python3 bench/parse_bench_log.py bench_iscas89.log
    python3 bench/parse_bench_log.py bench_iscas85.log bench_iscas89.log

Prints two tables: SA (stuck_at) and Transition, sorted by circuit name.
"""

import re
import sys
from collections import defaultdict

FIELD = re.compile(r"(\w+)=([^\s]+)")


def parse_line(line: str) -> dict[str, str]:
    return dict(FIELD.findall(line))


def fmt(v: str, unit: str = "") -> str:
    if v in ("N/A", ""):
        return "—"
    try:
        f = float(v)
        if unit == "%":
            return f"{f:.2f}%"
        if unit == "s":
            return f"{f:.1f}s"
        return str(int(f))
    except ValueError:
        return v


def print_table(rows: list[dict], model_label: str) -> None:
    if not rows:
        print(f"\n### {model_label}: no data\n")
        return

    rows = sorted(rows, key=lambda r: r.get("circuit", ""))
    print(f"\n### {model_label}\n")
    header = (
        "| Circuit | FFs | Chains | Gates | Coverage | Denominator | Detected "
        "| Vectors | Raw Vecs | ATPG s | Sim s | Total s |"
    )
    sep = (
        "|---------|-----|--------|-------|----------|-------------|---------|"
        "---------|----------|--------|-------|---------|"
    )
    print(header)
    print(sep)
    for r in rows:
        print(
            f"| {r.get('circuit','?'):12s} "
            f"| {fmt(r.get('ffs','?')):3s} "
            f"| {fmt(r.get('chains','N/A')):6s} "
            f"| {fmt(r.get('gates','?')):5s} "
            f"| {fmt(r.get('coverage','N/A'), '%'):8s} "
            f"| {fmt(r.get('denominator','N/A')):11s} "
            f"| {fmt(r.get('detected','N/A')):7s} "
            f"| {fmt(r.get('vectors','N/A')):7s} "
            f"| {fmt(r.get('raw_vectors','N/A')):8s} "
            f"| {fmt(r.get('atpg_s','N/A'), 's'):6s} "
            f"| {fmt(r.get('sim_s','N/A'), 's'):5s} "
            f"| {fmt(r.get('total_s','N/A'), 's'):7s} |"
        )


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    by_model: dict[str, list[dict]] = defaultdict(list)
    errors: list[dict] = []

    for path in sys.argv[1:]:
        try:
            with open(path) as fh:
                lines = fh.readlines()
        except OSError as exc:
            print(f"Error reading {path}: {exc}", file=sys.stderr)
            continue

        for line in lines:
            if "status=DONE" in line:
                fields = parse_line(line)
                model = fields.get("model", "unknown")
                by_model[model].append(fields)
            elif "status=ERROR" in line:
                errors.append(parse_line(line))

    print_table(by_model.get("stuck_at", []), "Stuck-at faults")
    print_table(by_model.get("transition", []), "Transition faults")

    if errors:
        print(f"\n### Errors ({len(errors)} circuits failed)\n")
        print("| Circuit | Model | Stage |")
        print("|---------|-------|-------|")
        for e in errors:
            print(
                f"| {e.get('circuit','?'):12s} | {e.get('model','?'):10s} | {e.get('stage','?')} |"
            )


if __name__ == "__main__":
    main()

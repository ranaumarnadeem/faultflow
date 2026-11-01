"""Collect the Fault vs FaultFlow sweep into one CSV + markdown table."""

import csv
import re
import sys
from pathlib import Path

ROOT = Path.home() / "ffcmp"
DESIGNS = "c432 c499 c880 c1355 c1908 c2670 c3540 c5315 s27 s298 s344 s382 s526 s820 s1238 s1423".split()


def kv(line):
    return dict(m.groups() for m in re.finditer(r"(\w+)=(\S+)", line))


def fault_results():
    out = {}
    for line in (ROOT / "fault_sweep.log").read_text(errors="replace").splitlines():
        if line.startswith("RESULT "):
            _, d, gen, rest = line.split(" ", 3)
            out[(d, gen)] = kv(rest)
    return out


def ff_results(workers):
    out = {}
    log = ROOT / f"ff_sweep_w{workers}.log"
    if not log.exists():
        return out
    cur = None
    for line in log.read_text(errors="replace").splitlines():
        if line.startswith("##START "):
            cur = line.split()[1]
            out[cur] = {}
        elif line.startswith("##WALL_S ") and cur:
            out[cur]["wall"] = line.split()[1]
        elif line.startswith("sim complete") and cur:
            out[cur]["vectors"] = kv(line).get("vectors")
        elif line.startswith("top=") and cur:
            out[cur].update(kv(line))
    return out


def pct(x):
    return f"{float(x):.2f}" if x not in (None, "") else "-"


def sec(x):
    return f"{float(x):.1f}" if x not in (None, "") else "-"


def main():
    fr = fault_results()
    ff = {w: ff_results(w) for w in (1, 10)}
    rows = []
    for d in DESIGNS:
        row = {"design": d}
        for gen in ("prng", "quaigh"):
            r = fr.get((d, gen), {})
            row[f"fault_{gen}_faults"] = r.get("faults")
            row[f"fault_{gen}_cov"] = r.get("coverage") if r.get("rc") == "0" else None
            row[f"fault_{gen}_tvs"] = r.get("tvs")
            row[f"fault_{gen}_wall"] = r.get("wall")
        for w in (1, 10):
            r = ff[w].get(d, {})
            det = int(r.get("detected", 0) or 0)
            den = int(r.get("denominator", 0) or 0)
            red = int(r.get("redundant", 0) or 0)
            if w == 1:
                row["ff_faults"] = den + red if r else None
                row["ff_redundant"] = red if r else None
                row["ff_test_cov"] = r.get("coverage", "").rstrip("%") or None
                row["ff_fault_cov"] = 100.0 * det / (den + red) if den + red else None
                row["ff_tvs"] = r.get("vectors")
            row[f"ff_w{w}_wall"] = r.get("wall")
        rows.append(row)

    with open(ROOT / "comparison.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)

    hdr = ("| Design | Fault faults | Fault PRNG cov % | vec | s | Fault+Quaigh cov % | vec | s "
           "| FF faults | FF fault cov % | FF test cov % | redundant | vec | s (1 core) | s (10 workers) |")
    print(hdr)
    print("|" + "---|" * hdr.count("|")[:-1] if False else "|" + "---|" * (hdr.count("|") - 1))
    for r in rows:
        print(
            f"| {r['design']} | {r['fault_prng_faults'] or '-'} | {pct(r['fault_prng_cov'])} | {r['fault_prng_tvs'] or '-'} "
            f"| {sec(r['fault_prng_wall'])} | {pct(r['fault_quaigh_cov'])} | {r['fault_quaigh_tvs'] or '-'} "
            f"| {sec(r['fault_quaigh_wall'])} | {r['ff_faults'] or '-'} | {pct(r['ff_fault_cov'])} "
            f"| {pct(r['ff_test_cov'])} | {r['ff_redundant'] if r['ff_redundant'] is not None else '-'} "
            f"| {r['ff_tvs'] or '-'} | {sec(r['ff_w1_wall'])} | {sec(r['ff_w10_wall'])} |"
        )


if __name__ == "__main__":
    sys.exit(main())

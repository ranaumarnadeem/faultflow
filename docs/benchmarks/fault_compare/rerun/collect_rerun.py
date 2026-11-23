"""Turn results.log into one markdown table per thread count: the median of each
point's repetitions, then the ratio of totals and the geometric mean of the
per-design ratios, against each Fault generator. A tool that did not finish every
design at a thread count (the one-thread PRNG point can be stopped) is left out of
that table and named, so no ratio is taken over a subset without saying so."""

import math
import re
import statistics
import sys

DESIGNS = (
    "c432 c499 c880 c1355 c1908 c2670 c3540 c5315 "
    "s27 s298 s344 s382 s526 s820 s1238 s1423"
).split()
TOOLS = ("faultflow", "prng", "quaigh")


def read(path):
    """(tool, threads, design) -> the fields of every repetition, failures refused."""
    runs = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            if not line.startswith("RESULT "):
                continue
            _, tool, threads, _rep, design, rest = line.split(" ", 5)
            fields = dict(re.findall(r"(\w+)=(\S*)", rest))
            if fields["rc"] != "0" or not fields["wall"]:
                raise SystemExit(f"failed run: {line.strip()}")
            runs.setdefault((tool, int(threads), design), []).append(fields)
    return runs


def median(runs, key, field="wall"):
    return statistics.median(float(r[field]) for r in runs[key])


def spread(runs, key):
    """Largest repetition's excess over the smallest, as a share of the median."""
    walls = [float(r["wall"]) for r in runs[key]]
    return (max(walls) - min(walls)) / statistics.median(walls)


def vectors(runs, key):
    """The vector count, or its range: Fault's differs between repetitions."""
    counts = sorted({int(r["vectors"]) for r in runs[key]})
    return str(counts[0]) if len(counts) == 1 else f"{counts[0]}-{counts[-1]}"


def table(runs, threads):
    print(f"\n### {threads} thread{'s' if threads > 1 else ''}\n")
    designs = [d for d in DESIGNS if any((t, threads, d) in runs for t in TOOLS)]
    present = [t for t in TOOLS if all((t, threads, d) in runs for d in designs)]
    for tool in TOOLS:
        have = sum((tool, threads, d) in runs for d in designs)
        if tool not in present and have:
            print(f"{tool} left out: {have} of {len(designs)} designs.\n")
    if "faultflow" not in present:
        print("No table without FaultFlow on every design.")
        return
    others = [t for t in present if t != "faultflow"]
    head = (
        "| Design | FaultFlow faults | Fault faults | faultflow s | faultflow vectors |"
    )
    rule = "|---|---:|---:|---:|---:|"
    for tool in others:
        head += f" {tool} s | {tool} vectors |"
        rule += "---:|---:|"
    head += " FaultFlow test cov. | FaultFlow fault cov. |"
    rule += "---:|---:|"
    for tool in others:
        head += f" {tool} cov. |"
        rule += "---:|"
    print(head)
    print(rule)
    for d in designs:
        ff = runs[("faultflow", threads, d)][0]
        fault_faults = runs[(others[0], threads, d)][0]["faults"] if others else "-"
        key = ("faultflow", threads, d)
        row = (
            f"| {d} | {ff['faults']} | {fault_faults} "
            f"| {median(runs, key):.1f} | {vectors(runs, key)} |"
        )
        for tool in others:
            key = (tool, threads, d)
            row += f" {median(runs, key):.1f} | {vectors(runs, key)} |"
        det, den, red = int(ff["detected"]), int(ff["faults"]), int(ff["redundant"])
        row += f" {100 * det / (den - red):.2f}% | {100 * det / den:.2f}% |"
        for tool in others:
            row += f" {median(runs, (tool, threads, d), 'coverage'):.2f}% |"
        print(row)
    total = {t: sum(median(runs, (t, threads, d)) for d in designs) for t in present}
    print("\n" + ", ".join(f"{t} total {total[t]:.1f} s" for t in present) + ".")
    worst = max(spread(runs, (t, threads, d)) for t in present for d in designs)
    print(f"Largest spread between repetitions of a point: {100 * worst:.0f}%.")
    for tool in others:
        ratios = [
            median(runs, (tool, threads, d)) / median(runs, ("faultflow", threads, d))
            for d in designs
        ]
        geo = math.exp(sum(math.log(r) for r in ratios) / len(ratios))
        print(
            f"{tool} / faultflow: {total[tool] / total['faultflow']:.2f}x in total, "
            f"{geo:.2f}x geometric mean, {min(ratios):.2f}x to {max(ratios):.2f}x."
        )


def main():
    runs = read(sys.argv[1])
    for threads in sorted({k[1] for k in runs}, reverse=True):
        table(runs, threads)


if __name__ == "__main__":
    main()

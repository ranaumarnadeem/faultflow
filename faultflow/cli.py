from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from faultflow.config import (
    ConfigError,
    add_clock_to_config,
    load_config,
    parse_bool_value,
)
from faultflow.runner import Runner
from faultflow.service import FlowService
from faultflow.shell.repl import run_shell


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python3 ff.py")
    sub = parser.add_subparsers(dest="command", required=True)

    shell = sub.add_parser("shell", help="Start the Faultflow Tcl shell")
    shell.add_argument("-f", "--file", type=Path, help="Execute a Tcl script")
    shell.add_argument("-c", "--config", type=Path, help="Preload a project config")
    shell.add_argument("--out", type=Path, default=Path("output"), help="Output root")
    shell.add_argument("--verbose", action="store_true")

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--top", required=True, help="Top module name")
        p.add_argument("-c", "--config", default="config.ofs", help="Config file")

    init = sub.add_parser("init", help="Initialize output directory and DB")
    add_common(init)

    sim = sub.add_parser("sim", help="Run combinational stuck-at simulation")
    add_common(sim)
    sim.add_argument(
        "--purge",
        action="store_true",
        help="Remove transient junk inside output/<top>/.faultflow/ before sim",
    )
    sim.add_argument(
        "--clean",
        action="store_true",
        help=(
            "Remove output/<top>/.faultflow/ internal workspace before sim; "
            "deliverables at output/<top>/ are kept"
        ),
    )
    sim.add_argument(
        "-v",
        "--verify",
        help="Override [simulation] verify for this sim run",
    )
    sim.add_argument(
        "--ext",
        type=Path,
        help="External .test vectors; requires a same-stem .bench sidecar",
    )
    sim.add_argument(
        "--max",
        type=int,
        metavar="ROUNDS",
        help="Maximum progressive ATPG rounds (default: [atpg] max_rounds or 20)",
    )
    sim.add_argument(
        "--scan",
        action="store_true",
        help="Run full-scan stuck-at ATPG on reduced pseudo-PI/PO view",
    )
    sim.add_argument(
        "--serial-ref",
        action="store_true",
        help=(
            "Run isolated serial reference diagnostics for an existing campaign; "
            "does not update production coverage"
        ),
    )
    sim.add_argument(
        "-t",
        dest="target_coverage",
        type=float,
        metavar="PCT",
        help="Target coverage percent to stop ATPG (default: [report] threshold)",
    )
    sim.add_argument(
        "--model",
        choices=["stuck-at", "transition"],
        help="Override [fault_model] model for this sim run",
    )
    sim.add_argument(
        "--export-patterns",
        dest="export_patterns",
        type=Path,
        metavar="PATH",
        help=(
            "Export scan ATPG patterns as JSON to PATH (requires --scan); "
            "input for `retarget --patterns`"
        ),
    )

    def add_wrapper_mode_opts(p: argparse.ArgumentParser) -> None:
        add_common(p)
        p.add_argument(
            "--purge",
            action="store_true",
            help="Remove transient junk inside output/<top>/.faultflow/ before run",
        )
        p.add_argument(
            "--clean",
            action="store_true",
            help=(
                "Remove output/<top>/.faultflow/ internal workspace before run; "
                "deliverables at output/<top>/ are kept. Needed when switching "
                "between intest/extest (test mode is part of the campaign "
                "fingerprint)"
            ),
        )
        p.add_argument(
            "--max",
            type=int,
            metavar="ROUNDS",
            help="Maximum progressive ATPG rounds (default: [atpg] max_rounds or 20)",
        )
        p.add_argument(
            "-t",
            dest="target_coverage",
            type=float,
            metavar="PCT",
            help="Target coverage percent to stop ATPG (default: [report] threshold)",
        )

    intest = sub.add_parser(
        "intest",
        help=(
            "IEEE 1500 INTEST: scan-integrated wrapper coverage of the core "
            "(== sim --scan with test mode forced to intest)"
        ),
    )
    add_wrapper_mode_opts(intest)

    extest = sub.add_parser(
        "extest",
        help=(
            "IEEE 1500 EXTEST: wrapper-boundary / interconnect coverage with the "
            "core held safe (combinational ATPG on the fused boundary view)"
        ),
    )
    add_wrapper_mode_opts(extest)

    project = sub.add_parser(
        "project",
        help=(
            "Hierarchical project: run per-block INTEST + assembly EXTEST and "
            "aggregate one chip coverage number"
        ),
    )
    project.add_argument(
        "-p", "--project", type=Path, required=True, help="Project manifest JSON"
    )
    project.add_argument("--clean", action="store_true", help="Clean scope workspaces")
    project.add_argument(
        "--max",
        type=int,
        metavar="ROUNDS",
        help="Max progressive ATPG rounds per scope",
    )
    project.add_argument(
        "-t",
        dest="target_coverage",
        type=float,
        metavar="PCT",
        help="Target coverage percent per scope",
    )

    retarget_p = sub.add_parser(
        "retarget",
        help="Retarget a block's exported scan patterns onto a SoC scan path",
    )
    retarget_p.add_argument(
        "--patterns",
        type=Path,
        required=True,
        help="Block scan pattern JSON (from run_atpg -export-patterns)",
    )
    retarget_p.add_argument(
        "--soc-access",
        dest="soc_access",
        type=Path,
        required=True,
        help="SoC access manifest JSON (faultflow_soc_access_v1)",
    )
    retarget_p.add_argument("--block", required=True, help="Source block name")
    retarget_p.add_argument(
        "--out", type=Path, required=True, help="Output retargeted pattern file"
    )

    autombist_p = sub.add_parser(
        "autombist-generate",
        help="Generate a FaultFlow synthesis (.ofs) from an autoMBIST manifest",
    )
    autombist_p.add_argument(
        "--config", type=Path, required=True, help="autoMBIST YAML config"
    )
    autombist_p.add_argument(
        "--out",
        type=Path,
        required=True,
        help="Output directory (passed to autombist --out)",
    )
    autombist_p.add_argument(
        "--autombist-cmd",
        dest="autombist_cmd",
        default="autombist",
        help="Command prefix to invoke autoMBIST, e.g. 'python3 -m autombist.cli'",
    )
    autombist_p.add_argument(
        "--algo",
        default=None,
        help="MBIST algorithm, passed to autombist generate --algo (march-c, "
        "march-raw, march-1r1w, march-2rw, march-x, mats-plus, checkerboard); "
        "autoMBIST's default without it",
    )
    autombist_p.add_argument(
        "--liberty",
        type=Path,
        required=True,
        help="Liberty file for synthesis + the written .ofs",
    )
    autombist_p.add_argument(
        "--cell-lib",
        dest="cell_lib",
        type=Path,
        required=True,
        help="Cell-map JSON for the written .ofs",
    )
    autombist_p.add_argument(
        "--test-access",
        dest="test_access",
        action="store_true",
        help="Wrap the generated design for JTAG access (autombist "
        "wrap-test-access, which needs warptap) and synthesize the wrapped "
        "design",
    )
    autombist_p.add_argument(
        "--tap-nonscan",
        dest="tap_nonscan",
        action="store_true",
        help="With --test-access: keep the TAP and its IJTAG network out of scan, "
        "held in reset ([scan] nonscan_cells, [scan] hold), for ff.py jtag to "
        "grade through TCK",
    )

    list_mem = sub.add_parser(
        "list-memories",
        help="List a design's memory instances and how each pin is connected, "
        "to help write an MBIST insertion file",
    )
    list_mem.add_argument("--top", required=True, help="Top module of the design")
    list_mem.add_argument(
        "--spec",
        type=Path,
        required=True,
        help="MBIST insertion file (YAML or JSON): the design's sources, and the "
        "memories already configured",
    )
    list_mem.add_argument(
        "--pattern",
        action="append",
        default=None,
        help="Memory macro module name glob, e.g. 'sky130_sram_*' (repeatable); "
        "replaces the file's memory_patterns",
    )

    insert = sub.add_parser(
        "mbist-insert",
        help="Insert MBIST into a design's RTL: an autoMBIST collar, in a shell, "
        "in place of every memory an insertion file configures",
    )
    insert.add_argument("--top", required=True, help="Top module of the design")
    insert.add_argument(
        "--spec", type=Path, required=True, help="MBIST insertion file (YAML or JSON)"
    )
    insert.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output directory (default: mbist_<top> in the current directory)",
    )
    insert.add_argument(
        "-c",
        "--config",
        type=Path,
        default=None,
        help="The design's .ofs: no inserted module may be named after one of its "
        "liberty's cells; the inserted design is synthesized with its liberty, and "
        "<top>_mbist.ofs written for it",
    )
    insert.add_argument(
        "--tap-nonscan",
        action="store_true",
        help="With jtag and -c: the TAP and the IJTAG network run non-scan in the "
        "written .ofs (trst_n and tck held at 0), for ff.py jtag to test",
    )

    status = sub.add_parser("status", help="Print current coverage status")
    add_common(status)
    status.add_argument(
        "--scan",
        action="store_true",
        help=(
            "Read status from the scan campaign in "
            "output/<top>/.faultflow/faultflow.sqlite"
        ),
    )

    scan = sub.add_parser("scan", help="Insert generic scan chains")
    add_common(scan)
    scan.add_argument("--scan-chains", type=int, help="Requested scan chain count")
    scan.add_argument(
        "--max-chain-length",
        type=int,
        help="Maximum FFs per scan chain; may increase chain count",
    )
    scan.add_argument("-SI", "--scan-in", help="Scan input port base name")
    scan.add_argument("-SO", "--scan-out", help="Scan output port base name")
    scan.add_argument("-SE", "--scan-enable", help="Scan enable port name")
    techmap_group = scan.add_mutually_exclusive_group()
    techmap_group.add_argument(
        "--techmap",
        dest="techmap",
        action="store_true",
        default=None,
        help="Run Sky130 techmap after stitching",
    )
    techmap_group.add_argument(
        "--no-techmap",
        dest="techmap",
        action="store_false",
        help="Write scan JSON and techmap file without running Yosys techmap",
    )
    scan.add_argument(
        "--skip-techmap",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    scan.add_argument("--dry-run", action="store_true", help="Print scan plan only")

    scan_status = sub.add_parser("scan-status", help="Print scan insertion status")
    add_common(scan_status)

    scan_check = sub.add_parser("scan-check", help="Validate inserted scan chains")
    add_common(scan_check)
    scan_check.add_argument("--vectors", type=Path, help="Explicit .test vector file")
    scan_check.add_argument(
        "--require-techmap",
        action="store_true",
        help="Require generated Sky130 Verilog artifact",
    )

    scan_techmap = sub.add_parser(
        "scan-techmap", help="Regenerate Sky130 techmap output from scanned JSON"
    )
    add_common(scan_techmap)

    jtag = sub.add_parser(
        "jtag",
        help=(
            "Grade the scan campaign's faults with a JTAG network-integrity "
            "program played through the TAP"
        ),
    )
    add_common(jtag)
    jtag.add_argument(
        "--program",
        type=Path,
        help=(
            "warptap-tck-program JSON to play (default: [jtag] program, else "
            "built from the [autombist] manifest with warptap)"
        ),
    )
    jtag.add_argument(
        "--verify",
        action="store_true",
        help=(
            "Also replay the program on the techmapped netlist in Icarus Verilog "
            "(four-state) and require the golden TDO"
        ),
    )
    jtag.add_argument(
        "--force",
        action="store_true",
        help="Grade again even if an identical grade is recorded",
    )
    jtag.add_argument(
        "--threads", type=int, help="Grading threads (default: [simulation])"
    )
    jtag.add_argument("--export", type=Path, help="Write the TCK program played")

    scan_compress = sub.add_parser(
        "scan-compress",
        help="Insert scan test-pattern compression (ring generator + phase shifter)",
    )
    add_common(scan_compress)

    scan_compact = sub.add_parser(
        "scan-compact",
        help="Insert scan test-response compaction (static XOR-tree space compactor)",
    )
    add_common(scan_compact)

    write_patterns = sub.add_parser(
        "write-patterns",
        help="Write exported scan patterns as STIL (IEEE 1450)",
    )
    add_common(write_patterns)
    write_patterns.add_argument(
        "--patterns",
        type=Path,
        required=True,
        help="Patterns sim --scan --export-patterns wrote",
    )
    write_patterns.add_argument(
        "-o", "--output", type=Path, required=True, help="The STIL file to write"
    )
    write_patterns.add_argument(
        "--no-overlap",
        action="store_true",
        help="Apply each pattern alone, its unload not overlapping the next load",
    )

    rule_check = sub.add_parser(
        "rule_check",
        help="Run DFT structural rules (DRC) on the synthesized netlist",
    )
    add_common(rule_check)
    rule_check.add_argument(
        "--strict",
        action="store_true",
        help="Treat warnings as blocking (non-zero exit)",
    )
    rule_check.add_argument(
        "--advisory",
        action="store_true",
        help="Report violations but always exit 0 (no gate)",
    )

    add_clock = sub.add_parser(
        "add-clock",
        help=(
            "Declare a clock domain in a config.ofs [clocks] section "
            "(equivalent to the Tcl shell's add_clock, for one-shot CLI use)"
        ),
    )
    add_clock.add_argument("port", help="Clock port name")
    add_clock.add_argument(
        "-c", "--config", default="config.ofs", help="Config file to edit"
    )
    add_clock.add_argument(
        "--off",
        dest="off_state",
        type=int,
        choices=[0, 1],
        default=0,
        help="Clock's inactive level (0=active-high/posedge default, 1=negedge)",
    )

    run = sub.add_parser(
        "run",
        help=(
            "Oracle mode: translate an OT-format config, run ATPG, "
            "write oracle_response.json"
        ),
    )
    run.add_argument(
        "-c",
        "--config",
        type=Path,
        required=True,
        help="Path to an OpenTestability-format faultflow.ofs",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "shell":
            return run_shell(
                script=args.file,
                config=args.config,
                output_root=args.out,
                verbose=args.verbose,
            )
        if args.command == "run":
            return _handle_run(args, parser)
        if args.command == "project":
            if args.max is not None and args.max < 1:
                parser.error("--max must be >= 1")
            if args.target_coverage is not None and not (
                0.0 < args.target_coverage <= 100.0
            ):
                parser.error("-t must be in (0, 100]")
            print(
                FlowService(runner_factory=Runner)
                .run_project(
                    args.project,
                    max_rounds=args.max,
                    target_coverage=args.target_coverage,
                    clean=args.clean,
                )
                .message
            )
            return 0
        if args.command == "retarget":
            return _handle_retarget(args)
        if args.command == "autombist-generate":
            return _handle_autombist_generate(args)
        if args.command == "list-memories":
            from faultflow.mbist.memories import list_memories

            print(list_memories(args.spec, args.top, args.pattern), end="")
            return 0
        if args.command == "mbist-insert":
            from faultflow.mbist.insert import insert_command

            print(
                insert_command(
                    args.spec,
                    args.top,
                    args.out,
                    args.config,
                    tap_nonscan=args.tap_nonscan,
                )
            )
            return 0
        if args.command == "add-clock":
            add_clock_to_config(Path(args.config), args.port, off_state=args.off_state)
            print(
                f"declared clock '{args.port}' (off={args.off_state}) in "
                f"{args.config}"
            )
            return 0
        cfg = load_config(Path(args.config), args.top)
        service = FlowService(runner_factory=Runner)
        if args.command == "init":
            print(service.initialize(cfg).message)
        elif args.command == "sim":
            if getattr(args, "model", None) is not None:
                import dataclasses

                model = args.model.replace("-", "_")
                if model == "transition" and cfg.fault_model.collapsing:
                    parser.error(
                        "--model transition cannot be combined with "
                        "[fault_model] collapsing = true"
                    )
                cfg = dataclasses.replace(
                    cfg,
                    fault_model=dataclasses.replace(cfg.fault_model, model=model),
                )
            verify = (
                parse_bool_value(args.verify, "verify")
                if args.verify is not None
                else None
            )
            if args.serial_ref:
                if args.ext is not None:
                    parser.error("--serial-ref cannot be combined with --ext")
                print(service.serial_reference(cfg, scan=args.scan).message)
                return 0
            if args.max is not None and args.max < 1:
                parser.error("--max must be >= 1")
            if args.target_coverage is not None and not (
                0.0 < args.target_coverage <= 100.0
            ):
                parser.error("-t must be in (0, 100]")
            sim_kwargs = {
                "purge": args.purge,
                "clean": args.clean,
                "verify": verify,
                "max_rounds": args.max,
                "target_coverage": args.target_coverage,
                "scan": args.scan,
            }
            if args.export_patterns is not None:
                sim_kwargs["export_patterns"] = args.export_patterns
            if args.ext is None:
                print(service.run_atpg(cfg, **sim_kwargs).message)
            else:
                print(service.run_atpg(cfg, **sim_kwargs, ext=args.ext).message)
        elif args.command in ("intest", "extest"):
            import dataclasses

            if args.max is not None and args.max < 1:
                parser.error("--max must be >= 1")
            if args.target_coverage is not None and not (
                0.0 < args.target_coverage <= 100.0
            ):
                parser.error("-t must be in (0, 100]")
            mode_cfg = dataclasses.replace(cfg, test_mode=args.command)
            print(
                service.run_atpg(
                    mode_cfg,
                    purge=args.purge,
                    clean=args.clean,
                    max_rounds=args.max,
                    target_coverage=args.target_coverage,
                    scan=True,
                ).message
            )
        elif args.command == "status":
            print(service.status(cfg, scan=args.scan).message)
        elif args.command == "scan":
            run_techmap = args.techmap
            if args.skip_techmap:
                run_techmap = False
            print(
                service.insert_scan(
                    cfg,
                    run_techmap=run_techmap,
                    scan_chains=args.scan_chains,
                    max_chain_length=args.max_chain_length,
                    scan_in=args.scan_in,
                    scan_out=args.scan_out,
                    scan_enable=args.scan_enable,
                    dry_run=args.dry_run,
                ).message
            )
        elif args.command == "scan-status":
            print(service.scan_status(cfg).message)
        elif args.command == "scan-check":
            print(
                service.check_scan(
                    cfg,
                    vectors_path=args.vectors,
                    require_techmap=args.require_techmap,
                ).message
            )
        elif args.command == "scan-techmap":
            print(service.regenerate_scan_techmap(cfg).message)
        elif args.command == "jtag":
            from faultflow.jtag.command import run_jtag

            outcome = run_jtag(
                cfg,
                program_path=args.program,
                force=args.force,
                sim_threads=args.threads,
                export=args.export,
                verify=args.verify,
            )
            print(outcome.message(cfg.top))
        elif args.command == "scan-compress":
            print(service.scan_compress(cfg).message)
        elif args.command == "scan-compact":
            print(service.scan_compact(cfg).message)
        elif args.command == "write-patterns":
            written = service.write_patterns(
                cfg, args.patterns, args.output, overlap=not args.no_overlap
            )
            print(written.message)
        elif args.command == "rule_check":
            result = service.rule_check(cfg, strict=args.strict)
            print(result.message)
            if result.passed or args.advisory:
                return 0
            return 1
        else:
            parser.error(f"unknown command {args.command}")
    except (RuntimeError, sqlite3.Error) as exc:
        # Every faultflow domain error subclasses RuntimeError by convention
        # (ConfigError, RunnerError, SchemaError, CoverageError, ScanError,
        # ShellError, and pybind11-mapped C++ engine errors), and sqlite3
        # errors ("database is locked", legacy schema) are equally
        # user-actionable -- map them all to a clean exit-2 message. Genuine
        # programming bugs (TypeError, KeyError, ...) still traceback.
        parser.exit(2, f"error: {exc}\n")
    return 0


def _handle_retarget(args: object) -> int:
    import json

    from faultflow.retarget.emit import pattern_to_dict, write_retargeted
    from faultflow.retarget.soc_access import load_soc_access
    from faultflow.retarget.transform import retarget_block_pattern
    from faultflow.scan.pattern_export import scan_pattern_from_dict

    patterns_path = Path(getattr(args, "patterns"))
    block = str(getattr(args, "block"))
    # Guard the patterns file the same way --soc-access is guarded, so a missing or
    # malformed file gives a clean CLI error (caught in main -> exit 2) instead of a
    # raw FileNotFoundError / JSONDecodeError traceback.
    if not patterns_path.exists():
        raise ConfigError(f"patterns file not found: {patterns_path}")
    try:
        raw = json.loads(patterns_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"patterns file is not valid JSON: {patterns_path}: {exc}")
    block_patterns = [scan_pattern_from_dict(d) for d in raw]
    access = load_soc_access(Path(getattr(args, "soc_access")))
    retargeted = [retarget_block_pattern(p, access, block) for p in block_patterns]
    payload = {
        "schema": "faultflow_retargeted_v1",
        "assembly_top": access.assembly_top,
        "source_block": block,
        "patterns": [
            pattern_to_dict(p, assembly_top=access.assembly_top) for p in retargeted
        ],
    }
    written = write_retargeted(Path(getattr(args, "out")), payload)
    print(f"retargeted {len(retargeted)} pattern(s) from {block!r} -> {written}")
    return 0


def _handle_autombist_generate(args: object) -> int:
    import shlex

    from faultflow.integrations.autombist import run_autombist_generate

    config = Path(getattr(args, "config"))
    if not config.exists():
        raise ConfigError(f"autoMBIST config not found: {config}")
    test_access = bool(getattr(args, "test_access", False))
    tap_nonscan = bool(getattr(args, "tap_nonscan", False))
    if tap_nonscan and not test_access:
        raise ConfigError("--tap-nonscan needs --test-access")
    cmd = tuple(shlex.split(str(getattr(args, "autombist_cmd"))))
    result = run_autombist_generate(
        config,
        Path(getattr(args, "out")),
        autombist_cmd=cmd,
        liberty=Path(getattr(args, "liberty")),
        cell_lib=Path(getattr(args, "cell_lib")),
        test_access=test_access,
        tap_nonscan=tap_nonscan,
        algo=getattr(args, "algo", None),
    )
    counts = ", ".join(f"{k}={v}" for k, v in sorted(result.instance_counts.items()))
    print(
        f"wrote {result.ofs_path}  (top={result.top_module}, "
        f"blocks={result.block_count}, {counts})"
    )
    if result.scan_chains is not None:
        print(
            f"clocks: {', '.join(result.clock_ports)}  "
            f"(scan chains: {result.scan_chains}, one per clock domain)"
        )
    if result.nonscan_cells:
        print(
            f"non-scan: {len(result.nonscan_cells)} TAP/IJTAG instances, held by "
            + ", ".join(f"{port}:{v}" for port, v in result.scan_holds)
            + "; ff.py jtag grades them after sim --scan"
        )
    print(
        f"run: python3 ff.py sim --scan --top {result.top_module} -c {result.ofs_path}"
    )
    return 0


def _handle_run(args: object, parser: argparse.ArgumentParser) -> int:
    from faultflow.service.oracle import run_oracle

    ofs_path = Path(getattr(args, "config"))
    if not ofs_path.exists():
        parser.exit(2, f"error: config not found: {ofs_path}\n")
    try:
        return run_oracle(ofs_path)
    except Exception as exc:
        parser.exit(1, f"error: oracle run failed: {exc}\n")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

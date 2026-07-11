from __future__ import annotations

import argparse
from pathlib import Path

from faultflow.config import (
    ConfigError,
    add_clock_to_config,
    load_config,
    parse_bool_value,
)
from faultflow.runner import Runner, RunnerError
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
        elif args.command == "rule_check":
            result = service.rule_check(cfg, strict=args.strict)
            print(result.message)
            if result.passed or args.advisory:
                return 0
            return 1
        else:
            parser.error(f"unknown command {args.command}")
    except (ConfigError, RunnerError) as exc:
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
    raw = json.loads(patterns_path.read_text(encoding="utf-8"))
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

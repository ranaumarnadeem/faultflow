from __future__ import annotations

import argparse
from pathlib import Path

from faultflow.config import ConfigError, load_config, parse_bool_value
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
        "-t",
        dest="target_coverage",
        type=float,
        metavar="PCT",
        help="Target coverage percent to stop ATPG (default: [report] threshold)",
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
        cfg = load_config(Path(args.config), args.top)
        service = FlowService(runner_factory=Runner)
        if args.command == "init":
            print(service.initialize(cfg).message)
        elif args.command == "sim":
            verify = (
                parse_bool_value(args.verify, "verify")
                if args.verify is not None
                else None
            )
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
        else:
            parser.error(f"unknown command {args.command}")
    except (ConfigError, RunnerError) as exc:
        parser.exit(2, f"error: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

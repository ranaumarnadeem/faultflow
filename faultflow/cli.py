from __future__ import annotations

import argparse
from pathlib import Path

from faultflow.config import ConfigError, load_config, parse_bool_value
from faultflow.runner import Runner, RunnerError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python3 ff.py")
    sub = parser.add_subparsers(dest="command", required=True)

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
        help="Clean transient junk inside output/<top>/ before sim",
    )
    sim.add_argument(
        "-v",
        "--verify",
        help="Override [simulation] verify for this sim run",
    )

    status = sub.add_parser("status", help="Print current coverage status")
    add_common(status)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        cfg = load_config(Path(args.config), args.top)
        runner = Runner(cfg)
        if args.command == "init":
            print(runner.init())
        elif args.command == "sim":
            verify = (
                parse_bool_value(args.verify, "verify")
                if args.verify is not None
                else None
            )
            print(runner.sim(purge=args.purge, verify=verify))
        elif args.command == "status":
            print(runner.status())
        else:
            parser.error(f"unknown command {args.command}")
    except (ConfigError, RunnerError) as exc:
        parser.exit(2, f"error: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

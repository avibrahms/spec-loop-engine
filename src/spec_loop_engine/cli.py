from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .engine import SpecLoopError, exit_code_for_run, print_summary, run_spec, validate_spec
from .spec_loader import load_spec


def _parse_sets(values: list[str]) -> dict[str, str]:
    overrides: dict[str, str] = {}
    for item in values:
        if "=" not in item:
            raise SpecLoopError(f"invalid --set value: {item!r}; expected key=value")
        key, value = item.split("=", 1)
        overrides[key] = value
    return overrides


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="spec-loop", description="Run a spec-driven sequential phase loop.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("spec", type=Path, help="Path to a YAML or JSON spec file")
    common.add_argument("--set", action="append", default=[], metavar="KEY=VALUE", help="Override a spec variable")

    validate_parser = subparsers.add_parser("validate", parents=[common], help="Validate a spec")
    validate_parser.set_defaults(handler=handle_validate)

    run_parser = subparsers.add_parser("run", parents=[common], help="Run a spec")
    run_parser.add_argument("--fresh", action="store_true", help="Start a fresh run instead of resuming the latest unfinished run")
    run_parser.set_defaults(handler=handle_run)

    return parser


def handle_validate(args: argparse.Namespace) -> int:
    spec = load_spec(args.spec, _parse_sets(args.set))
    validate_spec(spec)
    print(f"Valid: {spec.path}")
    print(f"Workspace: {spec.workspace}")
    print(f"Run root: {spec.run_root}")
    print(f"Phases: {len(spec.phases)}")
    return 0


def handle_run(args: argparse.Namespace) -> int:
    spec = load_spec(args.spec, _parse_sets(args.set))
    run_dir = run_spec(spec, fresh=args.fresh)
    print_summary(run_dir)
    return exit_code_for_run(run_dir)


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        code = args.handler(args)
    except SpecLoopError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    raise SystemExit(code)

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .core.contracts import SCHEMA_VERSION, ContractError
from .core.pipeline import doctor, read_artifact, run_research
from .core.storage import initialize_workspace
from .ingest import collect_cn_snapshot


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="a-share-research",
        description="Auditable A-share theme research engine (research only; no trading)",
    )
    parser.add_argument(
        "--workspace",
        help="Runtime workspace. Defaults to STOCK_RESEARCH_WORKSPACE or the project directory.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init", help="Create the SQLite database and runtime directories")
    subparsers.add_parser("doctor", help="Check the local runtime without accessing the network")

    run_parser = subparsers.add_parser("run", help="Run a versioned JSON research request")
    run_parser.add_argument(
        "--request-json",
        required=True,
        help="JSON file path or '-' to read exactly one JSON object from stdin",
    )

    read_parser = subparsers.add_parser("artifact-read", help="Read a bounded artifact section")
    read_parser.add_argument(
        "--request-json",
        required=True,
        help="JSON file path or '-' to read exactly one JSON object from stdin",
    )

    collect_parser = subparsers.add_parser(
        "collect-snapshot",
        help="Collect real BaoStock market data and build a normalized snapshot from a seed",
    )
    collect_parser.add_argument(
        "--seed-json",
        required=True,
        help="Editorial research seed JSON without market data",
    )
    collect_parser.add_argument(
        "--snapshot-id",
        required=True,
        help="Immutable identifier for the normalized snapshot",
    )

    demo_parser = subparsers.add_parser(
        "demo", help="Run the explicit synthetic installation fixture"
    )
    demo_parser.add_argument(
        "--decision-at",
        default="2026-08-16T08:30:00+08:00",
        help="Timezone-aware research cut-off used by the synthetic fixture",
    )
    demo_parser.add_argument("--top-n", type=int, default=5)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    workspace = _workspace(args.workspace)
    try:
        if args.command == "init":
            result: Any = initialize_workspace(workspace)
        elif args.command == "doctor":
            result = doctor(workspace)
        elif args.command == "run":
            result = run_research(_read_json(args.request_json), workspace)
        elif args.command == "artifact-read":
            result = read_artifact(_read_json(args.request_json), workspace)
        elif args.command == "collect-snapshot":
            collected = collect_cn_snapshot(
                _read_json(args.seed_json),
                workspace=workspace,
                snapshot_id=args.snapshot_id,
            )
            result = collected.to_dict()
        else:
            result = run_research(
                {
                    "schema_version": SCHEMA_VERSION,
                    "workflow": "daily_report",
                    "decision_at": args.decision_at,
                    "snapshot": {"selector": "demo"},
                    "top_n": args.top_n,
                },
                workspace,
            )
    except (
        ContractError,
        KeyError,
        RuntimeError,
        TypeError,
        ValueError,
        OSError,
        json.JSONDecodeError,
    ) as exc:
        print(
            json.dumps(
                {
                    "schema_version": SCHEMA_VERSION,
                    "market": "CN",
                    "error": type(exc).__name__,
                    "message": _safe_error_message(exc),
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _workspace(argument: str | None) -> Path:
    configured = argument or os.environ.get("STOCK_RESEARCH_WORKSPACE")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(__file__).resolve().parents[2]


def _read_json(value: str) -> dict[str, Any]:
    if value == "-":
        raw = json.load(sys.stdin)
    else:
        with Path(value).expanduser().open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
    if not isinstance(raw, dict):
        raise ContractError("request JSON root must be an object")
    return raw


def _safe_error_message(exc: BaseException) -> str:
    if isinstance(exc, OSError):
        return "filesystem operation failed"
    return str(exc)


if __name__ == "__main__":
    raise SystemExit(main())

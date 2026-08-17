from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .core.contracts import MARKET, SCHEMA_VERSION, ContractError
from .core.pipeline import doctor, read_artifact, run_research
from .core.storage import initialize_workspace
from .ingest.sec import collect_sec_snapshot


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="us-equity-research",
        description="Auditable US equity research engine (research only; no trading)",
    )
    parser.add_argument(
        "--workspace",
        help="Runtime workspace. Defaults to STOCK_RESEARCH_WORKSPACE or the project directory.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init", help="Create the US SQLite database and runtime directories")
    subparsers.add_parser("doctor", help="Check the offline US research runtime")

    collect_parser = subparsers.add_parser(
        "collect-sec-snapshot",
        help="Build one immutable US snapshot from SEC JSON and an explicit research seed",
    )
    collect_parser.add_argument("--seed-json", required=True, help="Research seed JSON path")
    collect_parser.add_argument("--snapshot-id", required=True, help="Immutable snapshot ID")
    collect_parser.add_argument(
        "--retrieved-at",
        help="Timezone-aware retrieval timestamp; defaults to current UTC time",
    )
    collect_parser.add_argument(
        "--market-json",
        help="Optional BYOK licensed structured-market JSON path",
    )

    run_parser = subparsers.add_parser("run", help="Run a versioned JSON research request")
    run_parser.add_argument(
        "--request-json",
        required=True,
        help="JSON file path or '-' to read exactly one JSON object from stdin",
    )

    read_parser = subparsers.add_parser("artifact-read", help="Read one bounded artifact section")
    read_parser.add_argument(
        "--request-json",
        required=True,
        help="JSON file path or '-' to read exactly one JSON object from stdin",
    )

    demo_parser = subparsers.add_parser("demo", help="Run the explicit synthetic fixture")
    demo_parser.add_argument(
        "--decision-at",
        default="2026-08-16T08:30:00-04:00",
        help="Timezone-aware research cut-off used by the synthetic demo request",
    )
    demo_parser.add_argument("--top-n", type=int, default=5)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    workspace = _workspace(args.workspace)
    try:
        if args.command == "init":
            result: dict[str, Any] = initialize_workspace(workspace)
        elif args.command == "doctor":
            result = doctor(workspace)
        elif args.command == "collect-sec-snapshot":
            result = collect_sec_snapshot(
                workspace=workspace,
                seed_path=Path(args.seed_json),
                snapshot_id=args.snapshot_id,
                retrieved_at=args.retrieved_at,
                market_path=Path(args.market_json) if args.market_json else None,
            )
        elif args.command == "run":
            result = run_research(_read_json(args.request_json), workspace)
        elif args.command == "artifact-read":
            result = read_artifact(_read_json(args.request_json), workspace)
        elif args.command == "demo":
            result = run_research(
                {
                    "schema_version": SCHEMA_VERSION,
                    "market": MARKET,
                    "workflow": "daily_report",
                    "decision_at": args.decision_at,
                    "snapshot": {"selector": "demo"},
                    "top_n": args.top_n,
                },
                workspace,
            )
        else:  # pragma: no cover - argparse enforces the command choices
            raise RuntimeError("unsupported command")
    except (
        ContractError,
        KeyError,
        RuntimeError,
        TypeError,
        ValueError,
        OSError,
        json.JSONDecodeError,
    ) as exc:
        _write_json(
            {
                "schema_version": SCHEMA_VERSION,
                "market": MARKET,
                "error": type(exc).__name__,
                "message": _safe_error_message(exc),
            },
            stream=sys.stderr,
        )
        return 2

    _write_json(result)
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


def _write_json(payload: dict[str, Any], *, stream: Any = sys.stdout) -> None:
    stream.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")


def _safe_error_message(exc: BaseException) -> str:
    if isinstance(exc, OSError):
        return "filesystem operation failed"
    return str(exc)


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .core.contracts import MARKET, SCHEMA_VERSION, ArtifactReadRequest, ContractError, RunRequest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="us-equity-research",
        description="Versioned US equity research contracts and CLI shell",
    )
    parser.add_argument(
        "--workspace",
        help="Runtime workspace. Defaults to STOCK_RESEARCH_WORKSPACE or the project directory.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init", help="Create the runtime directories")
    subparsers.add_parser("doctor", help="Check contract-only runtime availability")

    run_parser = subparsers.add_parser("run", help="Validate a versioned JSON research request")
    run_parser.add_argument(
        "--request-json",
        required=True,
        help="JSON file path or '-' to read exactly one JSON object from stdin",
    )

    read_parser = subparsers.add_parser("artifact-read", help="Validate an artifact read request")
    read_parser.add_argument(
        "--request-json",
        required=True,
        help="JSON file path or '-' to read exactly one JSON object from stdin",
    )

    demo_parser = subparsers.add_parser("demo", help="Emit the canonical demo request envelope")
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
            _ensure_runtime_dirs(workspace)
            result: dict[str, Any] = {
                "schema_version": SCHEMA_VERSION,
                "market": MARKET,
                "command": "init",
                "status": "ok",
            }
        elif args.command == "doctor":
            result = {
                "schema_version": SCHEMA_VERSION,
                "market": MARKET,
                "command": "doctor",
                "status": "ok",
                "capabilities": {
                    "contracts": "available",
                    "pipeline": "not_implemented",
                },
            }
        elif args.command == "run":
            request = RunRequest.from_dict(_read_json(args.request_json))
            result = _not_implemented_result("run", request.to_dict())
        elif args.command == "artifact-read":
            request = ArtifactReadRequest.from_dict(_read_json(args.request_json))
            result = _not_implemented_result("artifact-read", request.to_dict())
        else:
            request = RunRequest.from_dict(
                {
                    "schema_version": SCHEMA_VERSION,
                    "market": MARKET,
                    "workflow": "daily_report",
                    "decision_at": args.decision_at,
                    "snapshot": {"selector": "demo"},
                    "top_n": args.top_n,
                }
            )
            result = _not_implemented_result("demo", request.to_dict())
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
                "message": str(exc),
            },
            stream=sys.stderr,
        )
        return 2

    _write_json(result)
    return 0


def _not_implemented_result(command: str, request: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "market": MARKET,
        "command": command,
        "status": "not_implemented",
        "request": request,
    }


def _workspace(argument: str | None) -> Path:
    configured = argument or os.environ.get("STOCK_RESEARCH_WORKSPACE")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(__file__).resolve().parents[2]


def _ensure_runtime_dirs(workspace: Path) -> None:
    for relative in (
        Path("data") / "normalized" / "us",
        Path("artifacts") / "us",
    ):
        (workspace / relative).mkdir(parents=True, exist_ok=True)


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


if __name__ == "__main__":
    raise SystemExit(main())

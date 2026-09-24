from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .core.contracts import MARKET, SCHEMA_VERSION, ContractError
from .core.daily_review import verify_review, write_review
from .core.drift import audit_snapshot_drift
from .core.outcomes import record_outcome, summarize_outcome_history, summarize_outcomes
from .core.pipeline import doctor, read_artifact, run_research
from .core.storage import initialize_workspace
from .ingest.futu_mcp import FutuMCPTransport
from .ingest.futu_quotes import FutuMCPQuoteClient, observation_report, write_new_bundle
from .ingest.futu_quotes import collect as collect_futu
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
        "--market-json",
        help="Optional BYOK licensed structured-market JSON path",
    )

    futu_parser = subparsers.add_parser(
        "collect-futu-quotes", help="Collect US daily quote staging data using quote-only REST"
    )
    futu_parser.add_argument("--plan-json", required=True)
    futu_parser.add_argument("--output", required=True)
    futu_parser.add_argument("--transport", choices=["rest", "mcp"], default="rest")

    review_parser = subparsers.add_parser(
        "daily-review", help="Write an immutable daily review with explicit capability gaps"
    )
    review_parser.add_argument("--bundle-json", required=True)
    review_parser.add_argument("--calendar-json", required=True)
    review_parser.add_argument("--sec-snapshot-json")
    review_parser.add_argument("--fundamentals-json")
    review_parser.add_argument("--decision-at", required=True)
    review_parser.add_argument("--output-dir", required=True)
    verify_parser = subparsers.add_parser(
        "verify-daily-review", help="Verify hashes and recompute daily review from frozen inputs"
    )
    verify_parser.add_argument("--output-dir", required=True)
    calendar_parser = subparsers.add_parser(
        "refresh-us-calendar", help="Fetch official core calendar without guessing holidays"
    )
    calendar_parser.add_argument("--start", required=True)
    calendar_parser.add_argument("--end", required=True)
    calendar_parser.add_argument("--output", required=True)
    fundamental_parser = subparsers.add_parser(
        "collect-fundamentals", help="Collect SEC inputs with TTM lineage"
    )
    fundamental_parser.add_argument("--seed-json", required=True)
    fundamental_parser.add_argument("--output", required=True)
    ledger_parser = subparsers.add_parser(
        "ledger-add", help="Append a first-observed research record"
    )
    ledger_parser.add_argument("--record-json", required=True)
    ledger_parser.add_argument("--directory", required=True)
    expectation_parser = subparsers.add_parser(
        "ledger-expectation", help="Read only an expectation actually observed by the cutoff"
    )
    for option in (
        "directory",
        "symbol",
        "metric",
        "period-end",
        "decision-at",
        "period-type",
        "accounting-basis",
    ):
        expectation_parser.add_argument("--" + option, required=True)

    observe_parser = subparsers.add_parser(
        "futu-observation-report",
        help="Render staged US daily observations using an explicitly reviewed calendar",
    )
    observe_parser.add_argument("--bundle-json", required=True)
    observe_parser.add_argument("--calendar-json", required=True)
    observe_parser.add_argument("--decision-at", required=True)
    observe_parser.add_argument("--output", required=True)

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

    read_parser.add_argument(
        "--complete",
        action="store_true",
        help="Verify and concatenate every artifact page",
    )

    for command, help_text in (
        ("outcome-record", "Append one immutable future outcome observation"),
        ("outcome-summary", "Summarize only outcomes available by evaluation_at"),
        ("outcome-history", "Read prior outcome scorecards available by evaluation_at"),
    ):
        outcome_parser = subparsers.add_parser(command, help=help_text)
        outcome_parser.add_argument(
            "--request-json",
            required=True,
            help="JSON file path or '-' to read exactly one JSON object from stdin",
        )

    drift_parser = subparsers.add_parser(
        "audit-drift",
        help="Compare two frozen snapshots and persist an immutable drift receipt",
    )
    drift_parser.add_argument(
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
                market_path=Path(args.market_json) if args.market_json else None,
            )
        elif args.command == "collect-futu-quotes":
            client = FutuMCPQuoteClient(FutuMCPTransport()) if args.transport == "mcp" else None
            result_bundle = collect_futu(_read_json(args.plan_json), client)
            write_new_bundle(result_bundle, args.output)
            result = {
                "market": "US",
                "status": result_bundle["status"],
                "symbols": len(result_bundle["securities"]),
                "retrieved_at": result_bundle["retrieved_at"],
            }
        elif args.command == "refresh-us-calendar":
            from datetime import UTC, datetime

            from .ingest.exchange_calendar import build_calendar, fetch_schedule

            schedule = fetch_schedule(int(args.start[:4]))
            bundle = build_calendar(schedule, args.start, args.end, datetime.now(UTC).isoformat())
            write_new_bundle(bundle, args.output)
            result = {
                "status": "REFRESHED",
                "sessions": len(bundle["session_calendar"]["sessions"]),
            }
        elif args.command == "collect-fundamentals":
            from .ingest.fundamentals import collect_fundamentals
            from .ingest.sec import SecClient

            bundle = collect_fundamentals(
                _read_json(args.seed_json),
                SecClient(user_agent=os.environ.get("SEC_USER_AGENT", "")),
            )
            write_new_bundle(bundle, args.output)
            result = {"status": "COLLECTED", "companies": len(bundle["companies"])}
        elif args.command == "ledger-add":
            from .core.research_ledger import append_record

            result = append_record(args.directory, _read_json(args.record_json))
        elif args.command == "ledger-expectation":
            from .core.research_ledger import select_expectation

            record = select_expectation(
                args.directory,
                args.symbol,
                args.metric,
                args.period_end,
                args.decision_at,
                period_type=args.period_type,
                accounting_basis=args.accounting_basis,
            )
            result = {"status": "FOUND" if record else "UNKNOWN", "record": record}
        elif args.command == "daily-review":
            result = write_review(
                _read_json(args.bundle_json),
                _read_json(args.calendar_json),
                _read_json(args.sec_snapshot_json) if args.sec_snapshot_json else None,
                args.decision_at,
                args.output_dir,
                _read_json(args.fundamentals_json) if args.fundamentals_json else None,
            )
        elif args.command == "verify-daily-review":
            result = verify_review(args.output_dir)
        elif args.command == "futu-observation-report":
            report = observation_report(
                _read_json(args.bundle_json), _read_json(args.calendar_json), args.decision_at
            )
            with Path(args.output).open("x", encoding="utf-8") as handle:
                handle.write(report)
            result = {
                "market": "US",
                "status": "STAGED_OBSERVATIONS_ONLY",
                "characters": len(report),
            }
        elif args.command == "run":
            result = run_research(_read_json(args.request_json), workspace)
        elif args.command == "artifact-read":
            request = _read_json(args.request_json)
            if args.complete and request.get("cursor", 0) != 0:
                raise ContractError("--complete requires cursor 0")
            result = read_artifact(request, workspace)
            if args.complete:
                parts = [result["content"]]
                page = result
                while page["next_cursor"] is not None:
                    page = read_artifact({**request, "cursor": page["next_cursor"]}, workspace)
                    parts.append(page["content"])
                result.update(content="".join(parts), truncated=False, next_cursor=None)
                if len(result["content"]) != result["total_chars"]:
                    raise ContractError("complete artifact length mismatch")
                result["full_report_verified"] = True
        elif args.command == "outcome-record":
            result = record_outcome(_read_json(args.request_json), workspace)
        elif args.command == "outcome-summary":
            result = summarize_outcomes(_read_json(args.request_json), workspace)
        elif args.command == "outcome-history":
            result = summarize_outcome_history(_read_json(args.request_json), workspace)
        elif args.command == "audit-drift":
            result = audit_snapshot_drift(_read_json(args.request_json), workspace)
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

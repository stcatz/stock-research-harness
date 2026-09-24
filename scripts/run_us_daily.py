#!/usr/bin/env python3
"""Local US daily entry point. Explicit Codex quote authentication is opt-in.

Run with the US project's Python. No credentials are copied to outputs or REST.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from us_equity_research.core.contracts import ContractError
from us_equity_research.core.daily_review import analyze, verify_review, write_review
from us_equity_research.ingest.futu_mcp import MCP_URL, FutuMCPTransport
from us_equity_research.ingest.futu_quotes import (
    FutuMCPQuoteClient,
    collect,
    write_new_bundle,
)
from us_equity_research.ingest.sec import collect_sec_snapshot


def main():
    parser = argparse.ArgumentParser(
        description="Quote collection, SEC refresh and immutable US daily review"
    )
    parser.add_argument("--root", required=True)
    parser.add_argument("--plan-json", required=True)
    parser.add_argument(
        "--calendar-json", help="Reviewed calendar; omit to refresh official schedule"
    )
    parser.add_argument("--seed-json", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--codex-mcp-auth", action="store_true")
    parser.add_argument(
        "--quotes-json",
        help="Explicit frozen quote reuse; original retrieval time is preserved",
    )
    args = parser.parse_args()
    root = Path(args.root).resolve()
    from us_equity_research.core.contracts import validate_identifier

    run_id = validate_identifier(args.run_id, "run_id")
    out = root / "artifacts/us/daily_reviews" / run_id
    if out.exists():
        raise ContractError(
            "Daily review already exists; choose a new immutable run ID"
        )
    token = None
    if args.codex_mcp_auth and not args.quotes_json:
        # Host adapter only; select the named official server and require exact scope.
        credentials = json.loads((Path.home() / ".codex/.credentials.json").read_text())
        matches = [
            v
            for v in credentials.values()
            if isinstance(v, dict)
            and v.get("server_name") == "futu-quotes"
            and v.get("server_url") == MCP_URL
        ]
        if len(matches) != 1 or matches[0].get("scopes") != ["quote:read"]:
            raise ContractError("Expected exactly one quote-only Futu authorization")
        token = matches[0]["access_token"]
    ua = root / ".runtime/us-sec-user-agent.txt"
    if not os.environ.get("SEC_USER_AGENT") and ua.exists():
        os.environ["SEC_USER_AGENT"] = ua.read_text().strip()
    plan = json.loads(Path(args.plan_json).read_text())
    calendar = (
        json.loads(Path(args.calendar_json).read_text()) if args.calendar_json else None
    )
    if args.quotes_json:
        bundle = json.loads(Path(args.quotes_json).read_text())
        if bundle.get("plan") != plan:
            raise ContractError("Reused quotes do not match frozen plan")
    else:
        bundle = collect(plan, FutuMCPQuoteClient(FutuMCPTransport(token)))
    if calendar is None:
        from datetime import timedelta
        from zoneinfo import ZoneInfo

        from us_equity_research.ingest.exchange_calendar import (
            build_calendar,
            fetch_schedule,
        )

        today = datetime.now(ZoneInfo("America/New_York")).date()
        schedule = fetch_schedule(today.year)
        calendar = build_calendar(
            schedule,
            plan["start"],
            (today + timedelta(days=14)).isoformat(),
            datetime.now(UTC).isoformat(),
            {
                "start": plan["start"],
                "end": plan["end"],
                "days": bundle["supplier_calendar"],
            },
        )
    analyze(bundle, calendar, None, datetime.now(UTC).isoformat())
    staging = root / "data/normalized/us_daily" / run_id
    staging.mkdir(parents=True, exist_ok=False)
    write_new_bundle(bundle, staging / "quotes.json")
    write_new_bundle(calendar, staging / "calendar.json")
    sec_snapshot = None
    fundamentals = None
    filing_bodies = None
    sec_status = "SEC_USER_AGENT_MISSING"
    if os.environ.get("SEC_USER_AGENT"):
        from us_equity_research.ingest.sec import SecSessionClient

        sec_client = SecSessionClient(user_agent=os.environ["SEC_USER_AGENT"])
        seed = json.loads(Path(args.seed_json).read_text())
        seed["as_of"] = datetime.now(UTC).isoformat()
        write_new_bundle(seed, staging / "seed.json")
        try:
            r = collect_sec_snapshot(
                workspace=root,
                seed_path=staging / "seed.json",
                snapshot_id=run_id + "-sec",
                client=sec_client,
            )
            sec_snapshot = json.loads((root / r["relative_path"]).read_text())
            sec_status = "CURRENT_COLLECTION"
        except (ContractError, OSError, ValueError, RuntimeError):
            sec_status = "SEC_COLLECTION_FAILED"
        if sec_snapshot is not None:
            from us_equity_research.ingest.fundamentals import collect_fundamentals

            fundamentals = collect_fundamentals(seed, sec_client)
            write_new_bundle(fundamentals, staging / "fundamentals.json")
    if sec_snapshot is not None and fundamentals is not None:
        from us_equity_research.ingest.filing_body import fetch_body

        filing_bodies = {"schema_version": "us-filing-body-1", "documents": []}
        for company in fundamentals["companies"]:
            urls = {f["source_url"] for f in company["facts"]}
            candidates = [
                e
                for e in sec_snapshot["evidence"]
                if e["source_url"] in urls
                and ("10-K" in e["title"] or "10-Q" in e["title"])
            ]
            if not candidates:
                continue
            evidence = max(candidates, key=lambda e: e["published_at"])
            try:
                body = fetch_body(
                    evidence, company["symbol"], os.environ["SEC_USER_AGENT"]
                )
            except ContractError:
                body = {
                    "symbol": company["symbol"],
                    "source_url": evidence["source_url"],
                    "retrieved_at": datetime.now(UTC).isoformat(),
                    "facts": [],
                    "status": "FETCH_FAILED",
                }
            filing_bodies["documents"].append(body)
        write_new_bundle(filing_bodies, staging / "filing_bodies.json")
    result = write_review(
        bundle,
        calendar,
        sec_snapshot,
        datetime.now(UTC).isoformat(),
        out,
        fundamentals,
        filing_bodies,
    )
    import hashlib

    from us_equity_research.core.research_ledger import append_record

    packet = json.loads((out / "review.json").read_text())
    plan_records = []
    for candidate in packet["sec"]["candidates"]:
        now = datetime.now(UTC).isoformat()
        record = {
            "kind": "plan",
            "symbol": candidate["symbol"],
            "source_url": "UNKNOWN",
            "source_level": "research_plan",
            "artifact_ref": hashlib.sha256(
                (out / "manifest.json").read_bytes()
            ).hexdigest(),
            "published_at": now,
            "available_at": now,
            "retrieved_at": now,
            "as_of": now,
            "effective_at": packet["checks"][-1]["new_york"]
            if packet["checks"]
            else now,
            "text": json.dumps(packet["observation_plan"], ensure_ascii=False),
        }
        plan_records.append(
            append_record(root / "data/us_research_ledger", record)["record_id"]
        )
    print(
        json.dumps(
            {
                "status": result["status"],
                "sec_status": sec_status,
                "verification": verify_review(out)["status"],
                "report": str(out / "report.md"),
                "plan_record_ids": plan_records,
            }
        )
    )


if __name__ == "__main__":
    try:
        main()
    except (ContractError, OSError, ValueError, KeyError, TypeError, RuntimeError):
        # Never echo transport exceptions, credentials or raw vendor responses.
        raise SystemExit(
            "US daily run failed; verify quote authorization, reviewed inputs and connectivity."
        ) from None

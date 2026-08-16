# US Equity Research Engine v0.1

`us_equity_research/` is an offline, auditable US-equity research engine. It consumes point-in-time snapshots, applies deterministic evidence gates and financial calculations, stores immutable artifacts, and renders a cited report. The Python engine is the only canonical writer; the DeepSeek Harness adapter is a thin client that can trigger runs and read bounded artifact sections, but it never rewrites the canonical report.

## What v0.1 does

- Fixed `market=US` handshake and strict versioned JSON contracts.
- Three workflows: `daily_report`, `theme_research`, and `stock_research`.
- Point-in-time validation across `published_at`, `effective_at`, `available_at`, `retrieved_at`, `as_of`, and `decision_at`.
- Deterministic calculations with formula strings, `input_fact_ids`, and explicit `OK` / `UNKNOWN` / `NOT_MEANINGFUL` states.
- Immutable `packet.json`, `report.md`, and `manifest.json` artifacts plus SQLite audit records.
- Research-only decisions: `exclude`, `continue_research`, `observe`.

## Current limits

v0.1 is intentionally narrow:

- No network ingestion for SEC, issuer IR, market data, macro, or transcripts.
- No consensus estimates, transcript licensing, full DCF, target prices, portfolio construction, broker integrations, or scheduled jobs.
- No promise of production data coverage. Real runs only consume local snapshots that already satisfy the schema.
- `demo` uses synthetic issuers and URLs only. Every fixture output stays visibly marked `fixture` / `FIXTURE`.

## Install and test

```bash
cd ~/ai/stock/us_equity_research
uv sync
uv run python -m unittest discover -s tests -v
uvx ruff check src tests
uvx ruff format --check src tests
uv run python -m compileall -q src tests
```

Quick runtime checks:

```bash
uv run us-equity-research doctor
uv run us-equity-research demo
```

Runtime output stays outside Git:

```text
artifacts/us/runs/<run_id>/
reports/us/daily/<date>-<run_hash>.md
data/us_stock_research.sqlite3
data/normalized/us/<snapshot_id>/snapshot.json
```

Raw filings, issuer decks, market-provider responses, API keys, generated reports, and absolute artifact paths are not shipped in Git.

## Versioned CLI contracts

Initialize workspace state and verify the runtime:

```bash
uv run us-equity-research init
uv run us-equity-research doctor
```

`run` always accepts one JSON object through stdin or a file passed to `--request-json`.

Daily report against the explicit fixture:

```bash
printf '%s' '{
  "schema_version": "0.1",
  "market": "US",
  "workflow": "daily_report",
  "decision_at": "2026-08-16T08:30:00-04:00",
  "snapshot": {"selector": "demo"},
  "top_n": 5
}' | uv run us-equity-research run --request-json -
```

Daily report against the latest validated real snapshot:

```bash
printf '%s' '{
  "schema_version": "0.1",
  "market": "US",
  "workflow": "daily_report",
  "decision_at": "2026-08-16T08:30:00-04:00",
  "snapshot": {"selector": "latest"},
  "top_n": 5
}' | uv run us-equity-research run --request-json -
```

Theme research for a subject contained in the snapshot theme ID or name:

```bash
printf '%s' '{
  "schema_version": "0.1",
  "market": "US",
  "workflow": "theme_research",
  "decision_at": "2026-08-16T08:30:00-04:00",
  "subject": "automation",
  "snapshot": {"selector": "id", "snapshot_id": "us-example-20260816"},
  "top_n": 5
}' | uv run us-equity-research run --request-json -
```

Stock research for one normalized US ticker:

```bash
printf '%s' '{
  "schema_version": "0.1",
  "market": "US",
  "workflow": "stock_research",
  "decision_at": "2026-08-16T08:30:00-04:00",
  "symbol": "BRK.B",
  "snapshot": {"selector": "id", "snapshot_id": "us-example-20260816"},
  "top_n": 5
}' | uv run us-equity-research run --request-json -
```

Workflow rules are fixed:

- `daily_report`: `subject` forbidden, `symbol` forbidden.
- `theme_research`: `subject` required, `symbol` forbidden.
- `stock_research`: `subject` forbidden, `symbol` required.

Real snapshots must live at:

```text
<workspace>/data/normalized/us/<snapshot_id>/snapshot.json
```

`snapshot.selector=latest` sorts by validated `(retrieved_at, snapshot_id)` rather than directory mtime. `snapshot.selector=id` requires the exact `snapshot.snapshot_id`.

## Deterministic analysis semantics

This engine is not a free-form writing assistant. It uses a fixed Evidence -> Claim -> Thesis chain:

1. Evidence: SEC/issuer/official-macro/market records with full time fields.
2. Claim: company or theme statements supported by exact evidence refs and fact refs.
3. Thesis: the auditable research conclusion with bull case, bear case, risk verdict, invalidation conditions, and next catalyst.

Supported calculations include revenue growth, operating margin, free-cash-flow margin, market cap, net cash, enterprise value, EV/revenue, and free-cash-flow yield. Each output carries the formula, `input_fact_ids`, and explicit status. Missing inputs, time-boundary failures, zero denominators, or invalid price bases produce `UNKNOWN` or `NOT_MEANINGFUL`, never invented arithmetic.

## Reading artifacts

Use the bounded artifact reader for `summary`, `report`, `manifest`, or `packet`:

```bash
printf '%s' '{
  "artifact_id": "us-artifact-demo_01",
  "section": "report",
  "max_chars": 12000
}' | uv run us-equity-research artifact-read --request-json -
```

The read result returns an opaque `artifact_id`, bounded content, and a relative path only. It never returns credentials, raw provider payloads, or absolute local paths.

## DeepSeek Harness

The adapter in `adapter-pkg/` exposes exactly two tools:

- `us_research_run`
- `us_artifact_read`

It is a thin client for headless and Web profiles. It forwards versioned requests to `uv run python -m us_equity_research.cli --workspace <workspace> ...`, returns bounded results, and does not read SQLite, provider APIs, or artifact directories directly.

For remote use, prefer an SSH tunnel to a loopback-bound Web profile rather than a public listener. Example flow:

1. SSH to the remote Mac and start `dsh --profile web --host 127.0.0.1 --port <port>`.
2. Forward the port locally with SSH tunneling.
3. Keep DSH profile installation explicit and manual; `scripts/deploy_us_remote.sh` intentionally does not mutate DSH profiles.

See `adapter-pkg/README.md` for the exact DSH package commands.

## Source policy

- Primary evidence: SEC EDGAR filings and issuer IR materials.
- Official macro: BLS, BEA, and other official releases captured into the snapshot.
- Structured market data: a BYOK licensed market-data provider with clear redistribution and local-storage rights.
- Secondary commentary: research aid only, never sole support for a claim.
- FRED API: do not add it without separate permission review.

## Inspiration, attribution, and license boundary

This package implements original repo-specific code and contracts. It borrows public ideas, not upstream source:

- FinRobot: traceable formulas tied to input fact IDs.
- Dexter: versioned request envelopes, append-only manifests, bounded artifact retrieval.
- TradingAgents: separate bull, bear, and risk viewpoints.
- ai-hedge-fund: research mandate as code, without execution privileges.
- StockBench / FINSABER: fixed `decision_at`, PIT exclusion rules, and replay-friendly evaluation.
- Anthropic financial-services: recognizable daily/theme/stock workflow framing and professional financial-worker report sections.

The repository code is MIT-licensed under the root `LICENSE`. External projects, papers, and guidance remain under their original licenses or terms. No claim is made that their code, prompts, datasets, or proprietary materials were copied into this package. If future work imports upstream code or text, preserve the original attribution and license notice alongside that material.

本报告仅用于研究，不构成投资建议，所有事实与交易判断须由用户独立复核。

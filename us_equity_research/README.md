# US Equity Research Engine v0.1

`us_equity_research/` is an auditable US-equity research engine. Research runs consume frozen snapshots offline; a separate, explicit SEC collector can build one snapshot from submissions/companyfacts metadata and an operator-reviewed research seed. The Python engine is the only canonical writer; the DeepSeek Harness adapter is a thin client that can trigger runs and read bounded artifact sections, but it never rewrites the canonical report.

## What v0.1 does

- Fixed `market=US` handshake and strict versioned JSON contracts.
- Three workflows: `daily_report`, `theme_research`, and `stock_research`.
- Point-in-time validation across `published_at`, `effective_at`, `available_at`, `retrieved_at`, `as_of`, and `decision_at`.
- Deterministic calculations with formula strings, `input_fact_ids`, and explicit `OK` / `UNKNOWN` / `NOT_MEANINGFUL` states.
- Immutable `research_packet.json`, `report.md`, and `manifest.json` artifacts plus SQLite audit records.
- Research-only decisions: `exclude`, `continue_research`, `observe`.

## Current limits

v0.1 is intentionally narrow:

- The SEC collector does not parse filing prose and therefore does not claim that researcher-authored bull, bear, risk, or dimension narratives are verified by filing existence alone.
- No issuer-IR, macro, transcript, consensus-estimate, full DCF, target-price, portfolio-construction, broker, or scheduled-job integration.
- No bundled market-data license. Price and valuation fields remain `UNKNOWN` unless the operator supplies a separately authorized market JSON.
- No promise of production data coverage or strict first-public-availability replay. SEC snapshots are conservatively marked `P2`.
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

## Collect a real SEC snapshot

The tracked [sec-seed.example.json](config/sec-seed.example.json) is a non-publishable format template. Copy it outside the tracked source tree, remove `example_notice`, set `as_of` to the intended data-observation cutoff, and manually verify every theme and candidate narrative. The collector rejects the tracked example before making a network request.

SEC requires a descriptive `User-Agent` containing a real contact address. It is a request identity, not an API key:

```bash
cd ~/ai/stock/us_equity_research
uv sync
cp config/sec-seed.example.json ~/ai/stock/data/seeds/us-sec-seed.json
# Edit the copy: remove example_notice, update as_of, and verify all narrative fields.

export SEC_USER_AGENT='stock-research-harness your-real-contact@example.com'
uv run us-equity-research \
  --workspace ~/ai/stock \
  collect-sec-snapshot \
  --seed-json ~/ai/stock/data/seeds/us-sec-seed.json \
  --snapshot-id us-20260818-sec-v1
```

The production CLI always records its own UTC collection clock; it has no public option for relabeling `retrieved_at`. `seed.as_of` limits the observation/reporting period, while the later `decision_at` in each run is the knowledge cutoff. A filing may describe an earlier period but become available later; its `available_at` is preserved so a historical run can exclude it correctly. The collector checks CIK/ticker ownership, handles supported `/A` amendment forms, and keeps annual fiscal-year metrics distinct from TTM metrics.

`--market-json /path/to/market.json` is optional. The payload must explicitly attest `authorized_for_local_research_snapshot`, provide one finite positive `USD/share` close for every candidate, and preserve aligned evidence/fact timestamps. Without it, the snapshot is still valid SEC research input, but market gates and valuation calculations fail closed.

To collect, validate, run by the exact new snapshot ID, and read the complete report in one command:

```bash
SEC_USER_AGENT='stock-research-harness your-real-contact@example.com' \
bash ~/ai/stock/scripts/run_us_validation.sh \
  --root ~/ai/stock \
  --seed-json ~/ai/stock/data/seeds/us-sec-seed.json \
  --snapshot-id us-20260818-validation-v1
```

This wrapper never calls the demo fixture and has no broker capability. An SEC-only report is expected to return `exclude` or `continue_research` while market evidence and verified narrative mappings are absent; that is the intended fail-closed result.

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

It is a thin client for headless and Web profiles. It forwards versioned requests to the configured Python binary as `python -m us_equity_research.cli --workspace <workspace> ...`, returns bounded results, and does not read SQLite, provider APIs, or artifact directories directly.

For a local syntax and argument-contract check before touching a remote host:

```bash
bash ../scripts/deploy_us_remote.sh --self-test
```

For remote use, prefer an SSH tunnel to a loopback-bound Web profile rather than a public listener. Example flow:

1. SSH to the remote Mac and start `dsh --profile web --host 127.0.0.1 --port <port>`.
2. Forward the port locally with SSH tunneling.
3. Keep DSH profile installation explicit and manual; `scripts/deploy_us_remote.sh` intentionally does not mutate DSH profiles.

The deployment script never deletes remote runtime or research assets. Existing prompts and templates are preserved; repository defaults are copied only when the corresponding remote file is missing.

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

## 2026-09-12 美股工程升级

当前引擎 `us-research-plan-v1.0` 新增能力诊断、条件式研究计划卡、常规收盘时效门槛和显式交易日历契约；修复未来来源穿透计算和部分叙述的问题。运行标识区分算法版本，旧产物按冻结旧实现核验。

完整报告可用 `artifact-read --complete --request-json REQUEST.json` 获取；DSH 按 `next_offset` 连续读取。详见 [研究升级契约](docs/RESEARCH_UPGRADE.md)。没有接入新的行情供应商、预期库或自主语义模型；缺少对应输入时仍显示 UNKNOWN。

## 富途只读行情待验通道（2026-09-13）

新增 `collect-futu-quotes` 和 `futu-observation-report`，先规范化采集、再按人工核对日历生成行情观察附页。历史发布时间不足时保留 UNKNOWN，不自动进入正式候选证据。SEC 行情输入支持独立 `calendar_bundle`。详见 [接入说明](docs/FUTU_QUOTE_CONNECTION.md)。

## 可复算的本地美股日报

已新增 MCP 行情采集、行业 ETF 比较、SEC 刷新和明确降级的每日研究报告。功能缺口、验证证据与运行入口见 [日报能力审计](docs/DAILY_REVIEW.md)。行情接入不自动提升正式候选；缺时间、财务或预期数据继续保留 UNKNOWN。

日历自动刷新、TTM多来源计算、研究账本及剩余外部数据验收要求见 [缺口优化结果](docs/GAP_OPTIMIZATION.md)。

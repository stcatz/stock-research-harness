# DeepSeek Harness US Equity Research Workflow

## 1. Correct role

Do not ask the model to predict which stock will rise next. The system is for research triage:

> Freeze a point-in-time snapshot, validate evidence and time boundaries, compute deterministic facts and ratios, organize Evidence -> Claim -> Thesis, and leave the final judgment to a human reviewer.

The Python engine writes the canonical report. DeepSeek Harness is the thin client used to trigger the workflow, inspect bounded sections, and support operator review.

## 2. Fixed workflow matrix

There are exactly three workflows:

| workflow | required field | forbidden field | selection rule |
|---|---|---|---|
| `daily_report` | none | `subject`, `symbol` | evaluate every candidate in the selected snapshot and apply `top_n` |
| `theme_research` | `subject` | `symbol` | keep only themes whose ID or name contains `subject`, case-insensitive |
| `stock_research` | `symbol` | `subject` | keep only the exact normalized US symbol |

All workflows require:

- `schema_version="0.1"`
- `market="US"`
- timezone-aware `decision_at`
- `snapshot.selector` in `demo`, `latest`, or `id`

## 3. Evidence -> Claim -> Thesis

The chain is fixed and auditable:

1. Evidence: official issuer, official macro, regulatory, or structured market records with complete time semantics.
2. Claim: bounded statements about a theme or issuer, each citing evidence refs and fact refs.
3. Thesis: the final research state with bull case, bear case, risk verdict, invalidation conditions, next catalyst, and explicit data gaps.

If evidence is missing or late, the claim stays `UNKNOWN` or the candidate is pushed to `exclude` / `continue_research`. The model must not bridge missing facts with style or confidence.

## 4. Daily operating loop

### A. Freeze the snapshot

Use either the synthetic fixture or a validated local snapshot:

```text
<workspace>/data/normalized/us/<snapshot_id>/snapshot.json
```

The engine gates every fact and evidence item by `decision_at`. `published_at <= available_at <= retrieved_at` must hold; `as_of` cannot exceed availability; `effective_at` can describe a future event but cannot bypass PIT safety.

### B. Validate source hierarchy

Required source priorities:

- SEC EDGAR and issuer IR for issuer-specific facts
- BLS/BEA and other official releases for macro context
- BYOK licensed market provider for prices, breadth, and rates

Do not route real ingestion through FRED API, scraped quote pages, or transcript sites without a separate approval path.

### C. Build claims and calculations

For each surviving candidate:

- keep official and structured-market evidence separate
- calculate ratios only from usable facts available by `decision_at`
- preserve formulae and `input_fact_ids`
- mark incomplete or invalid ratios as `UNKNOWN` / `NOT_MEANINGFUL`

### D. Apply research gates

Candidates need official primary evidence, structured market support, a transmission chain, bull/bear/risk viewpoints, invalidation conditions, and a future catalyst to reach `observe`. Hard risks, fading stages, time leakage, or material data gaps can force `exclude` or `continue_research`.

### E. Write immutable outputs

The engine writes:

- `research_packet.json`
- `report.md`
- `manifest.json`

The packet is the structured record. The report is the reader-facing rendering. The manifest pins stable hashes and file integrity. DSH never rewrites them.

## 5. Human review expectations

The human reviewer still owns:

- confirming the cited filing or issuer material
- checking whether the market has already priced the thesis
- deciding whether to continue research outside this tool
- deciding whether anything should ever reach a trading workflow outside this repository

This workflow is research only. It never grants execution authority.

## 6. Fixture and remote use

- `uv run us-equity-research demo` is synthetic by design. Treat every fixture result as installation evidence, not market research.
- Headless DSH is appropriate for repeatable CLI-style runs.
- Web DSH should stay on `127.0.0.1` and be accessed through SSH tunneling when used on a remote Mac.
- `scripts/deploy_us_remote.sh` syncs code and runs remote verification, but intentionally does not install or edit DSH profiles automatically.

## 7. Output discipline

- Facts, calculations, and inference stay in separate sections.
- Every missing item remains explicit.
- Use only `exclude`, `continue_research`, or `observe`.
- Never write buy/sell/target-price/order language.
- End every report with: `本报告仅用于研究，不构成投资建议，所有事实与交易判断须由用户独立复核。`

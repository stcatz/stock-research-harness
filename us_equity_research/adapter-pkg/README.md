# US Equity Research DSH Adapter

DeepSeek Harness `0.1.0-rc.6` compatible thin bundle for the offline US-equity research engine.

It exposes exactly two model-facing tools:

- `us_research_run` runs `daily_report`, `theme_research`, or `stock_research`.
- `us_artifact_read` reads a bounded predefined artifact section by opaque ID.

The bundle is permanently bound to `market=US` and `schema_version=0.1`. It never connects to a data provider, SQLite, a broker, or the artifact filesystem itself. Instead it invokes the canonical Python writer with explicit argv and one JSON object on stdin:

```text
.venv/bin/python -m us_equity_research.cli \
  --workspace <workspace> run|artifact-read --request-json -
```

## Public tool contract

`us_research_run` accepts:

- `workflow`: `daily_report | theme_research | stock_research`
- `decision_at`: timezone-aware ISO-8601 timestamp
- `snapshot`: `{ selector: demo | latest | id, snapshot_id?: string }`
- `subject`: required only for `theme_research`
- `symbol`: required only for `stock_research`; uppercase `[A-Z][A-Z0-9.-]{0,14}`
- `top_n`: optional integer from 1 through 20

The snapshot field is named `snapshot_id`; the legacy `id` alias is intentionally rejected.

`us_artifact_read` accepts an `artifact_id`, an optional `summary | report | manifest | packet` section, and an optional `max_chars` from 500 through 20,000.

The adapter keeps domain outcomes such as `partial`, `UNKNOWN`, and explicit data gaps as successful results. Contract and not-found errors remain structured. Process failures, timeouts, integrity failures, invalid handshakes, or non-canonical output are infrastructure errors.

Model-visible structured run results preserve up to 20 focus candidates, matching the public `top_n` maximum. Warnings and gaps are bounded previews of up to five items each. Use `us_artifact_read` for the complete report or longer narrative instead of expecting it in the run result.

## Runtime boundaries

The child receives a minimal system-variable allowlist and no model/data API credentials. Cancellation is forwarded from `exec.signal`; on supported macOS/Linux systems the complete child process group is terminated. CLI output, rendered content, lists, errors, paths, and secret-like values are bounded or redacted before reaching the model.

Optional deployment overrides:

```bash
export US_EQUITY_RESEARCH_ROOT=/absolute/path/to/us_equity_research
export US_EQUITY_RESEARCH_PYTHON_BIN=/absolute/path/to/python
export STOCK_RESEARCH_WORKSPACE=/absolute/path/to/stock
```

These are used only to resolve the bridge command. They are not copied into the child environment.

## Build and test

Node 22 or newer on macOS or Linux is required. The adapter rejects other operating systems before spawning Python because cancellation and timeout safety depend on Unix process-group semantics.

```bash
npm ci
npm test
./node_modules/.bin/tsc --noEmit
```

The zero-dependency build step uses Node's built-in TypeScript transform to generate `dist/index.js`.

Install the directory into each desired DSH profile using an absolute path:

```bash
dsh plugin --profile web add /absolute/path/to/us_equity_research/adapter-pkg
dsh plugin --profile headless add /absolute/path/to/us_equity_research/adapter-pkg
dsh --profile web --dump-config
dsh --profile headless --dump-config
```

Before changing a profile, back up its package manifest and lockfile. Remove the bundle with:

```bash
dsh plugin --profile web remove @user/dsh-us-equity-research
dsh plugin --profile headless remove @user/dsh-us-equity-research
```

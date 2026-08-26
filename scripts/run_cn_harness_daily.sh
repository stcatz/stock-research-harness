#!/usr/bin/env bash

set -eu

umask 077

usage() {
  cat <<'EOF'
Usage:
  run_cn_harness_daily.sh --root PATH --seed-json PATH [options]

Required:
  --root PATH            Repository/workspace root
  --seed-json PATH       Operator-maintained real CN research seed

Options:
  --provider NAME        baostock or hithink (default: hithink)
  --raw-store-root PATH  Private HiThink raw-response store
  --snapshot-id ID       Immutable snapshot ID (default: generated locally)
  --decision-at ISO      Timezone-aware cut-off after collection
  --top-n N              Focus candidate count, 1-20 (default: 9)
  --dsh-bin PATH         DeepSeek Harness executable (default: dsh from PATH)
  --profile NAME         DSH profile (default: headless)
  -h, --help             Show this help

This is an explicitly networked two-model Harness. It first runs the canonical collector and
offline engine, then performs a thesis-blind bear review and a separate final web investigation.
It never edits canonical artifacts or invokes any broker/order capability.
EOF
}

die() {
  message=$1
  code=${2:-2}
  printf 'run_cn_harness_daily: %s\n' "$message" >&2
  exit "$code"
}

ROOT_ARGUMENT=
SEED_ARGUMENT=
PROVIDER=hithink
RAW_STORE_ARGUMENT=
SNAPSHOT_ID=
DECISION_AT=
TOP_N=9
DSH_ARGUMENT=
PROFILE=headless

while [ "$#" -gt 0 ]; do
  case "$1" in
    --root) [ "$#" -ge 2 ] || die "--root requires a value"; ROOT_ARGUMENT=$2; shift 2 ;;
    --seed-json) [ "$#" -ge 2 ] || die "--seed-json requires a value"; SEED_ARGUMENT=$2; shift 2 ;;
    --provider) [ "$#" -ge 2 ] || die "--provider requires a value"; PROVIDER=$2; shift 2 ;;
    --raw-store-root) [ "$#" -ge 2 ] || die "--raw-store-root requires a value"; RAW_STORE_ARGUMENT=$2; shift 2 ;;
    --snapshot-id) [ "$#" -ge 2 ] || die "--snapshot-id requires a value"; SNAPSHOT_ID=$2; shift 2 ;;
    --decision-at) [ "$#" -ge 2 ] || die "--decision-at requires a value"; DECISION_AT=$2; shift 2 ;;
    --top-n) [ "$#" -ge 2 ] || die "--top-n requires a value"; TOP_N=$2; shift 2 ;;
    --dsh-bin) [ "$#" -ge 2 ] || die "--dsh-bin requires a value"; DSH_ARGUMENT=$2; shift 2 ;;
    --profile) [ "$#" -ge 2 ] || die "--profile requires a value"; PROFILE=$2; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

[ -n "$ROOT_ARGUMENT" ] || die "--root is required"
[ -n "$SEED_ARGUMENT" ] || die "--seed-json is required"
[ -d "$ROOT_ARGUMENT" ] || die "repository root does not exist"
[ -f "$SEED_ARGUMENT" ] || die "seed JSON does not exist"
case "$PROVIDER" in baostock|hithink) ;; *) die "--provider must be baostock or hithink" ;; esac

ROOT=$(CDPATH= cd -- "$ROOT_ARGUMENT" && pwd -P)
SEED_DIR=$(CDPATH= cd -- "$(dirname -- "$SEED_ARGUMENT")" && pwd -P)
SEED_JSON="$SEED_DIR/$(basename -- "$SEED_ARGUMENT")"
PYTHON="$ROOT/a_share_research/.venv/bin/python"
CANONICAL_RUNNER="$ROOT/scripts/run_cn_daily.sh"
BEAR_TEMPLATE="$ROOT/a_share_research/prompts/harness-independent-bear.md"
FINAL_TEMPLATE="$ROOT/a_share_research/prompts/harness-daily-opportunity.md"
[ -x "$PYTHON" ] || die "A-share project Python is missing; run uv sync first"
[ -x "$CANONICAL_RUNNER" ] || die "scripts/run_cn_daily.sh is missing or not executable"
[ -f "$BEAR_TEMPLATE" ] || die "independent bear prompt is missing"
[ -f "$FINAL_TEMPLATE" ] || die "daily Harness prompt is missing"

if [ -n "$DSH_ARGUMENT" ]; then
  DSH_BIN=$DSH_ARGUMENT
else
  DSH_BIN=$(command -v dsh 2>/dev/null || true)
fi
[ -n "$DSH_BIN" ] && [ -x "$DSH_BIN" ] || die "DeepSeek Harness executable was not found"
case "$PROFILE" in ''|*[!A-Za-z0-9._-]*) die "--profile contains unsupported characters" ;; esac
case "$TOP_N" in ''|*[!0-9]*) die "--top-n must be an integer from 1 to 20" ;; esac
[ "$TOP_N" -ge 1 ] && [ "$TOP_N" -le 20 ] || die "--top-n must be an integer from 1 to 20"

if [ -z "$SNAPSHOT_ID" ]; then
  SNAPSHOT_ID="cn-$(date '+%Y%m%d-%H%M%S')-harness-$$"
fi
case "$SNAPSHOT_ID" in ''|*[!A-Za-z0-9._-]*) die "--snapshot-id contains unsupported characters" ;; esac
[ "${#SNAPSHOT_ID}" -le 128 ] || die "--snapshot-id is longer than 128 characters"

LOCK_PARENT="$ROOT/.runtime/locks"
LOCK_DIR="$LOCK_PARENT/cn-harness-daily.lock"
TMP_PARENT="$ROOT/.runtime/tmp"
OUTPUT_PARENT="$ROOT/.runtime/harness/cn"
mkdir -p "$LOCK_PARENT" "$TMP_PARENT" "$OUTPUT_PARENT"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  die "another CN Harness run holds the lock" 75
fi
printf '%s\n' "$$" >"$LOCK_DIR/pid"

TMP_DIR=
STAGING_OUTPUT=
cleanup() {
  if [ -n "$TMP_DIR" ] && [ -d "$TMP_DIR" ]; then
    rm -f "$TMP_DIR"/*
    rmdir "$TMP_DIR" 2>/dev/null || true
  fi
  if [ -n "$STAGING_OUTPUT" ] && [ -d "$STAGING_OUTPUT" ]; then
    rm -f "$STAGING_OUTPUT"/*
    rmdir "$STAGING_OUTPUT" 2>/dev/null || true
  fi
  rm -f "$LOCK_DIR/pid"
  rmdir "$LOCK_DIR" 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
TMP_DIR=$(mktemp -d "$TMP_PARENT/cn-harness.XXXXXX")

PREVIOUS_SNAPSHOT_ID=$(
  "$PYTHON" -c 'from datetime import datetime
import json
from pathlib import Path
import sys

root = Path(sys.argv[1]) / "data" / "normalized"
candidates = []
for path in root.glob("*/snapshot.json"):
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("market") != "CN":
            continue
        retrieved = datetime.fromisoformat(payload["retrieved_at"].replace("Z", "+00:00"))
        candidates.append((retrieved, payload["snapshot_id"]))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, OSError):
        continue
print(max(candidates)[1] if candidates else "")' "$ROOT"
)

CANONICAL_ARGUMENTS=(
  --root "$ROOT"
  --seed-json "$SEED_JSON"
  --provider "$PROVIDER"
  --snapshot-id "$SNAPSHOT_ID"
  --top-n "$TOP_N"
)
if [ -n "$RAW_STORE_ARGUMENT" ]; then
  CANONICAL_ARGUMENTS+=(--raw-store-root "$RAW_STORE_ARGUMENT")
fi
if [ -n "$DECISION_AT" ]; then
  CANONICAL_ARGUMENTS+=(--decision-at "$DECISION_AT")
fi

printf 'run_cn_harness_daily: collecting, freezing and running canonical research\n' >&2
if ! "$CANONICAL_RUNNER" "${CANONICAL_ARGUMENTS[@]}" >"$TMP_DIR/canonical.json"; then
  die "canonical daily research failed; online review was not started"
fi

read_json_string() {
  "$PYTHON" -c 'import json, pathlib, sys
payload = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
value = payload.get(sys.argv[2])
if not isinstance(value, str) or not value:
    raise SystemExit(3)
print(value)' "$1" "$2"
}

ARTIFACT_ID=$(read_json_string "$TMP_DIR/canonical.json" artifact_id) || die "canonical result has no artifact_id"
if [ -z "$DECISION_AT" ]; then
  DECISION_AT=$(
    "$PYTHON" -c 'import json
from pathlib import Path
import sys

artifact = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
workspace = Path(sys.argv[2])
database = workspace / "data" / "stock_research.sqlite3"
import sqlite3
with sqlite3.connect(database) as connection:
    row = connection.execute(
        "SELECT decision_at FROM runs WHERE artifact_id = ?", (artifact["artifact_id"],)
    ).fetchone()
if row is None:
    raise SystemExit(3)
print(row[0])' "$TMP_DIR/canonical.json" "$ROOT"
  ) || die "could not resolve canonical decision_at"
fi

DRIFT_STATUS=not_available
if [ -n "$PREVIOUS_SNAPSHOT_ID" ] && [ "$PREVIOUS_SNAPSHOT_ID" != "$SNAPSHOT_ID" ]; then
  printf 'run_cn_harness_daily: auditing cross-snapshot provider drift\n' >&2
  if printf '%s\n' \
      "{\"schema_version\":\"0.1\",\"market\":\"CN\",\"before_snapshot_id\":\"$PREVIOUS_SNAPSHOT_ID\",\"after_snapshot_id\":\"$SNAPSHOT_ID\"}" \
      | "$PYTHON" -m a_share_research.cli --workspace "$ROOT" audit-drift --request-json - \
          >"$TMP_DIR/drift.json"; then
    DRIFT_STATUS=$(read_json_string "$TMP_DIR/drift.json" status) || die "drift receipt has no status"
  else
    die "provider drift audit failed; online review was not started"
  fi
else
  printf '%s\n' '{"status":"not_available","reason":"no prior CN snapshot"}' >"$TMP_DIR/drift.json"
fi

render_prompt() {
  "$PYTHON" -c 'from pathlib import Path
import sys

text = Path(sys.argv[1]).read_text(encoding="utf-8")
for index in range(2, len(sys.argv), 2):
    text = text.replace(sys.argv[index], sys.argv[index + 1])
print(text, end="")' "$@"
}

render_prompt "$BEAR_TEMPLATE" \
  __ARTIFACT_ID__ "$ARTIFACT_ID" \
  __DECISION_AT__ "$DECISION_AT" \
  __DRIFT_STATUS__ "$DRIFT_STATUS" >"$TMP_DIR/bear.prompt"

printf 'run_cn_harness_daily: running thesis-blind independent bear investigation\n' >&2
BEAR_PROMPT=$("$PYTHON" -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).read_text(encoding="utf-8"), end="")' "$TMP_DIR/bear.prompt")
if ! env -u HITHINK_FINANCE_API_KEY "$DSH_BIN" --profile "$PROFILE" "$BEAR_PROMPT" \
    >"$TMP_DIR/bear.out"; then
  die "independent bear Harness call failed"
fi
if ! "$PYTHON" -c 'import json
from pathlib import Path
import sys

try:
    payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError):
    raise SystemExit(3)
if payload.get("artifact_id") != sys.argv[2]:
    raise SystemExit(3)
if payload.get("review_mode") != "independent_bear":
    raise SystemExit(3)
if not isinstance(payload.get("candidates"), list):
    raise SystemExit(3)' "$TMP_DIR/bear.out" "$ARTIFACT_ID"; then
  die "independent bear output did not satisfy the JSON review contract"
fi

BEAR_JSON_STRING=$("$PYTHON" -c 'import json
from pathlib import Path
import sys
print(json.dumps(Path(sys.argv[1]).read_text(encoding="utf-8"), ensure_ascii=False))' "$TMP_DIR/bear.out")
render_prompt "$FINAL_TEMPLATE" \
  __ARTIFACT_ID__ "$ARTIFACT_ID" \
  __SNAPSHOT_ID__ "$SNAPSHOT_ID" \
  __DECISION_AT__ "$DECISION_AT" \
  __DRIFT_STATUS__ "$DRIFT_STATUS" \
  __BEAR_REVIEW_JSON_STRING__ "$BEAR_JSON_STRING" >"$TMP_DIR/final.prompt"

printf 'run_cn_harness_daily: running final online judge and opportunity discovery\n' >&2
FINAL_PROMPT=$("$PYTHON" -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).read_text(encoding="utf-8"), end="")' "$TMP_DIR/final.prompt")
if ! env -u HITHINK_FINANCE_API_KEY "$DSH_BIN" --profile "$PROFILE" "$FINAL_PROMPT" \
    >"$TMP_DIR/memo.md"; then
  die "final Harness call failed"
fi
if ! "$PYTHON" -c 'from pathlib import Path
import sys

disclaimer = "本报告仅用于研究，不构成投资建议，所有事实与交易判断须由用户独立复核。"
prohibited = ("建议买入", "建议卖出", "目标价", "止损价", "仓位建议", "保证上涨")
try:
    text = Path(sys.argv[1]).read_text(encoding="utf-8")
except OSError:
    raise SystemExit(3)
lines = [line.strip() for line in text.splitlines() if line.strip()]
if not lines or lines[-1] != disclaimer:
    raise SystemExit(3)
if any(phrase in text for phrase in prohibited):
    raise SystemExit(3)
if sys.argv[2] not in text:
    raise SystemExit(3)' "$TMP_DIR/memo.md" "$ARTIFACT_ID"; then
  die "final Harness memo failed the research-only output policy"
fi

OUTPUT_DIR="$OUTPUT_PARENT/$SNAPSHOT_ID"
[ ! -e "$OUTPUT_DIR" ] || die "immutable Harness output already exists for snapshot_id"
STAGING_OUTPUT="$OUTPUT_PARENT/.$SNAPSHOT_ID.tmp-$$"
[ ! -e "$STAGING_OUTPUT" ] || die "temporary Harness output already exists"
mkdir "$STAGING_OUTPUT"
mv "$TMP_DIR/canonical.json" "$STAGING_OUTPUT/canonical.json"
mv "$TMP_DIR/drift.json" "$STAGING_OUTPUT/drift.json"
mv "$TMP_DIR/bear.out" "$STAGING_OUTPUT/independent-bear.json"
mv "$TMP_DIR/memo.md" "$STAGING_OUTPUT/opportunity-memo.md"

CODE_REVISION=unknown
CODE_DIRTY=unknown
if git -C "$ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  CODE_REVISION=$(git -C "$ROOT" rev-parse HEAD 2>/dev/null || printf 'unknown')
  if [ -z "$(git -C "$ROOT" status --porcelain --untracked-files=normal 2>/dev/null)" ]; then
    CODE_DIRTY=false
  else
    CODE_DIRTY=true
  fi
fi

"$PYTHON" -c 'from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import sys

root = Path(sys.argv[1])
output = Path(sys.argv[2])
bear_template = Path(sys.argv[9])
final_template = Path(sys.argv[10])

def digest(path):
    return sha256(path.read_bytes()).hexdigest()

stable = {
    "schema_version": "0.1",
    "market": "CN",
    "harness_contract_version": "0.1",
    "snapshot_id": sys.argv[3],
    "artifact_id": sys.argv[4],
    "decision_at": sys.argv[5],
    "drift_status": sys.argv[6],
    "dsh_profile": sys.argv[7],
    "code_revision": sys.argv[8],
    "code_worktree_dirty": {"true": True, "false": False}.get(sys.argv[11]),
    "network_boundary": {
        "collector_networked": True,
        "canonical_engine_networked": False,
        "review_calls_networked": True,
        "provider_credentials_forwarded_to_models": False,
        "canonical_artifact_rewritten": False,
    },
    "model_calls": [
        {"role": "independent_bear", "input_scope": "facts_only", "bull_thesis_visible": False},
        {"role": "final_judge", "input_scope": "paginated_report_and_available_outcomes"},
    ],
    "prompt_templates": {
        bear_template.relative_to(root).as_posix(): digest(bear_template),
        final_template.relative_to(root).as_posix(): digest(final_template),
    },
    "files": {
        name: digest(output / name)
        for name in (
            "canonical.json",
            "drift.json",
            "independent-bear.json",
            "opportunity-memo.md",
        )
    },
}
manifest = {
    **stable,
    "completed_at": datetime.now().astimezone().isoformat(),
    "manifest_hash": sha256(
        json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest(),
}
(output / "harness-manifest.json").write_text(
    json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)' \
  "$ROOT" "$STAGING_OUTPUT" "$SNAPSHOT_ID" "$ARTIFACT_ID" "$DECISION_AT" "$DRIFT_STATUS" \
  "$PROFILE" "$CODE_REVISION" "$BEAR_TEMPLATE" "$FINAL_TEMPLATE" "$CODE_DIRTY"
chmod 600 "$STAGING_OUTPUT"/*
"$PYTHON" -c 'import os
from pathlib import Path
import sys

staging = Path(sys.argv[1])
destination = Path(sys.argv[2])
if os.path.lexists(destination):
    raise SystemExit("immutable Harness destination appeared during publication")
os.rename(staging, destination)
descriptor = os.open(destination.parent, os.O_RDONLY)
try:
    os.fsync(descriptor)
finally:
    os.close(descriptor)' "$STAGING_OUTPUT" "$OUTPUT_DIR"
STAGING_OUTPUT=

"$PYTHON" -c 'import json
from pathlib import Path
import sys

root = Path(sys.argv[1])
output = Path(sys.argv[2])
print(json.dumps({
    "schema_version": "0.1",
    "market": "CN",
    "status": "completed",
    "networked_harness": True,
    "snapshot_id": sys.argv[3],
    "artifact_id": sys.argv[4],
    "decision_at": sys.argv[5],
    "drift_status": sys.argv[6],
    "output_dir": output.relative_to(root).as_posix(),
    "memo_path": (output / "opportunity-memo.md").relative_to(root).as_posix(),
    "harness_manifest_path": (output / "harness-manifest.json").relative_to(root).as_posix(),
}, ensure_ascii=False, indent=2))' \
  "$ROOT" "$OUTPUT_DIR" "$SNAPSHOT_ID" "$ARTIFACT_ID" "$DECISION_AT" "$DRIFT_STATUS"

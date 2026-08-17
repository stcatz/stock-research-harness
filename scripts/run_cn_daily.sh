#!/usr/bin/env bash

set -eu

umask 077

usage() {
  cat <<'EOF'
Usage:
  run_cn_daily.sh --root PATH --seed-json PATH [options]

Required:
  --root PATH            Repository/workspace root containing a_share_research/
  --seed-json PATH       Real, operator-maintained CN research seed JSON

Options:
  --snapshot-id ID       Immutable snapshot ID (default: generated from local time)
  --decision-at ISO      Override the research cut-off (default: time after collection)
  --top-n N              Number of focus candidates, 1-20 (default: 9)
  -h, --help             Show this help

This wrapper never invokes the demo fixture and does not require DeepSeek Harness.
EOF
}

die() {
  message=$1
  code=${2:-2}
  printf 'run_cn_daily: %s\n' "$message" >&2
  exit "$code"
}

ROOT_ARGUMENT=
SEED_ARGUMENT=
SNAPSHOT_ID=
DECISION_AT=
TOP_N=9

while [ "$#" -gt 0 ]; do
  case "$1" in
    --root)
      [ "$#" -ge 2 ] || die "--root requires a value"
      ROOT_ARGUMENT=$2
      shift 2
      ;;
    --seed-json)
      [ "$#" -ge 2 ] || die "--seed-json requires a value"
      SEED_ARGUMENT=$2
      shift 2
      ;;
    --snapshot-id)
      [ "$#" -ge 2 ] || die "--snapshot-id requires a value"
      SNAPSHOT_ID=$2
      shift 2
      ;;
    --decision-at)
      [ "$#" -ge 2 ] || die "--decision-at requires a value"
      DECISION_AT=$2
      shift 2
      ;;
    --top-n)
      [ "$#" -ge 2 ] || die "--top-n requires a value"
      TOP_N=$2
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    --)
      shift
      [ "$#" -eq 0 ] || die "unexpected positional arguments"
      ;;
    *)
      die "unknown argument: $1"
      ;;
  esac
done

[ -n "$ROOT_ARGUMENT" ] || die "--root is required"
[ -n "$SEED_ARGUMENT" ] || die "--seed-json is required"
[ -d "$ROOT_ARGUMENT" ] || die "repository root does not exist"
[ -f "$SEED_ARGUMENT" ] || die "seed JSON does not exist"

ROOT=$(CDPATH= cd -- "$ROOT_ARGUMENT" && pwd -P)
SEED_DIR=$(CDPATH= cd -- "$(dirname -- "$SEED_ARGUMENT")" && pwd -P)
SEED_JSON="$SEED_DIR/$(basename -- "$SEED_ARGUMENT")"
case "$(basename -- "$SEED_JSON")" in
  example.json|*.example.json) die "refusing to schedule an example seed as real research" ;;
esac
if LC_ALL=C grep -Eiq \
    '"example_notice"[[:space:]]*:|example[.]invalid|"EXAMPLE_ONLY"|SYNTHETIC FORMAT EXAMPLE' \
    "$SEED_JSON"; then
  die "refusing to schedule a seed that still contains synthetic example markers"
else
  GREP_STATUS=$?
  [ "$GREP_STATUS" -eq 1 ] || die "could not inspect the research seed"
fi
PYTHON="$ROOT/a_share_research/.venv/bin/python"
[ -x "$PYTHON" ] || die "A-share project Python is missing; run uv sync first"

case "$TOP_N" in
  ''|*[!0-9]*) die "--top-n must be an integer from 1 to 20" ;;
esac
[ "$TOP_N" -ge 1 ] && [ "$TOP_N" -le 20 ] || die "--top-n must be an integer from 1 to 20"

if [ -z "$SNAPSHOT_ID" ]; then
  SNAPSHOT_ID="cn-$(date '+%Y%m%d-%H%M%S')-$$"
fi
case "$SNAPSHOT_ID" in
  ''|*[!A-Za-z0-9._-]*) die "--snapshot-id contains unsupported characters" ;;
esac
[ "${#SNAPSHOT_ID}" -le 128 ] || die "--snapshot-id is longer than 128 characters"

validate_timestamp() {
  timestamp=$1
  option=$2
  case "$timestamp" in
    ''|*[!0-9TtZz:+.-]*) die "$option must be a timezone-aware ISO timestamp" ;;
  esac
}

if [ -n "$DECISION_AT" ]; then
  validate_timestamp "$DECISION_AT" "--decision-at"
fi

LOCK_PARENT="$ROOT/.runtime/locks"
LOCK_DIR="$LOCK_PARENT/cn-daily.lock"
TMP_PARENT="$ROOT/.runtime/tmp"
mkdir -p "$LOCK_PARENT" "$TMP_PARENT"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  die "another CN daily run holds the lock" 75
fi
printf '%s\n' "$$" >"$LOCK_DIR/pid"

TMP_DIR=
cleanup() {
  if [ -n "$TMP_DIR" ]; then
    rm -f \
      "$TMP_DIR/collect.json" \
      "$TMP_DIR/collect.stderr" \
      "$TMP_DIR/run.json" \
      "$TMP_DIR/run.stderr" \
      "$TMP_DIR/report.json" \
      "$TMP_DIR/report.stderr"
    rmdir "$TMP_DIR" 2>/dev/null || true
  fi
  rm -f "$LOCK_DIR/pid"
  rmdir "$LOCK_DIR" 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
TMP_DIR=$(mktemp -d "$TMP_PARENT/cn-daily.XXXXXX")

extract_json_field() {
  json_path=$1
  field=$2
  "$PYTHON" -c \
    'import json, pathlib, sys
payload = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
value = payload.get(sys.argv[2])
if not isinstance(value, str) or not value:
    raise SystemExit(3)
print(value)' \
    "$json_path" "$field"
}

canonical_collection_retrieved_at() {
  json_path=$1
  "$PYTHON" -c \
    'from datetime import datetime
import json
import pathlib
import sys

# validate_collection_metadata
payload = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
if payload.get("created") is not True:
    raise SystemExit(4)
value = payload.get("retrieved_at")
if not isinstance(value, str) or not value:
    raise SystemExit(4)
try:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
except ValueError:
    raise SystemExit(4)
if parsed.utcoffset() is None:
    raise SystemExit(4)
print(parsed.isoformat())' \
    "$json_path"
}

current_decision_time() {
  "$PYTHON" -c \
    'from datetime import datetime

# current_decision_time
print(datetime.now().astimezone().isoformat(timespec="microseconds"))'
}

decision_is_not_before_collection() {
  decision_at=$1
  collected_at=$2
  "$PYTHON" -c \
    'from datetime import datetime
import sys

# validate_decision_order
try:
    decision = datetime.fromisoformat(sys.argv[1].replace("Z", "+00:00"))
    collected = datetime.fromisoformat(sys.argv[2].replace("Z", "+00:00"))
except ValueError:
    raise SystemExit(4)
if decision.utcoffset() is None or collected.utcoffset() is None:
    raise SystemExit(4)
if decision < collected:
    raise SystemExit(4)' \
    "$decision_at" "$collected_at"
}

printf 'run_cn_daily: collecting a real normalized snapshot\n' >&2
COLLECT_ARGUMENTS=(
  --workspace "$ROOT"
  collect-snapshot
  --seed-json "$SEED_JSON"
  --snapshot-id "$SNAPSHOT_ID"
)
if ! "$PYTHON" -m a_share_research.cli "${COLLECT_ARGUMENTS[@]}" \
    >"$TMP_DIR/collect.json" 2>"$TMP_DIR/collect.stderr"; then
  die "snapshot collection failed; research was not started"
fi

PUBLISHED_SNAPSHOT_ID=$(extract_json_field "$TMP_DIR/collect.json" snapshot_id) || \
  die "collector returned invalid JSON; research was not started"
[ "$PUBLISHED_SNAPSHOT_ID" = "$SNAPSHOT_ID" ] || \
  die "collector returned an unexpected snapshot ID; research was not started"
PUBLISHED_SNAPSHOT_HASH=$(extract_json_field "$TMP_DIR/collect.json" snapshot_hash) || \
  die "collector returned no snapshot hash; research was not started"
case "$PUBLISHED_SNAPSHOT_HASH" in
  *[!0-9a-fA-F]*) die "collector returned an invalid snapshot hash; research was not started" ;;
esac
[ "${#PUBLISHED_SNAPSHOT_HASH}" -eq 64 ] || \
  die "collector returned an invalid snapshot hash; research was not started"
COLLECTED_RETRIEVED_AT=$(canonical_collection_retrieved_at "$TMP_DIR/collect.json") || \
  die "collector did not create a fresh snapshot with canonical retrieval time"

if [ -z "$DECISION_AT" ]; then
  DECISION_AT=$(current_decision_time) || \
    die "could not determine a timezone-aware decision time"
fi
validate_timestamp "$DECISION_AT" "--decision-at"
if ! decision_is_not_before_collection "$DECISION_AT" "$COLLECTED_RETRIEVED_AT"; then
  die "decision_at cannot be earlier than the collected snapshot retrieval time"
fi

printf 'run_cn_daily: running daily research against the published snapshot ID\n' >&2
if ! printf '%s\n' \
    "{\"schema_version\":\"0.1\",\"market\":\"CN\",\"workflow\":\"daily_report\",\"decision_at\":\"$DECISION_AT\",\"snapshot\":{\"selector\":\"id\",\"snapshot_id\":\"$SNAPSHOT_ID\"},\"top_n\":$TOP_N}" \
    | "$PYTHON" -m a_share_research.cli --workspace "$ROOT" run --request-json - \
        >"$TMP_DIR/run.json" 2>"$TMP_DIR/run.stderr"; then
  die "daily research failed; no report was published"
fi

RUN_SNAPSHOT_ID=$(extract_json_field "$TMP_DIR/run.json" snapshot_id) || \
  die "research returned no snapshot ID"
[ "$RUN_SNAPSHOT_ID" = "$SNAPSHOT_ID" ] || \
  die "research used an unexpected snapshot ID"
ARTIFACT_ID=$(extract_json_field "$TMP_DIR/run.json" artifact_id) || \
  die "research returned no artifact ID"
case "$ARTIFACT_ID" in
  ''|*[!A-Za-z0-9._-]*) die "research returned an invalid artifact ID" ;;
esac

printf 'run_cn_daily: reading the complete report artifact\n' >&2
if ! printf '%s\n' \
    "{\"artifact_id\":\"$ARTIFACT_ID\",\"section\":\"report\",\"max_chars\":20000}" \
    | "$PYTHON" -m a_share_research.cli --workspace "$ROOT" artifact-read --request-json - \
        >"$TMP_DIR/report.json" 2>"$TMP_DIR/report.stderr"; then
  die "artifact read failed; no report was emitted"
fi

cat "$TMP_DIR/report.json"

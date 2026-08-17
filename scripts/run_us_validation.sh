#!/usr/bin/env bash

set -eu

umask 077

usage() {
  cat <<'EOF'
Usage:
  run_us_validation.sh --root PATH --seed-json PATH --snapshot-id ID [options]

Required:
  --root PATH            Repository/workspace root containing us_equity_research/
  --seed-json PATH       Real US SEC research seed JSON
  --snapshot-id ID       Immutable ID used for collection and validation
  SEC_USER_AGENT         SEC-compliant identity supplied through the environment

Options:
  --market-json PATH     Optional licensed/approved structured market-data JSON
  --retrieved-at ISO     Override the SEC collector retrieval time
  --decision-at ISO      Override the research cut-off (default: time after collection)
  --top-n N              Number of focus candidates, 1-20 (default: 5)
  -h, --help             Show this help

The wrapper performs collection, doctor, research, and full artifact reading. It has
no broker integration, never submits orders, and never falls back to demo data.
EOF
}

die() {
  message=$1
  code=${2:-2}
  printf 'run_us_validation: %s\n' "$message" >&2
  exit "$code"
}

ROOT_ARGUMENT=
SEED_ARGUMENT=
MARKET_ARGUMENT=
SNAPSHOT_ID=
RETRIEVED_AT=
DECISION_AT=
TOP_N=5

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
    --market-json)
      [ "$#" -ge 2 ] || die "--market-json requires a value"
      MARKET_ARGUMENT=$2
      shift 2
      ;;
    --snapshot-id)
      [ "$#" -ge 2 ] || die "--snapshot-id requires a value"
      SNAPSHOT_ID=$2
      shift 2
      ;;
    --retrieved-at)
      [ "$#" -ge 2 ] || die "--retrieved-at requires a value"
      RETRIEVED_AT=$2
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

[ -n "${SEC_USER_AGENT:-}" ] || die "SEC_USER_AGENT is required"
case "$SEC_USER_AGENT" in
  *$'\n'*|*$'\r'*) die "SEC_USER_AGENT must be a single line" ;;
esac

[ -n "$ROOT_ARGUMENT" ] || die "--root is required"
[ -n "$SEED_ARGUMENT" ] || die "--seed-json is required"
[ -n "$SNAPSHOT_ID" ] || die "--snapshot-id is required"
[ -d "$ROOT_ARGUMENT" ] || die "repository root does not exist"
[ -f "$SEED_ARGUMENT" ] || die "seed JSON does not exist"
if [ -n "$MARKET_ARGUMENT" ] && [ ! -f "$MARKET_ARGUMENT" ]; then
  die "market JSON does not exist"
fi

ROOT=$(CDPATH= cd -- "$ROOT_ARGUMENT" && pwd -P)
SEED_DIR=$(CDPATH= cd -- "$(dirname -- "$SEED_ARGUMENT")" && pwd -P)
SEED_JSON="$SEED_DIR/$(basename -- "$SEED_ARGUMENT")"
if LC_ALL=C grep -Eiq \
    '"example_notice"[[:space:]]*:|example[.]invalid|"EXAMPLE_ONLY"|SYNTHETIC FORMAT EXAMPLE' \
    "$SEED_JSON"; then
  die "refusing to validate a seed that still contains synthetic example markers"
else
  GREP_STATUS=$?
  [ "$GREP_STATUS" -eq 1 ] || die "could not inspect the research seed"
fi
if [ -n "$MARKET_ARGUMENT" ]; then
  MARKET_DIR=$(CDPATH= cd -- "$(dirname -- "$MARKET_ARGUMENT")" && pwd -P)
  MARKET_JSON="$MARKET_DIR/$(basename -- "$MARKET_ARGUMENT")"
else
  MARKET_JSON=
fi
PYTHON="$ROOT/us_equity_research/.venv/bin/python"
[ -x "$PYTHON" ] || die "US project Python is missing; run uv sync first"

case "$TOP_N" in
  ''|*[!0-9]*) die "--top-n must be an integer from 1 to 20" ;;
esac
[ "$TOP_N" -ge 1 ] && [ "$TOP_N" -le 20 ] || die "--top-n must be an integer from 1 to 20"

case "$SNAPSHOT_ID" in
  ''|.*|*..*|*[!A-Za-z0-9._-]*) die "--snapshot-id contains unsupported characters" ;;
esac
[ "${#SNAPSHOT_ID}" -le 128 ] || die "--snapshot-id is longer than 128 characters"

validate_timestamp() {
  timestamp=$1
  option=$2
  case "$timestamp" in
    ''|*[!0-9TtZz:+.-]*) die "$option must be a timezone-aware ISO timestamp" ;;
  esac
}

if [ -n "$RETRIEVED_AT" ]; then
  validate_timestamp "$RETRIEVED_AT" "--retrieved-at"
fi
if [ -n "$DECISION_AT" ]; then
  validate_timestamp "$DECISION_AT" "--decision-at"
fi

LOCK_PARENT="$ROOT/.runtime/locks"
LOCK_DIR="$LOCK_PARENT/us-validation.lock"
TMP_PARENT="$ROOT/.runtime/tmp"
mkdir -p "$LOCK_PARENT" "$TMP_PARENT"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  die "another US validation run holds the lock" 75
fi
printf '%s\n' "$$" >"$LOCK_DIR/pid"

TMP_DIR=
cleanup() {
  if [ -n "$TMP_DIR" ]; then
    rm -f \
      "$TMP_DIR/collect.json" \
      "$TMP_DIR/doctor.json" \
      "$TMP_DIR/run.json" \
      "$TMP_DIR/report.json"
    rmdir "$TMP_DIR" 2>/dev/null || true
  fi
  rm -f "$LOCK_DIR/pid"
  rmdir "$LOCK_DIR" 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
TMP_DIR=$(mktemp -d "$TMP_PARENT/us-validation.XXXXXX")

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

printf 'run_us_validation: collecting an immutable SEC snapshot\n' >&2
COLLECT_ARGUMENTS=(
  --workspace "$ROOT"
  collect-sec-snapshot
  --seed-json "$SEED_JSON"
  --snapshot-id "$SNAPSHOT_ID"
)
if [ -n "$RETRIEVED_AT" ]; then
  COLLECT_ARGUMENTS+=(--retrieved-at "$RETRIEVED_AT")
fi
if [ -n "$MARKET_JSON" ]; then
  COLLECT_ARGUMENTS+=(--market-json "$MARKET_JSON")
fi
if ! "$PYTHON" -m us_equity_research.cli "${COLLECT_ARGUMENTS[@]}" >"$TMP_DIR/collect.json"; then
  die "SEC snapshot collection failed; validation was not started"
fi

PUBLISHED_SNAPSHOT_ID=$(extract_json_field "$TMP_DIR/collect.json" snapshot_id) || \
  die "collector returned invalid JSON; validation was not started"
[ "$PUBLISHED_SNAPSHOT_ID" = "$SNAPSHOT_ID" ] || \
  die "collector returned an unexpected snapshot ID; validation was not started"
PUBLISHED_SNAPSHOT_HASH=$(extract_json_field "$TMP_DIR/collect.json" snapshot_hash) || \
  die "collector returned no snapshot hash; validation was not started"
case "$PUBLISHED_SNAPSHOT_HASH" in
  *[!0-9a-fA-F]*) die "collector returned an invalid snapshot hash; validation was not started" ;;
esac
[ "${#PUBLISHED_SNAPSHOT_HASH}" -eq 64 ] || \
  die "collector returned an invalid snapshot hash; validation was not started"
PUBLISH_STATUS=$(extract_json_field "$TMP_DIR/collect.json" status) || \
  die "collector returned no publication status; validation was not started"
[ "$PUBLISH_STATUS" = "published" ] || \
  die "collector did not publish a snapshot; validation was not started"

printf 'run_us_validation: checking the offline research runtime\n' >&2
"$PYTHON" -m us_equity_research.cli --workspace "$ROOT" doctor >"$TMP_DIR/doctor.json"

if [ -z "$DECISION_AT" ]; then
  DECISION_AT=$("$PYTHON" -c \
    'from datetime import datetime
print(datetime.now().astimezone().isoformat(timespec="microseconds"))') || \
    die "could not determine a timezone-aware decision time"
fi
validate_timestamp "$DECISION_AT" "--decision-at"

printf 'run_us_validation: running research against the explicit SEC snapshot ID\n' >&2
printf '%s\n' \
  "{\"schema_version\":\"0.1\",\"market\":\"US\",\"workflow\":\"daily_report\",\"decision_at\":\"$DECISION_AT\",\"snapshot\":{\"selector\":\"id\",\"snapshot_id\":\"$SNAPSHOT_ID\"},\"top_n\":$TOP_N}" \
  | "$PYTHON" -m us_equity_research.cli --workspace "$ROOT" run --request-json - \
      >"$TMP_DIR/run.json"

RUN_SNAPSHOT_ID=$(extract_json_field "$TMP_DIR/run.json" snapshot_id) || \
  die "research returned no snapshot ID"
[ "$RUN_SNAPSHOT_ID" = "$SNAPSHOT_ID" ] || \
  die "research used an unexpected snapshot ID"
ARTIFACT_ID=$(extract_json_field "$TMP_DIR/run.json" artifact_id) || \
  die "research returned no artifact ID"
case "$ARTIFACT_ID" in
  ''|*[!A-Za-z0-9._-]*) die "research returned an invalid artifact ID" ;;
esac

printf 'run_us_validation: reading the complete report artifact\n' >&2
printf '%s\n' \
  "{\"artifact_id\":\"$ARTIFACT_ID\",\"section\":\"report\",\"max_chars\":20000}" \
  | "$PYTHON" -m us_equity_research.cli --workspace "$ROOT" artifact-read --request-json - \
      >"$TMP_DIR/report.json"

cat "$TMP_DIR/report.json"

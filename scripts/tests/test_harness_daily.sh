#!/usr/bin/env bash

set -eu

fail() {
  printf 'not ok - %s\n' "$1" >&2
  exit 1
}

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
REPOSITORY=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd -P)
TMP_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/cn-harness-test.XXXXXX")
cleanup() {
  rm -rf "$TMP_ROOT"
}
trap cleanup EXIT HUP INT TERM

python3 - "$REPOSITORY/scripts/launchd/com.stcatz.stock-research.cn-harness-daily.plist.example" <<'PY' || \
  fail "networked Harness launchd template is invalid"
import plistlib
from pathlib import Path
import sys

path = Path(sys.argv[1])
payload = plistlib.loads(path.read_bytes())
intervals = payload["StartCalendarInterval"]
assert [(item["Weekday"], item["Hour"], item["Minute"]) for item in intervals] == [
    (weekday, 20, 45) for weekday in range(2, 7)
]
arguments = payload["ProgramArguments"]
assert arguments[0].endswith("/scripts/run_cn_harness_daily.sh")
assert arguments[arguments.index("--bear-profile") + 1] == "web"
assert arguments[arguments.index("--judge-profile") + 1] == "headless"
assert "EnvironmentVariables" not in payload
assert "HITHINK_FINANCE_API_KEY" not in path.read_text(encoding="utf-8")
PY

ROOT="$TMP_ROOT/repository"
mkdir -p \
  "$ROOT/scripts" \
  "$ROOT/a_share_research/prompts" \
  "$ROOT/a_share_research/.venv/bin" \
  "$ROOT/a_share_research/src" \
  "$ROOT/data/normalized"
cp "$REPOSITORY/scripts/run_cn_harness_daily.sh" "$ROOT/scripts/run_cn_harness_daily.sh"
cp "$REPOSITORY/a_share_research/prompts/harness-independent-bear.md" \
  "$ROOT/a_share_research/prompts/harness-independent-bear.md"
cp "$REPOSITORY/a_share_research/prompts/harness-daily-opportunity.md" \
  "$ROOT/a_share_research/prompts/harness-daily-opportunity.md"
cp -R "$REPOSITORY/a_share_research/src/a_share_research" \
  "$ROOT/a_share_research/src/a_share_research"
mkdir -p "$ROOT/data/normalized/cn-harness-test"
python3 - \
  "$REPOSITORY/a_share_research/src/a_share_research/fixtures/demo_snapshot.json" \
  "$ROOT/data/normalized/cn-harness-test/snapshot.json" <<'PY'
import json
from pathlib import Path
import sys

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
payload["snapshot_id"] = "cn-harness-test"
payload["themes"] = []
Path(sys.argv[2]).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
PY
chmod 755 "$ROOT/scripts/run_cn_harness_daily.sh"
ln -s "$(command -v python3)" "$ROOT/a_share_research/.venv/bin/python"

SEED="$TMP_ROOT/research-seed.json"
printf '%s\n' '{"schema_version":"0.1","market":"CN","themes":[]}' >"$SEED"

cat >"$ROOT/scripts/run_cn_daily.sh" <<'EOF'
#!/usr/bin/env bash
set -eu
ROOT=
SNAPSHOT_ID=
DECISION_AT=
TOP_N=9
while [ "$#" -gt 0 ]; do
  case "$1" in
    --root) ROOT=$2; shift 2 ;;
    --snapshot-id) SNAPSHOT_ID=$2; shift 2 ;;
    --decision-at) DECISION_AT=$2; shift 2 ;;
    --top-n) TOP_N=$2; shift 2 ;;
    --seed-json|--provider|--raw-store-root) shift 2 ;;
    *) exit 2 ;;
  esac
done
PYTHON="$ROOT/a_share_research/.venv/bin/python"
RUN_JSON=$(printf '%s\n' \
  "{\"schema_version\":\"0.1\",\"market\":\"CN\",\"workflow\":\"daily_report\",\"decision_at\":\"$DECISION_AT\",\"snapshot\":{\"selector\":\"id\",\"snapshot_id\":\"$SNAPSHOT_ID\"},\"top_n\":$TOP_N}" \
  | env PYTHONPATH="$ROOT/a_share_research/src" \
      "$PYTHON" -m a_share_research.cli --workspace "$ROOT" run --request-json -)
ARTIFACT_ID=$(printf '%s' "$RUN_JSON" \
  | "$PYTHON" -c 'import json, sys; print(json.load(sys.stdin)["artifact_id"])')
printf '%s\n' \
  "{\"artifact_id\":\"$ARTIFACT_ID\",\"section\":\"report\",\"max_chars\":20000}" \
  | env PYTHONPATH="$ROOT/a_share_research/src" \
      "$PYTHON" -m a_share_research.cli --workspace "$ROOT" artifact-read --request-json -
EOF
chmod 755 "$ROOT/scripts/run_cn_daily.sh"

DSH_LOG="$TMP_ROOT/dsh.log"
DSH_BIN="$TMP_ROOT/dsh"
cat >"$DSH_BIN" <<'EOF'
#!/usr/bin/env bash
set -eu
: "${DSH_TEST_LOG:?}"
: "${DSH_TEST_ROOT:?}"
if [ -n "${HITHINK_FINANCE_API_KEY:-}" ]; then
  printf 'secret leaked\n' >>"$DSH_TEST_LOG"
  exit 9
fi
printf '%s\n' "$3" >>"$DSH_TEST_LOG"
ARTIFACT_ID=$(printf '%s\n' "$3" | sed -n 's/^- artifact_id：`\([^`]*\)`.*/\1/p' | head -1)
DECISION_AT=$(printf '%s\n' "$3" | sed -n 's/^- decision_at：`\([^`]*\)`.*/\1/p' | head -1)
SNAPSHOT_ID=$(printf '%s\n' "$3" | sed -n 's/^- snapshot_id：`\([^`]*\)`.*/\1/p' | head -1)
artifact_receipt() {
  section=$1
  printf '%s\n' \
    "{\"artifact_id\":\"$ARTIFACT_ID\",\"section\":\"$section\",\"max_chars\":20000}" \
    | env PYTHONPATH="$DSH_TEST_ROOT/a_share_research/src" \
        "$DSH_TEST_ROOT/a_share_research/.venv/bin/python" \
        -m a_share_research.cli --workspace "$DSH_TEST_ROOT" \
        artifact-read --request-json -
}
json_field() {
  printf '%s' "$1" \
    | "$DSH_TEST_ROOT/a_share_research/.venv/bin/python" \
        -c 'import json, sys; print(json.load(sys.stdin)[sys.argv[1]])' "$2"
}
CALLS=$(grep -c '^CALL$' "$DSH_TEST_LOG" 2>/dev/null || true)
printf 'CALL\n' >>"$DSH_TEST_LOG"
if [ "$CALLS" -eq 0 ]; then
  FACTS_RECEIPT=$(artifact_receipt facts)
  FACTS_HASH=$(json_field "$FACTS_RECEIPT" content_sha256)
  FACTS_CHARS=$(json_field "$FACTS_RECEIPT" total_chars)
  FACTS_PAGES=$(( (FACTS_CHARS + 19999) / 20000 ))
  printf '%s\n' '已完成反方检查，下面给出结构化结果：'
  printf '%s\n' "{\"artifact_id\":\"$ARTIFACT_ID\",\"decision_at\":\"$DECISION_AT\",\"review_mode\":\"independent_bear\",\"integrity\":{\"content_sha256\":\"$FACTS_HASH\",\"total_chars\":$FACTS_CHARS,\"pages_read\":$FACTS_PAGES},\"candidates\":[],\"global_data_risks\":[]}"
else
  REPORT_RECEIPT=$(artifact_receipt report)
  PACKET_RECEIPT=$(artifact_receipt packet)
  REPORT_HASH=$(json_field "$REPORT_RECEIPT" content_sha256)
  PACKET_HASH=$(json_field "$PACKET_RECEIPT" content_sha256)
  REPORT_CHARS=$(json_field "$REPORT_RECEIPT" total_chars)
  PACKET_CHARS=$(json_field "$PACKET_RECEIPT" total_chars)
  PAGES=$(( (REPORT_CHARS + 19999) / 20000 + (PACKET_CHARS + 19999) / 20000 ))
  printf '%s\n' "{\"artifact_id\":\"$ARTIFACT_ID\",\"snapshot_id\":\"$SNAPSHOT_ID\",\"decision_at\":\"$DECISION_AT\",\"integrity\":{\"report_content_sha256\":\"$REPORT_HASH\",\"packet_content_sha256\":\"$PACKET_HASH\",\"pages_read\":$PAGES},\"executive_summary\":\"测试运行没有冻结候选。\",\"candidate_rankings\":[],\"new_opportunities\":[],\"collection_requests\":[],\"outcome_feedback\":{\"sample_count\":0,\"summary\":\"尚无可用结算样本。\"},\"audit_notes\":[]}"
fi
EOF
chmod 755 "$DSH_BIN"

if "$ROOT/scripts/run_cn_harness_daily.sh" \
    --root "$ROOT" \
    --seed-json "$SEED" \
    --provider baostock \
    --snapshot-id cn-harness-same-profile-test \
    --decision-at 2026-08-26T20:30:00+08:00 \
    --dsh-bin "$DSH_BIN" \
    --bear-profile same \
    --judge-profile same \
    >"$TMP_ROOT/same-profile.stdout" 2>"$TMP_ROOT/same-profile.stderr"; then
  fail "Harness accepted one DSH profile for both review roles"
fi
grep -F 'profiles must differ' "$TMP_ROOT/same-profile.stderr" >/dev/null || \
  fail "same-profile rejection was not actionable"

OUTPUT=$(
  HITHINK_FINANCE_API_KEY=must-not-reach-model \
  DSH_TEST_LOG="$DSH_LOG" \
  DSH_TEST_ROOT="$ROOT" \
  "$ROOT/scripts/run_cn_harness_daily.sh" \
    --root "$ROOT" \
    --seed-json "$SEED" \
    --provider baostock \
    --snapshot-id cn-harness-test \
    --decision-at 2026-08-26T20:30:00+08:00 \
    --dsh-bin "$DSH_BIN" \
    --bear-profile skeptic \
    --judge-profile judge \
    --bear-model-id model-bear-v1 \
    --judge-model-id model-judge-v2
) || fail "Harness wrapper failed"

printf '%s' "$OUTPUT" | grep -F '"networked_harness": true' >/dev/null || \
  fail "result does not identify the networked Harness"
OUTPUT_DIR="$ROOT/.runtime/harness/cn/cn-harness-test"
[ -f "$OUTPUT_DIR/canonical.json" ] || fail "canonical receipt was not preserved"
[ -f "$OUTPUT_DIR/canonical-runner-receipt.json" ] || \
  fail "canonical runner receipt was not preserved"
[ -f "$OUTPUT_DIR/artifact-integrity.json" ] || \
  fail "artifact integrity receipt was not preserved"
grep -F '"method_id": "a-share-theme-v2.0"' "$OUTPUT_DIR/canonical.json" >/dev/null || \
  fail "verified canonical summary lost the method identity"
grep -F '"candidate_index": []' "$OUTPUT_DIR/canonical.json" >/dev/null || \
  fail "verified canonical summary lost the candidate index"
[ -f "$OUTPUT_DIR/settlement.json" ] || fail "settlement receipt was not preserved"
[ -f "$OUTPUT_DIR/outcome-history.json" ] || fail "outcome history was not preserved"
[ -f "$OUTPUT_DIR/research-history.json" ] || fail "research history was not preserved"
[ -f "$OUTPUT_DIR/independent-bear.json" ] || fail "bear review was not preserved"
[ -f "$OUTPUT_DIR/final-judgment.json" ] || fail "final judgment was not preserved"
[ -f "$OUTPUT_DIR/opportunity-memo.md" ] || fail "memo was not preserved"
[ -f "$OUTPUT_DIR/harness-manifest.json" ] || fail "Harness manifest was not preserved"
grep -F 'section=facts' "$DSH_LOG" >/dev/null || fail "bear prompt did not require facts"
grep -F 'independent_bear_review_json_string' "$DSH_LOG" >/dev/null || \
  fail "final prompt did not receive the independent review"
if grep -F 'secret leaked' "$DSH_LOG" >/dev/null; then
  fail "provider secret reached DSH"
fi
[ "$(grep -c '^CALL$' "$DSH_LOG")" -eq 2 ] || fail "Harness did not make two isolated calls"
grep -F '"provider_credentials_forwarded_to_models": false' \
  "$OUTPUT_DIR/harness-manifest.json" >/dev/null || fail "manifest lost credential boundary"
grep -F '"input_scope": "facts_only"' \
  "$OUTPUT_DIR/harness-manifest.json" >/dev/null || fail "manifest lost thesis-blind scope"
grep -F '"review_profiles_distinct": true' \
  "$OUTPUT_DIR/harness-manifest.json" >/dev/null || fail "manifest lost profile independence"
grep -F '"declared_model_id": "model-bear-v1"' \
  "$OUTPUT_DIR/harness-manifest.json" >/dev/null || fail "manifest lost bear model identity"
grep -F '"declared_model_id": "model-judge-v2"' \
  "$OUTPUT_DIR/harness-manifest.json" >/dev/null || fail "manifest lost judge model identity"

printf 'ok - CN networked Harness is isolated, auditable and research-only\n'

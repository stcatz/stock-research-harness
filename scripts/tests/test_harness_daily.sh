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
assert "EnvironmentVariables" not in payload
assert "HITHINK_FINANCE_API_KEY" not in path.read_text(encoding="utf-8")
PY

ROOT="$TMP_ROOT/repository"
mkdir -p \
  "$ROOT/scripts" \
  "$ROOT/a_share_research/prompts" \
  "$ROOT/a_share_research/.venv/bin" \
  "$ROOT/data/normalized"
cp "$REPOSITORY/scripts/run_cn_harness_daily.sh" "$ROOT/scripts/run_cn_harness_daily.sh"
cp "$REPOSITORY/a_share_research/prompts/harness-independent-bear.md" \
  "$ROOT/a_share_research/prompts/harness-independent-bear.md"
cp "$REPOSITORY/a_share_research/prompts/harness-daily-opportunity.md" \
  "$ROOT/a_share_research/prompts/harness-daily-opportunity.md"
chmod 755 "$ROOT/scripts/run_cn_harness_daily.sh"
ln -s "$(command -v python3)" "$ROOT/a_share_research/.venv/bin/python"

SEED="$TMP_ROOT/research-seed.json"
printf '%s\n' '{"schema_version":"0.1","market":"CN","themes":[]}' >"$SEED"

cat >"$ROOT/scripts/run_cn_daily.sh" <<'EOF'
#!/usr/bin/env bash
set -eu
printf '%s\n' '{"schema_version":"0.1","market":"CN","artifact_id":"cn-artifact-harness-test","section":"report","content_type":"text/markdown","content":"page","truncated":false,"cursor":0,"next_cursor":null,"total_chars":4,"content_sha256":"0000000000000000000000000000000000000000000000000000000000000000","relative_path":"artifacts/runs/test/report.md"}'
EOF
chmod 755 "$ROOT/scripts/run_cn_daily.sh"

DSH_LOG="$TMP_ROOT/dsh.log"
DSH_BIN="$TMP_ROOT/dsh"
cat >"$DSH_BIN" <<'EOF'
#!/usr/bin/env bash
set -eu
: "${DSH_TEST_LOG:?}"
if [ -n "${HITHINK_FINANCE_API_KEY:-}" ]; then
  printf 'secret leaked\n' >>"$DSH_TEST_LOG"
  exit 9
fi
printf '%s\n' "$3" >>"$DSH_TEST_LOG"
CALLS=$(grep -c '^CALL$' "$DSH_TEST_LOG" 2>/dev/null || true)
printf 'CALL\n' >>"$DSH_TEST_LOG"
if [ "$CALLS" -eq 0 ]; then
  printf '%s\n' '{"artifact_id":"cn-artifact-harness-test","decision_at":"2026-08-26T20:30:00+08:00","review_mode":"independent_bear","integrity":{"content_sha256":"hash","total_chars":1,"pages_read":1},"candidates":[],"global_data_risks":[]}'
else
  printf '%s\n' '# 每日联网研究备忘录'
  printf '%s\n' ''
  printf '%s\n' '- artifact_id: cn-artifact-harness-test'
  printf '%s\n' '- 状态：continue_research'
  printf '%s\n' ''
  printf '%s\n' '本报告仅用于研究，不构成投资建议，所有事实与交易判断须由用户独立复核。'
fi
EOF
chmod 755 "$DSH_BIN"

OUTPUT=$(
  HITHINK_FINANCE_API_KEY=must-not-reach-model \
  DSH_TEST_LOG="$DSH_LOG" \
  "$ROOT/scripts/run_cn_harness_daily.sh" \
    --root "$ROOT" \
    --seed-json "$SEED" \
    --provider baostock \
    --snapshot-id cn-harness-test \
    --decision-at 2026-08-26T20:30:00+08:00 \
    --dsh-bin "$DSH_BIN"
) || fail "Harness wrapper failed"

printf '%s' "$OUTPUT" | grep -F '"networked_harness": true' >/dev/null || \
  fail "result does not identify the networked Harness"
OUTPUT_DIR="$ROOT/.runtime/harness/cn/cn-harness-test"
[ -f "$OUTPUT_DIR/canonical.json" ] || fail "canonical receipt was not preserved"
[ -f "$OUTPUT_DIR/independent-bear.json" ] || fail "bear review was not preserved"
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

printf 'ok - CN networked Harness is isolated, auditable and research-only\n'

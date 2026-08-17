#!/usr/bin/env bash

set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
CN_WRAPPER="$REPO_ROOT/scripts/run_cn_daily.sh"
US_WRAPPER="$REPO_ROOT/scripts/run_us_validation.sh"
CN_INSTALLER="$REPO_ROOT/scripts/install_cn_launchd.sh"

TEST_TMP=$(mktemp -d "${TMPDIR:-/tmp}/stock-wrapper-tests.XXXXXX")
trap 'rm -rf "$TEST_TMP"' EXIT HUP INT TERM

pass_count=0

fail() {
  printf 'not ok - %s\n' "$1" >&2
  exit 1
}

pass() {
  pass_count=$((pass_count + 1))
  printf 'ok %s - %s\n' "$pass_count" "$1"
}

assert_contains() {
  needle=$1
  path=$2
  if ! grep -F -- "$needle" "$path" >/dev/null 2>&1; then
    fail "expected $path to contain: $needle"
  fi
}

assert_not_contains() {
  needle=$1
  path=$2
  if grep -F -- "$needle" "$path" >/dev/null 2>&1; then
    fail "expected $path not to contain: $needle"
  fi
}

new_root() {
  name=$1
  root="$TEST_TMP/$name"
  mkdir -p \
    "$root/a_share_research/.venv/bin" \
    "$root/us_equity_research/.venv/bin" \
    "$root/seeds"
  printf '{"themes":[],"candidates":[]}\n' >"$root/seeds/cn.json"
  printf '{"themes":[],"candidates":[]}\n' >"$root/seeds/us.json"
  printf '{"symbols":{}}\n' >"$root/seeds/market.json"
  install_mock_python "$root/a_share_research/.venv/bin/python"
  install_mock_python "$root/us_equity_research/.venv/bin/python"
  printf '%s\n' "$root"
}

install_mock_python() {
  target=$1
  cat >"$target" <<'MOCK'
#!/usr/bin/env bash
set -eu

: "${MOCK_CALL_LOG:?MOCK_CALL_LOG is required}"
printf '%s\n' "$*" >>"$MOCK_CALL_LOG"

if [ "${1:-}" = "-c" ]; then
  json_path=${3:?JSON path is required}
  field=${4:?JSON field is required}
  sed -n "s/.*\"$field\"[[:space:]]*:[[:space:]]*\"\([^\"]*\)\".*/\1/p" "$json_path" | head -n 1
  exit 0
fi

module=
command=
previous=
snapshot_id=
for argument in "$@"; do
  if [ "$previous" = "-m" ]; then
    module=$argument
  fi
  if [ "$previous" = "--snapshot-id" ]; then
    snapshot_id=$argument
  fi
  case "$argument" in
    collect-snapshot|collect-sec-snapshot|doctor|run|artifact-read)
      command=$argument
      ;;
  esac
  previous=$argument
done

if [ -n "${MOCK_FAIL_STAGE:-}" ] && [ "$command" = "$MOCK_FAIL_STAGE" ]; then
  printf '{"error":"mock failure"}\n' >&2
  exit 9
fi

case "$command" in
  collect-snapshot)
    printf '{"schema_version":"0.1","market":"CN","status":"published","snapshot_id":"%s","snapshot_hash":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}\n' "$snapshot_id"
    ;;
  collect-sec-snapshot)
    printf '{"schema_version":"0.1","market":"US","status":"published","snapshot_id":"%s","snapshot_hash":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}\n' "$snapshot_id"
    ;;
  doctor)
    printf '{"schema_version":"0.1","market":"US","status":"ok"}\n'
    ;;
  run)
    request_log="${MOCK_REQUEST_LOG:?MOCK_REQUEST_LOG is required}.${module}.run"
    cat >"$request_log"
    case "$module" in
      a_share_research.cli)
        printf '{"schema_version":"0.1","market":"CN","snapshot_id":"cn-20260818-test","artifact_id":"cn-artifact-real"}\n'
        ;;
      us_equity_research.cli)
        printf '{"schema_version":"0.1","market":"US","snapshot_id":"us-sec-20260818-test","artifact_id":"us-artifact-real"}\n'
        ;;
      *)
        printf 'unexpected module: %s\n' "$module" >&2
        exit 10
        ;;
    esac
    ;;
  artifact-read)
    request_log="${MOCK_REQUEST_LOG:?MOCK_REQUEST_LOG is required}.${module}.artifact"
    cat >"$request_log"
    printf '{"section":"report","content":"REAL RESEARCH REPORT","truncated":false}\n'
    ;;
  *)
    printf 'unexpected mock invocation: %s\n' "$*" >&2
    exit 11
    ;;
esac
MOCK
  chmod 700 "$target"
}

run_expect_failure() {
  stdout_path=$1
  stderr_path=$2
  shift 2
  if "$@" >"$stdout_path" 2>"$stderr_path"; then
    fail "command unexpectedly succeeded: $*"
  fi
}

test_cn_success() {
  root=$(new_root cn-success)
  call_log="$root/calls.log"
  request_prefix="$root/request"
  : >"$call_log"

  if ! MOCK_CALL_LOG=$call_log MOCK_REQUEST_LOG=$request_prefix \
    "$CN_WRAPPER" \
      --root "$root" \
      --seed-json "$root/seeds/cn.json" \
      --snapshot-id cn-20260818-test \
      --decision-at 2026-08-18T20:31:00+08:00 \
      >"$root/stdout" 2>"$root/stderr"; then
    sed -n '1,120p' "$root/stderr" >&2
    fail "CN success case failed"
  fi

  commands="$root/commands.log"
  grep ' -m \|^-m ' "$call_log" | grep -v '^-c ' >"$commands"
  [ "$(wc -l <"$commands" | tr -d ' ')" = "3" ] || fail "CN must make three CLI calls"
  sed -n '1p' "$commands" | grep -F 'collect-snapshot' >/dev/null || fail "CN collect must run first"
  sed -n '2p' "$commands" | grep -F ' run ' >/dev/null || fail "CN research run must run second"
  sed -n '3p' "$commands" | grep -F 'artifact-read' >/dev/null || fail "CN artifact read must run last"
  assert_contains '"selector":"id","snapshot_id":"cn-20260818-test"' "$request_prefix.a_share_research.cli.run"
  assert_not_contains 'demo' "$call_log"
  assert_not_contains 'demo' "$request_prefix.a_share_research.cli.run"
  assert_contains '"section":"report"' "$request_prefix.a_share_research.cli.artifact"
  assert_contains '"max_chars":20000' "$request_prefix.a_share_research.cli.artifact"
  assert_contains 'REAL RESEARCH REPORT' "$root/stdout"
  pass "CN success uses the exact newly collected real snapshot and reads the full report"
}

test_cn_collect_failure_short_circuits() {
  root=$(new_root cn-collect-failure)
  call_log="$root/calls.log"
  : >"$call_log"

  run_expect_failure "$root/stdout" "$root/stderr" \
    env MOCK_CALL_LOG="$call_log" MOCK_REQUEST_LOG="$root/request" MOCK_FAIL_STAGE=collect-snapshot \
    "$CN_WRAPPER" \
      --root "$root" \
      --seed-json "$root/seeds/cn.json" \
      --snapshot-id cn-failed-test \
      --decision-at 2026-08-18T20:31:00+08:00

  assert_contains 'collect-snapshot' "$call_log"
  assert_not_contains ' run ' "$call_log"
  assert_not_contains 'artifact-read' "$call_log"
  pass "CN collection failure prevents research and artifact publication"
}

test_cn_lock_rejects_overlap() {
  root=$(new_root cn-lock)
  mkdir -p "$root/.runtime/locks/cn-daily.lock"
  call_log="$root/calls.log"
  : >"$call_log"

  run_expect_failure "$root/stdout" "$root/stderr" \
    env MOCK_CALL_LOG="$call_log" MOCK_REQUEST_LOG="$root/request" \
    "$CN_WRAPPER" \
      --root "$root" \
      --seed-json "$root/seeds/cn.json" \
      --snapshot-id cn-lock-test \
      --decision-at 2026-08-18T20:31:00+08:00

  [ ! -s "$call_log" ] || fail "CN overlap must not invoke Python"
  pass "CN mkdir lock rejects overlapping daily runs"
}

test_synthetic_seed_markers_are_rejected() {
  root=$(new_root synthetic-seeds)
  call_log="$root/calls.log"
  : >"$call_log"
  printf '{"example_notice":"SYNTHETIC FORMAT EXAMPLE ONLY"}\n' >"$root/seeds/renamed-cn.json"
  run_expect_failure "$root/cn.stdout" "$root/cn.stderr" \
    env MOCK_CALL_LOG="$call_log" MOCK_REQUEST_LOG="$root/request-cn" \
    "$CN_WRAPPER" \
      --root "$root" \
      --seed-json "$root/seeds/renamed-cn.json" \
      --snapshot-id cn-renamed-example \
      --decision-at 2026-08-18T20:31:00+08:00
  [ ! -s "$call_log" ] || fail "CN synthetic marker rejection must happen before collection"

  printf '{"source_url":"https://example.invalid/filing","risk_flags":["EXAMPLE_ONLY"]}\n' \
    >"$root/seeds/renamed-us.json"
  run_expect_failure "$root/us.stdout" "$root/us.stderr" \
    env SEC_USER_AGENT='Test test@example.invalid' MOCK_CALL_LOG="$call_log" \
    MOCK_REQUEST_LOG="$root/request-us" \
    "$US_WRAPPER" \
      --root "$root" \
      --seed-json "$root/seeds/renamed-us.json" \
      --snapshot-id us-renamed-example \
      --decision-at 2026-08-18T08:30:00-04:00
  [ ! -s "$call_log" ] || fail "US synthetic marker rejection must happen before collection"
  pass "renamed seeds that retain synthetic fixture markers fail closed"
}

test_us_requires_user_agent() {
  root=$(new_root us-no-user-agent)
  call_log="$root/calls.log"
  : >"$call_log"

  run_expect_failure "$root/stdout" "$root/stderr" \
    env -u SEC_USER_AGENT MOCK_CALL_LOG="$call_log" MOCK_REQUEST_LOG="$root/request" \
    "$US_WRAPPER" \
      --root "$root" \
      --seed-json "$root/seeds/us.json" \
      --snapshot-id us-sec-test \
      --decision-at 2026-08-18T08:30:00-04:00

  [ ! -s "$call_log" ] || fail "US missing user agent must fail before invoking Python"
  assert_contains 'SEC_USER_AGENT' "$root/stderr"
  pass "US validation fails closed when SEC_USER_AGENT is absent"
}

test_us_success_and_no_secret_logging() {
  root=$(new_root us-success)
  call_log="$root/calls.log"
  request_prefix="$root/request"
  secret_user_agent='Research Operator secret-address@example.invalid'
  : >"$call_log"

  if ! SEC_USER_AGENT=$secret_user_agent MOCK_CALL_LOG=$call_log MOCK_REQUEST_LOG=$request_prefix \
    "$US_WRAPPER" \
      --root "$root" \
      --seed-json "$root/seeds/us.json" \
      --market-json "$root/seeds/market.json" \
      --snapshot-id us-sec-20260818-test \
      --decision-at 2026-08-18T08:30:00-04:00 \
      >"$root/stdout" 2>"$root/stderr"; then
    sed -n '1,120p' "$root/stderr" >&2
    fail "US success case failed"
  fi

  commands="$root/commands.log"
  grep ' -m \|^-m ' "$call_log" | grep -v '^-c ' >"$commands"
  [ "$(wc -l <"$commands" | tr -d ' ')" = "4" ] || fail "US must make four CLI calls"
  sed -n '1p' "$commands" | grep -F 'collect-sec-snapshot' >/dev/null || fail "US collect must run first"
  sed -n '2p' "$commands" | grep -F ' doctor' >/dev/null || fail "US doctor must run second"
  sed -n '3p' "$commands" | grep -F ' run ' >/dev/null || fail "US run must run third"
  sed -n '4p' "$commands" | grep -F 'artifact-read' >/dev/null || fail "US artifact read must run last"
  assert_contains '"selector":"id","snapshot_id":"us-sec-20260818-test"' "$request_prefix.us_equity_research.cli.run"
  assert_contains '"section":"report"' "$request_prefix.us_equity_research.cli.artifact"
  assert_contains '"max_chars":20000' "$request_prefix.us_equity_research.cli.artifact"
  assert_not_contains 'demo' "$call_log"
  assert_not_contains "$secret_user_agent" "$call_log"
  assert_not_contains "$secret_user_agent" "$root/stdout"
  assert_not_contains "$secret_user_agent" "$root/stderr"
  assert_contains 'REAL RESEARCH REPORT' "$root/stdout"
  pass "US validation runs collect/doctor/research/report in order without logging the SEC identity"
}

test_us_failures_short_circuit() {
  for stage in collect-sec-snapshot doctor run; do
    safe_stage=$(printf '%s' "$stage" | tr '-' '_')
    root=$(new_root "us-fail-$safe_stage")
    call_log="$root/calls.log"
    : >"$call_log"

    run_expect_failure "$root/stdout" "$root/stderr" \
      env SEC_USER_AGENT='Test test@example.invalid' MOCK_CALL_LOG="$call_log" \
      MOCK_REQUEST_LOG="$root/request" MOCK_FAIL_STAGE="$stage" \
      "$US_WRAPPER" \
        --root "$root" \
        --seed-json "$root/seeds/us.json" \
        --snapshot-id "us-fail-$safe_stage" \
        --decision-at 2026-08-18T08:30:00-04:00

    case "$stage" in
      collect-sec-snapshot)
        assert_not_contains ' doctor' "$call_log"
        assert_not_contains ' run ' "$call_log"
        ;;
      doctor)
        assert_not_contains ' run ' "$call_log"
        ;;
      run)
        assert_not_contains 'artifact-read' "$call_log"
        ;;
    esac
  done
  pass "US collection, doctor, and research failures each stop the pipeline"
}

test_launchd_example() {
  plist="$REPO_ROOT/scripts/launchd/com.stcatz.stock-research.cn-daily.plist.example"
  [ -f "$plist" ] || fail "launchd example is missing"
  if command -v plutil >/dev/null 2>&1; then
    plutil -lint "$plist" >/dev/null
  fi
  [ "$(grep -c '<key>Weekday</key>' "$plist")" = "5" ] || fail "launchd must schedule five weekdays"
  assert_contains '<integer>20</integer>' "$plist"
  assert_contains '<integer>30</integer>' "$plist"
  assert_contains 'run_cn_daily.sh' "$plist"
  assert_not_contains '>dsh<' "$plist"
  pass "launchd example runs the standalone CN pipeline at 20:30 on weekdays"
}

test_launchd_installer() {
  [ -x "$CN_INSTALLER" ] || fail "launchd installer is missing or not executable"
  "$CN_INSTALLER" --self-test >"$TEST_TMP/installer-self-test.stdout"
  assert_contains 'self-test passed' "$TEST_TMP/installer-self-test.stdout"

  fake_home="$TEST_TMP/installer-home"
  fake_root="$TEST_TMP/installer-repo"
  seed="$TEST_TMP/private-cn-seed.json"
  mkdir -p "$fake_home" "$fake_root/scripts"
  cp "$CN_WRAPPER" "$fake_root/scripts/run_cn_daily.sh"
  chmod 700 "$fake_root/scripts/run_cn_daily.sh"
  printf '{"themes":[],"candidates":[]}\n' >"$seed"
  HOME=$fake_home "$CN_INSTALLER" --root "$fake_root" --seed-json "$seed" \
    >"$TEST_TMP/installer.stdout"

  installed="$fake_home/Library/LaunchAgents/com.stcatz.stock-research.cn-daily.plist"
  canonical_fake_root=$(CDPATH= cd -- "$fake_root" && pwd -P)
  canonical_seed_dir=$(CDPATH= cd -- "$(dirname -- "$seed")" && pwd -P)
  canonical_seed="$canonical_seed_dir/$(basename -- "$seed")"
  [ -f "$installed" ] || fail "installer did not write the LaunchAgent plist"
  assert_contains "$canonical_fake_root/scripts/run_cn_daily.sh" "$installed"
  assert_contains "$canonical_seed" "$installed"
  assert_not_contains '__ROOT__' "$installed"
  assert_not_contains '__SEED_JSON__' "$installed"
  [ -d "$fake_root/.runtime/logs" ] || fail "installer did not create the launchd log directory"
  assert_contains 'not loaded' "$TEST_TMP/installer.stdout"

  run_expect_failure "$TEST_TMP/relative.stdout" "$TEST_TMP/relative.stderr" \
    "$CN_INSTALLER" --root relative/path --seed-json "$seed"
  pass "launchd installer is atomic, creates log parents, defaults to not loaded, and rejects relative roots"
}

test_cn_success
test_cn_collect_failure_short_circuits
test_cn_lock_rejects_overlap
test_synthetic_seed_markers_are_rejected
test_us_requires_user_agent
test_us_success_and_no_secret_logging
test_us_failures_short_circuit
test_launchd_example
test_launchd_installer

printf '1..%s\n' "$pass_count"

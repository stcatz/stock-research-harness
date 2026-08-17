#!/usr/bin/env bash

set -eu

umask 077

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
SELF="$SCRIPT_DIR/$(basename -- "$0")"
TEMPLATE="$SCRIPT_DIR/launchd/com.stcatz.stock-research.cn-daily.plist.example"
LABEL=com.stcatz.stock-research.cn-daily

usage() {
  cat <<'EOF'
Usage:
  install_cn_launchd.sh --root ABSOLUTE_PATH --seed-json ABSOLUTE_PATH [--load]
  install_cn_launchd.sh --self-test

The installer renders the tracked launchd template, creates the log directory, and
atomically writes ~/Library/LaunchAgents/com.stcatz.stock-research.cn-daily.plist.
It does not load or start the LaunchAgent unless --load is explicitly supplied.
EOF
}

die() {
  message=$1
  code=${2:-2}
  printf 'install_cn_launchd: %s\n' "$message" >&2
  exit "$code"
}

run_self_test() {
  test_tmp=$(mktemp -d "${TMPDIR:-/tmp}/cn-launchd-installer.XXXXXX")
  test_root="$test_tmp/repository"
  test_home="$test_tmp/home"
  test_seed="$test_tmp/private-seed.json"
  mkdir -p "$test_root/scripts" "$test_home"
  cp "$SCRIPT_DIR/run_cn_daily.sh" "$test_root/scripts/run_cn_daily.sh"
  chmod 700 "$test_root/scripts/run_cn_daily.sh"
  printf '{"themes":[],"candidates":[]}\n' >"$test_seed"
  test_root=$(CDPATH= cd -- "$test_root" && pwd -P)
  test_seed_dir=$(CDPATH= cd -- "$(dirname -- "$test_seed")" && pwd -P)
  test_seed="$test_seed_dir/$(basename -- "$test_seed")"

  HOME=$test_home "$SELF" --root "$test_root" --seed-json "$test_seed" \
    >"$test_tmp/install.stdout"
  installed="$test_home/Library/LaunchAgents/$LABEL.plist"
  [ -f "$installed" ] || die "self-test failed: plist was not installed"
  [ -d "$test_root/.runtime/logs" ] || die "self-test failed: log directory was not created"
  grep -F "$test_root/scripts/run_cn_daily.sh" "$installed" >/dev/null || \
    die "self-test failed: root token was not rendered"
  grep -F "$test_seed" "$installed" >/dev/null || \
    die "self-test failed: seed token was not rendered"
  if grep -E '__ROOT__|__SEED_JSON__' "$installed" >/dev/null; then
    die "self-test failed: template tokens remain"
  fi
  if HOME=$test_home "$SELF" --root relative/path --seed-json "$test_seed" \
      >"$test_tmp/relative.stdout" 2>"$test_tmp/relative.stderr"; then
    die "self-test failed: relative root was accepted"
  fi
  if HOME=$test_home "$SELF" --root / --seed-json "$test_seed" \
      >"$test_tmp/broad.stdout" 2>"$test_tmp/broad.stderr"; then
    die "self-test failed: broad root was accepted"
  fi

  rm -f \
    "$test_tmp/install.stdout" \
    "$test_tmp/relative.stdout" \
    "$test_tmp/relative.stderr" \
    "$test_tmp/broad.stdout" \
    "$test_tmp/broad.stderr" \
    "$installed" \
    "$test_root/scripts/run_cn_daily.sh" \
    "$test_seed"
  rmdir \
    "$test_home/Library/LaunchAgents" \
    "$test_home/Library" \
    "$test_home" \
    "$test_root/.runtime/logs" \
    "$test_root/.runtime" \
    "$test_root/scripts" \
    "$test_root" \
    "$test_tmp"
  printf 'install_cn_launchd: self-test passed\n'
}

ROOT_ARGUMENT=
SEED_ARGUMENT=
LOAD_AGENT=0

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
    --load)
      LOAD_AGENT=1
      shift
      ;;
    --self-test)
      [ "$#" -eq 1 ] || die "--self-test cannot be combined with other arguments"
      run_self_test
      exit 0
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unknown argument: $1"
      ;;
  esac
done

[ -n "$ROOT_ARGUMENT" ] || die "--root is required"
[ -n "$SEED_ARGUMENT" ] || die "--seed-json is required"
case "$ROOT_ARGUMENT" in
  /*) ;;
  *) die "--root must be an absolute path" ;;
esac
case "$SEED_ARGUMENT" in
  /*) ;;
  *) die "--seed-json must be an absolute path" ;;
esac
[ -d "$ROOT_ARGUMENT" ] || die "repository root does not exist"
[ -f "$SEED_ARGUMENT" ] || die "seed JSON does not exist"
[ -f "$TEMPLATE" ] || die "launchd template is missing"

ROOT=$(CDPATH= cd -- "$ROOT_ARGUMENT" && pwd -P)
SEED_DIR=$(CDPATH= cd -- "$(dirname -- "$SEED_ARGUMENT")" && pwd -P)
SEED_JSON="$SEED_DIR/$(basename -- "$SEED_ARGUMENT")"
HOME_PATH=$(CDPATH= cd -- "${HOME:?HOME is required}" && pwd -P)

case "$ROOT" in
  /|/Users|/home|/private|/private/tmp|/tmp|/var|"$HOME_PATH")
    die "refusing a broad repository root"
    ;;
esac
[ -x "$ROOT/scripts/run_cn_daily.sh" ] || \
  die "repository root does not contain executable scripts/run_cn_daily.sh"
case "$(basename -- "$SEED_JSON")" in
  example.json|*.example.json) die "refusing to install with an example seed" ;;
esac

validate_substitution_path() {
  value=$1
  option=$2
  case "$value" in
    *$'\n'*|*$'\r'*|*'&'*|*'<'*|*'>'*|*'|'*|*'\'*)
      die "$option contains characters that cannot be safely rendered into XML"
      ;;
  esac
}
validate_substitution_path "$ROOT" "--root"
validate_substitution_path "$SEED_JSON" "--seed-json"

LOG_DIR="$ROOT/.runtime/logs"
DESTINATION_DIR="$HOME_PATH/Library/LaunchAgents"
DESTINATION="$DESTINATION_DIR/$LABEL.plist"
mkdir -p "$LOG_DIR" "$DESTINATION_DIR"
chmod 700 "$LOG_DIR" "$DESTINATION_DIR"

TEMP_PLIST=$(mktemp "$DESTINATION_DIR/.$LABEL.XXXXXX")
cleanup() {
  rm -f "$TEMP_PLIST"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

sed \
  -e "s|__ROOT__|$ROOT|g" \
  -e "s|__SEED_JSON__|$SEED_JSON|g" \
  "$TEMPLATE" >"$TEMP_PLIST"
if grep -E '__ROOT__|__SEED_JSON__' "$TEMP_PLIST" >/dev/null; then
  die "rendered plist still contains template tokens"
fi
command -v plutil >/dev/null 2>&1 || die "plutil is required on macOS"
plutil -lint "$TEMP_PLIST" >/dev/null
chmod 600 "$TEMP_PLIST"
mv -f "$TEMP_PLIST" "$DESTINATION"
trap - EXIT HUP INT TERM

if [ "$LOAD_AGENT" -eq 1 ]; then
  DOMAIN="gui/$(id -u)"
  if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
    launchctl bootout "$DOMAIN/$LABEL"
  fi
  launchctl bootstrap "$DOMAIN" "$DESTINATION"
  printf 'install_cn_launchd: installed and loaded %s\n' "$DESTINATION"
else
  printf 'install_cn_launchd: installed %s (not loaded; pass --load to load it)\n' "$DESTINATION"
fi

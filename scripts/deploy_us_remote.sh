#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "Usage: $0 <user@host> </Users/name/ai/stock>" >&2
}

if [[ $# -ne 2 ]]; then
  usage
  exit 1
fi

remote_host="$1"
remote_root="$2"
script_directory="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
local_root="$(cd -- "$script_directory/.." && pwd)"
project_name="us_equity_research"
local_project="$local_root/$project_name"
remote_project="$remote_root/$project_name"

if [[ ! "$remote_host" =~ ^([A-Za-z0-9._-]+@)?[A-Za-z0-9.:-]+$ ]]; then
  echo "Unsafe remote host syntax: $remote_host" >&2
  exit 1
fi

if [[ ! "$remote_root" =~ ^/Users/[A-Za-z0-9._-]+/ai/stock$ ]]; then
  echo "Remote root must be an explicit /Users/<name>/ai/stock path: $remote_root" >&2
  exit 1
fi

if [[ ! -d "$local_project" ]]; then
  echo "US project not found: $local_project" >&2
  exit 1
fi

for tool in ssh rsync; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "Required command not found: $tool" >&2
    exit 1
  fi
done

backup_path="$(ssh -o BatchMode=yes "$remote_host" "bash -lc '
  set -euo pipefail
  target=\"$remote_project\"
  backup_dir=\"$remote_root/.deploy-backups\"
  mkdir -p \"$remote_root\" \"$target\"
  if [[ -n \$(find \"$target\" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null) ]]; then
    stamp=\$(date -u +%Y%m%dT%H%M%SZ)
    backup_file=\"$backup_dir/us_equity_research-\$stamp.tar.gz\"
    mkdir -p \"$backup_dir\"
    tar -czf \"\$backup_file\" \
      --exclude=.venv \
      --exclude=.pytest_cache \
      --exclude=.ruff_cache \
      --exclude=node_modules \
      --exclude=build \
      --exclude=data \
      --exclude=materials \
      --exclude=artifacts \
      --exclude=reports \
      --exclude=prompts \
      --exclude=templates \
      --exclude=credentials \
      --exclude=history \
      --exclude=journal \
      --exclude=research \
      -C \"$target\" .
    printf %s \"\$backup_file\"
  fi
'")"

rsync_excludes=(
  --exclude '.venv/'
  --exclude '.pytest_cache/'
  --exclude '.ruff_cache/'
  --exclude 'node_modules/'
  --exclude 'build/'
  --exclude '__pycache__/'
  --exclude '*.py[cod]'
  --exclude '*.egg-info/'
  --exclude 'data/'
  --exclude 'materials/'
  --exclude 'artifacts/'
  --exclude 'reports/'
  --exclude 'prompts/'
  --exclude 'templates/'
  --exclude 'credentials/'
  --exclude 'history/'
  --exclude 'journal/'
  --exclude 'research/'
  --exclude '*nmkdir/'
  --exclude '*_junk/'
  --exclude '*-junk/'
  --exclude '*_old/'
  --exclude '_legacy_copy_from_init/'
)

# Intentionally no --delete: the remote workspace may contain user-owned
# prompts, templates, historical reports, credentials, and runtime data.
rsync -azm --itemize-changes \
  -e "ssh -o BatchMode=yes" \
  "${rsync_excludes[@]}" \
  "$local_project/" \
  "$remote_host:$remote_project/"

ssh -o BatchMode=yes "$remote_host" "bash -lc '
  set -euo pipefail
  cd \"$remote_project\"
  uv sync --frozen
  uv run python -m unittest discover -s tests -v
  uv run us-equity-research doctor
  cd adapter-pkg
  npm ci
  npm test
  ./node_modules/.bin/tsc --noEmit
'"

echo "Synced $project_name without deleting remote assets."
if [[ -n "$backup_path" ]]; then
  echo "Previous remote code backup: $backup_path"
fi
echo "DSH profiles were not modified."
echo "Manual next step if desired: ssh -o BatchMode=yes '$remote_host' \"dsh plugin --profile web add '$remote_project/adapter-pkg'\""

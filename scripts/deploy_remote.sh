#!/usr/bin/env bash
set -euo pipefail

remote_host="${1:-${STOCK_REMOTE_HOST:-}}"
remote_root="${2:-${STOCK_REMOTE_ROOT:-}}"
script_directory="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
local_root="$(cd -- "$script_directory/.." && pwd)"

if [[ -z "$remote_host" || -z "$remote_root" ]]; then
  echo "Usage: $0 <user@host> </Users/name/ai/stock>" >&2
  echo "Or set STOCK_REMOTE_HOST and STOCK_REMOTE_ROOT." >&2
  exit 1
fi

if [[ ! "$remote_host" =~ ^([A-Za-z0-9._-]+@)?[A-Za-z0-9.:-]+$ ]]; then
  echo "Unsafe remote host syntax: $remote_host" >&2
  exit 1
fi

if [[ ! "$remote_root" =~ ^/Users/[A-Za-z0-9._-]+/ai/stock$ ]]; then
  echo "Remote root must be an explicit /Users/<name>/ai/stock path: $remote_root" >&2
  exit 1
fi

if [[ ! -d "$local_root/a_share_research" ]]; then
  echo "A-share project not found: $local_root/a_share_research" >&2
  exit 1
fi

backup_path="$(ssh \
  -o BatchMode=yes \
  "$remote_host" \
  "set -eu
target='$remote_root/a_share_research'
if [ -d \"\$target\" ]; then
  backup_dir='$remote_root/.deploy-backups'
  stamp=\$(date -u +%Y%m%dT%H%M%SZ)
  backup_file=\"\$backup_dir/a_share_research-\$stamp.tar.gz\"
  mkdir -p \"\$backup_dir\"
  tar -czf \"\$backup_file\" \
    --exclude='.venv' --exclude='node_modules' --exclude='data' \
    --exclude='materials' --exclude='artifacts' --exclude='reports' \
    -C \"\$target\" .
  printf '%s' \"\$backup_file\"
fi
mkdir -p \"\$target\""
)"

# Intentionally no --delete: the remote workspace contains user-owned course,
# data, prompts, and historical reports that this deploy must never remove.
rsync -azm --itemize-changes \
  -e "ssh -o BatchMode=yes" \
  --exclude '.venv/' \
  --exclude '.ruff_cache/' \
  --exclude '.pytest_cache/' \
  --exclude 'node_modules/' \
  --exclude '__pycache__/' \
  --exclude '*.py[cod]' \
  --exclude '*.egg-info/' \
  --exclude '*nmkdir/' \
  --exclude '*_junk/' \
  --exclude '*-junk/' \
  --exclude '*_old/' \
  --exclude '_legacy_copy_from_init/' \
  --exclude 'data/' \
  --exclude 'materials/' \
  --exclude 'artifacts/' \
  --exclude 'reports/' \
  --exclude 'prompts/' \
  --exclude 'templates/' \
  --exclude 'WORKFLOW.md' \
  "$local_root/a_share_research/" \
  "$remote_host:$remote_root/a_share_research/"

echo "Synced A-share code without deleting remote assets."
if [[ -n "$backup_path" ]]; then
  echo "Previous remote code backup: $backup_path"
fi
echo "Remote install: cd '$remote_root/a_share_research' && uv sync --frozen"

#!/usr/bin/env bash
set -euo pipefail

source_root="${1:-${COURSE_ARCHIVE_ROOT:-}}"
script_directory="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repository_root="$(cd -- "$script_directory/.." && pwd)"
destination_root="${2:-$repository_root/a_share_research/materials}"

if [[ -z "$source_root" ]]; then
  echo "Usage: $0 <course-archive-directory> [destination-directory]" >&2
  echo "Or set COURSE_ARCHIVE_ROOT." >&2
  exit 1
fi

if [[ ! -d "$source_root" ]]; then
  echo "Course archive not found: $source_root" >&2
  exit 1
fi

mkdir -p "$destination_root"
rsync -a \
  --include '/index.html' \
  --include '/manifest.json' \
  --include '/source/' \
  --include '/source/course_raw.json' \
  --include '/chapters/***' \
  --include '/assets/***' \
  --exclude '*' \
  "$source_root/" \
  "$destination_root/"

echo "Course materials copied locally. They remain gitignored and must not be redistributed."

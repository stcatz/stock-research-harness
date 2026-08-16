#!/usr/bin/env bash
set -euo pipefail

remote_host="${1:-${STOCK_REMOTE_HOST:-}}"
local_port="${2:-3080}"
remote_port="${3:-3080}"

if [[ -z "$remote_host" ]]; then
  echo "Usage: $0 <user@host> [local-port] [remote-port]" >&2
  echo "Or set STOCK_REMOTE_HOST." >&2
  exit 1
fi

if [[ ! "$remote_host" =~ ^([A-Za-z0-9._-]+@)?[A-Za-z0-9.:-]+$ ]]; then
  echo "Unsafe remote host syntax: $remote_host" >&2
  exit 1
fi

for port in "$local_port" "$remote_port"; do
  if [[ ! "$port" =~ ^[0-9]+$ ]] || ((port < 1 || port > 65535)); then
    echo "Port must be an integer from 1 to 65535: $port" >&2
    exit 1
  fi
done

exec ssh \
  -o ExitOnForwardFailure=yes \
  -N \
  -L "${local_port}:127.0.0.1:${remote_port}" \
  "$remote_host"

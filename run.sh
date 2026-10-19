#!/usr/bin/env bash
# Jarvis Desktop — one-command launch.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE/app"

exec python3 -m jarvis "$@"

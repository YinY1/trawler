#!/usr/bin/env bash
# Trawler Cron 调度脚本
# 用法: */3 * * * * /path/to/trawler/cron_run.sh >> /path/to/trawler/cron.log 2>&1

set -euo pipefail
cd "$(dirname "$0")"

echo "=== $(date '+%Y-%m-%d %H:%M:%S') Trawler check start ==="

uv run python run_check.py check --platform all

echo "=== $(date '+%Y-%m-%d %H:%M:%S') Trawler check end ==="

#!/usr/bin/env bash
# Trawler 容器入口
# 默认：启动 supervisord（托管 web + cron）
# 子命令 run-check：供 cron 调用，跑 trawler check 流程
set -euo pipefail

# 保证日志目录存在
mkdir -p /var/log/trawler /var/log/supervisor

case "${1:-serve}" in
    serve)
        # 默认行为：交给 CMD 启动 supervisor
        exec "$@"
        ;;
    run-check)
        cd /app
        # 直接调 run_check.py check 子命令
        exec /app/.venv/bin/python run_check.py check --platform all
        ;;
    web)
        # 单独跑 web（绕过 supervisor，调试用）
        cd /app
        exec /app/.venv/bin/uvicorn web.app:app --host 0.0.0.0 --port "${TRAWLER_WEB_PORT:-8080}"
        ;;
    *)
        exec "$@"
        ;;
esac

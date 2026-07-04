"""Trawler HTTP API — bot 友好的 JSON 接口，与 Web UI 平级挂在同一 FastAPI app。

与 ``web/`` 包的关系：
- ``web/``：browser-facing，HTML 响应、form-encoded、Session cookie + CSRF
- ``api/``：machine-facing，JSON in / JSON out、Bearer token 鉴权、CSRF 豁免

挂载在 ``/api/v1`` 前缀下（见 ``web/app.py:create_app`` 末尾的 include_router）。
中间件层 ``auth_guard`` / ``csrf_guard`` 对整个 ``/api/*`` 前缀豁免（见
``web/app.py:_PUBLIC_PREFIXES`` 与 ``csrf_guard`` 入口判断），各 API 路由通过
``api.auth.require_token`` FastAPI 依赖自行鉴权（``/health`` 除外）。

设计文档：``docs/superpowers/specs/2026-07-04-http-api-design.md``
"""

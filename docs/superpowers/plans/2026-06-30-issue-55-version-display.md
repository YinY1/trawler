# Issue #55 — 版本号展示（CLI / Web UI / health API / 告警推送）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 提供版本号，让人（CLI 用户、Web 用户）和机器（推送消息、health 接口）都能快速确认运行版本，覆盖 issue #55 全部验收项。

**Architecture:** `shared/constants.py` 作为运行期唯一真源 —— `importlib.metadata.version("trawler")` 读 `pyproject.toml` 的 `version`，`os.environ.get` 读构建期注入的 `TRAWLER_GIT_SHA` / `TRAWLER_BUILD_DATE`（本地 dev 未注入时 fallback 为 `'dev'` / `'unknown'`）。所有展示点（CLI、pipeline 启动日志、FastAPI app、health API、Web 模板、健康告警推送）统一 import 这组常量。Dockerfile + GitHub workflow 在构建期通过 `ARG` / `ENV` / `build-args` 注入 `github.sha` 与 `run_started_at`。

**Tech Stack:** Python 3.14 标准库 `importlib.metadata` / `os.environ`；Click `version_option`；FastAPI + Jinja2；Docker multi-stage + `docker/build-push-action@v6`；pytest + httpx ASGITransport。

---

## 关键约束（必读）

1. **所有 Python 命令通过 `uv run`**（禁止裸 `python3` / `pip`）。
2. **`uv run pyright` 不要加 `.` 参数**（会扫描整个仓库卡死，正确是无参数读 `pyproject.toml` 的 include）。
3. `from __future__ import annotations` 必须在每个新增/修改的 `.py` 模块顶部（已有的保持）。
4. **不改任何现有 emoji / 颜色标签 / 已有 error message 文本**（外部接口契约）。
5. **不引入新依赖**，仅用标准库 `importlib.metadata`。
6. `core/notifiers/base.py` 的 `render_markdown` 函数名保留（外部 import 接口），不改签名。
7. 每个 task 走 TDD：先写测试 → 跑确认 FAIL → 实现 → 跑确认 PASS → commit。

---

## File Structure（最终态）

**新增：**
- `web/routes/health.py` — `GET /api/health` 路由，返回 `{status, version, git_sha, build_date}`
- `tests/test_version_constants.py` — `shared/constants.py` 版本常量测试
- `tests/test_web_health.py` — health endpoint 测试（无需登录）

**修改：**
- `shared/constants.py` — 新增 `VERSION` / `GIT_SHA` / `BUILD_DATE` / `VERSION_DISPLAY` 常量
- `run_check.py` — `cli()` 加 `@click.version_option`，import `VERSION_DISPLAY`
- `core/pipeline.py:96` — 硬编码 `"▶ Trawler v0.1.0"` 改用 `VERSION_DISPLAY`
- `web/app.py` — `FastAPI(version=...)` 用常量；`_PUBLIC_PREFIXES` 加 `/api/health`；include health router
- `web/templates/base.html` — sidebar 底部加版本小字（`mt-auto` 推到底部）
- `web/templates/settings.html` — 顶部或底部加"系统信息"卡片
- `core/notifiers/base.py` — `render_markdown` 加 `health_alert` 分支，message 末尾追加版本 footer
- `Dockerfile` — `COPY . .` 之后加 `ARG GIT_SHA` / `ARG BUILD_DATE` + `ENV`
- `.github/workflows/docker-publish.yml` — build-push-action 加 `build-args`

---

## Task 1: shared/constants.py 版本常量 + 测试

**Files:**
- Modify: `shared/constants.py:1-24`
- Test: `tests/test_version_constants.py`（新建）

**说明：** 这是整个功能的基石。其他 task 全部 import 这里的常量。`importlib.metadata.version("trawler")` 从已安装的 dist metadata 读 `pyproject.toml` 的 `version`（本地 `uv pip install -e .` 后可用；测试环境已装）。ENV 未注入（本地 dev / 未配置 CI）时 fallback 为 `'dev'` / `'unknown'`，不调用 `git` 子进程（避免引入 git 依赖、避免非 git 环境报错）。

- [ ] **Step 1: 写测试（先 FAIL）**

新建 `tests/test_version_constants.py`：

```python
"""Tests for shared/constants.py version constants (issue #55)."""

from __future__ import annotations

import importlib.metadata
from unittest.mock import patch


def test_version_uses_package_metadata():
    """VERSION 等于 importlib.metadata 读到的 dist 版本。"""
    from shared.constants import VERSION

    expected = importlib.metadata.version("trawler")
    assert VERSION == expected


def test_version_fallback_when_not_installed(monkeypatch):
    """未安装 trawler dist 时 VERSION fallback '0.0.0+unknown'。

    直接跑源码（未 ``uv pip install -e .``）的场景，importlib.metadata.version
    会抛 PackageNotFoundError，constants.py 必须 catch 后 fallback。
    """
    import importlib

    from unittest.mock import patch
    from importlib.metadata import PackageNotFoundError

    with patch(
        "importlib.metadata.version",
        side_effect=PackageNotFoundError("trawler"),
    ):
        from shared import constants as constants_mod

        importlib.reload(constants_mod)
        assert constants_mod.VERSION == "0.0.0+unknown"


def test_git_sha_defaults_to_dev_when_env_missing(monkeypatch):
    """未注入 ENV 时 GIT_SHA == 'dev'。"""
    monkeypatch.delenv("TRAWLER_GIT_SHA", raising=False)
    # 重新 import 模块以触发模块级求值
    import importlib

    from shared import constants as constants_mod

    importlib.reload(constants_mod)
    assert constants_mod.GIT_SHA == "dev"


def test_git_sha_reads_env_when_present(monkeypatch):
    """注入 ENV 后 GIT_SHA 反映 ENV 值。"""
    monkeypatch.setenv("TRAWLER_GIT_SHA", "a1b2c3d")
    import importlib

    from shared import constants as constants_mod

    importlib.reload(constants_mod)
    assert constants_mod.GIT_SHA == "a1b2c3d"


def test_build_date_defaults_to_unknown_when_env_missing(monkeypatch):
    monkeypatch.delenv("TRAWLER_BUILD_DATE", raising=False)
    import importlib

    from shared import constants as constants_mod

    importlib.reload(constants_mod)
    assert constants_mod.BUILD_DATE == "unknown"


def test_build_date_reads_env_when_present(monkeypatch):
    monkeypatch.setenv("TRAWLER_BUILD_DATE", "2026-06-30T14:29:00Z")
    import importlib

    from shared import constants as constants_mod

    importlib.reload(constants_mod)
    assert constants_mod.BUILD_DATE == "2026-06-30T14:29:00Z"


def test_version_display_format_contains_all_parts(monkeypatch):
    """VERSION_DISPLAY 形如 `<version>+<git_sha> (<build_date>)`。"""
    monkeypatch.setenv("TRAWLER_GIT_SHA", "a1b2c3d")
    monkeypatch.setenv("TRAWLER_BUILD_DATE", "2026-06-30T14:29:00Z")
    import importlib

    from shared import constants as constants_mod

    importlib.reload(constants_mod)
    vd = constants_mod.VERSION_DISPLAY
    # 包含 dist version、git sha、build date 三段
    assert "a1b2c3d" in vd
    assert "2026-06-30T14:29:00Z" in vd
    assert "+" in vd
    assert "(" in vd and ")" in vd


def test_version_display_dev_fallback(monkeypatch):
    """未注入 ENV 时 VERSION_DISPLAY 仍可读，含 'dev' 和 'unknown'。"""
    monkeypatch.delenv("TRAWLER_GIT_SHA", raising=False)
    monkeypatch.delenv("TRAWLER_BUILD_DATE", raising=False)
    import importlib

    from shared import constants as constants_mod

    importlib.reload(constants_mod)
    vd = constants_mod.VERSION_DISPLAY
    assert "dev" in vd
    assert "unknown" in vd
```

- [ ] **Step 2: 跑测试确认 FAIL**

```bash
uv run pytest tests/test_version_constants.py -v
```

Expected: `ImportError: cannot import name 'VERSION' from 'shared.constants'`（或类似），全部 FAIL。

- [ ] **Step 3: 实现 shared/constants.py**

将 `shared/constants.py` 整体替换为（保留所有原有常量，仅在顶部新增版本块）：

```python
"""Trawler 全局常量"""

from __future__ import annotations

import os
from importlib.metadata import PackageNotFoundError, version as _dist_version

# ═══════════════════════════════════════════════════════════
# 版本信息（issue #55）
#
# - VERSION: dist metadata 读取（pyproject.toml [project].version），构建期不变
#   未安装（直接跑源码）时 fallback '0.0.0+unknown'，避免 PackageNotFoundError
# - GIT_SHA / BUILD_DATE: Docker 构建 ARG 注入 ENV，本地 dev fallback 'dev'/'unknown'
#   不调用 git 子进程，避免引入 git 依赖 + 非 git 环境报错
# - VERSION_DISPLAY: 统一展示字符串，形如 `0.1.0+a1b2c3d (2026-06-30T14:29:00Z)`
# ═══════════════════════════════════════════════════════════
try:
    VERSION: str = _dist_version("trawler")
except PackageNotFoundError:
    VERSION = "0.0.0+unknown"
GIT_SHA: str = os.environ.get("TRAWLER_GIT_SHA", "dev")
BUILD_DATE: str = os.environ.get("TRAWLER_BUILD_DATE", "unknown")
VERSION_DISPLAY: str = f"{VERSION}+{GIT_SHA} ({BUILD_DATE})"

# 超时（秒）
DOWNLOAD_TIMEOUT = 600  # yt-dlp 下载超时
LLM_API_TIMEOUT = 60  # OpenAI 兼容 API 超时
GOTIFY_TIMEOUT = 10  # Gotify 推送超时
RSS_REQUEST_TIMEOUT = 15  # RSS 请求超时
XHS_REQUEST_TIMEOUT = 15  # 小红书 API 请求超时
XHS_DOWNLOAD_TIMEOUT = 120  # 小红书文件下载超时

WEIBO_REQUEST_TIMEOUT = 15  # 微博 API 请求超时
WEIBO_DOWNLOAD_TIMEOUT = 120  # 微博文件下载超时
WEIBO_POLL_TIMEOUT = 240  # 二维码轮询超时（秒）

# 重试
GOTIFY_MAX_RETRIES = 3  # Gotify 最大重试次数

# 评论
MAX_COMMENT_HIGHLIGHTS = 5  # 最大评论亮点数量

# AI 摘要重试上限（连续失败 N 次后 mark_error 让 cron 永久跳过）
MAX_SUMMARY_RETRIES = 5
```

- [ ] **Step 4: 跑测试确认 PASS**

```bash
uv run pytest tests/test_version_constants.py -v
```

Expected: 全部 8 个 test PASS。

- [ ] **Step 5: 跑 lint + type check**

```bash
uv run ruff format shared/constants.py
uv run ruff check shared/constants.py tests/test_version_constants.py
uv run pyright
```

Expected: ruff 0 issues；pyright 0 errors（`shared/constants.py` 在 include 内）。

- [ ] **Step 6: Commit**

```bash
git add shared/constants.py tests/test_version_constants.py
git commit -m "feat(issue-55): shared/constants.py 版本常量 VERSION/GIT_SHA/BUILD_DATE/VERSION_DISPLAY

- importlib.metadata.version('trawler') 作为运行期真源
- ENV TRAWLER_GIT_SHA / TRAWLER_BUILD_DATE fallback 'dev'/'unknown'
- 不引入 git 子进程依赖，标准库 only

Refs: #55"
```

---

## Task 2: CLI --version + pipeline 启动日志替换

**Files:**
- Modify: `run_check.py:84-87`（`cli()` group）
- Modify: `core/pipeline.py:96`（启动日志）
- Test: `tests/test_run_check_cli.py`（追加 test）

**说明：** Click 的 `@click.version_option(version=..., message=...)` 自动给 group 加 `--version` flag，输 `message` 后 exit 0。message 用 `VERSION_DISPLAY`。pipeline.py 启动日志硬编码 `"▶ Trawler v0.1.0"` 替换为 `f"▶ Trawler {VERSION_DISPLAY}"` —— 注意保留 `▶ ` emoji 前缀和首字母大写 `Trawler`（外部日志分析依赖）。

- [ ] **Step 1: 写测试（追加到现有 `tests/test_run_check_cli.py` 末尾）**

在 `tests/test_run_check_cli.py` 文件末尾追加：

```python


# ── --version (issue #55) ────────────────────────────────────────


def test_cli_version_option_outputs_version_display() -> None:
    """trawler --version 输出 VERSION_DISPLAY 字符串。"""
    runner = CliRunner()
    result = runner.invoke(cli, ["--version"])
    assert result.exit_code == 0
    # VERSION_DISPLAY 形如 `0.1.0+dev (unknown)` 或 `0.1.0+a1b2c3d (...)`
    from shared.constants import VERSION_DISPLAY

    assert VERSION_DISPLAY in result.output
    assert "Trawler" in result.output


def test_cli_version_option_short_flag_v() -> None:
    """-V 短 flag 也应工作（Click version_option 默认 -V/--version）。"""
    runner = CliRunner()
    result = runner.invoke(cli, ["-V"])
    assert result.exit_code == 0
    from shared.constants import VERSION

    assert VERSION in result.output
```

- [ ] **Step 2: 跑测试确认 FAIL**

```bash
uv run pytest tests/test_run_check_cli.py::test_cli_version_option_outputs_version_display tests/test_run_check_cli.py::test_cli_version_option_short_flag_v -v
```

Expected: 2 个 test FAIL（`--version` 未实现时 Click 报 `No such option: --version`，exit_code != 0）。

- [ ] **Step 3: 修改 run_check.py — 加 version_option**

`run_check.py:84-87` 当前是：

```python
@click.group()
def cli() -> None:
    """Trawler - 多平台创作者内容追更自动化工作流"""
    pass
```

替换为（保留 docstring 和 pass）：

```python
@click.group()
@click.version_option(version=VERSION_DISPLAY, message="Trawler %(version)s")
def cli() -> None:
    """Trawler - 多平台创作者内容追更自动化工作流"""
    pass
```

`from shared.constants import VERSION_DISPLAY` 加到 `run_check.py` 顶部 import 区（约 line 26 附近，按字母序 ruff I 自动整理）。

- [ ] **Step 4: 修改 core/pipeline.py:96 启动日志**

`core/pipeline.py:96` 当前是：

```python
    logger.info("▶ Trawler v0.1.0")
```

替换为：

```python
    logger.info(f"▶ Trawler {VERSION_DISPLAY}")
```

并在 `core/pipeline.py` 顶部 import 区（约 line 21-22 `from shared.config import Config` / `from shared.protocols import Phase` 附近）加：

```python
from shared.constants import VERSION_DISPLAY
```

- [ ] **Step 5: 跑测试确认 PASS**

```bash
uv run pytest tests/test_run_check_cli.py::test_cli_version_option_outputs_version_display tests/test_run_check_cli.py::test_cli_version_option_short_flag_v -v
```

Expected: 2 个 test PASS。

- [ ] **Step 6: 跑 pipeline 现有测试确保未回归**

```bash
uv run pytest tests/test_pipeline_e2e.py tests/test_pipeline_concurrent.py -v
```

Expected: 全部 PASS（仅日志文本变化，不影响断言）。

- [ ] **Step 7: lint + type check**

```bash
uv run ruff check run_check.py core/pipeline.py tests/test_run_check_cli.py
uv run pyright
```

Expected: 0 issues / 0 errors。

- [ ] **Step 8: Commit**

```bash
git add run_check.py core/pipeline.py tests/test_run_check_cli.py
git commit -m "feat(issue-55): CLI --version + pipeline 启动日志用 VERSION_DISPLAY

- run_check.cli() 加 @click.version_option，输出 'Trawler <VERSION_DISPLAY>'
- core/pipeline.py:96 硬编码 'v0.1.0' 替换为 VERSION_DISPLAY

Refs: #55"
```

---

## Task 3: GET /api/health 接口 + FastAPI version 常量化

**Files:**
- Create: `web/routes/health.py`
- Modify: `web/app.py:23-24`（`_PUBLIC_PREFIXES`）、`web/app.py:133`（FastAPI version）、`web/app.py:235-256`（router 注册）
- Test: `tests/test_web_health.py`（新建）

**说明：**
- `GET /api/health` 必须无需登录（监控探针/告警系统调用，不带 session cookie）。所以 `/api/health` 要加进 `_PUBLIC_PREFIXES` —— 注意 `_PUBLIC_PREFIXES` 用 `startswith` 匹配，所以 `"/api/health"` 精确前缀即可；如担心未来加更多 `/api/*` 公开接口，可用 `"/api/"` 前缀，但保守起见先精确 `"/api/health"`。
- 返回 `{status: "ok", version, git_sha, build_date}`，HTTP 200。
- `web/app.py:133` `FastAPI(version="0.1.0", ...)` 改用 `VERSION` 常量（dist version，不含 sha，符合 OpenAPI schema 习惯）。

- [ ] **Step 1: 写测试（先 FAIL）**

新建 `tests/test_web_health.py`：

```python
"""Tests for GET /api/health (issue #55).

无需登录，返回 {status, version, git_sha, build_date}。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from web.app import create_app
from web.auth import set_password

PASSWORD = "test12345"


@pytest.fixture
async def health_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncClient:
    """无需登录的 client —— /api/health 必须在 setup/login guard 之外。

    即便如此，仍需 set_password 让 is_setup_complete() 返回 True，
    否则 auth_guard 会 302 到 /setup。
    """
    monkeypatch.setattr("web.auth.AUTH_TOML_PATH", tmp_path / "auth.toml")
    set_password(PASSWORD)
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


class TestHealth:
    async def test_health_returns_200_without_login(self, health_client: AsyncClient) -> None:
        resp = await health_client.get("/api/health")
        assert resp.status_code == 200

    async def test_health_response_shape(self, health_client: AsyncClient) -> None:
        resp = await health_client.get("/api/health")
        data = resp.json()
        assert data["status"] == "ok"
        # version 字段存在且为非空字符串
        assert isinstance(data["version"], str) and data["version"]
        assert "git_sha" in data
        assert "build_date" in data

    async def test_health_version_matches_constant(self, health_client: AsyncClient) -> None:
        from shared.constants import VERSION

        resp = await health_client.get("/api/health")
        assert resp.json()["version"] == VERSION

    async def test_health_git_sha_matches_constant(self, health_client: AsyncClient) -> None:
        from shared.constants import GIT_SHA

        resp = await health_client.get("/api/health")
        assert resp.json()["git_sha"] == GIT_SHA
```

- [ ] **Step 2: 跑测试确认 FAIL**

```bash
uv run pytest tests/test_web_health.py -v
```

Expected: 4 个 test FAIL —— `/api/health` 未注册，FastAPI 返回 404 或被 auth_guard 302 重定向。

- [ ] **Step 3: 创建 web/routes/health.py**

新建 `web/routes/health.py`：

```python
"""Health check endpoint (issue #55).

无需登录，供监控/告警系统探活与版本核对。
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from shared.constants import BUILD_DATE, GIT_SHA, VERSION

router = APIRouter()


@router.get("/api/health")
async def health() -> JSONResponse:
    """返回服务状态 + 版本信息。

    Response: ``{"status": "ok", "version": str, "git_sha": str, "build_date": str}``
    """
    return JSONResponse(
        status_code=200,
        content={
            "status": "ok",
            "version": VERSION,
            "git_sha": GIT_SHA,
            "build_date": BUILD_DATE,
        },
    )
```

- [ ] **Step 4: 修改 web/app.py — 放开 /api/health 公开访问 + 注册 router + version 常量化**

**改动 1：`web/app.py:23-24`** `_PUBLIC_PATHS` / `_PUBLIC_PREFIXES` 当前是：

```python
_PUBLIC_PATHS = {"/login", "/logout", "/setup"}
_PUBLIC_PREFIXES = ("/static",)
```

改为（在 `_PUBLIC_PREFIXES` 加 `"/api/health"`，注意保留尾部逗号便于后续扩展）：

```python
_PUBLIC_PATHS = {"/login", "/logout", "/setup"}
_PUBLIC_PREFIXES = ("/static", "/api/health")
```

**改动 2：`web/app.py:133`** `create_app` 内当前是：

```python
    app = FastAPI(title="Trawler Web UI", version="0.1.0", lifespan=lifespan)
```

改为（import `VERSION` 常量；展示在顶部 import 区约 line 10 附近加 `from shared.constants import VERSION`）：

```python
    app = FastAPI(title="Trawler Web UI", version=VERSION, lifespan=lifespan)
```

**改动 3：`web/app.py:235-256`** router 注册区。当前在 line 236-244 是 9 个 `from web.routes.X import router as X_router`，line 246-254 是 9 个 `app.include_router(...)`。

在 import 区（line 244 `from web.routes.web_auth import router as web_auth_router` 之后）加一行：

```python
    from web.routes.health import router as health_router
```

> **import 顺序提示：** import 顺序交给 ruff I 自动整理，手动插入位置不必精确。`include_router` 顺序无要求。

在 include 区（line 254 `app.include_router(messages_router)` 之后、`return app` 之前）加一行：

```python
    app.include_router(health_router)
```

并在 `web/app.py` 顶部 import 区（约 line 10 `from fastapi import FastAPI, Request` 附近，按 ruff I 排序）加：

```python
from shared.constants import VERSION
```

- [ ] **Step 5: 跑测试确认 PASS**

```bash
uv run pytest tests/test_web_health.py -v
```

Expected: 4 个 test PASS。

- [ ] **Step 6: 跑现有 web 测试确保未回归**

```bash
uv run pytest tests/test_web_settings.py tests/test_web_auth_guard.py tests/test_web_dashboard.py -v
```

Expected: 全部 PASS（auth_guard 不受 `_PUBLIC_PREFIXES` 新增影响，仅多放行 `/api/health`）。

- [ ] **Step 7: lint + type check**

```bash
uv run ruff check web/routes/health.py web/app.py tests/test_web_health.py
uv run pyright
```

Expected: 0 issues（ruff 必须通过）。pyright 此处对 `web/` 是 no-op。

> **⚠️ pyright 覆盖说明：** `web/` 不在 pyright include，pyright 不会检查这些文件，仅靠 ruff + 测试覆盖类型正确性。

- [ ] **Step 8: Commit**

```bash
git add web/routes/health.py web/app.py tests/test_web_health.py
git commit -m "feat(issue-55): GET /api/health 无需认证返回版本信息

- 新增 web/routes/health.py，返回 {status, version, git_sha, build_date}
- _PUBLIC_PREFIXES 加 /api/health，绕过 auth_guard
- FastAPI(version=...) 用 shared.constants.VERSION 替换硬编码

Refs: #55"
```

---

## Task 4: Dockerfile + GitHub workflow 注入 GIT_SHA / BUILD_DATE

**Files:**
- Modify: `Dockerfile:57-62`（`COPY . .` 之后）
- Modify: `.github/workflows/docker-publish.yml:46-52`（build-push-action with 块）

**说明：**
- Dockerfile 在 `COPY . .`（line 58）**之前**注入 `ARG` + `ENV`，再 `RUN uv sync`。位置选择：在源码 COPY 之前注入，避免 build-args 变化触发源码 layer 失效导致 uv sync 重复执行；ENV 必须在运行期可读，放在运行 stage 满足要求。
- workflow 用 `github.sha`（完整 40 字符 commit SHA）和 `github.run_started_at`（ISO 8601 UTC 时间戳）作为 build-args。
- **此 task 无法单元测试**（需要构建镜像 + GHCR 推送），验证方式：① Dockerfile 语法静态检查 ② workflow YAML lint ③ 留待 PR 合入后人工跑一次 master push 触发 CI 验证。Plan 给出本地 docker build 验证命令（若环境有 docker）。

- [ ] **Step 1: 修改 Dockerfile**

`Dockerfile:57-62` 当前是：

```dockerfile
# ── 项目源码 ──
COPY . .
RUN uv sync --frozen --extra web --extra xhs

# ── 拷入构建期生成的 CSS（覆盖任何残留）──
COPY --from=css-builder /build/main.css web/static/css/main.css
```

替换为（在 `COPY . .` **之前**插入 ARG/ENV 块 —— 这样 build-args 变化不会触发源码 layer 失效，避免 uv sync 重复执行）：

```dockerfile
# ── 版本信息注入（issue #55）：构建期 ARG → ENV，运行期 os.environ 读取 ──
# 本地构建：docker build . （未传 ARG，ENV 为空 → shared/constants.py fallback 'dev'/'unknown'）
# CI 构建：workflow 传 build-args GIT_SHA/BUILD_DATE
# 在源码 COPY 之前注入，避免 build-args 变化触发源码 layer 失效导致 uv sync 重复执行
ARG GIT_SHA=""
ARG BUILD_DATE=""
ENV TRAWLER_GIT_SHA=${GIT_SHA}
ENV TRAWLER_BUILD_DATE=${BUILD_DATE}

# ── 项目源码 ──
COPY . .
RUN uv sync --frozen --extra web --extra xhs

# ── 拷入构建期生成的 CSS（覆盖任何残留）──
COPY --from=css-builder /build/main.css web/static/css/main.css
```

- [ ] **Step 2: 修改 .github/workflows/docker-publish.yml**

`.github/workflows/docker-publish.yml:46-52` 当前是：

```yaml
      - name: Build and push
        uses: docker/build-push-action@v6
        with:
          context: .
          push: true
          tags: ${{ steps.meta.outputs.tags }}
          labels: ${{ steps.meta.outputs.labels }}
```

替换为（加 `build-args` 字段；注意 YAML 多行字符串 `|` 语法）：

```yaml
      - name: Build and push
        uses: docker/build-push-action@v6
        with:
          context: .
          push: true
          tags: ${{ steps.meta.outputs.tags }}
          labels: ${{ steps.meta.outputs.labels }}
          build-args: |
            GIT_SHA=${{ github.sha }}
            BUILD_DATE=${{ github.run_started_at }}
```

- [ ] **Step 3: 本地静态验证（无需 docker daemon 也能跑的检查）**

YAML 语法验证：

```bash
uv run python -c "import yaml; yaml.safe_load(open('.github/workflows/docker-publish.yml')); print('YAML OK')"
```

Expected: 输出 `YAML OK`。

Dockerfile ARG/ENV 配对检查（grep 确认）：

```bash
grep -nE '^(ARG|ENV) (TRAWLER_|GIT_SHA|BUILD_DATE)' Dockerfile
```

Expected: 看到 4 行（2 ARG + 2 ENV），其中 ENV key 是 `TRAWLER_GIT_SHA` / `TRAWLER_BUILD_DATE`（与 `shared/constants.py` 的 `os.environ.get` key 一致）。

workflow build-args 注入检查：

```bash
grep -A2 'build-args' .github/workflows/docker-publish.yml
```

Expected: 看到 `GIT_SHA=${{ github.sha }}` 和 `BUILD_DATE=${{ github.run_started_at }}`。

- [ ] **Step 4: 若本地有 docker，做端到端验证（可选）**

```bash
docker build -t trawler-version-test --build-arg GIT_SHA=test123 --build-arg BUILD_DATE=2026-06-30T00:00:00Z .
docker run --rm trawler-version-test python -c "from shared.constants import VERSION_DISPLAY; print(VERSION_DISPLAY)"
```

Expected: 输出形如 `0.1.0+test123 (2026-06-30T00:00:00Z)`。

若环境无 docker，跳过此步，注释 "待 CI 验证"。

- [ ] **Step 5: Commit**

```bash
git add Dockerfile .github/workflows/docker-publish.yml
git commit -m "feat(issue-55): Dockerfile ARG/ENV + workflow build-args 注入 GIT_SHA/BUILD_DATE

- Dockerfile: COPY . . 前加 ARG GIT_SHA/BUILD_DATE + ENV TRAWLER_GIT_SHA/TRAWLER_BUILD_DATE
- workflow: build-push-action 加 build-args 注入 github.sha / github.run_started_at
- 本地 dev 未传 ARG 时 ENV 为空 → constants.py fallback 'dev'/'unknown'

Refs: #55"
```

---

## Task 5: Web UI 模板展示版本号（sidebar 底部 + settings 系统信息）

**Files:**
- Modify: `web/templates/base.html:21-69`（sidebar 结构，加底部版本块）
- Modify: `web/templates/settings.html`（加"系统信息"卡片）
- Test: `tests/test_web_settings.py`（追加模板渲染断言）

**说明：**
- 前端模板改动难以单元测试交互行为，但可以用 fastapi.testclient 验证**模板渲染包含版本字符串**（server-side render，足够保证上下文传到了模板）。
- sidebar 当前结构是 `<aside class="...flex flex-col...">` 内含 header + `<nav>`。要让版本块贴底，需要让 `<nav>` 不占满，加一个 `<div class="mt-auto">` 放版本小字。`mt-auto` 在 flex column 容器里会把元素推到底部。
- settings 页加一个"系统信息"卡片，展示 version / git_sha / build_date 三段。位置：放在 "AI 分析" form 之后、"sticky save bar" 之前（line 120 后、line 122 前）。
- 模板渲染需要把 `VERSION_DISPLAY` / `VERSION` / `GIT_SHA` / `BUILD_DATE` 传入 context。settings 路由的 `settings_page` 已经传了 `{"active_nav": "settings", "config": config}`，扩展为加这 4 个常量。base.html 是全局模板，sidebar 渲染需要版本信息 —— 最佳位置是**通过 Jinja2 模板直接 import 常量**（`{% from %}` 不支持 import 模块；用 Jinja2 globals 或在 `create_app` 里注入）。

**实现策略：** 在 `web/app.py` 的 `TEMPLATES.env.globals` 注入 `VERSION_DISPLAY`，这样所有模板（base.html / settings.html）都能直接 `{{ VERSION_DISPLAY }}`，无需每个路由手动传 context。这是最小侵入方案。

- [ ] **Step 1: 写测试（先 FAIL）—— 模板渲染含版本字符串**

> **⚠️ 重要变更：** 原 plan 设计的独立 `tests/test_web_base_template.py` 测试方案**已删除** —— 该方案试图 patch dashboard 路由的 `load_config`，但 dashboard 路由还依赖 `MessageStore`（读真实文件）和 `list_subscriptions`，单纯 patch `load_config` 无法让路由跑通。sidebar 渲染已由 `TestSettingsVersionDisplay` 覆盖（settings 页同样继承 `base.html`，sidebar 也会被渲染），下面在已有测试中追加一条 `VERSION_DISPLAY` 断言即可。

在 `tests/test_web_settings.py` 末尾追加：

```python


class TestSettingsVersionDisplay:
    """issue #55: settings 页含 '系统信息' 卡片，展示版本字段。"""

    @patch("web.routes.settings.load_config", new_callable=AsyncMock)
    async def test_settings_page_contains_system_info_card(self, mock_load, client: AsyncClient) -> None:
        from shared.config import Config
        from shared.constants import VERSION_DISPLAY

        mock_load.return_value = Config()
        resp = await client.get("/settings")
        assert resp.status_code == 200
        body = resp.text
        assert "系统信息" in body
        # settings 页继承 base.html，sidebar 也会渲染；VERSION_DISPLAY 应出现
        assert VERSION_DISPLAY in resp.text

    @patch("web.routes.settings.load_config", new_callable=AsyncMock)
    async def test_settings_page_contains_version_display(self, mock_load, client: AsyncClient) -> None:
        from shared.config import Config
        from shared.constants import VERSION_DISPLAY

        mock_load.return_value = Config()
        resp = await client.get("/settings")
        assert resp.status_code == 200
        # VERSION_DISPLAY 形如 `0.1.0+dev (unknown)`，HTML 渲染后应原样出现
        assert VERSION_DISPLAY in resp.text
```

> （原独立 `tests/test_web_base_template.py` 测试文件已删除，不再保留。）

- [ ] **Step 2: 跑测试确认 FAIL**

```bash
uv run pytest tests/test_web_settings.py::TestSettingsVersionDisplay -v
```

Expected: 2 个 test FAIL（模板里没有 "系统信息" 和 VERSION_DISPLAY 字符串）。

- [ ] **Step 3: 修改 web/app.py — 注入 Jinja2 globals**

在 `web/app.py` `TEMPLATES.env.filters[...]` 三行（line 58/74/95）之后、`lifespan` 函数（line 98）之前，加 globals 注入：

```python
# ── issue #55: 注入版本常量到所有模板 ────────────────────────────
# 让 base.html sidebar / settings.html 等模板可直接 {{ VERSION_DISPLAY }}，
# 无需每个路由手动传 context。
# 顶部 import 区已 import 了 BUILD_DATE/GIT_SHA/VERSION/VERSION_DISPLAY，
# 此处仅做 globals 赋值，不再重复 import。
TEMPLATES.env.globals["VERSION"] = VERSION
TEMPLATES.env.globals["GIT_SHA"] = GIT_SHA
TEMPLATES.env.globals["BUILD_DATE"] = BUILD_DATE
TEMPLATES.env.globals["VERSION_DISPLAY"] = VERSION_DISPLAY
```

**⚠️ ruff I 提示：** 顶部 import 区已有 `from shared.config import Config`（在 routes 内部），`shared.constants` 的 import 按字母序应在 `shared.config` 前。但此处为模块顶层 import 区，建议放在 `web/app.py` 顶部 line 10 附近与其他第三方 import 一起：

实际执行时：
- 把 `from shared.constants import BUILD_DATE, GIT_SHA, VERSION, VERSION_DISPLAY` 加到 `web/app.py` 顶部 import 区（约 line 16 `logger = ...` 之前）
- 把 `TEMPLATES.env.globals[...]` 4 行加到 line 95（`TEMPLATES.env.filters["phase_label"] = _phase_label`）之后

- [ ] **Step 4: 修改 web/templates/base.html — sidebar 底部版本块**

`web/templates/base.html:21-69` sidebar 当前结构：

```html
  <aside id="sidebar" class="fixed left-0 top-0 h-full w-60 ... flex flex-col ...">
    <div class="px-5 py-6 flex items-center gap-2">...</div>
    <nav class="flex flex-col gap-1 px-3 mt-2">
      ...各导航项...
      {% if request.session.get("logged_in") %}
      ...退出登录按钮...
      {% endif %}
    </nav>
  </aside>
```

把 `</nav>` 之后、`</aside>` 之前（line 68-69 之间）加版本块。`mt-auto` 把它推到底部（因为 `<aside>` 是 `flex flex-col`）：

在 `</nav>` 之后插入：

```html
    <div class="mt-auto px-5 py-4 border-t border-[var(--sidebar-border)] text-[11px] text-[var(--text-tertiary)]">
      <div class="font-mono truncate" title="{{ VERSION_DISPLAY }}">{{ VERSION_DISPLAY }}</div>
    </div>
```

- [ ] **Step 5: 修改 web/templates/settings.html — 加"系统信息"卡片**

在 `web/templates/settings.html` 的 "AI 分析" form 闭合 `</form>`（line 120）之后、"sticky save bar" `<div id="save-bar"...>` （line 122）之前，插入新卡片：

在 line 120 `</form>` 之后、line 122 之前插入：

```html

<!-- Card: 系统信息 (issue #55) -->
<div class="bg-[var(--card-bg)] backdrop-blur-[12px] rounded-[14px] p-5 shadow-card border border-[var(--card-border)] mb-4">
  <div class="flex items-center gap-2 mb-4">
    <span class="w-7 h-7 rounded-[8px] bg-apple-blue/10 text-apple-blue inline-flex items-center justify-center">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M12 16v-4M12 8h.01"/></svg>
    </span>
    <h2 class="text-base font-semibold">系统信息</h2>
  </div>
  <p class="text-xs text-[var(--text-secondary)] mb-4">运行版本，用于核对部署是否更新到目标 commit</p>
  <div class="flex flex-col gap-2 text-sm">
    <div class="flex items-center justify-between py-1.5 border-b border-gray-100 dark:border-gray-800">
      <span class="text-[var(--text-secondary)]">版本号</span>
      <span class="font-mono text-xs">{{ VERSION }}</span>
    </div>
    <div class="flex items-center justify-between py-1.5 border-b border-gray-100 dark:border-gray-800">
      <span class="text-[var(--text-secondary)]">Git SHA</span>
      <span class="font-mono text-xs">{{ GIT_SHA }}</span>
    </div>
    <div class="flex items-center justify-between py-1.5">
      <span class="text-[var(--text-secondary)]">构建时间</span>
      <span class="font-mono text-xs">{{ BUILD_DATE }}</span>
    </div>
  </div>
</div>

```

- [ ] **Step 6: 跑测试确认 PASS**

```bash
uv run pytest tests/test_web_settings.py::TestSettingsVersionDisplay -v
```

Expected: 2 个 test PASS。

- [ ] **Step 7: 跑全部 web 测试确保未回归**

```bash
uv run pytest tests/test_web_*.py -v
```

Expected: 全部 PASS（globals 注入不影响其他模板）。

- [ ] **Step 8: lint + type check**

```bash
uv run ruff check web/app.py tests/test_web_settings.py
uv run pyright
```

Expected: 0 issues / 0 errors。

> **⚠️ pyright 覆盖说明：** `web/` 不在 pyright include，pyright 不会检查这些文件，仅靠 ruff + 测试覆盖类型正确性。

- [ ] **Step 9: Commit**

```bash
git add web/app.py web/templates/base.html web/templates/settings.html tests/test_web_settings.py
git commit -m "feat(issue-55): Web UI 展示版本号（sidebar 底部 + settings 系统信息卡片）

- web/app.py: TEMPLATES.env.globals 注入 VERSION/GIT_SHA/BUILD_DATE/VERSION_DISPLAY
- base.html: sidebar 底部 mt-auto 版本块（font-mono truncate）
- settings.html: 新增 '系统信息' 卡片，3 行展示 version/git_sha/build_date
- 测试: 模板渲染断言含 VERSION_DISPLAY 字符串

Refs: #55"
```

---

## Task 6: 健康告警推送加版本 footer

**Files:**
- Modify: `core/notifiers/base.py:27-59`（`render_markdown` 函数）
- Test: `tests/test_notifier_base.py`（追加 health_alert 分支测试）

**说明：**
- `NotificationContent.type` 现有 `"content"` / `"dynamic"` 两个值。`run_check.py:602-609` 健康告警已经传 `type="health_alert"`（见现有代码），但 `render_markdown` 没有对应分支，会走默认 content 模板 —— 这导致 health_alert 渲染输出含 `"关键词: 无"`、`"详情:"` 等无意义字段。
- 本 task 在 `render_markdown` 加 `health_alert` 分支：使用简化模板（title + summary），并在 message 末尾追加 `(trawler@<git_sha>)` 版本 footer。
- **决策 5 严格限定**：只在 `health_alert` 分支加版本 footer，`content` / `dynamic` 分支**不动**（内容推送给订阅者，加版本号是噪音）。
- footer 格式：`(trawler@<GIT_SHA>)` —— 用 GIT_SHA（短标识）而非 VERSION_DISPLAY（含 build_date 太长，告警推送留 sha 即可定位 commit）。
- 健康告警的 title 保留 `content.title` 原样（run_check.py 传 `'Trawler 检查失败'`，无 emoji），`render_markdown` 不负责加 emoji。

- [ ] **Step 1: 写测试（追加到 `tests/test_notifier_base.py` 末尾）**

在 `tests/test_notifier_base.py` 文件末尾追加：

```python


# ═══════════════════════════════════════════════════════════
# Task — health_alert 分支 + 版本 footer (issue #55)
# ═══════════════════════════════════════════════════════════


def test_render_health_alert_has_version_footer():
    """health_alert 分支应在 message 末尾追加 (trawler@<GIT_SHA>)。"""
    from shared.constants import GIT_SHA

    c = NotificationContent(
        platform="system",
        source_id="health",
        title="Trawler 检查失败",
        author="Trawler",
        summary="check 命令执行失败: KeyError",
        type="health_alert",
    )
    title, msg = render_markdown(c)
    # title 保留原样（emoji 由调用方在 content.title 里给）
    assert title == "Trawler 检查失败"
    # summary 出现在 message 中
    assert "check 命令执行失败: KeyError" in msg
    # 版本 footer 在末尾
    assert f"(trawler@{GIT_SHA})" in msg


def test_render_health_alert_no_keywords_section():
    """health_alert message 结构验证：summary 在首行，footer 在末尾。

    用结构性断言而非 '关键词' / '详情' 子串否定（health_alert 的 summary
    可能含任意文本，例如 ``KeyError('关键词')``，子串否定会假阳性）。
    """
    from shared.constants import GIT_SHA

    c = NotificationContent(
        platform="system",
        source_id="health",
        title="t",
        author="a",
        summary="s",
        type="health_alert",
    )
    _, msg = render_markdown(c)
    # summary 出现在 message 第一行
    assert msg.startswith("s")
    # 版本 footer 在 message 末尾
    assert msg.endswith(f"(trawler@{GIT_SHA})")


def test_render_content_type_does_not_get_version_footer():
    """content 分支不应含版本 footer（决策 5：内容推送不加版本号）。"""
    c = NotificationContent(
        platform="bili",
        source_id="BV1xx",
        title="t",
        author="UP",
        summary="s",
        type="content",
    )
    _, msg = render_markdown(c)
    assert "trawler@" not in msg


def test_render_dynamic_type_does_not_get_version_footer():
    """dynamic 分支不应含版本 footer。"""
    c = NotificationContent(
        platform="bili",
        source_id="dyn1",
        title="t",
        author="UP",
        summary="s",
        type="dynamic",
    )
    _, msg = render_markdown(c)
    assert "trawler@" not in msg
```

- [ ] **Step 2: 跑测试确认 FAIL**

```bash
uv run pytest tests/test_notifier_base.py::test_render_health_alert_has_version_footer tests/test_notifier_base.py::test_render_health_alert_no_keywords_section -v
```

Expected: 2 个 test FAIL（当前 `health_alert` 走默认 content 分支，含 "关键词:"/"详情:"，无版本 footer）。

`test_render_content_type_does_not_get_version_footer` / `test_render_dynamic_type_does_not_get_version_footer` 应该已经 PASS（验证当前行为不被破坏）。

- [ ] **Step 3: 修改 core/notifiers/base.py — 加 health_alert 分支**

`core/notifiers/base.py` 当前完整内容（line 1-59）：

```python
"""通知内容渲染层 — 跨 Provider 共享的纯文本 (plain text) 渲染。"""

from __future__ import annotations

from shared.protocols import NotificationContent

# 各 platform 的 title emoji 和"作者"标签
_PLATFORM_STYLE: dict[str, dict[str, str]] = {
    "bili": {"emoji": "📹", "author_label": "UP主"},
    "xhs": {"emoji": "📕", "author_label": "作者"},
    "weibo": {"emoji": "🐦", "author_label": "作者"},
}


def _build_url(content: NotificationContent) -> str:
    ...


def render_markdown(content: NotificationContent) -> tuple[str, str]:
    """..."""
    style = _PLATFORM_STYLE.get(content.platform, {"emoji": "📣", "author_label": "作者"})
    keywords_str = "；".join(content.keywords) if content.keywords else "无"
    url = _build_url(content)

    if content.type == "dynamic":
        ...
        return f"📢 {content.author} 的动态", "\n".join(parts)

    # 默认：完整内容模板
    parts = [...]
    return f"{style['emoji']} {content.title}", "\n".join(parts)
```

**改动 1：** 顶部 import 区加 `from shared.constants import GIT_SHA`（line 5 `from shared.protocols import NotificationContent` 之前，按字母序 `shared.constants` < `shared.protocols`）：

```python
from shared.constants import GIT_SHA
from shared.protocols import NotificationContent
```

**改动 2：** 在 `if content.type == "dynamic":` 分支**之前/之后均可**（type 是单一值无 fallthrough，顺序不影响；与现有 dynamic 分支并列放置），加 `health_alert` 分支。最终 `render_markdown` 函数体从 `style = ...` 开始应为：

```python
    style = _PLATFORM_STYLE.get(content.platform, {"emoji": "📣", "author_label": "作者"})
    keywords_str = "；".join(content.keywords) if content.keywords else "无"
    url = _build_url(content)

    # 健康告警（issue #55）：简化模板 + 版本 footer
    # 决策 5 限定：仅 health_alert 分支追加版本号，content/dynamic 不动
    if content.type == "health_alert":
        parts = [content.summary or content.title, "", f"(trawler@{GIT_SHA})"]
        return content.title, "\n".join(parts)

    if content.type == "dynamic":
        # 动态：简短格式，无 keywords/comment
        parts: list[str] = [f"{style['author_label']}: {content.author}"]
        if url:
            parts.append(f"链接: {content.source_id} {url}")
        parts.extend(["", content.summary or content.title])
        return f"📢 {content.author} 的动态", "\n".join(parts)

    # 默认：完整内容模板
    parts = [
        f"{style['author_label']}: {content.author}",
        f"链接: {content.source_id} {url}" if url else "",
        f"关键词: {keywords_str}",
        "",
        "详情:",
        content.summary,
    ]
    if content.comment_highlights:
        parts.extend(["", "评论区补充:", content.comment_highlights])
    return f"{style['emoji']} {content.title}", "\n".join(parts)
```

**关键变化点：**
1. 加 `health_alert` 分支（与 dynamic 分支并列放置即可，type 是单一值无 fallthrough，分支前后顺序无要求）
2. **类型注解方向**：`health_alert` 分支的 `parts = [...]` **不加类型注解**；dynamic 分支保留原代码的 `parts: list[str] = [...]` **不变**。pyright 静态分析遵循 "first-assignment-wins"：模块内对 `parts` 的首次赋值（出现在源码顺序最前的分支）的注解决定类型。原代码首次赋值就在 dynamic 分支带 `list[str]` 注解，保持原状即可，health_alert 不注解避免被识别为 "变量重复声明"。
3. `health_alert` 分支的 `parts` 不显式注解 —— 与 dynamic 分支保持单一声明点，pyright strict 满意。

- [ ] **Step 4: 跑测试确认 PASS**

```bash
uv run pytest tests/test_notifier_base.py -v
```

Expected: 全部 test PASS（含原有 14 个 + 新增 4 个）。

- [ ] **Step 5: 跑 pipeline e2e 测试确认告警路径未回归**

```bash
uv run pytest tests/test_pipeline_e2e.py tests/test_gotify_notifier.py -v
```

Expected: 全部 PASS。

- [ ] **Step 6: lint + type check**

```bash
uv run ruff check core/notifiers/base.py tests/test_notifier_base.py
uv run pyright
```

Expected: 0 issues / 0 errors。

- [ ] **Step 7: Commit**

```bash
git add core/notifiers/base.py tests/test_notifier_base.py
git commit -m "feat(issue-55): 健康告警推送加版本 footer (trawler@<GIT_SHA>)

- render_markdown 加 health_alert 分支：简化模板 + 末尾版本 footer
- 决策 5：仅 health_alert 加版本号，content/dynamic 不动（内容推送免噪音）
- health_alert 跳过 '关键词:'/'详情:' 等无意义字段

Refs: #55"
```

---

## Task 7: 全量验证 + push

**Files:** None（纯验证 + push）

**说明：** 所有 task 完成后,跑完整检查套件确认无回归,然后 push 触发 CI。

- [ ] **Step 1: 全量 lint**

```bash
uv run ruff check .
```

Expected: 0 issues。若有，修复后重跑。

- [ ] **Step 2: 全量格式检查**

```bash
uv run ruff format --check .
```

Expected: All files already formatted。若有未格式化文件，跑 `uv run ruff format .` 修复。

- [ ] **Step 3: 全量 type check**

```bash
uv run pyright
```

Expected: 0 errors（注意无参数，让 pyright 读 `pyproject.toml` include）。耗时约 7-10 秒。

- [ ] **Step 4: 全量测试**

```bash
uv run pytest -x
```

Expected: 全部 PASS，0 failed（`-x` fail fast，遇到第一个失败立即停止）。

- [ ] **Step 5: 端到端手动验证（CLI）**

```bash
uv run trawler --version
```

Expected: 输出 `Trawler <VERSION_DISPLAY>`，形如 `Trawler 0.1.0+dev (unknown)`（本地 dev 未注入 ENV）。

```bash
TRAWLER_GIT_SHA=test123 TRAWLER_BUILD_DATE=2026-06-30T14:29:00Z uv run trawler --version
```

Expected: 输出 `Trawler 0.1.0+test123 (2026-06-30T14:29:00Z)`（每次 uv run trawler 是全新 Python 进程，shared/constants.py 模块级 ENV 读取会重新执行，所以能读到 TRAWLER_GIT_SHA=test123）。

- [ ] **Step 6: 端到端手动验证（health API）**

启动 web server（后台）：

```bash
TRAWLER_GIT_SHA=manualtest TRAWLER_BUILD_DATE=2026-06-30 uv run uvicorn web.app:app --port 18080 &
sleep 2
curl -s http://localhost:18080/api/health | uv run python -m json.tool
kill %1
```

Expected: JSON 输出形如：

```json
{
    "status": "ok",
    "version": "0.1.0",
    "git_sha": "manualtest",
    "build_date": "2026-06-30"
}
```

（注意 `version` 字段是 dist version 不含 sha，符合 OpenAPI 习惯；`git_sha` / `build_date` 单独字段）。

- [ ] **Step 7: 查看 git log 确认所有 commit**

```bash
git log --oneline master..HEAD
```

Expected: 看到 6 个 commit（Task 1-6 各一个），commit message 含 `feat(issue-55)` 或相关前缀。

- [ ] **Step 8: Push 分支**

```bash
git push -u origin feat/issue-55-version-display
```

- [ ] **Step 9: 创建 PR**

```bash
gh pr create \
  --title "feat: issue #55 - 版本号展示（CLI/Web UI/health API/告警推送）" \
  --body "Closes #55

## 变更摘要

- **shared/constants.py**: 新增 \`VERSION\`/\`GIT_SHA\`/\`BUILD_DATE\`/\`VERSION_DISPLAY\`，importlib.metadata + ENV fallback 'dev'/'unknown'
- **CLI**: \`trawler --version\` + pipeline 启动日志用 \`VERSION_DISPLAY\`
- **Web UI**: sidebar 底部版本小字 + settings 页 '系统信息' 卡片
- **GET /api/health**: 无需认证返回 \`{status, version, git_sha, build_date}\`
- **健康告警推送**: \`health_alert\` 分支加 \`(trawler@<GIT_SHA>)\` footer
- **Dockerfile + workflow**: 构建期 ARG/ENV + build-args 注入 \`github.sha\` / \`run_started_at\`

## 验收对照（issue #55）

- [x] \`trawler --version\` 输出完整版本字符串
- [x] pipeline 启动日志 \`▶ Trawler <VERSION_DISPLAY>\`
- [x] Web UI sidebar 底部 + settings 页可见版本号
- [x] \`/api/health\` 返回 version 字段
- [x] 健康告警推送含 \`(trawler@<sha>)\` footer
- [x] Dockerfile 构建期注入 GIT_SHA/BUILD_DATE

## 测试

- 全量 \`uv run pytest -x\` PASS
- \`uv run ruff check .\` + \`uv run pyright\` 0 issues
- 新增测试: test_version_constants.py / test_web_health.py + 既有 test 文件追加用例

## 部署验证（合入后）

CI 自动构建镜像并推送 GHCR，Doco-CD 拉新镜像后：

\`\`\`
docker exec trawler curl -s localhost:8080/api/health | jq
\`\`\`

应返回含 \`git_sha\` 的 JSON，与 \`git log -1 --format=%h origin/master\` 对比可确认是否最新部署。" \
  --base master
```

- [ ] **Step 10: PR 创建后轮询 Qodo review**

按全局 AGENTS.md 流程：

```bash
~/.config/opencode/scripts/pr-poll-review.sh <PR_NUMBER>
```

每 3 分钟轮询，连续 2 次无新评论视为 review 完成。修复 review 意见后重新 push。

---

## Self-Review 检查清单

写完 plan 后自检（执行者无需再做）：

**1. Spec coverage（issue #55 需求点）：**
- ✅ 版本号来源（构建期注入）→ Task 4
- ✅ 版本读取（运行期 constants.py）→ Task 1
- ✅ CLI `--version` + 启动日志 → Task 2
- ✅ Web UI 展示（sidebar + settings）→ Task 5
- ✅ 健康告警推送 footer → Task 6（决策 5 限定仅 health_alert）
- ✅ `/api/health` API → Task 3
- ✅ Dockerfile + workflow 注入 → Task 4

**2. Placeholder scan:** 无 TBD/TODO/省略代码，所有 task 含完整代码块。

**3. Type consistency:**
- `VERSION_DISPLAY` 在 Task 1 定义，Task 2/5 使用 ✓
- `GIT_SHA` 在 Task 1 定义，Task 3/5/6 使用 ✓
- `NotificationContent.type == "health_alert"` 在 Task 6 使用，与 `run_check.py:608` 现有传值一致 ✓
- `_PUBLIC_PREFIXES` 元组类型，Task 3 加 `"/api/health"` 字符串 ✓

**4. 已知执行风险：**
- Task 5 Step 1 的 `web.routes.dashboard.load_config` patch target 需执行时 grep 确认（plan 已标注）
- Task 6 Step 3 的 `parts: list[str]` 类型注解跨分支需注意 pyright strict（plan 已说明处理方式）
- Task 4 无单元测试，依赖 CI 验证（plan 已说明，Step 4 给出本地 docker 验证可选路径）

# Web UI Token 管理 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Web UI 加 API token 管理 + sub ownership 分配界面，admin 不必 SSH 跑 CLI
**Architecture:** 薄 wrapper 路由复用 CLI 纯函数，session flash 存明文，HTMX + Jinja2
**Tech Stack:** FastAPI, Jinja2, HTMX, starlette SessionMiddleware

---

## Task 1: Sidebar nav item + 空路由骨架

**文件：** `web/routes/tokens.py`（新建）、`web/templates/tokens.html`（新建）、`web/app.py`、`web/templates/base.html`、`tests/test_web_tokens.py`（新建）

### Step 1.1 — 写测试：GET /tokens 返回 200 + 页面含标题

`tests/test_web_tokens.py`:

```python
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from web.app import create_app
from web.auth import set_password

PASSWORD = "test12345"


@pytest.fixture
async def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncClient:
    monkeypatch.setattr("web.auth.AUTH_TOML_PATH", tmp_path / "auth.toml")
    set_password(PASSWORD)
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.post("/login", data={"password": PASSWORD}, follow_redirects=False)
        assert resp.status_code == 303
        yield c


class TestTokensPage:
    @patch("web.routes.tokens.load_auth_config")
    async def test_list_page_returns_200(self, mock_load, client: AsyncClient) -> None:
        from shared.config import WebAuthConfig

        mock_load.return_value = WebAuthConfig(api_tokens=[])
        resp = await client.get("/tokens")
        assert resp.status_code == 200
        assert "API Token" in resp.text
```

验证失败：`uv run pytest -x tests/test_web_tokens.py` → ImportError（模块不存在）

### Step 1.2 — 创建空路由 + 空模板

`web/routes/tokens.py`:

```python
"""API Token 管理路由（Web UI）。"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from web.app import TEMPLATES

router = APIRouter()


@router.get("/tokens", response_class=HTMLResponse)
async def tokens_page(request: Request) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(
        request,
        "tokens.html",
        {"active_nav": "tokens"},
    )
```

`web/templates/tokens.html`:

```html
{% extends "base.html" %}
{% from "_macros.html" import badge %}

{% block title %}API Token · Trawler{% endblock %}

{% block content %}
<h1 class="text-2xl font-semibold tracking-tight mb-1">API Token 管理</h1>
<p class="text-sm text-[var(--text-secondary)] mb-6">创建、查看和撤销 API token</p>
{% endblock %}
```

### Step 1.3 — 注册路由 + sidebar nav

`web/app.py:254-275`（在 route imports 区域加两行）：

```python
    from web.routes.subscriptions import router as subscriptions_router
    from web.routes.tokens import router as tokens_router       # 新增
```

`web/app.py:266-275`（在 `app.include_router` 区域加一行）：

```python
    app.include_router(subscriptions_router)
    app.include_router(tokens_router)  # 新增
```

`web/templates/base.html`（在 `/auth` nav item 下方、`/endpoints` 上方加新 nav item）：

```html
      <a href="/auth" ...>登录管理</a>
      <a href="/tokens" class="flex items-center gap-3 px-3 py-2 rounded-[8px] text-sm transition-colors {% if active_nav == 'tokens' %}bg-[var(--color-primary)]/10 text-[var(--color-primary)] font-medium{% else %}text-[var(--text-secondary)] hover:bg-black/5 dark:hover:bg-white/5 hover:text-[var(--text-primary)]{% endif %}">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
        API Token
      </a>
      <a href="/endpoints" ...>推送端点</a>
```

### Step 1.4 — 验证通过

```
uv run pytest -x tests/test_web_tokens.py
```

### Step 1.5 — commit

```
git add -A && git commit -m "feat(web): tokens skeleton — sidebar nav + empty route + test"
```

---

## Task 2: GET /tokens token 列表显示

加载 auth config 显示 token 列表，参考 `endpoints.html` 的 list 模式。

### Step 2.1 — 写测试：列表渲染 token 行

`tests/test_web_tokens.py` TestTokensPage 下新增：

```python
    @patch("web.routes.tokens.load_auth_config")
    async def test_list_shows_tokens(self, mock_load, client: AsyncClient) -> None:
        from shared.config import ApiTokenEntry, WebAuthConfig

        mock_load.return_value = WebAuthConfig(
            api_tokens=[
                ApiTokenEntry(name="admin", token_hash="a1b2c3d4e5f6...", created_at=1720600000.0, scopes=["tokens:manage"]),
                ApiTokenEntry(name="viewer", token_hash="e5f6g7h8...", created_at=1720500000.0, scopes=["subscriptions:read"]),
            ]
        )
        resp = await client.get("/tokens")
        assert resp.status_code == 200
        assert "admin" in resp.text
        assert "viewer" in resp.text
        assert "a1b2c3d4" in resp.text  # hash 前 8 位
        assert "tokens:manage" in resp.text
        assert "subscriptions:read" in resp.text
```

验证失败：列表为空，admin/viewer 不在页面中。

### Step 2.2 — 实现路由加载 token 列表

`web/routes/tokens.py`:

```python
from web.auth import load_auth_config


@router.get("/tokens", response_class=HTMLResponse)
async def tokens_page(request: Request) -> HTMLResponse:
    cfg = load_auth_config()
    return TEMPLATES.TemplateResponse(
        request,
        "tokens.html",
        {
            "active_nav": "tokens",
            "tokens": cfg.api_tokens,
            "plaintext_name": request.session.pop("created_token_name", None),
            "plaintext_value": request.session.pop("created_token_plaintext", None),
        },
    )
```

### Step 2.3 — 更新模板渲染列表

`web/templates/tokens.html`:

```html
{% extends "base.html" %}
{% from "_macros.html" import badge %}

{% block title %}API Token · Trawler{% endblock %}

{% block content %}
<h1 class="text-2xl font-semibold tracking-tight mb-1">API Token 管理</h1>
<p class="text-sm text-[var(--text-secondary)] mb-6">创建、查看和撤销 API token</p>

{% if plaintext_name and plaintext_value %}
<div class="mb-6 p-4 rounded-[12px] border border-yellow-200 bg-yellow-50/80 dark:border-yellow-800/40 dark:bg-yellow-900/20">
  <p class="text-sm font-medium text-yellow-800 dark:text-yellow-200 mb-2">⚠️ Token「{{ plaintext_name }}」明文（仅此一次，刷新后丢失）：</p>
  <div class="flex items-center gap-2">
    <code id="plaintext-token" class="flex-1 px-3 py-2 rounded-[8px] bg-white dark:bg-gray-800 border border-yellow-300 dark:border-yellow-700 text-sm font-mono select-all">{{ plaintext_value }}</code>
    <button onclick="navigator.clipboard.writeText(document.getElementById('plaintext-token').textContent); showToast('已复制', 'success')"
            class="px-3 py-2 rounded-[8px] bg-yellow-600 text-white text-sm font-medium hover:bg-yellow-700 transition-colors">复制</button>
  </div>
  <p class="text-xs text-yellow-700 dark:text-yellow-300 mt-1">请立即保存。Trawler 不存储明文。</p>
</div>
{% endif %}

{% if tokens %}
<div class="bg-[var(--card-bg)] backdrop-blur-[12px] rounded-[14px] shadow-card border border-[var(--card-border)] overflow-hidden">
  <table class="w-full text-sm">
    <thead>
      <tr class="border-b border-[var(--card-border)] text-[var(--text-secondary)]">
        <th class="text-left px-5 py-3 font-medium">名称</th>
        <th class="text-left px-5 py-3 font-medium">Hash（前 8 位）</th>
        <th class="text-left px-5 py-3 font-medium">权限</th>
        <th class="text-left px-5 py-3 font-medium">创建时间</th>
        <th class="text-right px-5 py-3 font-medium">操作</th>
      </tr>
    </thead>
    <tbody>
      {% for t in tokens %}
      <tr class="border-b border-[var(--card-border)] last:border-0 hover:bg-gray-50/50 dark:hover:bg-gray-800/30">
        <td class="px-5 py-3 font-medium">{{ t.name }}</td>
        <td class="px-5 py-3 font-mono text-xs text-[var(--text-secondary)]">{{ t.token_hash[:8] }}</td>
        <td class="px-5 py-3">
          <div class="flex flex-wrap gap-1">
            {% for s in t.scopes %}
            {{ badge(s, color="blue" if s != "tokens:manage" else "red") }}
            {% else %}
            <span class="text-xs text-[var(--text-tertiary)]">—</span>
            {% endfor %}
          </div>
        </td>
        <td class="px-5 py-3 text-[var(--text-secondary)]">{{ t.created_at | timeago if t.created_at else "—" }}</td>
        <td class="px-5 py-3 text-right">
          <button hx-post="/tokens/revoke"
                  hx-vals='{"token_name":"{{ t.name }}"}'
                  hx-target="body"
                  hx-confirm="确定 revoke token「{{ t.name }}」？"
                  class="px-3 py-1.5 rounded-[8px] text-xs font-medium text-red-600 hover:bg-red-50 dark:hover:bg-red-900/20 transition-colors">revoke</button>
        </td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
</div>
{% else %}
<p class="text-sm text-[var(--text-secondary)]">暂无 token。创建第一个 token 开始使用 API。</p>
{% endif %}
{% endblock %}
```

### Step 2.4 — 验证

```
uv run pytest -x tests/test_web_tokens.py
```

### Step 2.5 — commit

```
git add -A && git commit -m "feat(web): token list display with hash/scopes/created"
```

---

## Task 3: POST /tokens/create

创建表单 + 调 `create_token` + session flash 存明文 + Redirect 303。

注意：`create_token` 是同步纯函数，路由直接同步调（不在 async 函数中 await）。

### Step 3.1 — 写测试：创建成功和表单验证

`tests/test_web_tokens.py` 下新增 class：

```python
class TestTokenCreate:
    @patch("web.routes.tokens.create_token")
    async def test_create_redirects_to_tokens(self, mock_create, client: AsyncClient) -> None:
        mock_create.return_value = "trawler_abc123def456"
        resp = await client.post(
            "/tokens/create",
            data={"name": "test-token", "scopes": ["subscriptions:read"]},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/tokens"
        mock_create.assert_called_once_with("test-token", ["subscriptions:read"])

    @patch("web.routes.tokens.create_token")
    async def test_create_multiple_scopes(self, mock_create, client: AsyncClient) -> None:
        mock_create.return_value = "trawler_xyz789"
        resp = await client.post(
            "/tokens/create",
            data={"name": "multi", "scopes": ["subscriptions:read", "messages:read", "messages:write"]},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert resp.status_code == 303
        mock_create.assert_called_once_with("multi", ["subscriptions:read", "messages:read", "messages:write"])

    async def test_create_empty_name_rejected(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/tokens/create",
            data={"name": "", "scopes": ["subscriptions:read"]},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert resp.status_code == 303
        loc = resp.headers["location"]
        assert "toast_key=token.name_invalid" in loc
        assert "type=error" in loc
```

### Step 3.2 — 实现创建路由

`web/routes/tokens.py`:

```python
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from api.auth import create_token
from web.app import TEMPLATES
from web.auth import load_auth_config


@router.post("/tokens/create")
async def tokens_create(
    request: Request,
    name: str = Form(...),
    scopes: list[str] | None = Form(None),
) -> RedirectResponse:
    if not name.strip():
        return RedirectResponse(
            url="/tokens?toast_key=token.name_invalid&type=error",
            status_code=303,
        )
    scope_list = scopes or []
    plaintext = create_token(name.strip(), scope_list)
    request.session["created_token_name"] = name.strip()
    request.session["created_token_plaintext"] = plaintext
    return RedirectResponse(url="/tokens?toast_key=token.created&type=success", status_code=303)
```

**注意：** `scopes` 字段在 HTML form 中 `name="scopes"`，多个 checkbox 同一 name → FastAPI 自动解析为 `list[str]`。`Form(None)` 处理无任何勾选的情况 → None。

### Step 3.3 — 模板加创建表单

在 `tokens.html` 的 `<h1>` / `<p>` 与 banner 之间、table 之前加创建表单：

```html
<div class="mb-6 bg-[var(--card-bg)] backdrop-blur-[12px] rounded-[14px] shadow-card border border-[var(--card-border)] p-5">
  <h2 class="text-base font-medium mb-4">创建 Token</h2>
  <form hx-post="/tokens/create" hx-target="body" class="space-y-4">
    <div>
      <label class="block text-sm font-medium mb-1.5">名称</label>
      <input type="text" name="name" required
             class="w-full max-w-xs px-3 py-2 rounded-[8px] border border-[var(--card-border)] bg-[var(--bg-base)] text-sm focus:ring-2 focus:ring-[var(--color-primary)]/20 focus:border-[var(--color-primary)] outline-none transition-all"
             placeholder="my-token">
    </div>
    <div>
      <label class="block text-sm font-medium mb-2">权限（Scope）</label>
      <div class="grid grid-cols-2 md:grid-cols-3 gap-2">
        <label class="flex items-center gap-2 px-3 py-2 rounded-[8px] border border-[var(--card-border)] hover:bg-gray-50/50 dark:hover:bg-gray-800/30 cursor-pointer transition-colors">
          <input type="checkbox" name="scopes" value="subscriptions:read" class="rounded border-gray-300">
          <span class="text-sm">subscriptions:read</span>
        </label>
        <label class="flex items-center gap-2 px-3 py-2 rounded-[8px] border border-[var(--card-border)] hover:bg-gray-50/50 dark:hover:bg-gray-800/30 cursor-pointer transition-colors">
          <input type="checkbox" name="scopes" value="subscriptions:write" class="rounded border-gray-300">
          <span class="text-sm">subscriptions:write</span>
        </label>
        <label class="flex items-center gap-2 px-3 py-2 rounded-[8px] border border-[var(--card-border)] hover:bg-gray-50/50 dark:hover:bg-gray-800/30 cursor-pointer transition-colors">
          <input type="checkbox" name="scopes" value="messages:read" class="rounded border-gray-300">
          <span class="text-sm">messages:read</span>
        </label>
        <label class="flex items-center gap-2 px-3 py-2 rounded-[8px] border border-[var(--card-border)] hover:bg-gray-50/50 dark:hover:bg-gray-800/30 cursor-pointer transition-colors">
          <input type="checkbox" name="scopes" value="messages:write" class="rounded border-gray-300">
          <span class="text-sm">messages:write</span>
        </label>
        <label class="flex items-center gap-2 px-3 py-2 rounded-[8px] border border-[var(--card-border)] hover:bg-gray-50/50 dark:hover:bg-gray-800/30 cursor-pointer transition-colors">
          <input type="checkbox" name="scopes" value="check:read" class="rounded border-gray-300">
          <span class="text-sm">check:read</span>
        </label>
        <label class="flex items-center gap-2 px-3 py-2 rounded-[8px] border border-[var(--card-border)] hover:bg-gray-50/50 dark:hover:bg-gray-800/30 cursor-pointer transition-colors">
          <input type="checkbox" name="scopes" value="check:run" class="rounded border-gray-300">
          <span class="text-sm">check:run</span>
        </label>
        <label class="flex items-center gap-2 px-3 py-2 rounded-[8px] border border-red-100 dark:border-red-900/30 hover:bg-red-50/50 dark:hover:bg-red-900/20 cursor-pointer transition-colors">
          <input type="checkbox" name="scopes" value="tokens:manage" class="rounded border-red-300">
          <span class="text-sm text-red-600 dark:text-red-400">tokens:manage ⚠️ superuser</span>
        </label>
      </div>
    </div>
    <button type="submit"
            class="px-5 py-2 bg-apple-blue text-white rounded-[8px] text-sm font-medium hover:bg-blue-600 transition-colors">创建</button>
  </form>
</div>
```

### Step 3.4 — 验证

```
uv run pytest -x tests/test_web_tokens.py
```

### Step 3.5 — commit

```
git add -A && git commit -m "feat(web): POST /tokens/create with session flash + scope checkboxes"
```

---

## Task 4: 创建后明文 inline banner

**已经在 Task 2 & 3 的模板中实现了**（session flash pop + banner HTML 在 `tokens.html` 顶部 + 复制按钮）。

### Step 4.1 — 写测试：验证 session flash banner 渲染

`tests/test_web_tokens.py` TestTokensPage 下新增：

```python
    async def test_plaintext_banner_appears_with_session(self, client: AsyncClient) -> None:
        """Simulate session flash set by create route, verify banner renders."""
        # Manually inject session data via cookie — we test the GET path
        # with session already populated via the POST → redirect flow in
        # integration. For unit-level: mock load_auth_config + use session.
        # Use a real POST first, then follow redirect.
        from unittest.mock import AsyncMock, patch

        with patch("web.routes.tokens.create_token", return_value="trawler_test_plain"):
            resp = await client.post(
                "/tokens/create",
                data={"name": "flash-test", "scopes": ["subscriptions:read"]},
                headers={"X-Requested-With": "XMLHttpRequest"},
            )
        assert resp.status_code == 303
        # Follow redirect (cookies carry session)
        follow = await client.get("/tokens")
        assert follow.status_code == 200
        assert "trawler_test_plain" in follow.text
        assert "flash-test" in follow.text
        # Second GET should NOT have banner (session popped)
        follow2 = await client.get("/tokens")
        assert follow2.status_code == 200
        assert "trawler_test_plain" not in follow2.text
```

### Step 4.2 — 验证

```
uv run pytest -x tests/test_web_tokens.py
```

### Step 4.3 — commit

```
git add -A && git commit -m "feat(web): plaintext banner one-shot display + copy button"
```

---

## Task 5: POST /tokens/revoke

### Step 5.1 — 写测试

`tests/test_web_tokens.py` 下新增 class：

```python
class TestTokenRevoke:
    @patch("web.routes.tokens.revoke_token")
    async def test_revoke_success(self, mock_revoke, client: AsyncClient) -> None:
        mock_revoke.return_value = True
        resp = await client.post(
            "/tokens/revoke",
            data={"token_name": "test-token"},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert resp.status_code == 303
        loc = resp.headers["location"]
        assert "toast_key=token.revoked" in loc
        assert "type=success" in loc
        mock_revoke.assert_called_once_with("test-token")

    @patch("web.routes.tokens.revoke_token")
    async def test_revoke_not_found(self, mock_revoke, client: AsyncClient) -> None:
        mock_revoke.return_value = False
        resp = await client.post(
            "/tokens/revoke",
            data={"token_name": "nonexistent"},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert resp.status_code == 303
        loc = resp.headers["location"]
        assert "toast_key=token.not_found" in loc
        assert "type=error" in loc
```

### Step 5.2 — 实现 revoke 路由

`web/routes/tokens.py`:

```python
from api.auth import create_token, revoke_token


@router.post("/tokens/revoke")
async def tokens_revoke(token_name: str = Form(...)) -> RedirectResponse:
    ok = revoke_token(token_name)
    if ok:
        return RedirectResponse(
            url="/tokens?toast_key=token.revoked&type=success",
            status_code=303,
        )
    return RedirectResponse(
        url="/tokens?toast_key=token.not_found&type=error",
        status_code=303,
    )
```

### Step 5.3 — 验证

```
uv run pytest -x tests/test_web_tokens.py
```

### Step 5.4 — commit

```
git add -A && git commit -m "feat(web): POST /tokens/revoke with toast feedback"
```

---

## Task 6: 订阅页 owner badge

在 `subscriptions.html` 每 sub 行加 owner badge + 「管理 ownership」按钮。

### Step 6.1 — 写测试：订阅页含 owner badge

`tests/test_web_subscriptions.py` 下新增 class（注意 mock `list_subscriptions` 时带 `owner_token` 字段）：

```python
class TestSubscriptionOwnershipBadge:
    @patch("web.routes.subscriptions.list_subscriptions", new_callable=AsyncMock)
    async def test_owner_badge_shows_token_name(self, mock_list, client: AsyncClient) -> None:
        mock_list.return_value = {
            "bilibili": [{"uid": 1, "name": "UP主", "owner_token": "admin-token"}]
        }
        resp = await client.get("/subscriptions")
        assert resp.status_code == 200
        assert "admin-token" in resp.text

    @patch("web.routes.subscriptions.list_subscriptions", new_callable=AsyncMock)
    async def test_orphan_badge_red(self, mock_list, client: AsyncClient) -> None:
        mock_list.return_value = {
            "bilibili": [{"uid": 2, "name": "孤儿UP"}]
        }
        resp = await client.get("/subscriptions")
        assert resp.status_code == 200
        assert "孤儿" in resp.text

    @patch("web.routes.subscriptions.list_subscriptions", new_callable=AsyncMock)
    async def test_orphan_shows_set_owner_button(self, mock_list, client: AsyncClient) -> None:
        mock_list.return_value = {
            "bilibili": [{"uid": 2, "name": "孤儿UP"}]
        }
        resp = await client.get("/subscriptions")
        assert resp.status_code == 200
        assert "设为 owner" in resp.text
```

### Step 6.2 — 修改 subscriptions.html

在每条 sub 的 `item.get("name", "-")` 右侧或 identifier 下方加 owner badge。

具体改：在 `templates/subscriptions.html` 的 name/identifier 行后、notify_endpoints 循环前加：

```html
            {% set owner = item.get("owner_token", "") %}
            {% if owner %}
            <span class="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[11px] font-medium bg-green-50/70 text-green-700 border border-green-200/60 dark:bg-green-900/20 dark:text-green-300 dark:border-green-800/40">
              {{ owner }}
            </span>
            {% else %}
            <span class="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[11px] font-medium bg-red-50/70 text-red-700 border border-red-200/60 dark:bg-red-900/20 dark:text-red-300 dark:border-red-800/40">
              孤儿
            </span>
            {% endif %}
            <button type="button"
              hx-get="/subscriptions/{{ p.key }}/{{ item.get('uid') or item.get('user_id', '') }}/ownership"
              hx-target="body"
              hx-swap="beforeend"
              class="ml-auto text-[11px] px-2 py-1 rounded-[6px] text-[var(--text-secondary)] hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors">
              {% if owner %}管理 ownership{% else %}设为 owner{% endif %}
            </button>
```

**注意：** 插入到 `subscriptions.html` 的 `shrink-0` 操作容器（`<div class="flex items-center gap-1.5 shrink-0">`）内、删除 form 之前——即在 endpoint 添加区域之后、删除 form 之前。

### Step 6.3 — 验证

```
uv run pytest -x tests/test_web_subscriptions.py::TestSubscriptionOwnershipBadge
```

### Step 6.4 — commit

```
git add -A && git commit -m "feat(web): subscription owner badge (orphan red / owner green) + action button"
```

---

## Task 7: GET ownership modal

新建 `web/routes/subscription_ownership.py` 路由 + `_token_modal.html` 模板。

### Step 7.1 — 写测试

`tests/test_web_subscription_ownership.py`（新建）：

```python
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from web.app import create_app
from web.auth import set_password

PASSWORD = "test12345"


@pytest.fixture
async def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncClient:
    monkeypatch.setattr("web.auth.AUTH_TOML_PATH", tmp_path / "auth.toml")
    set_password(PASSWORD)
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.post("/login", data={"password": PASSWORD}, follow_redirects=False)
        assert resp.status_code == 303
        yield c


class TestOwnershipModal:
    @patch("web.routes.subscription_ownership.load_subscriptions")
    @patch("web.routes.subscription_ownership.load_auth_config")
    async def test_modal_returns_html(
        self, mock_auth, mock_subs, client: AsyncClient
    ) -> None:
        from shared.config import ApiTokenEntry, WebAuthConfig

        mock_auth.return_value = WebAuthConfig(
            api_tokens=[
                ApiTokenEntry(name="admin-token", token_hash="aaa", scopes=["tokens:manage"]),
                ApiTokenEntry(name="viewer-token", token_hash="bbb", scopes=["subscriptions:read"]),
            ]
        )
        # `load_subscriptions` returns a list of subscription dicts
        mock_subs.return_value = {
            "bilibili": [
                {"uid": 1, "name": "UP主", "owner_token": "admin-token", "assigned_tokens": ["viewer-token"]}
            ]
        }
        resp = await client.get("/subscriptions/bili/1/ownership")
        assert resp.status_code == 200
        assert "admin-token" in resp.text  # current owner in dropdown
        assert "viewer-token" in resp.text  # assigned token shown
        assert "UP主" in resp.text

    @patch("web.routes.subscription_ownership.load_subscriptions")
    @patch("web.routes.subscription_ownership.load_auth_config")
    async def test_modal_orphan_shows_no_owner(
        self, mock_auth, mock_subs, client: AsyncClient
    ) -> None:
        from shared.config import ApiTokenEntry, WebAuthConfig

        mock_auth.return_value = WebAuthConfig(
            api_tokens=[
                ApiTokenEntry(name="admin-token", token_hash="aaa", scopes=["tokens:manage"]),
            ]
        )
        mock_subs.return_value = {
            "bilibili": [
                {"uid": 2, "name": "孤儿UP"}
            ]
        }
        resp = await client.get("/subscriptions/bili/2/ownership")
        assert resp.status_code == 200
        assert "孤儿" in resp.text or "无" in resp.text
```

验证失败：`uv run pytest -x tests/test_web_subscription_ownership.py` → ImportError（模块不存在）

### Step 7.2 — 实现 ownership modal 路由 + 模板

`web/routes/subscription_ownership.py`:

```python
"""Sub ownership 管理路由 — HTMX modal。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from core.subscription_cli import SUBSCRIPTION_KEY, list_subscriptions
from web.app import TEMPLATES
from web.auth import load_auth_config
from web.routes.subscriptions import _platform_key_to_name

router = APIRouter()


def _key_field(platform_key: str) -> str:
    return SUBSCRIPTION_KEY.get(platform_key, ("user_id",))[0]


@router.get("/subscriptions/{platform}/{identifier}/ownership")
async def ownership_modal(
    request: Request, platform: str, identifier: str
) -> HTMLResponse:
    plat_name = _platform_key_to_name(platform)
    key = _key_field(platform)
    subs = await list_subscriptions()
    items: list[dict[str, Any]] = subs.get(plat_name, [])
    sub = next((s for s in items if str(s.get(key, "")) == identifier), None)

    if sub is None:
        return HTMLResponse("<p class='text-sm text-red-500 p-4'>订阅不存在</p>")

    auth_cfg = load_auth_config()
    all_tokens = auth_cfg.api_tokens
    owner = sub.get("owner_token", "")
    assigned = sub.get("assigned_tokens", [])

    return TEMPLATES.TemplateResponse(
        request,
        "_token_modal.html",
        {
            "platform": platform,
            "identifier": identifier,
            "sub_name": sub.get("name", identifier),
            "owner": owner,
            "assigned": assigned,
            "all_tokens": [t.name for t in all_tokens],
        },
    )
```

`web/templates/_token_modal.html`:

```html
<div id="ownership-modal-overlay" class="fixed inset-0 bg-black/30 z-50 flex items-center justify-center"
     onclick="if(event.target===this) this.remove()">
  <div class="bg-[var(--card-bg)] backdrop-blur-[12px] rounded-[14px] shadow-card border border-[var(--card-border)] p-6 max-w-md w-full mx-4">
    <div class="flex items-center justify-between mb-4">
      <h3 class="text-base font-medium">管理 Ownership</h3>
      <button onclick="this.closest('#ownership-modal-overlay').remove()" class="text-[var(--text-tertiary)] hover:text-[var(--text-primary)]">
        <svg class="w-5 h-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
      </button>
    </div>
    <p class="text-sm text-[var(--text-secondary)] mb-4">订阅：{{ sub_name }}（{{ platform }}/{{ identifier }}）</p>

    <div class="mb-4">
      <label class="block text-sm font-medium mb-1.5">Owner token</label>
      <form hx-post="/subscriptions/{{ platform }}/{{ identifier }}/owner" hx-target="body" class="flex gap-2">
        <select name="owner_token" class="flex-1 px-3 py-2 rounded-[8px] border border-[var(--card-border)] bg-[var(--card-bg)] text-sm focus:ring-2 focus:ring-[var(--color-primary)]/20 outline-none">
          <option value="">（无 owner）</option>
          {% for t in all_tokens %}
          <option value="{{ t }}" {% if t == owner %}selected{% endif %}>{{ t }}</option>
          {% endfor %}
        </select>
        <button type="submit"
                class="px-3 py-2 rounded-[8px] bg-apple-blue text-white text-sm font-medium hover:bg-blue-600 transition-colors">改 Owner</button>
      </form>
    </div>

    <div>
      <label class="block text-sm font-medium mb-1.5">Assigned tokens（只读权限）</label>
      <div class="space-y-1">
        {% for t in all_tokens %}
        <form hx-post="/subscriptions/{{ platform }}/{{ identifier }}/{{ 'unassign' if t in assigned else 'assign' }}" hx-target="body" class="block">
          <input type="hidden" name="token_name" value="{{ t }}">
          <label class="flex items-center gap-2 px-3 py-1.5 rounded-[8px] hover:bg-gray-50/50 dark:hover:bg-gray-800/30 cursor-pointer transition-colors">
            <input type="checkbox" name="assigned"
                   {% if t in assigned %}checked{% endif %}
                   onchange="this.closest('form').requestSubmit()"
                   class="rounded border-gray-300">
            <span class="text-sm">{{ t }}</span>
          </label>
        </form>
        {% endfor %}
      </div>
    </div>
  </div>
</div>
```

### Step 7.3 — 注册路由

`web/app.py:254-275`（加 import + include）：

```python
    from web.routes.subscription_ownership import router as sub_ownership_router
```

```python
    app.include_router(sub_ownership_router)
```

### Step 7.4 — 验证

```
uv run pytest -x tests/test_web_subscription_ownership.py
```

### Step 7.5 — commit

```
git add -A && git commit -m "feat(web): ownership modal (GET) with owner dropdown + assigned checkboxes"
```

---

## Task 8: POST assign/unassign

每个 checkbox 独立 submit（勾选 = `/assign`，取消 = `/unassign`）。

### Step 8.1 — 写测试

`tests/test_web_subscription_ownership.py` 下新增：

```python
class TestTokenAssign:
    @patch("web.routes.subscription_ownership.assign_token_to_subscription", new_callable=AsyncMock)
    async def test_assign_success(self, mock_assign, client: AsyncClient) -> None:
        mock_assign.return_value = (True, "已分配: viewer-token")
        resp = await client.post(
            "/subscriptions/bili/1/assign",
            data={"token_name": "viewer-token"},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert resp.status_code == 303
        loc = resp.headers["location"]
        assert "toast_key=token.assigned" in loc
        assert "type=success" in loc

    @patch("web.routes.subscription_ownership.assign_token_to_subscription", new_callable=AsyncMock)
    async def test_assign_failure(self, mock_assign, client: AsyncClient) -> None:
        mock_assign.return_value = (False, "未知 token: bad")
        resp = await client.post(
            "/subscriptions/bili/1/assign",
            data={"token_name": "bad"},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert resp.status_code == 303
        loc = resp.headers["location"]
        assert "toast_key=token.assign_failed" in loc
        assert "type=error" in loc

    @patch("web.routes.subscription_ownership.unassign_token_from_subscription", new_callable=AsyncMock)
    async def test_unassign_success(self, mock_unassign, client: AsyncClient) -> None:
        mock_unassign.return_value = (True, "已解绑: viewer-token")
        resp = await client.post(
            "/subscriptions/bili/1/unassign",
            data={"token_name": "viewer-token"},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert resp.status_code == 303
        loc = resp.headers["location"]
        assert "toast_key=token.assigned" in loc  # 复用 success key
        assert "type=success" in loc

    @patch("web.routes.subscription_ownership.unassign_token_from_subscription", new_callable=AsyncMock)
    async def test_unassign_failure(self, mock_unassign, client: AsyncClient) -> None:
        mock_unassign.return_value = (False, "未找到订阅")
        resp = await client.post(
            "/subscriptions/bili/1/unassign",
            data={"token_name": "viewer-token"},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert resp.status_code == 303
        loc = resp.headers["location"]
        assert "toast_key=token.assign_failed" in loc
        assert "type=error" in loc
```

### Step 8.2 — 实现 assign/unassign 路由

`web/routes/subscription_ownership.py`:

```python
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from core.subscription_cli import (
    assign_token_to_subscription,
    unassign_token_from_subscription,
)


@router.post("/subscriptions/{platform}/{identifier}/assign")
async def ownership_assign(
    platform: str, identifier: str, token_name: str = Form(...)
) -> RedirectResponse:
    plat_name = _platform_key_to_name(platform)
    ok, _msg = await assign_token_to_subscription(plat_name, identifier, token_name)
    toast_key, t = ("token.assigned", "success") if ok else ("token.assign_failed", "error")
    return RedirectResponse(
        url=f"/subscriptions?toast_key={toast_key}&type={t}",
        status_code=303,
    )


@router.post("/subscriptions/{platform}/{identifier}/unassign")
async def ownership_unassign(
    platform: str, identifier: str, token_name: str = Form(...)
) -> RedirectResponse:
    plat_name = _platform_key_to_name(platform)
    ok, _msg = await unassign_token_from_subscription(plat_name, identifier, token_name)
    toast_key, t = ("token.assigned", "success") if ok else ("token.assign_failed", "error")
    return RedirectResponse(
        url=f"/subscriptions?toast_key={toast_key}&type={t}",
        status_code=303,
    )
```

### Step 8.3 — 验证

```
uv run pytest -x tests/test_web_subscription_ownership.py::TestTokenAssign
```

### Step 8.4 — commit

```
git add -A && git commit -m "feat(web): POST assign/unassign with toast feedback"
```

---

## Task 9: POST set_owner（含 adopt）

modal dropdown + 「改 owner」按钮。

### Step 9.1 — 写测试

`tests/test_web_subscription_ownership.py` 下新增：

```python
class TestOwnerSet:
    @patch("web.routes.subscription_ownership.set_subscription_owner", new_callable=AsyncMock)
    async def test_set_owner_success(self, mock_set, client: AsyncClient) -> None:
        mock_set.return_value = (True, "已设置 owner: admin-token")
        resp = await client.post(
            "/subscriptions/bili/1/owner",
            data={"token_name": "admin-token"},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert resp.status_code == 303
        loc = resp.headers["location"]
        assert "toast_key=token.owner_set" in loc
        assert "type=success" in loc

    @patch("web.routes.subscription_ownership.set_subscription_owner", new_callable=AsyncMock)
    async def test_set_owner_failure(self, mock_set, client: AsyncClient) -> None:
        mock_set.return_value = (False, "未找到订阅")
        resp = await client.post(
            "/subscriptions/bili/999/owner",
            data={"token_name": "admin-token"},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert resp.status_code == 303
        loc = resp.headers["location"]
        assert "toast_key=token.owner_failed" in loc
        assert "type=error" in loc
```

### Step 9.2 — 实现 set_owner 路由

`web/routes/subscription_ownership.py`:

```python
from core.subscription_cli import (
    assign_token_to_subscription,
    set_subscription_owner,
    unassign_token_from_subscription,
)


@router.post("/subscriptions/{platform}/{identifier}/owner")
async def ownership_set_owner(
    platform: str, identifier: str, token_name: str = Form(...)
) -> RedirectResponse:
    plat_name = _platform_key_to_name(platform)
    ok, _msg = await set_subscription_owner(plat_name, identifier, token_name)
    toast_key, t = ("token.owner_set", "success") if ok else ("token.owner_failed", "error")
    return RedirectResponse(
        url=f"/subscriptions?toast_key={toast_key}&type={t}",
        status_code=303,
    )
```

### Step 9.3 — 验证

```
uv run pytest -x tests/test_web_subscription_ownership.py::TestOwnerSet
```

### Step 9.4 — commit

```
git add -A && git commit -m "feat(web): POST set_owner (adopt orphan) with toast feedback"
```

---

## Task 10: TOAST_KEY_MAP 扩展 + lint/typecheck/全测试

### Step 10.1 — 扩展 TOAST_KEY_MAP

在 `web/templates/base.html` 的 `TOAST_KEY_MAP`（行 128-140）末尾加新 key：

```javascript
      'message.retry_failed': '重试失败：消息状态异常',
      'token.created': 'Token 创建成功',
      'token.revoked': 'Token 已撤销',
      'token.name_invalid': 'Token 名称无效',
      'token.not_found': 'Token 不存在',
      'token.assigned': '已成功分配',
      'token.assign_failed': '分配失败',
      'token.owner_set': 'Owner 已设置',
      'token.owner_failed': '设置 Owner 失败'
    };
```

### Step 10.2 — 写 toast key 测试

`tests/test_web_toast_headers.py` 末尾加新 case（参考现有模式）或加到新建的 `tests/test_web_tokens.py` 下：

```python
class TestTokenToastKeys:
    """验证新 toast key 在 JS TOAST_KEY_MAP 中存在。"""

    def test_toast_keys_exist(self) -> None:
        import re
        from pathlib import Path

        content = Path("web/templates/base.html").read_text()
        keys_in_map = re.findall(r"'([^']+)':\s*'", content)
        required_keys = [
            "token.created", "token.revoked", "token.name_invalid",
            "token.not_found", "token.assigned", "token.assign_failed",
            "token.owner_set", "token.owner_failed",
        ]
        for k in required_keys:
            assert k in keys_in_map, f"TOAST_KEY_MAP missing: {k}"
```

### Step 10.3 — 最终验证

```
uv run ruff check .
uv run pyright
uv run pytest -x
```

如果 `ruff` 或 `pyright` 报错，逐项修复。

### Step 10.4 — 验收清单确认

逐项验证 spec §11 验收清单：
- [ ] Token 列表页能看到所有 token（不含明文）
- [ ] 创建 token 表单（name + 7 scope checkbox）
- [ ] 创建后明文一次性显示（session flash）+ 复制按钮
- [ ] Revoke 按钮带 confirm
- [ ] Sub 详情页显示 owner + assigned，可改
- [ ] Adopt 孤儿 sub 一键按钮
- [ ] 所有操作走 admin session，不经 API ownership 层
- [ ] Sidebar 加「API Token」nav item
- [ ] Toast 反馈所有成功/失败场景
- [ ] `tests/test_web_tokens.py` + `tests/test_web_subscription_ownership.py` 通过

### Step 10.5 — commit

```
git add -A && git commit -m "feat(web): TOAST_KEY_MAP extension + final lint/typecheck/test pass"
```

---

## 文件清单总结

### 新增文件（6 个）

| 文件 | 职责 |
|---|---|
| `web/routes/tokens.py` | token CRUD 路由（list / create / revoke） |
| `web/routes/subscription_ownership.py` | sub ownership 路由（modal / assign / unassign / set_owner） |
| `web/templates/tokens.html` | token 列表 + 创建表单 + inline banner |
| `web/templates/_token_modal.html` | HTMX modal partial（sub ownership 编辑） |
| `tests/test_web_tokens.py` | token 管理路由测试 |
| `tests/test_web_subscription_ownership.py` | ownership 路由测试 |

### 修改文件（3 个）

| 文件 | 改动 |
|---|---|
| `web/app.py:254-275` | 注册 `tokens_router` + `sub_ownership_router` |
| `web/templates/base.html` | sidebar 加「API Token」nav item；`TOAST_KEY_MAP` 加 8 个新 key |
| `web/templates/subscriptions.html` | 每 sub 行加 owner badge + 「管理 ownership」按钮 |

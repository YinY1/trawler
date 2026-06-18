# TODO4 · Web 站点访问鉴权 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 Trawler Web UI 加上"Web 用户登录"层：首次启动强制设置管理员密码（setup 流程），之后所有非 `/login`、`/setup`、`/static/*` 路由必须通过账号密码 session 登录才能访问。密码与 session secret 存储在 `data/auth.toml`（与主 config 隔离），改密码时轮转 session secret 让所有旧 session 失效。

**重要命名区分（贯穿全 plan，不可混淆）：**

| 概念 | 文件 / 路由 | 含义 |
|------|-------------|------|
| **Web 用户登录**（本 TODO 新增） | `web/templates/login.html` + `/login` `/logout` `/setup` `/settings/account` | 浏览器访问 Web UI 的人类用户的账号密码登录 |
| **平台凭证登录**（已存在） | `web/templates/platform_auth.html` + `/auth` `/auth/*` | B站/小红书/微博的扫码 / cookie 续期，本质是平台 OAuth/token 管理 |

旧文件 `web/templates/login.html`（平台凭证页）必须**重命名**为 `platform_auth.html`，让出 `login.html` 这个名字给 Web 用户登录页。

**Architecture:** starlette `SessionMiddleware`（itsdangerous 签名 cookie）+ `passlib[bcrypt]` 密码 hash。三层防护：
1. **Setup guard 中间件**：`data/auth.toml` 不存在或无密码 hash 时，所有路由（除 `/setup`、`/static/*`）302 → `/setup`
2. **Login guard 中间件 / dependency**：已 setup 但未登录时，所有路由（除 `/login`、`/setup`、`/static/*`、`/logout`）302 → `/login`
3. **CSRF 防护**：HTMX 默认带 `X-Requested-With: XMLHttpRequest` 头；所有 mutation POST 端点必须校验此头存在（或同源 referer）。简单方案优先，见 Task 9。

**Tech Stack:** Python 3.12, FastAPI + Starlette `SessionMiddleware` + `itsdangerous` + `passlib[bcrypt]`, HTMX + Jinja2, pytest + pytest-asyncio

---

## 已定决策（不再征询）

| # | 问题 | 决策 |
|---|------|------|
| 1 | 用户模型 | **单用户**，一组账号密码（管理员）。username 固定为 `admin`，UI 不暴露 username 输入 |
| 2 | 密码 hash | `passlib[bcrypt]`（ CryptContext，`bcrypt` scheme） |
| 3 | Session | starlette `SessionMiddleware`，签名 cookie，secret_key 存 `data/auth.toml` |
| 4 | secret_key 来源 | 首次 setup 时 `secrets.token_urlsafe(64)` 随机生成并写入；改密码时**轮转**（重新生成），让旧 session 全部失效 |
| 5 | 密码初始化 | 检测 `data/auth.toml` 不存在或无 `admin_password_hash` → 所有路由 302 → `/setup`；setup 完成后写入并跳 `/login`（不直接登录） |
| 6 | 存储位置 | `data/auth.toml`（独立文件，不污染主 `config/config.toml`） |
| 7 | 登录页命名 | 新 Web 用户登录页用 `web/templates/login.html`；旧平台凭证页改名 `web/templates/platform_auth.html` |
| 8 | IP 白名单 | **不做**，所有 IP 都要登录 |
| 9 | Session 失效策略 | 改密码 → 重新生成 session_secret → 所有现有 cookie 失效；session cookie 本身设 `max_age` = `session_max_age_seconds`（默认 7 天） |
| 10 | CSRF 方案 | HTMX 请求校验 `X-Requested-With: XMLHttpRequest` 头存在；非 HTMX 的原生 form POST 同源 referer 校验。简单 header-based，不引入 token 机制 |
| 11 | 登出 | POST `/logout`，清 session，302 → `/login` |
| 12 | 改密码入口 | `/settings/account`（在现有 Settings 下新增），需要先登录。改密码后强制登出当前 session 并跳 `/login` |

---

## 取舍说明（explorer 自行判断后给出，请 review 时确认）

1. **`shared/config.py` 加 `WebAuthConfig` 但不挂到 `Config` dataclass**
   - 用户原文要求"新增 `WebAuthConfig` dataclass"，但实际存储在 `data/auth.toml` 而非主 config.toml。**做法**：定义 `WebAuthConfig` dataclass 放在 `shared/config.py`（集中 dataclass 模式），但**不**加入 `Config` 顶层（避免 `load_config()` 去主 config.toml 读它）。读写由 `web/auth.py` 的 `load_auth_config()` / `save_auth_config()` 独立处理 `data/auth.toml`。
   - `Config` 顶层不动，`_parse_config()` 不动。
2. **`pyproject.toml` 依赖位置**
   - 加入 `[project.optional-dependencies].web` 段（fastapi 已经在那），加 `passlib[bcrypt]>=1.7` 和显式 `itsdangerous>=2.1`。`itsdangerous` 虽然 starlette 会传递依赖，但显式列上避免 starlette 升级时丢失。
3. **CSRF 校验放哪**
   - 写成一个 FastAPI dependency `verify_csrf(request: Request) -> None`，在所有写操作端点（POST/PUT/DELETE）的 router 上用 `dependencies=[Depends(verify_csrf)]` 注入。当前所有写端点都是 POST（参考 `web/routes/auth.py` 的 logout/refresh、settings save），可以统一在 router 级别加。**但** `/login` 和 `/setup` 本身是 POST，不能加 CSRF guard（用户还没登录，没 session），需要走"同源 referer OR HTMX header"二选一的更宽松规则。
   - **简化方案**：CSRF 仅校验"已登录用户的写操作"。`/login` `/setup` 这两个 POST 端点不挂 CSRF dependency。
4. **SessionMiddleware 注册顺序**
   - 必须**在**所有路由 before，所有 exception handler 之后。FastAPI middleware 是 LIFO（后注册的先执行），需要确认 SessionMiddleware 在最外层。具体顺序在 Task 4 实施。
5. **`require_login` 用 dependency 还是 middleware**
   - 用 **middleware**（HTTP middleware），原因：
     - 路由多（dashboard/subscriptions/check/auth/logs/settings），每个 router 都要 `dependencies=[Depends(require_login)]` 太散
     - exception handler 已经在 app 级，统一拦截更一致
   - 中间件逻辑：判断 `request.url.path` 是否在白名单（`/login`、`/setup`、`/logout`、`/static/*`），不在白名单则检查 `request.session.get("logged_in")`，未登录返回 302 → `/login?next=<原始路径>`。
6. **`tests/test_web_auth.py` 已存在（测试平台凭证路由）**
   - 不能动它。新测试用 `test_web_auth_setup.py` / `test_web_auth_login.py` / `test_web_auth_guard.py` / `test_password_hash.py`。但 **现有 `test_web_auth.py` 在引入 login guard 后会全部失败**（GET `/auth` 会被重定向到 `/login`）。**Task 8 必须更新现有测试**，给 client 加上登录 session cookie。
7. **`data/` 目录是否一定存在**
   - `shared/config.py` 的 `GeneralConfig.data_dir` 默认 `./data`，但运行时不一定 mkdir。`save_auth_config()` 写入前必须 `Path("data").mkdir(parents=True, exist_ok=True)`。
8. **session 失效 vs cookie 失效**
   - starlette SessionMiddleware 的 `secret_key` 改变后，旧 cookie 解密失败 → session 自动失效。不需要主动清服务器端状态（本来就没服务器端状态，全在 cookie 里）。
9. **setup 完成后的安全窗口**
   - setup 成功 → 写盘 → 302 → `/login`。这个跳转之间，**不能**直接给浏览器塞 session cookie（必须再走一次登录表单）。这防止 setup 流程被中间人利用绕过密码确认。
10. **`/setup` 在已 setup 后访问**
    - 返回 404 或 302 → `/login`。**决策：302 → `/login`**（已 setup 的环境下 `/setup` 不应存在，但温和重定向比 404 友好）。
11. **`admin` username 是否写入 auth.toml**
    - **不写**。username 固定 `admin`（硬编码常量 `WEB_ADMIN_USERNAME = "admin"` in `web/auth.py`），auth.toml 只存 `admin_password_hash` + `session_secret` + `session_max_age_seconds`。简化文件结构。
12. **测试时如何 mock session**
    - 用 httpx ASGITransport + AsyncClient，先 POST `/login` 拿到 cookie，后续请求带 cookie。或直接用 starlette TestClient 的 session 注入。具体见 Task 5 测试。
13. **`web/routes/web_auth.py` vs 直接在 `web/routes/auth.py` 加**
    - **新建 `web/routes/web_auth.py`**。现有 `web/routes/auth.py` 347 行专门处理平台凭证（命名冲突的根源），把 Web 用户登录路由塞进去会让"auth"二字歧义更严重。新文件名 `web_auth.py` 明确是"Web 站点鉴权"。

---

## 文件映射清单

| 文件 | 操作 | 说明 |
|------|------|------|
| `pyproject.toml` | 改 | `[project.optional-dependencies].web` 加 `passlib[bcrypt]>=1.7`、`itsdangerous>=2.1`、`itsdangerous` 已被 starlette 传递，但显式列上 |
| `shared/config.py` | 改 | 新增 `WebAuthConfig` dataclass（**不挂到 `Config`**），含 `admin_password_hash: str`、`session_secret: str`、`session_max_age_seconds: int = 604800`（7 天） |
| `web/auth.py` | 新建 | auth.toml 读写 / 密码 hash / setup 检测 / require_login middleware factory / verify_csrf dependency |
| `web/app.py` | 改 | 加 `SessionMiddleware`、加 setup guard middleware、加 login guard middleware、注册 `web_auth` 路由 |
| `web/routes/web_auth.py` | 新建 | `/login` `/logout` `/setup` `/settings/account` 路由（HTML + 处理 POST） |
| `web/routes/__init__.py` | 改 | 不需要改（路由在 `web/app.py` 里直接 import 注册） |
| `web/routes/auth.py` | 改 | `TemplateResponse(..., "login.html", ...)` → `"platform_auth.html"`（行 70-74） |
| `web/templates/login.html` | **重命名** → `web/templates/platform_auth.html` | `git mv`，内容不变 |
| `web/templates/login.html` | **新建** | Web 用户登录页（账号密码表单），与旧的无关 |
| `web/templates/setup.html` | 新建 | 首次设密码页（密码 + 确认密码 + 强度提示） |
| `web/templates/account.html` | 新建 | 改密码页（当前密码 + 新密码 + 确认新密码），挂在 `/settings/account` |
| `web/templates/base.html` | 改 | nav 加"退出登录"按钮（仅 logged_in 时显示）；nav 现有项只在登录后才显示 |
| `tests/test_password_hash.py` | 新建 | bcrypt round-trip |
| `tests/test_web_auth_setup.py` | 新建 | setup 流程：未 setup 强制重定向 / setup 成功 / 重复 setup 重定向 |
| `tests/test_web_auth_login.py` | 新建 | 登录成功 / 密码错 / 登出 / 改密码后旧 session 失效 |
| `tests/test_web_auth_guard.py` | 新建 | 未登录访问受保护路由 302 → /login / 白名单路由不重定向 |
| `tests/test_web_auth.py` | 改 | 现有测试在 guard 引入后需要先登录拿 session cookie |

---

## 阶段划分

| 阶段 | Task | 目标 |
|------|------|------|
| **Phase 0 — 准备** | T1 | 依赖 + dataclass 骨架 + 重命名冲突文件 |
| **Phase 1 — 核心模块** | T2, T3 | `web/auth.py`：auth.toml I/O + 密码 hash + setup 检测 |
| **Phase 2 — 路由** | T4, T5 | `web/routes/web_auth.py`：/setup、/login、/logout、/settings/account |
| **Phase 3 — 守卫层** | T6, T7 | SessionMiddleware + setup guard + login guard |
| **Phase 4 — CSRF + UI** | T8, T9, T10 | CSRF dependency + 模板 + base.html nav 改造 |
| **Phase 5 — 收尾** | T11 | 更新现有测试、文档、example |

---

## Task 1: 准备 — 依赖、WebAuthConfig、文件重命名

**TDD: 否**（基础设施变更，无新行为可测）

**文件:**
- `pyproject.toml`
- `shared/config.py`
- `web/templates/login.html` → `web/templates/platform_auth.html`（git mv）
- `web/routes/auth.py`

**改动:**

- [ ] `pyproject.toml`：在 `[project.optional-dependencies].web` 列表里追加：
  ```toml
  "passlib[bcrypt]>=1.7",
  "itsdangerous>=2.1",
  ```
- [ ] `shared/config.py`：在 `# ── 顶层配置 ───` 段**之前**（紧接 `[ NotificationConfig ]` 后），新增 dataclass：
  ```python
  # ── Web 站点访问鉴权 ──────────────────────────────────────────

  @dataclass
  class WebAuthConfig:
      """Web UI 访问鉴权配置。

      存储在 ``data/auth.toml``（独立于主 ``config/config.toml``）。
      username 固定为 ``admin``，本 dataclass 不含 username 字段。
      """

      admin_password_hash: str = ""
      session_secret: str = ""
      session_max_age_seconds: int = 60 * 60 * 24 * 7  # 7 天
  ```
  **不要**把这个 dataclass 加到 `Config` 顶层，**不要**改 `_parse_config()`。
- [ ] `git mv web/templates/login.html web/templates/platform_auth.html`
- [ ] `web/routes/auth.py` 行 70-74：`"login.html"` → `"platform_auth.html"`（auth_page 函数的 TemplateResponse 调用）

**验证:**
- [ ] `uv pip install -e ".[web]"` 安装新依赖成功
- [ ] `uv run python -c "from passlib.context import CryptContext; print('ok')"` 输出 ok
- [ ] `uv run python -c "from itsdangerous import TimestampSigner; print('ok')"` 输出 ok
- [ ] `uv run python -c "from shared.config import WebAuthConfig; print(WebAuthConfig())"` 实例化成功
- [ ] `uv run python -c "from web.app import app"` 无报错（验证模板重命名没破坏 import）
- [ ] `uv run ruff check shared/config.py web/routes/auth.py` — 无 lint
- [ ] `ls web/templates/` 看到有 `platform_auth.html`，没有旧的 `login.html`
- [ ] `uv run pytest tests/test_web_auth.py -x` — 现有平台凭证测试应仍通过（GET `/auth` 现在渲染 `platform_auth.html`）

---

## Task 2: TDD — `tests/test_password_hash.py` 锁定密码 hash 行为

**TDD: 是（先写测试）**

**文件:** `tests/test_password_hash.py`（新建）

**前置:** Task 1 完成（依赖已装）

**改动:**

- [ ] 新建 `tests/test_password_hash.py`，覆盖以下 case（**目标 API**：`hash_password(plain: str) -> str`、`verify_password(plain: str, hashed: str) -> bool`，导入自 `web.auth`）：
  1. `test_hash_password_returns_bcrypt_string` — `hash_password("hello123")` 返回以 `$2` 开头的字符串
  2. `test_verify_password_correct` — `hash_password("hello123")` 后 `verify_password("hello123", h)` 返回 True
  3. `test_verify_password_wrong` — `verify_password("wrong", h)` 返回 False
  4. `test_hash_password_different_each_time` — 同一明文两次 hash 得到不同字符串（bcrypt 自带 salt）
  5. `test_verify_password_accepts_legacy_bcrypt` — 用 `passlib.context.CryptContext(schemes=["bcrypt"])` 直接 generate 的 hash，`verify_password` 也能验证（互操作性）
  6. `test_verify_password_empty_hash_returns_false` — `verify_password("x", "")` 返回 False（不抛异常）
- [ ] 此时运行 `pytest tests/test_password_hash.py` 应**全部 fail**（ImportError：`web.auth` 不存在）

**验证:**
- [ ] `uv run pytest tests/test_password_hash.py -x` — 6 个 case 全 fail with ImportError 或 AttributeError（确认测试在测尚未实现的功能）

---

## Task 3: 实现 `web/auth.py` 的密码 + auth.toml I/O

**TDD: 是（让 Task 2 测试通过）**

**文件:** `web/auth.py`（新建）

**前置:** Task 2 完成（测试已写）

**改动:**

- [ ] 新建 `web/auth.py`，顶部 `from __future__ import annotations` + 标准 import + 第三方 import（`passlib.context.CryptContext`、`tomlkit`）
- [ ] 模块常量：
  ```python
  WEB_ADMIN_USERNAME = "admin"
  AUTH_TOML_PATH = Path("data/auth.toml")
  _pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
  ```
- [ ] 密码函数：
  ```python
  def hash_password(plain: str) -> str:
      return _pwd_context.hash(plain)

  def verify_password(plain: str, hashed: str) -> bool:
      if not hashed:
          return False
      try:
          return _pwd_context.verify(plain, hashed)
      except Exception:
          return False
  ```
- [ ] auth.toml I/O：
  ```python
  def load_auth_config() -> WebAuthConfig:
      """从 data/auth.toml 加载。文件不存在或字段缺失返回默认值。"""
      if not AUTH_TOML_PATH.exists():
          return WebAuthConfig()
      with open(AUTH_TOML_PATH, "rb") as f:
          raw = tomllib.load(f)
      return WebAuthConfig(
          admin_password_hash=raw.get("admin_password_hash", ""),
          session_secret=raw.get("session_secret", ""),
          session_max_age_seconds=raw.get("session_max_age_seconds", 604800),
      )

  def save_auth_config(cfg: WebAuthConfig) -> None:
      """原子写入 data/auth.toml（先写 .tmp 再 rename）。"""
      AUTH_TOML_PATH.parent.mkdir(parents=True, exist_ok=True)
      doc = tomlkit.document()
      doc.add("admin_password_hash", cfg.admin_password_hash)
      doc.add("session_secret", cfg.session_secret)
      doc.add("session_max_age_seconds", cfg.session_max_age_seconds)
      tmp = AUTH_TOML_PATH.with_suffix(".toml.tmp")
      with open(tmp, "w", encoding="utf-8") as f:
          tomlkit.dump(doc, f)
      tmp.replace(AUTH_TOML_PATH)
  ```
- [ ] setup 检测 + 改密码（含 secret 轮转）：
  ```python
  def is_setup_complete() -> bool:
      """data/auth.toml 存在且 admin_password_hash 非空。"""
      cfg = load_auth_config()
      return bool(cfg.admin_password_hash)

  def set_password(plain: str) -> None:
      """更新密码 + 轮转 session_secret（让所有旧 session 失效）。"""
      cfg = load_auth_config()
      cfg.admin_password_hash = hash_password(plain)
      cfg.session_secret = secrets.token_urlsafe(64)
      save_auth_config(cfg)
  ```
- [ ] **不要**在本 task 实现 `require_login` / `verify_csrf`（Task 6/9 做），但要预留函数 stub 让 import 不爆：
  ```python
  # Task 6/9 实现
  def require_login(request): raise NotImplementedError
  def verify_csrf(request): raise NotImplementedError
  ```
  其实更干净：本 task 只写已确定的函数，Task 6/9 再追加。**采取后者：本 task 不写 stub**。

**验证:**
- [ ] `uv run pytest tests/test_password_hash.py -x` — Task 2 的 6 个 case 全 pass
- [ ] `uv run ruff check web/auth.py`
- [ ] `uv run pyright web/auth.py` — 无 error
- [ ] `uv run python -c "from web.auth import hash_password, verify_password, load_auth_config, save_auth_config, is_setup_complete, set_password; print('ok')"`

---

## Task 4: 实现 `/setup` 路由（首次设密码）

**TDD: 是**

**文件:**
- `tests/test_web_auth_setup.py`（新建）
- `web/routes/web_auth.py`（新建）

**前置:** Task 3 完成

**改动:**

- [ ] 新建 `tests/test_web_auth_setup.py`：
  - fixture：每个 test 前 `rm -f data/auth.toml`（用 `tmp_path` + monkeypatch `web.auth.AUTH_TOML_PATH`）
  - case：
    1. `test_setup_page_returns_200_when_not_setup` — 未 setup 时 GET `/setup` 返回 200，HTML 含 "首次设置密码"
    2. `test_setup_post_success_writes_auth_toml` — POST `/setup` 带 `password` + `password_confirm` → 302 `/login`；`data/auth.toml` 文件存在；文件含 `admin_password_hash` 非空且以 `$2` 开头；含 `session_secret` 长度 > 40
    3. `test_setup_post_password_mismatch` — 两次密码不一致 → 200 + 错误提示；auth.toml 不写入
    4. `test_setup_post_password_too_short` — 密码 < 8 字符 → 200 + 错误提示；auth.toml 不写入
    5. `test_setup_page_redirects_when_already_setup` — 先 POST `/setup` 设密码，再 GET `/setup` → 302 `/login`
    6. `test_setup_post_redirects_when_already_setup` — 已 setup 时 POST `/setup` → 302 `/login`（不覆盖现有密码）
- [ ] 运行测试确认全 fail（路由还没实现）
- [ ] 新建 `web/routes/web_auth.py`：
  ```python
  from __future__ import annotations

  import logging
  from fastapi import APIRouter, Form, Request
  from fastapi.responses import HTMLResponse, RedirectResponse

  from web.app import TEMPLATES
  from web.auth import is_setup_complete, set_password

  router = APIRouter()
  logger = logging.getLogger(__name__)

  MIN_PASSWORD_LENGTH = 8

  @router.get("/setup", response_class=HTMLResponse)
  async def setup_page(request: Request) -> HTMLResponse | RedirectResponse:
      if is_setup_complete():
          return RedirectResponse("/login", status_code=302)
      return TEMPLATES.TemplateResponse(request, "setup.html", {})

  @router.post("/setup")
  async def setup_submit(
      request: Request,
      password: str = Form(...),
      password_confirm: str = Form(...),
  ) -> HTMLResponse | RedirectResponse:
      if is_setup_complete():
          return RedirectResponse("/login", status_code=302)
      # 校验
      errors: list[str] = []
      if len(password) < MIN_PASSWORD_LENGTH:
          errors.append(f"密码至少 {MIN_PASSWORD_LENGTH} 个字符")
      if password != password_confirm:
          errors.append("两次输入的密码不一致")
      if errors:
          return TEMPLATES.TemplateResponse(
              request, "setup.html", {"errors": errors}, status_code=400,
          )
      set_password(password)
      logger.info("🔑 Web 管理员密码已初始化")
      return RedirectResponse("/login", status_code=303)
  ```
  注意：POST 后跳转用 303（See Other），让浏览器用 GET 重新拿 `/login`。

**验证:**
- [ ] `uv run pytest tests/test_web_auth_setup.py -x` — 全 pass（**注意**：此时还没注册路由到 app.py，测试需要 import router 直接 mount 到 TestClient 或临时 mount。**做法**：测试用 `from web.app import create_app` 然后 `app.include_router(web_auth_router)` 在 fixture 里；或者直接 import `web.routes.web_auth.router`，用 starlette TestClient 限定 router scope。**推荐**：等 Task 6 一起把 router 注册到 app.py，本 task 测试可以**临时**注册到 app.py 测试通过后撤销。**简化决策**：本 task **就注册到 app.py**（顺手把 import 加上、`app.include_router(web_auth_router)` 加上），让 Task 6 只聚焦 middleware。）
- [ ] `uv run ruff check web/routes/web_auth.py`
- [ ] `uv run pyright web/routes/web_auth.py`

**实施提示:** 顺便在 `web/app.py` 里加：
```python
from web.routes.web_auth import router as web_auth_router
app.include_router(web_auth_router)
```

---

## Task 5: 实现 `/login`、`/logout`、`/settings/account` 路由

**TDD: 是**

**文件:**
- `tests/test_web_auth_login.py`（新建）
- `web/routes/web_auth.py`（追加）

**前置:** Task 4 完成

**改动:**

- [ ] 新建 `tests/test_web_auth_login.py`：
  - fixture：monkeypatch `AUTH_TOML_PATH` 到 tmp，先 setup 一个固定密码（直接调 `set_password("test12345")`，不走 HTTP）
  - case：
    1. `test_login_page_returns_200` — GET `/login` 200，含密码输入框
    2. `test_login_success_sets_session` — POST `/login` 带正确密码 → 303 `/`；response 的 set-cookie 含 `session=...`；后续 GET `/` 带 cookie 不被重定向
    3. `test_login_wrong_password` — POST 错误密码 → 200 + "密码错误"；不写 session cookie
    4. `test_logout_clears_session` — 先 login 拿 cookie，POST `/logout` → 303 `/login`；后续 GET `/` 不带新 cookie 时 302 `/login`
    5. `test_account_page_requires_login` — 未登录 GET `/settings/account` → 302 `/login`
    6. `test_account_change_password_success` — 登录后 POST `/settings/account` 带正确的 `current_password` + 新密码 + 确认 → 303 `/login`（强制登出）；auth.toml 中 hash 变化
    7. `test_account_change_password_wrong_current` — `current_password` 错 → 200 + "当前密码错误"
    8. `test_account_change_password_invalidates_old_session` — 登录拿 cookie A，改密码，用 cookie A 再访问 `/` → 302 `/login`（session_secret 轮转导致旧 cookie 失效）
- [ ] 在 `web/routes/web_auth.py` 追加：
  ```python
  from web.auth import verify_password, set_password, load_auth_config

  @router.get("/login", response_class=HTMLResponse)
  async def login_page(request: Request) -> HTMLResponse:
      # 已登录访问 /login 也跳走（避免重复登录）
      if request.session.get("logged_in"):
          return RedirectResponse("/", status_code=302)
      return TEMPLATES.TemplateResponse(request, "login.html", {})

  @router.post("/login")
  async def login_submit(
      request: Request,
      password: str = Form(...),
  ) -> HTMLResponse | RedirectResponse:
      cfg = load_auth_config()
      if not cfg.admin_password_hash or not verify_password(password, cfg.admin_password_hash):
          return TEMPLATES.TemplateResponse(
              request, "login.html", {"error": "密码错误"}, status_code=401,
          )
      request.session["logged_in"] = True
      request.session["username"] = WEB_ADMIN_USERNAME
      logger.info("🔑 Web 管理员登录成功")
      # 支持 ?next= 参数跳回原页
      next_url = request.query_params.get("next") or "/"
      return RedirectResponse(next_url, status_code=303)

  @router.post("/logout")
  async def logout(request: Request) -> RedirectResponse:
      request.session.clear()
      return RedirectResponse("/login", status_code=303)

  @router.get("/settings/account", response_class=HTMLResponse)
  async def account_page(request: Request) -> HTMLResponse:
      return TEMPLATES.TemplateResponse(request, "account.html", {})

  @router.post("/settings/account")
  async def account_change_password(
      request: Request,
      current_password: str = Form(...),
      new_password: str = Form(...),
      new_password_confirm: str = Form(...),
  ) -> HTMLResponse | RedirectResponse:
      cfg = load_auth_config()
      errors: list[str] = []
      if not verify_password(current_password, cfg.admin_password_hash):
          errors.append("当前密码错误")
      if len(new_password) < MIN_PASSWORD_LENGTH:
          errors.append(f"新密码至少 {MIN_PASSWORD_LENGTH} 个字符")
      if new_password != new_password_confirm:
          errors.append("两次输入的新密码不一致")
      if errors:
          return TEMPLATES.TemplateResponse(
              request, "account.html", {"errors": errors}, status_code=400,
          )
      set_password(new_password)  # 同时轮转 session_secret
      logger.info("🔑 Web 管理员密码已修改，所有旧 session 已失效")
      # 强制登出当前用户：清 session + 跳 /login
      request.session.clear()
      return RedirectResponse("/login", status_code=303)
  ```

**验证:**
- [ ] `uv run pytest tests/test_web_auth_login.py -x` — 8 个 case 全 pass
- [ ] `uv run ruff check web/routes/web_auth.py`
- [ ] `uv run pyright web/routes/web_auth.py`

---

## Task 6: 实现 SessionMiddleware + setup/login guard middleware

**TDD: 是（用 Task 7 的 guard 测试驱动）**

**文件:**
- `web/app.py`（改）
- `web/auth.py`（追加 middleware factory）

**前置:** Task 5 完成（但路由能跑测试还需要 middleware 装上，所以 Task 5/6/7 实际上是循环驱动的。**实施建议**：先写 Task 7 的 guard 测试 → Task 6 实现 middleware 让测试通过）

**改动:**

- [ ] `web/app.py` 在 `create_app()` 里加 middleware（**顺序很关键**，FastAPI middleware LIFO）：
  ```python
  from starlette.middleware.sessions import SessionMiddleware
  from web.auth import load_auth_config, is_setup_complete

  # SessionMiddleware 必须先加（最后执行，最外层包装）
  auth_cfg = load_auth_config()
  # 如果 session_secret 为空（未 setup），用一个占位 secret（任何请求都会被 setup guard 拦下）
  secret = auth_cfg.session_secret or "SETUP_INCOMPLETE_PLACEHOLDER"
  app.add_middleware(
      SessionMiddleware,
      secret_key=secret,
      session_cookie="trawler_session",
      max_age=auth_cfg.session_max_age_seconds,
      same_site="lax",
      https_only=False,  # 默认本地 127.0.0.1，HTTPS 部署时改
  )
  ```
  **关键 gotcha**：`secret_key` 在 app 创建时固定，改密码后需要重启进程才能让新 secret 生效。但 starlette SessionMiddleware 是 instance attribute，**可以**在改密码后立即更新（但实践中重启更安全）。**决策**：改密码后强制跳 `/login` 且文档里写明"建议重启 Web 服务"。测试 case 8 验证旧 cookie 在 secret 变化后失效，**测试时手动替换 middleware 的 secret_key**。
- [ ] `web/app.py` 加 setup guard + login guard（合并成一个 HTTP middleware，按 path 分支）：
  ```python
  from starlette.types import ASGIApp, Receive, Scope, Send
  from starlette.requests import Request
  from starlette.responses import RedirectResponse

  PUBLIC_PATHS = {"/login", "/logout", "/setup"}
  PUBLIC_PREFIXES = ("/static",)

  @app.middleware("http")
  async def auth_guard(request: Request, call_next):
      path = request.url.path
      # 静态资源 / 公开端点放行
      if path in PUBLIC_PATHS or any(path.startswith(p) for p in PUBLIC_PREFIXES):
          return await call_next(request)
      # setup guard：未初始化时强制跳 /setup
      if not is_setup_complete():
          return RedirectResponse("/setup", status_code=302)
      # login guard：未登录时跳 /login?next=<path>
      if not request.session.get("logged_in"):
          login_url = f"/login?next={path}"
          return RedirectResponse(login_url, status_code=302)
      return await call_next(request)
  ```
- [ ] 不需要在 `web/auth.py` 写 `require_login` 函数（middleware 已覆盖）。如果某些 router 额外需要 dependency（比如内部 API），可以后续加。**本 plan 不引入 `require_login` 函数**，统一走 middleware。

**验证:**
- [ ] `uv run pytest tests/test_web_auth_setup.py tests/test_web_auth_login.py -x` — 通过
- [ ] `uv run ruff check web/app.py`
- [ ] `uv run pyright web/app.py`
- [ ] 手动启 `uv run python run_web.py --port 8080`，浏览器访问 `http://127.0.0.1:8080/` → 应被重定向到 `/setup`

---

## Task 7: TDD — Guard 测试 `tests/test_web_auth_guard.py`

**TDD: 是（先写）**

**文件:** `tests/test_web_auth_guard.py`（新建）

**前置:** Task 6 完成

**改动:**

- [ ] 新建 `tests/test_web_auth_guard.py`：
  - fixture：两个 client — 一个 setup 完成 + 已登录，一个 setup 未完成
  - case：
    1. `test_unprotected_path_static` — GET `/static/tokens.css` 不被重定向（200 或 404 但非 302）
    2. `test_unprotected_path_login_get` — 未登录 GET `/login` 200
    3. `test_unprotected_path_setup_get_when_not_setup` — 未 setup GET `/setup` 200
    4. `test_protected_path_dashboard_redirects_when_not_logged_in` — 已 setup 但未登录 GET `/` → 302 `/login?next=/`
    5. `test_protected_path_dashboard_redirects_to_next_param` — 验证 `next` 参数透传
    6. `test_protected_path_auth_redirects_when_not_logged_in` — GET `/auth` → 302 `/login?next=/auth`
    7. `test_protected_path_subscriptions_redirects` — GET `/subscriptions` → 302
    8. `test_protected_path_after_login_accessible` — 登录后 GET `/` 200
    9. `test_force_setup_redirect_when_not_initialized` — 未 setup 时 GET `/` → 302 `/setup`（不是 `/login`）
    10. `test_force_setup_redirect_for_protected_paths` — 未 setup 时 GET `/auth`、`/settings`、`/subscriptions` 全部 302 `/setup`
- [ ] 测试已 setup 的 fixture 走真实 HTTP setup 流程（先 POST `/setup`），不要绕过 middleware

**验证:**
- [ ] `uv run pytest tests/test_web_auth_guard.py -x` — 全 pass

---

## Task 8: 更新现有 `tests/test_web_auth.py`（适配 login guard）

**TDD: 否**（适配现有测试）

**文件:** `tests/test_web_auth.py`（改）

**前置:** Task 6 完成（middleware 已生效）

**改动:**

- [ ] 现状：`tests/test_web_auth.py` 用 `client = AsyncClient(transport=ASGITransport(app=app))`，所有 GET `/auth` 会被 middleware 拦下返回 302。
- [ ] 加 fixture `logged_in_client`：
  ```python
  @pytest.fixture
  async def logged_in_client(monkeypatch, tmp_path) -> AsyncClient:
      # 1. redirect AUTH_TOML_PATH to tmp
      monkeypatch.setattr("web.auth.AUTH_TOML_PATH", tmp_path / "auth.toml")
      # 2. set a known password
      from web.auth import set_password
      set_password("test12345")
      # 3. login via HTTP to get session cookie
      transport = ASGITransport(app=app)
      async with AsyncClient(transport=transport, base_url="http://test") as c:
          resp = await c.post("/login", data={"password": "test12345"}, follow_redirects=False)
          assert resp.status_code == 303
          # cookie 自动存在 c 里
          yield c
  ```
- [ ] 所有现有 test method 把 `client` 参数换成 `logged_in_client`
- [ ] 注意：`web.app.app` 是 module-level singleton，middleware 已经装上。如果 monkeypatch `AUTH_TOML_PATH` 时机晚于 app 创建，`load_auth_config()` 在 middleware 内每次请求时读最新值（因为 `is_setup_complete()` 每次调 `load_auth_config()`），所以 monkeypatch 应该生效。**验证**：测试运行后观察。
- [ ] **额外注意**：SessionMiddleware 的 `secret_key` 是在 app 创建时固定读取的。测试中如果 `set_password` 轮转了 secret，而 app 实例的 middleware 还用旧 secret，会导致 cookie 验签失败。**对策**：测试 fixture 中 `set_password` 是首次设置，secret 从 placeholder 变成真实值，但 app 启动时是 placeholder，cookie 用 placeholder 签的会失效。**修正方案**：
  - **方案 A**（推荐）：测试用 `create_app()` 重新构造 app 实例，让 middleware 读最新 secret。`pytest` fixture 里 `from web.app import create_app; app = create_app()`。
  - **方案 B**：测试中先 monkeypatch + 写 auth.toml，再 `import` app（用 importorskip / reload）。
  - **决策**：用方案 A。改 `tests/test_web_auth.py` 和新测试都用 `app = create_app()`（在 fixture 内构造）。
- [ ] 改完后跑 `uv run pytest tests/test_web_auth.py -x` 全 pass

**验证:**
- [ ] `uv run pytest tests/test_web_auth.py -x` — 现有平台凭证测试在 guard 下仍 pass
- [ ] `uv run pytest tests/ -x` — 全套测试不退化

---

## Task 9: CSRF 防护 dependency

**TDD: 是**

**文件:**
- `tests/test_web_csrf.py`（新建）
- `web/auth.py`（追加 `verify_csrf`）
- 所有写端点 router（用 `dependencies=[Depends(verify_csrf)]`）

**前置:** Task 6 完成

**改动:**

- [ ] 新建 `tests/test_web_csrf.py`：
  - case：
    1. `test_post_without_header_blocked_when_logged_in` — 登录后 POST `/settings/account` 不带 `X-Requested-With` 头 → 403
    2. `test_post_with_htmx_header_passes` — 同上但带 `X-Requested-With: XMLHttpRequest` → 通过 CSRF check（即使业务校验失败也是 400 不是 403）
    3. `test_post_with_same_origin_referer_passes` — 带 `Referer: http://test/...`（同源）→ 通过
    4. `test_post_with_cross_origin_referer_blocked` — 带 `Referer: http://evil.com/...` → 403
    5. `test_login_post_not_blocked_by_csrf` — POST `/login`（未登录）不带任何特殊头 → 不被 CSRF 拦（业务返回 401 密码错，但不是 403 CSRF）
    6. `test_setup_post_not_blocked_by_csrf` — POST `/setup`（未登录）不被 CSRF 拦
- [ ] `web/auth.py` 追加：
  ```python
  from fastapi import Request, HTTPException

  def verify_csrf(request: Request) -> None:
      """简单 CSRF 防护：HTMX 请求 OR 同源 referer。

      - 已登录用户的写操作（POST/PUT/DELETE）必须通过此 dependency。
      - /login /setup 本身不挂此 dependency（用户还没登录，没 session 可盗）。
      - HTMX 默认带 X-Requested-With: XMLHttpRequest 头。
      - 非 HTMX 表单需要浏览器自动带的 Referer（同源校验）。
      """
      # 只对写方法生效
      if request.method in ("GET", "HEAD", "OPTIONS"):
          return
      # 1. HTMX header
      if request.headers.get("x-requested-with") == "XMLHttpRequest":
          return
      # 2. 同源 referer
      referer = request.headers.get("referer", "")
      host = request.headers.get("host", "")
      if referer and host:
          # referer 形如 http://host/path...；校验 host 部分
          try:
              from urllib.parse import urlparse
              parsed = urlparse(referer)
              if parsed.netloc == host:
                  return
          except Exception:
              pass
      raise HTTPException(status_code=403, detail="CSRF check failed")
  ```
- [ ] 在所有"已登录用户写操作"的 router 上挂 dependency：
  - `web/routes/web_auth.py`：`/logout`、`/settings/account`（**不挂** `/login`、`/setup`）
    - **做法**：把 `/logout` `/settings/account` 拆到一个新的子-router 上，或单独加 `dependencies=[Depends(verify_csrf)]` 到每个装饰器
  - `web/routes/auth.py`：`/auth/logout/{platform}`、`/auth/refresh/{platform}`（platform 操作也算写）
  - `web/routes/settings.py`：所有 POST 端点（参考现有代码）
  - `web/routes/check.py`：所有 POST 端点
  - `web/routes/subscriptions.py`：所有 POST/DELETE 端点
- [ ] **简化做法**：不在每个端点单独挂，而是在 `web/app.py` 里用一个全局 HTTP middleware 拦截所有 POST/PUT/DELETE：
  ```python
  @app.middleware("http")
  async def csrf_guard(request: Request, call_next):
      # 公开端点（/login /setup）豁免
      if request.url.path in ("//login", "/setup") or request.url.path.startswith("/static"):
          return await call_next(request)
      # 只校验写方法
      if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
          return await call_next(request)
      # 已登录用户的写操作才校验（未登录的会被 auth_guard 拦下，到不了这里）
      # 但 /login /setup 是未登录的 POST，已豁免
      # ... 复用 verify_csrf 逻辑 ...
      from web.auth import verify_csrf
      try:
          verify_csrf(request)
      except HTTPException as e:
          return JSONResponse(status_code=e.status_code, content={"detail": e.detail})
      return await call_next(request)
  ```
  **决策**：用**全局 middleware**（更一致，覆盖未来新增端点）。但要在 auth_guard 之**前**（后注册先执行）：csrf_guard 先注册（auth_guard 后注册 → 后执行 → 更外层）。这样执行链：csrf_guard → auth_guard → route。**等一下**，CSRF 只对已登录用户有意义，未登录用户的 POST 应被 auth_guard 拦下。所以执行顺序：**auth_guard 先（更外层）→ csrf_guard 后（更内层）**。注册顺序：**csrf_guard 先注册，auth_guard 后注册**（FastAPI LIFO）。

  **再想想**：auth_guard 拦下未登录 GET → 302。POST `/login` 不在豁免列表？要加豁免。重新整理：
  - `auth_guard` 豁免：`/login`、`/logout`、`/setup`、`/static/*`
  - `csrf_guard` 豁免：`/login`、`/setup`（**不豁免** `/logout`，因为 logout 应防 CSRF）、`/static/*`
  - `csrf_guard` 仅对写方法生效
  - 执行顺序（外到内）：`csrf_guard` → `auth_guard` → route。注册顺序（FastAPI LIFO，先注册的在外）：先 `app.add_middleware(csrf_guard)`、后 `app.add_middleware(auth_guard)`。
  
  **但**：未登录 POST `/login`（密码错）走 csrf_guard 时被豁免 → 进入 auth_guard（豁免） → 路由。✓
  未登录 POST `/settings/account` → csrf_guard 拦（不在豁免，且没有有效 header/referer）→ 403。但**应该**是 302 → `/login`（让用户先登录）。**矛盾**：csrf_guard 在 auth_guard 外层，未登录就先被 csrf_guard 拦死。
  
  **修正决策**：csrf_guard 放 auth_guard **内层**（先注册 auth_guard 外层，后注册 csrf_guard 内层）。注册顺序：先 `auth_guard`，后 `csrf_guard`。这样：
  - 未登录任何请求 → auth_guard 拦下（302 /setup 或 /login），到不了 csrf_guard。
  - 已登录写请求 → 过 auth_guard → csrf_guard 校验。
  - `/login` POST → 过 auth_guard（豁免）→ csrf_guard（豁免）→ 路由。✓

  **最终顺序**：`web/app.py` 中先 `add_middleware(SessionMiddleware)`（最外层），再注册 `auth_guard` middleware，再注册 `csrf_guard` middleware。

**验证:**
- [ ] `uv run pytest tests/test_web_csrf.py -x` — 全 pass
- [ ] `uv run pytest tests/ -x` — 全套不退化（注意：现有测试的 POST 请求会需要带 HTMX 头，**测试 fixture 需要更新**：所有 client POST 都加 `headers={"X-Requested-With": "XMLHttpRequest"}`。这影响 `test_web_settings.py` / `test_web_check.py` / `test_web_subscriptions.py` / `test_web_auth.py`。**追加到 Task 11 处理**）

---

## Task 10: 模板 — `login.html` / `setup.html` / `account.html` + `base.html` 改造

**TDD: 否**（UI 模板）

**文件:**
- `web/templates/login.html`（新建，占用此名）
- `web/templates/setup.html`（新建）
- `web/templates/account.html`（新建）
- `web/templates/base.html`（改）

**前置:** Task 5、6 完成（路由能渲染模板）

**改动:**

- [ ] `web/templates/login.html`：
  - **不继承** `base.html`（登录页不需要 sidebar nav），独立 layout
  - Apple soft 风格：居中卡片，圆角 14px，subtle shadow，Tailwind + `apple-blue` 主色
  - 表单：单 `<input type="password" name="password">` + 提交按钮
  - 表单 POST `/login`，`accept-charset="UTF-8"`
  - 错误展示：`{% if error %}<div class="text-red-500 ...">{{ error }}</div>{% endif %}`
  - 支持 `?next=` 透传（form action 用 `{{ request.url.path }}?{{ request.url.query }}`）
  - 暗色模式（`dark:` Tailwind 类，与现有 tokens.css 一致）
- [ ] `web/templates/setup.html`：
  - 同样独立 layout（不继承 base）
  - 标题 "首次设置 · 创建管理员密码"
  - 两个 password input：`password` + `password_confirm`
  - 强度提示文字（最小 8 字符）
  - 错误列表 `{% for e in errors %}...{% endfor %}`
- [ ] `web/templates/account.html`：
  - 继承 `base.html`（已登录页）
  - `active_nav = "settings"`（高亮 Settings）
  - 表单：current_password / new_password / new_password_confirm
  - 提交后强制登出 → 用户看到登录页（前端 toast 提示 "密码已修改，请重新登录"）
  - HTMX 提交（带 `X-Requested-With` 头）
- [ ] `web/templates/base.html`：
  - nav 区域加"退出登录"按钮，仅在 `{% if request.session.get("logged_in") %}` 时显示
  - 位置：sidebar 底部（在 Settings 下面），右对齐
  ```html
  {% if request.session.get("logged_in") %}
  <form action="/logout" method="post" class="px-3 mt-4">
    <input type="hidden" name="_csrf" value="">  <!-- 不需要 token，HTMX 头解决 -->
    <button type="submit"
            class="w-full flex items-center gap-3 px-3 py-2 rounded-[8px] text-sm text-[var(--text-secondary)] hover:bg-red-500/10 hover:text-red-500 transition-colors">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/>
        <polyline points="16 17 21 12 16 7"/>
        <line x1="21" y1="12" x2="9" y2="12"/>
      </svg>
      退出登录
    </button>
  </form>
  {% endif %}
  ```
  注意：原生 form POST 需要通过 CSRF。两种方案：
  - **方案 1**：给 button 加 `onclick` 改用 fetch + HTMX 头
  - **方案 2**：用 `<button hx-post="/logout" hx-headers='{"X-Requested-With":"XMLHttpRequest"}'>` HTMX
  - **决策**：方案 2（HTMX），与现有 logout 风格一致。
  
  改成：
  ```html
  <button hx-post="/logout"
          hx-headers='{"X-Requested-With": "XMLHttpRequest"}'
          hx-swap="none"
          class="...">
    退出登录
  </button>
  ```
  服务器返回 303 重定向，HTMX 默认会跟随（`hx-follow` 处理）。或者 logout 端点对 HTMX 请求返回 `HX-Redirect: /login` 头。**决策**：logout 端点统一返回 303（浏览器原生 follow），HTMX 模式下额外加 `HX-Redirect` 头。

**验证:**
- [ ] 手动启动 `uv run python run_web.py`：
  - 未 setup 时访问 `/` → 跳 `/setup`，看到设密码页
  - setup 后跳 `/login`，输入密码 → 跳 `/`（dashboard）
  - 点击 sidebar 底部"退出登录" → 跳 `/login`
- [ ] 暗色模式 toggle（系统级）下页面正常
- [ ] `uv run ruff check .`（模板不改 lint）
- [ ] `uv run pytest tests/ -x` — 全套测试仍通过（现有测试不动模板）

---

## Task 11: 收尾 — 测试 fixture 适配 CSRF、文档、example

**TDD: 否**（适配 + 文档）

**文件:**
- `tests/test_web_auth.py`、`tests/test_web_settings.py`、`tests/test_web_check.py`、`tests/test_web_subscriptions.py`（如有 POST 测试）
- `README.md`（如涉及 Web 启动文档）
- `config/config.toml.example`（不修改主 config，但加注释说明）

**改动:**

- [ ] 全套测试中所有 `client.post(...)` 调用加 `headers={"X-Requested-With": "XMLHttpRequest"}`，统一通过 fixture 注入：
  ```python
  @pytest.fixture
  def htmx_headers():
      return {"X-Requested-With": "XMLHttpRequest"}
  ```
  或者更彻底：在 `logged_in_client` fixture 里 monkeypatch httpx 让它默认带此头。
  - **简单做法**：每个 POST 调用单独加头。grep `client.post` 找到所有点。
- [ ] 在 `config/config.toml.example` 顶部注释块加一行说明：
  ```toml
  # Web 访问鉴权配置位于 data/auth.toml（首次启动 Web 时自动创建）
  # 修改密码请访问 Web UI 的 Settings → 账户
  ```
- [ ] README 或新建 `docs/web-auth.md`（**只在 README 加段，不新建文档**，符合 AGENTS.md "不主动创建文档"）：
  - 启动后第一次访问会被强制设密码
  - 密码存储位置
  - 改密码 → 所有 session 失效
  - 部署 HTTPS 时 `https_only` 需要改 True（指明 `web/app.py` 哪一行）

**验证:**
- [ ] `uv run pytest tests/ -x` — 全套测试 pass
- [ ] `uv run ruff check .`
- [ ] `uv run pyright .`
- [ ] `uv run python run_web.py` 手动跑一遍完整流程：
  1. 删除 `data/auth.toml`（如存在）
  2. 启动 → 浏览器访问 → setup → login → dashboard → 改密码 → 强制登出 → 用新密码登录
  3. 第二次启动（auth.toml 已存在）→ 直接 `/login`

---

## 验收清单（所有 task 完成后）

- [ ] `uv run ruff check .` — 无新增 lint
- [ ] `uv run pyright .` — 无新增 type error
- [ ] `uv run pytest -x` — 全套测试 pass（含现有 + 新增 4 个测试文件）
- [ ] `uv run python run_web.py --port 8080` 启动后：
  - 未 setup → 强制 setup 流程
  - setup 完成 → /login
  - 登录 → 受保护页面可访问
  - 改密码 → 强制登出 → 旧 cookie 失效
- [ ] 所有命名区分清晰：`login.html`（Web 用户）vs `platform_auth.html`（平台凭证），`web/routes/web_auth.py`（Web 用户）vs `web/routes/auth.py`（平台凭证）
- [ ] 无新增 `require_login` / `require_auth` 函数散落在路由里（统一 middleware）
- [ ] CSRF 防护对已登录用户的写操作生效

---

## 风险与回滚

| 风险 | 缓解 |
|------|------|
| SessionMiddleware secret_key 在 app 创建时固定，改密码后不重启就无法轮转 | 文档说明"改密码后建议重启"；测试 case 验证 secret 替换后旧 cookie 失效 |
| CSRF 全局 middleware 误伤现有 POST 测试 | Task 11 显式给所有测试 POST 加 HTMX 头 |
| monkeypatch AUTH_TOML_PATH 在 app singleton 已创建后无效 | 测试用 `create_app()` 每次重建，不依赖 module-level `app` |
| 浏览器原生 form（非 HTMX）无法触发 logout | logout 用 HTMX `hx-post` + `HX-Redirect` 头处理 |
| `data/` 目录权限 | `save_auth_config()` 显式 mkdir，权限默认（同进程用户可读写） |

**回滚策略：** 所有改动可通过 `git revert` 单 PR 回滚。模板重命名（login.html → platform_auth.html）是单向的，回滚时需手动 `git mv` 回去。

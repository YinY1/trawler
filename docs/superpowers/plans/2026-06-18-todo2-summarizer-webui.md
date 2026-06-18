# TODO2 · AI 摘要 API 配置化（移除 codebuddy + Web UI） Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 移除 trawler 对 `codebuddy` CLI 的硬依赖，将 AI 摘要后端完全收敛到 OpenAI 兼容 provider 抽象（OpenAI / DeepSeek / Ollama），并在 Web Settings 页新增 "AI 分析" 配置区块，让用户在 UI 上即可完成 provider 选择、密钥填写、连通性测试与保存。

**Architecture:** `core/summarizer.py` 的 provider 抽象已就绪（`OpenAIProvider` / `LocalFallbackProvider` + `_create_provider()` 工厂），`AnalysisConfig` dataclass 已含 `enabled/provider/api_base/api_key/model_name`。本次改动是"删 + 加 UI"：
- **删**：`CodeBuddyProvider` 类、工厂 codebuddy 分支、`CODEBUDDY_TIMEOUT` 常量、相关 import / 配置默认值 / 注释里的 codebuddy 引用
- **加 UI**：在现有 `web/routes/settings.py` + `web/templates/settings.html` 上新增一个 Card（AI 分析），新增两个 HTMX 端点（`/settings/analysis/save`、`/settings/analysis/test`）
- **不动**：`generate_summary()` 公共接口、cron/web 入口、`LocalFallbackProvider`、`AnalysisConfig` 字段定义

**Tech Stack:** Python 3.12, FastAPI + HTMX + Jinja2 + tomlkit, httpx, pytest + pytest-asyncio

---

## 已定决策（不再征询）

| # | 问题 | 决策 |
|---|------|------|
| 1 | CodeBuddy provider 去留 | **删除**（class + 工厂分支 + 常量 + 默认值 + 注释） |
| 2 | OpenAI 兼容 provider 去留 | **保留**，OpenAI / DeepSeek / Ollama 全部走 `OpenAIProvider` |
| 3 | LocalFallback 去留 | **保留**，AI 失败时的降级，不改 |
| 4 | 后端 provider 抽象 | **不动**，抽象已就绪 |
| 5 | 新增 provider 类型 | **不加** |
| 6 | Web UI 范围 | Settings 页新增 "AI 分析" Card：provider 下拉 / api_base / api_key (password) / model_name / enabled 开关 / 测试按钮 / 保存按钮 |
| 7 | 测试连通性端点 | **必须 mock**，CI 无网络，不能真打 OpenAI |
| 8 | 配置落盘 | 写回 `config/config.toml` 的 `[analysis]` 段，用 tomlkit 保留注释 |

---

## 取舍说明（explorer 自行判断后给出，请 review 时确认）

1. **`shared/config.py:120` 默认值 `provider: str = "codebuddy"`**
   - task 描述只点了 summarizer.py / constants.py / toml example，但 dataclass 默认值仍是 `"codebuddy"`，移除 provider 后会变成无效默认。**必须同步改为 `"openai"`**。
2. **`tests/test_config.py:454` 断言 `assert a.provider == "codebuddy"`**
   - 同步改为 `"openai"`，否则 Task 改完测试立即红。
3. **`shared/protocols.py:216` docstring `# "codebuddy" | "openai" | ...`**
   - 这是注释，不影响运行，但留着 stale。**改为 `# "openai" | "ollama" | "local" | "local-fallback"`**（最小化文本改动）。
4. **`web/templates/_macros.html` 是否需要新增 password 宏**
   - 现有 `field(name, value, label, type="text", ...)` 宏 **已支持 `type="password"`**（settings.html:40-42 在用）。**不需要新增宏**，调用时传 `type="password"` 即可。Task 描述里"如果缺密码字段宏，加一个"的前提不成立。
5. **`config.toml.example` 的 codebuddy 默认**
   - 行 50-51 的注释 + `provider = "codebuddy"` 同步改为 OpenAI/DeepSeek/Ollama 三种示例（注释块形式，但保留单一 `provider = "openai"` 作为默认）。
6. **"测试连通性" 端点的 prompt 选择**
   - 用最短 prompt（如 `"ping"`），max_tokens 设小（如 5），减少误超时；不调用业务 `_SUMMARY_PROMPT_TEMPLATE`。
7. **测试端点是否落盘**
   - **不落盘**。仅做一次 dry-run 调用，结果以 JSON 返回前端 toast。保存端点才落盘。
8. **provider 下拉的取值**
   - 限定 `openai` / `ollama`（与 `_create_provider` 工厂分支一致）。DeepSeek 走 `openai`（OpenAI 兼容），用户在 api_base 里填 DeepSeek endpoint。下拉注释里说明这点。

---

## 文件映射清单

| 文件 | 操作 | 说明 |
|------|------|------|
| `core/summarizer.py` | 改 | 删 `CodeBuddyProvider` 类（行 35-96）、删 `_create_provider` 的 codebuddy 分支（行 408-409）、改 import 行 11 去掉 `CODEBUDDY_TIMEOUT`、删 `asyncio` import（仅 codebuddy 用到） |
| `shared/constants.py` | 改 | 删行 7 `CODEBUDDY_TIMEOUT = 120` |
| `shared/config.py` | 改 | 行 120 默认值 `"codebuddy"` → `"openai"` |
| `shared/protocols.py` | 改 | 行 216 docstring 去掉 `"codebuddy"` |
| `config/config.toml.example` | 改 | 行 50-51 注释 + 默认值 |
| `web/routes/settings.py` | 改 | GET 渲染（已有，传 `config.analysis`）；新增 POST `/settings/analysis/save` 和 POST `/settings/analysis/test` |
| `web/templates/settings.html` | 改 | 在 Card 3（平台启用）前/后插入 Card "AI 分析"；复用 `field` + `toggle` 宏 |
| `tests/test_config.py` | 改 | 行 454 断言改为 `"openai"` |
| `tests/test_summarizer.py` | 新建 | provider 工厂测试（删 codebuddy 后只有 openai/ollama/LocalFallback） |
| `tests/test_web_settings_analysis.py` | 新建 | `/settings/analysis/save` + `/settings/analysis/test` 端点测试（mock provider） |

---

## Task 1: 移除 CodeBuddyProvider 与所有 codebuddy 引用

**TDD: 否**（删除代码，无新行为）

**文件:**
- `core/summarizer.py`
- `shared/constants.py`
- `shared/config.py`
- `shared/protocols.py`
- `config/config.toml.example`
- `tests/test_config.py`

**改动:**

- [ ] `core/summarizer.py`
  - 删行 11 import 里的 `CODEBUDDY_TIMEOUT`：改为 `from shared.constants import LLM_API_TIMEOUT`
  - 删行 5 `import asyncio`（验证：grep `asyncio` 在该文件是否还有别的用处 — 仅 `CodeBuddyProvider.generate` 用到，可删；若 ruff 报未使用，删之）
  - 删行 35-96 整个 `class CodeBuddyProvider` 块（含其上方的 section 注释 `# ── CodeBuddy Provider ──`）
  - 删行 408-409 `_create_provider` 里的 `if provider_name == "codebuddy": return CodeBuddyProvider()` 分支
- [ ] `shared/constants.py`
  - 删行 7 `CODEBUDDY_TIMEOUT = 120  # CodeBuddy CLI 超时`
- [ ] `shared/config.py`
  - 行 120：`provider: str = "codebuddy"` → `provider: str = "openai"`
- [ ] `shared/protocols.py`
  - 行 216：`source: str  # "codebuddy" | "openai" | "ollama" | "local" | "local-fallback"` → `source: str  # "openai" | "ollama" | "local" | "local-fallback"`
- [ ] `config/config.toml.example`
  - 行 50：`# 分析服务提供者：codebuddy / openai / 其他兼容接口` → `# 分析服务提供者：openai / ollama（DeepSeek 等 OpenAI 兼容接口填 openai 并指定 api_base）`
  - 行 51：`provider = "codebuddy"` → `provider = "openai"`
- [ ] `tests/test_config.py`
  - 行 454：`assert a.provider == "codebuddy"` → `assert a.provider == "openai"`

**验证:**
- [ ] `uv run ruff check core/summarizer.py shared/constants.py shared/config.py shared/protocols.py` — 无 unused import / 未定义引用
- [ ] `uv run pyright core/summarizer.py shared/config.py` — 无 error
- [ ] `uv run pytest tests/test_config.py -x` — 通过（断言已改）
- [ ] `grep -ri codebuddy core/ shared/ config/config.toml.example tests/test_config.py` — 应无匹配

---

## Task 2: TDD — `tests/test_summarizer.py` 锁定 provider 工厂新行为

**TDD: 是（先写测试）**

**文件:** `tests/test_summarizer.py`（新建）

**前置:** Task 1 已完成（工厂不再有 codebuddy 分支）

**改动:**

- [ ] 新建 `tests/test_summarizer.py`，覆盖以下 case：
  1. `test_create_provider_openai` — `AnalysisConfig(provider="openai", api_base="https://api.openai.com/v1", api_key="sk-x", model_name="gpt-4o-mini")` → 返回 `OpenAIProvider` 实例，且字段对得上
  2. `test_create_provider_openai_missing_api_base_raises` — `api_base=""` → `pytest.raises(ValueError)`
  3. `test_create_provider_ollama_default_api_base` — `provider="ollama", api_base=""` → 返回 `OpenAIProvider`，`api_base == "http://localhost:11434/v1"`
  4. `test_create_provider_ollama_custom_api_base` — `provider="ollama", api_base="http://my-host:11434/v1"` → 用自定义
  5. `test_create_provider_unknown_raises` — `provider="foobar"` → `pytest.raises(ValueError, match="不支持的 provider")`
  6. `test_create_provider_codebuddy_removed` — `provider="codebuddy"` → `pytest.raises(ValueError)`，确认旧值不再被特殊处理
  7. `test_create_provider_case_insensitive` — `provider="OpenAI"`（大写）→ 不报错（工厂已 `.lower().strip()`）
  8. `test_local_fallback_provider_basic` — `LocalFallbackProvider().generate("标题：x\n作者：y\n正文：这是测试内容。需要足够长的句子。")` → 返回非空 str

**验证:**
- [ ] `uv run pytest tests/test_summarizer.py -x` — 全部通过（Task 1 改完后这些 case 应直接绿；如果 Task 1 漏改工厂，case 1/3/6 会失败）

---

## Task 3: 新增 `/settings/analysis/test` 端点（带 mock）

**TDD: 是（先写测试）**

**文件:**
- `web/routes/settings.py`
- `tests/test_web_settings_analysis.py`（新建，本 task 写 test 部分的 test 端点 case）

**改动:**

- [ ] 在 `tests/test_web_settings_analysis.py` 新建（先写测试）：
  - fixture `client`：复用 `test_web_settings.py` 的 `ASGITransport + AsyncClient` 模式
  - `test_analysis_test_success`：
    - `patch("web.routes.settings._probe_provider", new_callable=AsyncMock)` 返回 `{"ok": True, "message": "..."}`
    - POST `/settings/analysis/test` form: `provider=openai&api_base=https://api.openai.com/v1&api_key=sk-x&model_name=gpt-4o-mini`
    - 断言 status 200，body JSON `ok: true`
  - `test_analysis_test_failure`：
    - mock 返回 `{"ok": False, "message": "连接失败: ..."}`
    - 断言 status 200（端点本身不抛，错误信息走 body）+ `ok: false`
  - `test_analysis_test_invalid_provider`：
    - form `provider=codebuddy` → status 200，body `ok: false, message 含 "不支持的 provider"`
- [ ] 在 `web/routes/settings.py` 实现：
  - 引入 `from fastapi.responses import JSONResponse`（如未引入）
  - 新增模块级 helper `async def _probe_provider(provider: str, api_base: str, api_key: str, model_name: str) -> dict[str, Any]`：
    - 构造临时 `AnalysisConfig`（不写盘），调 `_create_provider()` 拿 provider 实例
    - 用极短 prompt（如 `"ping"`）调 `await provider.generate("ping")`
    - try/except 包住：成功返回 `{"ok": True, "message": f"连通正常，模型响应: {resp[:50]}"}`；失败返回 `{"ok": False, "message": str(e)}`
    - `ValueError`（不支持的 provider）也走 `{"ok": False}` 分支，message 保持原 error text（"不支持的 provider: ..." — **不要改 error text**）
  - 新增路由 `@router.post("/settings/analysis/test")`：
    - 入参 `provider/api_base/api_key/model_name` 用 `Form(...)`
    - 调 `_probe_provider()`，返回 `JSONResponse(result)`（始终 200，结果由 body 表达）
  - **不写盘**、**不动 config.toml**

**验证:**
- [ ] `uv run pytest tests/test_web_settings_analysis.py::test_analysis_test_success -xvs`
- [ ] `uv run pytest tests/test_web_settings_analysis.py::test_analysis_test_failure -xvs`
- [ ] `uv run pytest tests/test_web_settings_analysis.py::test_analysis_test_invalid_provider -xvs`
- [ ] `uv run ruff check web/routes/settings.py tests/test_web_settings_analysis.py`

**关键约束:**
- 测试必须 mock `_probe_provider` 或更底层的 `provider.generate`，**不能**真打 OpenAI（CI 无网络）
- `_probe_provider` 是模块级函数，方便单测 monkeypatch

---

## Task 4: 新增 `/settings/analysis/save` 端点

**TDD: 是**

**文件:**
- `web/routes/settings.py`
- `tests/test_web_settings_analysis.py`

**改动:**

- [ ] 先在测试文件加 case：
  - `test_analysis_save_writes_toml`：
    - `patch("web.routes.settings.Path.exists", return_value=True)`
    - `patch("web.routes.settings.Path.write_text")` 验证被调用一次
    - `patch("tomlkit.dumps", return_value="")` 防止真写
    - 准备一个 `tomlkit.parse` mock 返回带 `[analysis]` 段的 document（用 `tomlkit.parse("[analysis]\nenabled = true\nprovider = \"old\"\n")`）
    - POST `/settings/analysis/save` form: `enabled=true&provider=openai&api_base=https://api.openai.com/v1&api_key=sk-x&model_name=gpt-4o-mini`
    - 断言 status 200、`HX-Trigger` 含 toast
    - 断言 `tomlkit.dumps` 被调用，且传给 `write_text` 的内容（通过抓取 mock 的 tomlkit document 验证 `doc["analysis"]["provider"] == "openai"` 等）
  - `test_analysis_save_creates_section_if_missing`：
    - 同上但初始 document 无 `[analysis]` 段，验证写入后新增
  - `test_analysis_save_empty_api_key_is_allowed`：
    - Ollama 场景 api_key 可空，验证不报错
  - `test_analysis_save_disabled_flag`：
    - `enabled=false`（form 不传 enabled 字段时也应正确处理为 false）

- [ ] 在 `web/routes/settings.py` 实现 `settings_analysis_save`：
  - 路由 `@router.post("/settings/analysis/save")`
  - Form 参数：`enabled: bool = Form(False)`, `provider: str = Form(...)`, `api_base: str = Form("")`, `api_key: str = Form("")`, `model_name: str = Form("")`
  - 复用现有 tomlkit 读写模式（参考 `settings_save` 行 40-68）：
    - 读 `config/config.toml`（不存在则空 document）
    - `raw.setdefault("analysis", tomlkit.table())` 后赋 5 个字段
    - `tomlkit.dumps` 写回，保留其他段注释
  - 返回与 `settings_save` 一致的 `HX-Trigger` toast：`{"toast":{"key":"settings.saved","type":"success"}}`（**复用现有 key，不新增 i18n key**）

**验证:**
- [ ] `uv run pytest tests/test_web_settings_analysis.py -k save -xvs`
- [ ] 手动 review：tomlkit 写入是否会破坏既有 `[analysis]` 上方/下方的注释（tomlkit 默认保留）

---

## Task 5: Settings 模板新增 "AI 分析" Card

**TDD: 否**（UI，由集成测试 Task 6 兜底渲染不报错）

**文件:** `web/templates/settings.html`

**前置:** Task 3 + 4 端点已实现

**改动:**

- [ ] 在 Card 2（Gotify 通知）和 Card 3（平台启用）之间插入新 Card "AI 分析"，复用现有 Apple soft 风格：
  - Card 外壳 class 完全沿用（`bg-[var(--card-bg)] backdrop-blur-[12px] rounded-[14px] p-5 shadow-card border border-[var(--card-border)] mb-4`）
  - Header icon 用一个简单的 SVG（如 sparkles 或 cpu 图标），class 沿用 `w-7 h-7 rounded-[8px] bg-apple-blue/10 text-apple-blue`
  - 字段布局参考 Card 1（`grid grid-cols-1 md:grid-cols-2 gap-4`）：
    - `enabled` 用 `{{ toggle("analysis_enabled", config.analysis.enabled, "启用 AI 分析") }}`（占一整行，独占一行更直观）
    - `provider` 用原生 `<select>`（HTMX 表单提交需要 `name` 属性），options: `openai` / `ollama`，默认按 `config.analysis.provider` 选中。select 的 class 沿用 input 风格
    - `api_base` 用 `{{ field("analysis_api_base", config.analysis.api_base, "API Base", placeholder="https://api.openai.com/v1", width="full") }}`
    - `api_key` 用 `{{ field("analysis_api_key", config.analysis.api_key, "API Key", type="password", placeholder="sk-...", width="full") }}`
    - `model_name` 用 `{{ field("analysis_model_name", config.analysis.model_name, "模型名称", placeholder="gpt-4o-mini", width="full") }}`
  - **字段 name 前缀**：与现有 `gotify_url` / `data_dir` 风格不一致——为避免与 `/settings` 主表单冲突，AI 分析的 5 个字段 **必须**独立提交到 `/settings/analysis/save`，所以 **字段 name 不要加前缀也行**，但 form 应为独立子 form 或独立 HTMX 触发。

- [ ] **关键决策（取舍）：** 现有 Settings 页是一个 `<form id="settings-form" hx-post="/settings">` 包裹所有 Card。AI 分析 Card 如果放进去会和 sticky save bar 联动，但保存路径不同。两种方案：
  - **方案 A（推荐）**：AI 分析 Card **独立成一个 `<form>`**，自带"测试" + "保存"按钮，不参与顶部 sticky save bar 的 dirty 检测（dirty JS 只听 `settings-form` 的 change/input，AI form 不在 `settings-form` 内则不影响）
  - 方案 B：合并进主表单，把所有 AI 字段并入 `/settings` POST。但这会让 `settings_save` 路由签名爆炸式增长，违反"最小改动"。
  
  **采用方案 A**：AI 分析 Card 用独立 form `hx-post="/settings/analysis/save"`，按钮 inline 在 Card 底部；"测试"按钮用 `hx-post="/settings/analysis/test"` + `hx-swap="none"` + 通过 `hx-on::after-request` 在前端展示 toast。

- [ ] Card 完整结构草图：
```html
<!-- Card: AI 分析（独立 form，不参与主 sticky save bar）-->
<form hx-post="/settings/analysis/save" hx-trigger="submit" hx-swap="none" id="analysis-form">
  <div class="bg-[var(--card-bg)] ... mb-4">
    <div class="flex items-center gap-2 mb-4">
      <span class="w-7 h-7 rounded-[8px] bg-apple-blue/10 ..."><!-- sparkles svg --></span>
      <h2 class="text-base font-semibold">AI 分析</h2>
    </div>
    <p class="text-xs text-[var(--text-secondary)] mb-4">
      摘要与关键词生成。AI 失败时自动降级到本地 TF 摘要，不影响抓取流程。
    </p>
    <div class="flex items-center justify-between py-2 mb-3 border-b border-gray-100 dark:border-gray-800">
      <span class="text-sm font-medium">启用 AI 分析</span>
      {{ toggle("enabled", config.analysis.enabled) }}
    </div>
    <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
      <!-- provider select -->
      <div class="flex flex-col gap-1.5">
        <label class="text-xs font-medium text-[var(--text-secondary)] uppercase tracking-wider">Provider</label>
        <select name="provider" class="px-3 py-2 rounded-[8px] border ...">
          <option value="openai" {% if config.analysis.provider == "openai" %}selected{% endif %}>openai (OpenAI / DeepSeek 兼容)</option>
          <option value="ollama" {% if config.analysis.provider == "ollama" %}selected{% endif %}>ollama (本地)</option>
        </select>
      </div>
      {{ field("model_name", config.analysis.model_name, "模型名称", placeholder="gpt-4o-mini / qwen2.5:7b") }}
      {{ field("api_base", config.analysis.api_base, "API Base", placeholder="https://api.openai.com/v1", width="full") }}
      {{ field("api_key", config.analysis.api_key, "API Key", type="password", placeholder="sk-...", width="full") }}
    </div>
    <div class="flex items-center justify-end gap-2 mt-4 pt-4 border-t border-gray-100 dark:border-gray-800">
      <button type="button"
              hx-post="/settings/analysis/test"
              hx-include="closest form"
              hx-swap="none"
              hx-on::after-request="onAnalysisTestResult(event)"
              class="px-4 py-2 text-sm rounded-[10px] border border-gray-300 dark:border-gray-600 ...">
        测试连通性
      </button>
      <button type="submit" class="px-4 py-2 text-sm rounded-[10px] bg-apple-blue text-white ...">保存</button>
    </div>
  </div>
</form>

<script>
  // 把这个 script 块追加到 settings.html 末尾的 <script> 区域，不要重复包 IIFE
  function onAnalysisTestResult(event) {
    var xhr = event.detail.xhr;
    if (xhr.status !== 200) { return; }
    try {
      var data = JSON.parse(xhr.responseText);
      var type = data.ok ? 'success' : 'error';
      var key = data.ok ? 'analysis.test.ok' : 'analysis.test.fail';
      // 复用 base.html 的 showToast（如果它支持自定义 message 直接传，否则传 key）
      document.body.dispatchEvent(new CustomEvent('showToast', { detail: { key: key, type: type, fallback: data.message } }));
    } catch (e) {}
  }
</script>
```

- [ ] **不改 `_macros.html`**（取舍 4 已说明 field 宏支持 password）

**验证:**
- [ ] `uv run pytest tests/test_web_settings.py -x` — 旧测试不应破坏（GET 渲染要确保 `config.analysis.*` 字段都可访问）
- [ ] 手动：`uv run uvicorn web.app:app --reload`，打开 `/settings`，确认 Card 渲染、暗色模式正常、select/toggle 可交互

---

## Task 6: 集成测试 — Settings GET 渲染含 AI Card

**TDD: 是**

**文件:** `tests/test_web_settings.py`（**扩展现有文件**，不新建）

**前置:** Task 5 完成

**改动:**

- [ ] 在现有 `TestSettings` 类中追加 case：
  - `test_settings_page_contains_analysis_card`：
    - patch `load_config` 让 `mock_load.return_value.analysis` 返回真实 `AnalysisConfig()` 实例（用 dataclass，不要 MagicMock，避免模板属性访问报错）
    - GET `/settings`
    - 断言 status 200
    - 断言响应 HTML 含 `"AI 分析"`、`"analysis_api_base"` 或 `"name=\"api_base\""`、`"name=\"provider\""`、`"测试连通性"`
  - 修正现有 `test_settings_page`：当前用 `mock_load.return_value.general.data_dir = ...` 这种 attribute set，新模板访问 `config.analysis.enabled` 等 5 个字段——**必须在 mock 上补齐** `mock_load.return_value.analysis.enabled = True` 等（否则 Jinja2 拿不到字段会 500）。或更稳的做法：让 `mock_load.return_value` 返回真实 `Config()` 实例。

**验证:**
- [ ] `uv run pytest tests/test_web_settings.py -xvs`
- [ ] 如果 `test_settings_page` 因新字段访问失败，需要把 mock 补齐或换真实 Config 实例

---

## Task 7: 端到端验证 — 配置落盘 + provider 调用链

**TDD: 否**（回归验证）

**文件:** 无（运行既有测试套件）

**改动:** 无代码改动，仅验证

**步骤:**

- [ ] `uv run ruff check .` — 无新增问题
- [ ] `uv run ruff format --check .` — 格式无 drift
- [ ] `uv run pyright .` — 无新增 error（特别注意 `web/routes/settings.py` 的 Form 参数类型，和 `_probe_provider` 返回类型）
- [ ] `uv run pytest -x` — **全套**测试通过（含 test_config / test_summarizer / test_web_settings / test_web_settings_analysis）
- [ ] **手动 smoke test**（如本地有网络）：
  - 启动 `uv run uvicorn web.app:app --reload`
  - 打开 `/settings`，在 AI 分析 Card 填一个真实 OpenAI/DeepSeek key
  - 点"测试连通性" → 应看到绿色 toast
  - 改 `provider=ollama` + `api_base=http://localhost:11434/v1`，点测试（本地无 ollama 应红色 toast + 错误信息）
  - 点"保存" → 检查 `config/config.toml` 的 `[analysis]` 段被正确更新（其他段注释保留）
  - 触发一次 `trawler check --platform bili` → 观察日志，确认摘要用了新配置（`INFO  AI 摘要生成成功: ...`）

---

## 验证清单（最终）

1. **lint**: `uv run ruff check .` — 无新增问题
2. **format**: `uv run ruff format --check .` — 无 drift
3. **type**: `uv run pyright .` — 无新增 error
4. **test**: `uv run pytest -x` — 全部通过
5. **codebuddy 清除**: `grep -ri codebuddy core/ shared/ platforms/ web/ config/config.toml.example tests/` — **仅允许** `docs/superpowers/` 下的历史 spec/plan 出现（历史文档不动）
6. **Web UI**: 手动访问 `/settings`，AI 分析 Card 渲染、可保存、测试按钮可触发 toast
7. **配置落盘**: 保存后 `config/config.toml` 的 `[analysis]` 段字段正确，其他段注释保留

---

## 执行顺序建议

1. **Task 1**（删 codebuddy）— 基础清理，所有后续 task 依赖
2. **Task 2**（summarizer 工厂测试）— 锁定 Task 1 的行为
3. **Task 3**（test 端点 + 测试）— 后端先通
4. **Task 4**（save 端点 + 测试）— 后端完整
5. **Task 5**（模板 Card）— UI 接入
6. **Task 6**（集成测试扩展）— 兜底渲染
7. **Task 7**（端到端 + 全量回归）— 收尾

共 **7 个 Task**，预计耗时约 35-45 分钟（中等复杂度，主要是 tomlkit 落盘和 mock 链路调试）。

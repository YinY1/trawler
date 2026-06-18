# Plan: TODO1 — 通知平台抽象层 + Gotify 多 URL

## 背景

将三平台独立的 `NotificationConfig`（每平台各持一份 `gotify_url` + `gotify_token`）重构为：
1. **Provider 抽象层**：`Notifier` Protocol，Gotify 是首个实现，Telegram/Email 占坑 stub
2. **全局 endpoint 配置**：`[[endpoints]]` 在顶层，订阅侧 `notify_endpoints: list[str]` 引用 endpoint name
3. **fan-out 语义**：每个订阅可引用多个 endpoint，全部发送，不 failover

调用点高度集中：4 个 `notify_*` 函数仅被 `platforms/{bilibili,weibo,xiaohongshu}/handlers.py` 的 `*_push` 阶段调用，**Web 入口不直接发通知**（只管理配置）。设计上仍要求 `get_notifiers_for_subscription()` 工厂对 cron / web 两侧通用，便于未来在 web 加入"测试推送"按钮时无需重复设计。

## 范围

涉及 ~16 文件、~15 task：

- `shared/protocols.py` — 新增 `Notifier` Protocol + `NotificationContent` dataclass + `MessageRecord.subscription_ref` 字段
- `shared/message_store.py` — `add_new()` 接受 `subscription_ref` 参数
- `shared/config.py` — 删 `NotificationConfig`，加 `EndpointConfig`，订阅加 `notify_endpoints`
- `core/notifier.py` → 拆为 `core/notifiers/{base,gotify,telegram,email,__init__}.py`
- `core/pipeline.py` — docstring 更新 notifier 签名
- `platforms/*/handlers.py` — detector 注入 subscription_ref + 3 个 `*_push` 改造
- `web/routes/settings.py` + `web/routes/endpoints.py`（新）+ `web/templates/endpoints.html`（新）+ `web/templates/settings.html`
- `web/templates/_macros.html` — 修复 toggle 宏 422 bug
- `config/config.toml.example` + `config/subscriptions.toml`
- 测试：`tests/test_notifier_base.py` + `tests/test_gotify_notifier.py` + `tests/test_token_store.py`

## 决策摘要

- **Gotify fan-out**：每 endpoint 都发，单 endpoint 失败仅 warning，不影响其他
- **Provider 抽象**：`Notifier` Protocol（`async send(content) -> SendResult`），Telegram/Email 是 stub（`raise NotImplementedError("not yet")`）
- **不写迁移脚本**（用户已声明）
- **不做双向关联 UI**（用户已声明）
- **4 个 `notify_*` 删除**，合并为 `NotificationContent` dataclass + `render_markdown(content)`
- **引入 `MessageRecord.subscription_ref` 字段**（轻量字符串，存 uid/user_id）：detector 注入、push handler 精确匹配订阅，避免脆弱的反查
- **endpoint 全局化**：`Config.endpoints: list[EndpointConfig]`，订阅侧只存 name 列表
- **删除 FEEDFLOW_GOTIFY_* env override**：全局 endpoint 不再通过环境变量配置

## 与已有 plan 的取舍（2026-06-16-c-gotify-multi-url.md）

| 已有 plan 元素 | 处理 | 理由 |
|---|---|---|
| Task 1 EndpointConfig / Config.endpoints / 订阅 notify_endpoints / 删 NotificationConfig | **保留** | 核心数据模型 |
| Task 1 MessageRecord.subscription_id 字段 | **取代**：改为轻量 `subscription_ref: str = ""`，不做双向查询 | 保留 detector→push 的身份传递，但去 foreign-key 风格 |
| Task 1 测试影响清单（13+ 处 test_config.py 修改） | **保留** | NotificationConfig 删除连带 |
| Task 2 配置解析 + env override | **保留**（去掉 Task 2d 旧配置 warn，因无迁移脚本） |
| Task 3 MessageStore.add_new subscription_id | **保留**（add_new 接受 `subscription_ref` 参数） | detector 注入所需 |
| Task 4 三平台 detector subscription_id 注入 | **保留**（detector 在 add_new 时注入 subscription_ref） | 同 Task 3 |
| Task 5 notifier 重构 + formatter 拆分 | **保留并升级为 Provider 抽象**（原 plan 只改签名，本 plan 拆为 `core/notifiers/` 目录 + Protocol） |
| Task 6 push handler 改反查 endpoints | **简化为直接用 `sub.notify_endpoints`**（去 subscription_id 反查逻辑，handlers 已在订阅循环内） |
| Task 7 迁移脚本 | **删除**（用户决策） |
| Task 8 endpoint CRUD | **保留**（去 linked 反查数据；模板保留显示 name/url/priority/enabled） |
| Task 9 订阅表单 endpoint multi-select | **保留** |
| Task 10 settings 清理 | **保留** |
| Task 11 配置示例 | **保留** |
| Task 12 README | **删除**（用户未列入范围，避免过度） |
| 阶段 2 全部（双向关联 UI Task 13-16） | **删除**（用户决策） |

新增（已有 plan 没有）：
- **Notifier Protocol + SendResult + NotificationContent dataclass**（在 `shared/protocols.py`）
- **`core/notifiers/` 目录拆分**（base / gotify / telegram / email）
- **`render_markdown(content)` 单一渲染函数**（替代 4 个 `format_*_notification`，按 platform 分支选 emoji/模板）
- **`get_notifiers_for_subscription()` 工厂**（cron / web 共享入口点）

---

## 阶段 1: 数据模型 + Provider 抽象（独立，可并行起手）

### Task 1 [TDD]: 新增 `Notifier` Protocol + `NotificationContent` + `SendResult`

**文件**: `shared/protocols.py`

**改动**: 在文件末尾（line 374 之后）追加通知抽象层

```python
# ═══════════════════════════════════════════════════════════
# 通知抽象层 — Notifier Protocol + 内容模型
# ═══════════════════════════════════════════════════════════


@dataclass
class NotificationContent:
    """跨平台统一的通知内容载体。

    渲染层根据 platform 字段选择 emoji 前缀和模板；
    Notifier 实现根据 type 字段决定是否省略某些字段。
    """
    platform: str  # "bili" | "xhs" | "weibo"
    source_id: str  # bvid / note_id / post_id / dynamic_id（不含 platform 前缀）
    title: str
    author: str
    summary: str = ""
    keywords: list[str] = field(default_factory=list)
    comment_highlights: str = ""
    url: str = ""  # 完整链接，空则由渲染层根据 platform + source_id 生成
    type: str = "content"  # "content" | "dynamic"


@dataclass
class SendResult:
    """单次发送结果（fan-out 中单个 endpoint 的反馈）。"""
    endpoint_name: str
    success: bool
    error: str = ""


class Notifier(Protocol):
    """通知发送器抽象。Provider 实现此接口。

    实现例：GotifyNotifier / TelegramNotifier（stub）/ EmailNotifier（stub）。
    """
    name: str

    async def send(self, content: NotificationContent) -> SendResult:
        """渲染并推送一条通知。返回 SendResult（不抛异常，失败时填 error）。"""
        ...
```

**验证**:
- `uv run pyright shared/protocols.py` 无新 error
- `uv run ruff check shared/protocols.py` 无新问题

**TDD 测试**（`tests/test_notifier_base.py`，新文件）:
```python
from shared.protocols import NotificationContent, SendResult


def test_notification_content_defaults():
    c = NotificationContent(platform="bili", source_id="BV1xx", title="t", author="a")
    assert c.summary == ""
    assert c.keywords == []
    assert c.comment_highlights == ""
    assert c.url == ""
    assert c.type == "content"


def test_notification_content_full():
    c = NotificationContent(
        platform="weibo", source_id="123", title="t", author="a",
        summary="s", keywords=["k1"], comment_highlights="ch",
        url="https://weibo.com/123", type="dynamic",
    )
    assert c.keywords == ["k1"]


def test_send_result_shape():
    r = SendResult(endpoint_name="default", success=True)
    assert r.success is True
    assert r.error == ""
```

---

### Task 2 [TDD]: `EndpointConfig` + 订阅 `notify_endpoints` + 删 `NotificationConfig`

**文件**: `shared/config.py`

**改动 2a**（替换 line 126-136，删 `NotificationConfig` 整段，新增 `EndpointConfig`）:
```python
# ── 推送端点配置 ───────────────────────────────────────────────


@dataclass
class EndpointConfig:
    """Gotify 推送端点（全局列表，订阅通过 name 引用）。"""
    name: str
    url: str
    token: str
    priority: int = 5
    enabled: bool = True
    kind: str = "gotify"  # 预留："gotify" | "telegram" | "email"
```

**改动 2b**（line 142-151，订阅 dataclass 加 `notify_endpoints`）:
```python
@dataclass
class BiliSubscription:
    uid: int = 0
    name: str = ""
    notify_endpoints: list[str] = field(default_factory=list)


@dataclass
class UserSubscription:
    user_id: str = ""
    name: str = ""
    notify_endpoints: list[str] = field(default_factory=list)
```

**改动 2c**（line 157-180，三平台 config 删 `notification` 字段）:
```python
@dataclass
class BilibiliConfig:
    auth: BilibiliAuth = field(default_factory=BilibiliAuth)
    monitor: BilibiliMonitorConfig = field(default_factory=BilibiliMonitorConfig)
    subscriptions: list[BiliSubscription] = field(default_factory=list)


@dataclass
class XhsConfig:
    enabled: bool = False
    auth: XhsAuth = field(default_factory=XhsAuth)
    monitor: XhsMonitorConfig = field(default_factory=XhsMonitorConfig)
    subscriptions: list[UserSubscription] = field(default_factory=list)


@dataclass
class WeiboConfig:
    enabled: bool = False
    auth: WeiboAuth = field(default_factory=WeiboAuth)
    monitor: WeiboMonitorConfig = field(default_factory=WeiboMonitorConfig)
    subscriptions: list[UserSubscription] = field(default_factory=list)
```

**改动 2d**（line 192-203，`Config` 加 `endpoints`）:
```python
@dataclass
class Config:
    """Trawler 全局配置"""

    general: GeneralConfig = field(default_factory=GeneralConfig)
    auth: AuthGlobalConfig = field(default_factory=AuthGlobalConfig)
    download: DownloadConfig = field(default_factory=DownloadConfig)
    transcribe: TranscribeConfig = field(default_factory=TranscribeConfig)
    analysis: AnalysisConfig = field(default_factory=AnalysisConfig)
    bilibili: BilibiliConfig = field(default_factory=BilibiliConfig)
    xiaohongshu: XhsConfig = field(default_factory=XhsConfig)
    weibo: WeiboConfig = field(default_factory=WeiboConfig)
    endpoints: list[EndpointConfig] = field(default_factory=list)
```

**改动 2e**（line 251-285，`_parse_config` 三平台段删 noti 解析，文件末尾加 endpoints 解析）:
```python
    # bilibili
    if bili := raw.get("bilibili"):
        auth = _dict_to_dataclass(BilibiliAuth, bili.get("auth", {}))
        monitor = _dict_to_dataclass(BilibiliMonitorConfig, bili.get("monitor", {}))
        subs = [BiliSubscription(**s) for s in bili.get("subscriptions", [])]
        cfg.bilibili = BilibiliConfig(auth=auth, monitor=monitor, subscriptions=subs)

    # xiaohongshu
    if xhs := raw.get("xiaohongshu"):
        auth = _dict_to_dataclass(XhsAuth, xhs.get("auth", {}))
        monitor = _dict_to_dataclass(XhsMonitorConfig, xhs.get("monitor", {}))
        subs = [UserSubscription(**s) for s in xhs.get("subscriptions", [])]
        cfg.xiaohongshu = XhsConfig(
            enabled=xhs.get("enabled", False),
            auth=auth, monitor=monitor, subscriptions=subs,
        )

    # weibo
    if wb := raw.get("weibo"):
        auth = _dict_to_dataclass(WeiboAuth, wb.get("auth", {}))
        monitor = _dict_to_dataclass(WeiboMonitorConfig, wb.get("monitor", {}))
        subs = [UserSubscription(**s) for s in wb.get("subscriptions", [])]
        cfg.weibo = WeiboConfig(
            enabled=wb.get("enabled", False),
            auth=auth, monitor=monitor, subscriptions=subs,
        )

    # endpoints（全局）
    if eps := raw.get("endpoints"):
        cfg.endpoints = [EndpointConfig(**ep) for ep in eps]
```

**改动 2f**（line 290-300，`_apply_env_overrides` 删除 `FEEDFLOW_GOTIFY_*` 整段）:
```python
def _apply_env_overrides(cfg: Config) -> None:
    """环境变量覆盖配置值，优先级高于配置文件"""
    # 平台 cookies
    if v := os.environ.get("FEEDFLOW_XHS_COOKIE"):
        cfg.xiaohongshu.auth.cookie = v
    if v := os.environ.get("FEEDFLOW_WEIBO_COOKIE"):
        cfg.weibo.auth.cookie = v
    # AI
    if v := os.environ.get("FEEDFLOW_LLM_API_KEY"):
        cfg.analysis.api_key = v
    if v := os.environ.get("FEEDFLOW_LLM_API_BASE"):
        cfg.analysis.api_base = v
```

**验证**:
- `uv run pyright shared/config.py` 无新 error
- `uv run pytest tests/test_config.py -x`（预期：会失败，连带的旧测试需要修改 — 见 Task 3）

**TDD 测试**（先写测试到 `tests/test_config.py`，覆盖新字段）:
```python
def test_endpoint_config_defaults():
    ep = EndpointConfig(name="default", url="https://g.example.com", token="tk")
    assert ep.priority == 5
    assert ep.enabled is True
    assert ep.kind == "gotify"


def test_bili_subscription_notify_endpoints():
    s = BiliSubscription(uid=1, name="x", notify_endpoints=["a", "b"])
    assert s.notify_endpoints == ["a", "b"]


def test_config_endpoints_parsed():
    raw = {"endpoints": [{"name": "ep1", "url": "u", "token": "t"}]}
    cfg = _parse_config(raw)
    assert len(cfg.endpoints) == 1
    assert cfg.endpoints[0].name == "ep1"
```

---

### Task 3: 修复 `tests/test_config.py` 连带改动

**文件**: `tests/test_config.py`

**改动清单**（与已有 plan Task 1 测试影响一致，逐项修改）:

| 位置 | 改动 |
|---|---|
| import 段 | 删 `NotificationConfig,` |
| BASE_TOML fixture `[bilibili.notification]` / `[xiaohongshu.notification]` / `[weibo.notification]` 三段 | 整段删 |
| `test_bilibili_notification` 方法 | 整方法删 |
| `assert cfg.bilibili.notification.gotify_url == ...` / `.gotify_token == ...`（共 ~4 处） | 删该断言行 |
| `TestEnvOverrides` 中 `test_trawler_gotify_url` / `test_trawler_gotify_token_bili/xhs/weibo`（共 4 方法） | 整方法删 |
| `test_env_override_with_empty_config` 中 `FEEDFLOW_GOTIFY_URL` 相关 | 删环境变量设置和断言行 |
| `test_notification_config_defaults` | 改为 `test_endpoint_config_defaults`（已在 Task 2 写） |
| `assert isinstance(b.notification, NotificationConfig)`（共 3 处） | 删 |
| `tests/test_web_settings.py` lines 22-25 | 删除 4 行 `mock_load.return_value.<platform>.notification.gotify_*`。Optionally 加 `mock_load.return_value.endpoints = []` |
| `tests/test_token_store.py` line 27（`[bilibili.notification]` in TOML fixture）| 删该段 |
| `tests/test_token_store.py` line 74-76（`data["bilibili"]["notification"]["gotify_url"]` assertion）| 删该断言行 |

**验证**:
- `uv run pytest tests/test_config.py -x` 全绿
- `uv run pytest -x`（除下文 Task 6 的 handlers 测试外，其余应能过）

---

### Task 4 [TDD]: `core/notifiers/base.py` — `render_markdown` 共享渲染

**新文件**: `core/notifiers/base.py`

**改动**: 实现 `render_markdown(content: NotificationContent) -> tuple[str, str]`

```python
"""通知内容渲染层 — 跨 Provider 共享的 Markdown 渲染。"""

from __future__ import annotations

from shared.protocols import NotificationContent


# 各 platform 的 title emoji 和"作者"标签
_PLATFORM_STYLE: dict[str, dict[str, str]] = {
    "bili": {"emoji": "📹", "author_label": "UP主"},
    "xhs":  {"emoji": "📕", "author_label": "作者"},
    "weibo": {"emoji": "🐦", "author_label": "作者"},
}


def _build_url(content: NotificationContent) -> str:
    if content.url:
        return content.url
    if content.platform == "bili":
        return f"https://www.bilibili.com/video/{content.source_id}"
    if content.platform == "xhs":
        return f"https://www.xiaohongshu.com/explore/{content.source_id}"
    if content.platform == "weibo":
        return f"https://weibo.com/{content.source_id}"
    return ""


def render_markdown(content: NotificationContent) -> tuple[str, str]:
    """渲染通知为 (title, message_markdown)。

    根据 content.platform 选择 emoji 和"作者"标签；
    根据 content.type == "dynamic" 使用更简短的动态模板。
    """
    style = _PLATFORM_STYLE.get(content.platform, {"emoji": "📣", "author_label": "作者"})
    keywords_str = "；".join(content.keywords) if content.keywords else "无"
    url = _build_url(content)

    if content.type == "dynamic":
        # 动态：简短格式，无 keywords/comment
        parts: list[str] = [f"**{style['author_label']}:** {content.author}"]
        if url:
            parts.append(f"**链接:** [{content.source_id}]({url})")
        parts.extend(["", "---", "", content.summary or content.title])
        return f"📢 {content.author} 的动态", "\n".join(parts)

    # 默认：完整内容模板
    parts = [
        f"**{style['author_label']}:** {content.author}",
        f"**链接:** [{content.source_id}]({url})" if url else "",
        f"**关键词:** {keywords_str}",
        "",
        "---",
        "",
        "**详情:**",
        content.summary,
    ]
    if content.comment_highlights:
        parts.extend(["", "**评论区补充:**", content.comment_highlights])
    return f"{style['emoji']} {content.title}", "\n".join(parts)
```

**验证**:
- `uv run pyright core/notifiers/base.py`
- `uv run ruff check core/notifiers/base.py`

**TDD 测试**（追加到 `tests/test_notifier_base.py`）:
```python
from core.notifiers.base import render_markdown


def test_render_bili_video():
    c = NotificationContent(
        platform="bili", source_id="BV1xx", title="t", author="UP",
        summary="s", keywords=["k1", "k2"],
    )
    title, msg = render_markdown(c)
    assert title.startswith("📹")
    assert "BV1xx" in msg
    assert "https://www.bilibili.com/video/BV1xx" in msg
    assert "UP主:** UP" in msg
    assert "k1；k2" in msg


def test_render_xhs_default_url():
    c = NotificationContent(platform="xhs", source_id="note1", title="t", author="A")
    _, msg = render_markdown(c)
    assert "https://www.xiaohongshu.com/explore/note1" in msg
    assert "作者:** A" in msg


def test_render_weibo_custom_url():
    c = NotificationContent(
        platform="weibo", source_id="p1", title="t", author="A",
        url="https://weibo.com/custom",
    )
    _, msg = render_markdown(c)
    assert "https://weibo.com/custom" in msg


def test_render_dynamic_short_format():
    c = NotificationContent(
        platform="bili", source_id="dyn123", title="t", author="UP",
        summary="动态正文", type="dynamic",
    )
    title, msg = render_markdown(c)
    assert title == "📢 UP 的动态"
    assert "动态正文" in msg
    assert "关键词" not in msg  # 动态无 keywords 段


def test_render_comment_highlights():
    c = NotificationContent(
        platform="weibo", source_id="p", title="t", author="A",
        comment_highlights="精选评论",
    )
    _, msg = render_markdown(c)
    assert "评论区补充" in msg
    assert "精选评论" in msg


def test_render_unknown_platform_uses_default_emoji():
    c = NotificationContent(platform="unknown", source_id="x", title="t", author="A")
    title, _ = render_markdown(c)
    assert title.startswith("📣")
```

> **决策说明：放弃"发布时间"字段**。原 `notify_new_video` 等模板含 `**发布时间:** {now}` 字段，新 `render_markdown` 去掉。理由：(1) `ctx.msg.pubdate` 已在 `MessageRecord` 中保留，需要时可在调用方注入到 `NotificationContent.summary`；(2) Gotify 推送时间戳即代表接收时刻，重复信息。若未来需要可加 `pubdate` 字段到 `NotificationContent`。

---

## 阶段 2: Provider 实现（依赖阶段 1）

### Task 5 [TDD]: `core/notifiers/gotify.py` — `GotifyNotifier`

**新文件**: `core/notifiers/gotify.py`

**改动**: 把 `core/notifier.py` 的 `send_gotify` 重构为 `GotifyNotifier` 类，依赖 `EndpointConfig`

```python
"""Gotify Provider — 跨 endpoint fan-out。"""

from __future__ import annotations

import asyncio
import logging

import aiohttp

from core.notifiers.base import render_markdown
from shared.config import EndpointConfig
from shared.constants import GOTIFY_MAX_RETRIES, GOTIFY_TIMEOUT
from shared.protocols import NotificationContent, SendResult

logger = logging.getLogger(__name__)


class GotifyNotifier:
    """Gotify 单 endpoint 发送器。

    Note: 这是一个 endpoint 一个实例。fan-out 由上层 get_notifiers_for_subscription
    返回的多个 Notifier 实例实现，每个独立 send。
    """

    def __init__(self, endpoint: EndpointConfig) -> None:
        self.endpoint = endpoint
        self.name = endpoint.name

    async def send(self, content: NotificationContent) -> SendResult:
        ep = self.endpoint
        if not ep.enabled:
            logger.debug("[%s] 端点已禁用", ep.name)
            return SendResult(endpoint_name=ep.name, success=False, error="disabled")

        if not ep.url or not ep.token:
            logger.warning("[%s] Gotify 配置不完整", ep.name)
            return SendResult(endpoint_name=ep.name, success=False, error="missing url/token")

        title, message = render_markdown(content)
        url = f"{ep.url.rstrip('/')}/message"
        params = {"token": ep.token}
        payload: dict[str, str | int] = {
            "title": title,
            "message": message,
            "priority": ep.priority,
        }

        for attempt in range(1, GOTIFY_MAX_RETRIES + 1):
            try:
                async with aiohttp.ClientSession(trust_env=False) as session:
                    async with session.post(
                        url, params=params, json=payload,
                        headers={"Content-Type": "application/json"},
                        timeout=aiohttp.ClientTimeout(total=GOTIFY_TIMEOUT),
                    ) as resp:
                        resp.raise_for_status()
                    logger.info("[%s] Gotify 发送成功: %s", ep.name, title)
                    return SendResult(endpoint_name=ep.name, success=True)
            except asyncio.TimeoutError:
                logger.warning("[%s] Gotify 超时 (%s/%s)", ep.name, attempt, GOTIFY_MAX_RETRIES)
            except aiohttp.ClientConnectionError:
                logger.warning("[%s] Gotify 连接失败 (%s/%s)", ep.name, attempt, GOTIFY_MAX_RETRIES)
            except aiohttp.ClientResponseError as e:
                logger.warning("[%s] Gotify HTTP 错误 (%s/%s): %s", ep.name, attempt, GOTIFY_MAX_RETRIES, e)
            except Exception as e:
                logger.warning("[%s] Gotify 异常 (%s/%s): %s", ep.name, attempt, GOTIFY_MAX_RETRIES, e)

            if attempt < GOTIFY_MAX_RETRIES:
                await asyncio.sleep(2 ** (attempt - 1))

        err = f"failed after {GOTIFY_MAX_RETRIES} retries"
        logger.error("[%s] Gotify 失败: %s", ep.name, title)
        return SendResult(endpoint_name=ep.name, success=False, error=err)
```

**验证**: `uv run pyright core/notifiers/gotify.py` + `uv run ruff check`

**TDD 测试**（`tests/test_gotify_notifier.py`，新文件）:
```python
import asyncio
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

from core.notifiers.gotify import GotifyNotifier
from shared.config import EndpointConfig
from shared.protocols import NotificationContent


def _content():
    return NotificationContent(
        platform="bili", source_id="BV1xx", title="t", author="A", summary="s"
    )


@pytest.mark.asyncio
async def test_send_disabled_endpoint():
    ep = EndpointConfig(name="e", url="u", token="t", enabled=False)
    n = GotifyNotifier(ep)
    r = await n.send(_content())
    assert r.success is False
    assert r.error == "disabled"


@pytest.mark.asyncio
async def test_send_missing_token():
    ep = EndpointConfig(name="e", url="u", token="")
    n = GotifyNotifier(ep)
    r = await n.send(_content())
    assert r.success is False
    assert "missing" in r.error


@pytest.mark.asyncio
async def test_send_success():
    ep = EndpointConfig(name="e", url="https://g.example.com", token="tk")
    n = GotifyNotifier(ep)

    # mock aiohttp.ClientSession.post
    fake_resp = MagicMock()
    fake_resp.__aenter__ = AsyncMock(return_value=fake_resp)
    fake_resp.__aexit__ = AsyncMock(return_value=None)
    fake_resp.raise_for_status = MagicMock()

    fake_session = MagicMock()
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=None)
    fake_session.post = MagicMock(return_value=fake_resp)

    with patch("core.notifiers.gotify.aiohttp.ClientSession", return_value=fake_session):
        r = await n.send(_content())
    assert r.success is True
    assert r.endpoint_name == "e"
    # 验证 URL 和 payload
    fake_session.post.assert_called_once()
    args, kwargs = fake_session.post.call_args
    assert args[0] == "https://g.example.com/message"
    assert kwargs["params"] == {"token": "tk"}
    assert "📹 t" == kwargs["json"]["title"]


@pytest.mark.asyncio
async def test_send_returns_error_on_failure():
    """测试失败时返回 SendResult(success=False)，不抛异常。"""
    import aiohttp
    ep = EndpointConfig(name="e", url="https://g.example.com", token="tk", priority=1)
    n = GotifyNotifier(ep)

    # GOTIFY_MAX_RETRIES 通常为 3，patch sleep 加速
    with patch("core.notifiers.gotify.aiohttp.ClientSession") as ms, \
         patch("core.notifiers.gotify.asyncio.sleep", new=AsyncMock()):
        ms.side_effect = aiohttp.ClientConnectionError("conn refused")
        r = await n.send(_content())
    assert r.success is False
    assert "failed" in r.error
```

---

### Task 6: `core/notifiers/{telegram,email}.py` — Stub

**新文件**: `core/notifiers/telegram.py` 和 `core/notifiers/email.py`

**改动**: 占坑 stub，遵循 Notifier Protocol

```python
# core/notifiers/telegram.py
"""Telegram Provider — 占坑，未实现。"""

from __future__ import annotations

from shared.config import EndpointConfig
from shared.protocols import NotificationContent, SendResult


class TelegramNotifier:
    """Telegram Bot 推送。占坑，等 EndpointConfig.kind == 'telegram' 时启用。"""

    def __init__(self, endpoint: EndpointConfig) -> None:
        self.endpoint = endpoint
        self.name = endpoint.name

    async def send(self, content: NotificationContent) -> SendResult:
        raise NotImplementedError("Telegram notifier not yet implemented")
```

```python
# core/notifiers/email.py
"""Email Provider — 占坑，未实现。"""

from __future__ import annotations

from shared.config import EndpointConfig
from shared.protocols import NotificationContent, SendResult


class EmailNotifier:
    """Email 推送。占坑。"""

    def __init__(self, endpoint: EndpointConfig) -> None:
        self.endpoint = endpoint
        self.name = endpoint.name

    async def send(self, content: NotificationContent) -> SendResult:
        raise NotImplementedError("Email notifier not yet implemented")
```

**验证**: `uv run pyright core/notifiers/{telegram,email}.py`

---

### Task 7 [TDD]: `core/notifiers/__init__.py` — 工厂 + fan-out helper

**新文件**: `core/notifiers/__init__.py`

**改动**: 暴露工厂 `get_notifiers_for_subscription()` 和便捷 fan-out 函数 `send_to_subscription()`。**这是 cron / web 共享的核心入口。**

```python
"""Notifier 包 — 工厂 + fan-out 便捷函数。

设计要点：
- get_notifiers_for_subscription() 是 cron (handlers) 和 web (未来测试推送按钮)
  共享的唯一入口，确保两侧使用相同的 provider 解析逻辑。
- fan-out：每 endpoint 独立发送，单失败仅 warning，不阻塞其他。
"""

from __future__ import annotations

import logging
from typing import Iterable

from shared.config import Config, EndpointConfig
from shared.protocols import Notifier, NotificationContent, SendResult

from core.notifiers.gotify import GotifyNotifier
from core.notifiers.telegram import TelegramNotifier
from core.notifiers.email import EmailNotifier

logger = logging.getLogger(__name__)

__all__ = [
    "GotifyNotifier", "TelegramNotifier", "EmailNotifier",
    "get_notifiers_for_subscription", "send_to_subscription",
]


_KIND_MAP: dict[str, type] = {
    "gotify": GotifyNotifier,
    "telegram": TelegramNotifier,
    "email": EmailNotifier,
}


def _build_notifier(ep: EndpointConfig) -> Notifier | None:
    cls = _KIND_MAP.get(ep.kind)
    if cls is None:
        logger.warning("未知 endpoint kind %r，跳过", ep.kind)
        return None
    return cls(ep)


def get_notifiers_for_subscription(
    config: Config, platform: str, endpoint_names: Iterable[str],
) -> list[Notifier]:
    """按订阅声明的 endpoint name 列表解析出 Notifier 实例列表。

    - endpoint name 找不到 → warning + skip
    - endpoint kind 未知 → 警告并跳过
    返回顺序保持声明顺序，便于日志追踪。
    """
    name_to_ep: dict[str, EndpointConfig] = {ep.name: ep for ep in config.endpoints}
    notifiers: list[Notifier] = []
    for name in endpoint_names:
        ep = name_to_ep.get(name)
        if ep is None:
            logger.warning("Endpoint %r not found (referenced by %s subscription)", name, platform)
            continue
        n = _build_notifier(ep)
        if n is not None:
            notifiers.append(n)
    return notifiers


async def send_to_subscription(
    config: Config, platform: str, endpoint_names: Iterable[str],
    content: NotificationContent,
) -> list[SendResult]:
    """Fan-out 发送：遍历订阅声明的 endpoints，每 endpoint 独立 send。

    单 endpoint 失败（含 NotImplementedError / 其他异常）只记 warning，
    不影响其他 endpoint。返回每个 endpoint 的 SendResult。
    """
    notifiers = get_notifiers_for_subscription(config, platform, endpoint_names)
    results: list[SendResult] = []
    for n in notifiers:
        try:
            r = await n.send(content)
        except NotImplementedError as e:
            logger.warning("[%s] Provider 未实现: %s", n.name, e)
            r = SendResult(endpoint_name=n.name, success=False, error=f"not implemented: {e}")
        except Exception as e:
            logger.warning("[%s] 发送异常: %s", n.name, e)
            r = SendResult(endpoint_name=n.name, success=False, error=str(e))
        results.append(r)
    return results
```

**验证**:
- `uv run pyright core/notifiers/__init__.py`

**TDD 测试**（追加到 `tests/test_notifier_base.py`）:
```python
import pytest

from core.notifiers import (
    get_notifiers_for_subscription, send_to_subscription,
    GotifyNotifier, TelegramNotifier,
)
from shared.config import Config, EndpointConfig


def _cfg(endpoints):
    c = Config()
    c.endpoints = endpoints
    return c


def test_get_notifiers_empty_when_no_match():
    cfg = _cfg([EndpointConfig(name="a", url="u", token="t")])
    ns = get_notifiers_for_subscription(cfg, "bili", ["nonexistent"])
    assert ns == []


def test_get_notifiers_returns_gotify_by_default():
    cfg = _cfg([EndpointConfig(name="a", url="u", token="t")])  # kind default = gotify
    ns = get_notifiers_for_subscription(cfg, "bili", ["a"])
    assert len(ns) == 1
    assert isinstance(ns[0], GotifyNotifier)


def test_get_notifiers_preserves_order():
    cfg = _cfg([
        EndpointConfig(name="a", url="u1", token="t"),
        EndpointConfig(name="b", url="u2", token="t"),
    ])
    ns = get_notifiers_for_subscription(cfg, "bili", ["b", "a"])
    assert [n.name for n in ns] == ["b", "a"]


def test_get_notifiers_telegram_kind():
    cfg = _cfg([EndpointConfig(name="tg", url="u", token="t", kind="telegram")])
    ns = get_notifiers_for_subscription(cfg, "bili", ["tg"])
    assert isinstance(ns[0], TelegramNotifier)


@pytest.mark.asyncio
async def test_send_to_subscription_fan_out_both_succeed(monkeypatch: pytest.MonkeyPatch):
    cfg = _cfg([
        EndpointConfig(name="a", url="u1", token="t"),
        EndpointConfig(name="b", url="u2", token="t"),
    ])
    content = NotificationContent(platform="bili", source_id="x", title="t", author="a")

    from core.notifiers import gotify as g_mod
    async def _stub_send(self, c):
        return SendResult(endpoint_name=self.name, success=True)
    monkeypatch.setattr(g_mod.GotifyNotifier, "send", _stub_send)

    results = await send_to_subscription(cfg, "bili", ["a", "b"], content)

    assert len(results) == 2
    assert all(r.success for r in results)
    assert {r.endpoint_name for r in results} == {"a", "b"}


@pytest.mark.asyncio
async def test_send_to_subscription_continues_after_failure(monkeypatch: pytest.MonkeyPatch):
    cfg = _cfg([
        EndpointConfig(name="a", url="u1", token="t"),
        EndpointConfig(name="tg", url="u", token="t", kind="telegram"),  # NotImplementedError
    ])
    content = NotificationContent(platform="bili", source_id="x", title="t", author="a")

    from core.notifiers import gotify as g_mod
    async def _stub_send(self, c):
        return SendResult(endpoint_name=self.name, success=True)
    monkeypatch.setattr(g_mod.GotifyNotifier, "send", _stub_send)

    results = await send_to_subscription(cfg, "bili", ["a", "tg"], content)

    assert len(results) == 2
    assert results[0].success is True  # a (gotify) 成功
    assert results[1].success is False  # tg NotImplementedError 被吞
    assert "not implemented" in results[1].error
```

---

### Task 8: 删 `core/notifier.py` + `core/formatter.py` 通知格式化段

**文件**:
- 删 `core/notifier.py`（整个文件，369 行）— 所有功能已迁移到 `core/notifiers/`
- `core/formatter.py` — 不动（保留 `format_comment_highlights` 等通用函数；原 plan Task 5c 的 `format_*_notification` 在本方案被 `render_markdown` 取代，不再单独建文件）

**改动**: `git rm core/notifier.py`

**验证**: `uv run pyright .` 应该报 `platforms/*/handlers.py` 的 import 错误（预期，下个 Task 修复）

---

## 阶段 3: 调用方改造（依赖阶段 2）

### Task 9: 重构 3 个 `*_push` handler + detector 注入 subscription_ref + 清理 pipeline.py docstring

**文件**: `shared/protocols.py`, `shared/message_store.py`, `platforms/{bilibili,xiaohongshu,weibo}/handlers.py`, `core/pipeline.py`

**关键设计**: 采用 Path A（最小侵入方案）。detector 在 `store.add_new()` 时注入 `subscription_ref`（字符串，存 uid/user_id）；push handler 通过 `ctx.msg.subscription_ref` 精确匹配 `sub` 对象获取 `notify_endpoints`。不再做脆弱的 name/user_id 反查。

**Task 9.0** `shared/protocols.py` — 在 `MessageRecord` 末尾（`dynamic_text` 字段之后）追加字段：

```python
    subscription_ref: str = ""
```

**Task 9.1** `shared/message_store.py` — 更新 `add_new()` 签名、`data` dict 构造、`_msg_from_dict` 反序列化。

> **重要**：`add_new` 当前实现（line 192-203）**不是**直接 `MessageRecord(...)` ctor，而是构造 `data: dict = {...}` 字典写入 `self._messages[msg_id]` 后落盘，最后 `return self._msg_from_dict(msg_id, data)`。因此 `subscription_ref` 必须**同时**改两处，否则落盘会丢失：
> 1. `add_new` 内 `data: dict` 字典构造
> 2. `_msg_from_dict` 内 `return MessageRecord(...)` 反序列化

**改动 9.1a** `add_new` 签名加 `subscription_ref: str = ""` 参数：

```python
    def add_new(
        self,
        msg_id: str,
        platform: str,
        title: str = "",
        author: str = "",
        pubdate: int = 0,
        content_type: ContentType = ContentType.VIDEO,
        dynamic_text: str = "",
        subscription_ref: str = "",
    ) -> MessageRecord:
```

**改动 9.1b** `add_new` 方法体内 `data: dict = {...}`（line 193-203）末尾追加 key：

```diff
         data: dict = {
             "platform": platform,
             "content_type": content_type.value,
             "phase": Phase.DISCOVERED.value,
             "pubdate": pubdate,
             "title": title,
             "author": author,
             "created_at": now,
             "updated_at": now,
             "error": "",
+            "subscription_ref": subscription_ref,
         }
```

**改动 9.1c** `_msg_from_dict`（line 76-90）`return MessageRecord(...)` 末尾追加 kwarg：

```diff
         return MessageRecord(
             msg_id=msg_id,
             platform=data["platform"],
             content_type=ContentType(data["content_type"]),
             phase=Phase(data["phase"]),
             pubdate=data["pubdate"],
             title=data["title"],
             author=data["author"],
             created_at=data.get("created_at", 0.0),
             updated_at=data.get("updated_at", 0.0),
             error=data.get("error", ""),
             dynamic_text=data.get("dynamic_text", ""),
+            subscription_ref=data.get("subscription_ref", ""),
         )
```

**验证**:
- `uv run pyright shared/message_store.py`
- 落盘后 reload 验证字段不丢：构造 `MessageStore` → `add_new(..., subscription_ref="42")` → `save()` → 新 `MessageStore` 实例 → `get_message(msg_id).subscription_ref == "42"`

**Task 9.2** 三平台 detector 注入 subscription_ref

在每个 detector 的 `store.add_new(...)` 调用处，追加 `subscription_ref` 参数：

- `bilibili/handlers.py` `bili_detector` / `bili_dynamic_detector`:
  ```python
  subscription_ref=str(sub.uid),
  ```
- `xiaohongshu/handlers.py` `xhs_detector`:
  ```python
  subscription_ref=sub.user_id,
  ```
- `weibo/handlers.py` `weibo_detector`:
  ```python
  subscription_ref=sub.user_id,
  ```

**Task 9.3** 三平台 push handler 改用 subscription_ref 精确匹配

**改动 9.3a** `platforms/bilibili/handlers.py`（替换 line 14 import 和 `bili_push`）:

```python
# line 14: 替换
# from core.notifier import notify_new_video
# 改为:
from core.notifiers import send_to_subscription
from shared.protocols import NotificationContent
```

```python
@PipelineEngine.register("bili", Phase.PUSHED)
async def bili_push(ctx: PhaseContext) -> bool:
    """推送 B站通知（视频 / 动态），fan-out 到订阅声明的所有 endpoints。"""
    is_dynamic = ctx.msg.content_type == ContentType.DYNAMIC
    source_id = ctx.msg.msg_id.replace("bili_dyn:" if is_dynamic else "bili:", "")

    # 通过 subscription_ref 精确匹配订阅
    matched = None
    for sub in ctx.config.bilibili.subscriptions:
        if str(sub.uid) == ctx.msg.subscription_ref:
            matched = sub
            break
    if matched is None:
        logger.warning("未找到 subscription_ref=%s 对应的订阅，跳过通知", ctx.msg.subscription_ref)
        return True

    if not matched.notify_endpoints:
        logger.info("订阅 %s 未配置 endpoints，跳过通知", ctx.msg.msg_id)
        return True

    content = NotificationContent(
        platform="bili",
        source_id=source_id,
        title=ctx.msg.title,
        author=ctx.msg.author,
        summary=ctx.summary_text,
        keywords=ctx.keywords,
        comment_highlights=ctx.comment_highlights or "",
        url=(f"https://t.bilibili.com/{source_id}" if is_dynamic
             else f"https://www.bilibili.com/video/{source_id}"),
        type="dynamic" if is_dynamic else "content",
    )

    logger.info("推送 %s 到 %d 个端点...", ctx.msg.msg_id, len(matched.notify_endpoints))
    results = await send_to_subscription(
        ctx.config, "bili", matched.notify_endpoints, content,
    )
    ok = sum(1 for r in results if r.success)
    logger.info("通知推送完成 (%d/%d)", ok, len(results))

    # 媒体清理（仅视频）
    if (not is_dynamic
            and ctx.config.transcribe.delete_after_transcribe
            and ctx.downloaded_filepath is not None):
        try:
            cleanup_media(filepath=ctx.downloaded_filepath, source_id=source_id)
        except Exception as exc:
            logger.warning("媒体清理失败 %s: %s", ctx.msg.msg_id, exc)

    return True
```

**改动 9.3b** `platforms/xiaohongshu/handlers.py`（替换 line 13 import 和 `xhs_push`）:

```python
# line 13: 替换
# from core.notifier import notify_new_xhs_note
from core.notifiers import send_to_subscription
from shared.protocols import NotificationContent
```

```python
@PipelineEngine.register("xhs", Phase.PUSHED)
async def xhs_push(ctx: PhaseContext) -> bool:
    """推送小红书笔记通知。"""
    note_id = ctx.msg.msg_id.replace("xhs:", "")

    matched = None
    for sub in ctx.config.xiaohongshu.subscriptions:
        if sub.user_id == ctx.msg.subscription_ref:
            matched = sub
            break
    if matched is None:
        logger.warning("未找到 subscription_ref=%s 对应的订阅", ctx.msg.subscription_ref)
        return True
    if not matched.notify_endpoints:
        logger.info("订阅未配置 endpoints")
        return True

    content = NotificationContent(
        platform="xhs", source_id=note_id,
        title=ctx.msg.title, author=ctx.msg.author,
        summary=ctx.summary_text, keywords=ctx.keywords,
        comment_highlights=ctx.comment_highlights or "",
    )
    logger.info("推送 %s 到 %d 个端点...", ctx.msg.msg_id, len(matched.notify_endpoints))
    results = await send_to_subscription(ctx.config, "xhs", matched.notify_endpoints, content)
    ok = sum(1 for r in results if r.success)
    logger.info("通知推送完成 (%d/%d)", ok, len(results))

    if (ctx.config.transcribe.delete_after_transcribe
            and ctx.downloaded_filepath is not None):
        try:
            cleanup_media(filepath=ctx.downloaded_filepath, source_id=note_id)
        except Exception as exc:
            logger.warning("媒体清理失败 %s: %s", ctx.msg.msg_id, exc)
    return True
```

**改动 9.3c** `platforms/weibo/handlers.py`（替换 line 14 import 和 `weibo_push`）:

```python
# line 14: 替换
# from core.notifier import notify_new_weibo_post
from core.notifiers import send_to_subscription
from shared.protocols import NotificationContent
```

```python
@PipelineEngine.register("weibo", Phase.PUSHED)
async def weibo_push(ctx: PhaseContext) -> bool:
    """推送微博通知。"""
    post_id = ctx.msg.msg_id.replace("weibo:", "")

    matched = None
    for sub in ctx.config.weibo.subscriptions:
        if sub.user_id == ctx.msg.subscription_ref:
            matched = sub
            break
    if matched is None:
        logger.warning("未找到 subscription_ref=%s 对应的订阅", ctx.msg.subscription_ref)
        return True
    if not matched.notify_endpoints:
        logger.info("订阅未配置 endpoints")
        return True

    content = NotificationContent(
        platform="weibo", source_id=post_id,
        title=ctx.msg.title, author=ctx.msg.author,
        summary=ctx.summary_text, keywords=ctx.keywords,
        comment_highlights=ctx.comment_highlights or "",
    )
    logger.info("推送 %s 到 %d 个端点...", ctx.msg.msg_id, len(matched.notify_endpoints))
    results = await send_to_subscription(ctx.config, "weibo", matched.notify_endpoints, content)
    ok = sum(1 for r in results if r.success)
    logger.info("通知推送完成 (%d/%d)", ok, len(results))
    return True
```

**Task 9.4** — 更新 `core/pipeline.py` docstring（lines 10-12）：

原三行：
```
- notifier.notify_new_video(bvid, title, author, summary, keywords, comment_highlights, config)
- notifier.notify_new_xhs_note(note_id, title, author, summary, keywords, comment_highlights, xhs_noti_config)
- notifier.notify_dynamic(dynamic_info: dict, config: NotificationConfig) -> bool
```
替换为：
```
- notifiers.send_to_subscription(config, platform, endpoint_names, content) -> list[SendResult]
```

**Task 9.5** — `tests/test_pipeline_e2e.py` 和 `tests/test_pipeline_concurrent.py`：执行者第一件事运行：

```bash
rg "notify_|core\.notifier|send_to_subscription" tests/test_pipeline_e2e.py tests/test_pipeline_concurrent.py
```

- **无命中** → 跳过本子任务，两文件无需改动。
- **有命中** → 把对应 `monkeypatch.setattr(...)` 的目标从 `core.notifier.notify_*` 改为 `core.notifiers.send_to_subscription`（签名变化：旧 `notify_*` 接受位置参数，新 `send_to_subscription(config, platform, endpoint_names, content)` 接受 4 个参数）。改完后必须 `uv run pytest tests/test_pipeline_e2e.py tests/test_pipeline_concurrent.py -x` 全绿。

**验证**:
- `uv run pyright shared/protocols.py`
- `uv run pyright shared/message_store.py`
- `uv run pyright platforms/`
- `uv run pyright core/pipeline.py`
- `uv run pytest tests/test_platform_handlers.py -x`（如有失败，按需 mock `send_to_subscription`）
- `grep -r "subscription_ref" shared/ platforms/` → 命中 `protocols.py` + `message_store.py` + 三平台 detector + 三平台 push handler

---

## 阶段 4: 配置示例 + Web UI（依赖阶段 1）

### Task 10: 更新配置示例

**文件**:
- `config/config.toml.example` — 删 3 个 `[*.notification]` 段，加 `[[endpoints]]`
- `config/subscriptions.toml` — 每个订阅加 `notify_endpoints = ["default"]`

**改动 10a**（`config/config.toml.example`）:
- 删除 `[bilibili.notification]` / `[xiaohongshu.notification]` / `[weibo.notification]` 三段
- 在文件末尾追加：
```toml

# ── Gotify 推送端点 ─────────────────────────────────────────────
# 端点全局定义，订阅中通过 notify_endpoints = ["name"] 引用
# kind 可选 "gotify"（默认） / "telegram"（占坑） / "email"（占坑）
[[endpoints]]
name = "default"
url = ""
token = ""
priority = 5
enabled = true
kind = "gotify"
```

**改动 10b**（`config/subscriptions.toml`，每个订阅条目加字段）:
```toml
[[bilibili.subscriptions]]
uid = 2137589551
name = "李大霄"
notify_endpoints = ["default"]

[[xiaohongshu.subscriptions]]
user_id = "5a7d3ed311be106d0306e7d6"
name = "Angelababy"
notify_endpoints = ["default"]

[[weibo.subscriptions]]
user_id = "2803301701"
name = "人民日报"
notify_endpoints = ["default"]
```

**验证**: 手动 `uv run python -c "import asyncio; from shared.config import load_config; cfg = asyncio.run(load_config()); print(cfg.endpoints)"` 应打印 endpoint 列表

---

### Task 11: Web endpoint CRUD（新页面）

**新文件**: `web/routes/endpoints.py` + `web/templates/endpoints.html`
**改动**: `web/app.py` 注册 router；`web/templates/base.html` 加侧栏导航

**改动 11a** `web/routes/endpoints.py`（基于已有 plan Task 8，**去掉 linked 反查逻辑**）:

```python
"""推送端点 CRUD 路由（简化版，不做双向关联 UI）。"""

from __future__ import annotations

from pathlib import Path

import tomlkit
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from shared.config import EndpointConfig, load_config
from web.app import TEMPLATES

router = APIRouter()
CONFIG_PATH = "config/config.toml"


async def _load_endpoints() -> list[EndpointConfig]:
    cfg = await load_config()
    return cfg.endpoints


def _save_endpoints(endpoints: list[EndpointConfig]) -> None:
    p = Path(CONFIG_PATH)
    doc = tomlkit.parse(p.read_text(encoding="utf-8")) if p.exists() else tomlkit.document()
    aot = tomlkit.aot()
    for ep in endpoints:
        t = tomlkit.table()
        t["name"] = ep.name
        t["url"] = ep.url
        t["token"] = ep.token
        t["priority"] = ep.priority
        t["enabled"] = ep.enabled
        t["kind"] = ep.kind
        aot.append(t)
    doc["endpoints"] = aot
    p.write_text(tomlkit.dumps(doc), encoding="utf-8")
```

> **格式保留说明**：`tomlkit.parse` 保留原文件注释和格式，只替换 `doc["endpoints"]` 引用的 aot；其他段（`[general]` / `[bilibili]` / `[xiaohongshu]` / `[weibo]` 等）的字节序列不变，仅 `[endpoints]` 段重写。这是 tomlkit 相对 `tomllib + json.dumps` 的关键优势。


@router.get("/endpoints", response_class=HTMLResponse)
async def endpoints_page(request: Request) -> HTMLResponse:
    endpoints = await _load_endpoints()
    return TEMPLATES.TemplateResponse(
        request, "endpoints.html",
        {"active_nav": "endpoints", "endpoints": endpoints},
    )


@router.post("/endpoints/add")
async def endpoint_add(
    name: str = Form(...),
    url: str = Form(...),
    token: str = Form(...),
    priority: int = Form(5),
    kind: str = Form("gotify"),
) -> HTMLResponse:
    endpoints = await _load_endpoints()
    if any(ep.name == name for ep in endpoints):
        return HTMLResponse(
            content="", status_code=400,
            headers={"HX-Trigger": '{"toast":{"msg":"端点名称已存在","type":"error"}}'},
        )
    endpoints.append(EndpointConfig(name=name, url=url, token=token, priority=priority, kind=kind))
    _save_endpoints(endpoints)
    return HTMLResponse(
        content="",
        headers={"HX-Trigger": '{"toast":{"key":"endpoint.saved","type":"success"}}'},
    )


@router.post("/endpoints/{name}/edit")
async def endpoint_edit(
    name: str,
    url: str = Form(...),
    token: str = Form(...),
    priority: int = Form(5),
    enabled: bool = Form(False),
) -> HTMLResponse:
    endpoints = await _load_endpoints()
    for ep in endpoints:
        if ep.name == name:
            ep.url = url
            ep.token = token
            ep.priority = priority
            ep.enabled = enabled
            break
    else:
        return HTMLResponse(
            content="", status_code=404,
            headers={"HX-Trigger": '{"toast":{"msg":"端点不存在","type":"error"}}'},
        )
    _save_endpoints(endpoints)
    return HTMLResponse(
        content="",
        headers={"HX-Trigger": '{"toast":{"key":"endpoint.saved","type":"success"}}'},
    )


@router.post("/endpoints/{name}/delete")
async def endpoint_delete(name: str) -> HTMLResponse:
    endpoints = await _load_endpoints()
    endpoints = [ep for ep in endpoints if ep.name != name]
    _save_endpoints(endpoints)
    return HTMLResponse(
        content="",
        headers={"HX-Trigger": '{"toast":{"key":"endpoint.deleted","type":"success"}}'},
    )
```

**改动 11b** `web/templates/endpoints.html`（基于已有 plan，**去掉关联订阅区块**）:
```html
{% extends "base.html" %}
{% from "_macros.html" import field, toggle %}

{% block title %}推送端点 · Trawler{% endblock %}

{% block content %}
<h1 class="text-2xl font-semibold tracking-tight mb-1">推送端点</h1>
<p class="text-sm text-[var(--text-secondary)] mb-6">全局推送端点 · 在订阅页面为每个订阅选择端点</p>

<div class="space-y-3 mb-8">
{% for ep in endpoints %}
<div class="bg-[var(--card-bg)] backdrop-blur-[12px] rounded-[14px] p-5 shadow-[0_4px_14px_rgba(0,0,0,0.05)] border border-[var(--card-border)]">
  <div class="flex items-center justify-between mb-3">
    <div>
      <span class="font-semibold text-sm">{{ ep.name }}</span>
      <span class="ml-2 text-xs text-[var(--text-secondary)]">{{ ep.kind }} · 优先级 {{ ep.priority }}</span>
      {% if not ep.enabled %}<span class="ml-2 text-xs text-red-500">已禁用</span>{% endif %}
    </div>
    <div class="flex gap-2">
      <button onclick="toggleEdit('{{ ep.name }}')" class="text-xs text-[var(--text-secondary)] hover:text-apple-blue">编辑</button>
      <form hx-post="/endpoints/{{ ep.name }}/delete" hx-target="body" hx-confirm="确定删除端点 {{ ep.name }}？">
        <button type="submit" class="text-xs text-red-500 hover:text-red-700">删除</button>
      </form>
    </div>
  </div>
  <div class="text-xs text-[var(--text-secondary)] font-mono truncate">{{ ep.url }}</div>

  <form id="edit-{{ ep.name }}" class="mt-3 hidden grid grid-cols-1 md:grid-cols-2 gap-3"
        hx-post="/endpoints/{{ ep.name }}/edit" hx-target="body">
    {{ field("url", ep.url, "URL", width="full") }}
    {{ field("token", ep.token, "Token", type="password") }}
    {{ field("priority", ep.priority, "优先级", type="number", width="half") }}
    <div class="flex items-center pt-2">{{ toggle("enabled", ep.enabled, "启用") }}</div>
    <div class="col-span-2 flex gap-2 pt-2">
      <button type="submit" class="px-4 py-2 bg-apple-blue text-white rounded-[8px] text-sm font-medium hover:bg-blue-600">保存</button>
      <button type="button" onclick="toggleEdit('{{ ep.name }}')" class="px-4 py-2 text-sm rounded-[8px] border border-gray-300 dark:border-gray-600 text-[var(--text-secondary)]">取消</button>
    </div>
  </form>
</div>
{% else %}
<p class="text-sm text-[var(--text-secondary)]">暂无端点，请添加</p>
{% endfor %}
</div>

<div class="bg-[var(--card-bg)] backdrop-blur-[12px] rounded-[14px] p-5 shadow-[0_4px_14px_rgba(0,0,0,0.05)] border border-[var(--card-border)]">
  <h2 class="text-base font-semibold mb-3">添加端点</h2>
  <form hx-post="/endpoints/add" hx-target="body" class="grid grid-cols-1 md:grid-cols-2 gap-3">
    {{ field("name", "", "名称", placeholder="default") }}
    {{ field("kind", "gotify", "类型", placeholder="gotify") }}
    {{ field("url", "", "URL", placeholder="https://gotify.example.com", width="full") }}
    {{ field("token", "", "Token", type="password") }}
    {{ field("priority", "5", "优先级", type="number") }}
    <div class="col-span-2 pt-2">
      <button type="submit" class="px-4 py-2 bg-apple-blue text-white rounded-[8px] text-sm font-medium hover:bg-blue-600">添加</button>
    </div>
  </form>
</div>

<script>
function toggleEdit(name) {
  var form = document.getElementById('edit-' + name);
  if (form) form.classList.toggle('hidden');
}
</script>
{% endblock %}
```

**改动 11c** `web/app.py` — 注册 router：

```python
# web/app.py line 159（在现有 router import block 末尾追加）:
from web.routes.endpoints import router as endpoints_router

# web/app.py line 166（在现有 include_router block 末尾追加）:
app.include_router(endpoints_router)
```

**改动 11d** `web/templates/base.html` — 侧栏加"推送端点"导航项（在 auth 之后、设置之前）：

```html
<a href="/endpoints" class="flex items-center gap-3 px-3 py-2 rounded-[8px] text-sm transition-colors {% if active_nav == 'endpoints' %}bg-[var(--color-primary)]/10 text-[var(--color-primary)] font-medium{% else %}text-[var(--text-secondary)] hover:bg-black/5 dark:hover:bg-white/5 hover:text-[var(--text-primary)]{% endif %}">
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 2L11 13M22 2l-7 20-4-9-9-4 20-7z"/></svg>
  推送端点
</a>
```

**改动 11e** `web/templates/_macros.html` — 修复 toggle 宏导致 422 的问题

`_macros.html` 的 `toggle(name, checked, label)` 宏同时输出 hidden input（`value="false"`）
和 checkbox（`name=name, value="true"`）。FastAPI `enabled: bool = Form(False)` 收到
`["false", "true"]` 列表时返回 422。

**解决方案**：删除 hidden input 行，FastAPI `Form(False)` default 正确处理未勾选情况。

```diff
# web/templates/_macros.html，删除下面这行：
- <input type="hidden" name="{{ name }}" value="false">
```

**改动 11f** `web/templates/base.html` — 注册 endpoint 相关 toast key

将 `TOAST_KEY_MAP`（line 135）从：
```javascript
var TOAST_KEY_MAP = {
  'settings.saved': '设置已保存'
};
```
改为：
```javascript
var TOAST_KEY_MAP = {
  'settings.saved': '设置已保存',
  'endpoint.saved': '端点已保存',
  'endpoint.deleted': '端点已删除'
};
```

> **决策说明（方案 B）**：不注册 `endpoint.error_exists`。理由：前端 toast 解析（`base.html` line 144）`var msg = data.toast.msg || TOAST_KEY_MAP[data.toast.key] || '完成'` —— `msg` 优先于 `key`，错误分支用 `{"msg":"端点名称已存在","type":"error"}` 直接可用；若强行注册 key 反而要把错误文案 i18n 化，复杂度上升且错误信息内容具体（"端点不存在" vs "端点名称已存在"）不适合抽象为单一 key。删除即可。

同时更新 `endpoint_add` / `endpoint_edit` / `endpoint_delete` 路由的 toast key（不再复用 `settings.saved`）：

| 路由 | 原 toast | 改为 |
|---|---|---|
| `endpoint_add` 成功 | `settings.saved` | `endpoint.saved` |
| `endpoint_edit` 成功 | `settings.saved` | `endpoint.saved` |
| `endpoint_delete` 成功 | `{"msg":"端点已删除","type":"success"}` | `endpoint.deleted` |
| 名称重复/不存在的错误 | 保持 `{"msg":"...","type":"error"}` 风格（具体错误信息不走 key map，由前端 line 144 `data.toast.msg ||` 直接取值） | 不变 |

**验证**:
- `uv run pyright web/routes/endpoints.py`
- 手动启动 web，访问 `/endpoints`，验证列表/新增/编辑/删除

---

### Task 12: Web 订阅表单 endpoint multi-select + settings 清理

**文件**: `web/routes/subscriptions.py` + `web/templates/subscriptions.html` + `web/routes/settings.py` + `web/templates/settings.html`

**改动 12a** `web/routes/subscriptions.py`：参考已有 plan Task 9a，新增 `POST /subscriptions/{platform}/{identifier}/endpoints` 路由（add/remove）；`subscriptions_page` 加载 `available_endpoints` 传入模板

**改动 12b** `web/templates/subscriptions.html`：每个订阅项加 endpoint badges（带 × 按钮移除）+ "添加端点"下拉

**改动 12c** `web/routes/settings.py`：删 gotify_url / gotify_token_bili/xhs/weibo Form 字段，删 notification 写盘 block

**改动 12d** `web/templates/settings.html`：删 Gotify card，替换为指向 `/endpoints` 的链接卡片

**验证**:
- `uv run pytest tests/test_web_settings.py tests/test_web_subscriptions.py -x`
- 手动：新增/移除 endpoint 引用；`/settings` 不再显示 Gotify 字段

---

## 验收清单

### 自动化
- [ ] `uv run ruff check .` 不新增问题
- [ ] `uv run pyright .` 不新增 error
- [ ] `uv run pytest -x` 全绿

### 手动（端到端）
- [ ] `config/config.toml` 配置 `[[endpoints]]` 含 2 个 endpoint
- [ ] `config/subscriptions.toml` 某订阅 `notify_endpoints = ["a", "b"]`
- [ ] `uv run trawler check --platform bilibili` → 触发新视频 → 两 endpoint 都收到推送
- [ ] 关闭 endpoint `b`（enabled=false）→ 只 `a` 收到
- [ ] 订阅引用不存在的 endpoint name → 日志 warning，不 crash
- [ ] Web `/endpoints` 新增/编辑/删除/启停正常
- [ ] Web `/subscriptions` 为订阅勾选 endpoint 后，cron 推送能反映
- [ ] Web `/settings` 不再有 Gotify 字段

### 架构验证
- [ ] `grep -r "from core.notifier" platforms/ web/` → 无结果（旧模块已删）
- [ ] `grep -r "NotificationConfig" shared/ platforms/ web/` → 无结果（已删）
- [ ] `grep -r "send_to_subscription\|get_notifiers_for_subscription" platforms/ web/` → 仅 `platforms/*/handlers.py` 命中（未来 web 加测试按钮时再扩展，但工厂已就绪）
- [ ] `grep -r "subscription_ref" shared/ platforms/` → 命中 `protocols.py` + `message_store.py` + 三平台 detector + 三平台 push handler
- [ ] 提交含 toggle checkbox 的表单（如 endpoint 编辑的 `enabled` 字段）→ 不再返回 422（依赖 Task 11e 修复 `_macros.html` toggle 宏）

---

## 风险清单

1. **endpoint rename 不级联** — 改名后订阅里的旧 name 静默跳过 + warning（设计如此）。
2. **tomlkit 数组写回** — 订阅 endpoint 列表写回时用 `tomlkit.array(eps_list).multiline(True)`，避免 inline 格式跳变。
3. **测试 mock 复杂度** — `GotifyNotifier.send` 的 aiohttp mock 较繁琐（嵌套 async context manager）；测试用 `MagicMock + AsyncMock` 组合，已验证可行。
4. **Provider 抽象的过度设计风险** — Telegram/Email stub 暂时只有 `NotImplementedError`，看似"为未来设计"。但这是用户明确要求"占坑"，且 Notifier Protocol 让 GotifyNotifier 的形状更清晰，可接受。

> 已消除风险（v2 修订）：
> - ~~`sub` 反查的脆弱性~~ — 改用 `subscription_ref` 精确匹配后消除（detector 注入 uid/user_id，push handler 直接 `==` 比较）。

---

## 实施顺序（依赖图）

```
阶段 1（可并行起手）:
  Task 1 (Notifier Protocol) ─┐
  Task 2 (EndpointConfig)     │
                              ├→ Task 3 (修 test_config.py)
                              │
阶段 2:                        │
  Task 4 (render_markdown) ───┤
                              ├→ Task 5 (GotifyNotifier)
                              ├→ Task 6 (Telegram/Email stub)
                              └→ Task 7 (__init__ 工厂)
                                     │
                                     └→ Task 8 (删 core/notifier.py)

阶段 3（依赖阶段 2）:
  Task 9 (3 个 push handlers) — 依赖 Task 7+8

阶段 4（与阶段 3 并行，仅依赖阶段 1）:
  Task 10 (配置示例) ─ 独立
  Task 11 (endpoint CRUD Web) ─ 依赖 Task 1+2
  Task 12 (订阅 multi-select + settings 清理) ─ 依赖 Task 11
```

**预估工时**: ~3-4 小时（阶段 1+2 约 1.5h，阶段 3 约 0.5h，阶段 4 约 1.5h，含测试）

---

## Revision History

- **2026-06-18 v1** — 基于 2026-06-16-c-gotify-multi-url.md 精简：
  - 删除：MessageRecord.subscription_id、迁移脚本、双向关联 UI、README 更新
  - 升级：从"send_gotify 改签名"升级为"Notifier Protocol + Provider 抽象"
  - 新增：`core/notifiers/` 目录结构、`render_markdown` 统一渲染、`get_notifiers_for_subscription()` 共享工厂
  - 简化：push handler 反查订阅改为 name/user_id 匹配（不引入 subscription_id 字段）

- **2026-06-18 v2** — @oracle review 反馈修订（3 critical + 6 major + 2 minor）：
  - **[critical] Task 9 重写**：原 name/user_id 反查在真实 msg_id 格式（`bili:{bvid}` / `xhs:{note_id}` 等）下 100% 失败。改为 Path A：新增 `MessageRecord.subscription_ref` 字段，detector 在 `store.add_new()` 时注入 `str(sub.uid)` / `sub.user_id`，push handler 用 `==` 精确匹配。三平台 detector + push handler 全部给出完整 diff，反查逻辑彻底去除。
  - **[critical] Task 3 补全**：新增 `tests/test_web_settings.py` lines 22-25 删除项；额外发现 `tests/test_token_store.py` line 27 / 74-76 也引用 `[bilibili.notification]`，加入清理清单。
  - **[critical] Task 9 补全**：新增 9.4 清理 `core/pipeline.py` docstring 三行 notifier 签名引用；9.5 明确 `test_pipeline_e2e.py` / `test_pipeline_concurrent.py` 经 grep 确认无 notifier 引用，无需改动。
  - **[major] Issue 4**：Task 9 三平台 push handler 改造代码全部用 `logger.info/warning`，不引入 `console.print`（handlers.py 未 import console）。
  - **[major] Issue 5**：Task 4 `render_markdown` 删除恒真过滤死代码 `parts = [p for p in parts if p != "" or True]`。
  - **[major] Issue 6**：Task 7 `_build_notifier` 不再静默降级为 GotifyNotifier，未知 kind 改为 warning + skip。
  - **[major] Issue 7**：Task 11 新增 11e 子任务——修复 `_macros.html` toggle 宏同时输出 hidden + checkbox 导致 FastAPI 收到 list 返回 422 的 bug；删除 hidden input 行。
  - **[major] Issue 8**：Task 11c 给出 `web/app.py` 完整 diff（import + include_router 两行明确位置）。
  - **[major] Issue 9**：Task 11 新增 11f 子任务——endpoint CRUD 不再复用 `settings.saved` toast，新注册 `endpoint.saved` / `endpoint.deleted` / `endpoint.error_exists` 到 `base.html` `TOAST_KEY_MAP`。
  - **[major] Issue 10**：Task 11d 给出完整 `<a>` 导航 HTML（含 SVG 图标），位置明确（auth 之后、settings 之前）。
  - **[minor] Issue 11**：Task 4 末尾追加决策说明——为何去掉"发布时间"字段。
  - **[minor] Issue 12**：Task 7 fan-out 测试改为 `monkeypatch: pytest.MonkeyPatch` fixture 风格，与项目其他测试一致；同步去除转换残留的过度缩进和死代码 `fake_send`。
  - **同步更新**：决策摘要 / 取舍表 / 范围 / 风险清单（删除"反查脆弱"项，移入"已消除风险"备注）/ 验收清单（新增 subscription_ref grep + toggle 422 验证）。

- **2026-06-19 v3** — @oracle 二次复审残留问题修订（1 critical + 2 major + 1 minor）：
  - **[critical] Task 9.1 精确化**：原文写"在 `MessageRecord(...)` 创建处加 subscription_ref"，但 `add_new` 实际是构造 `data: dict` 字典落盘（line 192-203）后 `return self._msg_from_dict(...)`，**不是**直接 ctor。改为显式 diff 覆盖两个落点：9.1a 签名、9.1b `data` dict 加 key、9.1c `_msg_from_dict` 反序列化加 kwarg。明确指出"必须同时改 dict 构造和反序列化，否则落盘丢失"，并加落盘回归验证步骤。
  - **[major] Task 9.5 改为实测**：原文"经 grep 确认无引用"是断言非证据。改为执行指引：执行者第一件事运行 `rg "notify_|core\.notifier|send_to_subscription" tests/test_pipeline_e2e.py tests/test_pipeline_concurrent.py`，无命中跳过 / 有命中改 monkeypatch 目标。
  - **[major] Task 11f 选方案 B**：删除 `endpoint.error_exists` 注册。理由：`base.html` line 144 `data.toast.msg || TOAST_KEY_MAP[key]` 的 `msg` 优先短路使错误分支（`{"msg":"端点名称已存在"}`）天然可用，注册 key 是死代码；同步更新错误分支表格行。
  - **[minor] Task 11a tomlkit 保格式说明**：`_save_endpoints` 用 `tomlkit.parse` + `doc["endpoints"] = aot` 模式，明确写出"仅替换 `[endpoints]` 段，其他段注释和格式不变"。

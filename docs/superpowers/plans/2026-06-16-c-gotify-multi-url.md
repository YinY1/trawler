# Plan: C 组 Gotify 多 URL 重构 + 双向关联

## 背景

将三平台独立的 `NotificationConfig`（gotify_url + gotify_token 各存一份）重构为全局 `[[endpoints]]` 列表 + 订阅侧 `notify_endpoints: list[str]` 引用。endpoint 卡片的"关联订阅"是派生视图（运行时反查）。MessageRecord 新增 `subscription_id` 单值字段用于 push 路由。

## 范围

涉及 18+ 文件：config/protocols/message_store → 三个平台 detectors/push handlers → notifier/formatter → web routes/templates → 迁移脚本 + 配置示例 + 测试。

## 决策摘要

- **Source of truth 单点**：订阅侧 `notify_endpoints: list[str]`
- **派生视图**：endpoint 反查订阅（运行时遍历所有平台订阅，非双写）
- **MessageRecord subscription_id 单值**：`"{platform}:{identifier}"`（如 `"bili:2137589551"`）
- **完全替换 NotificationConfig**：删除三平台 `notification` 字段 + `NotificationConfig` dataclass
- **两阶段实施**：阶段 1 数据通路 + endpoint CRUD + 订阅表单；阶段 2 双向关联 UI

---

## 阶段 1: 数据通路 + endpoint 管理 + 订阅编辑表单

### Task 1: 新增数据模型

**文件**: `shared/config.py` (行 126-177) + `shared/protocols.py` (行 342-355)

**改动 1a**: `shared/config.py` — 新增 `EndpointConfig`，`Config` 加 `endpoints`，三个订阅加 `notify_endpoints`，删除 `NotificationConfig` 及其引用

```python
# 替换行 126-134（删除 NotificationConfig dataclass）
# 新增在 NotificationConfig 原位置（行 126 附近）：

@dataclass
class EndpointConfig:
    """Gotify 推送端点配置"""
    name: str
    url: str
    token: str
    priority: int = 5
    enabled: bool = True
```

```python
# 三个订阅 dataclass 加 notify_endpoints（行 139-148）：

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

```python
# BilibiliConfig / XhsConfig / WeiboConfig 删除 notification 字段（行 154-177）：
# Before:
@dataclass
class BilibiliConfig:
    auth: BilibiliAuth = field(default_factory=BilibiliAuth)
    monitor: BilibiliMonitorConfig = field(default_factory=BilibiliMonitorConfig)
    subscriptions: list[BiliSubscription] = field(default_factory=list)
    notification: NotificationConfig = field(default_factory=NotificationConfig)

# After:
@dataclass
class BilibiliConfig:
    auth: BilibiliAuth = field(default_factory=BilibiliAuth)
    monitor: BilibiliMonitorConfig = field(default_factory=BilibiliMonitorConfig)
    subscriptions: list[BiliSubscription] = field(default_factory=list)

# 同理 XhsConfig、WeiboConfig 删除 notification 行
```

```python
# Config 加 endpoints 字段（行 189-200）：
@dataclass
class Config:
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

**改动 1b**: `shared/protocols.py` — MessageRecord 加 `subscription_id`（行 342-355）

```python
# After（仅加一行）：
@dataclass
class MessageRecord:
    msg_id: str
    platform: str
    content_type: ContentType
    phase: Phase
    pubdate: int
    title: str
    author: str
    created_at: float = 0.0
    updated_at: float = 0.0
    error: str = ""
    subscription_id: str = ""  # 新增：格式 "{platform}:{identifier}"
```

**测试影响**: `tests/test_config.py` — 共 **13+ 处** 要删/改：

BASE_TOML fixture（3 段整删）：
- 行 68-72: 删除 `[bilibili.notification]` 整段（enabled/gotify_url/gotify_token/priority）
- 行 81-85: 删除 `[xiaohongshu.notification]` 整段
- 行 94-98: 删除 `[weibo.notification]` 整段

Import（1 处删）：
- 行 17: 删除 `NotificationConfig,` 导入

测试方法（5 处）：
- 行 175: `assert cfg.bilibili.notification.gotify_url == ""` → 删除此行（或改为 `assert cfg.bilibili.notification` 不存在）
- 行 253-258: 整方法 `test_bilibili_notification` 删除（因 notification 字段不存在）
- 行 275: `assert cfg.xiaohongshu.notification.gotify_token == "xhs-token"` → 删除
- 行 288: `assert cfg.weibo.notification.gotify_token == "weibo-token"` → 删除
- 行 314: `assert cfg.bilibili.notification.gotify_url == ""` → 删除（或改为 endpoints 断言）

TestEnvOverrides 类（5 处）：
- 行 322-327: 整方法 `test_trawler_gotify_url` 删除
- 行 329-333: 整方法 `test_trawler_gotify_token_bili` 删除
- 行 335-339: 整方法 `test_trawler_gotify_token_xhs` 删除
- 行 341-345: 整方法 `test_trawler_gotify_token_weibo` 删除
- 行 371-379: 整方法 `test_env_override_with_empty_config` 中删除 FEEDFLOW_GOTIFY_URL 相关断言（行 377）

Dataclass defaults（5 处）：
- 行 459-464: `test_notification_config_defaults` 改为 `test_endpoint_config_defaults`
- 行 460: `n = NotificationConfig()` → `EndpointConfig(name="test", url="http://", token="x")`
- 行 480: `assert isinstance(b.notification, NotificationConfig)` → 删除
- 行 488: `assert isinstance(x.notification, NotificationConfig)` → 删除
- 行 496: `assert isinstance(w.notification, NotificationConfig)` → 删除

---

### Task 2: 配置解析与 env override

**文件**: `shared/config.py`

**改动 2a**: `_parse_config` 解析 `[[endpoints]]` 段（在行 227-283 的 `_parse_config` 函数中，return cfg 之前追加）

```python
# 在 return cfg 之前（行 283 之前）添加：
    # endpoints
    if eps := raw.get("endpoints"):
        cfg.endpoints = [EndpointConfig(**ep) for ep in eps]
```

**改动 2b**: 删除三个平台的 notification 解析逻辑（行 253 附近）

```python
# Before（行 249-254）：
    if bili := raw.get("bilibili"):
        auth = _dict_to_dataclass(BilibiliAuth, bili.get("auth", {}))
        monitor = _dict_to_dataclass(BilibiliMonitorConfig, bili.get("monitor", {}))
        subs = [BiliSubscription(**s) for s in bili.get("subscriptions", [])]
        noti = _dict_to_dataclass(NotificationConfig, bili.get("notification", {}))
        cfg.bilibili = BilibiliConfig(auth=auth, monitor=monitor, subscriptions=subs, notification=noti)

# After：
    if bili := raw.get("bilibili"):
        auth = _dict_to_dataclass(BilibiliAuth, bili.get("auth", {}))
        monitor = _dict_to_dataclass(BilibiliMonitorConfig, bili.get("monitor", {}))
        subs = [BiliSubscription(**s) for s in bili.get("subscriptions", [])]
        cfg.bilibili = BilibiliConfig(auth=auth, monitor=monitor, subscriptions=subs)

# 同理 xiaohongshu（行 257-268）和 weibo（行 271-282）
```

**改动 2c**: `_apply_env_overrides` 删除 FEEDFLOW_GOTIFY_* 逻辑（行 287-308）

```python
# Before（行 287-308）：
def _apply_env_overrides(cfg: Config) -> None:
    """环境变量覆盖配置值，优先级高于配置文件"""
    # Gotify
    if v := os.environ.get("FEEDFLOW_GOTIFY_URL"):
        cfg.bilibili.notification.gotify_url = v
    if v := os.environ.get("FEEDFLOW_GOTIFY_TOKEN_BILI"):
        cfg.bilibili.notification.gotify_token = v
    if v := os.environ.get("FEEDFLOW_GOTIFY_TOKEN_XHS"):
        cfg.xiaohongshu.notification.gotify_token = v
    if v := os.environ.get("FEEDFLOW_GOTIFY_TOKEN_WEIBO"):
        cfg.weibo.notification.gotify_token = v
    # 平台 cookies
    ...

# After：
def _apply_env_overrides(cfg: Config) -> None:
    """环境变量覆盖配置值，优先级高于配置文件"""
    # 平台 cookies
    ...
```

**改动 2d**: `load_config` 检测旧 `[bilibili.notification]` 时 warn（在 load_config 函数末尾，行 363 之前）

```python
# 在 cfg = _parse_config(raw) 和 _apply_env_overrides(cfg) 之间添加：
    # 检测旧版 per-platform notification 配置
    for plat in ("bilibili", "xiaohongshu", "weibo"):
        if isinstance(raw.get(plat), dict) and "notification" in raw[plat]:
            import warnings
            warnings.warn(
                f"检测到 {plat}.notification 旧配置。请运行 `python scripts/migrate_notifications.py` 迁移到全局 [[endpoints]]。",
                UserWarning,
                stacklevel=2,
            )
```

---

### Task 3: MessageStore.add_new 加 subscription_id

**文件**: `shared/message_store.py`

**改动**: `add_new` 签名加 `subscription_id` 参数，写入 dict（行 140-175）

```python
# Before：
    def add_new(
        self,
        msg_id: str,
        platform: str,
        content_type: ContentType,
        pubdate: int,
        title: str,
        author: str,
    ) -> MessageRecord | None:

# After：
    def add_new(
        self,
        msg_id: str,
        platform: str,
        content_type: ContentType,
        pubdate: int,
        title: str,
        author: str,
        subscription_id: str = "",
    ) -> MessageRecord | None:
```

`data` dict 中追加 `"subscription_id": subscription_id`：

```python
# 在行 162-172 的 data dict 中添加：
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
            "subscription_id": subscription_id,  # 新增
        }
```

`_msg_from_dict` 也要读取该字段（行 76-89）：

```python
    def _msg_from_dict(self, msg_id: str, data: dict) -> MessageRecord:
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
            subscription_id=data.get("subscription_id", ""),  # 新增，兼容旧记录
        )
```

---

### Task 4: detector 注入 subscription_id（三个平台）

**文件**: `platforms/bilibili/handlers.py`、`platforms/xiaohongshu/handlers.py`、`platforms/weibo/handlers.py`

**通用模式**: 每个 detector 循环订阅列表时，对每条消息的 `store.add_new()` 调用追加 `subscription_id=f"{platform}:{identifier}"`

**改动 4a**: `platforms/bilibili/handlers.py` — bili_detector（行 32-52）

```python
# Before（行 44-52）：
        for v in videos:
            store.add_new(
                msg_id=f"bili:{v.bvid}",
                platform="bili",
                content_type=ContentType.VIDEO,
                pubdate=v.pubdate,
                title=v.title,
                author=v.author,
            )

# After：
        for v in videos:
            store.add_new(
                msg_id=f"bili:{v.bvid}",
                platform="bili",
                content_type=ContentType.VIDEO,
                pubdate=v.pubdate,
                title=v.title,
                author=v.author,
                subscription_id=f"bili:{sub.uid}",
            )
```

**改动 4b**: bili_dynamic_detector（行 55-73）同理

```python
# Before（行 66-73）：
            store.add_new(
                msg_id=f"bili_dyn:{dyn.dynamic_id}",
                platform="bili",
                content_type=ContentType.DYNAMIC,
                pubdate=dyn.pubdate,
                title=dyn.title,
                author=dyn.author,
            )

# After：
            store.add_new(
                msg_id=f"bili_dyn:{dyn.dynamic_id}",
                platform="bili",
                content_type=ContentType.DYNAMIC,
                pubdate=dyn.pubdate,
                title=dyn.title,
                author=dyn.author,
                subscription_id=f"bili:{sub.uid}",
            )
```

**改动 4c**: `platforms/xiaohongshu/handlers.py` — xhs_detector（行 31-48）

```python
# Before（行 41-48）：
        for n in notes:
            store.add_new(
                msg_id=f"xhs:{n.note_id}",
                platform="xhs",
                content_type=ContentType.VIDEO if n.note_type == "video" else ContentType.TEXT,
                pubdate=n.pubdate,
                title=n.title,
                author=n.author,
            )

# After：
        for n in notes:
            store.add_new(
                msg_id=f"xhs:{n.note_id}",
                platform="xhs",
                content_type=ContentType.VIDEO if n.note_type == "video" else ContentType.TEXT,
                pubdate=n.pubdate,
                title=n.title,
                author=n.author,
                subscription_id=f"xhs:{sub.user_id}",
            )
```

**改动 4d**: `platforms/weibo/handlers.py` — weibo_detector（行 33-50）

```python
# Before（行 43-50）：
        for p in posts:
            store.add_new(
                msg_id=f"weibo:{p.post_id}",
                platform="weibo",
                content_type=ContentType.TEXT,
                pubdate=p.pubdate,
                title=p.clean_text[:50] if p.clean_text else p.post_id,
                author=p.author,
            )

# After：
        for p in posts:
            store.add_new(
                msg_id=f"weibo:{p.post_id}",
                platform="weibo",
                content_type=ContentType.TEXT,
                pubdate=p.pubdate,
                title=p.clean_text[:50] if p.clean_text else p.post_id,
                author=p.author,
                subscription_id=f"weibo:{sub.user_id}",
            )
```

---

### Task 5: notifier 重构

**文件**: `core/notifier.py` + `core/formatter.py`

**改动 5a**: `core/notifier.py` — `send_gotify` 签名改为接受 `EndpointConfig`（行 20-87）

```python
# Before（行 11, 20-25）：
from shared.config import NotificationConfig

async def send_gotify(
    title: str,
    message: str,
    config: NotificationConfig,
    priority: int | None = None,
) -> bool:

# After：
from shared.config import EndpointConfig

async def send_gotify(
    title: str,
    message: str,
    endpoint: EndpointConfig,
    priority: int | None = None,
) -> bool:
```

内部逻辑调整（移除去 `enabled` 检查，改为用 `endpoint.enabled` + `endpoint.url` + `endpoint.token` + `endpoint.priority`）：

```python
# 替换行 40-53：
    if not endpoint.enabled:
        console.log("[dim]端点通知已禁用[/]")
        return False

    if not endpoint.url or not endpoint.token:
        console.log("[yellow]Gotify 端点配置不完整（缺少 URL 或 Token）[/]")
        return False

    url = f"{endpoint.url.rstrip('/')}/message"
    params = {"token": endpoint.token}
    payload: dict[str, str | int] = {
        "title": title,
        "message": message,
        "priority": priority if priority is not None else endpoint.priority,
    }
```

**改动 5b**: `core/notifier.py` — 删除 4 个高层包装函数（行 90-369）

删除以下函数及其中间注释 section：
- `notify_new_video` (行 90-162)
- `notify_new_xhs_note` (行 165-235)
- `notify_dynamic` (行 238-296)
- `notify_new_weibo_post` (行 299-369)

保留文件末尾多余的 section 分隔符（清掉即可）。

导入行中也删除不再需要的 `from datetime import datetime`（如果无其他引用）。

**改动 5c**: `core/formatter.py` — 新增通知格式化函数

在 `format_comment_highlights` 之后追加：

```python

# ═══════════════════════════════════════════════════════════
# 通知内容格式化
# ═══════════════════════════════════════════════════════════


def format_video_notification(
    title: str,
    author: str,
    bvid: str,
    summary: str,
    keywords: list[str],
    comment_highlights: str | None = None,
) -> tuple[str, str]:
    """格式化视频通知 (title, message_markdown)。"""
    keywords_str = "；".join(keywords) if keywords else "无"
    video_url = f"https://www.bilibili.com/video/{bvid}"

    parts: list[str] = [
        f"**UP主:** {author}",
        f"**链接:** [{bvid}]({video_url})",
        f"**关键词:** {keywords_str}",
        "",
        "---",
        "",
        "**详情:**",
        summary,
    ]

    if comment_highlights:
        parts.extend(["", "**评论区补充:**", comment_highlights])

    message = "\n".join(parts)
    return f"📹 {title}", message


def format_xhs_notification(
    title: str,
    author: str,
    note_id: str,
    summary: str,
    keywords: list[str],
    comment_highlights: str | None = None,
) -> tuple[str, str]:
    """格式化小红书笔记通知 (title, message_markdown)。"""
    keywords_str = "；".join(keywords) if keywords else "无"
    note_url = f"https://www.xiaohongshu.com/explore/{note_id}"

    parts: list[str] = [
        f"**作者:** {author}",
        f"**链接:** [{note_id}]({note_url})",
        f"**关键词:** {keywords_str}",
        "",
        "---",
        "",
        "**详情:**",
        summary,
    ]

    if comment_highlights:
        parts.extend(["", "**评论区补充:**", comment_highlights])

    message = "\n".join(parts)
    return f"📕 {title}", message


def format_weibo_notification(
    title: str,
    author: str,
    post_id: str,
    summary: str,
    keywords: list[str],
    comment_highlights: str | None = None,
) -> tuple[str, str]:
    """格式化微博通知 (title, message_markdown)。"""
    keywords_str = "；".join(keywords) if keywords else "无"
    post_url = f"https://weibo.com/{post_id}"

    parts: list[str] = [
        f"**作者:** {author}",
        f"**链接:** [{post_id}]({post_url})",
        f"**关键词:** {keywords_str}",
        "",
        "---",
        "",
        "**详情:**",
        summary,
    ]

    if comment_highlights:
        parts.extend(["", "**评论区补充:**", comment_highlights])

    message = "\n".join(parts)
    return f"🐦 {title}", message


def format_dynamic_notification(
    user: str,
    content: str,
    dynamic_type: str = "动态",
    dynamic_id: str = "",
    url: str = "",
) -> tuple[str, str]:
    """格式化动态通知 (title, message_markdown)。"""
    parts: list[str] = [
        f"**用户:** {user}",
    ]

    if url:
        link_text = dynamic_id if dynamic_id else "查看详情"
        parts.append(f"**链接:** [{link_text}]({url})")

    parts.extend(["", "---", "", str(content)])

    message = "\n".join(parts)
    title_text = f"📢 {user} 的{dynamic_type}"
    if dynamic_type != "动态":
        title_text = f"📢 {user} - {dynamic_type}"

    return title_text, message
```

**注意**：新格式化函数去掉了原 `notify_new_video`/`notify_new_xhs_note`/`notify_new_weibo_post` 中的 `**发布时间:**` 字段（原函数用 `datetime.now()` 获取推送时间，不是发布者实际发布时间）。`pubdate` 已在 MessageRecord 中保留，如有需要可在格式化时从 `ctx.msg.pubdate` 获取恢复。

---

### Task 6: push handler 改反查 endpoints

**通用模式**: push handler 从 `ctx.msg.subscription_id` 反查订阅 → 遍历 `notify_endpoints` → 查 `EndpointConfig` → 调 `send_gotify`

新增一个辅助函数 `_find_endpoint`（可放在 `core/notifier.py` 末尾或 `shared/config.py` 中）：

```python
# 在 core/notifier.py 末尾追加：
from shared.config import EndpointConfig


def find_endpoint(config: Config, name: str) -> EndpointConfig | None:
    """按名称查找 EndpointConfig，不存在返回 None。"""
    for ep in config.endpoints:
        if ep.name == name:
            return ep
    return None
```

**改动 6a**: `platforms/bilibili/handlers.py` — bili_push（行 206-256）

```python
# 更新 import（行 16-17）：
# 删除旧导入: from core.notifier import notify_new_video（如果存在）
# 新增:
from core.formatter import format_comment_highlights, format_video_notification, format_dynamic_notification
from core.notifier import find_endpoint, send_gotify

# 完整替换 bili_push 函数（行 206-256）：
@PipelineEngine.register("bili", Phase.PUSHED)
async def bili_push(ctx: PhaseContext) -> bool:
    """推送 B站通知（视频 / 动态），按 subscription_id 路由到端点。"""
    # 旧记录跳过（subscription_id="" 表示旧记录已 push 过，不重发；文件清理留给原 pipeline 逻辑）
    if not ctx.msg.subscription_id:
        logger.info("Skipping push for %s: no subscription_id (old record)", ctx.msg.msg_id)
        return True

    # 解析 subscription_id → 查找订阅 → 获取 endpoints
    parts = ctx.msg.subscription_id.split(":", 1)
    if len(parts) != 2:
        logger.warning("Invalid subscription_id: %s", ctx.msg.subscription_id)
        return True

    _plat, identifier = parts
    matched = None
    for sub in ctx.config.bilibili.subscriptions:
        if str(sub.uid) == identifier:
            matched = sub
            break

    if matched is None:
        logger.warning("Subscription %s not found in config", ctx.msg.subscription_id)
        return True

    endpoints_to_notify = matched.notify_endpoints
    if not endpoints_to_notify:
        logger.info("No endpoints configured for subscription %s", ctx.msg.subscription_id)
        return True

    # 构建通知内容
    if ctx.msg.content_type == ContentType.DYNAMIC:
        dynamic_id = ctx.msg.msg_id.replace("bili_dyn:", "")
        title, message = format_dynamic_notification(
            user=ctx.msg.author,
            content=ctx.summary_text or ctx.msg.title,
            dynamic_type="动态",
            dynamic_id=dynamic_id,
            url=f"https://t.bilibili.com/{dynamic_id}",
        )
    else:
        bvid = ctx.msg.msg_id.replace("bili:", "")
        title, message = format_video_notification(
            title=ctx.msg.title,
            author=ctx.msg.author,
            bvid=bvid,
            summary=ctx.summary_text,
            keywords=ctx.keywords,
            comment_highlights=ctx.comment_highlights or None,
        )

    # 遍历 endpoints 发送
    console.print(f"  [dim]🔔 推送通知到 {len(endpoints_to_notify)} 个端点...[/]")
    for ep_name in endpoints_to_notify:
        ep = find_endpoint(ctx.config, ep_name)
        if ep is None:
            logger.warning("Endpoint %s not found (referenced by subscription %s)", ep_name, ctx.msg.subscription_id)
            continue
        try:
            await send_gotify(title=title, message=message, endpoint=ep)
        except Exception as exc:
            logger.warning("Failed to send to endpoint %s: %s", ep_name, exc)

    console.print("  [green]✓ 通知推送完成[/]")

    if ctx.config.transcribe.delete_after_transcribe and ctx.downloaded_filepath is not None:
        try:
            bvid = ctx.msg.msg_id.replace("bili:", "")
            cleanup_media(filepath=ctx.downloaded_filepath, source_id=bvid)
        except Exception as exc:
            console.print(f"  [yellow]⚠️  媒体清理失败: {exc}[/]")
            logger.warning("Cleanup failed for %s: %s", ctx.msg.msg_id, exc)

    return True
```

**改动 6b**: `platforms/xiaohongshu/handlers.py` — xhs_push（行 117-145）

```python
# 更新 import：
# 删除旧导入: from core.notifier import notify_new_xhs_note（如果存在）
from core.formatter import format_xhs_notification
from core.notifier import find_endpoint, send_gotify

# 替换 xhs_push（行 117-145）：
@PipelineEngine.register("xhs", Phase.PUSHED)
async def xhs_push(ctx: PhaseContext) -> bool:
    """推送小红书笔记通知，按 subscription_id 路由到端点。"""
    # 旧记录跳过（subscription_id="" 表示旧记录已 push 过，不重发；文件清理留给原 pipeline 逻辑）
    if not ctx.msg.subscription_id:
        logger.info("Skipping push for %s: no subscription_id (old record)", ctx.msg.msg_id)
        return True

    parts = ctx.msg.subscription_id.split(":", 1)
    if len(parts) != 2:
        logger.warning("Invalid subscription_id: %s", ctx.msg.subscription_id)
        return True

    _plat, identifier = parts
    matched = None
    for sub in ctx.config.xiaohongshu.subscriptions:
        if sub.user_id == identifier:
            matched = sub
            break

    if matched is None:
        logger.warning("Subscription %s not found in config", ctx.msg.subscription_id)
        return True

    endpoints_to_notify = matched.notify_endpoints
    if not endpoints_to_notify:
        logger.info("No endpoints configured for subscription %s", ctx.msg.subscription_id)
        return True

    note_id = ctx.msg.msg_id.replace("xhs:", "")
    title, message = format_xhs_notification(
        title=ctx.msg.title,
        author=ctx.msg.author,
        note_id=note_id,
        summary=ctx.summary_text,
        keywords=ctx.keywords,
        comment_highlights=ctx.comment_highlights or None,
    )

    console.print(f"  [dim]🔔 推送通知到 {len(endpoints_to_notify)} 个端点...[/]")
    for ep_name in endpoints_to_notify:
        ep = find_endpoint(ctx.config, ep_name)
        if ep is None:
            logger.warning("Endpoint %s not found (referenced by subscription %s)", ep_name, ctx.msg.subscription_id)
            continue
        try:
            await send_gotify(title=title, message=message, endpoint=ep)
        except Exception as exc:
            logger.warning("Failed to send to endpoint %s: %s", ep_name, exc)

    console.print("  [green]✓ 通知推送完成[/]")

    if ctx.config.transcribe.delete_after_transcribe and ctx.downloaded_filepath is not None:
        try:
            cleanup_media(filepath=ctx.downloaded_filepath, source_id=note_id)
        except Exception as exc:
            console.print(f"  [yellow]⚠️  媒体清理失败: {exc}[/]")
            logger.warning("XHS cleanup failed for %s: %s", note_id, exc)

    return True
```

**改动 6c**: `platforms/weibo/handlers.py` — weibo_push（行 154-176）

```python
# 更新 import：
# 删除旧导入: from core.notifier import notify_new_weibo_post（如果存在）
from core.formatter import format_weibo_notification
from core.notifier import find_endpoint, send_gotify

# 替换 weibo_push（行 154-176）：
@PipelineEngine.register("weibo", Phase.PUSHED)
async def weibo_push(ctx: PhaseContext) -> bool:
    """推送微博通知，按 subscription_id 路由到端点。"""
    # 旧记录跳过（subscription_id="" 表示旧记录已 push 过，不重发；文件清理留给原 pipeline 逻辑）
    if not ctx.msg.subscription_id:
        logger.info("Skipping push for %s: no subscription_id (old record)", ctx.msg.msg_id)
        return True

    parts = ctx.msg.subscription_id.split(":", 1)
    if len(parts) != 2:
        logger.warning("Invalid subscription_id: %s", ctx.msg.subscription_id)
        return True

    _plat, identifier = parts
    matched = None
    for sub in ctx.config.weibo.subscriptions:
        if sub.user_id == identifier:
            matched = sub
            break

    if matched is None:
        logger.warning("Subscription %s not found in config", ctx.msg.subscription_id)
        return True

    endpoints_to_notify = matched.notify_endpoints
    if not endpoints_to_notify:
        logger.info("No endpoints configured for subscription %s", ctx.msg.subscription_id)
        return True

    post_id = ctx.msg.msg_id.replace("weibo:", "")
    title, message = format_weibo_notification(
        title=ctx.msg.title,
        author=ctx.msg.author,
        post_id=post_id,
        summary=ctx.summary_text,
        keywords=ctx.keywords,
        comment_highlights=ctx.comment_highlights or None,
    )

    console.print(f"  [dim]🔔 推送通知到 {len(endpoints_to_notify)} 个端点...[/]")
    for ep_name in endpoints_to_notify:
        ep = find_endpoint(ctx.config, ep_name)
        if ep is None:
            logger.warning("Endpoint %s not found (referenced by subscription %s)", ep_name, ctx.msg.subscription_id)
            continue
        try:
            await send_gotify(title=title, message=message, endpoint=ep)
        except Exception as exc:
            logger.warning("Failed to send to endpoint %s: %s", ep_name, exc)

    console.print("  [green]✓ 通知推送完成[/]")
    return True
```

---

### Task 7: 迁移脚本

**新文件**: `scripts/migrate_notifications.py`

```python
#!/usr/bin/env python3
"""迁移旧版 per-platform notification 配置到全局 [[endpoints]] + 订阅 notify_endpoints。

读取 config/config.toml 中的 [bilibili.notification] / [xiaohongshu.notification] / [weibo.notification]，
合并生成唯一的 default endpoint。给所有现有订阅写入 notify_endpoints = ["default"]。

幂等：检测到已有 [[endpoints]] 则跳过。
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

import tomlkit


def migrate(config_path: str = "config/config.toml", subs_path: str = "config/subscriptions.toml") -> bool:
    p = Path(config_path)
    if not p.exists():
        print("❌ 未找到配置文件")
        return False

    raw: dict = {}
    with open(p, "rb") as f:
        raw = tomllib.load(f)

    # 幂等检测：已有 endpoints 则跳过
    if "endpoints" in raw and raw["endpoints"]:
        print("⚠️  已存在 [[endpoints]]，跳过迁移")
        return False

    # 收集旧 notification 配置
    notify_configs: list[dict] = []
    for plat in ("bilibili", "xiaohongshu", "weibo"):
        plat_cfg = raw.get(plat, {})
        noti = plat_cfg.get("notification", {})
        if noti.get("gotify_url") and noti.get("gotify_token"):
            notify_configs.append({
                "url": noti["gotify_url"],
                "token": noti["gotify_token"],
                "priority": noti.get("priority", 5),
            })

    if not notify_configs:
        print("⚠️  未发现旧版 notification 配置，无需迁移")
        return False

    # 用第一个 config 创建 default endpoint
    first = notify_configs[0]
    doc = tomlkit.parse(p.read_text(encoding="utf-8"))
    ep = tomlkit.table()
    ep["name"] = "default"
    ep["url"] = first["url"]
    ep["token"] = first["token"]
    ep["priority"] = first["priority"]
    ep["enabled"] = True
    doc.setdefault("endpoints", tomlkit.aot()).append(ep)

    # 写入 config.toml
    p.write_text(tomlkit.dumps(doc), encoding="utf-8")
    print(f"✅ 已创建 default endpoint ({first['url']})")

    # ── 更新 subscriptions.toml ──────────────────────────────
    sp = Path(subs_path)
    if sp.exists():
        subs_doc = tomlkit.parse(sp.read_text(encoding="utf-8"))
        for plat in ("bilibili", "xiaohongshu", "weibo"):
            entry = subs_doc.get(plat)
            if not entry:
                continue
            subs_list = entry.get("subscriptions", [])
            for item in subs_list:
                if "notify_endpoints" not in item:
                    item["notify_endpoints"] = ["default"]
        sp.write_text(tomlkit.dumps(subs_doc), encoding="utf-8")
        print("✅ 已更新所有订阅的 notify_endpoints = ['default']")

    return True


if __name__ == "__main__":
    success = migrate()
    sys.exit(0 if success else 1)
```

---

### Task 8: Web endpoint CRUD

**新文件**: `web/routes/endpoints.py`

```python
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
    """从配置加载所有端点。"""
    config = await load_config()
    return config.endpoints


def _save_endpoints(endpoints: list[EndpointConfig]) -> None:
    """将端点列表写回 config.toml。"""
    p = Path(CONFIG_PATH)
    raw = tomlkit.parse(p.read_text(encoding="utf-8")) if p.exists() else tomlkit.document()
    aot = tomlkit.aot()
    for ep in endpoints:
        t = tomlkit.table()
        t["name"] = ep.name
        t["url"] = ep.url
        t["token"] = ep.token
        t["priority"] = ep.priority
        t["enabled"] = ep.enabled
        aot.append(t)
    raw["endpoints"] = aot
    p.write_text(tomlkit.dumps(raw), encoding="utf-8")


@router.get("/endpoints", response_class=HTMLResponse)
async def endpoints_page(request: Request) -> HTMLResponse:
    """端点管理页面。"""
    endpoints = await _load_endpoints()

    # 反查订阅（派生视图，阶段 2 会用到，阶段 1 先展示空列表）
    from core.subscription_cli import list_subscriptions
    subs_data = await list_subscriptions()
    # list_subscriptions() 返回的 key 是长名（bilibili/xiaohongshu/weibo），但模板和路由用短名
    LONG_TO_SHORT = {"bilibili": "bili", "xiaohongshu": "xhs", "weibo": "weibo"}
    linked: dict[str, list[dict]] = {}
    for ep in endpoints:
        linked[ep.name] = []
        for section_key, items in subs_data.items():
            platform = LONG_TO_SHORT.get(section_key, section_key)
            for item in items:
                ep_names = item.get("notify_endpoints", [])
                if ep.name in ep_names:
                    linked[ep.name].append({
                        "platform": platform,
                        "identifier": item.get("uid") or item.get("user_id", ""),
                        "name": item.get("name", ""),
                    })

    return TEMPLATES.TemplateResponse(
        request,
        "endpoints.html",
        {"active_nav": "endpoints", "endpoints": endpoints, "linked": linked},
    )


@router.post("/endpoints/add")
async def endpoint_add(
    name: str = Form(...),
    url: str = Form(...),
    token: str = Form(...),
    priority: int = Form(5),
) -> HTMLResponse:
    """新增端点。"""
    endpoints = await _load_endpoints()
    # 检查重名
    if any(ep.name == name for ep in endpoints):
        headers = {"HX-Trigger": '{"toast":{"msg":"端点名称已存在","type":"error"}}'}
        return HTMLResponse(content="", headers=headers, status_code=400)
    endpoints.append(EndpointConfig(name=name, url=url, token=token, priority=priority))
    _save_endpoints(endpoints)
    headers = {"HX-Trigger": '{"toast":{"key":"settings.saved","type":"success"}}'}
    return HTMLResponse(content="", headers=headers)


@router.post("/endpoints/{name}/edit")
async def endpoint_edit(
    name: str,
    url: str = Form(...),
    token: str = Form(...),
    priority: int = Form(5),
    enabled: bool = Form(False),
) -> HTMLResponse:
    """编辑端点。"""
    endpoints = await _load_endpoints()
    for ep in endpoints:
        if ep.name == name:
            ep.url = url
            ep.token = token
            ep.priority = priority
            ep.enabled = enabled
            break
    else:
        headers = {"HX-Trigger": '{"toast":{"msg":"端点不存在","type":"error"}}'}
        return HTMLResponse(content="", headers=headers, status_code=404)
    _save_endpoints(endpoints)
    headers = {"HX-Trigger": '{"toast":{"key":"settings.saved","type":"success"}}'}
    return HTMLResponse(content="", headers=headers)


@router.post("/endpoints/{name}/delete")
async def endpoint_delete(name: str) -> HTMLResponse:
    """删除端点（引用此端点的订阅静默跳过 + warning，不级联删除）。"""
    endpoints = await _load_endpoints()
    endpoints = [ep for ep in endpoints if ep.name != name]
    _save_endpoints(endpoints)
    headers = {"HX-Trigger": '{"toast":{"msg":"端点已删除","type":"success"}}'}
    return HTMLResponse(content="", headers=headers)
```

**注册 router**: `web/app.py`（行 106-116）追加

```python
    # 在行 110 附近添加
    from web.routes.endpoints import router as endpoints_router
    # 在行 116 附近添加
    app.include_router(endpoints_router)
```

**sidebar 加导航**: `web/templates/base.html`（行 65-68，在"设置"之前添加）

```html
      <a href="/endpoints" class="flex items-center gap-3 px-3 py-2 rounded-[8px] text-sm transition-colors {% if active_nav == 'endpoints' %}bg-[var(--color-primary)]/10 text-[var(--color-primary)] font-medium{% else %}text-[var(--text-secondary)] hover:bg-black/5 dark:hover:bg-white/5 hover:text-[var(--text-primary)]{% endif %}">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 6h16M4 12h16M4 18h16"/></svg>
        推送端点
      </a>
```

**新模板**: `web/templates/endpoints.html`

```html
{% extends "base.html" %}
{% from "_macros.html" import field, toggle %}

{% block title %}推送端点 · Trawler{% endblock %}

{% block content %}
<h1 class="text-2xl font-semibold tracking-tight mb-1">推送端点</h1>
<p class="text-sm text-[var(--text-secondary)] mb-6">全局 Gotify 推送端点 · 在订阅编辑中选择每个订阅使用的端点</p>

<!-- 已有端点列表 -->
<div class="space-y-3 mb-8">
{% for ep in endpoints %}
<div class="bg-[var(--card-bg)] backdrop-blur-[12px] rounded-[14px] p-5 shadow-[0_4px_14px_rgba(0,0,0,0.05)] border border-[var(--card-border)]">
  <div class="flex items-center justify-between mb-3">
    <div>
      <span class="font-semibold text-sm">{{ ep.name }}</span>
      <span class="ml-2 text-xs text-[var(--text-secondary)]">优先级 {{ ep.priority }}</span>
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

  <!-- 关联订阅列表（阶段 2 填充） -->
  <div class="mt-2 text-xs text-[var(--text-tertiary)]">
    <span>关联订阅: </span>
    {% if linked[ep.name] %}
      {% for sub in linked[ep.name] %}
        <span class="inline-flex items-center px-2 py-0.5 rounded bg-gray-100 dark:bg-gray-800 mr-1">{{ sub.platform }}/{{ sub.name }}</span>
      {% endfor %}
    {% else %}
      <span>无</span>
    {% endif %}
  </div>

  <!-- 编辑表单（内联折叠） -->
  <form id="edit-{{ ep.name }}" class="mt-3 hidden grid grid-cols-1 md:grid-cols-2 gap-3" hx-post="/endpoints/{{ ep.name }}/edit" hx-target="body">
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

<!-- 添加端点 -->
<div class="bg-[var(--card-bg)] backdrop-blur-[12px] rounded-[14px] p-5 shadow-[0_4px_14px_rgba(0,0,0,0.05)] border border-[var(--card-border)]">
  <h2 class="text-base font-semibold mb-3">添加端点</h2>
  <form hx-post="/endpoints/add" hx-target="body" class="grid grid-cols-1 md:grid-cols-2 gap-3">
    {{ field("name", "", "名称", placeholder="default") }}
    {{ field("url", "", "Gotify URL", placeholder="https://gotify.example.com", width="full") }}
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

---

### Task 9: 订阅编辑表单加 endpoint multi-select

**文件**: `web/routes/subscriptions.py` + `web/templates/subscriptions.html`

**改动 9a**: `web/routes/subscriptions.py` 新增 API — 订阅端点的添加/移除（行 58 之后追加）

```python
@router.post("/subscriptions/{platform}/{identifier}/endpoints")
async def subscription_endpoints_toggle(
    platform: str,
    identifier: str,
    action: str = Form(...),
    endpoint: str = Form(...),
) -> HTMLResponse:
    """添加或移除订阅的端点引用。

    POST /subscriptions/{platform}/{identifier}/endpoints
    Form: action=add|remove, endpoint=<name>
    """
    # Validate
    platform_map = {"bili": "bilibili", "xhs": "xiaohongshu", "weibo": "weibo"}
    section = platform_map.get(platform)
    if section is None:
        headers = {"HX-Trigger": '{"toast":{"msg":"无效平台","type":"error"}}'}
        return HTMLResponse(content="", headers=headers, status_code=400)

    sub_path = "config/subscriptions.toml"
    p = Path(sub_path)
    if not p.exists():
        headers = {"HX-Trigger": '{"toast":{"msg":"订阅文件不存在","type":"error"}}'}
        return HTMLResponse(content="", headers=headers, status_code=400)

    raw = p.read_text(encoding="utf-8")
    doc = tomlkit.parse(raw)
    entry = doc.get(section)
    if entry is None:
        headers = {"HX-Trigger": '{"toast":{"msg":"该平台无订阅","type":"error"}}'}
        return HTMLResponse(content="", headers=headers, status_code=404)

    # 查找订阅
    key_field = "uid" if platform == "bili" else "user_id"
    subs_list = entry.get("subscriptions", [])
    found = None
    for item in subs_list:
        if str(item.get(key_field, "")) == identifier:
            found = item
            break

    if found is None:
        headers = {"HX-Trigger": '{"toast":{"msg":"订阅未找到","type":"error"}}'}
        return HTMLResponse(content="", headers=headers, status_code=404)

    # 操作 endpoints 列表
    eps = found.get("notify_endpoints", [])
    # tomlkit arrays are not regular lists; convert to list
    if not isinstance(eps, list):
        eps = []
    eps_list = [str(e) for e in eps]

    if action == "add":
        if endpoint not in eps_list:
            eps_list.append(endpoint)
    elif action == "remove":
        eps_list = [e for e in eps_list if e != endpoint]
    else:
        headers = {"HX-Trigger": '{"toast":{"msg":"无效操作","type":"error"}}'}
        return HTMLResponse(content="", headers=headers, status_code=400)

    # tomlkit 写回 — ⚠️ 必须用 tomlkit.array() 显式构造，避免普通 list 导致 dumps 格式跳变（inline vs 多行）
    import tomlkit
    found["notify_endpoints"] = tomlkit.array(eps_list).multiline(True)
    p.write_text(tomlkit.dumps(doc), encoding="utf-8")

    # 触发两侧刷新
    headers = {
        "HX-Trigger": '{"toast":{"key":"settings.saved","type":"success"}}'
    }
    return HTMLResponse(content="", headers=headers)
```

**改动 9b**: `web/templates/subscriptions.html` — 订阅项加 endpoint checkboxes

在每个订阅项的 `</form>`（删除按钮）之后，追加 inline endpoint selector：

```html
      <!-- 端点选择器 -->
      <div class="mt-1 flex flex-wrap gap-1">
        {% set ns = namespace(endpoints=[]) %}
        {% if item.get("notify_endpoints") %}
          {% set ns.endpoints = item["notify_endpoints"] %}
        {% endif %}
        {% for ep_name in ns.endpoints %}
        <span class="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs bg-blue-50 text-blue-700 dark:bg-blue-900/30 dark:text-blue-300">
          {{ ep_name }}
          <button hx-post="/subscriptions/{{ p.key }}/{{ item.get('uid') or item.get('user_id', '') }}/endpoints"
                  hx-vals='{"action": "remove", "endpoint": "{{ ep_name }}"}'
                  hx-target="body"
                  class="text-blue-400 hover:text-red-500 ml-0.5">&times;</button>
        </span>
        {% endfor %}
      </div>
```

Append 一个「+ 添加端点」下拉菜单在端点列表末尾（每个平台 card 内的底部）：

```html
      <!-- 添加端点（下拉选择） -->
      <div class="mt-2 flex items-center gap-1">
        <select class="text-xs px-2 py-1 rounded border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800"
                onchange="addEndpoint(this, '{{ p.key }}', '{{ item.get('uid') or item.get('user_id', '') }}')">
          <option value="">+ 添加端点</option>
          {% for ep_name in available_endpoints %}
          <option value="{{ ep_name }}">{{ ep_name }}</option>
          {% endfor %}
        </select>
      </div>
```

需要把 `available_endpoints` 传递给模板。修改 `subscriptions_page`（行 13-32）以加载端点列表并传入模板：

```python
# 在 subscriptions_page 中，现有代码后追加
    from shared.config import load_config
    cfg = await load_config()
    available_endpoints = [ep.name for ep in cfg.endpoints]
    # 在模板上下文中加入
    return TEMPLATES.TemplateResponse(
        request,
        "subscriptions.html",
        {
            "active_nav": "subscriptions",
            "platforms": platforms_data,
            "flash_msg": flash_msg,
            "flash_type": flash_type,
            "available_endpoints": available_endpoints,  # 新增
        },
    )
```

在 subscriptions.html 底部或全局 script 中添加 JS：

```javascript
function addEndpoint(sel, platform, identifier) {
  var ep = sel.value;
  if (!ep) return;
  sel.value = '';
  var form = document.createElement('form');
  form.method = 'POST';
  form.action = '/subscriptions/' + platform + '/' + identifier + '/endpoints';
  var inp1 = document.createElement('input'); inp1.name = 'action'; inp1.value = 'add';
  var inp2 = document.createElement('input'); inp2.name = 'endpoint'; inp2.value = ep;
  form.appendChild(inp1); form.appendChild(inp2);
  htmx.ajax('POST', form.action, {values: {action: 'add', endpoint: ep}, target: 'body'});
}
```

---

### Task 10: settings 清理

**文件**: `web/routes/settings.py` + `web/templates/settings.html`

**改动 10a**: `web/routes/settings.py` — 删除 gotify 表单字段（行 32-35）、删除 notification 写盘逻辑（行 50-60）

```python
# Before（行 32-35）：
    gotify_url: str = Form(default=""),
    gotify_token_bili: str = Form(default=""),
    gotify_token_xhs: str = Form(default=""),
    gotify_token_weibo: str = Form(default=""),

# After：删除这 4 行
```

```python
# Before（行 50-60）：
    # Update notifications — always write so fields can be cleared via the UI
    tokens = {
        "bilibili": gotify_token_bili,
        "xiaohongshu": gotify_token_xhs,
        "weibo": gotify_token_weibo,
    }
    for plat in ("bilibili", "xiaohongshu", "weibo"):
        raw.setdefault(plat, tomlkit.table()).setdefault(
            "notification", tomlkit.table()
        )["gotify_url"] = gotify_url
        raw[plat]["notification"]["gotify_token"] = tokens[plat]

# After：删除上述整个 block
```

**改动 10b**: `web/templates/settings.html` — 删除 Gotify 通知 card（行 30-44 整块），加链接到 `/endpoints`

删除 `<!-- Card 2: 通知 -->` 整块（行 29-44）。在 Card 1（常规）之后添加提示：

```html
  <!-- 推送端点：跳转到 endpoints 管理 -->
  <div class="bg-[var(--card-bg)] backdrop-blur-[12px] rounded-[14px] p-5 shadow-[0_4px_14px_rgba(0,0,0,0.05)] border border-[var(--card-border)] mb-4">
    <div class="flex items-center justify-between">
      <div>
        <h2 class="text-base font-semibold">Gotify 推送</h2>
        <p class="text-xs text-[var(--text-secondary)] mt-1">通知端点管理已移至独立页面</p>
      </div>
      <a href="/endpoints" class="px-4 py-2 bg-apple-blue text-white rounded-[8px] text-sm font-medium hover:bg-blue-600 transition-colors">管理端点</a>
    </div>
  </div>
```

---

### Task 11: 配置示例更新

**文件**: `config/config.toml.example` + `config/subscriptions.toml`

**改动 11a**: `config/config.toml.example` — 追加 `[[endpoints]]` 段，删除三个 `[*.notification]` 段

删除（行 82-91）：
```toml
[bilibili.notification]
enabled = true
gotify_url = ""
gotify_token = ""
priority = 5
```

删除（行 109-113）：
```toml
[xiaohongshu.notification]
enabled = true
gotify_url = ""
gotify_token = ""
priority = 5
```

删除（行 132-136）：
```toml
[weibo.notification]
enabled = true
gotify_url = ""
gotify_token = ""
priority = 5
```

在文件末尾追加：
```toml
# ── Gotify 推送端点 ─────────────────────────────────────────────
# 端点全局定义，订阅中通过 notify_endpoints = ["name"] 引用
[[endpoints]]
name = "default"
url = ""
token = ""
priority = 5
enabled = true
```

**改动 11b**: `config/subscriptions.toml` — 每个订阅加 `notify_endpoints`

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

---

### Task 12: README 更新

**文件**: `README.md`

**改动 12a**: 删除环境变量表格中的 `FEEDFLOW_GOTIFY_*` 行（行 187-192）

删除以下 4 行：
```
| `FEEDFLOW_GOTIFY_URL` | Gotify server URL |
| `FEEDFLOW_GOTIFY_TOKEN_BILI` | Bilibili Gotify token |
| `FEEDFLOW_GOTIFY_TOKEN_XHS` | Xiaohongshu Gotify token |
| `FEEDFLOW_GOTIFY_TOKEN_WEIBO` | Weibo Gotify token |
```

**改动 12b**: 更新配置模型树（行 181-183），将三个平台中的 `notification` 替换为全局 `endpoints`：

```
# Before:
├── bilibili (auth, monitor, subscriptions, notification)
├── xiaohongshu (enabled, auth, monitor, subscriptions, notification)
└── weibo (enabled, auth, monitor, subscriptions, notification)

# After:
├── globals (endpoints)
├── bilibili (auth, monitor, subscriptions)
├── xiaohongshu (enabled, auth, monitor, subscriptions)
└── weibo (enabled, auth, monitor, subscriptions)
```

**改动 12c**: 在 Quick Start → Configuration 章节（行 48-74）追加 `[[endpoints]]` 配置示例：

```toml
[[endpoints]]
name = "default"
url = ""
token = ""
priority = 5
enabled = true
```

同时在 Architecture → Pipeline 章节（行 116-124）后的 "Push notifications" 特性说明中更新为 "Endpoint-based multi-URL notifications"。

---

### 阶段 1 验收清单

- [ ] `uv run ruff check .` 不新增问题
- [ ] `uv run pyright .` 不新增 error
- [ ] `uv run pytest -x` 全绿
- [ ] 手动：删除旧 `[*.notification]`，跑 `python scripts/migrate_notifications.py` → 生成 default endpoint
- [ ] 手动：改一个订阅的 `notify_endpoints = ["default", "test"]`
- [ ] 手动：`uv run trawler check --platform all` → 两个 endpoint 都收到通知
- [ ] Web UI: `/endpoints` 可新增/编辑/删除端点
- [ ] Web UI: 订阅页面可勾选端点
- [ ] Web UI: `/settings` 不再显示 Gotify 字段
- [ ] 旧记录 `subscription_id=""` → 日志 info 跳过，不 crash

---

## 阶段 2: 双向关联 UI

### Task 13: endpoint 卡片派生视图 API

**文件**: `web/routes/endpoints.py` — 在 endpoint_page 中已实现了反查（`linked: dict[str, list[dict]]`）。此 Task 确保 API 路由暴露。

新增独立 API 路由（可选，HTMX 用）：

```python
@router.get("/endpoints/{name}/linked-subscriptions")
async def endpoint_linked_subscriptions(name: str) -> HTMLResponse:
    """派生视图：返回端点的关联订阅列表（HTML 片段）。"""
    from core.subscription_cli import list_subscriptions

    # LONG_TO_SHORT 映射（同 Task 8）
    LONG_TO_SHORT = {"bilibili": "bili", "xiaohongshu": "xhs", "weibo": "weibo"}

    subs_data = await list_subscriptions()
    linked: list[dict] = []
    for section_key, items in subs_data.items():
        platform = LONG_TO_SHORT.get(section_key, section_key)
        for item in items:
            ep_names = item.get("notify_endpoints", [])
            if name in ep_names:
                linked.append({
                    "platform": platform,
                    "identifier": item.get("uid") or item.get("user_id", ""),
                    "name": item.get("name", ""),
                })

    return TEMPLATES.TemplateResponse(
        request := None,  # 需要 Request 参数，简化处理：实际实现需传 request
        "_linked_subs.html",
        {"linked": linked, "endpoint_name": name},
    )
```

**注意**: 实际 HTMX fragment 需要 `Request`。改为接受 `Request` 参数或使用 `APIRouter` 的 `request: Request` 注入。简化实现：在 `endpoints_page` 模板中直接渲染 `linked`（已在 Task 8 模板中包含）。

---

### Task 14: endpoint 卡片模板

利用 Task 8 中 `endpoints.html` 模板已有的「关联订阅」区域。当前已显示关联订阅列表并附带「添加/移除」能力。

在订阅列表中每项加「+添加」按钮和「移除」按钮：

```html
<!-- 在 endpoints.html 的关联订阅区域添加操作按钮 -->
<div class="mt-2 text-xs">
  <span class="text-[var(--text-tertiary)]">关联订阅: </span>
  {% if linked[ep.name] %}
    {% for sub in linked[ep.name] %}
      <span class="inline-flex items-center gap-1 px-2 py-0.5 rounded bg-gray-100 dark:bg-gray-800 mr-1">
        {{ sub.platform }}/{{ sub.name }}
        <button hx-post="/subscriptions/{{ sub.platform }}/{{ sub.identifier }}/endpoints"
                hx-vals='{"action": "remove", "endpoint": "{{ ep.name }}"}'
                hx-target="body"
                class="text-gray-400 hover:text-red-500 ml-0.5">&times;</button>
      </span>
    {% endfor %}
  {% else %}
    <span class="text-[var(--text-tertiary)]">无</span>
  {% endif %}
  <!-- 添加订阅下拉（由 endpoints_page 传入所有订阅列表） -->
  <select class="text-xs ml-2 px-1 py-0.5 rounded border border-gray-200"
          onchange="linkSub(this, '{{ ep.name }}')">
    <option value="">+ 添加订阅</option>
    {% for sub in all_subs %}
    <option value="{{ sub.platform }}|{{ sub.identifier }}">{{ sub.platform }}/{{ sub.name }}</option>
    {% endfor %}
  </select>
</div>
```

需要 `endpoints_page` 上下文增加 `all_subs`：

```python
# 在 endpoints_page 中追加
    # LONG_TO_SHORT 映射（同 Task 8）
    LONG_TO_SHORT = {"bilibili": "bili", "xiaohongshu": "xhs", "weibo": "weibo"}
    all_subs: list[dict] = []
    for section_key, items in subs_data.items():
        platform = LONG_TO_SHORT.get(section_key, section_key)
        for item in items:
            all_subs.append({
                "platform": platform,
                "identifier": str(item.get("uid") or item.get("user_id", "")),
                "name": item.get("name", ""),
            })
    # 传入模板
    ...
    return TEMPLATES.TemplateResponse(
        ...,
        {"active_nav": "endpoints", "endpoints": endpoints, "linked": linked, "all_subs": all_subs},
    )
```

JS：

```javascript
function linkSub(sel, epName) {
  var val = sel.value;
  if (!val) return;
  sel.value = '';
  var parts = val.split('|');
  var platform = parts[0], identifier = parts[1];
  htmx.ajax('POST', '/subscriptions/' + platform + '/' + identifier + '/endpoints', {
    values: {action: 'add', endpoint: epName},
    target: 'body'
  });
}
```

---

### Task 15: 订阅 API 支持 endpoint 增删

已在阶段 1 **Task 9a** 中实现 (`POST /subscriptions/{platform}/{identifier}/endpoints`)。阶段 2 在返回的 `HX-Trigger` 中增加 `refreshEndpoints` 事件，让 endpoint 页面侦测到后自动刷新：

```python
# 在 subscription_endpoints_toggle 中（行已存在），更新 headers：
    headers = {
        "HX-Trigger": '{"toast":{"key":"settings.saved","type":"success"},"refreshEndpoints":""}'
    }
```

在 `endpoints.html` 或 `base.html` 中添加全局侦听：

```javascript
document.body.addEventListener('refreshEndpoints', function() {
  // 如果当前在 endpoints 页面，重新加载
  if (window.location.pathname === '/endpoints') {
    htmx.ajax('GET', '/endpoints', {target: '#main-content', swap: 'innerHTML'});
  }
});
```

---

### Task 16: 双向同步

**策略**: 接受 staleness（多 tab 不同步），不引入 WebSocket。每次 mutation 后通过 `HX-Trigger` 触发两侧刷新：

- 订阅页面修改 endpoint 选择 → `refreshEndpoints` → endpoint 页面局部刷新
- Endpoint 页面添加/移除订阅 → `refreshSubscriptions` → 订阅页面局部刷新（或全页 reload）

在 `subscription_endpoints_toggle` 中同时触发两个事件：

```python
    headers = {
        "HX-Trigger": '{"toast":{"key":"settings.saved","type":"success"},"refreshEndpoints":"","refreshSubscriptions":""}'
    }
```

在 `base.html` 中全局侦听：

```javascript
document.body.addEventListener('refreshSubscriptions', function() {
  if (window.location.pathname === '/subscriptions') {
    htmx.ajax('GET', '/subscriptions', {target: '#main-content', swap: 'innerHTML'});
  }
});
```

---

### 阶段 2 验收清单

- [ ] endpoint 卡片显示关联订阅列表（派生视图）
- [ ] endpoint 卡片下拉"添加订阅 X" → 订阅 X 的 `notify_endpoints` 立即包含 `X`
- [ ] endpoint 卡片"移除"按钮 → 订阅 X 的 `notify_endpoints` 立即移除
- [ ] 订阅页面修改端点选择 → endpoint 页面关联订阅列表立刻反映（HTMX trigger）
- [ ] endpoint 页面修改 → 订阅页面立刻反映
- [ ] 旧 endpoint name 被订阅引用后改名 → push 时静默跳过 + warning
- [ ] 所有 mutation 走订阅侧 API（endpoint 页面不直接写 subscription）

---

## 测试清单

### Task 1 测试
- `test_endpoint_config_defaults`: `EndpointConfig(name="t", url="u", token="tk")` 验证 default
- `test_bili_subscription_notify_endpoints`: `BiliSubscription(uid=1, name="x", notify_endpoints=["a"])` 验证字段

### Task 3 测试
- `test_add_new_with_subscription_id`: 调 `store.add_new(..., subscription_id="bili:123")` → `msg.subscription_id == "bili:123"`
- `test_add_new_legacy`: 不传 `subscription_id` → `msg.subscription_id == ""`
- `test_msg_from_dict_legacy`: 旧 JSON 无 `subscription_id` → 反序列化后 `subscription_id == ""`

### Task 4 测试（集成）
- `test_bili_detector_subscription_id`: mock `fetch_user_videos`，验证 `store.add_new` 收到 `subscription_id=f"bili:{sub.uid}"`
- 同理 xhs / weibo

### Task 5 测试
- `test_send_gotify_with_endpoint`: 调 `send_gotify(..., endpoint=EndpointConfig(...))` → 成功
- `test_format_video_notification_returns_tuple`: 验证返回 `(str, str)`

### Task 6 测试（集成）
- `test_bili_push_subscription_id_empty`: `msg.subscription_id=""` → info 日志，返回 True
- `test_bili_push_endpoint_not_found`: 订阅引用不存在的 endpoint → warning，不 crash
- `test_bili_push_multiple_endpoints`: 两个 endpoint 都收到通知
- `test_bili_push_one_fails`: 一个 endpoint 失败，另一个仍成功

### Task 7 测试
- `test_migrate_creates_default_endpoint`: 迁移后 config.toml 有 `[[endpoints]]`
- `test_migrate_idempotent`: 已有 endpoints 时跳过
- `test_migrate_updates_subscriptions`: 订阅都获得 `notify_endpoints = ["default"]`
- `test_migrate_no_notification_config`: 无旧配置时静默跳过

### Task 8-9 测试（Web）
- `test_endpoints_page_returns_200`
- `test_endpoint_add_duplicate_name_returns_400`
- `test_subscription_endpoint_toggle_add_then_remove`

---

## 风险清单

1. **配置迁移** — 迁移脚本 `scripts/migrate_notifications.py` 幂等运行。`load_config` 检测到旧字段时 warn 但不自动迁移。
2. **环境变量清理** — 删除 `FEEDFLOW_GOTIFY_URL`/`FEEDFLOW_GOTIFY_TOKEN_*` 等旧 env override（详见 Task 12 README 更新）。
3. **迁移脚本仅处理 config.toml** — 迁移脚本只合并 config.toml 中的 `[*.notification]` 到 `[[endpoints]]`。不处理 cookies.toml 或手写 subscriptions.toml 中的 notification 字段。手写 notification 字段在 `load_config` 中会 warn 但不 crash。
4. **Endpoint rename 不级联** — Endpoint 改名后订阅引用的旧名静默跳过 + warning。
5. **MessageRecord 旧记录 subscription_id=""** — Push handler 必须容忍，跳过 + info 日志，不算 error。
6. **tomlkit 数组操作** — 注意 tomlkit 的数组类型（`AoT` / 普通 `list`），在 `subscription_endpoints_toggle` 中用 `tomlkit.array()` 显式构造（见 Issue 6 修复）。
7. **subscriptions.toml 手动编辑** — 用户可能手动编辑 TOML。`notify_endpoints` 缺失时当作空列表，不 crash。

---

## 实施顺序

### 阶段 1（有严格依赖顺序）
1. Task 1 — 数据模型（必须先于一切）
2. Task 2 — 配置解析（依赖 Task 1）
3. Task 3 — MessageStore（依赖 Task 1 的 MessageRecord）
4. Task 4 — Detectors（依赖 Task 3）
5. Task 5 — Notifier + Formatter（独立于 3-4，可并行）
6. Task 6 — Push handlers（依赖 Task 4 + Task 5）
7. Task 7 — 迁移脚本（依赖 Task 1-2）
8. Task 8 — Endpoint CRUD（依赖 Task 1-2）
9. Task 9 — 订阅表单（依赖 Task 8）
10. Task 10 — Settings 清理（依赖 Task 8-9）
11. Task 11 — 配置示例
12. Task 12 — README 更新（独立，可随时执行）

### 阶段 2（增量，不破坏阶段 1）
13. Task 13 — 派生视图 API（原 Task 12）
14. Task 14 — Endpoint 卡片模板（原 Task 13）
15. Task 15 — 订阅 API 增强（原 Task 14）
16. Task 16 — 双向同步（原 Task 15）

每个阶段完成后均为可发布状态。阶段 1 完成后用户可通过 TOML 手动配置 endpoints 并正常运行 pipeline。阶段 2 增加 UI 双向联动体验。

---

## Revision History

- **2026-06-16 第 2 轮 oracle review 修订** — 修复 8 个问题：
  - 问题 1: 扩 Task 1 测试影响为独立一节，列全 13+ 处删改位置
  - 问题 2/3: Task 8/13 派生视图加 `LONG_TO_SHORT`，传短名给模板
  - 问题 4: Task 6a/6b/6c 加删除旧 import 说明；旧记录分支注释补充文件清理策略
  - 问题 5: Task 5c 加 `**发布时间:**` 删除说明
  - 问题 6: Task 9a 写回用 `tomlkit.array(eps_list).multiline(True)`
  - 问题 7: Task 8 `enabled: bool = Form(False)` 修复 checkbox bug
  - 问题 8: 新增 Task 12 README 更新；阶段 2 重编号 13-16；风险清单加迁移范围说明

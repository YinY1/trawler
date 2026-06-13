# 消息状态管理与统一流水线引擎 — 设计文档

> 日期: 2026-06-13
> 状态: 草稿

## 目标

1. **时间窗口**：只下载 24 小时内发布的内容，超期消息自动清理
2. **阶段状态**：每条消息跟踪 `discovered → downloaded → transcribed(视频) → summarized → pushed`，支持断点续传
3. **统一引擎**：`core/engine.py` 提供通用的流水线引擎，平台差异通过装饰器注册的 handler 隔离

## 架构变更

```
shared/
  protocols.py         # + ContentType(enum), Phase(enum), PhaseContext(dataclass), MessageRecord(dataclass)
  message_store.py     # [NEW] MessageStore — 统一消息状态存储（JSON）, 取代各平台 JsonSetStore

core/
  engine.py            # [NEW] PipelineEngine — 流水线引擎 + 注册表 + 跨平台 handler
  pipeline.py          # [SIMPLIFY] 各 platform 的 run_*_check_once 委派给 engine

platforms/
  bilibili/
    handlers.py        # [NEW] B站各阶段的 handler 注册
    monitor.py         # [SIMPLIFY] 去掉 store 参数，改为纯检测函数 fetch_user_videos()
    ...
  xiaohongshu/
    handlers.py        # [NEW]
    monitor.py         # [SIMPLIFY]
    ...
  weibo/
    handlers.py        # [NEW]
    monitor.py         # [SIMPLIFY]
    ...

run_check.py           # + --from-phase CLI 参数
```

## 数据模型

### ContentType & Phase

```python
class ContentType(Enum):
    VIDEO = auto()       # B站视频 / XHS视频笔记 — 完整四阶段
    TEXT = auto()        # 微博 / XHS图文笔记 — 两阶段（下载+推送）
    DYNAMIC = auto()     # 预留：B站动态，本次不实现

> **行为变更**: TEXT 类型不再生成 AI 摘要和关键词。用户要求文字类仅两阶段（下载+推送），当前 weibo 和 XHS 图文的摘要逻辑将被移除。如果后续需要摘要，可重新加入作为可选阶段。

class Phase(Enum):
    DISCOVERED = auto()      # 刚被发现，尚未下载
    DOWNLOADED = auto()      # 媒体已下载
    TRANSCRIBED = auto()     # 视频已转写（仅 VIDEO 类型）
    SUMMARIZED = auto()      # 已生成摘要+关键词
    PUSHED = auto()          # 已推送通知

# 各类型消息的阶段流转路径
PHASE_FLOW: dict[ContentType, list[Phase]] = {
    ContentType.VIDEO: [DISCOVERED, DOWNLOADED, TRANSCRIBED, SUMMARIZED, PUSHED],
    ContentType.TEXT:  [DISCOVERED, DOWNLOADED, PUSHED],
}
```

### MessageRecord

```python
@dataclass
class MessageRecord:
    msg_id: str             # "{platform}:{id}" e.g. "bili:BV1xx", "xhs:note_id", "weibo:post_id"
    platform: str           # "bili" | "xhs" | "weibo"
    content_type: ContentType
    phase: Phase
    pubdate: int            # Unix 时间戳（内容发布时间）
    title: str
    author: str
    created_at: float = 0.0
    updated_at: float = 0.0
    error: str = ""
```

### PhaseContext（流水线上下文，各阶段产出积累）

```python
@dataclass
class PhaseContext:
    msg: MessageRecord
    config: Config
    downloaded_filepath: Path | None = None
    image_paths: list[Path] = field(default_factory=list)
    content_text: str = ""
    transcript_text: str = ""
    summary_text: str = ""
    keywords: list[str] = field(default_factory=list)
    comment_highlights: str = ""
    error: str = ""           # 处理器返回 False 时在此记录错误信息
```

## 存储

`data/messages.json` — 统一 JSON 文件，取代各平台的 `known_bili_videos.json` / `known_xhs_notes.json` / `known_weibo_posts.json`

```json
{
  "version": 2,
  "messages": {
    "bili:BV1xx": {
      "platform": "bili",
      "content_type": "video",
      "phase": "downloaded",
      "pubdate": 1718000000,
      "title": "...",
      "author": "...",
      "created_at": 1718100000.0,
      "updated_at": 1718100500.0,
      "error": ""
    }
  }
}
```

### MessageStore 接口

```python
class MessageStore:
    def __init__(self, data_dir: str | Path): ...

    def is_in_window(self, pubdate: int) -> bool: ...
    def is_known(self, msg_id: str) -> bool: ...
    def get_message(self, msg_id: str) -> MessageRecord | None: ...

    def add_new(self, msg_id, platform, content_type, pubdate, title, author) -> MessageRecord | None: ...

    def mark_phase(self, msg_id: str, phase: Phase) -> None: ...
    def mark_error(self, msg_id: str, error: str) -> None: ...

    def get_messages(self, *, phase: Phase | None = None, exclude: bool = False) -> list[MessageRecord]: ...

    def cleanup(self, window_hours: int = 24) -> None: ...
    def save(self) -> None: ...
```

## 流水线引擎

`core/engine.py` — 通用引擎，不感知平台细节。

### PipelineEngine.process_message

```python
@classmethod
async def process_message(cls, msg: MessageRecord, config: Config, store: MessageStore) -> None:
    """从当前 phase 开始逐阶段推进。每推进一个阶段立即 save()。"""
    ctx = PhaseContext(msg=msg, config=config)
    phases = PHASE_FLOW[msg.content_type]

    start_idx = phases.index(msg.phase)
    for next_phase in phases[start_idx + 1:]:
        handler = cls._handlers.get((msg.platform, next_phase))
        if handler is None:
            logger.error("No handler for %s / %s", msg.platform, next_phase)
            break
        success = await handler(ctx)
        if not success:
            store.mark_error(msg.msg_id, ctx.error)
            store.save()   # 记录错误后保存
            break
        msg.phase = next_phase
        store.save()       # 每推进一个阶段保存一次，避免中途崩溃丢失进度
```

> 每阶段 save 是故意的。JSON 全量重写在 24h 窗口内数据量很小（百条级），IO 成本可忽略。如果未来数据量增长，可以改为定期 save（每 N 条一次）。

```python
PhaseHandler = Callable[[PhaseContext], Awaitable[bool]]

class PipelineEngine:
    _handlers: dict[tuple[str, Phase], PhaseHandler] = {}
    _detectors: dict[str, Callable] = {}

    @classmethod
    def register(cls, platform: str, phase: Phase) -> Callable[[PhaseHandler], PhaseHandler]: ...
    @classmethod
    def register_detector(cls, platform: str) -> Callable: ...

    @classmethod
    async def process_message(cls, msg: MessageRecord, config: Config, store: MessageStore) -> None: ...
    @classmethod
    async def run_platform(cls, config: Config, platform: str) -> None:
        """统一入口：cleanup → detect → process"""
        store = MessageStore(config.general.data_dir)
        store.cleanup(24)
        detector = cls._detectors.get(platform)
        if detector:
            await detector(config, store)
        for msg in store.get_messages(phase=PUSHED, exclude=True):
            await cls.process_message(msg, config, store)
        store.save()
```

### Detector 注册

每个平台注册一个 detector 函数，负责从 API/RSS 获取原始数据并注册新消息到 store：

```python
# platforms/bilibili/handlers.py
@PipelineEngine.register_detector("bili")
async def bili_detector(config: Config, store: MessageStore) -> None:
    for sub in config.bilibili.subscriptions:
        videos = await fetch_user_videos(uid=sub.uid, config=config)
        for v in videos:
            store.add_new(
                msg_id=f"bili:{v.bvid}",
                platform="bili",
                content_type=ContentType.VIDEO,
                pubdate=v.pubdate,
                title=v.title,
                author=v.author,
            )
```

`store.add_new()` 内部做时间窗口过滤 (`is_in_window`) 和去重 (`is_known`)，返回 `None` 时跳过。

### run_platform 流程

```
1. MessageStore.cleanup(24h)       → 清理超期消息
2. detector(config, store)         → 注册新消息（时间窗口 + 去重由 store 内部处理）
3. for msg in store.get_messages(phase=PUSHED, exclude=True):
     process_message(msg, config)  → 逐阶段推进，每推进一个阶段就 save()
4. store.save()                    → 最终保底保存
```

### 平台 handler 注册示例

```python
# platforms/bilibili/handlers.py
@PipelineEngine.register("bili", Phase.DOWNLOADED)
async def bili_download(ctx: PhaseContext) -> bool: ...

@PipelineEngine.register("bili", Phase.SUMMARIZED)
@PipelineEngine.register("xhs", Phase.SUMMARIZED)
@PipelineEngine.register("weibo", Phase.SUMMARIZED)
async def summarize_phase(ctx: PhaseContext) -> bool: ...
```

### 跨平台共用 handler

- `TRANSCRIBED`：所有视频类型共用（bili + xhs）
- `SUMMARIZED`：所有类型共用
- `PUSHED`：各平台独立（通知格式不同）

## 各平台改动

### monitor.py — 纯检测函数

每个平台的 `check_new_*` 改为纯检测函数，不再接收 `store` 参数：

```python
# 改造前
async def check_new_videos(uid, config, store) -> list[VideoInfo]:
    raw = await _fetch_user_videos(...)
    return [parse(v) for v in raw if not store.is_known(v.bvid)]

# 改造后
async def fetch_user_videos(uid, config) -> list[VideoInfo]:
    raw = await _fetch_user_videos(...)
    return [parse(v) for v in raw]
```

时间窗口过滤和去重统一在 engine 的 detector 阶段完成。

### pipeline.py — 大幅简化

每个 `run_*_check_once` 简化为：

```python
async def run_bili_check_once(config: Config) -> None:
    await PipelineEngine.run_platform(config, "bili")
```

## CLI

```python
@click.option("--from-phase", default=None,
              type=click.Choice(["discovered", "downloaded", "transcribed", "summarized"]),
              help="从指定阶段开始处理，不指定则自动断点续传")
```

- 指定 `--from-phase` 时：将 store 中所有该阶段及之后的消息回退到指定阶段，重新处理
- 不指定时：自动从每条消息的当前阶段继续

## 错误处理

- 阶段执行失败：`msg.error` 记录错误信息，`msg.phase` 不变
- 每次 `run_check_once` 都重试所有失败消息
- 不做指数退避，保持简单

## B站动态（spec 预留）

已预留 `ContentType.DYNAMIC`，但不包含在 `PHASE_FLOW` 中。后续实现步骤：

1. 定义 `PHASE_FLOW[ContentType.DYNAMIC]` 的阶段路径
2. 实现 B站动态的 detector（从现有 `check_new_dynamics` 改造）
3. 注册各阶段 handler
4. 核心引擎不需改动

## 向后兼容

- 旧版 `known_*.json` 文件不再使用，可手动清理
- `MessageStore` 首次运行时创建新格式文件，不读取旧文件
- 旧版 `JsonSetStore` 子类保留但标记为 deprecated

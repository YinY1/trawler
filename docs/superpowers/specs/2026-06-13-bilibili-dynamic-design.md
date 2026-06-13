# B站动态接入统一流水线 — 设计文档

> 日期: 2026-06-13
> 状态: 草稿
> 依赖: 消息状态管理与统一流水线引擎设计（`message-state-pipeline-design.md`）必须先实现

## 目标

将 B站动态（UP 主非视频动态：转发、图文、纯文本等）接入新构建的统一流水线引擎 PipelineEngine，复用其阶段注册和推进机制。

## 架构变更

```
shared/
  protocols.py         # ContentType.DYNAMIC 在已有枚举中实际不存在 → 在此 spec 中定义

platforms/bilibili/
  handlers.py          # + DYNAMIC 相关 handler 注册
  dynamic.py           # 现有 check_new_dynamics → 改为纯检测函数 fetch_new_dynamics()

core/
  engine.py            # 无改动 — 引擎通用，不感知新增 ContentType
  pipeline.py          # run_bili_check_once 已委派给 engine，自动包含 dynamic
```

## ContentType

```python
class ContentType(Enum):
    VIDEO  = auto()     # 已在主 spec 定义
    TEXT   = auto()     # 已在主 spec 定义
    DYNAMIC = auto()    # B站动态
```

## 阶段流转

```
DYNAMIC: DISCOVERED → SUMMARIZED → PUSHED
```

B站动态特点：
- **无下载阶段**（动态本身是文字/图片，无需下载视频）
- **无转写阶段**（无视频）
- **有摘要阶段**（生成简要总结）
- **有推送阶段**（通过 Gotify 通知）

```python
PHASE_FLOW[ContentType.DYNAMIC] = [
    Phase.DISCOVERED,
    Phase.SUMMARIZED,
    Phase.PUSHED,
]
```

## Detector

```python
@PipelineEngine.register_detector("bili_dynamic")
async def bili_dynamic_detector(config: Config, store: MessageStore) -> None:
    for sub in config.bilibili.subscriptions:
        dynamics = await fetch_new_dynamics(uid=sub.uid, config=config)
        for dyn in dynamics:
            store.add_new(
                msg_id=f"bili_dyn:{dyn.dynamic_id}",
                platform="bili",
                content_type=ContentType.DYNAMIC,
                pubdate=dyn.pubdate,
                title=dyn.content[:50],
                author=dyn.author,
            )
```

> 注意：detector 注册 key 为 `"bili_dynamic"` 而非 `"bili"`，因为 B站已有一个 `"bili"` detector 负责 VIDEO。`run_platform` 需要支持同时调用多个 detector，或改为按平台+类型调度。

## Handler 注册

### 摘要阶段（DYNAMIC 的 SUMMARIZED 可能与其他类型共享）

```python
@PipelineEngine.register("bili", Phase.SUMMARIZED)  # 复用已注册的摘要 handler
# 动态不需要额外 handler — SUMMARIZED 已在主 spec 的 VIDEO handler 中注册
```

### 推送阶段

```python
@PipelineEngine.register("bili_dynamic", Phase.PUSHED)
async def bili_dynamic_push(ctx: PhaseContext) -> bool:
    """发送 B站动态通知"""
    return await notify_dynamic(
        dynamic_info={...},
        config=ctx.config.bilibili.notification,
    )
```

## 对主 spec 的修改要求

1. `PipelineEngine.run_platform()` 需支持同时调用多个 detector（例如 `"bili"` + `"bili_dynamic"`）
2. 或 `run_bili_check_once` 中手动调用两次 `engine.run_detector(...)` + `engine.process_all(...)`

推荐方案：`run_platform("bili")` 自动发现所有以 `"bili"` 开头的 detector key 并执行。

# 按需消息处理(fetch-and-process)

- **Issue**: [#101](https://github.com/YinY1/trawler/issues/101)
- **日期**: 2026-07-06
- **分支**: `feat/on-demand-message-processing`(基于 `docs/readme-bilingual-update`)
- **状态**: Draft

## 背景与动机

当前手动重跑能力(`run_specific_messages` / CLI `--mode manual` / API `POST /messages/rerun`)全部强依赖 `MessageStore` 里已有记录。对一条**从未入库**的历史消息,没有任何入口能让它走完整流水线(detector → process → push)。

用户需求:**单纯指定某些历史消息进行处理,算不上重跑**。这应当是一个独立于订阅体系的"按需处理入口"。

### 现状障碍

| 入口 | 对不存在消息的处理 |
|---|---|
| API `/messages/rerun` | 全部不存在 → 404 |
| Web `/messages/batch-reprocess` | 静默跳过 |
| CLI `--mode manual` | `query_messages` 查不到 → 直接 return |

`run_specific_messages` 注释明确写"不跑 detector(只对已存在的消息重跑)"。

`MessageStore.add_new` 硬性时间窗检查:`if not self.is_in_window(pubdate): return None` —— 超 24h 历史消息被丢弃。

三平台 detector 走订阅列表(`fetch_user_videos(uid)` / `fetch_user_notes(user_id)` / `fetch_user_posts(user_id)`),**没有按 ID 抓单条**的入口。

### 平台 API 能力调研

| 平台 | 按 ID 抓单条能力 | 障碍 |
|---|---|---|
| **微博** | ✅ `fetch_post_detail(cookie, post_id)` 现成(在 download 阶段已用) | 无 |
| **小红书** | ⚠️ `AsyncXhsClient.get_note_by_id(note_id, xsec_token, xsec_source)` 现成 | token 拦路:外部仅给 note_id 时无 token,走 `pc_feed` 链路可能拿不到正文 |
| **B 站** | ❌ 无 `fetch_video_by_id`,只有 `fetch_user_videos(uid)` | 需新增,基于 `bilibili_api.video.Video(bvid).get_info()` |

## 决策摘要

| # | 决策点 | 选择 |
|---|---|---|
| 1 | 平台覆盖 | 全三平台(bili + xhs + weibo) |
| 2 | xhs xsec_token | 仅支持 note_id,token 缺失时失败明示(抛 `PermanentFetchError`) |
| 3 | 时间窗 | 突破 24h 限制,`add_new(force=True)` 入口允许任意历史 |
| 4 | cleanup 保护 | 不标记,按规则删(用户自行把握处理时机,24h 内走完即可) |
| 5 | skip_push 默认 | **False**(默认要推,与"处理新消息"语义一致) |
| 6 | 入口语义 | 新增独立 fetch-and-process 入口(与 rerun 区分) |
| 7 | 实现方案 | 方案 A:平台层 `fetch_by_id` + 引擎层 `run_fetch_and_process` |

## 架构

### 分层

```
┌──────────────────────────────────────────────────────────┐
│  入口层:  CLI  trawler fetch --ids ...                    │
│           API  POST /messages/fetch                       │
│           Web  /messages/fetch-form (后续扩展,本次不做)   │
└────────────────────────┬─────────────────────────────────┘
                         │
┌────────────────────────▼─────────────────────────────────┐
│  引擎层:  PipelineEngine.run_fetch_and_process           │
│           - 对每个 msg_id:                                │
│             已存在 → _safe_process_message(续跑)          │
│             不存在 → 平台 fetch_by_id → add_new(force) →  │
│                      _safe_process_message                │
└────────────────────────┬─────────────────────────────────┘
                         │
┌────────────────────────▼─────────────────────────────────┐
│  平台层:  fetch_by_id(msg_id, config) -> FetchedMessage  │
│           - bili:   基于 bilibili_api.video.Video         │
│           - xhs:    AsyncXhsClient.get_note_by_id         │
│           - weibo:  包装 fetch_post_detail                │
└──────────────────────────────────────────────────────────┘
```

### 数据流

```
[用户给 IDs]
     │
     ▼
[CLI/API 入口] → 占 state.check_running 单锁
     │
     ▼
[run_fetch_and_process]
     │
     ├─ 对每个 msg_id:
     │   │
     │   ├─ store.get_message != None  ──→ 已存在 → _safe_process_message
     │   │
     │   └─ store.get_message == None  ──→ fetch_by_id(msg_id, config)
     │                                       │
     │                                       ├─ PermanentFetchError → log + skip
     │                                       ├─ None                → log + skip
     │                                       └─ FetchedMessage      → add_new(force=True)
     │                                                                    │
     │                                                                    ▼
     │                                                            _safe_process_message
     │
     ▼
[store.save()]
```

## 组件设计

### §1 平台层 `fetch_by_id`

#### 统一契约

```python
async def fetch_by_id(
    msg_id: str,           # "bili:BV1xx" / "xhs:note1" / "weibo:123"
    config: Config,
) -> FetchedMessage | None:
    """按单条 ID 抓取消息元数据(不入库,纯抓取)。

    Returns:
        FetchedMessage 或 None(抓取失败/ID 不存在)。

    Raises:
        PermanentFetchError: 抓取永久失败(如 xhs token 缺失、平台明确拒绝),
            调用方应明示给用户,不重试。
    """
```

#### 新增数据结构(`shared/protocols.py`)

```python
@dataclass
class FetchedMessage:
    """fetch_by_id 返回的轻量载体,字段映射到 MessageRecord 所需。"""
    msg_id: str
    platform: str
    content_type: ContentType
    pubdate: int
    title: str
    author: str
    xsec_token: str = ""
    body: str = ""
```

#### 新增异常(`shared/exceptions.py`,继承 `TrawlerError`)

```python
class PermanentFetchError(TrawlerError):
    """按 ID 抓取永久失败(调用方应明示,不重试)。"""
```

#### 三平台实现

| 平台 | 文件 | 实现要点 |
|---|---|---|
| **bili** | `platforms/bilibili/monitor.py` 新增 `fetch_video_by_id` | 基于 `bilibili_api.video.Video(bvid).get_info()`;`content_type=VIDEO`;从 API 返回解析 title/pubdate/author/desc |
| **xhs** | `platforms/xiaohongshu/monitor.py` 新增 `fetch_note_by_id` | 调 `AsyncXhsClient.get_note_by_id(note_id, xsec_token="", xsec_source="pc_feed")`;**失败信号**:`DataError` 异常(server 拒绝/-100)→ 抛 `PermanentFetchError`;若拿到 `note_card` 但 `desc` / `image_list` / `video` 全空 → 抛 `PermanentFetchError("xhs: 笔记正文为空,可能 xsec_token 缺失")`;`content_type` 按 `note_card.type == "video"` 判断 |
| **weibo** | `platforms/weibo/monitor.py` 新增 `fetch_post_by_id`(包装 `api.fetch_post_detail`) | `fetch_post_detail(cookie, post_id)` 返回 dict;判 `page_info.media_info` 有无 video → `content_type`;title 取 `clean_text[:50]`;body 取长文 `_fetch_long_text` 结果 |

#### 注册机制

引擎新增 `_FETCHERS` 注册表(对称现有 `_detectors`):

```python
_FETCHERS: dict[str, Callable[[str, Config], Awaitable[FetchedMessage | None]]] = {}

@classmethod
def register_fetcher(cls, platform: str) -> Callable[..., Any]:
    def decorator(func): 
        cls._FETCHERS[platform] = func
        return func
    return decorator
```

平台 `handlers.py` 用装饰器注册:

```python
# platforms/bilibili/handlers.py
@PipelineEngine.register_fetcher("bili")
async def bili_fetch_by_id(msg_id: str, config: Config) -> FetchedMessage | None:
    bvid = msg_id.removeprefix("bili:")
    return await fetch_video_by_id(bvid, config)
```

### §2 Store 层 `add_new` 加 `force` 参数

**改动**(`shared/message_store.py::add_new`):

```python
def add_new(self, ..., *, force: bool = False) -> MessageRecord | None:
    """添加新消息。

    Args:
        force: True 时绕过 is_in_window 时间窗口检查(按需入口专用)。
            is_known 去重检查**不变**(force 不绕过去重)。
            cron detector 调用方默认 False,行为完全不变。
    """
    if self.is_known(msg_id):
        return None
    if not force and not self.is_in_window(pubdate):
        return None
    ...
```

**Trade-offs**:
- ✅ 单参数最小改动,所有 45 个现有调用点零影响
- ✅ `is_known` 去重保留 —— 重复 fetch 同一 ID 不会创建重复记录
- ⚠️ force=True 入库的消息**不享受 cleanup 保护**(决策 #4),24h 内未处理完会被 cleanup 删除

### §3 引擎层 `PipelineEngine.run_fetch_and_process`

**新方法**(放 `core/engine.py`,与 `run_specific_messages` 对称):

```python
@classmethod
async def run_fetch_and_process(
    cls,
    msg_ids: list[str],
    skip_push: bool,
    config: Config,
    store: MessageStore,
    log_callback: Callable[[str, str], None] | None = None,
) -> None:
    """按 ID 抓取并处理(不依赖订阅 / 已入库记录)。

    对每个 msg_id:
    1. 已存在 store → 直接 _safe_process_message(走当前 phase 续跑)
    2. 不存在 → 调对应平台 fetch_by_id:
       - PermanentFetchError → log + 跳过(明示给用户,不创建 record)
       - 返回 None(抓取失败但可重试) → log + 跳过
       - 成功 → store.add_new(force=True) → _safe_process_message

    与 run_specific_messages 区别:
    - 跑 fetch(detector 的按 ID 单条等价物)
    - 不调 cleanup(同 D6 决策,避免误删历史)
    - from_phase 固定 DISCOVERED(新抓的消息从 0 开始)
    """
```

**msg_id 前缀路由**:

```python
def _platform_from_msg_id(msg_id: str) -> str | None:
    if msg_id.startswith("bili:"): return "bili"
    if msg_id.startswith("xhs:"):  return "xhs"
    if msg_id.startswith("weibo:"): return "weibo"
    return None
```

**并发安全**:与 `run_specific_messages` 相同 —— 不持有文件锁,复用 `state.check_running` 单锁(在 API/Web 入口占锁),避免与 cron `run_check_once` 并发写 store。

**错误处理**:

| 场景 | 行为 |
|---|---|
| msg_id 前缀未知 | log warning,跳过(不创建 record) |
| `fetch_by_id` 抛 `PermanentFetchError` | log error,跳过(不创建 record) |
| `fetch_by_id` 返回 None | log warning,跳过 |
| `fetch_by_id` 成功但 `add_new` 返回 None | 已存在(理论不该发生,step 1 已检查),直接 process |
| `process_message` 失败 | 现有 `_safe_process_message` 三档 retry 策略接管 |

**与 `run_specific_messages` 关系**:不互相复用,各自独立,但共用底层 `_safe_process_message` / `process_message`。

**已存在消息的处理边界**:
- 当前 phase < PUSHED → `_safe_process_message` 继续推进剩余阶段
- 当前 phase == PUSHED → `phases[start_idx + 1:]` 为空,`process_message` 内部 for 循环不执行,log "处理完成" → 无副作用(幂等)
- 当前 phase == PUSHED 且有 error → 现有 `process_message` 不清 error 直接走循环(也是无副作用,因 phase 已到终点)

即:**fetch 入口对已 PUSHED 消息是 no-op**,不会重复推送也不会重置状态。用户要重推已 PUSHED 消息应走 `rerun` 入口(显式 reset + skip_push=False)。

### §4 CLI 入口 `trawler fetch`

新增独立命令(`run_check.py`,与 `check` / `subscription` 平级):

```bash
trawler fetch --ids bili:BV1xx,xhs:note1,weibo:123 [--skip-push] [--platform all]
```

**Click 命令**:

```python
@cli.command()
@click.option("--ids", required=True,
              help="逗号分隔的消息 ID,如 bili:BV1xx,xhs:note1,weibo:123")
@click.option("--skip-push", is_flag=True, default=False,
              help="跳过推送通知(默认推送)")
@click.option("--platform", default="all",
              help="平台过滤器(默认 all,一般无需指定)")
def fetch(ids: str, skip_push: bool, platform: str) -> None:
    """按指定消息 ID 抓取并处理(不依赖订阅)。

    - 对每个 ID:不存在则抓取入库 + 走完整流水线;已存在则直接处理
    - 默认推送给订阅者(--skip-push 跳过)
    - 突破 24h 时间窗限制,允许任意历史消息
    """
```

**前缀校验**(快速失败):

```python
valid_prefixes = {"bili:", "xhs:", "weibo:"}
invalid = [m for m in msg_ids if not any(m.startswith(p) for p in valid_prefixes)]
if invalid:
    console.print(f"[red]✗[/] 无效的 msg_id(需 bili:/xhs:/weibo: 前缀): {invalid}")
    sys.exit(1)
```

**与 `_run_manual_check` 区别**:
- 无 `--since` / `--title` / `--author` / `--reset-phase`(那些是查已入库的筛选器)
- 有 `--ids`(显式指定)
- 不调 `query_messages` / detector / cleanup

### §5 API 入口 `POST /messages/fetch`

**新 schema**(`api/schemas.py`):

```python
class FetchRequest(BaseModel):
    """POST /messages/fetch 请求体。"""
    msg_ids: list[str]
    skip_push: bool = False    # 决策 #5: 默认推


class FetchResponse(BaseModel):
    """POST /messages/fetch 成功响应(202)。"""
    status: str
    task_id: str | None = None
    fetch_count: int | None = None    # 实际进入抓取流程的 ID 数
```

**新路由**(`api/routes/messages.py`):

```python
@router.post("/messages/fetch", response_model=FetchResponse, status_code=202)
async def fetch_messages(
    body: FetchRequest,
    request: Request,
    _token_name: str = Depends(require_token),
) -> FetchResponse | JSONResponse:
    """按 ID 抓取并处理。

    与 /messages/rerun 对称:
    - msg_ids 空 → 422
    - 已有 run 在跑 → 409 {"status": "already_running", "task_id": ...}
    - 成功 → 202 + {"status": "started", "task_id": ..., "fetch_count": ...}

    后台 task 调 PipelineEngine.run_fetch_and_process。
    """
```

**与 `/messages/rerun` 区别**:
- 不返回 `reset_count`(没有 reset 操作)
- 返回 `fetch_count`(实际进入抓取的 ID 数)
- 不存在 ID 不返回 404(本次就是要抓取不存在的)

### §6 Web 入口(本次不做,后续扩展)

Web UI 的批量 fetch 表单作为后续 issue 处理,本次仅做 CLI + API 两个入口,保持范围聚焦。

## 不做(YAGNI)

- ❌ Web UI 的 fetch 表单(后续 issue)
- ❌ cleanup 保护标记(决策 #4 已拒绝)
- ❌ xsec_token 的可选参数模式(决策 #2 已选最简方案)
- ❌ fetch 历史记录持久化(用户自行通过 messages.json 查看)
- ❌ fetch 进度 SSE 推送(复用现有 `/check/status` 轮询机制即可)

## 测试策略

### 单元测试

| 模块 | 测试点 |
|---|---|
| `FetchedMessage` / `PermanentFetchError` | 数据类构造、异常 raise/catch |
| `store.add_new(force=True)` | 超 24h 消息成功入库;is_known 去重仍生效 |
| `fetch_video_by_id` (bili) | mock `bilibili_api.video.Video.get_info` → 正确解析 |
| `fetch_note_by_id` (xhs) | mock `AsyncXhsClient.get_note_by_id`:有 token / 无 token 抛 PermanentFetchError |
| `fetch_post_by_id` (weibo) | mock `fetch_post_detail`:有 video / 无 video 区分 content_type |
| `run_fetch_and_process` | 已存在消息走续跑;不存在走 fetch+process;PermanentFetchError 跳过不创建 record |

### 集成测试

- CLI `trawler fetch --ids ...` 端到端(mock 平台 API)
- API `POST /messages/fetch` 端到端(mock 平台 API)

### 测试策略

按 TDD 流程:先写测试 → 跑红 → 实现 → 跑绿。

## 实现顺序(供 plan 参考)

1. `shared/protocols.py`:加 `FetchedMessage` + `PermanentFetchError`
2. `shared/message_store.py`:`add_new` 加 `force` 参数(+测试)
3. 平台层 `fetch_by_id`(三平台,+测试)
4. `core/engine.py`:`register_fetcher` + `_FETCHERS` + `run_fetch_and_process`(+测试)
5. `run_check.py`:`fetch` CLI 命令(+测试)
6. `api/schemas.py` + `api/routes/messages.py`:`POST /messages/fetch`(+测试)
7. 文档更新(`config.toml.example` 无关;`README.md` 提及新命令)

## 风险与开放问题

### 风险

| 风险 | 影响 | 缓解 |
|---|---|---|
| bili `bilibili_api.video.Video` 接口变更 | 抓取失败 | 实现时查证当前库版本 API;失败时返回 None 让用户重试 |
| xhs `pc_feed` 链路拿不到正文 | xhs fetch 大部分失败 | 失败明示(PermanentFetchError),用户知道原因;未来可加 token 输入 |
| weibo 长文 `_fetch_long_text` 失败 | body 为空 | 不阻塞,仍入库走流水线(summary 基于标题) |

### 开放问题

无 —— 所有关键决策已在 brainstorming 阶段确认。

## 参考

- Issue #101: https://github.com/YinY1/trawler/issues/101
- 现有 `run_specific_messages`: `core/engine.py:334`
- 现有 `MessageStore.add_new`: `shared/message_store.py:228`
- 现有 platform detectors: `platforms/{bili,xhs,weibo}/handlers.py`

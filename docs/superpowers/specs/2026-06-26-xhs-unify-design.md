# xiaohongshu 模块彻底统一到 xhs 库 - 设计文档

**日期**: 2026-06-26
**分支**: `refactor/xhs-unify`
**状态**: 设计完成,待 plan
**前置**: PR #41 (xhs auth 迁移) + PR #42 (nickname 修复 + dump 工具) 已合入 master

## 1. 背景与目标

### 1.1 现状

`platforms/xiaohongshu/` 当前存在**两套 HTTP 栈并存**的技术债:

- **★ xhs 库栈**(PR #41 引入): `auth.py` + `async_xhs_wrapper.py`,经 `_sign_adapter` 接 xhshow 签名
- **▣ aiohttp 老栈**(initial commit): `client.py`(371 行)+ `monitor.py` / `comments.py` / `search.py` / `downloader.py` 第三层,经 `signer.py` 直接签名

两套栈都调 xhshow 签名,但 HTTP 客户端、异常翻译、返回结构处理各做一套,导致维护成本翻倍。

### 1.2 目标

**彻底统一到 xhs 库栈**,删除老路径,模块只剩一套 HTTP 栈。具体:

1. `monitor.py` / `comments.py` / `search.py` / `downloader.py` 的 4 个业务方法调用点全部切到 `AsyncXhsClient` wrapper
2. 删除 `client.py` 整个文件
3. 删除 `downloader.py` 第二层死代码(外部 API server)
4. 修复 `downloader.py` 协调逻辑 bug(`success=False` 不降级)
5. 删除跨平台死分支(`bilibili/handlers.py` 的 xhs elif)
6. 补齐测试覆盖(downloader 当前 0%)

### 1.3 非目标(显式排除)

- **分页策略不变**:保持 monitor 单页取(num=30)+ comments 两页,不迁移到 xhs 库的 `get_user_all_notes` / `get_note_all_comments`(那是行为变更,单独评估)
- **不重写为真异步**:保留 `asyncio.to_thread` 假异步方案(理由见 4.3)
- **不改 auth 业务逻辑**:`auth.py` 的 QR 登录流程不动,只把异常翻译 decorator 下沉

## 2. 架构总览

### 2.1 改前

```
platforms/xiaohongshu/
├── auth.py            ★ xhs 库 (via AsyncXhsClient)
├── async_xhs_wrapper  ★ xhs 库 (5 auth 方法)
├── signer.py          ▣ xhshow 签名 (被两侧复用)
├── client.py          ▣ aiohttp + signer (4业务+3死auth+辅助)
├── monitor.py         ▣ client.XhsClient.get_user_notes
├── comments.py        ▣ client.XhsClient.get_comments
├── search.py          ▣ client.XhsClient.search_users
├── downloader.py      混合 (1层xhs库弱签名 / 2层死API / 3层client.py)
├── parser.py          纯函数
└── handlers.py        编排

两套 HTTP 栈:
  ★ 栈: xhs.core.XhsClient (sync requests) → _sign_adapter → xhshow
  ▣ 栈: aiohttp.ClientSession → signer.get_xhs_sign → xhshow
```

### 2.2 改后

```
platforms/xiaohongshu/
├── auth.py            ★ xhs 库 (_wrap_xhs_call 改 import)
├── async_xhs_wrapper  ★ xhs 库 (+4业务方法 +异常翻译下沉 +dump)
├── signer.py          ▣ xhshow 签名 (不变)
├── [client.py 删]
├── monitor.py         ★ → AsyncXhsClient.get_user_notes
├── comments.py        ★ → AsyncXhsClient.get_note_comments
├── search.py          ★ → AsyncXhsClient.get_user_by_keyword
├── downloader.py      ★ 简化 (删2层 + 修协调bug + 1层/2层走wrapper)
├── parser.py          纯函数 (可能微调,待真机验证)
└── handlers.py        编排 (不变)

单一 HTTP 栈:
  xhs.core.XhsClient → _sign_adapter → xhshow
```

### 2.3 数据流(改后)

```
pipeline.run_platform("xhs")
  │
  ├─ xhs_detector
  │    └─ monitor.fetch_user_notes
  │          └─ AsyncXhsClient.get_user_notes ──┐
  │                                              │
  └─ [DOWNLOADED] xhs_download                   │
       ├─ downloader.download_note               │
       │    ├─ 第一层 AsyncXhsClient.get_note_by_id ── xhs.core.XhsClient
       │    └─ 第二层 AsyncXhsClient.get_note_by_id ── (同上,带 xsec_token + pc_share)
       ├─ parser.parse_note_content              │
       └─ [TEXT] comments.fetch_xhs_comment_highlights
            └─ AsyncXhsClient.get_note_comments ─┘
```

## 3. 详细设计

### 3.1 wrapper 扩展(`async_xhs_wrapper.py`)

#### 3.1.1 新增 4 个业务方法

延续现有 `asyncio.to_thread` 委托 + `_sign_adapter` 签名模式:

```python
async def get_user_notes(self, user_id: str, cursor: str = "") -> dict[str, Any]:
    """取用户笔记列表(单页, xhs 库写死 num=30)。
    返回完整 data dict: {notes, cursor, has_more}。
    """
    result = await asyncio.to_thread(self._client.get_user_notes, user_id, cursor)
    if DUMP_ENABLED:
        dump_response("xhs_user_notes", {"user_id": user_id, "cursor": cursor}, result)
    return result

async def get_note_by_id(
    self, note_id: str, xsec_token: str = "", xsec_source: str = "pc_feed"
) -> dict[str, Any]:
    """取笔记详情。返回 note_card dict(库内已解包 items[0].note_card)。
    xsec_source: 默认 pc_feed。downloader 第二层传 pc_share(老路径行为)。
    """
    result = await asyncio.to_thread(
        self._client.get_note_by_id, note_id, xsec_token, xsec_source
    )
    if DUMP_ENABLED:
        dump_response("xhs_note_by_id", {"note_id": note_id, "xsec_source": xsec_source}, result)
    return result

async def get_note_comments(
    self, note_id: str, cursor: str = "", xsec_token: str = ""
) -> dict[str, Any]:
    """取笔记评论(单页)。返回完整 data dict: {comments, cursor, has_more}。"""
    result = await asyncio.to_thread(
        self._client.get_note_comments, note_id, cursor, xsec_token
    )
    if DUMP_ENABLED:
        dump_response("xhs_note_comments", {"note_id": note_id, "cursor": cursor}, result)
    return result

async def get_user_by_keyword(self, keyword: str, page: int = 1) -> dict[str, Any]:
    """搜索用户。返回完整 data dict: {users: [...]}。"""
    result = await asyncio.to_thread(self._client.get_user_by_keyword, keyword, page)
    if DUMP_ENABLED:
        dump_response("xhs_user_by_keyword", {"keyword": keyword, "page": page}, result)
    return result
```

**设计决策**:
- **返回 dict,不解包** —— 让调用方显式处理层级(老 client.py 在 client 层解包是返回结构不一致的根源)
- **参数顺序对齐 xhs 库** —— `get_note_comments` 是 `(note_id, cursor, xsec_token)`,cursor 在前
- **`get_note_by_id` 暴露 `xsec_source`** —— 第三参数,默认 `pc_feed`(库默认),downloader 第二层显式传 `pc_share`

#### 3.1.2 异常翻译 decorator 下沉

**现状**: `auth.py:119-156` 的 `_wrap_xhs_call` 只装饰 auth 方法。

**改后**: 移到 `async_xhs_wrapper.py`,装饰**所有** wrapper 方法(auth + 4 业务)。`auth.py` 改 import 复用。

翻译映射:
```
xhs.DataFetchError    → DataError
xhs.IPBlockError      → IpBlockError   (注意:库大写 P,项目小写 p)
xhs.NeedVerifyError   → CaptchaError
xhs.SignError         → DataError
```

#### 3.1.3 不改的部分

- `_sign_adapter`(`:63-80`)—— 签名桥接不变
- `_suppress_xhs_stdout`(`:39-60`)—— 仅 `get_self_info` 等有 print 副作用的方法需要,4 业务方法**不接**
- `__init__` / `cookie` / `close` —— 不变

### 3.2 调用方适配

#### 3.2.1 `monitor.py`

```python
# 改前
from platforms.xiaohongshu.client import XhsClient
async def _fetch_notes_via_api(...) -> list[dict]:
    client = XhsClient(cookie=cookie)
    return await client.get_user_notes(user_id, cursor=cursor, num=num)  # 已解包 list

# 改后
from platforms.xiaohongshu.async_xhs_wrapper import AsyncXhsClient
async def _fetch_notes_via_api(...) -> list[dict]:
    client = AsyncXhsClient(cookie=cookie)
    data = await client.get_user_notes(user_id, cursor=cursor)  # 返回 dict
    return data.get("notes", [])  # 显式解包
```

调整:import 换 / 去掉 `num=num`(库写死 30) / 接收 dict 解包。

#### 3.2.2 `comments.py`

```python
# 改前
client = XhsClient(cookie=cookie)
data = await client.get_comments(note_id, xsec_token=xsec_token)
data2 = await client.get_comments(note_id, cursor=cursor, xsec_token=xsec_token)

# 改后
client = AsyncXhsClient(cookie=cookie)
data = await client.get_note_comments(note_id, xsec_token=xsec_token)
data2 = await client.get_note_comments(note_id, cursor=cursor, xsec_token=xsec_token)
```

调整:import 换 / 方法名 `get_comments`→`get_note_comments` / 参数顺序对齐库。

#### 3.2.3 `search.py`

```python
# 改前
client = XhsClient(cookie=cookie)
return await client.search_users(query, page=page)  # 已解包 list

# 改后
client = AsyncXhsClient(cookie=cookie)
data = await client.get_user_by_keyword(query, page=page)  # 返回 dict
return data.get("users", [])  # 显式解包
```

调整:import 换 / 方法名改 / 解包。

#### 3.2.4 `downloader.py` 第二层(原第三层)`_fetch_note_detail`

```python
# 改前
from platforms.xiaohongshu.client import XhsClient
client = XhsClient(cookie=cookie)
return await client.get_note_detail(note.note_id, note.xsec_token)

# 改后
from platforms.xiaohongshu.async_xhs_wrapper import AsyncXhsClient
client = AsyncXhsClient(cookie=cookie)
return await client.get_note_by_id(
    note.note_id,
    xsec_token=note.xsec_token,
    xsec_source="pc_share",  # 老路径行为,匹配分享链路 token
)
```

#### 3.2.5 `downloader.py` 第一层 `_try_xhs_downloader_lib`

```python
# 改前
from xhs import XhsClient  # ReaJason/xhs 同步 client,弱签名
client = XhsClient(cookie=cookie)
note_detail = client.get_note_by_id(note.note_id)

# 改后
from platforms.xiaohongshu.async_xhs_wrapper import AsyncXhsClient
client = AsyncXhsClient(cookie=cookie)  # 走 _sign_adapter,7头签名
note_detail = await client.get_note_by_id(note.note_id)  # 默认 pc_feed
```

调整:删 `from xhs import`,走 wrapper(降低风控概率)。

### 3.3 downloader 协调逻辑修复

**现状 bug**(`downloader.py:386-404`):

```python
result = await _try_xhs_downloader_lib(note, config)
if result is not None:          # None 才降级
    return result               # success=False 也 return,不降级 ← BUG
```

**改后**:

```python
async def download_note(note: NoteInfo, config: Config) -> XhsDownloadResult:
    logger.info(f"开始下载笔记: [{note.title}] (类型: {note.note_type})")

    # 第一层:快速路径(无 token, 默认 pc_feed)
    result = await _try_xhs_downloader_lib(note, config)
    if result is not None and result.success:
        logger.info(f"[第一层] xhs 库下载成功: {note.title}")
        return result

    # 第二层(原第三层):完整路径(带 token + pc_share)
    logger.info("[第二层] 使用直接下载")
    result = await _try_direct_download(note, config)
    logger.info(f"下载{'成功' if result.success else '失败'}: {note.title}")
    return result
```

调整:删第二层调用 / 第一层降级条件改 `is not None and result.success` / 日志改"第二层"。

### 3.4 删除清单

| 文件 | 删除内容 | 行数 | 依据 |
|---|---|---:|---|
| `platforms/xiaohongshu/client.py` | **整个文件** | 371 | 4业务迁wrapper + 3auth死方法 + `_request`/辅助 全无caller |
| `tests/test_xhs_client.py` | **整个文件** | 313 | 测被删的 client.py |
| `platforms/xiaohongshu/downloader.py` | `_try_xhs_downloader_api` + 主入口调用 | ~75 | 死代码(env无默认/config无字段/docs无提及/0测试) |
| `platforms/bilibili/handlers.py` | L196-205 `elif platform=="xhs"` | 10 | 跨平台死分支(bili handler 处理的 msg.platform 永远是 bili) |
| `README.md` | L204 `TRAWLER_XHS_DOWNLOADER_API` 行 | 1 | 死 API 的 env 文档 |
| `README.zh.md` | 对应行 | 1 | 同上 |

**总删除**: ~770 行

## 4. 关键决策与权衡

### 4.1 单一 HTTP 栈 vs 双栈

选择**单一 xhs 库栈**(方案 A)。理由:
- 复用 PR #41/#42 已验证的 wrapper 模式
- 改动集中,风险可控
- 双栈维护成本翻倍

### 4.2 异常翻译下沉 vs 各处自翻译

选择**下沉到 wrapper**。理由:
- 单一来源,4 业务方法自动获得翻译
- 老 client.py 自翻译那层不再需要

### 4.3 假异步(`to_thread`)vs 真异步(aiohttp)

选择**假异步**。理由:
- trawler pipeline 串行处理笔记(`engine.py:111` 的 `process_message` 一条一条)
- 一次 check 周期峰值并发 < 10,线程池默认 32 容量绰绰有余
- 真异步方案(方案 C)等于回到老路径,违背"统一"目标

### 4.4 分页策略不变

选择**保持单页**(`get_user_notes` / `get_note_comments`),不迁移到 `get_user_all_notes` / `get_note_all_comments`。理由:
- 这次目标是"统一 HTTP 栈、删技术债",不是改业务行为
- 全量分页涉及首次慢、风控概率升高、store 膨胀,应单独评估

### 4.5 downloader 删第二层

依据 @explorer 调研铁证:
- env 无默认值、`config.py` 无字段、`config.toml.example` 无提及、docker-compose 不列
- README 仅一行表格无说明、docs/superpowers/** 零提及
- git log 只有重命名 commit,无"添加/修复 server"commit
- 0% 测试覆盖
- `shutil.move` 设计与单容器部署不兼容

### 4.6 downloader 协调 bug 必修

`success=False` 不降级是潜藏 bug:第一层视频 URL 提取失败返回 `success=False` 时,不会到第三层,直接失败。必修。

## 5. 测试策略

### 5.1 测试文件变更

| 文件 | 操作 |
|---|---|
| `tests/test_xhs_client.py` | **删除**(测被删的 client.py) |
| `tests/test_async_xhs_wrapper.py` | **扩展**(+4 业务方法测试) |
| `tests/test_xhs_downloader.py` | **新建**(补 0% 覆盖) |
| `tests/test_xhs_monitor.py` | **新建**(解包逻辑) |
| `tests/test_xhs_comments.py` | **新建**(调用+解包) |
| `tests/test_xhs_search.py` | **改**(import 换 + 解包) |
| `tests/test_xhs_authenticator.py` | **微改**(`_wrap_xhs_call` import 换) |

### 5.2 核心测试场景

#### wrapper 4 业务方法(test_async_xhs_wrapper.py 扩展)

每个方法测:委托正确性 / 返回 dict 不解包 / dump 接入 / 异常翻译。
特别测 `get_note_by_id` 的 `xsec_source` 第三参数(pc_feed 默认 + pc_share 显式)。
异常翻译注意 `IPBlockError` 大小写差异。

#### downloader 协调逻辑(test_xhs_downloader.py 核心)

```python
async def test_first_layer_success_no_fallback(self):
    # 第一层 success=True → 不调第二层

async def test_first_layer_failure_falls_back(self):
    # 第一层 success=False → 降级到第二层  ← 修的bug

async def test_first_layer_none_falls_back(self):
    # 第一层 None → 降级到第二层
```

#### monitor/comments 解包

```python
async def test_unpacks_notes_from_dict(self):
    # mock 返回 {"notes":[...]} → 断言返回 list

async def test_empty_notes_when_missing_key(self):
    # mock 返回 {} → 断言返回 []
```

### 5.3 TDD 顺序

1. **红**:先写 wrapper 4 方法 + downloader 协调 + monitor/comments 解包测试
2. **绿**:写实现(章节 3 的代码)
3. **蓝**:重构(删 client.py + 删死代码)

### 5.4 真机字段验证移交章节 6

单元测试用 mock,**无法验证**字段名假设。移交真机验证。

## 6. 真机验证计划

### 6.1 必须真机验证的 3 项

| 验证项 | 风险 | 方案 |
|---|---|---|
| 4 业务方法返回结构字段名 | parser 字段假设错位 | dump 接入 → 跑一次 → 对比 |
| 签名适配对登录态请求够用性 | 只 a1 签名被风控 | 真机调 4 方法观察 461/471 |
| xsec_source=pc_feed vs pc_share | downloader 第一层挂 | 真机下载视频+图文各 1 |

### 6.2 临时验证脚本

新建 `scripts/verify_xhs_business.py`(**验证后删**):

```python
"""真机验证 4 业务方法返回结构 + 签名适配。
用法: TRAWLER_DUMP=1 uv run python scripts/verify_xhs_business.py
需要: config/cookies.toml 配好有效 xhs cookie
"""
import asyncio
from platforms.xiaohongshu.async_xhs_wrapper import AsyncXhsClient
from shared.config import load_config

async def main():
    config = load_config()
    cookie = config.xiaohongshu.auth.cookie
    client = AsyncXhsClient(cookie=cookie)

    # 1. get_user_notes
    notes_data = await client.get_user_notes("61ea4b8a0000000010003c15")
    print(f"[1] get_user_notes keys: {list(notes_data.keys())}")
    if notes_data.get("notes"):
        first_note = notes_data["notes"][0]
        print(f"    first note keys: {list(first_note.keys())}")
        print(f"    has display_title: {'display_title' in first_note}")
        print(f"    has xsec_token: {'xsec_token' in first_note}")

    # 2. get_note_by_id
    if notes_data.get("notes"):
        n = notes_data["notes"][0]
        note_detail = await client.get_note_by_id(
            n["note_id"], xsec_token=n.get("xsec_token", ""), xsec_source="pc_feed"
        )
        print(f"[2] get_note_by_id keys: {list(note_detail.keys())}")

    # 3. get_note_comments
    if notes_data.get("notes"):
        n = notes_data["notes"][0]
        comments_data = await client.get_note_comments(n["note_id"])
        print(f"[3] get_note_comments keys: {list(comments_data.keys())}")

    # 4. get_user_by_keyword
    users_data = await client.get_user_by_keyword("测试")
    print(f"[4] get_user_by_keyword keys: {list(users_data.keys())}")

    await client.close()

asyncio.run(main())
```

### 6.3 验证流程

```
1. TDD 完成(单元测试全绿) → 进真机验证
2. 配置有效 cookie (config/cookies.toml)
3. TRAWLER_DUMP=1 uv run python scripts/verify_xhs_business.py
4. 检查 console 输出 + /tmp/xhs_*_dump.jsonl
5. 对比 parser 假设(monitor._parse_note_from_api / comments._parse_comment 字段)
6. 若字段漂移 → 修 parser → 重跑
7. uv run trawler check --platform xhs (端到端)
8. 观察日志:461/471? 协调降级正常?
9. 删 scripts/verify_xhs_business.py
```

### 6.4 风险缓解

| 风险 | 缓解 |
|---|---|
| 4 业务方法无真机样本 | 6.2 脚本 + dump |
| xsec_source 差异 | 第一层 pc_feed,第二层 pc_share,互补 |
| 签名只 a1 | 真机观察;不够则补 web_session(fallback) |
| 并发模型变 | 串行处理峰值 <10,线程池 32 够 |
| 异常翻译缺口 | `_wrap_xhs_call` 下沉 |
| test_xhs_client.py 作废 | 等量替换 |
| parser 字段假设 | 真机 dump 对比 |

### 6.5 回滚预案

真机验证发现**无法快速修复**的问题(签名根本不够 / 字段结构完全不符)时:

1. `git checkout master` 丢弃 `refactor/xhs-unify`
2. 真机 dump 数据保留(`/tmp/xhs_*_dump.jsonl`),供下次 spec
3. 重新评估(可能需方案 C:aiohttp 重写)

## 7. 验收标准

merge 前必须满足:

- [ ] `uv run ruff check .` clean
- [ ] `uv run pyright .` clean
- [ ] `uv run pytest -x` 全绿(含新增测试)
- [ ] 真机验证脚本跑通,4 方法返回结构确认
- [ ] parser 字段假设全部验证(或修正)
- [ ] `uv run trawler check --platform xhs` 端到端成功
- [ ] 真机无 461/471 风控(或确认签名够用)
- [ ] `client.py` / `test_xhs_client.py` 已删
- [ ] downloader 协调 bug 已修 + 测试覆盖
- [ ] 临时验证脚本已删

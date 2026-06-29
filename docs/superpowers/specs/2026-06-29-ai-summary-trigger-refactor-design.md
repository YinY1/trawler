# AI 摘要触发条件重构 — 基于「是否有视频」而非 content_type/平台

**Issue**: #46
**Date**: 2026-06-29
**Status**: Design approved, ready for plan

## 背景与动机

issue #46 指出当前 AI 摘要触发条件的设计缺陷:

> 只有视频摘取音频转写后才需要 AI 摘要,其他时候全平台都应该推送全文。
> 重点不是平台行为,而是是否存在视频需要转写+摘要。

### 当前痛点

| 场景 | 当前行为 | 期望行为 |
|---|---|---|
| bili VIDEO | DISCOVERED→DOWNLOADED→TRANSCRIBED→SUMMARIZED→PUSHED | ✅ 一致 |
| bili DYNAMIC 含视频 | DISCOVERED→SUMMARIZED→PUSHED (对动态文字做 AI,没转写视频!) | 应走 VIDEO flow |
| bili DYNAMIC 纯文字 | DISCOVERED→SUMMARIZED→PUSHED (多余 AI 摘要,空文本调 LLM) | 走简化 flow 推全文 |
| weibo (各种) | DISCOVERED→DOWNLOADED→PUSHED + download handler 内联 AI 摘要 | text 推全文,含视频走转写+摘要 |
| xhs VIDEO | DISCOVERED→DOWNLOADED→TRANSCRIBED→SUMMARIZED→PUSHED | ✅ 一致 |
| xhs TEXT | DISCOVERED→DOWNLOADED→PUSHED (无摘要) | ✅ 一致 |

根本原因: `ContentType` 用了 DYNAMIC 类型来区分 bili 动态,但动态本身可以含视频或纯文字,单一 DYNAMIC 类型无法表达。各平台也有自己的特殊路径(weibo 内联 AI 摘要、xhs 完全无摘要),违反「阶段职责分离」。

## 设计目标

1. **统一抽象**: AI 摘要的触发条件变为「该消息是否含视频」,与平台/动态与否无关
2. **简化 PHASE_FLOW**: 从 3 条路径(VIDEO/TEXT/DYNAMIC)简化为 2 条(VIDEO/TEXT)
3. **职责分离**: 移除 weibo download handler 的内联 AI 摘要,所有摘要统一走 SUMMARIZED 阶段
4. **扩展性**: 未来新增平台只需判断「有无视频」,不需为每个 content_type 写特判

## 详细设计

### §1 数据模型变更

#### ContentType 枚举简化

```python
class ContentType(StrEnum):
    VIDEO = "video"   # 任何含视频附件的内容
    TEXT = "text"     # 纯文字/图片
```

**移除** `ContentType.DYNAMIC`。

#### PHASE_FLOW 简化

```python
PHASE_FLOW: dict[ContentType, list[Phase]] = {
    ContentType.VIDEO: [Phase.DISCOVERED, Phase.DOWNLOADED, Phase.TRANSCRIBED,
                         Phase.SUMMARIZED, Phase.PUSHED],
    ContentType.TEXT:  [Phase.DISCOVERED, Phase.DOWNLOADED, Phase.PUSHED],
}
```

只剩 2 条路径。DYNAMIC 不再是独立类型,bili 动态会按是否含视频归类到 VIDEO 或 TEXT。

#### MessageRecord

- 删除 `content_type=DYNAMIC` 用法
- `dynamic_text` 字段保留(bili 动态文字会附加到对应 VIDEO 消息,转写+摘要时一并考虑)
- **不新增** `has_video` 字段(决策:用 content_type 区分,不加冗余字段)

#### 数据迁移

用户已确认重置数据。无迁移逻辑。Release notes 标注「升级前请删除 `data/messages.json`」。

### §2 bili_dynamic_detector 改造

#### 当前行为(`platforms/bilibili/handlers.py:53-96`)

抓所有动态,全部注册为 `ContentType.DYNAMIC`。若 linked_bvid 对应的 VIDEO 消息已存在,跳过并把动态文字追加到该 VIDEO 消息的 dynamic_text。

#### 改造后

```python
@PipelineEngine.register_detector("bili_dynamic")
async def bili_dynamic_detector(...):
    dynamics = await fetch_dynamics(...)
    for d in dynamics:
        # 情况 1: 视频型动态,linked_bvid 对应的视频已被 bili_detector 注册
        if d.linked_bvid:
            existing = store.get_message(f"bili:{d.linked_bvid}")
            if existing:
                # 追加 dynamic_text 到该 VIDEO 消息(原逻辑保留)
                store.update_dynamic_text(f"bili:{d.linked_bvid}", d.text)
                continue

            # 情况 2: 视频型动态但视频未被注册 → 反查 bvid 注册为 VIDEO
            # 通过 dynamic API 拿到完整 bvid,以 bili:{bvid} 入库
            bvid = await _resolve_bvid_from_dynamic(d.id)  # 调 dynamic.detail API
            if bvid:
                store.add_new(
                    msg_id=f"bili:{bvid}",
                    platform="bili",
                    content_type=ContentType.VIDEO,
                    pubdate=d.pubdate,
                    title=d.text[:50] or f"bili:{bvid}",
                    author=d.author,
                    subscription_ref=sub.mid,
                )
                # 动态文字作为 dynamic_text 附加
                store.update_dynamic_text(f"bili:{bvid}", d.text)
                continue

        # 情况 3: 纯文字动态 → 注册为 TEXT,推全文
        store.add_new(
            msg_id=f"bili_dyn:{d.id}",
            platform="bili",
            content_type=ContentType.TEXT,
            pubdate=d.pubdate,
            title=d.text[:50] or f"bili_dyn:{d.id}",
            author=d.author,
            subscription_ref=sub.mid,
        )
```

#### dynamic.py 暴露的判断字段

`platforms/bilibili/dynamic.py:94-127` 已经解析动态 type:
- type 8 = 视频投屏
- type 4 = 短视频  
- type 2 = 图文动态
- type 1 = 文字动态

在 `_parse_dynamic` 返回值中新增 `has_video: bool` (type in (4, 8))。

### §3 weibo 视频检测与下载

#### API 调研结论

✅ 可行 — 通过 `m.weibo.cn/statuses/show?id={bid}` 拿视频直链:

- `data.page_info.urls` (dict[str, str]): 多分辨率 mp4 直链,key 是清晰度标签
- `data.page_info.media_info.stream_url`: 最低码率 mp4 直链(兜底)
- `data.page_info.media_info.stream_url_hd`: 高清 mp4 直链

cookie 推荐带但非必须(游客访问有频率限制)。秒拍/外部引用视频需特殊处理(本期暂不支持)。

#### WeiboPost 字段扩展

```python
@dataclass
class WeiboPost:
    ...
    video_urls: list[str] = field(default_factory=list)  # 视频直链(m3u8 或 mp4)
```

#### detector 改造(`platforms/weibo/handlers.py:30-48`)

```python
@PipelineEngine.register_detector("weibo")
async def weibo_detector(...):
    posts = await fetch_user_posts(...)
    for p in posts:
        content_type = ContentType.VIDEO if p.video_urls else ContentType.TEXT
        store.add_new(
            msg_id=f"weibo:{p.post_id}",
            platform="weibo",
            content_type=content_type,
            ...
        )
```

#### api.py 改造

`_parse_mobile_post` / `_parse_pc_post` 增加视频分支:
- 检测 `page_info.type in ("video", "live")` 且 `page_info.urls` 存在
- 取最高分辨率 mp4 URL 填入 `video_urls`

#### downloader 扩展

新增 `download_weibo_video(post, config) -> WeiboDownloadResult`,weibo_download handler 按 content_type 分支:
- VIDEO: 调 `download_weibo_video` 下视频到 `{download_dir}/weibo/{post_id}/{post_id}.mp4`
- TEXT: 保持当前行为(下图片)

### §4 xhs 改造

#### detector

xhs 已经按 `note.note_type == "video"` 区分 VIDEO/TEXT(`platforms/xiaohongshu/handlers.py:28-46`),**detector 层不需改**。

#### handler

xhs VIDEO 已走 5 阶段。TEXT 已走 3 阶段无摘要。**逻辑不需改**。

只需要 PHASE_FLOW 简化后,xhs 受益于统一的 2 条路径。

### §5 transcribe_phase / summarize_phase 改造

#### transcribe_phase(`platforms/bilibili/handlers.py:131+`)

```python
# 删除这个特判(PHASE_FLOW 已保证只有 VIDEO 会到 TRANSCRIBED):
# if ctx.msg.content_type != ContentType.VIDEO:
#     return True
```

保留 `if not filepath: ctx.permanent_error = True` 检查。

#### summarize_phase(`platforms/bilibili/handlers.py:187+`)

- 删除 DYNAMIC 特判逻辑
- bili VIDEO / xhs VIDEO / weibo VIDEO 都走评论抓取 + AI 摘要
- weibo VIDEO 抓评论用 `fetch_weibo_comment_highlights`(已有)

### §6 weibo 内联 AI 摘要移除

`platforms/weibo/handlers.py:116-134` 当前在 DOWNLOADED 阶段做 AI 摘要。改造后:

- weibo_download 只下载,不做 AI 摘要
- weibo VIDEO 通过 PHASE_FLOW 自然走 SUMMARIZED handler(`@register("*", Phase.SUMMARIZED)` 已覆盖)
- weibo TEXT 不再有 AI 摘要(推全文)

`engine.py:_flush_ctx_to_store` 中为兼容 weibo 路径的双 if 回写(line 48-49)可以简化为单一路径。

### §7 数据迁移

启动时无自动迁移代码。文档明确告知用户:
- 升级前手动删除 `data/messages.json`(或备份后清空)
- 旧 `DYNAMIC` 类型的消息没有兼容路径
- 升级后第一次 cron 会重新拉取所有订阅源

### §8 PR 拆分

为控制 review 复杂度,分 3 个 PR:

#### PR-1: 核心架构改造

- 删 `ContentType.DYNAMIC`,简化 PHASE_FLOW
- bili_dynamic_detector 改造(含反查 bvid)
- dynamic.py 暴露 has_video 字段
- transcribe_phase / summarize_phase 移除 content_type 特判
- 所有测试 fixture 更新(`ContentType.DYNAMIC` → `TEXT`/`VIDEO`)
- weibo 暂不动(保留内联 AI 摘要),xhs 不动

#### PR-2: weibo 视频支持

- WeiboPost 加 `video_urls` 字段
- weibo_detector 视频检测
- api.py parser 解析视频字段
- download_weibo_video 新函数
- weibo_download 移除内联 AI 摘要,按 content_type 分支
- engine.py `_flush_ctx_to_store` 简化

#### PR-3(可选): 数据迁移工具

- 如果上线后需要保留进度,加版本化迁移脚本
- 默认不实现,除非用户提出需求

## 涉及文件清单

### PR-1
| 文件 | 改动 |
|---|---|
| `shared/protocols.py` | 删 `ContentType.DYNAMIC`, 简化 PHASE_FLOW |
| `platforms/bilibili/handlers.py` | bili_dynamic_detector 区分 TEXT/VIDEO;移除 DYNAMIC 特判 |
| `platforms/bilibili/dynamic.py` | `_parse_dynamic` 返回 has_video |
| `platforms/bilibili/api.py` | (可能)新增 `_resolve_bvid_from_dynamic` |
| `platforms/bilibili/handlers.py` transcribe_phase | 移除 content_type 特判 |
| `platforms/bilibili/handlers.py` summarize_phase | 移除 DYNAMIC 特判 |
| `tests/` | 所有 DYNAMIC fixture 改为 TEXT/VIDEO;新增 detector 区分测试 |

### PR-2
| 文件 | 改动 |
|---|---|
| `shared/protocols.py` | WeiboPost 加 video_urls |
| `platforms/weibo/handlers.py` | detector 视频检测;download 移除内联 AI 摘要 |
| `platforms/weibo/api.py` | parser 解析视频字段 |
| `platforms/weibo/downloader.py` | 新增 download_weibo_video |
| `core/engine.py` | `_flush_ctx_to_store` 简化 |
| `tests/` | weibo 视频测试 |

## 风险与限制

1. **bili 反查 bvid 依赖动态 API**: 若 dynamic.detail API 失效或限流,孤立视频型动态会注册失败 → 测试覆盖此场景的降级(注册为 TEXT 或跳过)
2. **weibo 视频下载**: 视频 URL 有时效性,大文件下载可能超时 → 调整 `WEIBO_DOWNLOAD_TIMEOUT` 或单独配置
3. **weibo 秒拍/外部视频**: 本期不支持,messages 标记 TEXT 处理
4. **DYNAMIC 删除的下游影响**: dashboard / formatter / push handler 等可能有 DYNAMIC 特判,实现前需审计
5. **数据丢失**: 升级会清空 messages.json,用户感知是「重新开始拉取」— 在 PR description 和 release notes 强调

## 测试策略

### PR-1
- 单元测试:
  - bili_dynamic_detector 含视频(linked_bvid 已注册) → 追加 dynamic_text 到 VIDEO 消息
  - bili_dynamic_detector 含视频(linked_bvid 未注册) → 反查 bvid 后注册为 VIDEO
  - bili_dynamic_detector 纯文字 → 注册为 TEXT
  - transcribe_phase 移除特判后,TEXT 类型不会到达此阶段(用 PHASE_FLOW 测试覆盖)
  - summarize_phase 对所有 VIDEO 类型统一行为
- 删除所有 `ContentType.DYNAMIC` fixture,改为 TEXT 或 VIDEO

### PR-2
- 单元测试:
  - weibo_detector 检测 video_urls 字段
  - WeiboPost parser 正确提取视频 URL
  - download_weibo_video 成功下载 mp4
  - weibo VIDEO 走完整 5 阶段(engine 集成测试)
  - weibo TEXT 不再走 AI 摘要(对比改造前)

## 验证清单

每个 PR 合并前必须通过:
- `uv run ruff check .` — All checks passed
- `uv run pyright` — 0 errors
- `uv run pytest -x -q` — 全过
- 手动验证: 本地 `uv run trawler check --platform bili` 抓取一条新动态,确认正确分类

## 关联

- issue #46
- 当前 PHASE_FLOW 实现: `shared/protocols.py:258-276`
- 当前 detector 实现: 各 `platforms/*/handlers.py`
- weibo API 调研报告(本次 librarian 输出)

from __future__ import annotations

import time

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from core.subscription_cli import list_subscriptions
from shared.config import load_config
from shared.message_store import MessageStore
from shared.protocols import MessageRecord, Phase
from web.app import TEMPLATES

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    """Dashboard: message stats + recent messages."""
    config = await load_config()

    store = MessageStore(config.general.data_dir)
    all_msgs = store.get_messages_in_window()  # 默认 24h，只读安全

    # Stats
    total_msgs = len(all_msgs)
    pushed_count = sum(1 for m in all_msgs if m.phase == Phase.PUSHED)
    error_count = sum(1 for m in all_msgs if m.error)
    active_count = total_msgs - pushed_count

    # Tooltip message lists (sorted by pubdate desc; one-day window is small enough)
    def _recent(msgs: list[MessageRecord]) -> list[MessageRecord]:
        return sorted(msgs, key=lambda m: m.pubdate, reverse=True)

    active_messages = _recent([m for m in all_msgs if m.phase != Phase.PUSHED])
    error_messages = _recent([m for m in all_msgs if m.error])
    pushed_sample = _recent([m for m in all_msgs if m.phase == Phase.PUSHED])
    total_sample = _recent(all_msgs)

    # Token status counts
    token_ok = 0
    token_expired = 0
    token_none = 0
    for auth in (
        config.bilibili.auth,
        config.xiaohongshu.auth,
        config.weibo.auth,
    ):
        if auth.expires_at <= 0:
            token_none += 1
        elif auth.expires_at < time.time():
            token_expired += 1
        else:
            token_ok += 1

    # Subscription counts
    subs = await list_subscriptions()
    sub_counts = {platform: len(items) for platform, items in subs.items()}

    # Recent messages (top 20) — sorted by updated_at desc:
    # 用户希望按"最近处理"顺序看，updated_at 反映最后一次阶段推进时间
    recent = sorted(all_msgs, key=lambda m: m.updated_at, reverse=True)[:20]
    last_updated = recent[0].updated_at if recent else 0

    return TEMPLATES.TemplateResponse(
        request,
        "dashboard.html",
        {
            "active_nav": "dashboard",
            "total_msgs": total_msgs,
            "pushed_count": pushed_count,
            "error_count": error_count,
            "active_count": active_count,
            "active_messages": active_messages,
            "error_messages": error_messages,
            "pushed_sample": pushed_sample,
            "total_sample": total_sample,
            "token_ok": token_ok,
            "token_expired": token_expired,
            "token_none": token_none,
            "sub_counts": sub_counts,
            "recent_messages": recent,
            "last_updated": last_updated,
        },
    )

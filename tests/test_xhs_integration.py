from __future__ import annotations

import asyncio
import json
from pathlib import Path

from rich.console import Console

from platforms.xiaohongshu.auth import XhsAuthenticator
from platforms.xiaohongshu.comments import fetch_xhs_comment_highlights as fetch_xhs_comments
from platforms.xiaohongshu.monitor import fetch_user_notes
from shared.auth.base import AuthStatus, PlatformTokens
from shared.config import Config

# ════════════════════════════════════════════════════════════
# 集成测试：auth.py → monitor.py → comments.py
# 直接调用生产代码模块，验证 QR 登录后所有 API 正常工作。
# ════════════════════════════════════════════════════════════

console = Console()


async def main():
    console.print("=" * 60)
    console.print(" XHS 全链路集成测试：auth.py → monitor.py → comments.py")
    console.print("=" * 60)

    # ── Step 1: 登录 ──
    console.print("\n[Step 1/3] XhsAuthenticator.qr_login()")
    auth = XhsAuthenticator()
    status_msg = []

    def on_status(status: AuthStatus):
        status_msg.append(f"{status.status}: {status.message}")

    tokens = await auth.qr_login(on_status=on_status)
    console.print(f"  登录{'成功' if tokens else '失败'}")
    console.print(f"  cookies: {list(tokens.cookies.keys())}")
    console.print(f"  web_session: {'✓' if 'web_session' in tokens.cookies else '✗'}")
    assert "web_session" in tokens.cookies, "必须获取到 web_session"
    assert tokens.cookies.get("a1"), "必须有 a1"

    # 保存凭证供后续分析
    # 使用 write_text 而非 JsonSetStore，因为 tokens 是键值凭证存储而非去重集合
    out = Path("tests/xhs_debug_tokens.json")
    out.write_text(json.dumps({
        "platform": "xhs",
        "cookies": tokens.cookies,
        "obtained_at": tokens.obtained_at,
        "expires_at": tokens.expires_at,
    }, indent=2, ensure_ascii=False))
    console.print(f"  凭证已保存到 {out}")

    # ── Step 2: 验证 validate_tokens + refresh_tokens ──
    console.print("\n[Step 1.5/3] validate_tokens() + refresh_tokens()")
    valid = await auth.validate_tokens(tokens)
    console.print(f"  validate_tokens: {'✓' if valid else '✗'}")
    assert valid, "token 必须有效"

    refreshed = await auth.refresh_tokens(tokens)
    console.print(f"  refresh_tokens: {'✓ 续期' if refreshed.expires_at > tokens.expires_at else '≈ 不变'}")
    assert refreshed.expires_at >= tokens.expires_at

    # ── Step 3: monitor.py 获取笔记列表 ──
    console.print("\n[Step 2/3] monitor.fetch_user_notes()")

    # 测试用公开用户
    test_users = [
        {"user_id": "61ea4b8a0000000010003c15", "name": "测试用户1"},
    ]

    # 构建带 cookie 的 Config
    cfg = _build_config(tokens)

    for user in test_users:
        notes = await fetch_user_notes(
            user_id=user["user_id"],
            name=user["name"],
            config=cfg,
        )
        console.print(f"  {user['name']}: {len(notes)} 条笔记")
        for note in notes[:3]:
            console.print(f"    - {note.note_id}: {note.title[:40]}")
            assert note.note_id, "note_id 不能为空"

    # ── Step 4: comments.py 获取评论 ──
    console.print("\n[Step 3/3] fetch_xhs_comment_highlights()")
    note_id = "6608b0cc000000001f00e3b5"
    comments = await fetch_xhs_comments(note_id=note_id, config=cfg)
    console.print(f"  note {note_id}: {len(comments)} 条评论")
    for c in comments[:3]:
        console.print(f"    - {c.user_name}: {c.content[:40]}...")
        assert c.content, "comment content 不能为空"

    console.print("\n" + "=" * 60)
    console.print(" 全部通过！")
    console.print("=" * 60)


def _build_config(tokens: PlatformTokens) -> Config:
    """从 PlatformTokens 构建带 cookie 的 Config。"""
    cfg = Config()
    cookie_str = "; ".join(f"{k}={v}" for k, v in tokens.cookies.items())
    cfg.xiaohongshu.auth.cookie = cookie_str
    cfg.xiaohongshu.auth.expires_at = tokens.expires_at
    # 添加测试订阅
    cfg.xiaohongshu.subscriptions = []
    return cfg


if __name__ == "__main__":
    asyncio.run(main())

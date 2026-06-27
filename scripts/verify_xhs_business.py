"""真机验证 4 业务方法返回结构 + 签名适配 (临时脚本).

用法:
    TRAWLER_DUMP=1 uv run python scripts/verify_xhs_business.py

需要: config/cookies.toml 配好有效 xhs cookie(登录后的)

验证目的(spec §6.1):
    1. 4 业务方法返回结构字段名(parser 假设验证)
    2. 签名适配对登录态请求够用性(观察 461/471 风控)
    3. xsec_source=pc_feed vs pc_share 实际行为

验证完后,本脚本由 Task 16 删除(临时工具)。

See docs/superpowers/specs/2026-06-26-xhs-unify-design.md §6.2.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any, cast

from platforms.xiaohongshu.async_xhs_wrapper import AsyncXhsClient
from shared.config import load_config


def _print_section(title: str, data: dict[str, Any]) -> None:
    """打印一节内容,dict 显示 keys,完整数据已通过 dump 落盘。"""
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}")
    print(f"  top-level keys: {list(data.keys())}")
    # 第一层嵌套也打印 keys(辅助字段名核对)
    for k, v in list(data.items())[:5]:
        if isinstance(v, dict):
            v_dict = cast(dict[str, Any], v)
            print(f"  ├── {k}: dict with keys {list(v_dict.keys())[:8]}")
        elif isinstance(v, list):
            v_list = cast(list[dict[str, Any]], v)
            if v_list:
                print(f"  ├── {k}: list[{len(v_list)}], first item keys: {list(v_list[0].keys())[:8]}")
            else:
                print(f"  ├── {k}: list[0]")
        else:
            v_str = str(v)
            preview = v_str[:60] + "..." if len(v_str) > 60 else v_str
            print(f"  ├── {k}: {preview}")


async def main() -> int:
    """跑 4 业务方法,打印结构,异常时打印风控信号。"""
    print("=" * 60)
    print("  XHS 业务方法真机验证")
    print("  分支: refactor/xhs-unify")
    print(" Dump 落盘: /tmp/xhs_*_dump.jsonl (TRAWLER_DUMP=1 时)")
    print("=" * 60)

    # 加载 config 获取 cookie
    try:
        config = await load_config()
    except Exception as e:
        print(f"❌ 加载 config 失败: {e}")
        print("   检查 config/cookies.toml 是否配好")
        return 1

    cookie = config.xiaohongshu.auth.cookie
    if not cookie:
        print("❌ config.xiaohongshu.auth.cookie 为空")
        print("   先用 trawler 跑一次 QR 登录拿到 cookie 写入 config/cookies.toml")
        return 1

    print(f"✓ cookie 已加载(长度 {len(cookie)},前 30 字符: {cookie[:30]}...)")

    client = AsyncXhsClient(cookie=cookie)
    exit_code = 0

    # 用 test_xhs_integration.py 写死的测试用户 ID
    test_user_id = "59b3829850c4b4197d115edf"  # 小红书体育(653万粉丝，官方账号)

    # ═══════════════════════════════════════════════════════════
    # 1. get_user_notes
    # ═══════════════════════════════════════════════════════════
    notes_data: dict[str, Any] = {}
    try:
        notes_data = await client.get_user_notes(test_user_id)
        _print_section("1. get_user_notes", notes_data)
        # 验证 parser 假设字段(monitor._parse_note_from_api)
        notes = notes_data.get("notes", [])
        if notes:
            first = notes[0]
            print(f"\n  [parser 假设验证] first note 关键字段:")
            for field in ("note_id", "display_title", "cover", "interact_info", "xsec_token", "type", "user"):
                exists = field in first
                preview = ""
                if exists:
                    val = first[field]
                    preview = str(val)[:50] + "..." if len(str(val)) > 50 else str(val)
                print(f"    {field}: {'✓' if exists else '✗'} {preview}")
    except Exception as e:
        print(f"\n❌ [1] get_user_notes 失败: {type(e).__name__}: {e}")
        print("   (可能是风控 461/471,或 cookie 失效)")
        exit_code = 1

    # ═══════════════════════════════════════════════════════════
    # 2. get_note_by_id(pc_feed,快速路径)
    # ═══════════════════════════════════════════════════════════
    try:
        if notes_data.get("notes"):
            first_note = notes_data["notes"][0]
            note_id = first_note.get("note_id", "")
            xsec_token = first_note.get("xsec_token", "")
            print(f"\n  用 note_id={note_id} xsec_token={xsec_token[:20]}... 测试 get_note_by_id")
            detail = await client.get_note_by_id(note_id, xsec_token=xsec_token, xsec_source="pc_feed")
            _print_section("2. get_note_by_id (pc_feed)", detail)
        else:
            print("\n⚠️ [2] 跳过(第 1 步没拿到 notes,无法测 get_note_by_id)")
    except Exception as e:
        print(f"\n❌ [2] get_note_by_id 失败: {type(e).__name__}: {e}")
        exit_code = 1

    # ═══════════════════════════════════════════════════════════
    # 3. get_note_comments
    # ═══════════════════════════════════════════════════════════
    try:
        if notes_data.get("notes"):
            note_id = notes_data["notes"][0].get("note_id", "")
            comments_data = await client.get_note_comments(note_id)
            _print_section("3. get_note_comments", comments_data)
            # 验证 parser 假设字段(comments._parse_comment)
            comments = comments_data.get("comments", [])
            if comments:
                first_c = comments[0]
                print(f"\n  [parser 假设验证] first comment 关键字段:")
                for field in ("id", "content", "user_info", "like_count", "sub_comment_count"):
                    exists = field in first_c
                    print(f"    {field}: {'✓' if exists else '✗'}")
    except Exception as e:
        print(f"\n❌ [3] get_note_comments 失败: {type(e).__name__}: {e}")
        exit_code = 1

    # ═══════════════════════════════════════════════════════════
    # 4. get_user_by_keyword
    # ═══════════════════════════════════════════════════════════
    try:
        users_data = await client.get_user_by_keyword("小红书")
        _print_section("4. get_user_by_keyword", users_data)
        users = users_data.get("users", [])
        if users:
            first_u = users[0]
            print(f"\n  [parser 假设验证] first user 关键字段:")
            for field in ("user_id", "nickname", "avatar", "red_id"):
                exists = field in first_u
                print(f"    {field}: {'✓' if exists else '✗'}")
    except Exception as e:
        print(f"\n❌ [4] get_user_by_keyword 失败: {type(e).__name__}: {e}")
        exit_code = 1

    await client.close()

    # ═══════════════════════════════════════════════════════════
    # 总结
    # ═══════════════════════════════════════════════════════════
    print(f"\n{'=' * 60}")
    print("  验证完成")
    print(f"{'=' * 60}")
    if exit_code == 0:
        print("✓ 4 业务方法全部成功")
        print("\n下一步:")
        print("  1. 检查上面 [parser 假设验证] 字段是否齐全(✗ 表示字段漂移)")
        print("  2. 检查 /tmp/xhs_user_notes_dump.jsonl 等文件(完整数据)")
        print("  3. 把字段漂移或异常报给 controller,决定 Task 15a/15b 分支")
    else:
        print("⚠️  有方法失败,查看上面的 ❌ 错误")
        print("   常见原因:cookie 失效、风控(461/471)、网络")

    return exit_code


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

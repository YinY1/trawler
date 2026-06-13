"""B站登录调试 - 纯手写 HTTP，零库依赖"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import aiohttp
from rich.console import Console

from shared.auth.qr_display import display_qr_in_terminal

OUT_DIR = Path("data") / "debug"
OUT_DIR.mkdir(parents=True, exist_ok=True)
console = Console()

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
POLL_INTERVAL = 2


def _mask(value: str, keep: int = 4) -> str:
    """脱敏：保留前后 keep 位，中间用 * 代替。"""
    if len(value) <= keep * 2:
        return value[:keep] + "..." + value[-keep:]
    return value[:keep] + "*" * (len(value) - keep * 2) + value[-keep:]


def save(name: str, data: Any) -> None:
    """将调试数据保存到 JSON 文件（脱敏后写入）。"""
    # 脱敏敏感字段
    sanitized = _sanitize(data)
    path = OUT_DIR / f"{name}.json"
    path.write_text(json.dumps(sanitized, indent=2, ensure_ascii=False), encoding="utf-8")
    console.print(f"  [green]✓[/] {path.name}")


def _sanitize(obj: Any) -> Any:
    """递归脱敏 refresh_token 和 cookie value。"""
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize(v) for v in obj]
    return obj


def extract_set_cookies(resp: aiohttp.ClientResponse) -> dict[str, str]:
    """从响应头提取所有 Set-Cookie name→value。"""
    cookies: dict[str, str] = {}
    for header in resp.headers.getall("set-cookie", []):
        if "=" in header:
            name, rest = header.split("=", 1)
            value = rest.split(";")[0] if ";" in rest else rest
            cookies[name.strip()] = value.strip()
    return cookies


async def main() -> None:
    console.print("[bold]=== B站登录调试 - 纯手写 HTTP ===[/bold]\n")

    headers = {"User-Agent": UA, "Referer": "https://www.bilibili.com/"}

    # ── 1. 申请二维码 ──
    async with aiohttp.ClientSession(headers=headers, trust_env=False) as s:
        console.print("[1/4] [dim]申请二维码...[/dim]")
        async with s.get("https://passport.bilibili.com/x/passport-login/web/qrcode/generate") as resp:
            body = await resp.json()
            qr_key: str = body["data"]["qrcode_key"]
            qr_url: str = (
                f"https://account.bilibili.com/h5/account-h5/auth/scan-web?navhide=1&callback=close&qrcode_key={qr_key}"
            )

        save(
            "initial",
            {
                "qr_key": qr_key,
                "qr_url": qr_url,
                "generate_body": body,
                "generate_set_cookie": extract_set_cookies(resp),
            },
        )

        console.print(f"\n  [dim]QR key:[/dim] {qr_key}")
        display_qr_in_terminal(qr_url)
        print()  # QR 码后换行（display 里用 print）

        # ── 2. 轮询扫码状态 ──
        console.print("[2/4] [dim]轮询扫码状态...[/dim]\n")
        token = None
        for poll_num in range(1, 601):  # 最长等 20 分钟
            async with s.get(
                "https://passport.bilibili.com/x/passport-login/web/qrcode/poll",
                params={"qrcode_key": qr_key},
            ) as resp:
                body = await resp.json()
                data = body.get("data") or {}
                code = data.get("code", -1)
                msg = data.get("message", "")
                set_cookies = extract_set_cookies(resp)
                refresh_token = data.get("refresh_token", "")

                save(
                    f"poll_{poll_num:02d}",
                    {
                        "code": code,
                        "message": msg,
                        "set_cookie": set_cookies,
                        "refresh_token": refresh_token,
                        "body": body,
                    },
                )

                rt_icon = "[green]✓[/]" if refresh_token else "[red]✗[/]"
                console.print(
                    f"  [#{poll_num:03d}] code={code} msg={msg} cookies={len(set_cookies)} refresh_token={rt_icon}"
                )

                if code == 0:
                    # ── 3. 登录成功，拿 token ──
                    console.print("\n[green]✓ 登录成功！[/green]")
                    token = {
                        "refresh_token": refresh_token,
                        "url": data.get("url", ""),
                        "timestamp": data.get("timestamp", 0),
                        "set_cookie": set_cookies,
                    }
                    save("credential", token)
                    break

                if code == 86038:
                    console.print("[red]✗ 二维码已过期[/red]")
                    break
                # code=86090 = 已扫码未确认，继续
                # code=86101 = 未扫码，继续

            await asyncio.sleep(POLL_INTERVAL)

        # ── 4. 请求 redirect URL 拿完整 Cookie ──
        if token and token.get("url"):
            console.print("\n[3/4] [dim]请求 redirect URL...[/dim]")
            async with s.get(token["url"], allow_redirects=True) as resp:
                redirect_cookies = extract_set_cookies(resp)
                save(
                    "redirect",
                    {
                        "status": resp.status,
                        "url": str(resp.url),
                        "set_cookie": redirect_cookies,
                    },
                )
                console.print(f"  status={resp.status} cookies={len(redirect_cookies)}")
                token["redirect_set_cookie"] = redirect_cookies
                save("credential", token)

        console.print("\n[4/4] [dim]汇总[/dim]")
        console.print(f"\n数据已保存到 [dim]{OUT_DIR}/[/dim]")

        if token:
            rt = token.get("refresh_token", "")
            if rt:
                console.print(f"\nrefresh_token: [green]✓ 非空[/green] ({_mask(rt)})")
            else:
                console.print("\nrefresh_token: [red]✗ 空字符串[/red]")
            console.print(f"Set-Cookie 总数: {len(token.get('set_cookie', {}))}")
        else:
            console.print("\n[red]✗ 登录失败[/red]")


if __name__ == "__main__":
    asyncio.run(main())

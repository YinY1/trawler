"""QR code terminal rendering — uses terminal-qrcode for proper aspect ratio."""

from __future__ import annotations

import sys

from terminal_qrcode import generate


def display_qr_in_terminal(url: str) -> None:
    """Print QR code, then fallback URL.

    Uses terminal-qrcode with auto-detected renderer:
    - Kitty/iTerm2/WezTerm/Sixel: pixel-perfect graphic protocol
    - Fallback: halfblock text rendering
    """
    out = generate(url)
    print("─" * 40)
    out.print()
    print()
    print(f"扫码链接（备用）: {url}")
    sys.stdout.flush()

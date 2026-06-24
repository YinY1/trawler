#!/usr/bin/env bash
# 本地构建 Tailwind CSS（与 Dockerfile css-builder stage 等价）
#
# 用法：./scripts/build-css.sh
#
# 跨平台二进制选择（plan R4）：
#   - linux x86_64  → tailwindcss-linux-x64
#   - linux aarch64 → tailwindcss-linux-arm64
#   - macOS arm64   → tailwindcss-macos-arm64
#   - macOS x86_64  → tailwindcss-macos-x64
# 二进制缓存到 ~/.cache/tailwindcss-<asset>，避免重复下载。
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

VERSION=3.4.13

# ── 选对应平台二进制 asset 名 ──
case "$(uname -s)/$(uname -m)" in
  Linux/x86_64)  ASSET="tailwindcss-linux-x64" ;;
  Linux/aarch64|Linux/arm64) ASSET="tailwindcss-linux-arm64" ;;
  Darwin/arm64)  ASSET="tailwindcss-macos-arm64" ;;
  Darwin/x86_64) ASSET="tailwindcss-macos-x64" ;;
  *)
    echo "[build-css.sh] 不支持的平台: $(uname -s)/$(uname -m)" >&2
    echo "  请从 https://github.com/tailwindlabs/tailwindcss/releases/download/v${VERSION}/ 手动下载对应二进制" >&2
    exit 1
    ;;
esac

BIN="$HOME/.cache/${ASSET}"
mkdir -p "$(dirname "$BIN")"
if [ ! -x "$BIN" ]; then
  echo "[build-css.sh] 下载 ${ASSET} (v${VERSION})..."
  curl -fsSL -o "$BIN" \
    "https://github.com/tailwindlabs/tailwindcss/releases/download/v${VERSION}/${ASSET}"
  chmod +x "$BIN"
fi

mkdir -p web/static/css
"$BIN" --input web/src/input.css --output web/static/css/main.css --minify
echo "✓ built web/static/css/main.css"

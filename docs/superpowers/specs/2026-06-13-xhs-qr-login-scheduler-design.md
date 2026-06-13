# Phase 3+4: XHS QR 登录 + Token 续期调度集成 — 设计文档

> 日期: 2026-06-13
> 状态: 已批准（基于用户决策）

## Phase 3: 小红书 QR 登录

### 目标

为小红书平台实现 QR 扫码登录（`XhsAuthenticator(BaseAuthenticator)`），替换当前手动 Cookie 填写方式。

### 架构变更

```
platforms/xiaohongshu/
  auth.py              # 重构: XhsAuthenticator(BaseAuthenticator) + 保持 get_xhs_cookie() 向后兼容
  signer.py            # 新增: 签名模块封装 (subprocess + Node.js → vendor/spider_xhs)

vendor/spider_xhs/     # 从 cv-cat/Spider_XHS 克隆的签名 JS 文件

shared/auth/__init__.py  # + "xhs" in get_authenticator() factory

run_check.py           # trawler login --platform xhs 移除占位，走完整 QR 流程
```

### XhsAuthenticator 实现

| 方法 | 实现 |
|------|------|
| `generate_qr_code()` | POST edith.xiaohongshu.com/api/sns/web/v1/login/qrcode/create（需签名）→ 返回 qr_id + qr_url，生成 QRCodeResult(qr_url, qr_key=qr_id, expires_in=180) |
| `poll_qr_status()` | GET edith.xiaohongshu.com/api/sns/web/v1/login/qrcode/status?qr_id=...（需签名）→ 映射 code 到 QRStatus |
| `get_tokens()` | 轮询确认后调用确认登录 API，从 Set-Cookie 提取 a1, web_session, webId 等 cookies |
| `refresh_tokens()` | 访问 XHS 主页 keepalive + 访问个人主页保持 cookie 活跃，更新 expires_at |
| `validate_tokens()` | 访问 XHS 用户主页 API，检查 200/302 判断 cookie 有效性 |
| `supports_qr_login()` | True |
| `supports_refresh()` | True（保活） |

**关键设计决策：**
- QR 登录流程中的签名由外部 `signer.py` 模块负责，XhsAuthenticator 只关心认证逻辑
- Cookie 持久化方式不变：整个 cookie 字符串存入 `config.toml` 的 `[xiaohongshu.auth]` section
- `get_xhs_cookie()` 保持向后兼容，从 `Config.xiaohongshu.auth.cookie` 读取
- QR 登录成功后需多等待 2 秒再执行 `get_tokens()`，因为 XHS 服务端确认有延迟

### 签名集成（signer.py）

通过 `subprocess.run()` 调用 `node vendor/spider_xhs/static/xhs_main_260411.js` 生成签名。

```python
def get_xhs_sign(api: str, data: dict | str, a1: str = "", method: str = "POST") -> dict:
    """调用 Node.js 生成 XHS 签名参数。返回 {xs, xt, xs_common}"""
```

Spider_XHS 的 JS 文件需要 `node` 运行时。项目需要 Check: `shutil.which("node")`。

**QR 登录特殊说明：** QR 登录 API 在扫码前可能不需要完整签名（因为此时还没有 a1 cookie）。`generate_qr_code()` 调用时 a1 为空字符串，签名模块应能处理此情况。

### 向后兼容

现有 `get_xhs_cookie(config) → str` 和 `get_signed_params()` / `get_request_headers()` 保持不变。新增的 `XhsAuthenticator` 是独立的类，不影响现有监控/下载/评论流程。

---

## Phase 4: Token 续期调度集成

### 目标

将 token 续期检查集成到 `trawler check` 流程中，不需要独立 daemon 线程。

### 设计

**不是独立 daemon，而是跟随 check 流程：**

```
每次 trawler check --platform bili 启动时:
  1. 加载 config → 构建 PlatformTokens
  2. should_renew() 判断是否需要续期
  3. 如果需要 → authenticator.refresh_tokens() → token_store.update_auth_section()
  4. 续期后 → 正常执行内容检查
```

### 新增/修改文件

| 文件 | 变更 |
|------|------|
| `shared/auth/scheduler.py` | 新增 `check_and_renew_tokens(platform, config)` — 单平台 token 检查+续期入口 |
| `core/pipeline.py` | `run_check_once()` 开头调用 `check_and_renew_tokens()` 检查对应平台 token |
| `run_check.py` | `trawler token refresh --all` 新增 --all 选项，遍历所有已配置平台续期 |

### `check_and_renew_tokens()` 设计

```python
async def check_and_renew_tokens(platform: str, config: Config) -> RenewalResult:
    """Check if platform tokens need renewal, and renew if needed.
    
    Returns RenewalResult indicating what happened.
    """
```

流程：
1. 从 config 提取 `PlatformTokens`（各平台的 `_build_tokens()` 辅助函数）
2. 调用 `should_renew(tokens, config.auth.renewal)` 
3. 如果需要续期且未过期 → `authenticator.refresh_tokens()` → `update_auth_section()`
4. 如果已过期 → 记录警告日志，跳过续期

### 各平台 PlatformTokens 构建

各认证器新增类方法 `build_tokens_from_config(config) → PlatformTokens | None`：

- **B站**: `BilibiliAuth`(sessdata, bili_jct, buvid3, dedeuserid, expires_at) → PlatformTokens(cookies={SESSDATA, bili_jct, buvid3, DedeUserID}, ...)
- **微博**: `WeiboAuth`(cookie, expires_at) → 解析 cookie 字符串 → PlatformTokens
- **小红书**: `XhsAuth`(cookie, expires_at) → 解析 cookie 字符串 → PlatformTokens

### 风险与缓解

| 风险 | 缓解 |
|------|------|
| XHS 签名 API 变更频繁 | 使用 Spider_XHS 最新 JS 文件，签名失败时降级为 local sign 并记录警告 |
| Node.js 未安装 | `signer.py` 检查 `shutil.which("node")`，不可用时抛明确错误提示 |
| XHS QR 登录 API path 可能变更 | 从 Spider_XHS 的 `xhs_pc_login_apis.py` 参考最新 API endpoint |
| vendor/ 目录权限 or gitignore | `vendor/` 已在 .gitignore 中，确保不需要提交二进制 |
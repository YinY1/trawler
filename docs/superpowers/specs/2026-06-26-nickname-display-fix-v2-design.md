# 登录后昵称显示修复 v2 — 设计文档(基于真机数据)

**日期**: 2026-06-26
**状态**: 待实现
**前置**: PR #41 merge(8b1b14a),feat/dump-tool 已 commit
**取代**: 之前 v1 spec(已丢弃,基于错误假设)

## 背景

PR #41 完成后,weibo/xhs 登录成功但昵称显示为空。v1 spec 基于两个臆断(weibo SCANNED 响应带 nickname / xhs get_self_info2 能拿 nickname),真机全部证伪。本 v2 spec 完全基于 dump 工具抓到的真机数据。

## 真机事实(铁证)

### weibo(基于 /tmp/weibo_*_dump.jsonl)
- SCANNED 响应(retcode=50114002):`data: null`,无 nickname
- SUCCESS 响应(retcode=20000000):`data: {"url": "..."}`,无 nickname
- get_tokens cookies:`SCF, SUB, SUBP, ALC, ALF`,全是 token,无 nickname
- **整个 weibo 登录流程响应里都没有 nickname**,必须用 SUB cookie 额外调 API

### xhs(基于 /tmp/xhs_*_dump.jsonl)
- check_qrcode SUCCESS 响应:`{"login_info": {"user_id": "67405eb7...1b73"(真实), "session": "040069b6...96"}, "code_status": 2}` —— Set-Cookie 已把真实用户的 web_session 写入 jar
- **`activate()` 调用后**:服务端返回 `{"user_id": "6a3e7e6a...01400"(陌生), "session": "030037ad...13"}`,cookie jar 被覆盖
- **get_self_info 返回**:`{'code': -104, 'msg': '您当前登录的账号没有权限访问'}` —— 用了 activate 写入的匿名 session
- **xhs 库源码铁证**:`activate()` 是孤立无参方法,POST `/api/sns/web/v1/login/activate` 空 body,完全不读 login_info
- **官方 example 不调 activate**,check_qrcode SUCCESS 后直接读 cookie + 调 get_self_info
- **维护者测试标注**:`@pytest.mark.skip(reason="current this func is useless")`

## 目标

- **xhs**: 删除 `get_tokens` 里的 `activate()` 调用,让 check_qrcode 写入的真实 session 保留 → get_self_info 能正常拿到 nickname
- **weibo**: 实现 `get_user_nickname`,用 SUB cookie GET 主页 HTML,regex 抓 screen_name
- **共享**: PlatformTokens 加 nickname 字段 + 写 config 持久化 + 读 config 回填(这套 v1 实现是对的,保留)

## 设计

### 决策

| 决策 | 选择 | 理由 |
|---|---|---|
| PlatformTokens.nickname 字段 | 保留(v1 已对) | 跨进程持久化,向后兼容 |
| xhs get_tokens 调 activate | **删除** | 官方 example 不调,维护者标 useless,真机证伪 activate 会覆盖真实 session |
| xhs get_user_nickname | 调 get_self_info2(去掉 activate 后就能用) | 不带 activate 的 get_self_info 能正常返回,沿用 v1 的优先 tokens 降级 API 策略 |
| weibo get_user_nickname | GET `https://weibo.com/` + regex 抓 screen_name | 真机 6 次零风控,仅需 SUB cookie,内联 JSON 同时含 uid + screen_name |
| weibo 旧 ajax/profile/info | 删除 | 必须先有 uid 才能用,主页 HTML 一步到位更稳 |

### 变更清单

#### 1. `shared/auth/base.py` — PlatformTokens 加字段(v1 已实现,沿用)

```python
@dataclass
class PlatformTokens:
    platform: str
    cookies: dict[str, str]
    obtained_at: float
    expires_at: float
    nickname: str | None = None  # 新增
```

#### 2. `shared/config.py` — WeiboAuth + XhsAuth 加 nickname 字段

```python
@dataclass
class WeiboAuth:
    cookie: str = ""
    expires_at: float = 0.0
    nickname: str = ""  # 新增

@dataclass
class XhsAuth:
    cookie: str = ""
    expires_at: float = 0.0
    nickname: str = ""  # 新增
```

#### 3. `config/cookies.toml.example` — 两段补 nickname 字段(文档)

#### 4. `platforms/xiaohongshu/auth.py` — 删 activate + get_user_nickname 实现

**4a. get_tokens 删除 activate 调用**:
```python
# 当前(v1):
await self._client.activate()   # ← 删这一行
full_cookie_str = self._client.cookie
```

**4b. get_tokens 加 nickname 填充**(v1 已实现,但需重测 —— 去 activate 后 get_self_info2 应该能拿到 nickname):
```python
# activate 删除后,直接尝试 get_self_info2 填 nickname
nickname: str | None = None
try:
    info = await self._client.get_self_info2()
    if isinstance(info, dict):
        nickname = info.get("nickname") or None
except Exception as e:
    logger.warning("XHS get_self_info2 拿 nickname 失败: %s", e)
```

**4c. get_user_nickname 优先读 tokens,降级 API**(v1 已实现,沿用)。

**4d. build_tokens_from_config 传 nickname**(v1 已实现,沿用)。

#### 5. `platforms/weibo/auth.py` — 用主页 HTML 抓 screen_name

**5a. 删除 `WEIBO_USER_INFO_URL` 常量 + 注释**(老 ajax 接口)。

**5b. get_user_nickname 改为 GET 主页 + regex**:
```python
import re

# 主页 HTML 内联 JSON 里的 screen_name 正则
# 真机数据样本:{"uid":"5494676173","screen_name":"YinY1丶",...}
_SCREEN_NAME_RE = re.compile(r'"screen_name"\s*:\s*"([^"]+)"')

async def get_user_nickname(self, tokens: PlatformTokens) -> str | None:
    """从 weibo.com 主页 HTML 内联 JSON 提取登录账号昵称。

    主页会服务端渲染当前登录用户信息到 HTML(内联 JSON),
    regex 抓 screen_name。仅需 SUB cookie,未触发 6102 风控。
    """
    sub = tokens.cookies.get("SUB", "")
    if not sub:
        return None
    cookie_str = "; ".join(f"{k}={v}" for k, v in tokens.cookies.items())
    async with aiohttp.ClientSession(trust_env=False) as session:
        try:
            resp = await session.get(
                "https://weibo.com/",
                headers={
                    "User-Agent": _get_user_agent(),  # 桌面 UA
                    "Cookie": cookie_str,
                },
                timeout=aiohttp.ClientTimeout(total=WEIBO_REQUEST_TIMEOUT),
                allow_redirects=False,
            )
            try:
                if resp.status != 200:
                    return None
                html = await resp.text()
            finally:
                resp.close()
            match = _SCREEN_NAME_RE.search(html)
            return match.group(1) if match else None
        except Exception as e:
            logger.warning("微博 nickname 获取失败: %s", e)
            return None
```

**5c. build_tokens_from_config 传 nickname**(沿用 v1)。

**注意**:weibo 登录流程本身不填 tokens.nickname(响应里没 nickname),auth_poll 写 config 时 `nickname=""`(空串)。weibo 的 nickname 不依赖 config 持久化 —— `get_user_nickname` 每次调都实时 GET 主页 HTML 抓取,主页 HTML 是无副作用 GET,且即使 config 里 nickname 是空串,下次 _fetch_nickname 调用仍会触发 get_user_nickname 实时获取。这与 xhs 不同(xhs 依赖 config 持久化的 nickname,因为 get_self_info2 需要 cookie 完整登录态)。

#### 6. `web/routes/auth.py` — auth_poll + _tokens_to_auth_dict 写 nickname

```python
# auth_poll 的 weibo/xhs 分支(v1 已实现):
auth_dict = {
    "cookie": cookie_str,
    "expires_at": tokens.expires_at,
    "nickname": tokens.nickname or "",  # weibo 永远是 ""(登录时没填),xhs 会填
}

# _tokens_to_auth_dict(被 auth_refresh 调用)同样加 nickname(v1 已实现)
```

#### 7. dump 调用调整

现有 dump 工具的调用点要适配新逻辑:
- xhs `get_tokens` 删 activate 后,dump 位置调整
- weibo `get_user_nickname` 新增,加 dump 点(抓主页 HTML 响应)

## 测试

### xhs(沿用 v1 测试,验证去 activate 后能 GREEN)
- TestGetTokens:mock 不含 activate 调用,assert 直接读 cookie
- TestGetUserNickname:优先读 tokens / 降级 get_self_info2 / API 异常返 None
- 验证 mock 字段名与实现一致(get_self_info2 返回 nickname)

### weibo(新增主页 HTML 抓取测试)
- TestGetUserNickname:
  - `test_returns_screen_name_from_html`:mock 主页 HTML 含 `"screen_name":"测试用户"` → 返回 "测试用户"
  - `test_returns_none_on_no_sub`:tokens 无 SUB cookie → 返回 None
  - `test_returns_none_on_404`:status=404 → 返回 None
  - `test_returns_none_on_no_match`:HTML 不含 screen_name → 返回 None
  - `test_returns_none_on_network_error`:aiohttp.ClientError → 返回 None
  - **不 mock 6102 风控分支**(主页 HTML 真机不触发风控,YAGNI)

### 回归
- 全量 `pytest -x` 523+ passed
- `ruff check .` clean
- `pyright` 0 errors

## 真机验证(实现完成后)

**weibo**: 扫码登录 → 调 get_user_nickname → 验证主页 HTML 抓取返回 screen_name
**xhs**: 扫码登录 → 验证去掉 activate 后 cookie 里是真实 session → get_self_info 返回真实 nickname

## 风险

| 风险 | 缓解 |
|---|---|
| xhs 去 activate 后业务 API 仍然失败 | 真机 example 流程就是 activate-free,业务 API 用 check_qrcode 写入的真实 session,这是官方推荐流程 |
| weibo 主页 HTML 结构变了,正则失效 | 正则宽松 `"screen_name":"([^"]+)"`,内联 JSON 是新浪长年稳定的渲染方式;失败返 None 不阻断 |
| weibo 主页开始触发风控 | 真机 6 次零风控;主页是常规 GET,与 /ajax/profile/info 风控强度不同;若真触发,get_user_nickname 返 None 兜底 |

## 范围

7 文件:
```
shared/auth/base.py                         (+1 行)
shared/config.py                            (+2 行)
config/cookies.toml.example                 (+2 行)
platforms/weibo/auth.py                     (~30 行净增,新增主页 HTML 抓取)
platforms/xiaohongshu/auth.py               (-1 行删 activate,~15 行 nickname 填充)
web/routes/auth.py                          (+2 行)
tests/                                       (weibo 新增 ~5 测试,xhs 沿用 v1)
```

预计实现:1-2 小时 TDD。

# Plan: 修复 Weibo 按名称搜索用户始终返回空结果

**日期**: 2026-06-28
**范围**: `platforms/weibo/api.py`、`tests/test_weibo_api.py`
**作者**: @explorer (writing-plans)
**状态**: draft（已含调研证据 + 真机复现，待 review）
**分支**: `fix/weibo-search`（基于 master）

---

## 1. 背景

### 1.1 Bug 现场

用户在 web 订阅页 (`/subscriptions`) 微博卡片按名称搜索用户，结果列表为空。前端 toast 显示 `未找到名为「{name}」的用户`。

**已真机复现**（`ssh usa` → `docker exec trawler python3`）：

```python
>>> from platforms.weibo.api import search_user_by_name
>>> from shared.config import load_config
>>> cfg = await load_config("config/config.toml")
>>> users = await search_user_by_name(cfg.weibo.auth.cookie, "人民日报")
>>> users  # 实际输出
[]
```

服务器 cookie 配置正确（含 SUB，PC 端 mymblog/longtext/buildComments 等 API 都正常），唯独按名搜索不工作。

### 1.2 调用链（已确认）

```
web/routes/subscriptions.py:88   POST /subscriptions/search → search_by_name("weibo", name)
core/subscription_cli.py:318     _search_weibo → search_user_by_name(cookie, name)
platforms/weibo/api.py:484       search_user_by_name → GET MOBILE_USER_SEARCH_API
platforms/weibo/api.py:481       MOBILE_USER_SEARCH_API = "https://m.weibo.cn/api/container/getIndex?type=suggestion&value={nickname}"
                                ↑↑↑ 该 API 端点已废弃，返回 HTTP 404
platforms/weibo/api.py:516       resp.status != 200 → return []
web/templates/_candidates.html   candidates == [] → 显示 search_msg="未找到名为「...」的用户"
```

---

## 2. 根因分析

### 2.1 主因：搜索 API 端点已被微博官方下线

`platforms/weibo/api.py:481`：

```python
MOBILE_USER_SEARCH_API = "https://m.weibo.cn/api/container/getIndex?type=suggestion&value={nickname}"
```

**真机实测**（用服务器真实 cookie，2026-06-28）：

```
GET https://m.weibo.cn/api/container/getIndex?type=suggestion&value=人民日报
→ HTTP 404
→ body: <!DOCTYPE html>...<title>微博 - 出错了404</title>...「啊哦，你要找的页面不见啦！」
```

`type=suggestion` 这个 m.weibo.cn 端点已经下线。`search_user_by_name` 走到 `resp.status != 200 → return []` 静默分支，没有抛异常也没有日志（只有 `logger.exception` 在请求抛网络异常时才会写日志），所以服务器日志里看不到任何 `weibo.*search|搜索` 记录。

### 2.2 验证：现有 cookie 在 m.weibo.cn 域是「未登录」

服务器 cookies.toml 里的 cookie 来自 QR 登录流程（`platforms/weibo/auth.py` 走 `passport.weibo.com/sso/v2/qrcode`），主要落在 `passport.weibo.com` / `weibo.com` 域：

```
SCF / SUB / SUBP / ALC / ALF / XSRF-TOKEN / WBPSESS
```

真机调用 `m.weibo.cn/api/config`：

```json
{"preferQuickapp":0,"data":{"login":false,...}}
```

→ **`login:false`**。这些 cookie 在 m.weibo.cn 域视为未登录。即使存在可用的 m 端搜索 API，移动端路径也无法工作，除非：

- (a) 重新设计登录流程拿到 m.weibo.cn 域 cookie，或
- (b) 改走 PC 端的搜索接口

### 2.3 现有测试为什么没拦住

`tests/test_weibo_api.py:296-356` 的 `TestSearchUserByName` 全部用 MagicMock mock `aiohttp.ClientSession`，**只断言返回值结构 / 状态码分支处理**，**完全没断言 URL 是否仍可用**。所以 API 端点失效，测试照样绿。这是「mock 了不该 mock 的东西」的典型反例。

### 2.4 顺带发现的次级问题（不强求本次修）

- `platforms/weibo/api.py:136`：`except ValueError, OSError:` 是 Python 2 语法。Python 3.14 兼容地把它当 `(ValueError, OSError)` 解析（已用 dis 验证），所以暂时不影响运行，但是语法陷阱，建议顺手改成 `except (ValueError, OSError):`。

---

## 3. 调研证据（API 选型）

在服务器上对真实 cookie 测试了 4 个候选端点：

| 候选 | URL | 结果 |
|---|---|---|
| ❌ A. 旧 m 端 suggestion | `m.weibo.cn/api/container/getIndex?type=suggestion&value=` | **404 HTML 404 页**（当前用的就是这个） |
| ❌ B. m 端 container user | `m.weibo.cn/api/container/getIndex?containerid=100103type%3D3%26q%3D{name}` | 200 但返回 **SPA HTML**（已非 JSON） |
| ❌ C. m 端 + XHR header | 同 B 加 `X-Requested-With` | 200 但返回 `{"ok":-100, "url":"...passport.weibo.com/sso/signin..."}`，被 m 端视作未登录 |
| ❌ D. PC `/ajax/search/*` | `weibo.com/ajax/search/all`、`weibo.com/ajax/search/user` 等 | 404 `{"ok":0,"message":"你访问的地址不存在"}` |
| ✅ **E. s.weibo.com PC 网页搜索** | `s.weibo.com/user?q={name}&Refer=SUer_box` | **200，HTML 含完整结果列表**，cookie 正确认证 |

**E 的响应样本**（搜「人民日报」）：

```html
<a href="//weibo.com/u/2803301701" class="name" ... suda-data="...">人民日报</a>
<a href="//weibo.com/u/1411163204" class="name" ...>人民日报健康客户端</a>
<a href="//weibo.com/u/5703735355" class="name" ...>人民日报体育</a>
...
```

页面内嵌 `$CONFIG` 也确认登录态正确：

```javascript
$CONFIG['islogin'] = '1';
$CONFIG['uid'] = '5494676173';
$CONFIG['nick'] = 'YinY1丶';
```

**结论：改用 PC 网页搜索 `s.weibo.com/user`，HTML 正则解析。**

### 3.1 为什么不修登录流程拿 m 端 cookie？

- 登录流程已在 Phase-2（`docs/superpowers/plans/2026-06-12-weibo-platform-phase-2.md`）稳定下来，改它影响面大（auth.toml 格式、QR 流程、token 续期、所有依赖 cookie 的 PC 端 API）
- 现有 cookie 已经能跑 PC mymblog / longtext / buildComments，搜索也应该走 PC 路径，保持一致
- m 端在服务端越来越严格（B 和 C 都不可用），PC s.weibo.com 反而是官方支持的搜索入口

---

## 4. 修复方案

### 4.1 改 `platforms/weibo/api.py`

**位置**：`platforms/weibo/api.py:479-537`（`search_user_by_name` 整段）。

#### 4.1.1 常量替换

```diff
-# ── 用户搜索 ────────────────────────────────────────────────────
-
-MOBILE_USER_SEARCH_API = "https://m.weibo.cn/api/container/getIndex?type=suggestion&value={nickname}"
+# ── 用户搜索 ────────────────────────────────────────────────────
+
+# PC 网页搜索（s.weibo.com），返回 HTML
+# 用法: PC_USER_SEARCH_API.format(query=url_encoded_name)
+# 2026-06-28: m.weibo.cn/api/container/getIndex?type=suggestion 已下线（404）；
+# weibo.com/ajax/search/all 等也已失效（404 message:地址不存在）。
+# s.weibo.com/user 是当前唯一仍可用的搜索入口，HTML 内嵌 <a class="name">。
+# 风控兜底: s.weibo.com 在被风控时仍返回 200，但 HTML 是验证/跳转页，
+# 既不含 s.weibo.com 自身标记也不含 $CONFIG。函数内会先校验页面有效性，
+# 再用下面的块正则解析（见 §4.1.2 实现 diff）。
+PC_USER_SEARCH_API = "https://s.weibo.com/user?q={query}&Refer=SUer_box"
+
+# 搜索结果解析：以单个 <a ...>...</a> 为块（DOTALL 跨行），块内分别匹配 uid 与 name。
+# 不假设 href/class 顺序，不假设属性在同一行（微博模板排版不稳定）。
+# 单条样本形如:
+#   <a href="//weibo.com/u/2803301701" class="name" ...>人民日报</a>
+_SEARCH_RESULT_BLOCK_RE = re.compile(
+    r'<a\b(?P<attrs>[^>]*)>(?P<name>.*?)</a>',
+    re.DOTALL,
+)
+_SEARCH_UID_RE = re.compile(r'href\s*=\s*"?//weibo\.com/u/(?P<uid>\d+)"?')
+_SEARCH_NAME_CLASS_RE = re.compile(r'class\s*=\s*"[^"]*\bname\b[^"]*"')
```

#### 4.1.2 函数重写

```diff
 async def search_user_by_name(
     cookie: str,
     nickname: str,
     user_agent: str = _DEFAULT_UA,
 ) -> list[dict[str, Any]]:
-    """通过昵称搜索微博用户（移动端 suggestion API）。
+    """通过昵称搜索微博用户（PC 网页 s.weibo.com/user）。
+
+    失败原因（状态码 != 200 / 风控页 / 网络异常）只通过 ``logger.warning`` /
+    ``logger.exception`` 写到日志，**不抛异常、不在返回值中体现**。
+    因此调用方（``core/subscription_cli.py:_search_weibo``）只能看到空列表 +
+    通用提示「未找到名为「...」的用户」，无法区分「API 下线/风控」与「真的没结果」。
+    这是有意取舍：见 §10「日志与可见性」对权衡的讨论。

     Args:
-        cookie: 微博 Cookie 字符串（需含 SUB）
+        cookie: 微博 Cookie 字符串（需含 weibo.com 域 SUB）
         nickname: 搜索的昵称
         user_agent: 自定义 UA

     Returns:
-        用户列表，每项含 id / screen_name / description 等字段
+        用户列表，每项含 id / screen_name 字段（保持与旧 API 字段名兼容）
     """
-    url = MOBILE_USER_SEARCH_API.format(nickname=nickname)
+    from urllib.parse import quote
+
+    url = PC_USER_SEARCH_API.format(query=quote(nickname))
     headers = {
         "User-Agent": user_agent,
-        "Referer": "https://m.weibo.cn/",
+        "Referer": "https://weibo.com/",
         "Accept": "application/json, text/plain, */*",
         "Accept-Language": "zh-CN,zh;q=0.9",
         "Cookie": cookie,
     }

     async with aiohttp.ClientSession(trust_env=False) as session:
         try:
             async with session.get(
                 url,
                 headers=headers,
                 timeout=aiohttp.ClientTimeout(total=WEIBO_REQUEST_TIMEOUT),
             ) as resp:
                 if resp.status != 200:
+                    logger.warning("微博用户搜索返回状态码: %s", resp.status)
                     return []
-                data = await resp.json()
+                html = await resp.text()
         except Exception:
             logger.exception("微博用户搜索请求异常")
             return []

-    if not data.get("ok"):
-        return []
-
-    cards = data.get("data", {}).get("cards", [])
-    users: list[dict[str, Any]] = []
-    for card in cards:
-        if not isinstance(card, dict):
-            continue
-        card_group = card.get("card_group", [])
-        if not isinstance(card_group, list):
-            continue
-        for item in card_group:
-            user = item.get("user", {}) if isinstance(item, dict) else {}
-            if user.get("id") and user.get("screen_name"):
-                users.append(user)
-    return users
+    # 风控兜底：s.weibo.com 在被风控时仍返回 200，但页面是
+    # passport.weibo.com 跳转/验证页，HTML 中既无 $CONFIG 也无 s.weibo.com 自身标记。
+    # 与「正常但 0 结果」必须区分（否则用户/UI 无法判断是 API 失效还是真的没结果）。
+    if "s.weibo.com" not in html[:2000] and "$CONFIG" not in html[:5000]:
+        logger.warning("微博搜索返回疑似验证/风控页面（无 s.weibo.com / $CONFIG 标记），可能触发了风控")
+        return []
+
+    # 去重（同 uid 可能多次出现）
+    seen: set[str] = set()
+    users: list[dict[str, Any]] = []
+    import html as _html_module
+
+    for block_m in _SEARCH_RESULT_BLOCK_RE.finditer(html):
+        attrs = block_m.group("attrs")
+        raw_name = block_m.group("name")
+        uid_m = _SEARCH_UID_RE.search(attrs)
+        if not uid_m:
+            continue
+        if not _SEARCH_NAME_CLASS_RE.search(attrs):
+            continue
+        uid = uid_m.group("uid")
+        # html.unescape: 用户名里可能含 &amp; &lt; &quot; &#xxx; 等实体
+        name = _html_module.unescape(raw_name).strip()
+        if uid in seen or not name:
+            continue
+        seen.add(uid)
+        # 保持旧字段名: id (int) / screen_name (str)，
+        # 因为 core/subscription_cli.py:_search_weibo 用这两个键。
+        users.append({"id": int(uid), "screen_name": name})
+    return users
```

**字段契约**：必须保留 `id` (int) 和 `screen_name` (str) 两个 key。`core/subscription_cli.py:325-326` 写的是：

```python
uid = u.get("id")
uname = u.get("screen_name", name)
```

所以新的 dict 不能改名（旧版返回的 user 对象字段更多但实际只用这两个）。

### 4.2 顺带改语法陷阱（同 PR）

`platforms/weibo/api.py:136`：

```diff
-    except ValueError, OSError:
+    except (ValueError, OSError):
```

理由：

- Python 2 风格语法，靠 3.14 容错解析通过；后续如果工具链升级或加 ruff 规则会立刻翻车
- 改动 1 字符，零风险

### 4.3 改测试（同时复现 + 验证）

`tests/test_weibo_api.py:280-356` 整段 `TestSearchUserByName` 重写。

#### 4.3.1 新增「真实 HTML 样本」常量

从服务器 `/tmp/sweibo.html` 提取一段最小样本（约 20 行）保存为测试常量，避免依赖网络。

#### 4.3.2 测试用例

```python
# tests/test_weibo_api.py

# 注意: 文件顶部需 ``import json``（用于 _mock_response 里构造 JSONDecodeError）。
# 其他 import: aiohttp, pytest, AsyncMock/MagicMock/patch。

# 真实抓取样本（已脱敏，2026-06-28 抓自服务器）
_SAMPLE_SEARCH_HTML = """
<!DOCTYPE html>
<html><body>
  <div class="card-wrap">
    <a href="//weibo.com/u/2803301701" class="name" target="_blank">人民日报</a>
    <p class="info">...简介...</p>
  </div>
  <div class="card-wrap">
    <a href="//weibo.com/u/1411163204" class="name" target="_blank">人民日报健康客户端</a>
  </div>
  <div class="card-wrap">
    <a href="//weibo.com/u/5703735355" class="name" target="_blank">人民日报体育</a>
  </div>
  <a href="//weibo.com/u/2803301701" class="name">人民日报</a>  <!-- 重复出现，应去重 -->
</body></html>
"""


class TestSearchUserByName:
    def _mock_response(self, status: int, body: str) -> MagicMock:
        """构造 mock 响应。

        显式把 ``.json`` 设为抛 ``json.JSONDecodeError`` 的 AsyncMock，
        以便 RED 阶段（旧实现走 ``resp.json()``）能稳定触发解码异常，
        而不是落到 MagicMock 默认行为导致的隐晦 ``TypeError``。
        """
        mock_resp = MagicMock()
        mock_resp.status = status
        mock_resp.text = AsyncMock(return_value=body)
        mock_resp.json = AsyncMock(
            side_effect=json.JSONDecodeError("Expecting value", body, 0)
        )
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=None)
        return mock_resp

    def _setup_mocks(self, mock_resp: MagicMock) -> tuple[MagicMock, MagicMock]:
        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_cls = MagicMock()
        mock_cls.return_value.__aenter__.return_value = mock_session
        return mock_cls, mock_session

    @pytest.mark.asyncio
    async def test_returns_matching_users_from_html(self):
        """TDD-RED: 旧实现走 JSON 解析（resp.json()）。

        由于 ``_mock_response`` 显式把 ``.json`` 设为抛 ``JSONDecodeError``，
        旧实现调用 ``await resp.json()`` 时直接抛 ``json.JSONDecodeError``，
        测试失败，错误形式直观可读（不是隐晦的 MagicMock TypeError）。
        """
        mock_resp = self._mock_response(200, _SAMPLE_SEARCH_HTML)
        mock_cls, _session = self._setup_mocks(mock_resp)

        with patch("platforms.weibo.api.aiohttp.ClientSession", mock_cls):
            users = await search_user_by_name("cookie", "人民日报")

        assert len(users) == 3  # 不是 4（重复 uid 去重）
        assert users[0]["id"] == 2803301701
        assert users[0]["screen_name"] == "人民日报"
        assert users[1]["id"] == 1411163204
        assert users[2]["screen_name"] == "人民日报体育"

    @pytest.mark.asyncio
    async def test_returns_empty_on_no_match(self):
        """页面正常返回但没有任何匹配项 → 空 list。"""
        mock_resp = self._mock_response(200, "<html><body>无结果</body></html>")
        mock_cls, _session = self._setup_mocks(mock_resp)

        with patch("platforms.weibo.api.aiohttp.ClientSession", mock_cls):
            users = await search_user_by_name("cookie", "不存在的用户名XYZ")

        assert users == []

    @pytest.mark.asyncio
    async def test_returns_empty_on_404(self):
        """服务端 404（与当前 bug 同症状）→ 空 list + warning log。"""
        mock_resp = self._mock_response(404, "<html>404</html>")
        mock_cls, _session = self._setup_mocks(mock_resp)

        with patch("platforms.weibo.api.aiohttp.ClientSession", mock_cls):
            users = await search_user_by_name("cookie", "test")

        assert users == []

    @pytest.mark.asyncio
    async def test_returns_empty_on_request_exception(self):
        """网络异常 → 空 list + exception log。

        注意: 这是「兜底回归」测试，验证 ``try/except Exception`` 兜住任何异常并返回 []。
        不依赖具体的异常类型——只要异常从 ``session.get`` 路径抛出，实现都应该返回 []。

        这里把异常挂在 ``session.get`` 上（更接近真实网络层抛 ClientError 的语义），
        而不是 ``__aenter__``，因为实现写的是
        ``try: async with session.get(...) as resp:``——异常既可能从 ``get()``
        也可能从 ``__aenter__`` 抛，但挂在 ``get`` 上语义更直白。
        """
        mock_session = MagicMock()
        mock_session.get = MagicMock(side_effect=aiohttp.ClientError("boom"))
        mock_cls = MagicMock()
        mock_cls.return_value.__aenter__.return_value = mock_session

        with patch("platforms.weibo.api.aiohttp.ClientSession", mock_cls):
            users = await search_user_by_name("cookie", "test")

        assert users == []

    @pytest.mark.asyncio
    async def test_returns_empty_on_risk_control_page(self):
        """风控场景: s.weibo.com 返回 200，但页面是验证/跳转页（无 s.weibo.com 自身标记、无 $CONFIG）。

        必须与「正常但 0 结果」区分：当前实现先做页面有效性校验后 return []，
        避免把风控页错误地解析成「正常但 0 结果」。
        """
        risk_html = (
            "<!DOCTYPE html><html><head><title>验证码</title></head>"
            "<body><script>location.href='https://passport.weibo.com/...'</script>"
            "</body></html>"
        )
        mock_resp = self._mock_response(200, risk_html)
        mock_cls, _session = self._setup_mocks(mock_resp)

        with patch("platforms.weibo.api.aiohttp.ClientSession", mock_cls):
            users = await search_user_by_name("cookie", "test")

        assert users == []

    @pytest.mark.asyncio
    async def test_uses_pc_search_endpoint(self):
        """回归测试：断言请求走 s.weibo.com/user 而非 m.weibo.cn suggestion。"""
        mock_resp = self._mock_response(200, _SAMPLE_SEARCH_HTML)
        mock_cls, mock_session = self._setup_mocks(mock_resp)

        with patch("platforms.weibo.api.aiohttp.ClientSession", mock_cls):
            await search_user_by_name("cookie", "人民日报")

        called_url = mock_session.get.call_args[0][0]
        assert called_url.startswith("https://s.weibo.com/user"), (
            f"必须走 s.weibo.com PC 网页搜索，当前 URL: {called_url}"
        )
        assert "q=%E4%BA%BA%E6%B0%91%E6%97%A5%E6%8A%A5" in called_url  # urlencoded 人民日报
```

**关键**：新增的 `test_uses_pc_search_endpoint` 是回归防线——即使将来有人误改回 m 端 API，测试会立刻 fail。

---

## 5. TDD 步骤

按 TDD：先写一个能复现 bug 的失败测试，再改实现。

### Step 1：RED — 先写测试，不改实现

```bash
# 在 tests/test_weibo_api.py 末尾追加 TestSearchUserByName 的重写版（见 4.3.2）
# 旧实现走 data.get("data").get("cards") JSON 路径，
# 新测试 mock 的 body 是 HTML。
# 关键点: _mock_response 中显式把 mock_resp.json 设为 AsyncMock(side_effect=json.JSONDecodeError(...)),
# 旧实现调用 await resp.json() 时会抛 JSONDecodeError；旧实现随后会进入
# `for card in cards`（cards 是 MagicMock）并抛
# `TypeError: object of type 'MagicMock' is not iterable`。
# 显式 mock .json 让 RED 错误更直观（JSONDecodeError 而非隐晦的 TypeError）。
uv run pytest tests/test_weibo_api.py::TestSearchUserByName -x
# 期望：test_returns_matching_users_from_html 失败
# 错误形式: json.JSONDecodeError（由我们显式 mock 的 .json side_effect 抛出）
#         或 TypeError: 'MagicMock' object is not iterable（若未 mock .json 的 fallback 路径）
```

### Step 2：GREEN — 改实现

按 4.1 改 `platforms/weibo/api.py`。

```bash
uv run pytest tests/test_weibo_api.py::TestSearchUserByName -v
# 期望：5 个用例全绿（含风控页 + PC 端点回归）
```

### Step 3：顺手改语法陷阱

按 4.2 改 `except (ValueError, OSError):`，跑全量：

```bash
uv run pytest tests/test_weibo_api.py -v
```

### Step 4：REFACTOR — 检查

```bash
uv run ruff check platforms/weibo/api.py tests/test_weibo_api.py
uv run ruff format platforms/weibo/api.py tests/test_weibo_api.py
uv run pyright  # 无参数！见 AGENTS.md
```

---

## 6. 验证步骤（手动触发）

### 6.1 本地单元测试

```bash
uv run pytest tests/test_weibo_api.py -v
uv run pytest tests/test_subscription_cli.py -v  # _search_weibo 端到端 mock
```

### 6.2 真机验证（部署后）

```bash
ssh usa
docker exec trawler python3 -c "
import asyncio
from platforms.weibo.api import search_user_by_name
from shared.config import load_config

async def main():
    cfg = await load_config('config/config.toml')
    cookie = cfg.weibo.auth.cookie
    for name in ['人民日报', '央视新闻', '新浪科技']:
        users = await search_user_by_name(cookie, name)
        print(f'[{name}] {len(users)} 个结果')
        for u in users[:3]:
            print(f'  - {u[\"id\"]} {u[\"screen_name\"]}')

asyncio.run(main())
"
```

期望输出（修复前：全部 0）：

```
[人民日报] 20 个结果
  - 2803301701 人民日报
  - 1411163204 人民日报健康客户端
  - 5703735355 人民日报体育
[央视新闻] ...
[新浪科技] ...
```

### 6.3 Web UI 验证

部署后访问 `https://<host>/subscriptions`，在微博卡片搜索「人民日报」，应看到候选列表（含 + 添加 按钮）。

### 6.4 日志验证

搜索发起后，服务器日志应能看到（新加的 warning 只在失败时出现，成功无日志；可选用 `web/routes/subscriptions.py:87` 现有的 `logger.info("📋 Web 搜索: %s / %s", platform, name)` 验证）：

```bash
ssh usa 'docker exec trawler grep "Web 搜索: weibo" /app/data/trawler.log | tail -5'
```

---

## 7. 任务列表

| # | 任务 | 文件 | 依赖 |
|---|---|---|---|
| T1 | TDD-RED：重写 `TestSearchUserByName`（用 HTML mock + 显式 `.json` JSONDecodeError）+ 新增 `test_uses_pc_search_endpoint` + `test_returns_empty_on_risk_control_page` 回归用例 | `tests/test_weibo_api.py` | - |
| T2 | 验证 RED：`uv run pytest tests/test_weibo_api.py::TestSearchUserByName -x` 失败 | - | T1 |
| T3 | GREEN：替换 `MOBILE_USER_SEARCH_API` 为 `PC_USER_SEARCH_API`，重写 `search_user_by_name` 走 HTML 正则 | `platforms/weibo/api.py` | T2 |
| T4 | 顺手修语法陷阱：`except (ValueError, OSError):` | `platforms/weibo/api.py:136` | T3 |
| T5 | 验证 GREEN：`uv run pytest tests/test_weibo_api.py -v` 全绿 | - | T3, T4 |
| T6 | lint / format / pyright：`uv run ruff check .`、`uv run ruff format .`、`uv run pyright` | - | T5 |
| T7 | 真机验证（6.2 + 6.3 + 6.4） | - | T6 部署后 |

---

## 8. 风险

| 风险 | 概率 | 影响 | 缓解 |
|---|---|---|---|
| s.weibo.com HTML 结构变更（class/name 等属性） | 中 | 搜索结果解析返回空 | 正则宽松匹配 `class="name"`（HTML 里搜索结果固定用这个 class），加 `test_uses_pc_search_endpoint` 回归断言 URL |
| 微博将来下线 s.weibo.com PC 搜索页 | 低 | 整个搜索失效 | 当前是官方唯一可用入口，无 fallback；下线时再调研 |
| 服务器部署后搜索结果中含大量同名的普通用户（噪声） | 中 | UI 列表很长 | 当前不去重 name 只去重 uid；可后续在 `_search_weibo` 里加「精确匹配优先」排序 |
| 正则匹配到非搜索结果的 `<a class="name">` | 低 | 多返回几条 | 已通过 `href="//weibo.com/u/{uid}"` 锚定，且页面里 `class="name"` 几乎只用于用户搜索结果 |
| `int(uid)` 在异常 uid 上崩溃 | 极低 | 单次搜索失败 | uid 一定是纯数字（regex 已限制 `\d+`）；保守起见可在循环内 try/except，但当前样本不需要 |

---

## 9. 决策点（如有）

**`decisions_for_user`: 无**

根因唯一（API 端点下线），修复方案唯一（PC 网页搜索是当前唯一可用入口）。所有其他候选端点在真机上都已经验证失败（见 §3 表格），没有取舍空间。

唯一可选项是「是否顺带改 `except ValueError, OSError:` 语法陷阱」（4.2），但这是 1 字符零风险改动，默认带上。

---

## 10. 日志与可见性（Issue 5 取舍）

### 10.1 现状

按项目规范（AGENTS.md）：用户可见输出用 `console.print`，调试用 `logging.getLogger(__name__)`。

`search_user_by_name` 当前（修复后）的设计是：

| 失败原因 | 处理 | 用户能否看到根因 |
|---|---|---|
| HTTP 状态码 != 200 | `logger.warning` + `return []` | ❌ 只进日志 |
| 风控验证页（200 但无 $CONFIG） | `logger.warning` + `return []` | ❌ 只进日志 |
| 网络异常 | `logger.exception` + `return []` | ❌ 只进日志 |
| 正常但 0 结果 | 直接 `return []` | ✅ 合理 |

调用链上层 `core/subscription_cli.py:_search_weibo` 在收到空列表时统一返回：

```python
return False, f"未找到名为「{name}」的用户", []
```

这条消息会通过 `web/templates/_candidates.html` 显示为前端 toast。**用户看到的根因信息丢失**——无法区分「API 下线 / 风控 / 真的没结果」。

### 10.2 取舍方案

**本 PR 选择：保持现状（失败原因只进日志）**，理由：

1. `search_user_by_name` 是 platforms 层（纯适配层），不应该直接调 `console.print`——console 是 UI 关注点，platforms 层不应依赖 Rich
2. 「未找到」的通用消息对终端用户是可接受的（他们不需要知道是 404 还是风控）
3. 根因对运维/开发者可见（服务器日志），通过 `docker logs` 或 `trawler.log` 可定位
4. 翻译失败原因到具体用户消息属于 UX 改进，应该在 `core/subscription_cli.py:_search_weibo` 层做（让它读 log 或新增返回字段），不在本 bugfix 范围

**未来改进（不在本 PR）**：

- 让 `search_user_by_name` 返回 `(users, reason)` 元组，`_search_weibo` 根据 `reason` 翻译成更具体的 UI 消息（如「微博搜索暂时不可用，请稍后再试」/「未找到」/「触发风控，请稍后再试」）
- 或在 `_search_weibo` 里订阅 log（过度设计，不推荐）

### 10.3 验证 log 确实有写

`platforms/weibo/api.py` 顶部已有 `logger = logging.getLogger(__name__)`。新增的三条 warning/exception 都会进 `trawler.log`。`docs/superpowers/plans/...` §6.4 已给出验证命令：

```bash
ssh usa 'docker exec trawler grep "微博.*搜索" /app/data/trawler.log | tail -10'
```

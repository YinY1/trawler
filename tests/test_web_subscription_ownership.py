from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from core.subscription_cli import (
    assign_token_to_subscription as _real_assign,
)
from core.subscription_cli import (
    set_subscription_owner as _real_set_owner,
)
from core.subscription_cli import (
    unassign_token_from_subscription as _real_unassign,
)
from web.app import create_app
from web.auth import set_password

PASSWORD = "test12345"


@pytest.fixture
async def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncClient:
    monkeypatch.setattr("web.auth.AUTH_TOML_PATH", tmp_path / "auth.toml")
    set_password(PASSWORD)
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.post("/login", data={"password": PASSWORD}, follow_redirects=False)
        assert resp.status_code == 303
        yield c


class TestOwnershipModal:
    @patch("web.routes.subscription_ownership.load_subscriptions")
    @patch("web.routes.subscription_ownership.load_auth_config")
    async def test_modal_returns_html(self, mock_auth, mock_subs, client: AsyncClient) -> None:
        from shared.config import ApiTokenEntry, WebAuthConfig

        mock_auth.return_value = WebAuthConfig(
            api_tokens=[
                ApiTokenEntry(name="admin-token", token_hash="aaa", scopes=["tokens:manage"]),
                ApiTokenEntry(name="viewer-token", token_hash="bbb", scopes=["subscriptions:read"]),
            ]
        )
        # `load_subscriptions` returns a list of subscription dicts
        mock_subs.return_value = {
            "bilibili": [{"uid": 1, "name": "UP主", "owner_token": "admin-token", "assigned_tokens": ["viewer-token"]}]
        }
        resp = await client.get("/subscriptions/bili/1/ownership")
        assert resp.status_code == 200
        assert "admin-token" in resp.text  # current owner in dropdown
        assert "viewer-token" in resp.text  # assigned token shown
        assert "UP主" in resp.text

    @patch("web.routes.subscription_ownership.load_auth_config")
    @patch("web.routes.subscription_ownership.load_subscriptions")
    async def test_modal_orphan_shows_no_owner(self, mock_subs, mock_auth, client: AsyncClient) -> None:
        from shared.config import ApiTokenEntry, WebAuthConfig

        mock_auth.return_value = WebAuthConfig(
            api_tokens=[
                ApiTokenEntry(name="admin-token", token_hash="aaa", scopes=["tokens:manage"]),
            ]
        )
        mock_subs.return_value = {"bilibili": [{"uid": 2, "name": "孤儿UP"}]}
        resp = await client.get("/subscriptions/bili/2/ownership")
        assert resp.status_code == 200
        assert "孤儿" in resp.text or "无" in resp.text


class TestTokenAssign:
    @patch("web.routes.subscription_ownership.assign_token_to_subscription", new_callable=AsyncMock)
    async def test_assign_success(self, mock_assign, client: AsyncClient) -> None:
        mock_assign.return_value = (True, "已分配: viewer-token")
        resp = await client.post(
            "/subscriptions/bili/1/assign",
            data={"token_name": "viewer-token"},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert resp.status_code == 303
        loc = resp.headers["location"]
        assert "toast_key=token.assigned" in loc
        assert "type=success" in loc

    @patch("web.routes.subscription_ownership.assign_token_to_subscription", new_callable=AsyncMock)
    async def test_assign_failure(self, mock_assign, client: AsyncClient) -> None:
        mock_assign.return_value = (False, "未知 token: bad")
        resp = await client.post(
            "/subscriptions/bili/1/assign",
            data={"token_name": "bad"},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert resp.status_code == 303
        loc = resp.headers["location"]
        assert "toast_key=token.assign_failed" in loc
        assert "type=error" in loc

    @patch("web.routes.subscription_ownership.unassign_token_from_subscription", new_callable=AsyncMock)
    async def test_unassign_success(self, mock_unassign, client: AsyncClient) -> None:
        mock_unassign.return_value = (True, "已解绑: viewer-token")
        resp = await client.post(
            "/subscriptions/bili/1/unassign",
            data={"token_name": "viewer-token"},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert resp.status_code == 303
        loc = resp.headers["location"]
        assert "toast_key=token.assigned" in loc  # 复用 success key
        assert "type=success" in loc

    @patch("web.routes.subscription_ownership.unassign_token_from_subscription", new_callable=AsyncMock)
    async def test_unassign_failure(self, mock_unassign, client: AsyncClient) -> None:
        mock_unassign.return_value = (False, "未找到订阅")
        resp = await client.post(
            "/subscriptions/bili/1/unassign",
            data={"token_name": "viewer-token"},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert resp.status_code == 303
        loc = resp.headers["location"]
        assert "toast_key=token.assign_failed" in loc
        assert "type=error" in loc


class TestOwnerSet:
    @patch("web.routes.subscription_ownership.set_subscription_owner", new_callable=AsyncMock)
    async def test_set_owner_success(self, mock_set, client: AsyncClient) -> None:
        mock_set.return_value = (True, "已设置 owner: admin-token")
        resp = await client.post(
            "/subscriptions/bili/1/owner",
            data={"owner_token": "admin-token"},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert resp.status_code == 303
        loc = resp.headers["location"]
        assert "toast_key=token.owner_set" in loc
        assert "type=success" in loc

    @patch("web.routes.subscription_ownership.set_subscription_owner", new_callable=AsyncMock)
    async def test_set_owner_failure(self, mock_set, client: AsyncClient) -> None:
        mock_set.return_value = (False, "未找到订阅")
        resp = await client.post(
            "/subscriptions/bili/999/owner",
            data={"owner_token": "admin-token"},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert resp.status_code == 303
        loc = resp.headers["location"]
        assert "toast_key=token.owner_failed" in loc
        assert "type=error" in loc


# ═══════════════════════════════════════════════════════════
# Integration tests — 真实 core 函数 + tmp TOML（防 false-green）
# ═══════════════════════════════════════════════════════════
#
# 历史 bug：Task 7/8 路由层用 `_platform_key_to_name(platform)` 把 `bili`
# 转成 `bilibili` 后再传给 core，但 core 内部又用 `PLATFORM_TO_SECTION`
# 做一次转换，结果收到 `"bilibili"` 不在 `VALID_PLATFORMS` 里 → 永远返回
# `(False, "无效平台: bilibili")`，静默失败、不写盘。
#
# 上面的 mock 测试全绿但发现不了这个 bug。本类不 mock core，验证真实
# 写盘路径：路由必须传短名（`bili`）给 core。
# ===================================================================


class TestAssignIntegration:
    """不 mock core 函数，验证路由层把正确的短名 platform 传给 core。"""

    @pytest.fixture
    def subs_and_auth(self, client: AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        """写盘 tmp subscriptions.toml，往已存在的 auth.toml 加一个 token。

        ``client`` fixture 已经 set_password + 登录拿到 session cookie，
        所以这里**不能**重写 auth.toml（会轮转 session_secret 让 cookie 失效）。
        用 ``load_auth_config`` / ``save_auth_config`` 追加 token。

        patch 路由层 import 的 core 函数为「真实函数 + tmp path」包装器。

        返回 subs_path 用于断言落盘。
        """
        # 1. subscriptions.toml — 一条 bili uid=1 sub
        subs_path = tmp_path / "subscriptions.toml"
        subs_path.write_text(
            '[[bilibili.subscriptions]]\nuid = 1\nname = "UP主"\n',
            encoding="utf-8",
        )
        # 2. 往 client 已设置的 auth.toml 追加 token 'real-token'（core 校验存在性）
        #    不重写文件 → 不动 session_secret → session cookie 仍有效。
        from shared.config import ApiTokenEntry
        from web.auth import load_auth_config, save_auth_config

        cfg = load_auth_config()
        cfg.api_tokens.append(ApiTokenEntry(name="real-token", token_hash="a" * 64, scopes=[]))
        save_auth_config(cfg)

        # 3. 包装路由层 import 的 core 函数：注入 tmp path，其余走真代码。
        #    这样路由层对 platform 参数的任何错误处理都会被真实 core 的
        #    VALID_PLATFORMS 校验抓到（即本 bug 的检测点）。
        async def assign_wrapper(platform: str, identifier: int | str, token_name: str) -> tuple[bool, str]:
            return await _real_assign(platform, identifier, token_name, path=str(subs_path))

        async def unassign_wrapper(platform: str, identifier: int | str, token_name: str) -> tuple[bool, str]:
            return await _real_unassign(platform, identifier, token_name, path=str(subs_path))

        async def set_owner_wrapper(
            platform: str, identifier: int | str, owner_token: str
        ) -> tuple[bool, str]:
            return await _real_set_owner(platform, identifier, owner_token, path=str(subs_path))

        monkeypatch.setattr(
            "web.routes.subscription_ownership.assign_token_to_subscription",
            assign_wrapper,
        )
        monkeypatch.setattr(
            "web.routes.subscription_ownership.unassign_token_from_subscription",
            unassign_wrapper,
        )
        monkeypatch.setattr(
            "web.routes.subscription_ownership.set_subscription_owner",
            set_owner_wrapper,
        )
        return subs_path

    async def test_assign_real_writes_toml(self, client: AsyncClient, subs_and_auth: Path) -> None:
        """路由传短名 bili → core 成功写盘 assigned_tokens。

        若路由层仍错误地传 bilibili（double-convert），core 会返回
        (False, "无效平台: bilibili")，本断言会 fail。
        """
        resp = await client.post(
            "/subscriptions/bili/1/assign",
            data={"token_name": "real-token"},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert resp.status_code == 303
        loc = resp.headers["location"]
        assert "toast_key=token.assigned" in loc, (
            f"assign 应成功但 toast 显示失败: {loc}（疑似 platform 参数 double-convert）"
        )
        assert "type=success" in loc
        # 落盘验证
        content = subs_and_auth.read_text(encoding="utf-8")
        assert "real-token" in content
        assert "assigned_tokens" in content

    async def test_unassign_real_removes_token(self, client: AsyncClient, subs_and_auth: Path) -> None:
        """先 assign 再 unassign，验证真实写盘 + 删除路径。

        unassign 不校验 token 存在性（core 设计），故只关心 platform 短名
        正确传入即可成功。
        """
        # 先 assign（走真实 core 写盘）
        resp = await client.post(
            "/subscriptions/bili/1/assign",
            data={"token_name": "real-token"},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert "type=success" in resp.headers["location"]
        assert "real-token" in subs_and_auth.read_text(encoding="utf-8")

        # 再 unassign
        resp = await client.post(
            "/subscriptions/bili/1/unassign",
            data={"token_name": "real-token"},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert resp.status_code == 303
        loc = resp.headers["location"]
        assert "toast_key=token.assigned" in loc, (
            f"unassign 应成功但 toast 显示失败: {loc}（疑似 platform 参数 double-convert）"
        )
        assert "type=success" in loc
        # 空列表 → 字段应被移除
        content = subs_and_auth.read_text(encoding="utf-8")
        assert "real-token" not in content


class TestOwnerSetIntegration:
    """不 mock core，验证 set_owner 路由传正确的短名 platform + 真实写盘 owner_token。"""

    @pytest.fixture
    def subs_and_auth(self, client: AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        """复用 TestAssignIntegration 同款 fixture 模式（见上方注释）。"""
        subs_path = tmp_path / "subscriptions.toml"
        subs_path.write_text(
            '[[bilibili.subscriptions]]\nuid = 1\nname = "UP主"\n',
            encoding="utf-8",
        )
        from shared.config import ApiTokenEntry
        from web.auth import load_auth_config, save_auth_config

        cfg = load_auth_config()
        cfg.api_tokens.append(ApiTokenEntry(name="real-token", token_hash="a" * 64, scopes=[]))
        save_auth_config(cfg)

        async def set_owner_wrapper(
            platform: str, identifier: int | str, owner_token: str
        ) -> tuple[bool, str]:
            return await _real_set_owner(platform, identifier, owner_token, path=str(subs_path))

        monkeypatch.setattr(
            "web.routes.subscription_ownership.set_subscription_owner",
            set_owner_wrapper,
        )
        return subs_path

    async def test_set_owner_real_writes_toml(self, client: AsyncClient, subs_and_auth: Path) -> None:
        """路由传短名 bili → core 成功写盘 owner_token 字段。

        若路由层错误地用 _platform_key_to_name(platform) 传 bilibili，
        core 会返回 (False, "无效平台: bilibili")，本断言 fail。
        """
        resp = await client.post(
            "/subscriptions/bili/1/owner",
            data={"owner_token": "real-token"},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert resp.status_code == 303
        loc = resp.headers["location"]
        assert "toast_key=token.owner_set" in loc, (
            f"set_owner 应成功但 toast 显示失败: {loc}（疑似 platform 参数 double-convert）"
        )
        assert "type=success" in loc
        # 落盘验证：owner_token 字段写入 real-token
        content = subs_and_auth.read_text(encoding="utf-8")
        assert 'owner_token = "real-token"' in content, (
            f"owner_token 未落盘: {content}"
        )

    async def test_set_owner_real_replaces_existing(
        self, client: AsyncClient, subs_and_auth: Path
    ) -> None:
        """set（非 add）语义：已有 owner 应被覆盖。"""
        # 第一次设置
        resp = await client.post(
            "/subscriptions/bili/1/owner",
            data={"owner_token": "real-token"},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert "type=success" in resp.headers["location"]

        # 再次设置同一个（idempotent）— core 仍返回 success
        resp = await client.post(
            "/subscriptions/bili/1/owner",
            data={"owner_token": "real-token"},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert resp.status_code == 303
        loc = resp.headers["location"]
        assert "toast_key=token.owner_set" in loc
        assert "type=success" in loc

        content = subs_and_auth.read_text(encoding="utf-8")
        # 只有一个 owner_token 字段（replace 不是 append）
        assert content.count("owner_token") == 1

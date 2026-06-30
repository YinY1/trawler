"""Tests for GET /api/health (issue #55).

无需登录，返回 {status, version, git_sha, build_date}.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from web.app import create_app
from web.auth import set_password

PASSWORD = "test12345"


@pytest.fixture
async def health_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncClient:
    """无需登录的 client —— /api/health 必须在 setup/login guard 之外。

    即便如此，仍需 set_password 让 is_setup_complete() 返回 True，
    否则 auth_guard 会 302 到 /setup.
    """
    monkeypatch.setattr("web.auth.AUTH_TOML_PATH", tmp_path / "auth.toml")
    set_password(PASSWORD)
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


class TestHealth:
    async def test_health_returns_200_without_login(self, health_client: AsyncClient) -> None:
        resp = await health_client.get("/api/health")
        assert resp.status_code == 200

    async def test_health_response_shape(self, health_client: AsyncClient) -> None:
        resp = await health_client.get("/api/health")
        data = resp.json()
        assert data["status"] == "ok"
        # version 字段存在且为非空字符串
        assert isinstance(data["version"], str) and data["version"]
        assert "git_sha" in data
        assert "build_date" in data

    async def test_health_version_matches_constant(self, health_client: AsyncClient) -> None:
        from shared.constants import VERSION

        resp = await health_client.get("/api/health")
        assert resp.json()["version"] == VERSION

    async def test_health_git_sha_matches_constant(self, health_client: AsyncClient) -> None:
        from shared.constants import GIT_SHA

        resp = await health_client.get("/api/health")
        assert resp.json()["git_sha"] == GIT_SHA

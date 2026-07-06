"""Pydantic 请求/响应模型（API v1）。

仅在需要约束 schema 的端点使用；简单端点可直接返回 dict（FastAPI 会 JSON 化）。
后续 task（T2-T4）在此追加 ``CheckRunRequest`` / ``MessageOut`` 等。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel


class ApiResponse(BaseModel):
    """统一响应包装（可选，简单端点直接返回 dict 也可）。"""

    status: str
    data: dict[str, Any] | None = None


class ErrorDetail(BaseModel):
    """错误响应统一 shape（与 FastAPI 默认 ``{"detail": ...}`` 对齐）。"""

    detail: str | list[Any]


# ═══════════════════════════════════════════════════════════
# T2: check 系列端点
# ═══════════════════════════════════════════════════════════


class CheckRunRequest(BaseModel):
    """``POST /check/run`` 请求体。

    - ``mode="full"``：走 ``run_check_once``（detector + cleanup），
      ``platform`` 作为平台过滤器。
    - ``mode="manual"``：走 ``PipelineEngine.run_specific_messages``，
      必须携带 ``since``/``title``/``author``/``reset_phase`` 中至少一项，
      否则路由返回 422。
    """

    mode: Literal["full", "manual"] = "full"
    platform: str = "all"
    since: str | None = None
    title: str | None = None
    author: str | None = None
    reset_phase: str | None = None
    skip_push: bool = True


class CheckRunResponse(BaseModel):
    """``POST /check/run`` 成功响应（202）。"""

    status: str
    task_id: str | None = None
    mode: str | None = None


class CheckStatusResponse(BaseModel):
    """``GET /check/status`` 当前 run 状态快照。

    ``log_history`` 内的 ``_ts`` 内部字段在序列化前由路由 strip 掉，
    客户端不会看到该实现细节。
    """

    running: bool
    processed_count: int
    started_at: float | None
    log_history: list[dict[str, Any]]


# ═══════════════════════════════════════════════════════════
# T3: messages 系列端点
# ═══════════════════════════════════════════════════════════


class MessageOut(BaseModel):
    """单条消息响应模型（从 ``MessageRecord`` 转）。

    ``content_type`` / ``phase`` 序列化为枚举名（``ContentType.VIDEO.name == "VIDEO"``），
    而非 enum 值（``auto()`` 分配的整数）。客户端按字符串名比对，与 Phase
    枚举在 ``Phase["SUMMARIZED"]`` 反查时使用的 key 一致。
    """

    msg_id: str
    platform: str
    content_type: str
    phase: str
    pubdate: int
    title: str
    author: str
    created_at: float = 0.0
    updated_at: float = 0.0
    error: str = ""
    dynamic_text: str = ""
    subscription_ref: str = ""
    xsec_token: str = ""
    body: str = ""
    summary: str = ""
    retry_count: int = 0
    last_error: str = ""
    permanent_error: bool = False


class MessageListResponse(BaseModel):
    """``GET /messages`` 响应。"""

    messages: list[MessageOut]
    count: int


class RerunRequest(BaseModel):
    """``POST /messages/rerun`` 请求体。

    - ``msg_ids`` 必须非空（``min_items=1``）
    - ``from_phase`` 默认 ``summarized``（与 CLI/Web UI 行为一致）
    - ``skip_push`` 默认 ``True``（手动重跑禁止重推，避免重复打扰订阅者）
    """

    msg_ids: list[str]
    from_phase: str = "summarized"
    skip_push: bool = True


class RerunResponse(BaseModel):
    """``POST /messages/rerun`` 成功响应（202）。"""

    status: str
    task_id: str | None = None
    reset_count: int | None = None


class FetchRequest(BaseModel):
    """``POST /messages/fetch`` 请求体（issue #101）。

    - ``msg_ids`` 必须非空
    - ``skip_push`` 默认 ``False``（与 ``RerunRequest`` 默认 ``True`` 相反，
      按需入口语义是"处理新消息"，应当推送）
    """

    msg_ids: list[str]
    skip_push: bool = False


class FetchResponse(BaseModel):
    """``POST /messages/fetch`` 成功响应（202）。

    ``fetch_count`` 恒为 ``None``：抓取是异步的，202 响应提交时尚未跑完。
    实际抓取数通过 SSE ``done`` 事件推送（见路由 docstring）。
    字段保留是为与 ``RerunResponse`` 对称、未来如需同步模式可填。
    """

    status: str
    task_id: str | None = None
    fetch_count: int | None = None  # 恒 None（异步），保留为对称字段


# ═══════════════════════════════════════════════════════════
# T4: subscriptions 系列端点
# ═══════════════════════════════════════════════════════════


class SubscriptionListResponse(BaseModel):
    """``GET /subscriptions`` 响应。

    透传 ``list_subscriptions`` 的原始 dict（section → list[entry]），
    entry 结构由 ``config/subscriptions.toml`` 决定（uid/user_id + name + 可选 notify_endpoints）。
    """

    platforms: dict[str, list[dict[str, Any]]]


class SubscriptionAddRequest(BaseModel):
    """``POST /subscriptions`` 请求体。

    ``identifier`` 在 API 层统一为 str，``add_subscription`` 内部按平台转 int/str。
    ``default_notify_endpoint`` 可选，传入时会在添加订阅后绑定该 endpoint；
    endpoint 不存在时回滚订阅添加，返回 ``success=False``。
    """

    platform: str
    identifier: str
    name: str
    default_notify_endpoint: str | None = None


class SubscriptionAddResponse(BaseModel):
    """``POST /subscriptions`` 响应（成功 / 业务失败共用，见 ``success``）。"""

    success: bool
    message: str


class SubscriptionRemoveResponse(BaseModel):
    """``DELETE /subscriptions/{platform}/{identifier}`` 响应。"""

    success: bool
    message: str


class EndpointBindRequest(BaseModel):
    """``POST /subscriptions/{platform}/{identifier}/endpoints`` 请求体。

    仅一个字段 —— ``endpoint_name``。响应复用 ``SubscriptionAddResponse``，
    不为 endpoint 端点新建 schema（YAGNI，spec §4.4）。
    """

    endpoint_name: str

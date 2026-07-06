"""Trawler 异常层次结构 + 异步重试工具

设计原则：
- ``TrawlerError`` 为所有异常的基类
- 按失败性质分层：配置/认证/网络/数据
- 调用方可按需捕获特定粒度
"""

from __future__ import annotations

import asyncio
from functools import wraps
from typing import Callable, ParamSpec, TypeVar, cast

P = ParamSpec("P")
R = TypeVar("R")


# ═══════════════════════════════════════════════════════════
# 异常层次
# ═══════════════════════════════════════════════════════════


class TrawlerError(Exception):
    """所有 trawler 异常的基类"""


class ConfigError(TrawlerError):
    """配置错误（文件缺失、格式错误、字段无效）"""


class AuthError(TrawlerError):
    """认证失败（凭证过期、登录态失效、权限不足）"""


class HttpError(TrawlerError):
    """HTTP 请求失败基类"""

    def __init__(self, message: str, status_code: int = 0) -> None:
        self.status_code = status_code
        super().__init__(message)


class HttpStatusError(HttpError):
    """非预期的 HTTP 状态码（4xx/5xx）"""


class HttpTimeoutError(HttpError):
    """请求超时"""


class DataError(TrawlerError):
    """数据解析/结构异常"""


class NotFoundError(DataError):
    """资源不存在（视频/笔记/帖子已被删除）"""


class IpBlockError(TrawlerError):
    """IP 被平台封禁（XHS code 300012 等）"""


class CaptchaError(TrawlerError):
    """触发了平台验证码（HTTP 461/471 等）"""

    def __init__(self, message: str, verify_type: str | None = None, verify_uuid: str | None = None) -> None:
        self.verify_type = verify_type
        self.verify_uuid = verify_uuid
        super().__init__(message)


class RetryableError(TrawlerError):
    """可重试的临时错误（HTTP 403/429/5xx、网络抖动的基类）"""


class PermanentFetchError(TrawlerError):
    """按 ID 抓取永久失败（issue #101）。

    调用方（``run_fetch_and_process``）应明示给用户、不重试、不创建 record。

    典型场景：
    - xhs ``xsec_token`` 缺失导致 server 拒绝（``DataError`` 等价信号）
    - 平台明确返回"资源不存在 / 已删除"
    - ``note_card`` 正文为空（desc/image_list/video 全空）

    与 ``NotFoundError`` 区别：``NotFoundError`` 是数据层"资源不存在"，
    ``PermanentFetchError`` 是抓取层"无法获取"的更宽口径（含 token 缺失等）。
    """


# ── Session 失效检测辅助函数 ──────────────────────────────────────


def is_session_expired_error(exc: BaseException) -> bool:
    """检测异常是否为 XHS -100 session 失效错误。

    XHS 服务端使 session 失效时，底层 xhs 库会抛出 DataFetchError，
    响应数据形如 ``{"code": -100, "msg": "登录已过期"}``。该异常被
    ``_wrap_xhs_call`` 转译为 ``DataError`` 后，错误信息中仍保留
    原始 code 与 msg。

    本函数检查异常类型的字符串表示是否包含特征码 "-100" 或关键词，
    供 monitor 与 scheduler 判断是否需要写回 ``expires_at=0``。
    """
    msg = str(exc)
    return "-100" in msg or "登录已过期" in msg or "登录失效" in msg


# ═══════════════════════════════════════════════════════════
# 异步重试装饰器
# ═══════════════════════════════════════════════════════════


def async_retry(
    max_attempts: int = 3,
    delay: float = 1.0,
    backoff: float = 2.0,
    exceptions: tuple[type[Exception], ...] = (Exception,),
) -> Callable[[Callable[P, asyncio.Future[R]]], Callable[P, asyncio.Future[R]]]:
    """异步函数重试装饰器（指数退避）。

    Args:
        max_attempts: 最大重试次数（默认 3）
        delay: 首次重试延迟秒数（默认 1.0）
        backoff: 每次重试延迟倍率（默认 2.0）
        exceptions: 需要重试的异常类型元组（默认所有 Exception）

    Usage::

        @async_retry(max_attempts=3, delay=0.5, exceptions=(HttpTimeoutError,))
        async def fetch_data(url: str) -> dict:
            ...
    """

    def decorator(
        func: Callable[P, asyncio.Future[R]],
    ) -> Callable[P, asyncio.Future[R]]:
        @wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            last_exc: Exception | None = None
            current_delay = delay
            for attempt in range(max_attempts):
                try:
                    return await func(*args, **kwargs)
                except exceptions as e:
                    last_exc = e
                    if attempt < max_attempts - 1:
                        await asyncio.sleep(current_delay)
                        current_delay *= backoff
            # All attempts exhausted
            assert last_exc is not None  # help pyright narrow type
            raise last_exc

        return cast("Callable[P, asyncio.Future[R]]", wrapper)

    return decorator

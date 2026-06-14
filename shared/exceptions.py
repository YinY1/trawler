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

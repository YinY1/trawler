"""并发回归测试 — 验证 run_check_once 并发执行多平台时 MessageStore 数据完整。

背景：``run_check_once(platform="all")`` 改为 ``asyncio.gather`` 并发后，
三个平台共享同一个 MessageStore 实例。本测试构造两个 mock 平台，让它们
各自的 detector 添加消息、各自 handler 推进阶段，最终断言：
1. 两个平台的消息都正确写入磁盘
2. 阶段都推进到 PUSHED（没有互相覆盖）

如果回归到"各自创建独立 MessageStore 实例"的写法，本测试会失败：
后 save 的实例会用过期的内存快照覆盖前面的写入。
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest

from core.engine import PipelineEngine
from core.pipeline import PLATFORM_REGISTRY, PlatformDef, run_check_once
from shared.config import Config
from shared.message_store import MessageStore
from shared.protocols import ContentType, Phase, PhaseContext


@pytest.fixture(autouse=True)
def clean_engine() -> None:
    PipelineEngine._handlers = {}
    PipelineEngine._detectors = {}


@pytest.fixture
def config() -> Config:
    return Config()


@pytest.fixture(autouse=True)
def stub_registry(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """用两个 mock 平台替换真实 PLATFORM_REGISTRY，避免触发真实网络。"""

    # 保留原始注册表内容以便恢复（pipeline 模块持有同一 dict 对象引用）
    original = dict(PLATFORM_REGISTRY)

    # PipelineEngine.run_platform 通过 _HANDLER_MODULES 延迟 import handler；
    # 测试平台没有对应模块，置空以跳过 import
    monkeypatch.setattr(PipelineEngine, "_HANDLER_MODULES", {})

    # 替换全局注册表内容（保留同一 dict 对象，pipeline 模块持有其引用）
    PLATFORM_REGISTRY.clear()
    PLATFORM_REGISTRY.update(
        {
            "alpha": PlatformDef(
                platform_key="alpha",
                auth_name="alpha",
                enabled_check=lambda _: True,
            ),
            "beta": PlatformDef(
                platform_key="beta",
                auth_name="beta",
                enabled_check=lambda _: True,
            ),
        }
    )

    yield

    PLATFORM_REGISTRY.clear()
    PLATFORM_REGISTRY.update(original)


async def test_concurrent_platforms_do_not_lose_data(config: Config, tmp_path: Path) -> None:
    """两个平台并发执行，detector 各加一条消息、handler 各推到 PUSHED。

    若共享 store 实例方案退回到"各自创建独立实例"，后 save 的实例会用
    启动时的内存快照覆盖前面的写入 —— 至少一条消息会丢，phase 会回退。
    """
    # 用一个 gate 让两个 detector 在 beta 进入 await 后再同时继续，
    # 制造真正的并发切换点（而非"第一个跑完才轮到第二个"）
    gate: asyncio.Event = asyncio.Event()

    @PipelineEngine.register_detector("alpha")
    async def alpha_detector(cfg: Config, st: MessageStore) -> None:
        st.add_new("alpha:1", "alpha", ContentType.TEXT, 2000000000, "A1", "Author")
        # 等待 beta 也进入 detector —— 此时两个协程都处于 await
        gate.set()
        await asyncio.sleep(0.01)

    @PipelineEngine.register_detector("beta")
    async def beta_detector(cfg: Config, st: MessageStore) -> None:
        await gate.wait()
        st.add_new("beta:1", "beta", ContentType.TEXT, 2000000000, "B1", "Author")

    for plat in ("alpha", "beta"):

        @PipelineEngine.register(plat, Phase.DOWNLOADED)
        async def _dl(ctx: PhaseContext) -> bool:
            return True

        @PipelineEngine.register(plat, Phase.PUSHED)
        async def _push(ctx: PhaseContext) -> bool:
            return True

    config.general.data_dir = str(tmp_path)

    # 关闭 token 续期网络调用：check_and_renew_tokens 在并发路径里会
    # 被调用，这里直接 monkeypatch 掉
    from shared.auth import scheduler

    async def _noop_renew(*args: object, **kwargs: object) -> None:
        return None

    scheduler.check_and_renew_tokens = _noop_renew  # type: ignore[assignment]

    await run_check_once(config, platform="all")

    # 重新加载 store 验证磁盘状态
    store2 = MessageStore(tmp_path)
    assert store2.is_known("alpha:1"), "alpha 消息丢失 —— 共享 store 方案被破坏"
    assert store2.is_known("beta:1"), "beta 消息丢失 —— 共享 store 方案被破坏"

    alpha_msg = store2.get_message("alpha:1")
    beta_msg = store2.get_message("beta:1")
    assert alpha_msg is not None and alpha_msg.phase == Phase.PUSHED
    assert beta_msg is not None and beta_msg.phase == Phase.PUSHED


async def test_concurrent_platform_run_in_parallel(config: Config, tmp_path: Path) -> None:
    """并发执行时多个平台应真正同时进行，而非串行排队。

    通过让每个 detector 记录开始/结束时间戳，断言两个 detector 的执行
    时间窗口有重叠。串行执行时窗口不重叠。
    """
    timeline: list[tuple[str, float]] = []

    @PipelineEngine.register_detector("alpha")
    async def alpha_detector(cfg: Config, st: MessageStore) -> None:
        timeline.append(("alpha_start", asyncio.get_event_loop().time()))
        st.add_new("alpha:1", "alpha", ContentType.TEXT, 2000000000, "A1", "Author")
        await asyncio.sleep(0.05)  # 模拟 IO
        timeline.append(("alpha_end", asyncio.get_event_loop().time()))

    @PipelineEngine.register_detector("beta")
    async def beta_detector(cfg: Config, st: MessageStore) -> None:
        timeline.append(("beta_start", asyncio.get_event_loop().time()))
        st.add_new("beta:1", "beta", ContentType.TEXT, 2000000000, "B1", "Author")
        await asyncio.sleep(0.05)
        timeline.append(("beta_end", asyncio.get_event_loop().time()))

    for plat in ("alpha", "beta"):

        @PipelineEngine.register(plat, Phase.DOWNLOADED)
        async def _dl(ctx: PhaseContext) -> bool:
            return True

        @PipelineEngine.register(plat, Phase.PUSHED)
        async def _push(ctx: PhaseContext) -> bool:
            return True

    config.general.data_dir = str(tmp_path)

    from shared.auth import scheduler

    async def _noop_renew(*args: object, **kwargs: object) -> None:
        return None

    scheduler.check_and_renew_tokens = _noop_renew  # type: ignore[assignment]

    await run_check_once(config, platform="all")

    # 时间线应有 4 条记录
    assert len(timeline) == 4
    starts = {name for name, _ts in timeline if name.endswith("_start")}
    assert starts == {"alpha_start", "beta_start"}, "两个 detector 应都已启动"

    # 验证并发：两个 start 都早于任一 end（串行执行时第一个 end 早于第二个 start）
    times = {name: ts for name, ts in timeline}
    latest_start = max(times["alpha_start"], times["beta_start"])
    earliest_end = min(times["alpha_end"], times["beta_end"])
    assert latest_start < earliest_end, (
        f"detector 未并发执行：latest_start={latest_start} >= earliest_end={earliest_end}"
    )

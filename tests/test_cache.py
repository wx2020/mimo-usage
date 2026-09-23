"""TTL cache / single-flight / interval gate tests."""

from __future__ import annotations

import asyncio

import pytest

from mimo_usage.cache import COALESCED, HIT, MISS, STALE, IntervalGate, TTLCache
from mimo_usage.client import AuthError, UpstreamUnavailable


async def test_hit_miss_cycle() -> None:
    cache = TTLCache(60)
    loads = 0

    async def loader():
        nonlocal loads
        loads += 1
        return "v"

    value, state = await cache.get_or_load("k", loader)
    assert (value, state, loads) == ("v", MISS, 1)
    value, state = await cache.get_or_load("k", loader)
    assert (value, state, loads) == ("v", HIT, 1)


async def test_single_flight_collapses_concurrent_loads() -> None:
    cache = TTLCache(60)
    loads = 0

    async def loader():
        nonlocal loads
        loads += 1
        await asyncio.sleep(0.05)
        return loads

    results = await asyncio.gather(*(cache.get_or_load("k", loader) for _ in range(8)))
    assert loads == 1
    states = {state for _, state in results}
    assert MISS in states and COALESCED in states


async def test_expiry_then_reload() -> None:
    clock = {"now": 0.0}
    cache = TTLCache(10, clock=lambda: clock["now"])
    loads = 0

    async def loader():
        nonlocal loads
        loads += 1
        return loads

    await cache.get_or_load("k", loader)
    clock["now"] = 11.0
    _, state = await cache.get_or_load("k", loader)
    assert state == MISS
    assert loads == 2


async def test_stale_fallback_allows_upstream_errors() -> None:
    """默认 allow_stale=True 的错误在 stale 窗口内会拿到旧值（见下一条的显式类）。"""
    clock = {"now": 0.0}
    cache = TTLCache(10, stale_ttl=30, clock=lambda: clock["now"])

    async def ok():
        return "old"

    await cache.get_or_load("k", ok)
    clock["now"] = 11.0  # 过期但仍在 stale 窗口内

    async def boom():
        raise UpstreamUnavailable("down")

    value, state = await cache.get_or_load("k", boom)
    assert (value, state) == ("old", STALE)


async def test_stale_returned_when_error_allows() -> None:
    """模拟 MimoError 默认 allow_stale=True：过期后加载失败返回旧值。"""
    clock = {"now": 0.0}
    cache = TTLCache(10, stale_ttl=30, clock=lambda: clock["now"])

    async def ok():
        return "old"

    await cache.get_or_load("k", ok)
    clock["now"] = 11.0

    class Recoverable(UpstreamUnavailable):
        allow_stale = True

    async def boom():
        raise Recoverable("down")

    value, state = await cache.get_or_load("k", boom)
    assert (value, state) == ("old", STALE)


async def test_auth_errors_opt_out_of_stale() -> None:
    clock = {"now": 0.0}
    cache = TTLCache(10, stale_ttl=30, clock=lambda: clock["now"])

    async def ok():
        return "old"

    await cache.get_or_load("k", ok)
    clock["now"] = 11.0

    async def boom():
        raise AuthError("expired", code=401)

    with pytest.raises(AuthError):
        await cache.get_or_load("k", boom)


async def test_max_age_caps_stale() -> None:
    clock = {"now": 0.0}
    cache = TTLCache(60, stale_ttl=60, clock=lambda: clock["now"])

    async def ok():
        return "old"

    await cache.get_or_load("k", ok, max_age=5)
    clock["now"] = 6.0  # fresh，但 age 6 > max_age 5

    class Recoverable(UpstreamUnavailable):
        allow_stale = True

    async def boom():
        raise Recoverable("down")

    with pytest.raises(Recoverable):
        await cache.get_or_load("k", boom, max_age=5)


def test_interval_gate_allows_once_per_window() -> None:
    clock = {"now": 0.0}
    gate = IntervalGate(30, clock=lambda: clock["now"])

    assert gate.allow("k") is True
    assert gate.allow("k") is False
    clock["now"] = 31.0
    assert gate.allow("k") is True
    # 不同 key 互不影响
    assert gate.allow("other") is True


def test_interval_gate_disabled_when_zero() -> None:
    gate = IntervalGate(0)
    assert gate.allow("k") is True
    assert gate.allow("k") is True


async def test_concurrent_failures_share_the_error() -> None:
    cache = TTLCache(60)
    calls = 0

    async def boom():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        raise UpstreamUnavailable("x")

    results = await asyncio.gather(*(cache.get_or_load("k", boom) for _ in range(5)), return_exceptions=True)
    assert calls == 1
    assert all(isinstance(r, UpstreamUnavailable) for r in results)

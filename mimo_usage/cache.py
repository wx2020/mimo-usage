"""In-process TTL cache with request coalescing.

两个性质对本服务至关重要：

* ``get_or_load`` 把同一 key 的并发未命中合并为一次上游调用，看板刷屏也只打一次
  platform.xiaomimimo.com。
* 过期后 ``stale_ttl`` 秒内旧值仍可读，上游抖动时服务还能应答；鉴权类错误通过
  ``allow_stale = False`` 退出该兜底——过期的 serviceToken 必须暴露，不能拿旧数糊弄。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Hashable
from dataclasses import dataclass
from typing import Any, TypeVar

T = TypeVar("T")

HIT = "HIT"
MISS = "MISS"
COALESCED = "COALESCED"
STALE = "STALE"

CACHED_STATES = frozenset({HIT, COALESCED, STALE})


@dataclass(slots=True)
class _Entry:
    value: Any
    expires_at: float
    stale_until: float
    created_at: float

    def age(self, now: float) -> float:
        return max(now - self.created_at, 0.0)


@dataclass(slots=True)
class _Outcome:
    value: Any = None
    error: BaseException | None = None


def _usable_as_stale(entry: _Entry | None, now: float, max_age: float | None) -> bool:
    if entry is None or entry.stale_until <= now:
        return False
    return max_age is None or entry.age(now) <= max_age


class IntervalGate:
    """Allows an action at most once per ``min_interval`` seconds, per key."""

    __slots__ = ("_min_interval", "_max_entries", "_clock", "_last")

    def __init__(
        self,
        min_interval: float,
        *,
        max_entries: int = 256,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._min_interval = max(min_interval, 0.0)
        self._max_entries = max(max_entries, 1)
        self._clock = clock
        self._last: dict[Hashable, float] = {}

    @property
    def min_interval(self) -> float:
        return self._min_interval

    def allow(self, key: Hashable) -> bool:
        if self._min_interval <= 0:
            return True
        now = self._clock()
        last = self._last.get(key)
        if last is not None and now - last < self._min_interval:
            return False
        self._last[key] = now
        if len(self._last) > self._max_entries:
            self._prune(now)
        return True

    def _prune(self, now: float) -> None:
        for key, last in list(self._last.items()):
            if now - last >= self._min_interval:
                del self._last[key]
        if len(self._last) > self._max_entries:
            overflow = len(self._last) - self._max_entries
            oldest = sorted(self._last, key=lambda key: self._last[key])
            for key in oldest[:overflow]:
                del self._last[key]


class TTLCache:
    """Small, allocation-light cache keyed by hashable values."""

    __slots__ = ("_ttl", "_stale_ttl", "_max_entries", "_clock", "_data", "_inflight")

    def __init__(
        self,
        ttl: float,
        *,
        stale_ttl: float = 0.0,
        max_entries: int = 256,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl
        self._stale_ttl = max(stale_ttl, 0.0)
        self._max_entries = max(max_entries, 1)
        self._clock = clock
        self._data: dict[Hashable, _Entry] = {}
        self._inflight: dict[Hashable, asyncio.Future[_Outcome]] = {}

    def __len__(self) -> int:
        return len(self._data)

    def get(self, key: Hashable) -> Any | None:
        entry = self._data.get(key)
        if entry is None or entry.expires_at <= self._clock():
            return None
        return entry.value

    def age(self, key: Hashable) -> float | None:
        entry = self._data.get(key)
        return None if entry is None else entry.age(self._clock())

    def set(self, key: Hashable, value: Any) -> None:
        now = self._clock()
        self._data[key] = _Entry(
            value=value,
            expires_at=now + self._ttl,
            stale_until=now + self._ttl + self._stale_ttl,
            created_at=now,
        )
        if len(self._data) > self._max_entries:
            self._prune(now)

    def stats(self) -> dict[str, Any]:
        now = self._clock()
        fresh = sum(1 for entry in self._data.values() if entry.expires_at > now)
        return {
            "entries": len(self._data),
            "fresh": fresh,
            "inflight": len(self._inflight),
            "ttl": self._ttl,
            "staleTtl": self._stale_ttl,
        }

    async def get_or_load(
        self,
        key: Hashable,
        loader: Callable[[], Awaitable[T]],
        *,
        max_age: float | None = None,
    ) -> tuple[T, str]:
        """Return ``(value, state)`` where state is HIT/MISS/COALESCED/STALE."""
        entry = self._data.get(key)
        now = self._clock()
        if entry is not None and entry.expires_at > now and (max_age is None or entry.age(now) <= max_age):
            return entry.value, HIT

        pending = self._inflight.get(key)
        if pending is not None:
            outcome = await asyncio.shield(pending)
            if outcome.error is not None:
                raise outcome.error
            return outcome.value, COALESCED

        future: asyncio.Future[_Outcome] = asyncio.get_running_loop().create_future()
        self._inflight[key] = future
        try:
            value = await loader()
        except BaseException as exc:  # noqa: BLE001 - forwarded to every waiter
            if isinstance(exc, asyncio.CancelledError):
                self._finish(key, future, _Outcome(error=exc))
                raise
            if getattr(exc, "allow_stale", True) and _usable_as_stale(entry, self._clock(), max_age):
                self._finish(key, future, _Outcome(value=entry.value))
                return entry.value, STALE
            self._finish(key, future, _Outcome(error=exc))
            raise
        else:
            self.set(key, value)
            self._finish(key, future, _Outcome(value=value))
            return value, MISS

    def _finish(self, key: Hashable, future: asyncio.Future[_Outcome], outcome: _Outcome) -> None:
        self._inflight.pop(key, None)
        if not future.done():
            future.set_result(outcome)

    def _prune(self, now: float) -> None:
        for key, entry in list(self._data.items()):
            if entry.stale_until <= now:
                del self._data[key]
        if len(self._data) <= self._max_entries:
            return
        overflow = len(self._data) - self._max_entries
        oldest = sorted(self._data, key=lambda key: self._data[key].created_at)
        for key in oldest[:overflow]:
            del self._data[key]

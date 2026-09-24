"""Cheap in-process counters exposed through ``/api/v1/metrics``."""

from __future__ import annotations

import time
from collections import Counter
from typing import Any


class Metrics:
    __slots__ = (
        "started_at",
        "_requests",
        "_upstream_calls",
        "_upstream_errors",
        "_upstream_ms",
        "_auth_rejections",
        "_last_auth_rejection",
        "_view_cache",
    )

    def __init__(self) -> None:
        self.started_at = time.time()
        self._requests: Counter[str] = Counter()
        self._upstream_calls = 0
        self._upstream_errors = 0
        self._upstream_ms = 0.0
        self._auth_rejections = 0
        self._last_auth_rejection: float | None = None
        self._view_cache: Counter[str] = Counter()

    def record_request(self, route: str, status: int) -> None:
        self._requests[f"{route}|{status}"] += 1

    def record_cache(self, state: str) -> None:
        self._requests[f"cache:{state}"] += 1

    def record_view(self, state: str) -> None:
        """端点级视图缓存的 HIT/MISS/STALE/… 计数（与分段缓存分开统计）。"""
        self._view_cache[state] += 1

    def view_cache(self) -> dict[str, int]:
        return dict(self._view_cache)

    def record_upstream(self, elapsed_ms: float, *, error: bool = False) -> None:
        self._upstream_calls += 1
        self._upstream_ms += elapsed_ms
        if error:
            self._upstream_errors += 1

    def record_auth_failure(self) -> None:
        self._auth_rejections += 1
        self._last_auth_rejection = time.time()

    def record_auth_success(self) -> None:
        self._last_auth_rejection = None

    def auth_state(self) -> dict[str, Any]:
        state: dict[str, Any] = {
            "rejected": self._last_auth_rejection is not None,
            "rejections": self._auth_rejections,
        }
        if self._last_auth_rejection is not None:
            state["rejectedAgoSeconds"] = round(time.time() - self._last_auth_rejection, 1)
        return state

    def snapshot(self) -> dict[str, Any]:
        calls = self._upstream_calls
        return {
            "uptimeSeconds": round(time.time() - self.started_at, 3),
            "requests": {key: value for key, value in self._requests.items() if "|" in key},
            "cache": {
                key.split(":", 1)[1]: value
                for key, value in self._requests.items()
                if key.startswith("cache:")
            },
            "viewCache": dict(self._view_cache),
            "upstream": {
                "calls": calls,
                "errors": self._upstream_errors,
                "avgLatencyMs": round(self._upstream_ms / calls, 2) if calls else 0.0,
            },
            "upstreamAuth": self.auth_state(),
        }

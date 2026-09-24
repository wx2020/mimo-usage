"""Shared plumbing for the API layer: JSON responses, envelopes and cache access."""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable, Hashable, Iterable
from typing import Any

from sanic import Request
from sanic.response import BaseHTTPResponse, empty
from sanic.response import json as sanic_json

from ..cache import CACHED_STATES, HIT, MISS, STALE, TTLCache
from ..client import MimoClient
from ..metrics import Metrics
from ..timerange import iso_now

try:
    import orjson
except ImportError:  # pragma: no cover - optional acceleration
    orjson = None  # type: ignore[assignment]


def json_response(body: Any, *, status: int = 200, headers: dict[str, str] | None = None) -> BaseHTTPResponse:
    if orjson is not None:
        return sanic_json(body, status=status, headers=headers, dumps=orjson.dumps)
    return sanic_json(body, status=status, headers=headers)


def compact(**values: Any) -> dict[str, Any]:
    """Build a dict, dropping keys whose value is ``None``."""
    return {key: value for key, value in values.items() if value is not None}


def envelope(data: Any, state: str, ttl: float, **extra: Any) -> dict[str, Any]:
    meta = compact(
        cacheState=state,
        cached=state != MISS,
        ttl=int(ttl),
        generatedAt=iso_now(),
        **extra,
    )
    return {"data": data, "meta": meta}


def aggregate(data: Any, errors: dict[str, Any], states: dict[str, str], **extra: Any) -> dict[str, Any]:
    meta = compact(
        cacheState=states,
        cached=bool(states) and all(state in CACHED_STATES for state in states.values()),
        generatedAt=iso_now(),
        **extra,
    )
    meta["errors"] = errors or None
    return {"data": data, "meta": meta}


def headers(state: str, ttl: float) -> dict[str, str]:
    return {"X-Cache": state, "Cache-Control": f"private, max-age={int(max(ttl, 0))}"}


def cache_header(states: dict[str, str]) -> str:
    if not states:
        return MISS
    values = set(states.values())
    if STALE in values:
        return STALE
    return HIT if values <= {HIT} else MISS


def first_auth_error(errors: dict[str, Any]) -> dict[str, Any] | None:
    """聚合端点里任一分段 401/403：整端点按该错误返回，绝不拿旧数据糊弄。"""
    for error in errors.values():
        if error.get("status") in (401, 403):
            return error
    return None


def client(request: Request) -> MimoClient:
    return request.app.ctx.client


def fold_results(
    pairs: tuple[tuple[str, Any], ...],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, str]]:
    """Split ``gather(..., return_exceptions=True)`` output into per-section results."""
    sections: dict[str, Any] = {}
    errors: dict[str, Any] = {}
    states: dict[str, str] = {}
    for name, result in pairs:
        if isinstance(result, BaseException):
            errors[name] = {
                "type": getattr(result, "kind", "error"),
                "message": str(result),
                "status": getattr(result, "status", 500),
            }
        else:
            sections[name], states[name] = result
    return sections, errors, states


def sections_meta(request: Request, entries: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    caches = request.app.ctx.caches
    ages = [age for age in (caches[name].age(key) for name, key in entries) if age is not None]
    return compact(
        ageSeconds=round(max(ages), 1) if ages else None,
        refreshThrottled=getattr(request.ctx, "refresh_throttled", None) or None,
    )


async def cached(
    request: Request,
    cache_name: str,
    key: Hashable,
    loader: Callable[[], Awaitable[Any]],
    *,
    max_age: float | None = None,
) -> tuple[Any, str]:
    """Serve from cache, coalescing concurrent misses (``?refresh=1`` 有限流)."""
    app = request.app
    cache: TTLCache = app.ctx.caches[cache_name]
    metrics: Metrics = app.ctx.metrics
    if request.get_args().get("refresh") in {"1", "true", "yes"}:
        if app.ctx.refresh_gate.allow(key):
            data = await loader()
            cache.set(key, data)
            metrics.record_cache("REFRESH")
            return data, MISS
        request.ctx.refresh_throttled = True
        metrics.record_cache("REFRESH_THROTTLED")
    data, state = await cache.get_or_load(key, loader, max_age=max_age)
    metrics.record_cache(state)
    return data, state


# --------------------------------------------------------------------------- #
# 端点级「整响应」视图缓存 + 条件请求（ETag / If-None-Match）
# --------------------------------------------------------------------------- #

class ViewUncacheable(Exception):
    """视图构建得到非成功响应：不写视图缓存。

    ``allow_stale`` 镜像分段缓存的规则——鉴权错误（401/403）绝不能被 stale 视图遮盖，
    其余上游故障允许用过期视图兜底。
    """

    def __init__(self, response: Any, status: int, *, allow_stale: bool) -> None:
        super().__init__("view response is not cacheable")
        self.response = response
        self.status = status
        self.allow_stale = allow_stale


def view_key(
    endpoint: str,
    ident: str,
    *,
    days: int = 0,
    fields: tuple[str, ...] = (),
) -> tuple[Any, ...]:
    """视图缓存键：serviceToken 指纹 + 端点 + 影响输出的查询参数（days/fields）。"""
    return ("view", endpoint, ident, days, tuple(fields))


def _body_bytes(body: Any) -> bytes:
    if orjson is not None:
        return orjson.dumps(body)
    import json

    return json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def etag_for(body: Any) -> str:
    """内容 ETag（强校验器）：同一响应体字节 → 同一 ETag。"""
    return '"' + hashlib.blake2b(_body_bytes(body), digest_size=16).hexdigest() + '"'


def etag_matches(header: str | None, etag: str) -> bool:
    if not header:
        return False
    for candidate in header.split(","):
        candidate = candidate.strip()
        if candidate == "*":
            return True
        if candidate.startswith("W/"):
            candidate = candidate[2:]
        if candidate == etag:
            return True
    return False


async def view_cached(
    request: Request,
    key: Hashable,
    loader: Callable[[], Awaitable[Any]],
) -> tuple[Any, str]:
    """整响应缓存：命中跳过 loader；沿用 HIT/MISS/STALE 与 ``?refresh=1`` 限流语义。"""
    app = request.app
    cache: TTLCache = app.ctx.caches["view"]
    metrics: Metrics = app.ctx.metrics
    if cache.ttl <= 0:
        return await loader(), MISS
    if request.get_args().get("refresh") in {"1", "true", "yes"}:
        if app.ctx.refresh_gate.allow(key):
            data = await loader()
            cache.set(key, data)
            metrics.record_view("REFRESH")
            return data, MISS
        request.ctx.refresh_throttled = True
        metrics.record_view("REFRESH_THROTTLED")
    data, state = await cache.get_or_load(key, loader)
    metrics.record_view(state)
    return data, state


def _conditional_response(request: Request, body: Any, state: str, ttl: float) -> BaseHTTPResponse:
    if getattr(request.ctx, "refresh_throttled", None) and isinstance(body, dict):
        meta = body.get("meta")
        if isinstance(meta, dict) and not meta.get("refreshThrottled"):
            # 视图命中时刷新被限流：把真实状态补进返回副本（缓存体保持不动）
            body = {**body, "meta": {**meta, "refreshThrottled": True}}
    etag = etag_for(body)
    headers = {
        "ETag": etag,
        "Cache-Control": f"private, max-age={int(max(ttl, 0))}",
        "X-Cache": state,
    }
    if etag_matches(request.headers.get("if-none-match"), etag):
        return empty(status=304, headers=headers)
    return json_response(body, headers=headers)


async def serve_computed(
    request: Request,
    key: Hashable,
    compute: Callable[[], Awaitable[tuple[Any, int, dict[str, str], bool]]],
    ttl: float,
) -> BaseHTTPResponse:
    """统一出口：``compute`` 返回 ``(body, status, section_states, cacheable)``。

    * ``ttl > 0``：经视图缓存；不可缓存的响应（鉴权/部分失败）直接按原状态返回，
      鉴权错误且已有过期视图时也不吃 stale。
    * ``ttl <= 0``：视图缓存关闭，行为与 2.0.0 的分段缓存完全一致。
    """
    if ttl > 0:

        async def loader() -> Any:
            body, status, _states, cacheable = await compute()
            if not cacheable:
                raise ViewUncacheable(body, status, allow_stale=status not in (401, 403))
            return body

        try:
            body, state = await view_cached(request, key, loader)
        except ViewUncacheable as exc:
            return json_response(exc.response, status=exc.status)
        return _conditional_response(request, body, state, ttl)

    body, status, states, _cacheable = await compute()
    headers = {"X-Cache": cache_header(states)} if status == 200 else None
    return json_response(body, status=status, headers=headers)

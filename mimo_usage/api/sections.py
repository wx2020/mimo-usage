"""Single-section endpoints: thin, cached passthroughs to one upstream each."""

from __future__ import annotations

import asyncio
from typing import Any

from sanic import Request
from sanic.response import BaseHTTPResponse

from .. import compat
from ..config import Settings
from .blueprint import bp
from .common import (
    aggregate,
    cache_header,
    cached,
    client,
    compact,
    envelope,
    fold_results,
    headers,
    json_response,
    sections_meta,
)
from .params import (
    fingerprint,
    parse_fields,
    parse_year_month,
    resolve_token,
    select_fields,
    select_sections,
)


@bp.get("/usage")
async def usage(request: Request) -> BaseHTTPResponse:
    """账户维度 token/费用/限速概览（上游 GET /api/v1/usage，无时间窗参数）。"""
    settings: Settings = request.app.ctx.settings
    fields = parse_fields(request)
    token = resolve_token(request)
    key = ("usage", fingerprint(token))
    data, state, extra = await _loaded(
        request,
        "usage",
        key,
        _norm(client(request).usage, compat.normalise_usage),
        fields,
        settings.usage_ttl,
    )
    return json_response(
        envelope(data, state, settings.usage_ttl, **extra),
        headers=headers(state, settings.usage_ttl),
    )


@bp.get("/usage/detail")
async def usage_detail(request: Request) -> BaseHTTPResponse:
    """按年（月可选）的用量明细：POST 上游 usage/detail/list。

    窗口参数 ``?year=&month=``（缺省当前年/整年，Asia/Shanghai），缓存键只含年月，
    整月内稳定不漂移。
    """
    settings: Settings = request.app.ctx.settings
    fields = parse_fields(request)
    token = resolve_token(request)
    window = parse_year_month(request)
    key = ("detail", fingerprint(token), *window.key)
    data, state, extra = await _loaded(
        request,
        "detail",
        key,
        _norm(lambda: client(request).usage_detail_list(window), compat.normalise_detail_list),
        fields,
        settings.detail_ttl,
    )
    return json_response(
        envelope(data, state, settings.detail_ttl, range=window.as_dict(), **extra),
        headers=headers(state, settings.detail_ttl),
    )


@bp.get("/usage/trend")
async def usage_trend(request: Request) -> BaseHTTPResponse:
    """Token Plan 用量统计：每日 × 每模型 Token 明细（上游 usage/token-plan/list）。

    与 ``/usage/detail`` 同窗口参数（``?year=&month=``，缓存键含年月稳定）。
    """
    settings: Settings = request.app.ctx.settings
    fields = parse_fields(request)
    token = resolve_token(request)
    window = parse_year_month(request)
    key = ("trend", fingerprint(token), *window.key)
    data, state, extra = await _loaded(
        request,
        "detail",
        key,
        _norm(lambda: client(request).usage_trend_list(window), compat.normalise_usage_trend),
        fields,
        settings.detail_ttl,
    )
    return json_response(
        envelope(data, state, settings.detail_ttl, range=window.as_dict(), **extra),
        headers=headers(state, settings.detail_ttl),
    )


@bp.get("/usage/bill")
async def usage_bill(request: Request) -> BaseHTTPResponse:
    """月账单列表（上游 GET usage/bill/monthly）。"""
    settings: Settings = request.app.ctx.settings
    fields = parse_fields(request)
    token = resolve_token(request)
    key = ("bill", fingerprint(token))
    data, state, extra = await _loaded(
        request,
        "bill",
        key,
        _norm(client(request).usage_bill_monthly, compat.normalise_bill_monthly),
        fields,
        settings.bill_ttl,
    )
    return json_response(
        envelope(data, state, settings.bill_ttl, **extra),
        headers=headers(state, settings.bill_ttl),
    )


@bp.get("/token-plan")
async def token_plan(request: Request) -> BaseHTTPResponse:
    """订阅详情 + 额度用量 + 可购套餐，三段并发聚合。"""
    token = resolve_token(request)
    ident = fingerprint(token)
    upstream = client(request)
    loaders = {
        "detail": upstream.token_plan_detail,
        "usage": upstream.token_plan_usage,
        "plans": upstream.open_token_plan_list,
    }
    normalisers = {
        "detail": compat.normalise_token_plan_detail,
        "usage": compat.normalise_token_plan_usage,
        "plans": compat.normalise_open_plans,
    }
    keys = {name: ("tokenplan", name, ident) for name in loaders}
    names = select_sections(parse_fields(request), loaders)
    results = await asyncio.gather(
        *(
            cached(request, "tokenplan", keys[name], _norm(loaders[name], normalisers[name]))
            for name in names
        ),
        return_exceptions=True,
    )
    sections, errors, states = fold_results(tuple(zip(names, results, strict=True)))
    return json_response(
        aggregate(
            sections,
            errors,
            states,
            **sections_meta(request, [("tokenplan", keys[name]) for name in states]),
        ),
        headers={"X-Cache": cache_header(states)},
        status=200 if sections else 502,
    )


@bp.get("/account")
async def account(request: Request) -> BaseHTTPResponse:
    """userProfile + balance + projects（只读；变化少，缓存更久）。"""
    token = resolve_token(request)
    ident = fingerprint(token)
    upstream = client(request)
    loaders = {
        "profile": upstream.user_profile,
        "balance": upstream.balance,
        "projects": upstream.projects,
        "verification": upstream.verification_status,
    }
    normalisers = {
        "profile": compat.normalise_profile,
        "balance": compat.normalise_balance,
        "projects": compat.normalise_projects,
        "verification": compat.normalise_verification,
    }
    keys = {name: ("account", name, ident) for name in loaders}
    names = select_sections(parse_fields(request), loaders)
    results = await asyncio.gather(
        *(
            cached(request, "account", keys[name], _norm(loaders[name], normalisers[name]))
            for name in names
        ),
        return_exceptions=True,
    )
    sections, errors, states = fold_results(tuple(zip(names, results, strict=True)))
    return json_response(
        aggregate(
            sections,
            errors,
            states,
            **sections_meta(request, [("account", keys[name]) for name in states]),
        ),
        headers={"X-Cache": cache_header(states)},
        status=200 if sections else 502,
    )


def _norm(loader: Any, normaliser: Any) -> Any:
    async def run() -> Any:
        return normaliser(await loader())

    return run


async def _loaded(
    request: Request,
    cache_name: str,
    key: Any,
    loader: Any,
    fields: tuple[str, ...],
    ttl: float,
) -> tuple[Any, str, dict[str, Any]]:
    data, state = await cached(request, cache_name, key, loader)
    narrowed, ignored = select_fields(data, fields)
    if fields and not narrowed:
        from ..timerange import BadRequest

        raise BadRequest(f"fields 没有匹配到任何字段：{', '.join(ignored)}")
    age = request.app.ctx.caches[cache_name].age(key)
    return narrowed, state, compact(
        fields=fields or None,
        ignoredFields=ignored or None,
        ageSeconds=None if age is None else round(age, 1),
        refreshThrottled=getattr(request.ctx, "refresh_throttled", None) or None,
    )

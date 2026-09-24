"""Single-section endpoints plus the dashboard's computed ``/usage`` payload."""

from __future__ import annotations

import asyncio
from datetime import date, timedelta
from typing import Any

from sanic import Request
from sanic.response import BaseHTTPResponse

from .. import compat
from ..config import Settings
from ..insights import (
    build_chart,
    is_zero_cost_usage,
    is_zero_token_usage,
    package_plan,
)
from ..timerange import BadRequest, YearMonth, now
from .blueprint import bp
from .common import (
    aggregate,
    cache_header,
    cached,
    client,
    compact,
    envelope,
    first_auth_error,
    fold_results,
    headers,
    json_response,
    sections_meta,
    serve_computed,
    view_key,
)
from .params import (
    fingerprint,
    parse_fields,
    parse_year_month,
    resolve_token,
    select_fields,
    select_sections,
)

#: ``/api/v1/usage`` 的顶层字段；``tokenUsage``/``costUsage`` 包月恒 0 时默认省略。
USAGE_FIELDS = ("chart", "accountRateLimit", "pluginUsage", "tokenUsage", "costUsage")
DEFAULT_CHART_DAYS = 30


@bp.get("/usage")
async def usage(request: Request) -> BaseHTTPResponse:
    """看板用量：近 30 天 chart（后端算好）+ 账户限速/插件用量 + 原始 usage 字段。

    包月账号（``tokenPlanUsage.limit > 0``）且 ``tokenUsage``/``costUsage`` 全为 0 时
    默认不输出（``?fields=tokenUsage,costUsage`` 可点名取回）；按量账号照常输出。
    ``chart`` 的按日行与 ``/usage/trend`` 共用 ``trend`` 缓存，不重复打上游。
    """
    settings: Settings = request.app.ctx.settings
    fields = parse_fields(request)
    if fields:
        unknown = [name for name in fields if name not in USAGE_FIELDS]
        if unknown:
            raise BadRequest(
                f"usage 的字段只能是 {' / '.join(USAGE_FIELDS)}，无法识别：{' / '.join(unknown)}"
            )
    days = _parse_days(request, settings)
    token = resolve_token(request)
    ident = fingerprint(token)
    upstream = client(request)
    reference = now()
    today = reference.date()
    windows = _months(today - timedelta(days=days - 1), today)

    specs: list[tuple[str, str, tuple[Any, ...], Any]] = [
        ("usage", "usage", ("usage", ident), _norm(upstream.usage, compat.normalise_usage)),
        (
            "plan",
            "tokenplan",
            ("tokenplan", "usage", ident),
            _norm(upstream.token_plan_usage, compat.normalise_token_plan_usage),
        ),
    ]
    for window in windows:
        specs.append(
            (
                f"trend:{window.label()}",
                "detail",
                ("trend", ident, *window.key),
                _norm(lambda w=window: upstream.usage_trend_list(w), compat.normalise_usage_trend),
            )
        )

    async def compute() -> tuple[dict[str, Any], int, dict[str, str], bool]:
        results = await asyncio.gather(
            *(cached(request, cache_name, key, loader) for _, cache_name, key, loader in specs),
            return_exceptions=True,
        )
        sections, errors, states = fold_results(
            tuple(zip((name for name, *_ in specs), results, strict=True))
        )

        forced = first_auth_error(errors)
        if forced is not None:
            error_body = {"error": {"type": forced["type"], "message": forced["message"]}}
            return error_body, forced["status"], states, False

        rows: list[Any] = []
        for name, *_ in specs:
            if name.startswith("trend:"):
                rows.extend(sections.get(name) or [])

        usage_data = compat.as_dict(sections.get("usage"))
        data: dict[str, Any] = {
            "chart": build_chart(rows, days=days, today=today),
            "accountRateLimit": usage_data.get("accountRateLimit"),
            "pluginUsage": usage_data.get("pluginUsage"),
            "tokenUsage": usage_data.get("tokenUsage"),
            "costUsage": usage_data.get("costUsage"),
        }
        # 包月账号 + 恒 0：默认省略；?fields= 点名取回
        if package_plan(compat.as_dict(sections.get("plan"))):
            for name, is_zero in (
                ("tokenUsage", is_zero_token_usage(data["tokenUsage"])),
                ("costUsage", is_zero_cost_usage(data["costUsage"])),
            ):
                if is_zero and name not in fields:
                    data.pop(name)

        data = compact(**data)
        narrowed, ignored = select_fields(data, fields)
        if fields and not narrowed:
            raise BadRequest(f"fields 没有匹配到任何字段：{', '.join(ignored)}")

        meta_entries = [(cache_name, key) for name, cache_name, key, _ in specs if name in states]
        body = aggregate(narrowed, errors, states, **sections_meta(request, meta_entries))
        cacheable = not errors and bool(sections)
        return body, (200 if sections else 502), states, cacheable

    return await serve_computed(
        request,
        view_key("usage", ident, days=days, fields=fields),
        compute,
        settings.view_ttl,
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

    保留为**原始行入口**（不在 ``/usage`` 的 ``chart`` 里重复提供 ``dailyUsage``）。
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


def _parse_days(request: Request, settings: Settings) -> int:
    raw = request.get_args().get("days")
    if raw is None or raw.strip() == "":
        return DEFAULT_CHART_DAYS
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise BadRequest(f"days 必须是整数，收到 {raw!r}") from None
    if value < 1:
        raise BadRequest("days 必须大于 0")
    return min(value, settings.max_range_days)


def _months(start: date, end: date) -> list[YearMonth]:
    """[start, end] 覆盖到的自然月列表（含端点）。"""
    windows: list[YearMonth] = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        windows.append(YearMonth(year=year, month=month))
        month += 1
        if month > 12:
            month = 1
            year += 1
    return windows


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
        raise BadRequest(f"fields 没有匹配到任何字段：{', '.join(ignored)}")
    age = request.app.ctx.caches[cache_name].age(key)
    return narrowed, state, compact(
        fields=fields or None,
        ignoredFields=ignored or None,
        ageSeconds=None if age is None else round(age, 1),
        refreshThrottled=getattr(request.ctx, "refresh_throttled", None) or None,
    )

"""``/overview``: every section in one round trip (for the dashboard)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from sanic import Request
from sanic.response import BaseHTTPResponse

from .. import compat
from ..config import Settings
from .blueprint import bp
from .common import aggregate, cache_header, cached, client, fold_results, json_response, sections_meta
from .params import fingerprint, parse_fields, parse_year_month, resolve_token, select_sections


@bp.get("/overview")
async def overview(request: Request) -> BaseHTTPResponse:
    """看板一次拿全：usage / detail / bill / tokenPlan / account 并发聚合。

    分段容错：某一段上游挂了只在 ``meta.errors`` 记一笔，其余照常渲染。
    ``?fields=`` 选择分段并跳过未选中的上游调用。
    """
    settings: Settings = request.app.ctx.settings
    token = resolve_token(request)
    ident = fingerprint(token)
    window = parse_year_month(request)
    upstream = client(request)

    loaders: dict[str, Callable[[], Awaitable[Any]]] = {
        "usage": lambda: _norm(upstream.usage, compat.normalise_usage),
        "usageDetail": lambda: _norm(lambda: upstream.usage_detail_list(window), compat.normalise_detail_list),
        "usageTrend": lambda: _norm(lambda: upstream.usage_trend_list(window), compat.normalise_usage_trend),
        "usageBill": lambda: _norm(upstream.usage_bill_monthly, compat.normalise_bill_monthly),
        "tokenPlanDetail": lambda: _norm(upstream.token_plan_detail, compat.normalise_token_plan_detail),
        "tokenPlanUsage": lambda: _norm(upstream.token_plan_usage, compat.normalise_token_plan_usage),
        "plans": lambda: _norm(upstream.open_token_plan_list, compat.normalise_open_plans),
        "profile": lambda: _norm(upstream.user_profile, compat.normalise_profile),
        "balance": lambda: _norm(upstream.balance, compat.normalise_balance),
        "projects": lambda: _norm(upstream.projects, compat.normalise_projects),
        "verification": lambda: _norm(upstream.verification_status, compat.normalise_verification),
    }
    keys: dict[str, Any] = {
        "usage": ("usage", ident),
        "usageDetail": ("detail", ident, *window.key),
        "usageTrend": ("trend", ident, *window.key),
        "usageBill": ("bill", ident),
        "tokenPlanDetail": ("tokenplan", "detail", ident),
        "tokenPlanUsage": ("tokenplan", "usage", ident),
        "plans": ("tokenplan", "plans", ident),
        "profile": ("account", "profile", ident),
        "balance": ("account", "balance", ident),
        "projects": ("account", "projects", ident),
        "verification": ("account", "verification", ident),
    }
    ttl_of = {
        "usage": settings.usage_ttl,
        "usageDetail": settings.detail_ttl,
        "usageBill": settings.bill_ttl,
        "tokenPlanDetail": settings.token_plan_ttl,
        "tokenPlanUsage": settings.token_plan_ttl,
        "plans": settings.token_plan_ttl,
        "profile": settings.account_ttl,
        "balance": settings.account_ttl,
        "projects": settings.account_ttl,
    }

    names = select_sections(parse_fields(request), loaders)
    results = await asyncio.gather(
        *(cached(request, _cache_of(name), keys[name], loaders[name]) for name in names),
        return_exceptions=True,
    )
    sections, errors, states = fold_results(tuple(zip(names, results, strict=True)))

    _ = ttl_of  # TTL 语义由各 cache 实例持有；此处保留映射供维护者对照
    return json_response(
        aggregate(
            sections,
            errors,
            states,
            **sections_meta(request, [( _cache_of(name), keys[name]) for name in states]),
            range={"year": window.year, "month": window.month},
        ),
        headers={"X-Cache": cache_header(states)},
        status=200 if sections else 502,
    )


def _cache_of(name: str) -> str:
    if name in {"usage"}:
        return "usage"
    if name in {"usageDetail", "usageTrend"}:
        return "detail"
    if name == "usageBill":
        return "bill"
    if name in {"tokenPlanDetail", "tokenPlanUsage", "plans"}:
        return "tokenplan"
    return "account"


async def _norm(loader: Callable[[], Awaitable[Any]], normaliser: Any) -> Any:
    return normaliser(await loader())

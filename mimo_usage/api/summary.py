"""``/summary``: the compact, normalised view used by status bars and scripts."""

from __future__ import annotations

import asyncio
from typing import Any

from sanic import Request
from sanic.response import BaseHTTPResponse

from .. import compat
from ..config import Settings
from ..timerange import SHANGHAI, BadRequest
from .blueprint import bp
from .common import aggregate, cached, client, compact, fold_results, json_response, sections_meta
from .params import fingerprint, parse_fields, parse_max_age, resolve_token, select_fields

SUMMARY_FIELDS = (
    "plan",
    "monthUsage",
    "planUsage",
    "balance",
    "tokenUsage",
    "costUsage",
    "rateLimit",
    "window",
)


def _norm(loader: Any, normaliser: Any) -> Any:
    async def run() -> Any:
        return normaliser(await loader())

    return run


def _plan_block(detail: dict[str, Any]) -> dict[str, Any]:
    return compact(
        planCode=detail.get("planCode"),
        planName=detail.get("planName"),
        currentPeriodEnd=detail.get("currentPeriodEnd"),
        expired=detail.get("expired"),
        enableAutoRenew=detail.get("enableAutoRenew"),
        clawEnabled=detail.get("clawEnabled"),
    )


def _pick_item(usage: dict[str, Any], *, section: str, name: str) -> dict[str, Any] | None:
    block = usage.get(section) if section in usage else None
    if not isinstance(block, dict):
        return None
    for item in block.get("items") or []:
        if isinstance(item, dict) and item.get("name") == name:
            return item
    return None


@bp.get("/summary")
async def summary(request: Request) -> BaseHTTPResponse:
    """状态栏一眼所需：套餐、月用量、计划额度、余额、token/费用概览。

    复用与 /token-plan、/account、/usage 相同的缓存段——本端点自身不新增上游流量。
    """
    settings: Settings = request.app.ctx.settings
    fields = parse_fields(request)
    if fields:
        unknown = [name for name in fields if name not in SUMMARY_FIELDS]
        if unknown:
            raise BadRequest(
                f"summary 的字段只能是 {' / '.join(SUMMARY_FIELDS)}，无法识别：{' / '.join(unknown)}"
            )
    max_age = parse_max_age(request, default=settings.summary_max_age)
    token = resolve_token(request)
    ident = fingerprint(token)
    upstream = client(request)
    keys = {
        "detail": ("tokenplan", "detail", ident),
        "planUsage": ("tokenplan", "usage", ident),
        "balance": ("account", "balance", ident),
        "usage": ("usage", ident),
    }

    detail_result, plan_usage_result, balance_result, usage_result = await asyncio.gather(
        cached(
            request, "tokenplan", keys["detail"],
            _norm(upstream.token_plan_detail, compat.normalise_token_plan_detail),
            max_age=max_age,
        ),
        cached(
            request, "tokenplan", keys["planUsage"],
            _norm(upstream.token_plan_usage, compat.normalise_token_plan_usage),
            max_age=max_age,
        ),
        cached(
            request, "account", keys["balance"],
            _norm(upstream.balance, compat.normalise_balance),
            max_age=max_age,
        ),
        cached(
            request, "usage", keys["usage"],
            _norm(upstream.usage, compat.normalise_usage),
            max_age=max_age,
        ),
        return_exceptions=True,
    )
    sections, errors, states = fold_results(
        (
            ("detail", detail_result),
            ("planUsage", plan_usage_result),
            ("balance", balance_result),
            ("usage", usage_result),
        )
    )

    detail = compat.as_dict(sections.get("detail"))
    plan_usage = compat.as_dict(sections.get("planUsage"))
    balance = compat.as_dict(sections.get("balance"))
    usage = compat.as_dict(sections.get("usage"))

    data, ignored = select_fields(
        compact(
            plan=_plan_block(detail) if detail else None,
            monthUsage=_pick_item(plan_usage, section="monthUsage", name="month_total_token"),
            planUsage=_pick_item(plan_usage, section="usage", name="plan_total_token"),
            balance=balance or None,
            tokenUsage=usage.get("tokenUsage"),
            costUsage=usage.get("costUsage"),
            rateLimit=usage.get("accountRateLimit"),
        ),
        fields,
    )
    if fields and not data:
        raise BadRequest(f"fields 没有匹配到任何字段：{', '.join(ignored)}")

    # sections_meta 需要的是 (缓存名, 缓存键)，不是分段名
    cache_of = {"detail": "tokenplan", "planUsage": "tokenplan", "balance": "account", "usage": "usage"}
    meta_entries = [(cache_of[name], keys[key]) for name, key in (
        ("detail", "detail"),
        ("planUsage", "planUsage"),
        ("balance", "balance"),
        ("usage", "usage"),
    ) if name in states]
    return json_response(
        aggregate(
            data,
            errors,
            states,
            **sections_meta(request, meta_entries),
            timezone=str(SHANGHAI),
            maxAgeSeconds=max_age,
            fields=fields or None,
            ignoredFields=ignored or None,
        ),
        status=200 if data else 502,
    )

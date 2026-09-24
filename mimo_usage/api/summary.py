"""``/summary``: 看板所需的、后端算好的套餐与卡片数值。

前端不再做任何业务计算（Credits 折算、百分比、剩余天数、天限额、token 汇总、
峰值、时间窗口）——本端点直接返回算好的 ``cards`` 等字段。
"""

from __future__ import annotations

import asyncio
from typing import Any

from sanic import Request
from sanic.response import BaseHTTPResponse

from .. import compat
from ..insights import build_cards, is_zero_balance, package_plan
from ..timerange import SHANGHAI, BadRequest, YearMonth, now
from .blueprint import bp
from .common import (
    aggregate,
    cached,
    client,
    compact,
    first_auth_error,
    fold_results,
    sections_meta,
    serve_computed,
    view_key,
)
from .params import fingerprint, parse_fields, resolve_token, select_fields

SUMMARY_FIELDS = ("plan", "cards", "account", "verification", "rateLimit", "balance")


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


def _account_block(profile: dict[str, Any]) -> dict[str, Any]:
    return compact(
        userId=profile.get("userId"),
        phone=profile.get("phone"),
        email=profile.get("email"),
        weixin=profile.get("weixin"),
    )


@bp.get("/summary")
async def summary(request: Request) -> BaseHTTPResponse:
    """套餐 + 顶部卡片 + 账户 + 限速；数值全部后端算好，前端纯渲染。"""
    fields = parse_fields(request)
    if fields:
        unknown = [name for name in fields if name not in SUMMARY_FIELDS]
        if unknown:
            raise BadRequest(
                f"summary 的字段只能是 {' / '.join(SUMMARY_FIELDS)}，无法识别：{' / '.join(unknown)}"
            )
    token = resolve_token(request)
    ident = fingerprint(token)
    upstream = client(request)
    reference = now()
    year, month = reference.year, reference.month

    # (分段名, 缓存名, 缓存键, loader)；缓存段与 /account、/usage、/usage/trend 互通
    specs: list[tuple[str, str, tuple[Any, ...], Any]] = [
        (
            "detail",
            "tokenplan",
            ("tokenplan", "detail", ident),
            _norm(upstream.token_plan_detail, compat.normalise_token_plan_detail),
        ),
        (
            "planUsage",
            "tokenplan",
            ("tokenplan", "usage", ident),
            _norm(upstream.token_plan_usage, compat.normalise_token_plan_usage),
        ),
        ("usage", "usage", ("usage", ident), _norm(upstream.usage, compat.normalise_usage)),
        ("balance", "account", ("account", "balance", ident), _norm(upstream.balance, compat.normalise_balance)),
        ("profile", "account", ("account", "profile", ident), _norm(upstream.user_profile, compat.normalise_profile)),
        (
            "verification",
            "account",
            ("account", "verification", ident),
            _norm(upstream.verification_status, compat.normalise_verification),
        ),
        (
            "monthTrend",
            "detail",
            ("trend", ident, year, month),
            _norm(lambda: upstream.usage_trend_list(YearMonth(year=year, month=month)), compat.normalise_usage_trend),
        ),
        (
            "yearTrend",
            "detail",
            ("trend", ident, year, 0),
            _norm(lambda: upstream.usage_trend_list(YearMonth(year=year, month=None)), compat.normalise_usage_trend),
        ),
        (
            "prevYearTrend",
            "detail",
            ("trend", ident, year - 1, 0),
            _norm(
                lambda: upstream.usage_trend_list(YearMonth(year=year - 1, month=None)),
                compat.normalise_usage_trend,
            ),
        ),
    ]

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

        detail = compat.as_dict(sections.get("detail"))
        plan_usage = compat.as_dict(sections.get("planUsage"))
        usage = compat.as_dict(sections.get("usage"))
        balance = compat.as_dict(sections.get("balance"))
        profile = compat.as_dict(sections.get("profile"))
        verification = compat.as_dict(sections.get("verification"))

        cards = build_cards(
            daily_rows=sections.get("monthTrend") or [],
            year_rows=sections.get("yearTrend") or [],
            prev_year_rows=sections.get("prevYearTrend") or [],
            plan_usage=plan_usage,
            plan_detail=detail,
            now=reference,
        )

        data: dict[str, Any] = compact(
            plan=_plan_block(detail),
            cards=cards,
            account=_account_block(profile),
            verification=verification,
            rateLimit=usage.get("accountRateLimit"),
            balance=balance or None,
        )
        # 包月账号且余额恒 0：默认不输出（?fields=balance 可点名取回；按量账号照常输出）
        zero_balance_hidden = (
            package_plan(plan_usage) and "balance" in data and is_zero_balance(data["balance"])
        )
        if zero_balance_hidden and "balance" not in fields:
            data.pop("balance")

        data, ignored = select_fields(data, fields)
        if fields and not data:
            raise BadRequest(f"fields 没有匹配到任何字段：{', '.join(ignored)}")

        meta_entries = [(cache_name, key) for name, cache_name, key, _ in specs if name in states]
        body = aggregate(
            data,
            errors,
            states,
            timezone=str(SHANGHAI),
            **sections_meta(request, meta_entries),
        )
        cacheable = not errors and bool(sections)
        return body, (200 if sections else 502), states, cacheable

    return await serve_computed(
        request,
        view_key("summary", ident, fields=fields),
        compute,
        request.app.ctx.settings.view_ttl,
    )

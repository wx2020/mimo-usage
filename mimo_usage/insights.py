"""纯计算层：把上游原始行折算成看板直接渲染的数值。

设计约束：

* **无 I/O、无 async、无全局状态**：只做数学与整形，便于单测与复用。
* Credits 单价表是**服务端唯一来源**（`CREDIT_RATES`）——前端不再持有折算逻辑。
* 所有业务口径（百分比、剩余天数、天限额、token 汇总、峰值、模型占比排序）
  都在这里算好；`/api/v1/summary` 与 `/api/v1/usage` 原样输出。
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from typing import Any

#: 官方 Token Plan 单价（Credits / token）：
#: pro 系（v2.6-pro/v2.5-pro）命中 2.5 / 未命中 300 / 输出 600；
#: flash 系（v2.6-flash/v2.5）命中 2 / 未命中 100 / 输出 200。
#: 来源：mimo.mi.com 文档《Token Plan · 个人版 · 额度消耗规则》
#: （ASR 30M Credits/小时、TTS 免费；夜间 0:00-8:00 另有 0.8x 系数）。
CREDIT_RATES: dict[str, tuple[float, float, float]] = {
    "mimo-v2.6-pro": (2.5, 300, 600),
    "mimo-v2.5-pro": (2.5, 300, 600),
    "mimo-v2.6-flash": (2, 100, 200),
    "mimo-v2.5": (2, 100, 200),
}

_DATE_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d")


def _as_float(value: Any) -> float:
    if value is None or isinstance(value, bool):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def credits_of(rows: Any) -> float:
    """按官方单价把用量行折算成 Credits（未识别模型跳过，不猜）。"""
    total = 0.0
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        rate = CREDIT_RATES.get(row.get("model"))
        if rate is None:
            continue
        total += _as_float(row.get("inputHitToken")) * rate[0]
        total += _as_float(row.get("inputMissToken")) * rate[1]
        total += _as_float(row.get("outputToken")) * rate[2]
    return total


def nice_ceil(value: Any) -> float:
    """把刻度上限抬到 1/1.2/1.5/2/2.5/3/4/5/6/8/10 的整数倍。"""
    number = _as_float(value)
    if number <= 0:
        return 1.0
    base = 10 ** math.floor(math.log10(number))
    for step in (1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10):
        if step * base >= number:
            return step * base
    return 10 * base


def period_end(value: Any, now: datetime) -> datetime | None:
    """解析 ``currentPeriodEnd``（如 ``2027-09-22 23:59:59``）为带时区时间。"""
    if not value:
        return None
    text = str(value).strip().replace("T", " ")
    for fmt in _DATE_FORMATS:
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return parsed.replace(tzinfo=now.tzinfo)
    return None


def sum_tokens(rows: Any, key: str = "totalToken") -> float:
    return sum(_as_float(row.get(key)) for row in rows or [] if isinstance(row, dict))


def sum_requests(rows: Any) -> int:
    return int(sum(_as_float(row.get("requestCount")) for row in rows or [] if isinstance(row, dict)))


def peak_day(rows: Any) -> dict[str, Any] | None:
    """单日 token 峰值（按日聚合，取最大）。"""
    per_day: dict[str, float] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        day = row.get("date")
        if not day:
            continue
        per_day[day] = per_day.get(day, 0.0) + _as_float(row.get("totalToken"))
    peak: dict[str, Any] | None = None
    for day, tokens in per_day.items():
        if peak is None or tokens > peak["tokens"]:
            peak = {"date": day, "tokens": tokens}
    return peak


def plan_limits(plan_usage: Any) -> tuple[float, float]:
    """从 tokenPlanUsage 取 ``(limit, used)``；优先 plan_total_token，回退 monthUsage 首项。"""
    payload = plan_usage if isinstance(plan_usage, dict) else {}
    item = _first_item(payload.get("usage"), "plan_total_token")
    if item is None:
        month_items = _items(payload.get("monthUsage"))
        item = month_items[0] if month_items else None
    if not isinstance(item, dict):
        return 0.0, 0.0
    return _as_float(item.get("limit")), _as_float(item.get("used"))


def package_plan(plan_usage: Any) -> bool:
    """包月账号判定：``plan_total_token.limit > 0``。"""
    limit, _used = plan_limits(plan_usage)
    return limit > 0


def _items(block: Any) -> list[Any]:
    if not isinstance(block, dict):
        return []
    items = block.get("items")
    return items if isinstance(items, list) else []


def _first_item(block: Any, name: str) -> dict[str, Any] | None:
    for item in _items(block):
        if isinstance(item, dict) and item.get("name") == name:
            return item
    return None


def _clamp01(value: float | None) -> float | None:
    if value is None:
        return None
    return max(0.0, min(value, 1.0))


def _all_numeric_zero(mapping: Any) -> bool:
    """所有数值字段都为 0（非数值如 currency 跳过）；一个都没取到则视为非零。"""
    if not isinstance(mapping, dict):
        return False
    seen = False
    for value in mapping.values():
        if value is None or isinstance(value, bool):
            continue
        if isinstance(value, str):
            try:
                number = float(value)
            except ValueError:
                continue  # 非数值（如 currency/CNY）不参与判定
        else:
            number = _as_float(value)
        seen = True
        if number != 0:
            return False
    return seen


def is_zero_token_usage(token_usage: Any) -> bool:
    return _all_numeric_zero(token_usage)


def is_zero_cost_usage(cost_usage: Any) -> bool:
    return _all_numeric_zero(cost_usage)


def is_zero_balance(balance: Any) -> bool:
    return _all_numeric_zero(balance)


def build_cards(
    *,
    daily_rows: Any,
    year_rows: Any,
    prev_year_rows: Any,
    plan_usage: Any,
    plan_detail: Any,
    now: datetime,
) -> dict[str, Any]:
    """顶部卡片所需的全部数值：今日额度、总额度、天限额、剩余、tokens、峰值、Credits。"""
    limit, used = plan_limits(plan_usage)
    remaining = max(limit - used, 0.0) if limit > 0 else None
    ratio = (used / limit) if limit > 0 else None

    end = period_end((plan_detail or {}).get("currentPeriodEnd") if isinstance(plan_detail, dict) else None, now)
    days_left: int | None = None
    if end is not None:
        days_left = max(int(math.ceil((end - now).total_seconds() / 86400)), 1)
    daily_quota = (remaining / days_left) if (remaining is not None and days_left) else None

    daily = [row for row in (daily_rows or []) if isinstance(row, dict)]
    today_key = now.date().isoformat()
    month_key = now.strftime("%Y-%m")
    today_rows = [row for row in daily if row.get("date") == today_key]

    today_tokens = sum_tokens(today_rows)
    today_requests = sum_requests(today_rows)
    month_tokens = sum_tokens(daily)
    month_requests = sum_requests(daily)
    month_days = len({row.get("date") for row in daily if row.get("date")})
    today_credits = credits_of(today_rows)
    month_credits = credits_of(daily)

    historic = sum_tokens([row for row in (year_rows or []) if row.get("date") != month_key])
    historic += sum_tokens(prev_year_rows)
    all_time = month_tokens + historic

    today_ratio = (today_credits / daily_quota) if daily_quota else None
    return {
        "today": {
            "credits": round(today_credits, 2),
            "dailyQuota": round(daily_quota, 2) if daily_quota is not None else None,
            "percent": round(today_ratio * 100, 4) if today_ratio is not None else None,
            "ratio": today_ratio,
            "bar": _clamp01(today_ratio),
            "warn": bool(today_ratio is not None and today_ratio > 1),
            "tokens": today_tokens,
            "requests": today_requests,
        },
        "total": {
            "used": used,
            "limit": limit,
            "remaining": remaining,
            "percent": round(ratio * 100, 4) if ratio is not None else None,
            "ratio": ratio,
            "bar": _clamp01(ratio),
            "days": days_left,
        },
        "tokens": {
            "today": today_tokens,
            "todayRequests": today_requests,
            "month": month_tokens,
            "monthRequests": month_requests,
            "monthDays": month_days,
            "monthAverage": (month_tokens / month_days) if month_days else 0.0,
            "allTime": all_time,
            "peak": peak_day(daily),
        },
        "credits": {
            "today": round(today_credits, 2),
            "month": round(month_credits, 2),
            "used": used,
            "delta": round(month_credits - used, 2) if limit > 0 else None,
        },
    }


def build_chart(rows: Any, *, days: int = 30, today: date | None = None) -> dict[str, Any]:
    """近 ``days`` 天（含今天）的按日 × 模型用量 + 窗口模型占比。

    返回 ``points``（含空白日）/ ``models``（rank/share/sharePercent）/ ``totals`` / ``axisMax``。
    """
    reference = today if today is not None else date.today()
    span = max(int(days), 1)

    by_date: dict[str, dict[str, Any]] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        day = row.get("date")
        if not day:
            continue
        tokens = _as_float(row.get("totalToken"))
        entry = by_date.setdefault(day, {"models": {}, "totalToken": 0.0, "requestCount": 0})
        model = row.get("model") or "?"
        entry["models"][model] = entry["models"].get(model, 0.0) + tokens
        entry["totalToken"] += tokens
        entry["requestCount"] += int(_as_float(row.get("requestCount")))

    points: list[dict[str, Any]] = []
    model_totals: dict[str, float] = {}
    for offset in range(span - 1, -1, -1):
        key = (reference - timedelta(days=offset)).isoformat()
        hit = by_date.get(key)
        models = []
        if hit is not None:
            models = [
                {"model": model, "tokens": tokens}
                for model, tokens in sorted(hit["models"].items(), key=lambda item: (-item[1], item[0]))
            ]
        points.append(
            {
                "date": key,
                "label": key[5:],
                "totalToken": hit["totalToken"] if hit is not None else 0.0,
                "requestCount": hit["requestCount"] if hit is not None else 0,
                "models": models,
            }
        )
        for item in models:
            model_totals[item["model"]] = model_totals.get(item["model"], 0.0) + item["tokens"]

    grand = sum(model_totals.values())
    ranked = sorted(model_totals.items(), key=lambda item: (-item[1], item[0]))
    models = [
        {
            "model": model,
            "tokens": tokens,
            "share": (tokens / grand) if grand else 0.0,
            "sharePercent": round((tokens / grand) * 100, 4) if grand else 0.0,
            "rank": rank,
        }
        for rank, (model, tokens) in enumerate(ranked)
    ]

    totals = {
        "totalToken": sum(point["totalToken"] for point in points),
        "requestCount": sum(point["requestCount"] for point in points),
        "peak": peak_day(points),
        "days": len([point for point in points if point["totalToken"] > 0]),
        "models": len(models),
    }
    axis_max = nice_ceil(max((point["totalToken"] for point in points), default=0))
    return {"days": span, "points": points, "models": models, "totals": totals, "axisMax": axis_max}


__all__ = [
    "CREDIT_RATES",
    "build_cards",
    "build_chart",
    "credits_of",
    "is_zero_balance",
    "is_zero_cost_usage",
    "is_zero_token_usage",
    "nice_ceil",
    "package_plan",
    "peak_day",
    "period_end",
    "plan_limits",
    "sum_requests",
    "sum_tokens",
]

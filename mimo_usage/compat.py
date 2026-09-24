"""上游字段兼容层：把 platform 的响应整形成前端/调用方稳定的形状。

上游是网页内部接口，字段会无预警改版。
这里只做"保证键存在 + 已知别名归一"，不发明上游没有的数据：某键缺失时给 None/[]，
让前端空态自己说话。
"""

from __future__ import annotations

from typing import Any


def as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def as_int(value: Any) -> int | None:
    number = as_float(value)
    return int(number) if number is not None else None


#: ``GET /api/v1/usage`` → data.tokenUsage 的已知键；缺哪个补 0，前端免判空。
TOKEN_USAGE_KEYS = (
    "inputToken",
    "outputToken",
    "cacheToken",
    "totalToken",
    "inputAudioDuration",
    "batchInputToken",
    "batchOutputToken",
    "batchCacheToken",
    "batchInputAudioDuration",
)

RATE_LIMIT_KEYS = ("tpm", "rpm", "queryTpm", "concurrency")


def normalise_usage(data: Any) -> dict[str, Any]:
    payload = as_dict(data)
    token = as_dict(payload.get("tokenUsage"))
    for key in TOKEN_USAGE_KEYS:
        token.setdefault(key, 0)
    rate = as_dict(payload.get("accountRateLimit"))
    for key in RATE_LIMIT_KEYS:
        rate.setdefault(key, None)
    cost = as_dict(payload.get("costUsage"))
    cost.setdefault("totalCost", "0.00")
    cost.setdefault("currentMonthCost", "0.00")
    plugin = as_dict(payload.get("pluginUsage"))
    plugin.setdefault("totalRequestCount", "0")
    plugin.setdefault("webSearchRequestCount", "0")
    payload.setdefault("tokenUsage", token)
    payload.setdefault("accountRateLimit", rate)
    payload.setdefault("costUsage", cost)
    payload.setdefault("pluginUsage", plugin)
    return payload


def normalise_detail_list(data: Any) -> list[Any]:
    return as_list(data)


def normalise_usage_trend(data: Any) -> list[dict[str, Any]]:
    """Token 用量统计行（date/model/totalToken/…）；缺键给 None，不发明数值。"""
    rows: list[dict[str, Any]] = []
    for item in as_list(data):
        row = as_dict(item)
        rows.append(
            {
                "date": row.get("date"),
                "model": row.get("model"),
                "totalToken": as_float(row.get("totalToken")),
                "inputHitToken": as_float(row.get("inputHitToken")),
                "inputMissToken": as_float(row.get("inputMissToken")),
                "outputToken": as_float(row.get("outputToken")),
                "requestCount": as_int(row.get("requestCount")),
                "inputAudioDuration": as_float(row.get("inputAudioDuration")),
            }
        )
    return rows


def normalise_token_plan_detail(data: Any) -> dict[str, Any]:
    payload = as_dict(data)
    payload.setdefault("planCode", None)
    payload.setdefault("planName", None)
    payload.setdefault("currentPeriodEnd", None)
    payload.setdefault("expired", None)
    payload.setdefault("enableAutoRenew", None)
    return payload


def _usage_items(data: Any) -> dict[str, Any]:
    payload = as_dict(data)
    items = [as_dict(item) for item in as_list(payload.get("items"))]
    return {
        "percent": as_float(payload.get("percent")),
        "items": [
            {
                "name": item.get("name"),
                "used": as_float(item.get("used")),
                "limit": as_float(item.get("limit")),
                "percent": as_float(item.get("percent")),
            }
            for item in items
        ],
    }


def normalise_token_plan_usage(data: Any) -> dict[str, Any]:
    payload = as_dict(data)
    month = as_dict(payload.get("monthUsage"))
    plan = as_dict(payload.get("usage"))
    return {"monthUsage": _usage_items(month), "usage": _usage_items(plan)}


def normalise_profile(data: Any) -> dict[str, Any]:
    payload = as_dict(data)
    payload.setdefault("userId", None)
    payload.setdefault("phone", None)
    payload.setdefault("email", None)
    payload.setdefault("nickName", None)
    payload.setdefault("userName", None)
    return payload


def normalise_balance(data: Any) -> dict[str, Any]:
    payload = as_dict(data)
    for key in (
        "balance",
        "frozenBalance",
        "currency",
        "overdraftLimit",
        "remainingOverdraftLimit",
        "giftBalance",
        "cashBalance",
    ):
        payload.setdefault(key, None)
    return payload


def normalise_projects(data: Any) -> list[dict[str, Any]]:
    payload = as_dict(data)
    return [as_dict(item) for item in as_list(payload.get("projects"))]


def normalise_verification(data: Any) -> dict[str, Any]:
    """实名认证状态：state=NOT_AUTHORIZED(未认证)/AUTHORIZED(已认证) 等，原样透传不翻译。"""
    payload = as_dict(data)
    return {
        "state": payload.get("userVerificationState"),
        "realName": payload.get("realName"),
        "cardType": payload.get("cardType"),
        "authTime": payload.get("authTime"),
        "authorizeUrl": payload.get("authorizeUrl"),
    }

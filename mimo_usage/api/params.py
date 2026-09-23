"""Query-parameter parsing and credential resolution."""

from __future__ import annotations

import hashlib
from typing import Any

from sanic import Request

from ..client import MissingCredentialError
from ..config import CredentialStore, Settings
from ..timerange import BadRequest, TimeRange, YearMonth, resolve_range, resolve_year_month


def credentials(request: Request) -> CredentialStore:
    return request.app.ctx.credentials


def resolve_token(request: Request) -> str:
    """The serviceToken every upstream call authenticates with.

    允许调用方用 ``Authorization: Bearer`` / ``X-Mimo-Service-Token`` 覆盖配置值，
    便于多账号排查；缺配置则 401 missing_credential。
    """
    header = request.headers.get("x-mimo-service-token")
    if header and header.strip():
        return header.strip()
    auth = request.headers.get("authorization")
    if auth:
        token = auth[7:].strip() if auth[:7].lower() == "bearer " else auth.strip()
        if token:
            return token
    token = credentials(request).service_token.get()
    if not token:
        raise MissingCredentialError(
            "未配置 serviceToken：请设置 MIMO_SERVICE_TOKEN / MIMO_SERVICE_TOKEN_FILE，"
            "或在请求中带上 Authorization 请求头"
        )
    return token


def fingerprint(token: str) -> str:
    """Short, non-reversible token id used to keep cache entries separate."""
    return hashlib.blake2b(token.encode("utf-8"), digest_size=8).hexdigest()


def parse_window(request: Request, *, default_days: int, settings: Settings) -> TimeRange:
    args = request.get_args()
    raw_days = args.get("days")
    if raw_days:
        try:
            default_days = int(raw_days)
        except ValueError:
            raise BadRequest(f"days 必须是整数，收到 {raw_days!r}") from None
        if default_days < 1:
            raise BadRequest("days 必须大于 0")
    return resolve_range(
        args.get("startTime") or args.get("start"),
        args.get("endTime") or args.get("end"),
        default_days=default_days,
        max_days=settings.max_range_days,
    )


def parse_year_month(request: Request) -> YearMonth:
    args = request.get_args()
    return resolve_year_month(args.get("year"), args.get("month"))


def window_key(window: TimeRange) -> tuple[str, str]:
    formatted = window.formatted()
    return formatted["startTime"], formatted["endTime"]


def parse_fields(request: Request) -> tuple[str, ...]:
    """``?fields=a,b`` keeps only those top-level keys of ``data``."""
    raw = request.get_args().get("fields")
    if not raw:
        return ()
    names = tuple(dict.fromkeys(part.strip() for part in raw.split(",") if part.strip()))
    if not names:
        raise BadRequest("fields 不能为空，例如 ?fields=tokenUsage,balance")
    for name in names:
        if len(name) > 64 or "." in name:
            raise BadRequest(f"fields 只支持 data 下的一级字段名，收到 {name!r}")
    return names


def select_fields(data: Any, fields: tuple[str, ...]) -> tuple[Any, list[str]]:
    if not fields:
        return data, []
    if not isinstance(data, dict):
        return data, list(fields)
    return {name: data[name] for name in fields if name in data}, [name for name in fields if name not in data]


def select_sections(fields: tuple[str, ...], vocabulary: Any) -> tuple[str, ...]:
    names = tuple(vocabulary)
    if not fields:
        return names
    wanted = tuple(name for name in names if name in fields)
    if not wanted:
        raise BadRequest(f"fields 只能是 {' / '.join(names)}")
    return wanted


def parse_max_age(request: Request, *, default: float | None = None) -> float | None:
    """``?maxAge=60`` refuses cache entries older than 60 seconds (``0`` = always reload)."""
    raw = request.get_args().get("maxAge")
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise BadRequest(f"maxAge 必须是秒数，收到 {raw!r}") from None
    if value < 0:
        raise BadRequest("maxAge 不能为负数")
    return value

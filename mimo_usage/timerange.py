"""Time-window helpers.

上游与本服务统一使用 ``Asia/Shanghai``。时间段（TimeRange）服务 ``days=`` 类窗口；
year/month 窗口服务 ``/usage/detail``——缓存键只含年月，天然按月稳定、不随秒漂移。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

try:  # pragma: no cover - depends on the tz database being present
    from zoneinfo import ZoneInfo

    SHANGHAI = ZoneInfo("Asia/Shanghai")
except Exception:  # pragma: no cover - fallback keeps the service usable
    SHANGHAI = timezone(timedelta(hours=8), "Asia/Shanghai")

TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
_PARSERS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d",
)


class BadRequest(ValueError):
    """Raised for any query parameter the caller got wrong."""


class InvalidRange(BadRequest):
    """Raised when a caller supplied time window cannot be parsed or is unusable."""


@dataclass(frozen=True, slots=True)
class TimeRange:
    start: datetime
    end: datetime

    def formatted(self) -> dict[str, str]:
        return {"startTime": format_time(self.start), "endTime": format_time(self.end)}

    def as_dict(self) -> dict[str, str]:
        return {
            "startTime": self.start.isoformat(),
            "endTime": self.end.isoformat(),
            "days": round((self.end - self.start).total_seconds() / 86400, 2),
        }


def now() -> datetime:
    return datetime.now(SHANGHAI)


def format_time(value: datetime) -> str:
    if value.tzinfo is not None:
        value = value.astimezone(SHANGHAI)
    return value.strftime(TIME_FORMAT)


def parse_time(raw: str, *, field: str = "time") -> datetime:
    text = raw.strip().replace("+00:00", "").strip()
    for fmt in _PARSERS:
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=SHANGHAI)
        except ValueError:
            continue
    raise InvalidRange(
        f"无法解析 {field}={raw!r}，请使用 YYYY-MM-DD 或 'YYYY-MM-DD HH:mm:ss' 格式"
    )


def _normalise(value: datetime) -> datetime:
    return value.astimezone(SHANGHAI) if value.tzinfo else value.replace(tzinfo=SHANGHAI)


def resolve_range(
    start_raw: str | None,
    end_raw: str | None,
    *,
    default_days: int,
    max_days: int,
) -> TimeRange:
    """Turn loose query parameters into a validated window (Asia/Shanghai)."""
    reference = now()
    if end_raw:
        end = _normalise(parse_time(end_raw, field="endTime"))
    else:
        # 隐式窗口止步于当前整点 59:59：秒级终点会让每个请求都成为新的缓存键
        end = reference.replace(minute=59, second=59, microsecond=0)
    if start_raw:
        start = _normalise(parse_time(start_raw, field="startTime"))
    else:
        start = (end - timedelta(days=max(default_days, 1))).replace(
            hour=0, minute=0, second=0, microsecond=0
        )

    if start >= end:
        raise InvalidRange("startTime 必须早于 endTime")
    if end > reference + timedelta(days=1):
        raise InvalidRange("endTime 不能超过当前时间")
    if end - start > timedelta(days=max_days + 1):
        raise InvalidRange(f"时间跨度不能超过 {max_days} 天")
    return TimeRange(start=start, end=end)


@dataclass(frozen=True, slots=True)
class YearMonth:
    """A stable month-scale window: cache keys built from it do not drift."""

    year: int
    month: int | None  # None = 整年

    @property
    def key(self) -> tuple[int, int]:
        return self.year, self.month if self.month is not None else 0

    def as_dict(self) -> dict[str, int | None]:
        # month 恒出现在键里（null = 整年），前端/调用方无需判键存在
        return {"year": self.year, "month": self.month}

    def label(self) -> str:
        return f"{self.year}-{self.month:02d}" if self.month else str(self.year)


def resolve_year_month(year_raw: str | None, month_raw: str | None) -> YearMonth:
    """解析 ``?year=&month=``；缺省为 Asia/Shanghai 的当前年，月可省（整年）。

    年月窗口只在自然月边界变化，因此 ``/usage/detail`` 的缓存键天然稳定。
    """
    reference = now()
    if year_raw is None or year_raw.strip() == "":
        return YearMonth(year=reference.year, month=None)
    try:
        year = int(year_raw)
    except (TypeError, ValueError):
        raise BadRequest(f"year 必须是整数，收到 {year_raw!r}") from None
    if not 2000 <= year <= 2100:
        raise BadRequest(f"year 需在 2000–2100 之间，收到 {year}")

    month: int | None = None
    if month_raw is not None and month_raw.strip() != "":
        try:
            month = int(month_raw)
        except (TypeError, ValueError):
            raise BadRequest(f"month 必须是整数，收到 {month_raw!r}") from None
        if not 1 <= month <= 12:
            raise BadRequest(f"month 需在 1–12 之间，收到 {month}")
        if year == reference.year and month > reference.month:
            raise BadRequest(f"{year}-{month:02d} 尚未到来（当前 {reference.year}-{reference.month:02d}）")
    return YearMonth(year=year, month=month)


def iso_now() -> str:
    return now().isoformat(timespec="seconds")

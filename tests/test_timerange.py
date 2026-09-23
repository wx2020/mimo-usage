"""timerange: TimeRange + year/month window tests (Asia/Shanghai)."""

from __future__ import annotations

import pytest

from mimo_usage.timerange import (
    SHANGHAI,
    BadRequest,
    TimeRange,
    YearMonth,
    format_time,
    now,
    parse_time,
    resolve_range,
    resolve_year_month,
)


def test_shanghai_timezone() -> None:
    assert SHANGHAI.key == "Asia/Shanghai"


def test_parse_time_formats() -> None:
    assert format_time(parse_time("2026-09-23 10:00:00")) == "2026-09-23 10:00:00"
    assert format_time(parse_time("2026-09-23")) == "2026-09-23 00:00:00"
    assert format_time(parse_time("2026-09-23T10:30")) == "2026-09-23 10:30:00"


def test_parse_time_rejects_garbage() -> None:
    with pytest.raises(BadRequest, match="无法解析"):
        parse_time("not-a-date")


def test_resolve_range_defaults_align_midnight() -> None:
    window = resolve_range(None, None, default_days=7, max_days=366)
    assert window.start.hour == 0 and window.start.minute == 0
    # 末对齐到整点 59:59，保持缓存键稳定
    assert window.end.minute == 59 and window.end.second == 59
    assert window.end.tzinfo is not None


def test_resolve_range_rejects_inverted_window() -> None:
    with pytest.raises(BadRequest, match="必须早于"):
        resolve_range("2026-09-23 00:00:00", "2026-09-01 00:00:00", default_days=7, max_days=366)


def test_resolve_range_rejects_too_long() -> None:
    with pytest.raises(BadRequest, match="时间跨度"):
        resolve_range("2020-01-01 00:00:00", "2026-09-23 00:00:00", default_days=7, max_days=366)


def test_resolve_range_rejects_future_end() -> None:
    with pytest.raises(BadRequest, match="不能超过"):
        resolve_range(None, "2030-01-01 00:00:00", default_days=7, max_days=366)


def test_time_range_as_dict() -> None:
    window = resolve_range("2026-09-01 00:00:00", "2026-09-07 23:59:59", default_days=7, max_days=366)
    data = window.as_dict()
    assert data["startTime"] == "2026-09-01T00:00:00+08:00"
    assert data["days"] == 7.0


def test_year_month_defaults_to_current_year() -> None:
    window = resolve_year_month(None, None)
    assert window.year == now().year
    assert window.month is None
    assert window.key == (now().year, 0)


def test_year_month_stable_key() -> None:
    a = resolve_year_month("2026", "9")
    b = resolve_year_month("2026", "09")
    assert a.key == b.key == (2026, 9)
    assert a.label() == "2026-09"
    assert a.as_dict() == {"year": 2026, "month": 9}


def test_year_month_validation() -> None:
    with pytest.raises(BadRequest, match="year"):
        resolve_year_month("abc", None)
    with pytest.raises(BadRequest, match="2000"):
        resolve_year_month("1999", None)
    with pytest.raises(BadRequest, match="month"):
        resolve_year_month("2026", "13")
    with pytest.raises(BadRequest, match="month"):
        resolve_year_month("2026", "0")


def test_year_month_rejects_future_month_of_current_year() -> None:
    current = now()
    if current.month >= 12:
        pytest.skip("12 月无当年未来月")
    with pytest.raises(BadRequest, match="尚未到来"):
        resolve_year_month(str(current.year), str(current.month + 1))


def test_year_month_allows_past_and_current_month() -> None:
    current = now()
    assert resolve_year_month(str(current.year), str(current.month)).month == current.month
    assert resolve_year_month("2025", "12").key == (2025, 12)


def test_year_month_empty_string_is_default() -> None:
    window = resolve_year_month("", "")
    assert window.year == now().year
    assert window.month is None


def test_timerange_type() -> None:
    window = resolve_range(None, None, default_days=3, max_days=30)
    assert isinstance(window, TimeRange)
    assert isinstance(YearMonth(year=2026, month=1), YearMonth)

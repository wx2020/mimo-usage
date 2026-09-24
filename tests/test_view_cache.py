"""端点级视图缓存（整响应）+ ETag / If-None-Match 条件请求。

覆盖：命中跳过 loader、days/fields 区隔缓存键、stale 兜底、鉴权错误不吃 stale、
``?refresh=1`` 绕过与限流、ETag 相同→304 / 变化→200、/metrics 视图缓存观测。
"""

from __future__ import annotations

import time

import pytest
from conftest import (
    AUTH_VERIFICATION_PATH,
    BALANCE_PATH,
    TOKEN_PLAN_DETAIL_PATH,
    TOKEN_PLAN_USAGE_PATH,
    USAGE_PATH,
    USAGE_TREND_PATH,
    USER_PROFILE_PATH,
    FakeUpstream,
    build_app,
)
from sanic import Sanic
from sanic_testing.testing import SanicTestClient

from mimo_usage.timerange import now

# summary 会并发打到的全部上游路径（用于制造“整体失败”）
SUMMARY_PATHS = (
    USAGE_PATH,
    TOKEN_PLAN_DETAIL_PATH,
    TOKEN_PLAN_USAGE_PATH,
    USAGE_TREND_PATH,
    BALANCE_PATH,
    USER_PROFILE_PATH,
    AUTH_VERIFICATION_PATH,
)


@pytest.fixture
def client_for(upstream: FakeUpstream):
    def _make(**overrides) -> SanicTestClient:
        app: Sanic = build_app(upstream, **overrides)
        return SanicTestClient(app, port=None)

    return _make


def set_today_trend(upstream: FakeUpstream, *, tokens: int, requests: int) -> None:
    today = now().date().isoformat()
    upstream.payloads[USAGE_TREND_PATH] = {
        "code": 0,
        "message": "",
        "data": [
            {
                "date": today,
                "model": "mimo-v2.6-flash",
                "totalToken": tokens,
                "inputHitToken": 800,
                "inputMissToken": 100,
                "outputToken": 100,
                "requestCount": requests,
                "inputAudioDuration": 0,
            }
        ],
    }


def test_summary_view_cache_hit_skips_loader(client_for, upstream: FakeUpstream) -> None:
    client = client_for()
    first = client.get("/api/v1/summary")[1]
    assert first.status == 200
    assert first.headers["x-cache"] == "MISS"
    calls_after_first = len(upstream.requests)

    second = client.get("/api/v1/summary")[1]
    assert second.status == 200
    assert second.headers["x-cache"] == "HIT"
    # 命中视图缓存：不再触发任何上游调用，响应体与首次一致
    assert len(upstream.requests) == calls_after_first
    assert second.json["data"] == first.json["data"]


def test_usage_view_keys_include_days_and_fields(client_for) -> None:
    client = client_for()
    assert client.get("/api/v1/usage")[1].headers["x-cache"] == "MISS"
    assert client.get("/api/v1/usage?days=7")[1].headers["x-cache"] == "MISS"
    assert client.get("/api/v1/usage?fields=chart")[1].headers["x-cache"] == "MISS"

    # 三个不同键各自独立命中
    assert client.get("/api/v1/usage")[1].headers["x-cache"] == "HIT"
    assert client.get("/api/v1/usage?days=7")[1].headers["x-cache"] == "HIT"
    assert client.get("/api/v1/usage?fields=chart")[1].headers["x-cache"] == "HIT"

    metrics = client.get("/api/v1/metrics")[1].json["data"]
    assert metrics["caches"]["view"]["entries"] == 3


def test_view_cache_stale_fallback(client_for, upstream: FakeUpstream) -> None:
    """视图过期且分段也已过期耗尽 stale 时，仍用过期视图顶住。"""
    client = client_for(
        view_ttl=0.4,
        stale_ttl=0.4,
        retries=0,
        usage_ttl=0.05,
        detail_ttl=0.05,
        token_plan_ttl=0.05,
        account_ttl=0.05,
    )
    assert client.get("/api/v1/summary")[1].headers["x-cache"] == "MISS"

    time.sleep(0.5)
    for path in SUMMARY_PATHS:
        upstream.fail_paths.add(path)

    _, response = client.get("/api/v1/summary")
    assert response.status == 200
    assert response.headers["x-cache"] == "STALE"


def test_view_cache_auth_error_not_masked_by_stale(client_for, upstream: FakeUpstream) -> None:
    """鉴权错误绝不拿过期视图兜底（allow_stale=False 贯穿到视图缓存）。"""
    client = client_for(
        view_ttl=0.4,
        stale_ttl=0.4,
        retries=0,
        reauth_cooldown=0.0,
        usage_ttl=0.05,
        detail_ttl=0.05,
        token_plan_ttl=0.05,
        account_ttl=0.05,
    )
    assert client.get("/api/v1/summary")[1].status == 200

    time.sleep(0.5)
    upstream.always_unauthorized = True
    upstream.reauth_fails = True

    _, response = client.get("/api/v1/summary")
    assert response.status == 401
    assert response.json["error"]["type"] in {"reauth_failed", "invalid_token"}


def test_view_refresh_bypasses_and_throttles(client_for, upstream: FakeUpstream) -> None:
    client = client_for(view_ttl=600, usage_ttl=600, refresh_min_interval=60)
    assert client.get("/api/v1/summary")[1].headers["x-cache"] == "MISS"

    bypass = client.get("/api/v1/summary?refresh=1")[1]
    assert bypass.headers["x-cache"] == "MISS"

    throttled = client.get("/api/v1/summary?refresh=1")[1]
    assert throttled.headers["x-cache"] == "HIT"
    assert throttled.json["meta"]["refreshThrottled"] is True


def test_view_etag_304_and_change(client_for, upstream: FakeUpstream) -> None:
    client = client_for()
    first = client.get("/api/v1/summary")[1]
    assert first.status == 200
    etag = first.headers["etag"]
    assert etag.startswith('"')

    # 内容未变：If-None-Match 命中 → 304，且带同一 ETag
    second = client.get("/api/v1/summary", headers={"If-None-Match": etag})[1]
    assert second.status == 304
    assert second.headers["etag"] == etag

    # 内容变化（refresh 绕过缓存重建）→ 200 与新 ETag
    set_today_trend(upstream, tokens=987654, requests=9)
    third = client.get("/api/v1/summary?refresh=1", headers={"If-None-Match": etag})[1]
    assert third.status == 200
    assert third.headers["etag"] != etag


def test_metrics_exposes_view_cache(client_for) -> None:
    client = client_for()
    client.get("/api/v1/summary")
    client.get("/api/v1/summary")

    data = client.get("/api/v1/metrics")[1].json["data"]
    assert data["viewCache"]["MISS"] == 1
    assert data["viewCache"]["HIT"] == 1
    assert data["caches"]["view"]["entries"] == 1
    assert data["caches"]["view"]["ttl"] == 30.0

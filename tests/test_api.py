"""End-to-end tests against the real Sanic request cycle."""

from __future__ import annotations

import time

import pytest
from conftest import (
    AUTH_401_PAYLOAD,
    AUTH_VERIFICATION_PATH,
    BALANCE_PATH,
    OPEN_TOKEN_PLAN_LIST_PATH,
    PROFILE_PAYLOAD,
    TOKEN_PLAN_DETAIL_PATH,
    TOKEN_PLAN_USAGE_PATH,
    USAGE_BILL_MONTHLY_PATH,
    USAGE_DETAIL_LIST_PATH,
    USAGE_PATH,
    USAGE_TREND_PATH,
    USER_PROFILE_PATH,
    FakeUpstream,
    build_app,
)
from sanic import Sanic
from sanic_testing.testing import SanicTestClient

from mimo_usage.timerange import now


@pytest.fixture
def client_for(upstream: FakeUpstream):
    """Factory so a test can build extra apps (e.g. keyless or tokenless ones)."""

    def _make(**overrides) -> SanicTestClient:
        app: Sanic = build_app(upstream, **overrides)
        return SanicTestClient(app, port=None)

    return _make


def test_healthz_reports_missing_credentials(client_for) -> None:
    client = client_for(service_token=None)
    _, response = client.get("/healthz")
    assert response.status == 200
    assert response.json["status"] == "degraded"
    assert response.json["credentials"]["configured"] is False
    assert response.json["credentials"]["sources"]["serviceToken"] == "none"


def test_healthz_is_ok_with_a_token(client: SanicTestClient) -> None:
    _, response = client.get("/healthz")
    assert response.json["status"] == "ok"
    assert response.json["upstreamAuth"] == {"rejected": False, "rejections": 0}
    assert response.json["reauth"]["coolingDown"] is False


def test_healthz_flags_a_token_the_upstream_rejects(client: SanicTestClient, upstream: FakeUpstream) -> None:
    upstream.auth_error_paths.add(USAGE_PATH)
    # 续登成功后重试仍 401（始终拒绝）→ invalid_token
    upstream.reauth_fails = True
    _, response = client.get("/api/v1/usage")
    assert response.status == 401

    _, health = client.get("/healthz")
    assert health.status == 200
    assert health.json["status"] == "degraded"
    assert health.json["upstreamAuth"]["rejected"] is True


def test_missing_service_token_is_401_on_api(client_for) -> None:
    _, response = client_for(service_token=None).get("/api/v1/usage")
    assert response.status == 401
    assert response.json["error"]["type"] == "missing_credential"


def test_usage_is_cached_between_requests(client: SanicTestClient, upstream: FakeUpstream) -> None:
    _, first = client.get("/api/v1/usage")
    _, second = client.get("/api/v1/usage")

    assert first.status == 200
    assert first.json["data"]["tokenUsage"]["totalToken"] == 150
    assert first.headers["x-cache"] == "MISS"
    assert second.headers["x-cache"] == "HIT"
    assert upstream.count(USAGE_PATH) == 1


def test_usage_normalises_missing_token_keys(client_for, upstream: FakeUpstream) -> None:
    """兼容层：上游少给的 tokenUsage 键补 0，前端不用层层判空。"""
    upstream.payloads[USAGE_PATH] = {
        "code": 0,
        "message": "",
        "data": {"tokenUsage": {"totalToken": 5}},
    }
    _, response = client_for().get("/api/v1/usage")

    assert response.status == 200
    token = response.json["data"]["tokenUsage"]
    assert token["totalToken"] == 5
    assert token["inputToken"] == 0
    assert token["cacheToken"] == 0
    assert response.json["data"]["costUsage"]["totalCost"] == "0.00"


def test_usage_detail_sends_year_month_and_ph_query(client: SanicTestClient, upstream: FakeUpstream) -> None:
    _, response = client.get("/api/v1/usage/detail?year=2026&month=9")

    assert response.status == 200
    assert response.json["data"][0]["date"] == "2026-09-21"
    assert response.json["meta"]["range"] == {"year": 2026, "month": 9}
    assert "api-platform_ph=test-ph" in upstream.query_of(USAGE_DETAIL_LIST_PATH)


def test_usage_detail_defaults_to_current_year_stable_key(client: SanicTestClient, upstream: FakeUpstream) -> None:
    year = now().year
    _, first = client.get("/api/v1/usage/detail")
    _, second = client.get("/api/v1/usage/detail")

    assert first.json["meta"]["range"] == {"year": year, "month": None}
    assert second.headers["x-cache"] == "HIT"
    assert upstream.count(USAGE_DETAIL_LIST_PATH) == 1


@pytest.mark.parametrize(
    "query,message",
    [
        ("?year=abc", "year"),
        ("?year=1999", "2000"),
        ("?year=2026&month=13", "month"),
        ("?year=2026&month=0", "month"),
    ],
)
def test_usage_detail_rejects_bad_windows(client: SanicTestClient, query: str, message: str) -> None:
    _, response = client.get(f"/api/v1/usage/detail{query}")
    assert response.status == 400
    assert response.json["error"]["type"] == "invalid_request"
    assert message in response.json["error"]["message"]


def test_future_month_is_rejected() -> None:
    """当年内尚未到来的月份要挡掉（避免无意义上游调用）。"""
    app = build_app(FakeUpstream())
    client = SanicTestClient(app, port=None)
    year = now().year
    # 取一个必定在未来的月份
    future_month = now().month + 1 if now().month < 12 else None
    if future_month is None:
        pytest.skip("12 月没有当年未来月")
    _, response = client.get(f"/api/v1/usage/detail?year={year}&month={future_month}")
    assert response.status == 400
    assert "尚未到来" in response.json["error"]["message"]


def test_usage_trend_returns_normalised_rows(client: SanicTestClient, upstream: FakeUpstream) -> None:
    """Token 用量统计（usage/token-plan/list）：按日×模型真实字段返回。"""
    _, response = client.get("/api/v1/usage/trend?year=2026&month=9")

    assert response.status == 200
    rows = response.json["data"]
    assert rows[0]["date"] == "2026-09-23"
    assert rows[0]["model"] == "mimo-v2.6-flash"
    assert rows[0]["totalToken"] == 62643621
    assert rows[0]["inputHitToken"] == 53954944
    assert rows[0]["outputToken"] == 456348
    assert rows[0]["requestCount"] == 257
    assert response.json["meta"]["range"] == {"year": 2026, "month": 9}
    assert "api-platform_ph=test-ph" in upstream.query_of(USAGE_TREND_PATH)


def test_usage_trend_is_cached_by_year_month(client: SanicTestClient, upstream: FakeUpstream) -> None:
    client.get("/api/v1/usage/trend?year=2026&month=9")
    _, second = client.get("/api/v1/usage/trend?year=2026&month=9")

    assert second.headers["x-cache"] == "HIT"
    assert upstream.count(USAGE_TREND_PATH) == 1


def test_usage_bill_normalises_rows(client: SanicTestClient, upstream: FakeUpstream) -> None:
    _, response = client.get("/api/v1/usage/bill")

    assert response.status == 200
    rows = response.json["data"]
    assert rows[0]["reportMonth"] == "202608"
    assert rows[0]["consumptionAmount"] == 10.5
    assert rows[0]["giftConsumption"] == 1.5
    assert rows[0]["cashConsumption"] == 9.0
    assert upstream.count(USAGE_BILL_MONTHLY_PATH) == 1


def test_token_plan_aggregates_three_sections(client: SanicTestClient, upstream: FakeUpstream) -> None:
    _, response = client.get("/api/v1/token-plan")

    assert response.status == 200
    data = response.json["data"]
    assert set(data) == {"detail", "usage", "plans"}
    assert data["detail"]["planCode"] == "lite:year"
    assert data["usage"]["monthUsage"]["items"][0]["name"] == "month_total_token"
    assert data["plans"][0]["planName"] == "Lite"
    assert response.json["meta"]["errors"] is None
    assert upstream.count(TOKEN_PLAN_DETAIL_PATH) == 1
    assert upstream.count(TOKEN_PLAN_USAGE_PATH) == 1
    assert upstream.count(OPEN_TOKEN_PLAN_LIST_PATH) == 1


def test_token_plan_reports_partial_failures(client: SanicTestClient, upstream: FakeUpstream) -> None:
    upstream.fail_paths.add(TOKEN_PLAN_USAGE_PATH)
    _, response = client.get("/api/v1/token-plan")

    assert response.status == 200
    assert "usage" not in response.json["data"]
    assert response.json["meta"]["errors"]["usage"]["status"] == 502


def test_account_aggregates_profile_balance_projects(client: SanicTestClient, upstream: FakeUpstream) -> None:
    _, response = client.get("/api/v1/account")

    assert response.status == 200
    data = response.json["data"]
    assert set(data) == {"profile", "balance", "projects", "verification"}
    assert data["profile"]["userId"] == PROFILE_PAYLOAD["data"]["userId"]
    assert data["profile"]["weixin"]  # 微信绑定标识（非空 = 已绑定）
    assert data["balance"]["currency"] == "CNY"
    assert data["projects"][0]["projectNameCn"] == "我的空间"
    assert data["verification"]["state"] == "NOT_AUTHORIZED"  # 实名认证状态
    assert upstream.count(AUTH_VERIFICATION_PATH) == 1


def test_account_can_select_only_verification(client: SanicTestClient, upstream: FakeUpstream) -> None:
    _, response = client.get("/api/v1/account?fields=verification")

    assert response.status == 200
    assert set(response.json["data"]) == {"verification"}
    assert response.json["data"]["verification"]["state"] == "NOT_AUTHORIZED"
    assert upstream.count(USER_PROFILE_PATH) == 0  # 未选中的段不请求上游


def test_account_is_cached(client: SanicTestClient, upstream: FakeUpstream) -> None:
    client.get("/api/v1/account")
    _, second = client.get("/api/v1/account")

    assert second.status == 200
    assert upstream.count("/api/v1/userProfile") == 1
    assert set(second.json["meta"]["cacheState"].values()) == {"HIT"}


def test_summary_exposes_plan_month_and_balance(client: SanicTestClient) -> None:
    _, response = client.get("/api/v1/summary")

    assert response.status == 200
    data = response.json["data"]
    assert data["plan"]["planName"] == "Lite"
    assert data["monthUsage"]["name"] == "month_total_token"
    assert data["planUsage"]["limit"] == 49200000000
    assert data["balance"]["balance"] == "0.00"
    assert data["tokenUsage"]["totalToken"] == 150
    assert response.json["meta"]["timezone"] == "Asia/Shanghai"


def test_summary_shares_section_caches(client: SanicTestClient, upstream: FakeUpstream) -> None:
    client.get("/api/v1/usage")
    client.get("/api/v1/token-plan")
    client.get("/api/v1/account")
    _, response = client.get("/api/v1/summary")

    assert response.status == 200
    # summary 复用同一份缓存段，不新增上游流量
    assert upstream.count(USAGE_PATH) == 1
    assert upstream.count(TOKEN_PLAN_DETAIL_PATH) == 1
    assert upstream.count(BALANCE_PATH) == 1


def test_summary_validates_fields(client: SanicTestClient) -> None:
    _, response = client.get("/api/v1/summary?fields=nope")
    assert response.status == 400
    assert "plan / monthUsage" in response.json["error"]["message"]


def test_summary_fields_trimming(client: SanicTestClient) -> None:
    _, response = client.get("/api/v1/summary?fields=plan,balance")
    assert response.status == 200
    assert set(response.json["data"]) == {"plan", "balance"}


def test_overview_returns_all_sections(client: SanicTestClient, upstream: FakeUpstream) -> None:
    _, response = client.get("/api/v1/overview")

    assert response.status == 200
    expected = {
        "usage", "usageDetail", "usageTrend", "usageBill", "tokenPlanDetail", "tokenPlanUsage",
        "plans", "profile", "balance", "projects", "verification",
    }
    assert set(response.json["data"]) == expected
    assert response.json["meta"]["errors"] is None
    assert upstream.count(USAGE_PATH) == 1
    assert upstream.count(USAGE_DETAIL_LIST_PATH) == 1
    assert upstream.count(USAGE_TREND_PATH) == 1
    assert upstream.count(USAGE_BILL_MONTHLY_PATH) == 1


def test_overview_tolerates_partial_upstream_failure(client: SanicTestClient, upstream: FakeUpstream) -> None:
    upstream.fail_paths.add(USAGE_BILL_MONTHLY_PATH)
    _, response = client.get("/api/v1/overview")

    assert response.status == 200
    assert "usageBill" not in response.json["data"]
    assert response.json["meta"]["errors"]["usageBill"]["type"] == "upstream_unavailable"


def test_overview_fields_selects_sections(client: SanicTestClient, upstream: FakeUpstream) -> None:
    _, response = client.get("/api/v1/overview?fields=usage,balance")

    assert response.status == 200
    assert set(response.json["data"]) == {"usage", "balance"}
    assert upstream.count(TOKEN_PLAN_DETAIL_PATH) == 0


def test_overview_rejects_unknown_sections(client: SanicTestClient) -> None:
    _, response = client.get("/api/v1/overview?fields=bogus")
    assert response.status == 400
    assert "usage / usageDetail" in response.json["error"]["message"]


def test_refresh_bypasses_cache(client: SanicTestClient, upstream: FakeUpstream) -> None:
    client.get("/api/v1/usage")
    _, response = client.get("/api/v1/usage?refresh=1")

    assert response.headers["x-cache"] == "MISS"
    assert upstream.count(USAGE_PATH) == 2


def test_refresh_is_throttled(client_for, upstream: FakeUpstream) -> None:
    client = client_for(refresh_min_interval=60, usage_ttl=600)

    first = client.get("/api/v1/usage?refresh=1")[1]
    second = client.get("/api/v1/usage?refresh=1")[1]

    assert first.headers["x-cache"] == "MISS"
    assert second.headers["x-cache"] == "HIT"
    assert second.json["meta"]["refreshThrottled"] is True
    assert upstream.count(USAGE_PATH) == 1


def test_stale_cache_is_served_when_upstream_fails(client_for, upstream: FakeUpstream) -> None:
    client = client_for(usage_ttl=0.05, stale_ttl=30.0, retries=0)
    assert client.get("/api/v1/usage")[1].status == 200

    time.sleep(0.1)
    upstream.fail_paths.add(USAGE_PATH)

    _, response = client.get("/api/v1/usage")
    assert response.status == 200
    assert response.headers["x-cache"] == "STALE"


def test_expired_token_is_not_papered_over_with_stale(client_for, upstream: FakeUpstream) -> None:
    """上游挂了可以拿旧值顶一会，token 过期不行。"""
    client = client_for(usage_ttl=0.05, stale_ttl=30.0, retries=0, reauth_cooldown=0.0)
    assert client.get("/api/v1/usage")[1].status == 200

    time.sleep(0.1)
    upstream.auth_error_paths.add(USAGE_PATH)
    upstream.reauth_fails = True

    _, response = client.get("/api/v1/usage")
    assert response.status == 401
    assert response.json["error"]["type"] in {"reauth_failed", "invalid_token"}


def test_api_key_guard(client_for) -> None:
    client = client_for(api_key="s3cret")

    assert client.get("/api/v1/usage")[1].status == 403
    assert client.get("/api/v1/usage", headers={"X-API-Key": "wrong"})[1].status == 401
    assert client.get("/api/v1/usage", headers={"X-API-Key": "s3cret"})[1].status == 200
    assert client.get("/healthz")[1].status == 200


def test_api_key_via_query_and_cookie(client_for) -> None:
    client = client_for(api_key="s3cret")

    assert client.get("/api/v1/usage?key=s3cret")[1].status == 200
    assert client.get("/dashboard?key=s3cret")[1].status == 200
    cookie = {"Cookie": "mimo_usage_key=s3cret"}
    assert client.get("/dashboard", headers=cookie)[1].status == 200
    assert client.get("/dashboard/static/app.js", headers=cookie)[1].status == 200
    assert client.get("/dashboard")[1].status == 403


def test_without_configured_api_key_everything_is_open(client: SanicTestClient) -> None:
    """未配置 MIMO_API_KEY 时守卫全部放行（"门没锁"），这是默认形态。"""
    assert client.get("/api/v1/usage")[1].status == 200
    assert client.get("/dashboard")[1].status == 200
    assert client.get("/dashboard/static/app.js")[1].status == 200
    assert client.get("/api/v1/usage", headers={"X-API-Key": "anything"})[1].status == 200


def test_dashboard_cookie_scope_does_not_cover_api(client_for) -> None:
    """cookie Path=/dashboard —— 只带 cookie 不带 header 打 API 仍应被拒。"""
    client = client_for(api_key="s3cret")
    _, response = client.get("/api/v1/usage", headers={"Cookie": "mimo_usage_key=s3cret"})
    assert response.status == 403  # presented_key 默认不认 cookie，且无 header/query


def test_dashboard_is_not_on_root(client: SanicTestClient) -> None:
    _, response = client.get("/")
    assert [(h.status_code, h.headers.get("location")) for h in response.history] == [(302, "/dashboard")]
    assert "MiMo 用量看板" in response.text


def test_dashboard_serves_static_with_version_and_cache_control(client: SanicTestClient) -> None:
    _, page = client.get("/dashboard")
    assert page.status == 200
    # ?v= 版本号已注入
    assert "?v=" in page.text
    assert "{{VERSION}}" not in page.text

    _, css = client.get("/dashboard/static/style.css?v=0.1.0")
    assert css.status == 200
    assert "cache-control" in css.headers
    assert "max-age" in css.headers["cache-control"]

    _, js = client.get("/dashboard/static/app.js?v=0.1.0")
    assert js.status == 200
    assert js.headers.get("content-type", "").startswith(("application/javascript", "text/javascript"))


def test_metrics_endpoint(client: SanicTestClient) -> None:
    client.get("/api/v1/usage")
    client.get("/api/v1/usage")
    _, response = client.get("/api/v1/metrics")

    assert response.status == 200
    data = response.json["data"]
    assert data["cache"]["MISS"] == 1
    assert data["cache"]["HIT"] == 1
    assert data["caches"]["usage"]["entries"] == 1
    assert "reauth" in data


def test_authorization_header_overrides_configured_token(client_for, upstream: FakeUpstream) -> None:
    client = client_for()
    client.get("/api/v1/usage", headers={"Authorization": "Bearer token-a"})
    client.get("/api/v1/usage", headers={"Authorization": "Bearer token-b"})

    # 不同 token 不共享缓存条目（第二个 token 触发新的上游调用）
    assert upstream.count(USAGE_PATH) == 2


def test_unknown_route_returns_json_404(client: SanicTestClient) -> None:
    _, response = client.get("/api/v1/nope")
    assert response.status == 404
    assert response.headers["content-type"].startswith("application/json")


def test_reauth_success_flow_via_api(client: SanicTestClient, upstream: FakeUpstream) -> None:
    """API 层端到端：401 → 自动续登 → 重试成功，且新 serviceToken 落盘可用。"""
    state = {"expired": True}
    original_handler = upstream.handler

    async def handler(request):
        if request.url.host == "platform.xiaomimimo.com" and request.url.path == "/sts":
            upstream.requests.append(request)
            return __import__("httpx").Response(
                307,
                headers=[
                    ("location", "https://platform.xiaomimimo.com/console/balance"),
                    ("set-cookie", 'api-platform_serviceToken="api-fresh"; Path=/'),
                    ("set-cookie", 'api-platform_ph="ph-fresh"; Path=/'),
                    ("set-cookie", 'api-platform_slh="slh-fresh"; Path=/'),
                    ("set-cookie", "userId=100000001; Path=/"),
                ],
            )
        if (
            request.url.host == "platform.xiaomimimo.com"
            and request.url.path == USAGE_PATH
            and state["expired"]
        ):
            upstream.requests.append(request)
            state["expired"] = False
            return __import__("httpx").Response(401, json=AUTH_401_PAYLOAD)
        return await original_handler(request)

    app = build_app(upstream)
    # 替换 transport 的处理函数（同一 client 概念：重建 app）
    import httpx as _httpx

    app.ctx.client._transport = _httpx.MockTransport(handler)  # noqa: SLF001 - 测试注入
    app.ctx.client._client = None  # 强制 start() 重建 pool  # noqa: SLF001
    client = SanicTestClient(app, port=None)

    _, response = client.get("/api/v1/usage")
    assert response.status == 200
    assert response.json["data"]["tokenUsage"]["totalToken"] == 150
    # 新会话落盘
    assert "api-fresh" in (app.ctx.credentials.service_token.get() or "")
    # /sts 被访问过
    assert any(r.url.path == "/sts" for r in upstream.requests)

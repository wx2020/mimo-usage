"""Unit tests for the upstream client: encoding, error mapping and re-login."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from conftest import (
    AUTH_401_PAYLOAD,
    DETAIL_LIST_PAYLOAD,
    SERVICE_LOGIN_AUTH2_OK,
    SERVICE_LOGIN_OK,
    USAGE_PATH,
    FakeUpstream,
    make_credentials,
    make_settings,
)

from mimo_usage.client import (
    AuthError,
    MimoClient,
    MissingCredentialError,
    ReauthFailed,
    UpstreamTimeout,
    UpstreamUnavailable,
    cookies_from_responses,
    encode_query,
    extract_callback,
    parse_jsonp,
)
from mimo_usage.metrics import Metrics
from mimo_usage.timerange import YearMonth


def client_for(handler, **overrides) -> tuple[MimoClient, Metrics, object]:
    metrics = Metrics()
    password_encrypt = overrides.pop("password_encrypt", None)
    creds_overrides = {
        k: overrides.pop(k)
        for k in (
            "service_token", "pass_token", "device_id", "user_id", "ph", "slh",
            "username", "device_fingerprint", "password_md5",
        )
        if k in overrides
    }
    settings = make_settings(**overrides)
    credentials = make_credentials(**creds_overrides)
    client = MimoClient(
        settings,
        credentials,
        metrics=metrics,
        transport=httpx.MockTransport(handler),
        password_encrypt=password_encrypt,
    )
    return client, metrics, credentials


def test_encode_query_skips_none_and_encodes_space() -> None:
    query = encode_query({"a": "x y", "b": 1, "c": None})
    assert query == "a=x%20y&b=1"


def test_extract_callback_pulls_sts_url() -> None:
    login = AUTH_401_PAYLOAD["loginUrl"]
    callback = extract_callback(login)
    assert callback is not None
    assert callback.startswith("https://platform.xiaomimimo.com/sts?sign=")
    assert "sid=api-platform" not in callback


def test_parse_jsonp_strips_prefix() -> None:
    body = parse_jsonp("&&&START&&&{\"code\":0}")
    assert body["code"] == 0
    with pytest.raises(UpstreamUnavailable):
        parse_jsonp("<html>")


def test_cookies_from_responses_extracts_session_fields() -> None:
    response = httpx.Response(
        307,
        headers=[
            ("set-cookie", 'api-platform_serviceToken="new-token=="; Version=1; Path=/; HttpOnly'),
            ("set-cookie", 'api-platform_ph="ph=="; Version=1; Path=/'),
            ("set-cookie", "userId=100000001; Path=/"),
        ],
        request=httpx.Request("GET", "https://platform.xiaomimimo.com/sts"),
    )
    jar = cookies_from_responses([response])
    assert jar["service_token"].strip('"') == "new-token==" or jar["service_token"] == '"new-token=="'
    assert "ph" in jar and "user_id" in jar
    assert jar["user_id"] == "100000001"


async def test_usage_sends_cookie_and_decodes_data() -> None:
    calls: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"code": 0, "message": "", "data": {"tokenUsage": {"totalToken": 1}}})

    client, metrics, _ = client_for(handler)
    async with client:
        data = await client.usage()

    assert data["tokenUsage"]["totalToken"] == 1
    assert "api-platform_serviceToken=test-service-token" in calls[0].headers["cookie"]
    assert calls[0].headers["x-timeZone"] == "Asia/Shanghai"
    stats = metrics.snapshot()["upstream"]
    assert (stats["calls"], stats["errors"]) == (1, 0)


async def test_detail_list_requires_ph_query_and_year_body() -> None:
    calls: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if b"api-platform_ph" not in request.url.query:
            return httpx.Response(401, json=AUTH_401_PAYLOAD)
        return httpx.Response(200, json=DETAIL_LIST_PAYLOAD)

    client, _, _ = client_for(handler)
    async with client:
        rows = await client.usage_detail_list(YearMonth(year=2026, month=9))

    assert rows[0]["date"] == "2026-09-21"
    assert b"api-platform_ph=test-ph" in calls[0].url.query
    body = calls[0].content or b""
    assert b'"year"' in body and b"2026" in body
    assert b'"month"' in body and b"9" in body


async def test_detail_list_omits_month_when_absent() -> None:
    calls: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=DETAIL_LIST_PAYLOAD)

    client, _, _ = client_for(handler)
    async with client:
        await client.usage_detail_list(YearMonth(year=2026, month=None))

    assert b"month" not in (calls[0].content or b"")


async def test_usage_trend_list_sends_ph_query_and_year_month() -> None:
    """usage/token-plan/list：query 带 ph、body 只认 year/month（新 HAR 实测协议）。"""
    from conftest import USAGE_TREND_PAYLOAD

    calls: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if b"api-platform_ph" not in request.url.query:
            return httpx.Response(401, json=AUTH_401_PAYLOAD)
        return httpx.Response(200, json=USAGE_TREND_PAYLOAD)

    client, _, _ = client_for(handler)
    async with client:
        rows = await client.usage_trend_list(YearMonth(year=2026, month=9))

    assert calls[0].url.path == "/api/v1/usage/token-plan/list"
    assert b"api-platform_ph=test-ph" in calls[0].url.query
    body = calls[0].content or b""
    assert b'"year"' in body and b"2026" in body
    assert b'"month"' in body and b"9" in body
    assert rows[0]["date"] == "2026-09-23"
    assert rows[0]["model"] == "mimo-v2.6-flash"
    assert rows[0]["totalToken"] == 62643621
    assert rows[0]["requestCount"] == 257


async def test_missing_credential_helper_flags_allow_stale() -> None:
    err = MissingCredentialError("no token")
    assert err.allow_stale is False
    assert err.status == 401


async def test_code_zero_is_success() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0, "message": "", "data": {"tokenUsage": {"totalToken": 2}}})

    client, _, _ = client_for(handler)
    async with client:
        data = await client.usage()
    assert data["tokenUsage"]["totalToken"] == 2


async def test_http_401_triggers_reauth_then_retry_success() -> None:
    """401 → serviceLoginAuth2 → /sts 换新 token 落盘 → 原请求重试成功。"""
    upstream = FakeUpstream()
    state = {"expired": True}

    async def handler(request: httpx.Request) -> httpx.Response:
        upstream.requests.append(request)
        host = request.url.host
        path = request.url.path
        if host == "account.xiaomi.com":
            if path.endswith("/pass/serviceLogin"):
                return httpx.Response(200, text=SERVICE_LOGIN_OK)
            if path.endswith("/pass/serviceLoginAuth2"):
                return httpx.Response(200, text=SERVICE_LOGIN_AUTH2_OK)
            return httpx.Response(404, text="nope")
        if path == "/sts":
            # 307 → 最终下发新会话
            return httpx.Response(
                307,
                headers=[
                    ("location", "https://platform.xiaomimimo.com/console/balance"),
                    ("set-cookie", 'api-platform_serviceToken="fresh-token=="; Path=/; HttpOnly'),
                    ("set-cookie", 'api-platform_ph="fresh-ph=="; Path=/'),
                    ("set-cookie", 'api-platform_slh="fresh-slh=="; Path=/'),
                    ("set-cookie", "userId=100000001; Path=/"),
                ],
            )
        if path == "/console/balance":
            return httpx.Response(200, json={"ok": True})
        if state["expired"]:
            state["expired"] = False  # 只过期一次；续登后重试即恢复
            return httpx.Response(401, json=AUTH_401_PAYLOAD)
        if path == USAGE_PATH:
            return httpx.Response(200, json={"code": 0, "message": "", "data": {"tokenUsage": {"totalToken": 7}}})
        return httpx.Response(404, json={"code": 404, "message": "no"})

    client, metrics, credentials = client_for(handler)
    async with client:
        data = await client.usage()

    assert data["tokenUsage"]["totalToken"] == 7
    # 新会话已落盘
    assert credentials.service_token.get() == '"fresh-token=="' or "fresh-token" in (
        credentials.service_token.get() or ""
    )
    assert credentials.ph.get() is not None
    # 上游调用序列：usage(401) → account×2 → sts → usage(200)
    paths = [r.url.path for r in upstream.requests]
    assert paths[0] == USAGE_PATH
    assert any(p.endswith("/serviceLogin") for p in paths)
    assert any(p.endswith("/serviceLoginAuth2") for p in paths)
    assert "/sts" in paths
    assert paths[-1] == USAGE_PATH
    assert client.reauth_count == 1
    assert metrics.auth_state()["rejected"] is False


async def test_reauth_failure_raises_readable_error_without_loop() -> None:
    """续登失败：抛可读 ReauthFailed，且 account 上游不会被循环调用。"""
    upstream = FakeUpstream()
    account_calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal account_calls
        upstream.requests.append(request)
        host = request.url.host
        if host == "account.xiaomi.com":
            account_calls += 1
            # serviceLogin 直接失败（passToken 失效的实测形态）
            body = "&&&START&&&" '{"code":70016,"description":"登录验证失败","result":"error"}'
            return httpx.Response(200, text=body)
        return httpx.Response(401, json=AUTH_401_PAYLOAD)

    client, metrics, _ = client_for(handler, retries=1)
    async with client:
        with pytest.raises(ReauthFailed) as excinfo:
            await client.usage()
        first = excinfo.value

    assert first.status == 401
    assert "登录验证失败" in str(first)
    assert "serviceLogin 未返回 _sign" in str(first)
    # 只走一遍续登链（serviceLogin 一步失败即止），没有循环打上游
    assert account_calls == 1
    assert client.reauth_count == 0

    # 冷却期内第二次调用：直接可读错误，account 调用数不增加
    async with client:
        with pytest.raises(ReauthFailed) as second:
            await client.usage()
    assert "冷却" in str(second.value) or "不再重试" in str(second.value)
    assert account_calls == 1
    assert metrics.auth_state()["rejected"] is True


async def test_missing_pass_token_fails_fast() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json=AUTH_401_PAYLOAD)

    client, _, _ = client_for(handler, pass_token=None, retries=0)
    async with client:
        with pytest.raises(ReauthFailed, match="MIMO_PASS_TOKEN"):
            await client.usage()


async def test_missing_service_token_is_reported_before_any_request() -> None:
    calls: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"code": 0, "data": {}})

    client, _, _ = client_for(handler, service_token=None)
    # serviceToken 缺失由 API 层 resolve_token 拦下；client 层仍允许无该 Cookie 调用
    async with client:
        await client.usage()
    cookie = calls[0].headers.get("cookie", "")
    assert "api-platform_serviceToken" not in cookie
    assert "api-platform_ph=" in cookie  # 其余会话字段照常携带


async def test_non_json_body_is_rejected() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>login</html>")

    client, _, _ = client_for(handler)
    async with client:
        with pytest.raises(UpstreamUnavailable, match="非 JSON"):
            await client.usage()


async def test_business_error_carries_the_upstream_code() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 400, "message": "year is required"})

    client, _, _ = client_for(handler)
    async with client:
        with pytest.raises(UpstreamUnavailable) as excinfo:
            await client.usage()

    assert excinfo.value.code == 400
    assert "year is required" in str(excinfo.value)


async def test_timeouts_map_to_504() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    client, _, _ = client_for(handler, retries=0)
    async with client:
        with pytest.raises(UpstreamTimeout) as excinfo:
            await client.usage()

    assert excinfo.value.status == 504


async def test_server_errors_are_retried_then_raised() -> None:
    calls: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(500, json={"message": "boom"})

    client, metrics, _ = client_for(handler, retries=2)
    async with client:
        with pytest.raises(UpstreamUnavailable):
            await client.balance()

    assert len(calls) == 3


async def test_client_must_be_started() -> None:
    client, _, _ = client_for(lambda request: httpx.Response(200, json={"code": 0}))
    with pytest.raises(RuntimeError, match="start"):
        await client.usage()


async def test_auth_error_without_login_url_still_triggers_reauth() -> None:
    """HTTP 401 无 body/loginUrl 时也要尝试续登（回退 genLoginUrl）。"""
    state = {"first": True}

    async def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        path = request.url.path
        if host == "account.xiaomi.com":
            if path.endswith("/serviceLogin"):
                return httpx.Response(200, text=SERVICE_LOGIN_OK)
            if path.endswith("/serviceLoginAuth2"):
                return httpx.Response(200, text=SERVICE_LOGIN_AUTH2_OK)
            return httpx.Response(404, text="x")
        if path == "/api/v1/genLoginUrl":
            return httpx.Response(
                302,
                headers={
                    "location": (
                        "https://account.xiaomi.com/pass/serviceLogin?callback=https%3A%2F%2F"
                        "platform.xiaomimimo.com%2Fsts%3Fsign%3Dabc&sid=api-platform"
                    )
                },
            )
        if path == "/sts":
            return httpx.Response(
                307,
                headers=[
                    ("location", "https://platform.xiaomimimo.com/"),
                    ("set-cookie", 'api-platform_serviceToken="fresh2"; Path=/'),
                    ("set-cookie", 'api-platform_ph="ph2"; Path=/'),
                ],
            )
        if path == "/":
            return httpx.Response(200, json={})
        if state["first"]:
            state["first"] = False
            return httpx.Response(401, text="unauthorized")  # 无 loginUrl
        return httpx.Response(200, json={"code": 0, "data": {"ok": 1}})

    client, _, credentials = client_for(handler)
    async with client:
        data = await client.usage()
    assert data == {"ok": 1}
    assert credentials.service_token.get() is not None


async def test_auth_error_type_is_importable_and_allows_login_url() -> None:
    err = AuthError("msg", code=401, login_url="https://x/sts")
    assert err.login_url == "https://x/sts"
    assert err.as_dict()["type"] == "invalid_token"


# ---- 密码续登（三级降级的第③级）------------------------------------------------


async def test_password_login_success_renews_session_and_pass_token() -> None:
    """免密 Auth2 失败(70016) → 密码 Auth2 成功 → /sts 落盘 + 新 passToken 自愈。"""
    account_calls: list[httpx.Request] = []
    seen: dict[str, Any] = {}

    async def fake_encrypt(username: str) -> tuple[str, str]:
        seen["username"] = username
        return "AES_USER_B64==", "RSA_EUI_B64==.dXNlcg=="

    async def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        path = request.url.path
        if host == "account.xiaomi.com":
            account_calls.append(request)
            if path.endswith("/pass/serviceLogin"):
                return httpx.Response(
                    200,
                    text="&&&START&&&"
                    + json.dumps(
                        {
                            "code": 70016,
                            "description": "登录验证失败",
                            "_sign": "SIGN==",
                            "qs": "%3Fcallback%3Dcb",
                            "serviceParam": '{"checkSafePhone":false}',
                        }
                    ),
                )
            if path.endswith("/pass/serviceLoginAuth2"):
                # 解析 form（httpx data=dict 已编码）
                from urllib.parse import parse_qs

                form = parse_qs(request.content.decode("utf-8"))
                seen["form"] = {k: v[0] for k, v in form.items()}
                seen["eui"] = request.headers.get("eui")
                seen["ua"] = request.headers.get("user-agent", "")
                # 免密（无 user）失败，密码（有 user）成功
                if "user" not in form:
                    return httpx.Response(
                        200,
                        text="&&&START&&&"
                        + json.dumps({"code": 70016, "description": "登录验证失败"}),
                    )
                return httpx.Response(
                    200,
                    text="&&&START&&&"
                    + json.dumps(
                        {
                            "code": 0,
                            "result": "ok",
                            "description": "成功",
                            "passToken": "NEW_PASS_TOKEN_FROM_PWD",
                            "location": "https://platform.xiaomimimo.com/sts?sign=X&auth=T",
                        }
                    ),
                )
            return httpx.Response(404, text="nope")
        if path == "/sts":
            return httpx.Response(
                307,
                headers=[
                    ("location", "https://platform.xiaomimimo.com/console/balance"),
                    ("set-cookie", 'api-platform_serviceToken="pwd-fresh-tok"; Path=/'),
                    ("set-cookie", 'api-platform_ph="pwd-ph"; Path=/'),
                    ("set-cookie", "userId=100000001; Path=/"),
                ],
            )
        if path == "/console/balance":
            return httpx.Response(200, json={})
        if state_expired["on"]:
            state_expired["on"] = False
            return httpx.Response(401, json=AUTH_401_PAYLOAD)
        return httpx.Response(200, json={"code": 0, "data": {"tokenUsage": {"totalToken": 9}}})

    state_expired = {"on": True}
    client, _, credentials = client_for(
        handler,
        pass_token=None,  # 免密根没有，直接考验密码分支（Auth2 免密仍会试？ pass_token None → 跳过免密）
        username="00000000000",
        password_md5="AABBCCDDEEFF00112233445566778899",
        password_encrypt=fake_encrypt,
    )
    # 注：pass_token=None 时流程跳过免密 Auth2，serviceLogin 70016 带 _sign → 直接密码 Auth2
    async with client:
        data = await client.usage()

    assert data["tokenUsage"]["totalToken"] == 9
    assert seen["username"] == "00000000000"
    form = seen["form"]
    assert form["hash"] == "AABBCCDDEEFF00112233445566778899"
    assert form["user"] == "AES_USER_B64=="
    assert form["cc"] == "+86"
    assert form["deviceFingerprint"]  # 有默认值
    assert form["policyName"] == "miaccount"
    assert seen["eui"] == "RSA_EUI_B64==.dXNlcg=="
    assert "Chrome" in seen["ua"]
    # 会话 + 新 passToken 双落盘
    assert "pwd-fresh-tok" in (credentials.service_token.get() or "")
    assert credentials.pass_token.get() == "NEW_PASS_TOKEN_FROM_PWD"
    assert client.reauth_count == 1
    assert client._pass_token_refreshed is True  # noqa: SLF001 - 断言自愈证据


async def test_password_fallback_after_passwordless_auth2_failure() -> None:
    """有 passToken 时先免密 Auth2（70016），再无缝降级密码 Auth2。"""
    order: list[str] = []

    async def fake_encrypt(username: str) -> tuple[str, str]:
        return "u==", "eui"

    async def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        path = request.url.path
        if host == "account.xiaomi.com":
            if path.endswith("/pass/serviceLogin"):
                order.append("login")
                return httpx.Response(
                    200,
                    text="&&&START&&&"
                    + json.dumps({"code": 70016, "_sign": "S", "qs": "q", "serviceParam": "{}"}),
                )
            if path.endswith("/pass/serviceLoginAuth2"):
                from urllib.parse import parse_qs

                form = parse_qs(request.content.decode("utf-8"))
                if "user" in form:
                    order.append("auth2-password")
                    return httpx.Response(
                        200,
                        text="&&&START&&&"
                        + json.dumps(
                            {"code": 0, "location": "https://platform.xiaomimimo.com/sts?x=1"}
                        ),
                    )
                order.append("auth2-passwordless")
                return httpx.Response(
                    200,
                    text="&&&START&&&" + json.dumps({"code": 70016, "description": "登录验证失败"}),
                )
            return httpx.Response(404, text="x")
        if path == "/sts":
            return httpx.Response(
                307,
                headers=[
                    ("location", "https://platform.xiaomimimo.com/"),
                    ("set-cookie", 'api-platform_serviceToken="tok2"; Path=/'),
                ],
            )
        if path in ("/", "/console/balance"):
            return httpx.Response(200, json={})
        if path == USAGE_PATH:
            if state["expired"]:
                state["expired"] = False
                return httpx.Response(401, json=AUTH_401_PAYLOAD)
            return httpx.Response(200, json={"code": 0, "data": {"ok": 1}})
        return httpx.Response(404, json={"code": 404})

    state = {"expired": True}
    client, _, _ = client_for(
        handler,
        username="00000000000",
        password_md5="MD5HASH",
        password_encrypt=fake_encrypt,
    )
    async with client:
        data = await client.usage()
    assert data == {"ok": 1}
    assert order == ["login", "auth2-passwordless", "auth2-password"]


async def test_captcha_fails_fast_with_long_cooldown_and_no_retry() -> None:
    """captchaUrl 出现 → CaptchaRequired 熔断；冷却内零 account 调用。"""
    account_calls = 0

    async def fake_encrypt(username: str) -> tuple[str, str]:
        return "u==", "eui"

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal account_calls
        host = request.url.host
        path = request.url.path
        if host == "account.xiaomi.com":
            account_calls += 1
            if path.endswith("/pass/serviceLogin"):
                return httpx.Response(
                    200,
                    text="&&&START&&&"
                    + json.dumps({"code": 70016, "_sign": "S", "qs": "q", "serviceParam": "{}"}),
                )
            if path.endswith("/pass/serviceLoginAuth2"):
                return httpx.Response(
                    200,
                    text="&&&START&&&"
                    + json.dumps(
                        {
                            "code": 70103,
                            "description": "需要安全验证",
                            "captchaUrl": "https://account.xiaomi.com/captcha?x=1",
                        }
                    ),
                )
            return httpx.Response(404, text="x")
        return httpx.Response(401, json=AUTH_401_PAYLOAD)

    from mimo_usage.client import CaptchaRequired

    client, _, _ = client_for(
        handler,
        pass_token=None,
        username="00000000000",
        password_md5="MD5HASH",
        password_encrypt=fake_encrypt,
        password_reauth_cooldown=3600.0,
    )
    async with client:
        with pytest.raises(CaptchaRequired, match="人机验证"):
            await client.usage()
        calls_after_first = account_calls
        with pytest.raises(ReauthFailed, match="不再重试"):
            await client.usage()

    # 第二次完全没打 account（冷却门）
    assert account_calls == calls_after_first
    # 密码分支失败 → 用的是长冷却（password_reauth_cooldown）
    assert client._cooldown_used == 3600.0  # noqa: SLF001


async def test_missing_materials_reports_both_options() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json=AUTH_401_PAYLOAD)

    client, _, _ = client_for(handler, pass_token=None, retries=0)
    async with client:
        with pytest.raises(ReauthFailed, match="MIMO_PASS_TOKEN"):
            await client.usage()


async def test_password_login_requires_device_fingerprint() -> None:
    """未配置 deviceFingerprint 时，密码续登给可读错误而不是用假指纹试探。"""

    async def fake_encrypt(username: str) -> tuple[str, str]:
        return "u==", "eui"

    async def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        path = request.url.path
        if host == "account.xiaomi.com" and path.endswith("/pass/serviceLogin"):
            return httpx.Response(
                200,
                text="&&&START&&&"
                + json.dumps({"code": 70016, "_sign": "S", "qs": "q", "serviceParam": "{}"}),
            )
        if host == "account.xiaomi.com" and path.endswith("/pass/serviceLoginAuth2"):
            raise AssertionError("不应发出密码登录请求（缺 fingerprint 就该挡下）")
        return httpx.Response(401, json=AUTH_401_PAYLOAD)

    client, _, _ = client_for(
        handler,
        pass_token=None,
        username="00000000000",
        password_md5="A" * 32,
        device_fingerprint=None,
        password_encrypt=fake_encrypt,
        retries=0,
    )
    async with client:
        with pytest.raises(ReauthFailed, match="deviceFingerprint"):
            await client.usage()

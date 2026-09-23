"""Shared fixtures: a fake platform/account upstream and a Sanic app wired to it."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import httpx
import pytest
from sanic import Sanic
from sanic_testing.testing import SanicTestClient

from mimo_usage.app import create_app
from mimo_usage.client import (
    AUTH_VERIFICATION_PATH,
    BALANCE_PATH,
    OPEN_TOKEN_PLAN_LIST_PATH,
    PROJECTS_PATH,
    TOKEN_PLAN_DETAIL_PATH,
    TOKEN_PLAN_USAGE_PATH,
    USAGE_BILL_MONTHLY_PATH,
    USAGE_DETAIL_LIST_PATH,
    USAGE_PATH,
    USAGE_TREND_PATH,
    USER_PROFILE_PATH,
)
from mimo_usage.config import CredentialStore, Settings

USAGE_PAYLOAD: dict[str, Any] = {
    "code": 0,
    "message": "",
    "data": {
        "tokenUsage": {
            "inputToken": 100,
            "outputToken": 50,
            "cacheToken": 20,
            "totalToken": 150,
            "inputAudioDuration": 0,
            "batchInputToken": 0,
            "batchOutputToken": 0,
            "batchCacheToken": 0,
            "batchInputAudioDuration": 0,
        },
        "accountRateLimit": {"tpm": 3000000, "rpm": 1000, "queryTpm": 10000000, "concurrency": 100},
        "costUsage": {"totalCost": "12.34", "currentMonthCost": "3.21"},
        "pluginUsage": {"totalRequestCount": "0", "webSearchRequestCount": "0"},
    },
}

DETAIL_LIST_PAYLOAD: dict[str, Any] = {
    "code": 0,
    "message": "",
    "data": [
        {"date": "2026-09-21", "totalToken": 1000, "consumedAmount": 0.1},
        {"date": "2026-09-22", "totalToken": 2000, "consumedAmount": 0.2},
    ],
}

#: usage/token-plan/list 真实行结构（2026-09-23 新 HAR 实测样本）
USAGE_TREND_PAYLOAD: dict[str, Any] = {
    "code": 0,
    "message": "",
    "data": [
        {
            "date": "2026-09-23",
            "model": "mimo-v2.6-flash",
            "totalToken": 62643621,
            "inputHitToken": 53954944,
            "inputMissToken": 8232329,
            "outputToken": 456348,
            "requestCount": 257,
            "inputAudioDuration": 0,
        },
        {
            "date": "2026-09-23",
            "model": "mimo-v2.6-pro",
            "totalToken": 98021,
            "inputHitToken": 4096,
            "inputMissToken": 92926,
            "outputToken": 999,
            "requestCount": 1,
            "inputAudioDuration": 0,
        },
        {
            "date": "2026-09-22",
            "model": "mimo-v2.6-flash",
            "totalToken": 12235,
            "inputHitToken": 64,
            "inputMissToken": 12063,
            "outputToken": 108,
            "requestCount": 4,
            "inputAudioDuration": 0,
        },
        {
            "date": "2026-09-22",
            "model": "mimo-v2.6-pro",
            "totalToken": 3131981,
            "inputHitToken": 3009792,
            "inputMissToken": 100217,
            "outputToken": 21972,
            "requestCount": 44,
            "inputAudioDuration": 0,
        },
    ],
}

BILL_PAYLOAD: dict[str, Any] = {
    "code": 0,
    "message": "",
    "data": [
        {"reportMonth": "202608", "consumptionAmount": "10.50", "giftConsumption": "1.50", "cashConsumption": "9.00"},
        {"reportMonth": "202609", "consumptionAmount": "3.21", "giftConsumption": "0.21", "cashConsumption": "3.00"},
    ],
}

TOKEN_PLAN_DETAIL_PAYLOAD: dict[str, Any] = {
    "code": 0,
    "message": "",
    "data": {
        "planCode": "lite:year",
        "planName": "Lite",
        "currentPeriodEnd": "2027-09-22 23:59:59",
        "expired": False,
        "enableAutoRenew": True,
        "autoRenewDiscount": None,
        "hasAutoRenewSubscribed": True,
        "clawEnabled": False,
        "clawPeriodEnd": None,
        "clawPurchased": False,
    },
}

TOKEN_PLAN_USAGE_PAYLOAD: dict[str, Any] = {
    "code": 0,
    "message": "",
    "data": {
        "monthUsage": {
            "percent": 0.0062,
            "items": [{"name": "month_total_token", "used": 304041752, "limit": 49200000000, "percent": 0.0062}],
        },
        "usage": {
            "percent": 0.01,
            "items": [
                {"name": "plan_total_token", "used": 304041752, "limit": 49200000000, "percent": 0.01},
                {"name": "compensation_total_token", "used": 0, "limit": 0, "percent": 0},
            ],
        },
    },
}

OPEN_PLANS_PAYLOAD: dict[str, Any] = {
    "code": 0,
    "message": "",
    "data": [
        {
            "planCode": "lite",
            "planName": "Lite",
            "planLevel": 1,
            "originalPrice": "39.00",
            "discountPrice": "34.32",
            "planPrice": "39.00",
            "currency": "CNY",
            "tokenQuotaCn": "41 亿 Credits",
            "tokenQuotaEn": "4.1 Billion Credits",
            "periodInterval": 1,
            "active": True,
            "descriptionCn": "尝鲜入门",
            "descriptionEn": "Starter Pack",
        }
    ],
}

BALANCE_PAYLOAD: dict[str, Any] = {
    "code": 0,
    "message": "",
    "data": {
        "balance": "0.00",
        "frozenBalance": "0.00",
        "currency": "CNY",
        "overdraftLimit": "0.00",
        "remainingOverdraftLimit": "0.00",
        "giftBalance": "0.00",
        "cashBalance": "0.00",
    },
}

PROFILE_PAYLOAD: dict[str, Any] = {
    "code": 0,
    "message": "",
    "data": {
        "userId": "100000001",
        "phone": "+86 138****0000",
        "email": "demo***@example.com",
        "platformEmail": None,
        "weixin": "wxid_demo0000000000@WEIXIN",
        "agreement": True,
        "idc": 310,
        "nickName": None,
        "userName": None,
    },
}

PROJECTS_PAYLOAD: dict[str, Any] = {
    "code": 0,
    "message": "",
    "data": {
        "projects": [
            {"projectId": None, "projectNameCn": "我的空间", "projectNameEn": "My Space", "role": "OWNER"}
        ]
    },
}

#: 实名认证状态（/api/v1/auth/verificationStatus 实测形态）
AUTH_VERIFICATION_PAYLOAD: dict[str, Any] = {
    "code": 0,
    "message": "",
    "data": {
        "userVerificationState": "NOT_AUTHORIZED",
        "authorizeUrl": "https://certify.mipay.com/authorization/callUserAuthorize?partnerId=900000005546",
        "cardType": None,
        "realName": None,
        "cardNo": None,
        "industry": None,
        "authTime": None,
    },
}

#: 401 body，verbatim（2026-09-23 实测形态）
AUTH_401_PAYLOAD: dict[str, Any] = {
    "code": 401,
    "loginUrl": (
        "https://account.xiaomi.com/pass/serviceLogin?callback=https%3A%2F%2Fplatform.xiaomimimo.com"
        "%2Fsts%3Fsign%3DDEMO_SIGN%253D%26followup%3Dhttp%253A%252F%252F"
        "platform.xiaomimimo.com%2Fapi%252Fv1%252Fusage&sid=api-platform&_group=DEFAULT"
    ),
}

SERVICE_LOGIN_OK = "&&&START&&&" + (
    '{"code":0,"_sign":"SIGN==","qs":"%3Fcallback%3Dcb","serviceParam":"{}",'
    '"description":"成功","sid":"api-platform","result":"ok"}'
)

SERVICE_LOGIN_AUTH2_OK = "&&&START&&&" + (
    '{"code":0,"result":"ok","description":"成功","location":'
    '"https://platform.xiaomimimo.com/sts?sign=X&auth=TICKET"}'
)


class FakeUpstream:
    """In-memory stand-in for platform.xiaomimimo.com + account.xiaomi.com."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.fail_paths: set[str] = set()
        self.auth_error_paths: set[str] = set()
        self.delay = 0.0
        self.inflight = 0
        self.max_inflight = 0
        #: True = 上游一律 401（触发续登链）
        self.always_unauthorized = False
        #: True = 续登链失败（serviceLogin 返回 70016）
        self.reauth_fails = False
        self.payloads: dict[str, dict[str, Any]] = {
            USAGE_PATH: USAGE_PAYLOAD,
            USAGE_DETAIL_LIST_PATH: DETAIL_LIST_PAYLOAD,
            USAGE_TREND_PATH: USAGE_TREND_PAYLOAD,
            USAGE_BILL_MONTHLY_PATH: BILL_PAYLOAD,
            TOKEN_PLAN_DETAIL_PATH: TOKEN_PLAN_DETAIL_PAYLOAD,
            TOKEN_PLAN_USAGE_PATH: TOKEN_PLAN_USAGE_PAYLOAD,
            OPEN_TOKEN_PLAN_LIST_PATH: OPEN_PLANS_PAYLOAD,
            BALANCE_PATH: BALANCE_PAYLOAD,
            USER_PROFILE_PATH: PROFILE_PAYLOAD,
            PROJECTS_PATH: PROJECTS_PAYLOAD,
            AUTH_VERIFICATION_PATH: AUTH_VERIFICATION_PAYLOAD,
        }
        self._expired_once: set[str] = set()

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.inflight += 1
        self.max_inflight = max(self.max_inflight, self.inflight)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            host = request.url.host
            path = request.url.path

            if host == "account.xiaomi.com":
                return await self._account(request, path)

            # platform 侧
            if self.always_unauthorized or path in self.auth_error_paths:
                return httpx.Response(401, json=AUTH_401_PAYLOAD)
            if path in self.fail_paths:
                return httpx.Response(500, json={"message": "upstream exploded"})

            # 每个受保护路径第一次调用返回 401（模拟 serviceToken 中途过期），
            # 续登成功后（credential 已更新）后续调用恢复正常。
            if path in self._expired_once and self._service_token(request) == "fresh-token":
                pass  # 已续登，放行
            payload = self.payloads.get(path)
            if payload is None:
                return httpx.Response(404, json={"code": 404, "message": "No static resource."})
            return httpx.Response(200, json=payload)
        finally:
            self.inflight -= 1

    async def _account(self, request: httpx.Request, path: str) -> httpx.Response:
        if path.endswith("/pass/serviceLogin"):
            if self.reauth_fails:
                body = (
                    "&&&START&&&" '{"code":70016,"description":"登录验证失败","result":"error",'
                    '"_sign":"SIGN==","qs":"qs","serviceParam":"{}"}'
                )
                return httpx.Response(200, text=body)
            return httpx.Response(200, text=SERVICE_LOGIN_OK)
        if path.endswith("/pass/serviceLoginAuth2"):
            if self.reauth_fails:
                body = (
                    "&&&START&&&" '{"code":70016,"description":"登录验证失败","result":"error"}'
                )
                return httpx.Response(200, text=body)
            return httpx.Response(200, text=SERVICE_LOGIN_AUTH2_OK)
        return httpx.Response(404, text="not found")

    def mark_expired(self, *paths: str) -> None:
        """让指定路径在拿到 fresh-token 前一直 401。"""
        self.auth_error_paths.update(paths)

    @staticmethod
    def _service_token(request: httpx.Request) -> str | None:
        cookie = request.headers.get("cookie", "")
        for part in cookie.split(";"):
            part = part.strip()
            if part.startswith("api-platform_serviceToken="):
                return part.split("=", 1)[1].strip().strip('"')
        return None

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def count(self, path: str) -> int:
        return sum(1 for request in self.requests if request.url.path == path)

    def count_host(self, host: str) -> int:
        return sum(1 for request in self.requests if request.url.host == host)

    def query_of(self, path: str, index: int = 0) -> str:
        queries = [r.url.query.decode() for r in self.requests if r.url.path == path]
        return queries[index]


@pytest.fixture
def upstream() -> FakeUpstream:
    return FakeUpstream()


def make_settings(**overrides: Any) -> Settings:
    defaults: dict[str, Any] = {
        "access_log": False,
        "retry_backoff": 0.0,
        "retries": 1,
        "usage_ttl": 60.0,
        "detail_ttl": 60.0,
        "bill_ttl": 60.0,
        "token_plan_ttl": 60.0,
        "account_ttl": 60.0,
        "reauth_cooldown": 300.0,
    }
    defaults.update(overrides)
    return Settings(**defaults)


def make_credentials(**overrides: Any) -> CredentialStore:
    defaults: dict[str, str | None] = {
        "service_token": "test-service-token",
        "pass_token": "test-pass-token",
        "device_id": "wb_test-device",
        "user_id": "100000001",
        "ph": "test-ph",
        "slh": "test-slh",
        "username": None,
        "device_fingerprint": "test-fingerprint",
        "password_md5": None,
    }
    defaults.update(overrides)
    return CredentialStore.from_values(**defaults)


def build_app(upstream: FakeUpstream | None = None, **overrides: Any) -> Sanic:
    settings_keys = {
        "access_log", "retry_backoff", "retries", "usage_ttl", "detail_ttl", "bill_ttl",
        "token_plan_ttl", "account_ttl", "stale_ttl", "cache_max_entries",
        "refresh_min_interval", "summary_max_age", "dashboard_path", "api_key",
        "reauth_cooldown", "password_reauth_cooldown", "default_usage_days",
        "max_range_days", "debug", "request_timeout", "connect_timeout",
    }
    settings_overrides = {k: v for k, v in overrides.items() if k in settings_keys}
    settings = make_settings(**settings_overrides)

    cred_overrides = {
        k: overrides[k]
        for k in (
            "service_token", "pass_token", "device_id", "user_id", "ph", "slh",
            "username", "device_fingerprint", "password_md5",
        )
        if k in overrides
    }
    credentials = make_credentials(**cred_overrides)

    name = f"mimo_usage_test_{uuid.uuid4().hex[:8]}"
    transport = upstream.transport if upstream is not None else None
    return create_app(settings, name=name, credentials=credentials, transport=transport)


@pytest.fixture
def app(upstream: FakeUpstream) -> Sanic:
    return build_app(upstream)


@pytest.fixture
def client(app: Sanic) -> SanicTestClient:
    return SanicTestClient(app, port=None)

"""Async client for platform.xiaomimimo.com (read-only usage endpoints).

鉴权与协议（实测校准）：

* Cookie 鉴权：``api-platform_serviceToken``（连同 userId / api-platform_slh /
  api-platform_ph）是 platform 域的会话凭证。
* 成功响应：HTTP 200 + body ``{"code":0, "message":"", "data":...}``。
* 过期响应：HTTP 401（或 200+``code:401``）且 body 带 ``loginUrl``。
* 自动续登三级链：① serviceLogin 免密直通 → ② serviceLoginAuth2（passToken
  免密 form）→ ③ **密码登录**（username AES + EUI + hash=MD5(密码)大写，参数用
  node 内置 crypto 复刻网页登录加密）→ ``/sts`` 换新会话落盘。
  密码登录成功响应自带**新 passToken**，一并落盘（自愈，重新获得 30 天免密根）。
  任何失败进入冷却（密码/验证码类用更长的 ``password_reauth_cooldown``），
  窗口内返回可读错误、不再打 account 上游。
* ``POST /api/v1/usage/detail/list`` 额外要求 query ``api-platform_ph``（网页端
  封装把该 cookie 并进 POST query；cookie 含 ph 时缺它一律 401）。
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from contextlib import suppress
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

import httpx

from .config import CredentialStore, Settings
from .metrics import Metrics
from .timerange import YearMonth

USAGE_PATH = "/api/v1/usage"
USAGE_DETAIL_LIST_PATH = "/api/v1/usage/detail/list"
USAGE_TREND_PATH = "/api/v1/usage/token-plan/list"
USAGE_BILL_MONTHLY_PATH = "/api/v1/usage/bill/monthly"
TOKEN_PLAN_DETAIL_PATH = "/api/v1/tokenPlan/detail"
TOKEN_PLAN_USAGE_PATH = "/api/v1/tokenPlan/usage"
OPEN_TOKEN_PLAN_LIST_PATH = "/api/v1/openTokenPlan/list"
BALANCE_PATH = "/api/v1/balance"
USER_PROFILE_PATH = "/api/v1/userProfile"
PROJECTS_PATH = "/api/v1/projects"
AUTH_VERIFICATION_PATH = "/api/v1/auth/verificationStatus"
GEN_LOGIN_URL_PATH = "/api/v1/genLoginUrl"

SERVICE_LOGIN_PATH = "/pass/serviceLogin"
SERVICE_LOGIN_AUTH2_PATH = "/pass/serviceLoginAuth2"

# account.xiaomi.com 的 JSON 接口用 &&&START&&& 前缀包 JSON
JSONP_PREFIX = "&&&START&&&"

MAX_RETRY_AFTER = 5.0

#: 与浏览器登录请求逐字对齐的拟态头（降低风控触发）
CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36"
)
DEFAULT_SERVICE_PARAM = '{"checkSafePhone":false,"checkSafeAddress":false,"lsrp_score":0.0}'
NODE_HELPER = Path(__file__).parent / "_crypto_node.cjs"
ENCRYPT_TIMEOUT = 15.0


class MimoError(Exception):
    """Any failure while talking to the upstream service."""

    status = 502
    kind = "upstream_error"
    allow_stale = True

    def __init__(self, message: str, *, code: Any = None, status: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        if status is not None:
            self.status = status

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"type": self.kind, "message": self.message}
        if self.code is not None:
            payload["code"] = self.code
        return payload


class MissingCredentialError(MimoError):
    status = 401
    kind = "missing_credential"
    allow_stale = False


class AuthError(MimoError):
    """serviceToken 被拒且自动续登未能挽回——必须让调用方看见。"""

    status = 401
    kind = "invalid_token"
    allow_stale = False

    def __init__(
        self,
        message: str,
        *,
        code: Any = None,
        status: int | None = None,
        login_url: str | None = None,
    ) -> None:
        super().__init__(message, code=code, status=status)
        self.login_url = login_url


class ReauthFailed(AuthError):
    kind = "reauth_failed"


class CaptchaRequired(ReauthFailed):
    """上游要求人机验证（captchaUrl / 验证码类错误）——立即熔断，绝不重试硬闯。"""

    kind = "captcha_required"


class UpstreamUnavailable(MimoError):
    status = 502
    kind = "upstream_unavailable"


class UpstreamTimeout(MimoError):
    status = 504
    kind = "upstream_timeout"


def encode_query(params: dict[str, Any]) -> str:
    """Percent-encode query params (space as ``%20``), skipping None values."""
    return "&".join(
        f"{quote(str(key), safe='')}={quote(str(value), safe=':')}" for key, value in params.items()
        if value is not None
    )


def extract_callback(login_url: str | None) -> str | None:
    """从 401 的 loginUrl 中取出 platform 侧 ``/sts`` callback（服务端签好 sign）。"""
    if not login_url:
        return None
    try:
        query = parse_qs(urlsplit(login_url).query)
    except ValueError:
        return None
    values = query.get("callback")
    return values[0] if values else None


def parse_jsonp(text: str) -> dict[str, Any]:
    """解析 ``&&&START&&&{...}`` 包装的 account JSON 响应。"""
    raw = text.strip()
    if raw.startswith(JSONP_PREFIX):
        raw = raw[len(JSONP_PREFIX) :]
    import json  # 局部导入：只在续登路径用到

    try:
        data = json.loads(raw)
    except ValueError:
        raise UpstreamUnavailable("account 返回了非 JSON 响应") from None
    if not isinstance(data, dict):
        raise UpstreamUnavailable("account 返回了非预期的响应结构")
    return data


def cookies_from_responses(responses: list[httpx.Response]) -> dict[str, str]:
    """从响应（含 history）的 Set-Cookie 中提取 /sts 下发的会话字段。"""
    jar: dict[str, str] = {}
    wanted = {
        "api-platform_serviceToken": "service_token",
        "api-platform_ph": "ph",
        "api-platform_slh": "slh",
        "userId": "user_id",
    }
    for response in responses:
        for header in response.headers.get_list("set-cookie"):
            name, sep, rest = header.partition("=")
            if not sep:
                continue
            name = name.strip()
            target = wanted.get(name)
            if target is None:
                continue
            value = rest.split(";", 1)[0].strip()
            if value and value.upper() != "EXPIRED":
                jar[target] = value
    return jar


def pass_token_from(body: dict[str, Any] | None, responses: list[httpx.Response]) -> str | None:
    """account 响应里是否（重新）下发了 passToken——用于滑动续期落盘。

    来源优先级：body ``passToken`` 字段（serviceLoginAuth2 成功响应实测有）→
    Set-Cookie。``EXPIRED`` 是服务端作废标记，一律忽略。
    """
    if body:
        value = body.get("passToken")
        if isinstance(value, str) and value.strip() and value.strip().upper() != "EXPIRED":
            return value.strip()
    for response in responses:
        for header in response.headers.get_list("set-cookie"):
            if header.startswith("passToken="):
                value = header.split(";", 1)[0].split("=", 1)[1].strip()
                if value and value.upper() != "EXPIRED":
                    return value
    return None


class MimoClient:
    """Thin, pooled wrapper around the read-only platform usage endpoints."""

    def __init__(
        self,
        settings: Settings,
        credentials: CredentialStore,
        *,
        metrics: Metrics | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        password_encrypt: Any | None = None,
    ) -> None:
        self._settings = settings
        self._credentials = credentials
        self._metrics = metrics
        self._transport = transport
        #: 密码登录参数加密：``async (username) -> (user_aes, eui)``；默认 node helper
        self._password_encrypt = password_encrypt
        self._client: httpx.AsyncClient | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        # 续登 single-flight + 失败冷却：并发 401 只触发一次续登，失败后
        # 冷却窗口内不再打 account 上游，直接返回可读错误。
        self._reauth_lock = asyncio.Lock()
        self._reauth_inflight: asyncio.Task[None] | None = None
        self._reauth_failed_at: float | None = None
        self._reauth_last_error: str | None = None
        self._cooldown_used: float | None = None
        self._pwd_attempted = False
        self.reauth_count = 0
        #: 最近一次成功续登时，上游是否下发了新 passToken（滑动续期证据）
        self._pass_token_refreshed: bool | None = None

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        if self._client is not None:
            if self._loop is loop:
                return
            self._client = None
        self._client = httpx.AsyncClient(
            base_url=self._settings.base_url,
            transport=self._transport,
            timeout=httpx.Timeout(self._settings.request_timeout, connect=self._settings.connect_timeout),
            limits=httpx.Limits(
                max_connections=self._settings.max_connections,
                max_keepalive_connections=self._settings.max_keepalive_connections,
                keepalive_expiry=30.0,
            ),
            headers={
                "Accept": "application/json",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "x-timeZone": "Asia/Shanghai",
                "User-Agent": f"mimo-usage/{self._settings.version}",
            },
            follow_redirects=False,
        )
        self._loop = loop

    async def close(self) -> None:
        client, self._client, self._loop = self._client, None, None
        if client is None:
            return
        with suppress(RuntimeError):
            await client.aclose()

    async def __aenter__(self) -> MimoClient:
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    # ---- read-only upstream calls ------------------------------------------

    async def usage(self) -> dict[str, Any]:
        """账户维度的 token/费用/限速概览（上游不接受时间窗参数）。"""
        return await self._api("GET", USAGE_PATH)

    async def usage_detail_list(self, window: YearMonth) -> list[Any]:
        """按年（月可选）的用量明细列表。

        上游 ``POST /api/v1/usage/detail/list`` 只认 ``{"year":..,"month":..}``
        （其它字段一律 400 Unrecognized field），且必须带 query ``api-platform_ph``。
        """
        body: dict[str, Any] = {"year": window.year}
        if window.month is not None:
            body["month"] = window.month
        data = await self._api("POST", USAGE_DETAIL_LIST_PATH, json_body=body, with_ph_query=True)
        return data if isinstance(data, list) else []

    async def usage_trend_list(self, window: YearMonth) -> list[Any]:
        """Token Plan 用量统计（plan-manage 使用详情）：每日 × 每模型一行。

        上游 ``POST /api/v1/usage/token-plan/list``（实测）：
        query 同样必带 ``api-platform_ph``，body 只认 ``{"year","month"?}``。
        行字段：``date/model/totalToken/inputHitToken/inputMissToken/outputToken/
        requestCount/inputAudioDuration``。
        """
        body: dict[str, Any] = {"year": window.year}
        if window.month is not None:
            body["month"] = window.month
        data = await self._api("POST", USAGE_TREND_PATH, json_body=body, with_ph_query=True)
        return data if isinstance(data, list) else []

    async def usage_bill_monthly(self) -> list[Any]:
        """月账单列表（字段：reportMonth/consumptionAmount/giftConsumption/cashConsumption）。"""
        data = await self._api("GET", USAGE_BILL_MONTHLY_PATH)
        return data if isinstance(data, list) else []

    async def token_plan_detail(self) -> dict[str, Any]:
        return await self._api("GET", TOKEN_PLAN_DETAIL_PATH)

    async def token_plan_usage(self) -> dict[str, Any]:
        return await self._api("GET", TOKEN_PLAN_USAGE_PATH)

    async def open_token_plan_list(self) -> list[Any]:
        data = await self._api("GET", OPEN_TOKEN_PLAN_LIST_PATH)
        return data if isinstance(data, list) else []

    async def balance(self) -> dict[str, Any]:
        return await self._api("GET", BALANCE_PATH)

    async def user_profile(self) -> dict[str, Any]:
        return await self._api("GET", USER_PROFILE_PATH)

    async def projects(self) -> dict[str, Any]:
        return await self._api("GET", PROJECTS_PATH)

    async def verification_status(self) -> dict[str, Any]:
        """实名认证状态（userVerificationState: NOT_AUTHORIZED / AUTHORIZED …）。"""
        return await self._api("GET", AUTH_VERIFICATION_PATH)

    # ---- request plumbing ---------------------------------------------------

    async def _api(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        with_ph_query: bool = False,
        _retried: bool = False,
    ) -> Any:
        if self._client is None:
            raise RuntimeError("client.start() must be awaited before use")

        query = dict(params or {})
        if with_ph_query:
            ph = self._credentials.ph.get()
            if ph:
                query["api-platform_ph"] = _unquote_cookie(ph)
        url = f"{path}?{encode_query(query)}" if query else path

        headers = self._headers()
        attempts = max(self._settings.retries, 0) + 1
        failure: MimoError | None = None

        for attempt in range(attempts):
            started = time.perf_counter()
            retryable = True
            try:
                if method == "POST":
                    response = await self._client.post(url, headers=headers, json=json_body)
                else:
                    response = await self._client.get(url, headers=headers)
            except httpx.TimeoutException:
                failure = UpstreamTimeout(f"platform 请求超时（{self._settings.request_timeout:g}s）")
            except httpx.HTTPError as exc:
                failure = UpstreamUnavailable(f"无法连接 platform：{exc.__class__.__name__}")
            else:
                auth_error = self._auth_error(response)
                if auth_error is not None and not _retried:
                    # 401 → 自动续登一次 → 原样重试；再 401 才抛给调用方
                    self._observe(started, error=True, failure=auth_error)
                    await self._reauthenticate(auth_error.login_url)
                    return await self._api(
                        method,
                        path,
                        json_body=json_body,
                        params=params,
                        with_ph_query=with_ph_query,
                        _retried=True,
                    )
                if auth_error is not None:
                    self._observe(started, error=True, failure=auth_error)
                    raise auth_error
                try:
                    data = self._decode(response)
                except MimoError as exc:
                    # 5xx/非 JSON 等可重试错误走退避循环；业务错误码直接抛
                    failure = exc
                    retryable = isinstance(exc, (UpstreamUnavailable, UpstreamTimeout)) and (
                        response.status_code >= 500 or "非 JSON" in exc.message or "非预期" in exc.message
                    )
                    self._observe(started, error=True, failure=exc)
                    if not retryable or attempt + 1 >= attempts:
                        raise exc
                else:
                    self._observe(started, error=False)
                    return data

            self._observe(started, error=True, failure=failure)
            if not retryable or attempt + 1 >= attempts:
                break
            await asyncio.sleep(self._backoff(attempt))

        raise failure if failure is not None else UpstreamUnavailable("platform 请求失败")

    def _headers(self) -> dict[str, str]:
        cookie = self._credentials.cookie_header()
        headers: dict[str, str] = {}
        if cookie:
            headers["Cookie"] = cookie
        headers["Referer"] = f"{self._settings.base_url}/console/balance"
        return headers

    def _backoff(self, attempt: int) -> float:
        base = self._settings.retry_backoff * (2**attempt)
        return min(base * (0.5 + random.random()), 5.0)

    def _observe(self, started: float, *, error: bool, failure: MimoError | None = None) -> None:
        if self._metrics is None:
            return
        self._metrics.record_upstream((time.perf_counter() - started) * 1000, error=error)
        if not error:
            self._metrics.record_auth_success()
        elif isinstance(failure, AuthError):
            self._metrics.record_auth_failure()

    # ---- auth / re-login ----------------------------------------------------

    def _auth_error(self, response: httpx.Response) -> AuthError | None:
        """识别 401/loginUrl：HTTP 401，或 200+``code:401`` 且带 loginUrl。"""
        payload: dict[str, Any] | None = None
        if response.status_code == 401:
            with suppress(ValueError):
                body = response.json()
                payload = body if isinstance(body, dict) else None
            message = "platform 返回 401，serviceToken 可能已过期"
            login = str(payload["loginUrl"]) if payload and payload.get("loginUrl") else None
            return AuthError(message, code=401, login_url=login)
        if response.status_code == 200:
            with suppress(ValueError):
                body = response.json()
                if isinstance(body, dict) and body.get("code") == 401:
                    message = str(body.get("message") or "serviceToken 已过期")
                    return AuthError(message, code=401, login_url=str(body.get("loginUrl") or "") or None)
        return None

    @staticmethod
    def _decode(response: httpx.Response) -> Any:
        if response.status_code >= 500:
            raise UpstreamUnavailable(f"platform 服务异常（HTTP {response.status_code}）")
        try:
            payload = response.json()
        except ValueError:
            raise UpstreamUnavailable("platform 返回了非 JSON 响应") from None
        if not isinstance(payload, dict):
            raise UpstreamUnavailable("platform 返回了非预期的响应结构")

        code = payload.get("code")
        message = str(payload.get("message") or payload.get("msg") or f"platform 错误码 {code}")
        if code == 401:
            raise AuthError(message, code=401, login_url=str(payload.get("loginUrl") or "") or None)
        if code not in (0, 200):
            if response.status_code >= 400:
                raise UpstreamUnavailable(f"platform 返回 HTTP {response.status_code}：{message}", code=code)
            raise UpstreamUnavailable(message, code=code)
        return payload.get("data", payload)

    async def _reauthenticate(self, login_url: str | None) -> None:
        """Single-flight 续登；失败进入 cooldown，窗口内直接抛可读错误。"""
        if self._reauth_failed_at is not None:
            cooldown = self._cooldown_used or self._settings.reauth_cooldown
            if cooldown > 0:
                elapsed = time.monotonic() - self._reauth_failed_at
                if elapsed < cooldown:
                    remaining = int(cooldown - elapsed)
                    raise ReauthFailed(
                        f"自动续登失败（{self._reauth_last_error or '未知原因'}），"
                        f"{remaining}s 内不再重试；请浏览器重登 platform.xiaomimimo.com "
                        "并更新 config.yaml 的 credentials（passToken/密码）",
                        code=401,
                        login_url=login_url,
                    )

        async with self._reauth_lock:
            running = self._reauth_inflight
            if running is not None and not running.done():
                waiter = running
            else:
                self._reauth_inflight = asyncio.get_running_loop().create_task(
                    self._do_reauthenticate(login_url)
                )
                waiter = self._reauth_inflight
        await asyncio.shield(waiter)

    async def _do_reauthenticate(self, login_url: str | None) -> None:
        self._pwd_attempted = False
        try:
            await self._reauthenticate_flow(login_url)
        except MimoError as exc:
            self._reauth_failed_at = time.monotonic()
            self._reauth_last_error = exc.message
            # 密码分支一旦出手，失败就用长冷却：撞风控的代价远高于免密失败
            self._cooldown_used = (
                self._settings.password_reauth_cooldown
                if self._pwd_attempted
                else self._settings.reauth_cooldown
            )
            if self._metrics is not None:
                self._metrics.record_auth_failure()
            raise
        else:
            self._reauth_failed_at = None
            self._reauth_last_error = None
            self._cooldown_used = None
            self.reauth_count += 1
            if self._metrics is not None:
                self._metrics.record_auth_success()

    async def _reauthenticate_flow(self, login_url: str | None) -> None:
        """三级续登链 → /sts：换新 serviceToken/ph/slh/userId 并落盘。

        ① serviceLogin 免密直通；② serviceLoginAuth2 passToken 免密；
        ③ 密码登录（username+passwordMd5 齐备时）。任何一步失败即止，不循环。
        """
        pass_token = self._credentials.pass_token.get()
        device_id = self._credentials.device_id.get()
        username = self._credentials.username.get()
        password_md5 = self._credentials.password_md5.get()
        password_ready = bool(username and password_md5)
        if not device_id:
            raise ReauthFailed("serviceToken 已过期，且缺少 deviceId，无法自动续登", code=401)
        if not pass_token and not password_ready:
            raise ReauthFailed(
                "serviceToken 已过期，且缺少 MIMO_PASS_TOKEN（或 username+password）"
                "，无法自动续登",
                code=401,
            )
        # account 域免密校验会回看 userId（实测缺了它 serviceLogin 恒 70016）
        user_id = self._credentials.user_id.get() or ""

        callback = extract_callback(login_url)
        if not callback:
            callback = await self._fetch_login_callback()
        if not callback:
            raise ReauthFailed("无法获取 /sts callback（genLoginUrl 无跳转），自动续登中止", code=401)

        base_cookie = (
            f"deviceId={device_id}; uLocale=zh_CN; pass_ua=web; passInfo=login-end"
        )
        account_cookie = base_cookie
        if pass_token:
            account_cookie = f"passToken={pass_token}; {account_cookie}"
        if user_id:
            account_cookie = f"{account_cookie}; userId={user_id}"
        common = {
            "Accept": "application/json, text/plain, */*",
            "User-Agent": CHROME_UA,
            "Cookie": account_cookie,
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{self._settings.account_base_url}/fe/service/login",
        }

        # 1) serviceLogin?_json=true：免密可能直通；否则拿 _sign/qs 供 Auth2
        login_url_full = (
            f"{self._settings.account_base_url}{SERVICE_LOGIN_PATH}"
            f"?_json=true&sid={quote(self._settings.sid)}&callback={quote(callback, safe='')}"
        )
        login_resp = await self._account_get(login_url_full, common)
        login_body = parse_jsonp(login_resp.text)
        fresh_pass = pass_token_from(login_body, [login_resp])
        location = str(login_body.get("location") or "")

        if login_body.get("code") == 0 and location.startswith(self._settings.base_url):
            sts_location = location
        else:
            sign = login_body.get("_sign")
            if not sign:
                description = str(login_body.get("description") or login_body.get("message") or "")
                raise ReauthFailed(
                    f"serviceLogin 未返回 _sign（code={login_body.get('code')} {description}），"
                    "凭证可能已失效",
                    code=401,
                )
            sts_location = None
            first_error: ReauthFailed | None = None

            # ② passToken 免密 Auth2
            if pass_token:
                try:
                    sts_location, extra_pass = await self._auth2(
                        callback=callback,
                        login_body=login_body,
                        form_extra={},
                        headers=common,
                    )
                    fresh_pass = extra_pass or fresh_pass
                except ReauthFailed as exc:
                    first_error = exc

            # ③ 密码登录 Auth2（免密失败或没有 passToken 时兜底）
            if sts_location is None and password_ready:
                self._pwd_attempted = True
                sts_location, extra_pass = await self._auth2_password(
                    callback=callback,
                    login_body=login_body,
                    device_id=device_id,
                    username=str(username),
                    password_md5=str(password_md5),
                )
                fresh_pass = extra_pass or fresh_pass

            if sts_location is None:
                raise first_error or ReauthFailed("serviceLoginAuth2 未能取得 /sts location", code=401)

        # /sts：307 落到 platform 域并 Set-Cookie 新会话（auth 票 TTL≈5 分钟）
        sts_resp = await self._account_get(sts_location, {"User-Agent": CHROME_UA}, follow=True)
        responses = [sts_resp, *sts_resp.history]
        jar = cookies_from_responses(responses)
        service_token = jar.get("service_token")
        if not service_token:
            raise ReauthFailed("/sts 未下发 api-platform_serviceToken，续登未完成", code=401)

        self._credentials.persist_session(
            service_token=service_token,
            user_id=jar.get("user_id"),
            ph=jar.get("ph"),
            slh=jar.get("slh"),
        )
        # passToken 刷新：免密成功通常不下发；密码登录成功响应必带新 passToken
        # （30 天免密根自愈）。有新值就落盘。
        self._pass_token_refreshed = bool(fresh_pass and fresh_pass != pass_token)
        if fresh_pass and fresh_pass != pass_token:
            self._credentials.pass_token.set(fresh_pass)

    async def _auth2(
        self,
        *,
        callback: str,
        login_body: dict[str, Any],
        form_extra: dict[str, Any],
        headers: dict[str, str],
    ) -> tuple[str, str | None]:
        """POST serviceLoginAuth2（免密或密码 form 由调用方给 extra 字段）。"""
        form: dict[str, Any] = {
            "bizDeviceType": "",
            "needTheme": "false",
            "theme": "",
            "showActiveX": "false",
            "serviceParam": str(login_body.get("serviceParam") or DEFAULT_SERVICE_PARAM),
            "callback": callback,
            "qs": str(login_body.get("qs") or ""),
            "sid": self._settings.sid,
            "_sign": str(login_body.get("_sign") or ""),
            "_json": "true",
            "policyName": "miaccount",
            "captCode": "",
        }
        form.update(form_extra)
        auth2_url = f"{self._settings.account_base_url}{SERVICE_LOGIN_AUTH2_PATH}"
        resp = await self._account_post(auth2_url, form, headers)
        body = parse_jsonp(resp.text)
        fresh_pass = pass_token_from(body, [resp])
        self._raise_for_auth2(body)
        sts = str(body.get("location") or "")
        if not sts:
            raise ReauthFailed("serviceLoginAuth2 未返回 location，无法完成 /sts 换票", code=401)
        return sts, fresh_pass

    async def _auth2_password(
        self,
        *,
        callback: str,
        login_body: dict[str, Any],
        device_id: str,
        username: str,
        password_md5: str,
    ) -> tuple[str, str | None]:
        """密码登录：AES(user) + EUI(RSA) + hash=MD5 大写，全套拟态头。"""
        user_aes, eui = await self._encrypt(username)
        fingerprint = self._credentials.device_fingerprint.get()
        if not fingerprint:
            raise ReauthFailed(
                "密码续登需要 deviceFingerprint：请先在浏览器登录 platform.xiaomimimo.com，"
                "取该会话的设备指纹（FingerprintJS visitorId）写入 "
                "MIMO_DEVICE_FINGERPRINT 或 config.yaml 的 credentials.deviceFingerprint",
                code=401,
            )
        qs = str(login_body.get("qs") or "")
        form_extra = {
            "user": user_aes,
            "hash": password_md5,
            "deviceFingerprint": fingerprint,
        }
        # cc=+86 只属于手机表单分支；uid/邮箱登录的 user 分支不带 cc
        if username.isdigit() and len(username) == 11:
            form_extra["cc"] = "+86"
        referer = (
            f"{self._settings.account_base_url}/fe/service/login/password"
            f"?_group=DEFAULT&sid={quote(self._settings.sid)}&qs={quote(qs, safe='')}"
        )
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Cache-Control": "no-cache",
            "User-Agent": CHROME_UA,
            # 密码登录的 cookie 不含 passToken（密码本身就是凭证，与浏览器一致）
            "Cookie": f"deviceId={device_id}; pass_ua=web; uLocale=zh_CN; passInfo=login-end",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": self._settings.account_base_url,
            "Referer": referer,
            "X-Requested-With": "XMLHttpRequest",
            "DNT": "1",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "sec-ch-ua": '"Google Chrome";v="153", "Not_A Brand";v="8", "Chromium";v="153"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "EUI": eui,
        }
        return await self._auth2(
            callback=callback, login_body=login_body, form_extra=form_extra, headers=headers
        )

    @staticmethod
    def _raise_for_auth2(body: dict[str, Any]) -> None:
        if body.get("captchaUrl"):
            raise CaptchaRequired(
                f"上游要求人机验证（captchaUrl 已下发，description="
                f"{body.get('description')!r}）；已熔断不再重试，请浏览器登录 platform"
                ".xiaomimimo.com 解除验证并更新凭证",
                code=body.get("code", 401),
            )
        if body.get("code") != 0:
            description = str(body.get("description") or body.get("message") or "未知原因")
            if "验证码" in description:
                raise CaptchaRequired(
                    f"上游要求验证码（code={body.get('code')} {description}）；已熔断，"
                    "请浏览器登录解除",
                    code=body.get("code", 401),
                )
            raise ReauthFailed(
                f"serviceLoginAuth2 失败（code={body.get('code')} {description}），"
                "凭证可能已失效，请重新登录 platform.xiaomimimo.com 更新 config.yaml",
                code=401,
            )

    async def _encrypt(self, username: str) -> tuple[str, str]:
        """复刻 account 前端 encryptAes：node 内置 crypto（AES-CBC + RSA + EUI）。"""
        if self._password_encrypt is not None:
            return await self._password_encrypt(username)
        try:
            proc = await asyncio.create_subprocess_exec(
                "node",
                str(NODE_HELPER),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, err = await asyncio.wait_for(
                proc.communicate(json.dumps({"user": username}).encode("utf-8")),
                timeout=ENCRYPT_TIMEOUT,
            )
        except (OSError, TimeoutError) as exc:
            raise ReauthFailed(f"调用 node 加密助手失败：{exc}", code=401) from exc
        if proc.returncode != 0:
            raise ReauthFailed(
                f"node 加密助手失败：{err.decode('utf-8', 'replace')[:200]}", code=401
            )
        try:
            data = json.loads(out)
            return str(data["user"]), str(data["eui"])
        except (ValueError, KeyError) as exc:
            raise ReauthFailed(f"node 加密助手输出异常：{exc}", code=401) from exc

    async def _fetch_login_callback(self) -> str | None:
        """没有 loginUrl 时的回退：genLoginUrl 302 Location 里的 callback。"""
        url = f"{GEN_LOGIN_URL_PATH}?currentPath=/console/balance"
        assert self._client is not None
        try:
            response = await self._client.get(url, headers=self._headers())
        except httpx.HTTPError:
            return None
        location = response.headers.get("location") or ""
        if not location:
            return None
        # genLoginUrl 的 Location 就是 account 的 serviceLogin 链接，其中
        # callback= 指向 platform 的 /sts（sign 已由服务端签好）
        return extract_callback(location)

    async def _account_get(
        self, url: str, headers: dict[str, str], *, follow: bool = False
    ) -> httpx.Response:
        assert self._client is not None
        # account/跨域跳转：base_url 是 platform，这里传绝对 URL 覆盖
        try:
            if follow:
                # 临时把跟随打开：/sts 的 307 → console 是同一轮换票过程
                original = self._client.follow_redirects
                self._client.follow_redirects = True
                try:
                    return await self._client.get(url, headers=headers)
                finally:
                    self._client.follow_redirects = original
            return await self._client.get(url, headers=headers)
        except httpx.TimeoutException as exc:
            raise UpstreamTimeout("account 请求超时") from exc
        except httpx.HTTPError as exc:
            raise UpstreamUnavailable(f"无法连接 account：{exc.__class__.__name__}") from exc

    async def _account_post(
        self, url: str, form: dict[str, Any], headers: dict[str, str]
    ) -> httpx.Response:
        assert self._client is not None
        post_headers = {**headers, "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"}
        try:
            return await self._client.post(url, headers=post_headers, data=form)
        except httpx.TimeoutException as exc:
            raise UpstreamTimeout("account 请求超时") from exc
        except httpx.HTTPError as exc:
            raise UpstreamUnavailable(f"无法连接 account：{exc.__class__.__name__}") from exc

    def reauth_state(self) -> dict[str, Any]:
        state: dict[str, Any] = {"reauths": self.reauth_count}
        if self._pass_token_refreshed is not None:
            state["passTokenRefreshed"] = self._pass_token_refreshed
        if self._reauth_failed_at is not None:
            cooldown = self._cooldown_used or self._settings.reauth_cooldown
            state["coolingDown"] = True
            state["lastError"] = self._reauth_last_error
            state["cooldownRemainingSeconds"] = max(
                int(cooldown - (time.monotonic() - self._reauth_failed_at)), 0
            )
        else:
            state["coolingDown"] = False
        return state


def _unquote_cookie(value: str) -> str:
    raw = value.strip()
    if len(raw) >= 2 and raw.startswith('"') and raw.endswith('"'):
        return raw[1:-1]
    return raw


__all__ = [
    "AUTH_VERIFICATION_PATH",
    "AuthError",
    "BALANCE_PATH",
    "CHROME_UA",
    "CaptchaRequired",
    "GEN_LOGIN_URL_PATH",
    "JSONP_PREFIX",
    "MimoClient",
    "MimoError",
    "MissingCredentialError",
    "OPEN_TOKEN_PLAN_LIST_PATH",
    "PROJECTS_PATH",
    "ReauthFailed",
    "SERVICE_LOGIN_AUTH2_PATH",
    "SERVICE_LOGIN_PATH",
    "TOKEN_PLAN_DETAIL_PATH",
    "TOKEN_PLAN_USAGE_PATH",
    "USER_PROFILE_PATH",
    "USAGE_BILL_MONTHLY_PATH",
    "USAGE_DETAIL_LIST_PATH",
    "USAGE_TREND_PATH",
    "USAGE_PATH",
    "UpstreamTimeout",
    "UpstreamUnavailable",
    "cookies_from_responses",
    "encode_query",
    "extract_callback",
    "parse_jsonp",
    "pass_token_from",
]

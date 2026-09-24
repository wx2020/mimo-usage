"""Sanic application factory."""

from __future__ import annotations

import secrets
from pathlib import Path

import httpx
from sanic import Request, Sanic
from sanic.exceptions import SanicException
from sanic.response import BaseHTTPResponse, html, redirect

from . import api
from .api import json_response
from .cache import IntervalGate, TTLCache
from .client import MimoClient, MimoError, MissingCredentialError
from .config import CredentialStore, Settings
from .logging_filters import install_secret_filter
from .metrics import Metrics
from .timerange import BadRequest

STATIC_DIR = Path(__file__).parent / "static"
APP_NAME = "mimo_usage"
DASHBOARD_COOKIE = "mimo_usage_key"
DASHBOARD_COOKIE_MAX_AGE = 30 * 24 * 3600
#: 静态资源版本号：与 __version__ 同步，配合 ?v= 让改版立即生效
STATIC_VERSION = "2.0.0"  # 静态资源缓存键（改静态资源后同步 bump；发版时随版本号刷新）

#: 未授权时给「浏览器导航」看的引导页。
#: API 客户端与静态资源仍返回泛化 JSON（不把认证方式喂给扫描器）；
#: 仅当请求是看板 HTML 路径且 Accept 含 text/html 时使用本页。
UNAUTHORIZED_PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>未授权 · MiMo 用量看板</title>
<style>
  body { margin: 0; min-height: 100vh; display: flex; align-items: center; justify-content: center;
         background: #f5f6f8; color: #1f2329;
         font: 14px/1.7 -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif; }
  .card { background: #fff; border: 1px solid #e6e8ec; border-radius: 12px; padding: 24px 28px;
          max-width: 560px; margin: 16px; }
  h1 { font-size: 18px; margin: 0 0 10px; }
  p { margin: 6px 0; color: #4b5563; }
  code { background: #f3f4f6; padding: 1px 5px; border-radius: 4px; }
  .hint { color: #8a919f; font-size: 12px; margin-top: 14px; }
  @media (prefers-color-scheme: dark) {
    body { background: #14161a; color: #e6e8eb; }
    .card { background: #1c1f24; border-color: #2c323a; }
    p { color: #b6bcc6; }
    code { background: #2c323a; color: #e6e8eb; }
    .hint { color: #8a919f; }
  }
</style>
</head>
<body>
<div class="card">
  <h1>未授权</h1>
  <p>本看板已开启访问密钥。请用 <code>{{PATH}}?key=你的密钥</code> 打开一次本页。</p>
  <p>密钥配置在 <code>config.yaml</code> 的 <code>server.apiKey</code>（或环境变量
     <code>MIMO_API_KEY</code>）。</p>
  <p>打开成功后，页面会把密钥存入浏览器本地存储，并自动抹掉地址栏里的参数。</p>
  <p class="hint">API 客户端请用请求头 <code>X-API-Key</code> 或 <code>?key=</code> 参数调用。</p>
</div>
</body>
</html>
"""


def normalise_path(raw: str) -> str:
    """``dashboard`` / ``/dashboard/`` -> ``/dashboard`` (``/`` stays ``/``)."""
    path = "/" + raw.strip().strip("/")
    return "/" if path == "/" else path


def create_app(
    settings: Settings | None = None,
    *,
    name: str = APP_NAME,
    credentials: CredentialStore | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Sanic:
    """Build the app.

    ``transport`` is injectable so tests can stand in a fake upstream without
    touching the network; ``credentials`` likewise for tests.
    """
    settings = settings or Settings.from_env()
    credentials = credentials or CredentialStore.from_env()
    dashboard_path = normalise_path(settings.dashboard_path)
    app = Sanic(name)
    app.config.FALLBACK_ERROR_FORMAT = "json"
    app.config.DEBUG = settings.debug
    app.config.ACCESS_LOG = settings.access_log
    app.config.KEEP_ALIVE = True
    app.config.KEEP_ALIVE_TIMEOUT = 65
    app.config.REQUEST_TIMEOUT = max(int(settings.request_timeout) + 5, 30)
    app.config.RESPONSE_TIMEOUT = max(int(settings.request_timeout) + 5, 30)
    app.config.GRACEFUL_SHUTDOWN_TIMEOUT = 5

    metrics = Metrics()
    app.ctx.settings = settings
    app.ctx.metrics = metrics
    app.ctx.credentials = credentials
    app.ctx.client = MimoClient(settings, credentials, metrics=metrics, transport=transport)
    app.ctx.caches = {
        "usage": TTLCache(settings.usage_ttl, stale_ttl=settings.stale_ttl, max_entries=settings.cache_max_entries),
        "detail": TTLCache(settings.detail_ttl, stale_ttl=settings.stale_ttl, max_entries=settings.cache_max_entries),
        "tokenplan": TTLCache(
            settings.token_plan_ttl, stale_ttl=settings.stale_ttl, max_entries=settings.cache_max_entries
        ),
        "account": TTLCache(
            settings.account_ttl, stale_ttl=settings.stale_ttl, max_entries=settings.cache_max_entries
        ),
        # 端点级「整响应」视图缓存：summary/usage 命中即跳过 gather 与计算
        "view": TTLCache(settings.view_ttl, stale_ttl=settings.stale_ttl, max_entries=settings.cache_max_entries),
    }
    app.ctx.refresh_gate = IntervalGate(settings.refresh_min_interval, max_entries=settings.cache_max_entries)

    @app.before_server_start
    async def _open_upstream(app: Sanic) -> None:
        await app.ctx.client.start()
        install_secret_filter()

    @app.after_server_stop
    async def _close_upstream(app: Sanic) -> None:
        await app.ctx.client.close()

    def presented_key(request: Request, *, allow_cookie: bool = False) -> str:
        key = request.headers.get("x-api-key") or request.get_args().get("key") or ""
        if not key and allow_cookie:
            key = request.cookies.get(DASHBOARD_COOKIE) or ""
        return key

    def rejection(provided: str) -> BaseHTTPResponse:
        missing = not provided
        return json_response(
            {"error": {"type": "forbidden" if missing else "unauthorized", "message": "未授权"}},
            status=403 if missing else 401,
        )

    def dashboard_rejection(request: Request, provided: str) -> BaseHTTPResponse:
        """看板 HTML 被浏览器直接打开时给引导页；API/静态资源保持泛化 JSON。"""
        accept = (request.headers.get("accept") or "").lower()
        if "text/html" in accept and request.path == dashboard_path:
            status = 403 if not provided else 401
            return html(UNAUTHORIZED_PAGE.replace("{{PATH}}", dashboard_path), status=status)
        return rejection(provided)

    @app.on_request
    async def _require_api_key(request: Request) -> BaseHTTPResponse | None:
        expected = settings.api_key
        if not expected or not request.path.startswith("/api/"):
            return None
        provided = presented_key(request)
        return None if secrets.compare_digest(provided, expected) else rejection(provided)

    @app.on_request
    async def _require_dashboard_key(request: Request) -> BaseHTTPResponse | None:
        expected = settings.api_key
        if not expected:
            return None
        if request.path != dashboard_path and not request.path.startswith(f"{dashboard_path}/"):
            return None
        provided = presented_key(request, allow_cookie=True)
        return None if secrets.compare_digest(provided, expected) else dashboard_rejection(request, provided)

    @app.on_response
    async def _record(request: Request, response: BaseHTTPResponse) -> None:
        route = getattr(request.route, "path", None) or "-"
        request.app.ctx.metrics.record_request(route, response.status)

    @app.exception(MimoError)
    async def _upstream_error(_request: Request, exc: MimoError) -> BaseHTTPResponse:
        return json_response({"error": exc.as_dict()}, status=exc.status)

    @app.exception(BadRequest)
    async def _invalid_request(_request: Request, exc: BadRequest) -> BaseHTTPResponse:
        return json_response(
            {"error": {"type": "invalid_request", "message": str(exc)}},
            status=400,
        )

    @app.exception(SanicException)
    async def _sanic_error(_request: Request, exc: SanicException) -> BaseHTTPResponse:
        return json_response(
            {"error": {"type": "http_error", "message": str(exc)}},
            status=exc.status_code or 500,
        )

    @app.get("/healthz")
    async def healthz(request: Request) -> BaseHTTPResponse:
        """Liveness probe plus the small amount of config the UI needs.

        恒 200：凭证过期不该让编排器 restart-loop；``status``/``credentials``/
        ``upstreamAuth`` 如实汇报（任一上游成功即可清掉 ``rejected``）。
        """
        creds: CredentialStore = request.app.ctx.credentials
        metrics: Metrics = request.app.ctx.metrics
        configured = creds.configured
        auth = metrics.auth_state()
        client: MimoClient = request.app.ctx.client
        return json_response(
            {
                "status": "ok" if configured and not auth["rejected"] else "degraded",
                "version": settings.version,
                "uptimeSeconds": round(metrics.snapshot()["uptimeSeconds"], 3),
                "credentials": {"configured": configured, "sources": creds.sources()},
                "upstreamAuth": auth,
                "reauth": client.reauth_state(),
                "upstream": settings.base_url,
                "refreshSeconds": settings.dashboard_refresh_seconds,
            }
        )

    app.blueprint(api.bp)

    dashboard_html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    dashboard_html = dashboard_html.replace("{{STATIC}}", f"{dashboard_path}/static")
    # ?v= 版本号随发布同步，改静态资源后浏览器立刻拉新
    dashboard_html = dashboard_html.replace("{{VERSION}}", STATIC_VERSION)

    async def _dashboard(request: Request) -> BaseHTTPResponse:
        response = html(dashboard_html)
        bootstrap = request.args.get("key") or ""
        if settings.api_key and bootstrap and secrets.compare_digest(bootstrap, settings.api_key):
            response.add_cookie(
                DASHBOARD_COOKIE,
                settings.api_key,
                path=dashboard_path,
                httponly=True,
                secure=False,
                max_age=DASHBOARD_COOKIE_MAX_AGE,
            )
        return response

    app.add_route(_dashboard, dashboard_path, methods=["GET"])

    # 静态资源：Sanic 默认 no-cache；这里显式给可缓存窗口，失效靠 ?v= 版本号
    @app.on_response
    async def _static_cache_headers(request: Request, response: BaseHTTPResponse) -> None:
        if request.path.startswith(f"{dashboard_path}/static/"):
            response.headers["Cache-Control"] = "public, max-age=3600"

    app.static(f"{dashboard_path}/static", str(STATIC_DIR), name="dashboard-static")

    if dashboard_path != "/":

        @app.get("/")
        async def _to_dashboard(_request: Request) -> BaseHTTPResponse:
            return redirect(dashboard_path)

    return app


__all__ = ["create_app", "MissingCredentialError", "STATIC_VERSION"]

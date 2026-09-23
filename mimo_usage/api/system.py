"""Process-local observability endpoints."""

from __future__ import annotations

from sanic import Request
from sanic.response import BaseHTTPResponse

from ..timerange import BadRequest, iso_now
from .blueprint import bp
from .common import compact, json_response
from .params import parse_fields, select_fields


@bp.get("/metrics")
async def metrics(request: Request) -> BaseHTTPResponse:
    """Per-worker counters plus cache occupancy (``?fields=`` trims the payload)."""
    app = request.app
    raw = app.ctx.metrics.snapshot()
    raw["caches"] = {name: cache.stats() for name, cache in app.ctx.caches.items()}
    raw["reauth"] = app.ctx.client.reauth_state()
    raw["version"] = app.ctx.settings.version

    fields = parse_fields(request)
    data, ignored = select_fields(raw, fields)
    if fields and not data:
        raise BadRequest(f"fields 没有匹配到任何字段：{', '.join(ignored)}")
    return json_response(
        {
            "data": data,
            "meta": compact(generatedAt=iso_now(), fields=fields or None, ignoredFields=ignored or None),
        }
    )

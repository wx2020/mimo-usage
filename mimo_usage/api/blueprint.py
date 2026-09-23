"""The API blueprint.

It lives in its own module so the route modules can import it without creating
an import cycle with the package ``__init__``.
"""

from __future__ import annotations

from sanic import Blueprint

bp = Blueprint("usage", url_prefix="/api/v1")

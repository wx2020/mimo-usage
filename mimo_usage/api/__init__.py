"""HTTP layer: the JSON API consumed by the dashboard and by scripts.

Importing this package registers every route on :data:`mimo_usage.api.bp`.
"""

from __future__ import annotations

from . import overview, sections, summary, system  # noqa: F401  (route registration)
from .blueprint import bp
from .common import json_response
from .params import resolve_token

__all__ = ["bp", "json_response", "resolve_token"]

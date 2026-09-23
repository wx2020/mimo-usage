"""Import target for the Sanic CLI: ``sanic mimo_usage.server:app --fast``."""

from __future__ import annotations

from .app import create_app

app = create_app()

__all__ = ["app"]

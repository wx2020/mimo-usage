"""Entrypoint smoke: module import, AppLoader target, console script symbol."""

from __future__ import annotations

import importlib


def test_main_module_builds_loader_target() -> None:
    module = importlib.import_module("mimo_usage.__main__")
    assert module.APP_TARGET == "mimo_usage.server:app"
    assert callable(module.main)


def test_server_module_exports_app() -> None:
    module = importlib.import_module("mimo_usage.server")
    assert hasattr(module.app, "blueprint")
    assert module.app.name == "mimo_usage"


def test_package_version() -> None:
    import mimo_usage

    assert mimo_usage.__version__ == "1.1.1"


def test_create_app_is_importable() -> None:
    from mimo_usage.app import STATIC_VERSION, create_app

    app = create_app(name="entry_smoke")
    assert STATIC_VERSION
    assert app.name == "entry_smoke"
    # 路由注册在 app.router；直接断言关键 handler 已挂上
    registered: set[str] = set()
    for route in app.router.routes:
        paths = getattr(route, "paths", None) or ()
        for path in paths:
            registered.add(path if path.startswith("/") else f"/{path}")
        path = getattr(route, "path", None)
        if path:
            registered.add(path if path.startswith("/") else f"/{path}")
    assert "/healthz" in registered
    assert "/dashboard" in registered
    assert "/api/v1/overview" in registered

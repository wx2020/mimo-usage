"""Settings from env + credential file hot reload."""

from __future__ import annotations

import os
import time

import pytest

from mimo_usage.config import CredentialStore, Settings, YamlStore, _Secret


def test_settings_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.startswith("MIMO_"):
            monkeypatch.delenv(key, raising=False)
    settings = Settings.from_env()
    assert settings.base_url == "https://platform.xiaomimimo.com"
    assert settings.account_base_url == "https://account.xiaomi.com"
    assert settings.sid == "api-platform"
    assert settings.port == 8000
    assert settings.dashboard_path == "/dashboard"
    assert settings.reauth_cooldown == 300.0


def test_settings_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIMO_PORT", "9001")
    monkeypatch.setenv("MIMO_API_KEY", "s3cret")
    monkeypatch.setenv("MIMO_REAUTH_COOLDOWN", "60")
    monkeypatch.setenv("MIMO_WORKERS", "2")
    settings = Settings.from_env()
    assert settings.port == 9001
    assert settings.api_key == "s3cret"
    assert settings.reauth_cooldown == 60.0
    assert settings.workers == 2


def test_settings_invalid_int_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIMO_PORT", "not-a-port")
    with pytest.raises(RuntimeError, match="MIMO_PORT"):
        Settings.from_env()


def test_secret_prefers_file_and_hot_reloads(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "serviceToken"
    path.write_text("token-one\n", encoding="utf-8")
    secret = _Secret("SERVICE_TOKEN", inline="inline-token", path=path)

    assert secret.source == "file"
    assert secret.get() == "token-one"

    # 改文件（确保 mtime 变化），并关掉 1s 检查节流
    time.sleep(0.05)
    path.write_text("token-two\n", encoding="utf-8")
    monkeypatch.setattr(_Secret, "CHECK_INTERVAL", 0.0)
    assert secret.get() == "token-two"


def test_secret_falls_back_to_inline_when_file_missing(tmp_path) -> None:
    secret = _Secret("SERVICE_TOKEN", inline="inline-token", path=tmp_path / "nope")
    assert secret.get() == "inline-token"
    assert secret.source == "env"  # 文件未提供值，实际来源是内联


def test_secret_set_writes_back_to_file(tmp_path) -> None:
    path = tmp_path / "serviceToken"
    path.write_text("old\n", encoding="utf-8")
    secret = _Secret("SERVICE_TOKEN", path=path)
    assert secret.get() == "old"

    secret.set("rotated==value")
    assert path.read_text(encoding="utf-8").strip() == "rotated==value"
    assert secret.get() == "rotated==value"


def test_secret_set_inline_when_no_file() -> None:
    secret = _Secret("SERVICE_TOKEN", "old")
    secret.set("new")
    assert secret.get() == "new"


def _dual_secret(tmp_path, *, yaml_token: str, file_token: str | None, inline: str | None = None):
    """store + path 并存（方案 A 双开场景）。"""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "credentials:\n"
        f"  serviceToken: '{yaml_token}'\n" if yaml_token else "credentials:\n  serviceToken: \"\"\n",
        encoding="utf-8",
    )
    store = YamlStore(str(cfg))
    path = tmp_path / "serviceToken"
    if file_token is not None:
        path.write_text(file_token + "\n", encoding="utf-8")
    return _Secret(
        "SERVICE_TOKEN",
        inline=inline,
        path=path,
        store=store,
        store_key="credentials.serviceToken",
    )


def test_dual_store_and_path_set_visible_to_get(tmp_path) -> None:
    """set 写 yaml、get 先读 yaml——续登写回对读立即可见（方案 A 核心）。"""
    secret = _dual_secret(tmp_path, yaml_token="YAML-OLD", file_token="FILE-OLD")
    assert secret.get() == "YAML-OLD"
    assert secret.source == "yaml"

    secret.set("NEW-TOK")
    assert secret.get() == "NEW-TOK"
    assert secret.source == "yaml"
    # 文件未被动过，旧值仍在；yaml 已是新值
    assert (tmp_path / "serviceToken").read_text(encoding="utf-8").strip() == "FILE-OLD"
    assert "NEW-TOK" in (tmp_path / "config.yaml").read_text(encoding="utf-8")


def test_dual_empty_yaml_falls_back_to_file(tmp_path) -> None:
    """config.yaml.example 式空串 → None → 回落 *_FILE 预填。"""
    secret = _dual_secret(tmp_path, yaml_token="", file_token="FILE-BOOT")
    assert secret.get() == "FILE-BOOT"
    assert secret.source == "file"


def test_dual_nonempty_yaml_wins_over_file(tmp_path) -> None:
    secret = _dual_secret(tmp_path, yaml_token="YAML-WINS", file_token="FILE-LOSES")
    assert secret.get() == "YAML-WINS"
    assert secret.source == "yaml"


def test_dual_empty_yaml_missing_file_uses_inline(tmp_path) -> None:
    secret = _dual_secret(tmp_path, yaml_token="", file_token=None, inline="INLINE-TOK")
    assert secret.get() == "INLINE-TOK"
    assert secret.source == "env"


def test_credential_store_from_values_cookie_header() -> None:
    store = CredentialStore.from_values(
        service_token='"tok=="',
        user_id="42",
        slh='"slh"',
        ph='"ph"',
    )
    cookie = store.cookie_header()
    assert "api-platform_serviceToken=\"tok==\"" in cookie
    assert "userId=42" in cookie
    assert "api-platform_slh=\"slh\"" in cookie
    assert "api-platform_ph=\"ph\"" in cookie
    assert store.configured is True
    assert store.sources()["serviceToken"] == "env"


def test_credential_store_empty() -> None:
    store = CredentialStore.from_values()
    assert store.configured is False
    assert store.cookie_header() == ""
    assert store.sources()["passToken"] == "none"


def test_persist_session_updates_all_fields(tmp_path) -> None:
    store = CredentialStore.from_values()
    store.persist_session(
        service_token="new-tok",
        user_id="7",
        ph="new-ph",
        slh="new-slh",
    )
    assert store.service_token.get() == "new-tok"
    assert store.user_id.get() == "7"
    assert store.cookie_header().startswith("api-platform_serviceToken=new-tok")


def test_credential_store_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIMO_SERVICE_TOKEN", "env-tok")
    monkeypatch.setenv("MIMO_PASS_TOKEN", "env-pass")
    monkeypatch.setenv("MIMO_DEVICE_ID", "wb_env")
    store = CredentialStore.from_env()
    assert store.service_token.get() == "env-tok"
    assert store.pass_token.get() == "env-pass"
    assert store.device_id.get() == "wb_env"
    assert store.configured is True

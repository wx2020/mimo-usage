"""config.yaml 集中配置：加载优先级、密码读后即焚迁移、原子写回。"""

from __future__ import annotations

import errno
import hashlib
import stat
from pathlib import Path

import bcrypt
import pytest

from mimo_usage.config import CredentialStore, Settings, YamlStore, _discover_config


def write_config(path, text: str) -> None:
    path.write_text(text.lstrip(), encoding="utf-8")
    path.chmod(0o644)


def test_discover_requires_existing_file(tmp_path) -> None:
    assert _discover_config(None) is None
    with pytest.raises(RuntimeError, match="missing file"):
        _discover_config(str(tmp_path / "nope.yaml"))
    ok = tmp_path / "config.yaml"
    ok.write_text("server: {}\n")
    assert _discover_config(str(ok)) == ok


def test_settings_prefer_env_over_yaml(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "config.yaml"
    write_config(
        cfg,
        """
server:
  port: 9001
  accessLog: false
  apiKey: yaml-key
upstream:
  baseUrl: https://yaml.example
reauth:
  cooldownSeconds: 60
  passwordCooldownSeconds: 7200
""",
    )
    monkeypatch.delenv("MIMO_PORT", raising=False)
    settings = Settings.from_env(str(cfg))
    assert settings.port == 9001
    assert settings.access_log is False
    assert settings.api_key == "yaml-key"
    assert settings.base_url == "https://yaml.example"
    assert settings.reauth_cooldown == 60.0
    assert settings.password_reauth_cooldown == 7200.0
    assert settings.config_file == str(cfg)

    monkeypatch.setenv("MIMO_PORT", "9500")
    assert Settings.from_env(str(cfg)).port == 9500


def test_no_config_file_keeps_env_behaviour(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MIMO_CONFIG", raising=False)
    monkeypatch.setenv("MIMO_PORT", "8123")
    settings = Settings.from_env()
    assert settings.port == 8123
    assert settings.config_file is None


def test_credentials_load_from_yaml(tmp_path) -> None:
    cfg = tmp_path / "config.yaml"
    write_config(
        cfg,
        """
credentials:
  serviceToken: '"tok-from-yaml"'
  passToken: pass-from-yaml
  deviceId: wb_yaml-device
  userId: 100000001
  username: "00000000000"
""",
    )
    creds = CredentialStore.from_env(str(cfg))
    assert creds.service_token.get() == '"tok-from-yaml"'
    assert creds.service_token.source == "yaml"
    assert creds.device_id.get() == "wb_yaml-device"
    assert creds.user_id.get() == "100000001"  # int in yaml → str out
    assert creds.username.get() == "00000000000"
    assert creds.configured is True
    assert "cookie" not in creds.cookie_header()
    assert "api-platform_serviceToken=\"tok-from-yaml\"" in creds.cookie_header()


def test_password_read_once_then_bcrypt_and_removed(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MIMO_PASSWORD", raising=False)
    cfg = tmp_path / "config.yaml"
    write_config(
        cfg,
        """
server:
  port: 8000
credentials:
  username: "00000000000"
  password: S3cret-Plaintext!
""",
    )
    creds = CredentialStore.from_env(str(cfg))
    raw = cfg.read_text(encoding="utf-8")

    # 明文消失，md5 + bcrypt 就位
    assert "S3cret-Plaintext!" not in raw
    assert "password:" not in raw.split("passwordBcrypt")[0]  # 原 password 键已删
    expected_md5 = hashlib.md5(b"S3cret-Plaintext!").hexdigest().upper()
    assert creds.password_md5.get() == expected_md5
    assert "passwordMd5" in raw
    bcrypt_line = next(line for line in raw.splitlines() if "passwordBcrypt" in line)
    bcrypt_value = bcrypt_line.split(":", 1)[1].strip().strip('"')
    assert bcrypt_value.startswith(("$2a$", "$2b$"))
    assert bcrypt.checkpw(b"S3cret-Plaintext!", bcrypt_value.encode())
    assert creds.password_login_ready is True

    # 落盘权限收紧为 600
    assert stat.S_IMODE(cfg.stat().st_mode) == 0o600

    # 幂等：再加载一次不改动（bcrypt 盐不重算）
    before = cfg.read_text(encoding="utf-8")
    CredentialStore.from_env(str(cfg))
    assert cfg.read_text(encoding="utf-8") == before


def test_password_from_env_writes_hash_but_never_plaintext(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "config.yaml"
    write_config(cfg, "server:\n  port: 8000\n")
    monkeypatch.setenv("MIMO_PASSWORD", "Env-Only-Pass")
    try:
        creds = CredentialStore.from_env(str(cfg))
    finally:
        monkeypatch.delenv("MIMO_PASSWORD", raising=False)
    raw = cfg.read_text(encoding="utf-8")
    assert "Env-Only-Pass" not in raw  # env 明文绝不写入文件
    assert creds.password_md5.get() == hashlib.md5(b"Env-Only-Pass").hexdigest().upper()
    assert "passwordBcrypt" in raw


def test_env_overrides_yaml_credentials(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "config.yaml"
    write_config(cfg, 'credentials:\n  serviceToken: yaml-tok\n')
    monkeypatch.setenv("MIMO_SERVICE_TOKEN", "env-tok")
    try:
        creds = CredentialStore.from_env(str(cfg))
        assert creds.service_token.get() == "env-tok"
        assert creds.service_token.source == "env"
    finally:
        monkeypatch.delenv("MIMO_SERVICE_TOKEN", raising=False)


def test_persist_session_writes_back_to_yaml(tmp_path) -> None:
    cfg = tmp_path / "config.yaml"
    write_config(
        cfg,
        """
credentials:
  serviceToken: old-tok
  userId: "1"
  ph: old-ph
  slh: old-slh
""",
    )
    creds = CredentialStore.from_env(str(cfg))
    creds.persist_session(service_token="new-tok", user_id="42", ph="new-ph", slh="new-slh")

    store = YamlStore(cfg)
    assert store.get("credentials.serviceToken") == "new-tok"
    assert store.get("credentials.userId") == "42"
    assert store.get("credentials.ph") == "new-ph"
    assert store.get("credentials.slh") == "new-slh"
    # 其它键不丢
    assert store.get("credentials") is None  # get returns leaves only
    assert "username" not in (cfg.read_text(encoding="utf-8"))  # 未涉及的键未被发明
    assert stat.S_IMODE(cfg.stat().st_mode) == 0o600


def test_yaml_store_set_is_atomic_and_preserves_sections(tmp_path) -> None:
    cfg = tmp_path / "config.yaml"
    write_config(cfg, 'server:\n  port: 8000\ncredentials:\n  deviceId: wb_x\n')
    store = YamlStore(cfg)
    store.set("credentials.serviceToken", "rotated")
    assert store.get("credentials.serviceToken") == "rotated"
    assert store.get("credentials.deviceId") == "wb_x"
    raw = cfg.read_text(encoding="utf-8")
    assert "port: 8000" in raw
    assert not list(tmp_path.glob(".*.tmp"))  # 临时文件已替换


def test_yaml_store_falls_back_to_inplace_write_when_rename_busy(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """单文件 bind mount：rename 覆盖挂载点报 EBUSY → 退化原地写且不报错。"""
    cfg = tmp_path / "config.yaml"
    write_config(cfg, 'credentials:\n  serviceToken: old-tok\n')
    store = YamlStore(cfg)

    def busy(self: Path, target: Path) -> None:  # noqa: ARG001
        raise OSError(errno.EBUSY, "Device or resource busy", str(self), str(target))

    monkeypatch.setattr(Path, "replace", busy)
    store.set("credentials.serviceToken", "rotated")

    assert store.get("credentials.serviceToken") == "rotated"
    assert "rotated" in cfg.read_text(encoding="utf-8")
    assert stat.S_IMODE(cfg.stat().st_mode) == 0o600
    assert not list(tmp_path.glob(".*.tmp"))  # 临时文件已清理


def test_persist_session_survives_store_write_failure(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """落盘失败（如只读挂载）不应让续登/请求抛错：内存值仍可用。"""
    cfg = tmp_path / "config.yaml"
    write_config(cfg, 'credentials:\n  serviceToken: old-tok\n')
    creds = CredentialStore.from_env(str(cfg))

    def boom(self: YamlStore) -> None:
        self._dirty = True  # 与真实 _save 一致：失败标记内存新于磁盘
        raise OSError(errno.EROFS, "Read-only file system")

    monkeypatch.setattr(YamlStore, "_save", boom)
    creds.persist_session(service_token="new-tok", user_id="42")  # 不抛异常

    assert creds.service_token.get() == "new-tok"
    assert creds.user_id.get() == "42"


def test_credentials_from_env_without_yaml_still_works(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MIMO_CONFIG", raising=False)
    creds = CredentialStore.from_env()
    assert creds.store is None
    assert creds.sources()["config"] == "none"

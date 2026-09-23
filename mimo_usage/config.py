"""Configuration and credential resolution for the MiMo usage server.

配置分两层，优先级 **环境变量 > config.yaml > 默认值**：

* ``config.yaml``（``MIMO_CONFIG`` 指定路径，``./run.sh`` 会在文件存在时自动导出）——
  运行配置与凭据集中于此；``credentials.*`` 按 mtime 热重载，续登换发的
  serviceToken/ph/slh/userId/passToken 原子写回该文件。
* 凭据中的**密码明文**只允许在 ``credentials.password`` 出现一次：服务**加载时立即**
  换算为 ``passwordMd5``（登录所需，MD5 大写）与 ``passwordBcrypt``（``$2b$10$…``
  bcrypt，格式同 ``$2a$`` 示例族）并**从文件删除明文**——之后磁盘上不再有明文密码。
* ``*_FILE`` 文件后端仍然支持；``_Secret.get()`` 实序见下——**设了 ``*_FILE`` 则
  文件 > 内联 env > 无（yaml 不参与读取）；未设则 内联 env > yaml > 无**。
"""

from __future__ import annotations

import hashlib
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

import bcrypt
import yaml

from . import __version__

T = TypeVar("T")


def _raw(name: str) -> str | None:
    value = os.getenv(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _str(name: str, default: str | None = None) -> str | None:
    return _raw(name) or default


def _coerce(name: str, default: T, cast: Callable[[str], T]) -> T:
    value = _raw(name)
    if value is None:
        return default
    try:
        return cast(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"environment variable {name} has invalid value {value!r}") from exc


def _int(name: str, default: int) -> int:
    return _coerce(name, default, int)


def _float(name: str, default: float) -> float:
    return _coerce(name, default, float)


def _bool(name: str, default: bool) -> bool:
    value = _raw(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def _discover_config(explicit: str | None = None) -> Path | None:
    """Locate config.yaml. Only an explicit ``MIMO_CONFIG`` is honoured (tests
    and ad-hoc runs must never accidentally pick up a real credential file)."""
    raw = explicit if explicit is not None else _raw("MIMO_CONFIG")
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_file():
        raise RuntimeError(f"MIMO_CONFIG points to a missing file: {path}")
    return path


def _load_yaml(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise RuntimeError(f"cannot read config file {path}: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise RuntimeError(f"config file {path} must be a mapping at the top level")
    return data


def _ybool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class Settings:
    """Immutable runtime settings (env > config.yaml > defaults)."""

    base_url: str = "https://platform.xiaomimimo.com"
    account_base_url: str = "https://account.xiaomi.com"
    sid: str = "api-platform"

    host: str = "0.0.0.0"
    port: int = 8000
    workers: int = 1
    debug: bool = False
    access_log: bool = True

    request_timeout: float = 15.0
    connect_timeout: float = 5.0
    max_connections: int = 32
    max_keepalive_connections: int = 16
    retries: int = 2
    retry_backoff: float = 0.3

    usage_ttl: float = 300.0
    detail_ttl: float = 600.0
    bill_ttl: float = 3600.0
    token_plan_ttl: float = 600.0
    account_ttl: float = 3600.0
    stale_ttl: float = 600.0
    cache_max_entries: int = 256
    refresh_min_interval: float = 30.0

    default_usage_days: int = 7
    max_range_days: int = 366
    dashboard_refresh_seconds: int = 30
    summary_max_age: float = 300.0
    dashboard_path: str = "/dashboard"
    api_key: str | None = None

    reauth_cooldown: float = 300.0
    #: 密码登录（含触发验证码）失败后的冷却：撞风控代价高，窗口拉长
    password_reauth_cooldown: float = 3600.0

    config_file: str | None = None
    version: str = __version__

    @classmethod
    def from_env(cls, config_file: str | None = None) -> Settings:
        path = _discover_config(config_file)
        y = _load_yaml(path)
        server = dict(y.get("server") or {})
        upstream = dict(y.get("upstream") or {})
        cache = dict(y.get("cache") or {})
        reauth = dict(y.get("reauth") or {})

        def sval(section: dict[str, Any], key: str, env: str, default: str) -> str:
            v = _raw(env)
            if v is not None:
                return v
            value = section.get(key)
            return default if value is None else str(value)

        def ival(section: dict[str, Any], key: str, env: str, default: int) -> int:
            v = _raw(env)
            if v is not None:
                return _int(env, default)
            value = section.get(key)
            return default if value is None else int(value)

        def fval(section: dict[str, Any], key: str, env: str, default: float) -> float:
            v = _raw(env)
            if v is not None:
                return _float(env, default)
            value = section.get(key)
            return default if value is None else float(value)

        def bval(section: dict[str, Any], key: str, env: str, default: bool) -> bool:
            v = _raw(env)
            if v is not None:
                return _bool(env, default)
            value = section.get(key)
            return default if value is None else _ybool(value)

        api_key = _raw("MIMO_API_KEY")
        if api_key is None:
            value = server.get("apiKey")
            api_key = None if value is None else str(value)

        return cls(
            base_url=sval(upstream, "baseUrl", "MIMO_BASE_URL", "https://platform.xiaomimimo.com").rstrip("/"),
            account_base_url=sval(
                upstream, "accountBaseUrl", "MIMO_ACCOUNT_BASE_URL", "https://account.xiaomi.com"
            ).rstrip("/"),
            sid=sval(upstream, "sid", "MIMO_SID", "api-platform"),
            host=sval(server, "host", "MIMO_HOST", "0.0.0.0"),
            port=ival(server, "port", "MIMO_PORT", 8000),
            workers=ival(server, "workers", "MIMO_WORKERS", 1),
            debug=bval(server, "debug", "MIMO_DEBUG", False),
            access_log=bval(server, "accessLog", "MIMO_ACCESS_LOG", True),
            request_timeout=fval(upstream, "requestTimeout", "MIMO_REQUEST_TIMEOUT", 15.0),
            connect_timeout=fval(upstream, "connectTimeout", "MIMO_CONNECT_TIMEOUT", 5.0),
            max_connections=ival(upstream, "maxConnections", "MIMO_MAX_CONNECTIONS", 32),
            max_keepalive_connections=ival(
                upstream, "maxKeepaliveConnections", "MIMO_MAX_KEEPALIVE_CONNECTIONS", 16
            ),
            retries=ival(upstream, "retries", "MIMO_RETRIES", 2),
            retry_backoff=fval(upstream, "retryBackoff", "MIMO_RETRY_BACKOFF", 0.3),
            usage_ttl=fval(cache, "usageTtl", "MIMO_USAGE_TTL", 300.0),
            detail_ttl=fval(cache, "detailTtl", "MIMO_DETAIL_TTL", 600.0),
            bill_ttl=fval(cache, "billTtl", "MIMO_BILL_TTL", 3600.0),
            token_plan_ttl=fval(cache, "tokenPlanTtl", "MIMO_TOKEN_PLAN_TTL", 600.0),
            account_ttl=fval(cache, "accountTtl", "MIMO_ACCOUNT_TTL", 3600.0),
            stale_ttl=fval(cache, "staleTtl", "MIMO_STALE_TTL", 600.0),
            cache_max_entries=ival(cache, "cacheMaxEntries", "MIMO_CACHE_MAX_ENTRIES", 256),
            refresh_min_interval=fval(cache, "refreshMinInterval", "MIMO_REFRESH_MIN_INTERVAL", 30.0),
            default_usage_days=ival(cache, "defaultUsageDays", "MIMO_DEFAULT_USAGE_DAYS", 7),
            max_range_days=ival(cache, "maxRangeDays", "MIMO_MAX_RANGE_DAYS", 366),
            dashboard_refresh_seconds=ival(server, "dashboardRefreshSeconds", "MIMO_DASHBOARD_REFRESH_SECONDS", 30),
            summary_max_age=fval(cache, "summaryMaxAge", "MIMO_SUMMARY_MAX_AGE", 300.0),
            dashboard_path=sval(server, "dashboardPath", "MIMO_DASHBOARD_PATH", "/dashboard") or "/dashboard",
            api_key=api_key,
            reauth_cooldown=fval(reauth, "cooldownSeconds", "MIMO_REAUTH_COOLDOWN", 300.0),
            password_reauth_cooldown=fval(
                reauth, "passwordCooldownSeconds", "MIMO_PASSWORD_REAUTH_COOLDOWN", 3600.0
            ),
            config_file=str(path) if path else None,
        )


class YamlStore:
    """config.yaml 的凭据段读写：mtime 热载 + 原子写回（0600）。"""

    __slots__ = ("path", "_data", "_mtime", "_checked_at")

    CHECK_INTERVAL = 1.0

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._data: dict[str, Any] = {}
        self._mtime: float | None = None
        self._checked_at = 0.0

    def _load(self, *, force: bool = False) -> dict[str, Any]:
        now = time.monotonic()
        if not force and self._mtime is not None and now - self._checked_at < self.CHECK_INTERVAL:
            return self._data
        self._checked_at = now
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            return self._data
        if force or mtime != self._mtime:
            data = _load_yaml(self.path)
            self._data = data
            self._mtime = mtime
        return self._data

    @staticmethod
    def _walk(data: dict[str, Any], dotted: str) -> Any:
        node: Any = data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return None
            node = node[part]
        return node

    def get(self, dotted: str) -> str | None:
        node: Any = self._load()
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return None
            node = node[part]
        if node is None:
            return None
        if isinstance(node, str):
            return node.strip() or None
        if isinstance(node, bool):
            return "true" if node else "false"
        if isinstance(node, (dict, list)):
            return None  # 只取标量叶子
        return str(node)

    def set(self, dotted: str, value: str) -> None:
        data = self._load(force=True)
        parts = dotted.split(".")
        node: dict[str, Any] = data
        for part in parts[:-1]:
            child = node.get(part)
            if not isinstance(child, dict):
                child = {}
                node[part] = child
            node = child
        node[parts[-1]] = value
        self._save()

    def delete(self, dotted: str) -> bool:
        data = self._load(force=True)
        parts = dotted.split(".")
        node: Any = data
        for part in parts[:-1]:
            if not isinstance(node, dict) or part not in node:
                return False
            node = node[part]
        if not isinstance(node, dict) or parts[-1] not in node:
            return False
        del node[parts[-1]]
        self._save()
        return True

    def _save(self) -> None:
        text = yaml.safe_dump(self._data, allow_unicode=True, sort_keys=False, default_flow_style=False)
        tmp = self.path.parent / f".{self.path.name}.tmp"
        tmp.write_text(text, encoding="utf-8")
        tmp.chmod(0o600)  # 内含凭据：写回后文件必须是 600
        tmp.replace(self.path)
        self._mtime = None  # 强制下次 stat 重读

    def migrate_password(self, plaintext_env: str | None = None) -> bool:
        """读取一次明文密码 → ``passwordMd5`` + ``passwordBcrypt`` → 删除明文。

        明文来源：``MIMO_PASSWORD``（env，优先）或 ``credentials.password``（yaml，
        处理后删除；env 明文本身不写入文件）。返回是否改动了文件。
        幂等：无明文、且 md5 已一致时不动文件（bcrypt 盐不重算）。
        """
        plaintext = (plaintext_env or "").strip() or self.get("credentials.password")
        if not plaintext:
            return False

        md5 = hashlib.md5(plaintext.encode("utf-8")).hexdigest().upper()
        current_md5 = self.get("credentials.passwordMd5")
        changed = False

        if current_md5 != md5:
            self.set("credentials.passwordMd5", md5)
            changed = True
            # 密码变了：bcrypt 一并按新值重打
            self.set(
                "credentials.passwordBcrypt",
                bcrypt.hashpw(plaintext.encode("utf-8"), bcrypt.gensalt(rounds=10)).decode("ascii"),
            )
        elif not self.get("credentials.passwordBcrypt"):
            self.set(
                "credentials.passwordBcrypt",
                bcrypt.hashpw(plaintext.encode("utf-8"), bcrypt.gensalt(rounds=10)).decode("ascii"),
            )

        if self.delete("credentials.password"):
            changed = True
        return changed


class _Secret:
    """一个凭据位（读 ``get``，与测试 ``test_secret_prefers_file_*`` 一致）：

    * 设了 ``*_FILE``（path 非空）：**文件 > 内联 env > 无**——yaml 不参与读取；
      文件缺失/为空时回落内联，两者皆无则 ``None``（不会读 yaml）。
    * 未设 path：**内联 env > yaml > 无**。
    * 写回 ``set``：有 yaml store 则**只写 yaml**（原子、0600），否则写文件，再否则改内存；
      store 与 path 并存时写 yaml、读文件——续登新值在文件未同步前对 ``get`` 不可见。
    """

    __slots__ = ("name", "_inline", "_path", "_mtime", "_cached", "_checked_at", "_store", "_store_key")

    CHECK_INTERVAL = 1.0

    def __init__(
        self,
        name: str,
        inline: str | None = None,
        path: str | Path | None = None,
        *,
        store: YamlStore | None = None,
        store_key: str | None = None,
    ) -> None:
        self.name = name
        self._inline = inline.strip() if inline and inline.strip() else None
        self._path = Path(path).expanduser() if path else None
        self._mtime: float | None = None
        self._cached: str | None = None
        self._checked_at = 0.0
        self._store = store
        self._store_key = store_key

    @property
    def source(self) -> str:
        if self._path is not None:
            return "file"
        if self._inline:
            return "env"
        if self._store is not None and self._store.get(self._store_key or ""):
            return "yaml"
        return "none"

    def get(self) -> str | None:
        if self._path is None:
            if self._inline:
                return self._inline
            if self._store is not None and self._store_key:
                return self._store.get(self._store_key)
            return None
        now = time.monotonic()
        if self._mtime is not None and now - self._checked_at < self.CHECK_INTERVAL:
            return self._cached or self._inline
        self._checked_at = now
        try:
            mtime = self._path.stat().st_mtime
        except OSError:
            return self._cached or self._inline
        if mtime != self._mtime:
            try:
                value = self._path.read_text(encoding="utf-8").strip()
            except OSError:
                return self._cached or self._inline
            self._cached = value or None
            self._mtime = mtime
        return self._cached or self._inline

    def set(self, value: str) -> None:
        """续登成功后写回：优先 config.yaml（原子、0600），其次文件，再次内存。"""
        value = (value or "").strip()
        if self._store is not None and self._store_key:
            self._store.set(self._store_key, value)
            return
        if self._path is not None:
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                self._path.write_text(value + "\n", encoding="utf-8")
                self._mtime = self._path.stat().st_mtime
            except OSError:
                self._cached = value or None
                return
            self._cached = value or None
        else:
            self._inline = value or None


class CredentialStore:
    """serviceToken / passToken / 密码材料等凭证的统一读写入口。"""

    __slots__ = (
        "service_token",
        "pass_token",
        "device_id",
        "user_id",
        "ph",
        "slh",
        "username",
        "device_fingerprint",
        "password_md5",
        "store",
    )

    def __init__(
        self,
        *,
        service_token: _Secret,
        pass_token: _Secret,
        device_id: _Secret,
        user_id: _Secret,
        ph: _Secret,
        slh: _Secret,
        username: _Secret,
        device_fingerprint: _Secret,
        password_md5: _Secret,
        store: YamlStore | None = None,
    ) -> None:
        self.service_token = service_token
        self.pass_token = pass_token
        self.device_id = device_id
        self.user_id = user_id
        self.ph = ph
        self.slh = slh
        self.username = username
        self.device_fingerprint = device_fingerprint
        self.password_md5 = password_md5
        self.store = store

    @classmethod
    def from_env(cls, config_file: str | None = None) -> CredentialStore:
        path = _discover_config(config_file)
        store = YamlStore(path) if path else None
        pw_env = _raw("MIMO_PASSWORD")
        if store is not None:
            # 加载即迁移：明文密码在第一次读取后立即变为 md5+bcrypt 并从文件消失
            store.migrate_password(plaintext_env=pw_env)

        def secret(name: str, yaml_key: str, inline: str | None = None) -> _Secret:
            return _Secret(
                name,
                inline if inline is not None else _raw(f"MIMO_{name}"),
                _raw(f"MIMO_{name}_FILE"),
                store=store,
                store_key=f"credentials.{yaml_key}",
            )

        # 密码 md5：显式 HASH env > 由 env 明文派生（纯 env 模式也无 yaml 可落） > yaml
        hash_inline = _raw("MIMO_PASSWORD_HASH")
        if hash_inline is None and pw_env:
            hash_inline = hashlib.md5(pw_env.encode("utf-8")).hexdigest().upper()

        return cls(
            service_token=secret("SERVICE_TOKEN", "serviceToken"),
            pass_token=secret("PASS_TOKEN", "passToken"),
            device_id=secret("DEVICE_ID", "deviceId"),
            user_id=secret("USER_ID", "userId"),
            ph=secret("PH", "ph"),
            slh=secret("SLH", "slh"),
            username=secret("USERNAME", "username"),
            device_fingerprint=secret("DEVICE_FINGERPRINT", "deviceFingerprint"),
            password_md5=secret("PASSWORD_HASH", "passwordMd5", inline=hash_inline),
            store=store,
        )

    @classmethod
    def from_values(
        cls,
        *,
        service_token: str | None = None,
        pass_token: str | None = None,
        device_id: str | None = None,
        user_id: str | None = None,
        ph: str | None = None,
        slh: str | None = None,
        username: str | None = None,
        device_fingerprint: str | None = None,
        password_md5: str | None = None,
    ) -> CredentialStore:
        """测试用：全部内联，不读环境变量也不碰 config.yaml。"""
        return cls(
            service_token=_Secret("SERVICE_TOKEN", service_token),
            pass_token=_Secret("PASS_TOKEN", pass_token),
            device_id=_Secret("DEVICE_ID", device_id),
            user_id=_Secret("USER_ID", user_id),
            ph=_Secret("PH", ph),
            slh=_Secret("SLH", slh),
            username=_Secret("USERNAME", username),
            device_fingerprint=_Secret("DEVICE_FINGERPRINT", device_fingerprint),
            password_md5=_Secret("PASSWORD_HASH", password_md5),
        )

    @property
    def configured(self) -> bool:
        return bool(self.service_token.get())

    @property
    def password_login_ready(self) -> bool:
        """密码续登材料是否齐备（username + passwordMd5）。"""
        return bool(self.username.get() and self.password_md5.get())

    def cookie_header(self) -> str:
        """platform.xiaomimimo.com 请求的 Cookie（值含引号原样保留）。"""
        parts: list[str] = []
        token = self.service_token.get()
        if token:
            parts.append(f"api-platform_serviceToken={_cookie_value(token)}")
        uid = self.user_id.get()
        if uid:
            parts.append(f"userId={uid}")
        slh = self.slh.get()
        if slh:
            parts.append(f"api-platform_slh={_cookie_value(slh)}")
        ph = self.ph.get()
        if ph:
            parts.append(f"api-platform_ph={_cookie_value(ph)}")
        return "; ".join(parts)

    def sources(self) -> dict[str, str]:
        return {
            "serviceToken": self.service_token.source,
            "passToken": self.pass_token.source,
            "deviceId": self.device_id.source,
            "password": "md5" if self.password_md5.get() else "none",
            "config": "yaml" if self.store is not None else "none",
        }

    def persist_session(
        self,
        *,
        service_token: str | None = None,
        user_id: str | None = None,
        ph: str | None = None,
        slh: str | None = None,
    ) -> None:
        """续登成功：把 /sts 下发的新会话凭证落盘（yaml 原子写回）。"""
        if service_token:
            self.service_token.set(service_token)
        if user_id:
            self.user_id.set(user_id)
        if ph:
            self.ph.set(ph)
        if slh:
            self.slh.set(slh)


def _cookie_value(raw: str) -> str:
    value = raw.strip()
    if value.startswith('"') and value.endswith('"') and len(value) >= 2:
        return value
    if any(ch in value for ch in ' ,;\\'):
        return f'"{value}"'
    return value

"""两个配置模板必须与代码保持同步。

`.env.example`（环境变量形态）与 `config.yaml.example`（配置文件形态）是同一套选项的
两种写法；本文件从源码自动推导"应当存在"的选项，防止新增配置后模板漂移。
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import yaml

from mimo_usage import config as config_module
from mimo_usage.config import Settings

ROOT = Path(__file__).resolve().parents[1]
ENV_EXAMPLE = ROOT / ".env.example"
YAML_EXAMPLE = ROOT / "config.yaml.example"

SECTION_VARS = {"server", "upstream", "cache", "reauth"}

#: yaml 键与 env 名不是机械转换的例外（键名语义化，env 名带前缀）
ENV_NAME_ALIASES = {
    "reauth.cooldownSeconds": "MIMO_REAUTH_COOLDOWN",
    "reauth.passwordCooldownSeconds": "MIMO_PASSWORD_REAUTH_COOLDOWN",
}


def env_example_text() -> str:
    return ENV_EXAMPLE.read_text(encoding="utf-8")


def documented_env_names(text: str) -> set[str]:
    """`.env.example` 中出现的 MIMO_* 名（含被注释的示例行）。"""
    return set(re.findall(r"^\s*#?\s*(MIMO_[A-Z0-9_]+)=", text, flags=re.MULTILINE))


def yaml_example_paths() -> set[str]:
    data = yaml.safe_load(YAML_EXAMPLE.read_text(encoding="utf-8")) or {}
    paths: set[str] = set()

    def walk(node: dict, prefix: str) -> None:
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(value, dict):
                walk(value, path)
            else:
                paths.add(path)

    walk(data, "")
    return paths


def code_env_names() -> set[str]:
    """源码里出现的 MIMO_* 名 + 动态生成的凭证名（含 *_FILE）。"""
    src = inspect.getsource(config_module)
    names = set(re.findall(r"(MIMO_[A-Z0-9_]+)", src))
    for name in re.findall(r'secret\(\s*"([A-Z0-9_]+)"', src):
        names.add(f"MIMO_{name}")
        names.add(f"MIMO_{name}_FILE")
    return names


def code_yaml_paths() -> set[str]:
    """from_env 里读取的 yaml 键（sval/ival/fval/bval 调用与 section.get）。"""
    src = inspect.getsource(Settings.from_env)
    paths: set[str] = set()
    for section, key in re.findall(r'(?:sval|ival|fval|bval)\(\s*(\w+),\s*"([A-Za-z0-9]+)"', src):
        if section in SECTION_VARS:
            paths.add(f"{section}.{key}")
    for section, key in re.findall(r'(\w+)\.get\("([A-Za-z0-9]+)"', src):
        if section in SECTION_VARS:
            paths.add(f"{section}.{key}")
    return paths


def test_env_example_documents_every_supported_env_var() -> None:
    documented = documented_env_names(env_example_text())
    missing = sorted(code_env_names() - documented)
    assert not missing, f".env.example 缺少：{missing}"


def test_env_example_has_no_stale_env_var() -> None:
    documented = documented_env_names(env_example_text())
    unknown = sorted(documented - code_env_names())
    assert not unknown, f".env.example 含源码不认识的变量：{unknown}"


def test_yaml_example_matches_code_keys() -> None:
    provided = yaml_example_paths()
    expected = code_yaml_paths()
    assert expected, "未能从源码解析出 yaml 键（解析逻辑需更新）"
    missing = sorted(expected - provided)
    assert not missing, f"config.yaml.example 缺少：{missing}"


def test_two_examples_cover_the_same_logical_options() -> None:
    """env 模板与 yaml 模板覆盖同一批选项（凭证段另测：键名映射不一一对应）。"""
    env_names = documented_env_names(env_example_text())
    yaml_paths = {p for p in yaml_example_paths() if not p.startswith("credentials.")}

    # 每个非凭证 yaml 键都应有对应的 MIMO_* 环境变量名（camelCase → UPPER_SNAKE）
    def to_env_name(path: str) -> str:
        if path in ENV_NAME_ALIASES:
            return ENV_NAME_ALIASES[path]
        leaf = path.split(".")[-1]
        snake = re.sub(r"(?<!^)(?=[A-Z])", "_", leaf).upper()
        return f"MIMO_{snake}"

    missing_env = sorted(path for path in yaml_paths if to_env_name(path) not in env_names)
    assert not missing_env, f"yaml 有而 env 模板缺：{missing_env}"


def test_credentials_listed_in_both_examples() -> None:
    env_text = env_example_text()
    yaml_text = YAML_EXAMPLE.read_text(encoding="utf-8")
    # yaml 模板里 password/passwordMd5/passwordBcrypt 是注释示例，故查原文而非解析结果
    for key in (
        "serviceToken",
        "passToken",
        "deviceId",
        "userId",
        "ph",
        "slh",
        "username",
        "deviceFingerprint",
        "password",
        "passwordMd5",
        "passwordBcrypt",
    ):
        assert re.search(rf"^\s*#?\s*{key}:", yaml_text, flags=re.MULTILINE), (
            f"config.yaml.example 缺 credentials.{key}"
        )
    for env_name in (
        "MIMO_SERVICE_TOKEN",
        "MIMO_PASS_TOKEN",
        "MIMO_DEVICE_ID",
        "MIMO_USER_ID",
        "MIMO_PH",
        "MIMO_SLH",
        "MIMO_USERNAME",
        "MIMO_DEVICE_FINGERPRINT",
        "MIMO_PASSWORD",
        "MIMO_PASSWORD_HASH",
    ):
        assert env_name in env_text, f".env.example 缺 {env_name}"

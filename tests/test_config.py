"""配置文件和凭据包装层的自动化测试。"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from keyring.errors import KeyringError, PasswordDeleteError

from nt_medical_visa_appointment import credentials
from nt_medical_visa_appointment.config import (
    ConfigError,
    ensure_valid_config,
    load_config,
    save_config,
    validate_config,
)
from nt_medical_visa_appointment.models import MonitorConfig, SmtpConfig


def test_default_config_round_trip_does_not_store_password(tmp_path) -> None:
    """默认配置保存再读取后应保持一致，JSON 中不能出现密码。"""

    path = tmp_path / "config.json"
    original = MonitorConfig()

    assert load_config(path) == original
    assert save_config(original, path) == path
    assert load_config(path) == original

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["monitor"]["assessment_codes"] == ["501", "502", "705"]
    assert "password" not in path.read_text(encoding="utf-8").casefold()


def test_validation_reports_all_invalid_values() -> None:
    """界面应一次得到全部问题，而不是让用户逐个修正。"""

    invalid = replace(
        MonitorConfig(),
        timezone_name="Not/A-Timezone",
        poll_interval_seconds=30,
        alert_repeat_seconds=20,
        page_timeout_seconds=2,
        smtp=SmtpConfig(port=70_000),
    )

    errors = validate_config(invalid)
    assert len(errors) == 5
    assert any("时区" in error for error in errors)
    assert any("轮询" in error for error in errors)
    assert any("重复提醒" in error for error in errors)
    assert any("页面等待" in error for error in errors)
    assert any("SMTP 端口" in error for error in errors)
    with pytest.raises(ConfigError, match="配置无效"):
        ensure_valid_config(invalid)


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("{broken", "JSON 格式错误"),
        ('{"schema_version": 99}', "不支持配置版本"),
    ],
)
def test_invalid_json_and_schema_are_rejected(tmp_path, content: str, message: str) -> None:
    """损坏文件和未知版本必须明确失败，不能悄悄改用默认配置。"""

    path = tmp_path / "config.json"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(ConfigError, match=message):
        load_config(path)


def test_credentials_use_keyring_without_real_windows_access(monkeypatch) -> None:
    """保存、读取和删除均通过 mock keyring 完成，不访问真实凭据。"""

    stored: dict[tuple[str, str], str] = {}

    def get_password(service: str, key: str) -> str | None:
        return stored.get((service, key))

    def set_password(service: str, key: str, password: str) -> None:
        stored[(service, key)] = password

    def delete_password(service: str, key: str) -> None:
        try:
            del stored[(service, key)]
        except KeyError as exc:
            raise PasswordDeleteError("missing") from exc

    monkeypatch.setattr(credentials.keyring, "get_password", get_password)
    monkeypatch.setattr(credentials.keyring, "set_password", set_password)
    monkeypatch.setattr(credentials.keyring, "delete_password", delete_password)

    assert credentials.get_smtp_password() is None
    credentials.save_smtp_password("example-secret")
    assert credentials.get_smtp_password() == "example-secret"
    assert credentials.delete_smtp_password() is True
    assert credentials.delete_smtp_password() is False


def test_keyring_failure_is_converted_to_friendly_error(monkeypatch) -> None:
    """底层 keyring 错误不应直接泄漏到 GUI。"""

    def fail(*_args, **_kwargs):
        raise KeyringError("backend unavailable")

    monkeypatch.setattr(credentials.keyring, "get_password", fail)
    with pytest.raises(credentials.CredentialError, match="Windows 凭据管理器"):
        credentials.get_smtp_password()

    with pytest.raises(ValueError, match="不能为空"):
        credentials.save_smtp_password("")

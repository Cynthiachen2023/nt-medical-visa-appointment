"""应用配置的读取、校验和安全写入。

普通配置保存在用户的 Local AppData 目录中。SMTP 密码不属于普通配置，
因此本模块永远不会读取或写入密码；密码由 ``credentials`` 模块负责。
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from datetime import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import APP_NAME
from .models import MonitorConfig, SmtpConfig, SmtpSecurity


SCHEMA_VERSION = 1
CONFIG_FILENAME = "config.json"
AUSTRALIAN_STATES = frozenset({"ACT", "NSW", "NT", "QLD", "SA", "TAS", "VIC", "WA"})


class ConfigError(ValueError):
    """配置无法读取、解析或通过验证时抛出的异常。"""


def get_app_data_dir() -> Path:
    """返回当前用户的应用数据目录，但不会立即创建目录。

    正常的 Windows 安装环境都会提供 ``LOCALAPPDATA``。后备路径主要用于
    测试或环境变量异常的情况，确保错误不会把文件写到程序安装目录。
    """

    local_app_data = os.environ.get("LOCALAPPDATA")
    root = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
    return root / APP_NAME


def get_config_path() -> Path:
    """返回默认配置文件路径。"""

    return get_app_data_dir() / CONFIG_FILENAME


def _looks_like_email(value: str) -> bool:
    """执行适合桌面表单的基础邮箱格式检查。"""

    local, separator, domain = value.strip().rpartition("@")
    return bool(separator and local and domain and " " not in value)


def validate_config(
    config: MonitorConfig,
    *,
    require_email: bool = False,
) -> tuple[str, ...]:
    """返回全部配置问题；空元组表示配置有效。

    GUI 需要一次展示多个问题，因此本函数不会在发现第一个错误时停止。
    ``require_email`` 用于首次向导完成和测试邮件前的严格检查；尚未配置
    邮件时，程序仍可保存其他监控设置。
    """

    errors: list[str] = []
    parsed_url = urlparse(config.base_url.strip())
    if parsed_url.scheme.lower() != "https" or not parsed_url.netloc:
        errors.append("目标网址必须是完整的 HTTPS 地址。")

    if not (config.postcode.isdigit() and len(config.postcode) == 4):
        errors.append("邮编必须是 4 位数字。")
    if config.state not in AUSTRALIAN_STATES:
        errors.append("州必须是有效的澳大利亚州或领地缩写。")
    if not config.location_id.strip():
        errors.append("地点 ID 不能为空。")
    if not config.location_name.strip():
        errors.append("地点名称不能为空。")

    if not config.assessment_codes:
        errors.append("至少需要配置一个体检项目代码。")
    elif any(not code.isdigit() for code in config.assessment_codes):
        errors.append("体检项目代码必须全部为数字。")
    elif len(set(config.assessment_codes)) != len(config.assessment_codes):
        errors.append("体检项目代码不能重复。")

    try:
        ZoneInfo(config.timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        errors.append("无法识别配置的时区。")

    if config.start_time == config.end_time:
        errors.append("监控开始时间和结束时间不能相同。")
    if config.poll_interval_seconds < 60:
        errors.append("轮询间隔不能少于 60 秒。")
    if config.alert_repeat_seconds < config.poll_interval_seconds:
        errors.append("重复提醒间隔不能短于轮询间隔。")
    if config.error_alert_threshold < 1:
        errors.append("故障提醒阈值必须至少为 1 次。")
    if config.error_alert_cooldown_seconds < 60:
        errors.append("故障提醒冷却时间不能少于 60 秒。")
    if not 5 <= config.page_timeout_seconds <= 120:
        errors.append("页面等待超时必须在 5 到 120 秒之间。")

    smtp = config.smtp
    if not 1 <= smtp.port <= 65_535:
        errors.append("SMTP 端口必须在 1 到 65535 之间。")
    if smtp.sender and not _looks_like_email(smtp.sender):
        errors.append("SMTP 发件地址格式不正确。")
    if any(not _looks_like_email(address) for address in smtp.recipients):
        errors.append("一个或多个收件地址格式不正确。")

    if require_email:
        if not smtp.host.strip():
            errors.append("SMTP 服务器不能为空。")
        if not smtp.sender.strip():
            errors.append("发件地址不能为空。")
        if not smtp.recipients:
            errors.append("至少需要一个收件地址。")

    return tuple(errors)


def ensure_valid_config(
    config: MonitorConfig,
    *,
    require_email: bool = False,
) -> None:
    """验证配置，并把所有问题合并为一个便于显示的中文异常。"""

    errors = validate_config(config, require_email=require_email)
    if errors:
        formatted = "\n".join(f"- {message}" for message in errors)
        raise ConfigError(f"配置无效：\n{formatted}")


def _require_mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    """确认 JSON 节点是对象，避免出现难以理解的类型错误。"""

    if not isinstance(value, Mapping):
        raise ConfigError(f"配置项 {field_name} 必须是 JSON 对象。")
    return value


def _read_int(value: Any, field_name: str) -> int:
    """读取整数，同时拒绝 JSON 中容易被误当成 0/1 的布尔值。"""

    if isinstance(value, bool):
        raise ConfigError(f"配置项 {field_name} 必须是整数。")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"配置项 {field_name} 必须是整数。") from exc


def _read_bool(value: Any, field_name: str) -> bool:
    """读取严格的 JSON 布尔值。"""

    if not isinstance(value, bool):
        raise ConfigError(f"配置项 {field_name} 必须是 true 或 false。")
    return value


def _read_string_tuple(value: Any, field_name: str) -> tuple[str, ...]:
    """读取字符串数组，并去除每个值两端的空白。"""

    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ConfigError(f"配置项 {field_name} 必须是字符串数组。")
    return tuple(item.strip() for item in value)


def _read_time(value: Any, field_name: str) -> time:
    """读取 ISO 时间，例如 ``08:00`` 或 ``08:00:00``。"""

    if not isinstance(value, str):
        raise ConfigError(f"配置项 {field_name} 必须是时间文字。")
    try:
        return time.fromisoformat(value)
    except ValueError as exc:
        raise ConfigError(f"配置项 {field_name} 的时间格式不正确。") from exc


def _config_to_dict(config: MonitorConfig) -> dict[str, Any]:
    """转换为可以安全写入 JSON 的结构；此结构不包含密码。"""

    return {
        "schema_version": SCHEMA_VERSION,
        "monitor": {
            "base_url": config.base_url,
            "postcode": config.postcode,
            "state": config.state,
            "location_id": config.location_id,
            "location_name": config.location_name,
            "assessment_codes": list(config.assessment_codes),
            "timezone_name": config.timezone_name,
            "start_time": config.start_time.isoformat(),
            "end_time": config.end_time.isoformat(),
            "poll_interval_seconds": config.poll_interval_seconds,
            "alert_repeat_seconds": config.alert_repeat_seconds,
            "error_alert_threshold": config.error_alert_threshold,
            "error_alert_cooldown_seconds": config.error_alert_cooldown_seconds,
            "page_timeout_seconds": config.page_timeout_seconds,
            "headless": config.headless,
            "autostart_enabled": config.autostart_enabled,
            "start_minimized": config.start_minimized,
        },
        "smtp": {
            "host": config.smtp.host,
            "port": config.smtp.port,
            "security": config.smtp.security.value,
            "username": config.smtp.username,
            "sender": config.smtp.sender,
            "recipients": list(config.smtp.recipients),
        },
    }


def _config_from_dict(payload: Mapping[str, Any]) -> MonitorConfig:
    """从 JSON 对象构建配置，并为缺失字段应用当前默认值。"""

    schema_version = _read_int(payload.get("schema_version", SCHEMA_VERSION), "schema_version")
    if schema_version != SCHEMA_VERSION:
        raise ConfigError(
            f"不支持配置版本 {schema_version}；当前程序只支持版本 {SCHEMA_VERSION}。"
        )

    defaults = MonitorConfig()
    monitor = _require_mapping(payload.get("monitor", {}), "monitor")
    smtp_data = _require_mapping(payload.get("smtp", {}), "smtp")

    try:
        smtp_security = SmtpSecurity(
            str(smtp_data.get("security", defaults.smtp.security.value)).lower()
        )
    except ValueError as exc:
        raise ConfigError("SMTP 安全模式必须是 starttls、ssl 或 none。") from exc

    smtp = SmtpConfig(
        host=str(smtp_data.get("host", defaults.smtp.host)).strip(),
        port=_read_int(smtp_data.get("port", defaults.smtp.port), "smtp.port"),
        security=smtp_security,
        username=str(smtp_data.get("username", defaults.smtp.username)).strip(),
        sender=str(smtp_data.get("sender", defaults.smtp.sender)).strip(),
        recipients=_read_string_tuple(
            smtp_data.get("recipients", list(defaults.smtp.recipients)),
            "smtp.recipients",
        ),
    )

    config = MonitorConfig(
        base_url=str(monitor.get("base_url", defaults.base_url)).strip(),
        postcode=str(monitor.get("postcode", defaults.postcode)).strip(),
        state=str(monitor.get("state", defaults.state)).strip().upper(),
        location_id=str(monitor.get("location_id", defaults.location_id)).strip(),
        location_name=str(monitor.get("location_name", defaults.location_name)).strip(),
        assessment_codes=_read_string_tuple(
            monitor.get("assessment_codes", list(defaults.assessment_codes)),
            "monitor.assessment_codes",
        ),
        timezone_name=str(
            monitor.get("timezone_name", defaults.timezone_name)
        ).strip(),
        start_time=_read_time(
            monitor.get("start_time", defaults.start_time.isoformat()),
            "monitor.start_time",
        ),
        end_time=_read_time(
            monitor.get("end_time", defaults.end_time.isoformat()),
            "monitor.end_time",
        ),
        poll_interval_seconds=_read_int(
            monitor.get("poll_interval_seconds", defaults.poll_interval_seconds),
            "monitor.poll_interval_seconds",
        ),
        alert_repeat_seconds=_read_int(
            monitor.get("alert_repeat_seconds", defaults.alert_repeat_seconds),
            "monitor.alert_repeat_seconds",
        ),
        error_alert_threshold=_read_int(
            monitor.get("error_alert_threshold", defaults.error_alert_threshold),
            "monitor.error_alert_threshold",
        ),
        error_alert_cooldown_seconds=_read_int(
            monitor.get(
                "error_alert_cooldown_seconds",
                defaults.error_alert_cooldown_seconds,
            ),
            "monitor.error_alert_cooldown_seconds",
        ),
        page_timeout_seconds=_read_int(
            monitor.get("page_timeout_seconds", defaults.page_timeout_seconds),
            "monitor.page_timeout_seconds",
        ),
        headless=_read_bool(
            monitor.get("headless", defaults.headless),
            "monitor.headless",
        ),
        autostart_enabled=_read_bool(
            monitor.get("autostart_enabled", defaults.autostart_enabled),
            "monitor.autostart_enabled",
        ),
        start_minimized=_read_bool(
            monitor.get("start_minimized", defaults.start_minimized),
            "monitor.start_minimized",
        ),
        smtp=smtp,
    )
    ensure_valid_config(config)
    return config


def load_config(path: Path | None = None) -> MonitorConfig:
    """读取配置文件；文件尚不存在时返回默认配置且不创建文件。"""

    target = path or get_config_path()
    if not target.exists():
        return MonitorConfig()

    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"配置文件 JSON 格式错误（第 {exc.lineno} 行，第 {exc.colno} 列）。"
        ) from exc
    except OSError as exc:
        raise ConfigError(f"无法读取配置文件：{exc}") from exc

    if not isinstance(payload, Mapping):
        raise ConfigError("配置文件最外层必须是 JSON 对象。")
    return _config_from_dict(payload)


def save_config(config: MonitorConfig, path: Path | None = None) -> Path:
    """校验并原子写入配置，返回实际保存路径。

    数据先写入同目录临时文件，完整写入后再替换正式文件。这样即使电脑在
    保存期间断电，原配置也不会只剩下一段不完整的 JSON。
    """

    ensure_valid_config(config)
    target = path or get_config_path()
    temporary = target.with_suffix(f"{target.suffix}.tmp")
    serialized = json.dumps(_config_to_dict(config), ensure_ascii=False, indent=2) + "\n"

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(serialized, encoding="utf-8")
        os.replace(temporary, target)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise ConfigError(f"无法保存配置文件：{exc}") from exc

    return target


__all__ = [
    "CONFIG_FILENAME",
    "SCHEMA_VERSION",
    "ConfigError",
    "ensure_valid_config",
    "get_app_data_dir",
    "get_config_path",
    "load_config",
    "save_config",
    "validate_config",
]

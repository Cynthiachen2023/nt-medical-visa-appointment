"""桌面程序和维护命令的统一入口。"""

from __future__ import annotations

import argparse
import json
import logging
import platform
import sys
from dataclasses import replace
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import APP_DISPLAY_NAME, __version__
from .config import (
    ConfigError,
    get_app_data_dir,
    get_config_path,
    load_config,
    validate_config,
)
from .gui import AppointmentMonitorApp
from .models import AvailabilityStatus
from .monitor import AppointmentMonitor, find_bundled_browser


LOGGER = logging.getLogger(__name__)
LOG_FILENAME = "app.log"


def configure_logging(*, verbose: bool = False) -> Path | None:
    """配置控制台和轮转日志；目录不可写时安全回退到控制台。"""

    level = logging.DEBUG if verbose else logging.INFO
    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    handlers: list[logging.Handler] = [console]
    log_path = get_app_data_dir() / "logs" / LOG_FILENAME

    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_path,
            maxBytes=2 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        handlers.append(file_handler)
    except OSError as exc:
        console.handle(
            logging.LogRecord(
                name=__name__,
                level=logging.WARNING,
                pathname=__file__,
                lineno=0,
                msg=f"无法创建日志文件，将只使用控制台日志：{exc}",
                args=(),
                exc_info=None,
            )
        )
        log_path = None

    logging.basicConfig(level=level, handlers=handlers, force=True)
    return log_path


def _build_parser() -> argparse.ArgumentParser:
    """创建维护命令参数解析器。"""

    parser = argparse.ArgumentParser(
        prog="bupa-appointment-monitor",
        description="Bupa Darwin 预约监控桌面程序",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="执行一次检查后退出")
    mode.add_argument("--diagnostics", action="store_true", help="输出环境诊断后退出")
    parser.add_argument(
        "--headed",
        action="store_true",
        help="配合 --once 显示浏览器窗口",
    )
    parser.add_argument("--verbose", action="store_true", help="启用详细日志")
    parser.add_argument("--version", action="version", version=__version__)
    return parser


def _run_diagnostics(log_path: Path | None) -> int:
    """输出不包含密码和邮箱地址的运行环境摘要。"""

    bundled = find_bundled_browser()
    config_path = get_config_path()
    print(f"应用版本：{__version__}")
    print(f"Python：{platform.python_version()}")
    print(f"系统：{platform.platform()}")
    print(f"配置文件：{config_path}")
    print(f"配置文件存在：{'是' if config_path.exists() else '否'}")
    print(f"日志文件：{log_path or '不可用，仅输出到控制台'}")
    if bundled:
        print(f"内置浏览器：{bundled[0]}")
        print(f"内置驱动：{bundled[1]}")
    else:
        print("内置浏览器：尚未安装（开发环境将使用 Selenium Manager）")

    try:
        config = load_config()
        errors = validate_config(config, require_email=True)
    except ConfigError as exc:
        print(f"配置状态：无法读取（{exc}）")
    else:
        if errors:
            print("配置状态：尚未完成")
            for error in errors:
                print(f"  - {error}")
        else:
            print("配置状态：普通配置有效（未检查或显示密码）")
    return 0


def _run_once(*, headed: bool) -> int:
    """执行一次检测，以 JSON 输出结果但不发送邮件。"""

    try:
        config = load_config()
        errors = validate_config(config)
        if errors:
            raise ConfigError("配置无效：\n" + "\n".join(f"- {item}" for item in errors))
    except ConfigError as exc:
        LOGGER.error("无法执行单次检查：%s", exc)
        print(str(exc), file=sys.stderr)
        return 2

    if headed:
        config = replace(config, headless=False)
    result = AppointmentMonitor(config).check_once()
    output = {
        "status": result.status.value,
        "checked_at": result.checked_at.isoformat(),
        "location": result.location_name,
        "dates": [
            {
                "value": date.value,
                "label": date.label,
                "active": date.active,
            }
            for date in result.available_dates
        ],
        "details": result.details,
        "screenshot": str(result.screenshot_path) if result.screenshot_path else None,
        "error_type": result.error_type,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if result.status in {
        AvailabilityStatus.AVAILABLE,
        AvailabilityStatus.UNAVAILABLE,
    } else 2


def _run_gui() -> int:
    """启动桌面界面，并把未处理异常记录后显示给用户。"""

    try:
        app = AppointmentMonitorApp()
        app.mainloop()
    except Exception as exc:  # GUI 顶层必须阻止程序直接闪退。
        LOGGER.exception("桌面程序发生未处理异常")
        try:
            from tkinter import messagebox

            messagebox.showerror(
                f"{APP_DISPLAY_NAME} 发生错误",
                f"程序无法继续运行：\n{exc}\n\n请查看日志获取详细信息。",
            )
        except Exception:
            pass
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    """解析命令参数并运行指定模式。"""

    parser = _build_parser()
    arguments = parser.parse_args(argv)
    if arguments.headed and not arguments.once:
        parser.error("--headed 必须与 --once 一起使用")

    log_path = configure_logging(verbose=arguments.verbose)
    if arguments.diagnostics:
        return _run_diagnostics(log_path)
    if arguments.once:
        return _run_once(headed=arguments.headed)
    return _run_gui()


if __name__ == "__main__":
    raise SystemExit(main())

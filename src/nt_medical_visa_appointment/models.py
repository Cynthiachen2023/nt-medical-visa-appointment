"""配置、监控结果和提醒状态所共用的数据模型。

本模块只定义数据，不包含浏览器、文件存储或邮件发送逻辑。这样可以让
配置更容易校验、序列化、测试，并能安全地展示在桌面界面中。
"""

from dataclasses import dataclass, field
from datetime import datetime, time
from enum import StrEnum
from pathlib import Path


class AvailabilityStatus(StrEnum):
    """一次完整预约检查可能产生的结果。"""

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    ERROR = "error"
    CAPTCHA = "captcha"


class SmtpSecurity(StrEnum):
    """邮件服务器支持的连接加密方式。"""

    STARTTLS = "starttls"
    SSL = "ssl"
    NONE = "none"


@dataclass(slots=True, frozen=True)
class AvailableDate:
    """在 ``divPaginationNavigation`` 中发现的一个日期按钮。

    ``value`` 保存网站 ``data-value`` 属性中的机器可读日期，``label``
    保存用户在页面上看到的文字。``active`` 只表示网站默认选中了该日期，
    并不表示已经完成预约。
    """

    value: str
    label: str
    active: bool = False


@dataclass(slots=True)
class SmtpConfig:
    """连接 SMTP 邮件服务器所需的非敏感配置。

    这里故意不包含 SMTP 密码。密码由 Windows Credential Manager 单独
    保存和读取，确保普通配置文件中不会出现明文密码。
    """

    host: str = ""
    port: int = 587
    security: SmtpSecurity = SmtpSecurity.STARTTLS
    username: str = ""
    sender: str = ""
    recipients: tuple[str, ...] = ()


@dataclass(slots=True)
class MonitorConfig:
    """调度器和 Selenium 流程使用的用户配置。

    默认值对应 Jobfit Darwin 和指定的三个体检项目。监控时间使用
    ``timezone_name`` 指定的时区，而不是运行电脑的本地时区，因此程序在
    澳大利亚其他地区的电脑上运行时仍会按 Darwin 时间工作。地点和项目
    ID 使用字符串保存，以便直接与网页 HTML 属性进行比较。
    """

    base_url: str = (
        "https://bmvs.onlineappointmentscheduling.net.au/oasis/Default.aspx"
    )
    postcode: str = "0810"
    state: str = "NT"
    location_id: str = "137"
    location_name: str = "Jobfit Darwin"
    assessment_codes: tuple[str, ...] = ("501", "502", "705")

    timezone_name: str = "Australia/Darwin"
    start_time: time = time(hour=8)
    end_time: time = time(hour=10)
    poll_interval_seconds: int = 120
    alert_repeat_seconds: int = 600
    error_alert_threshold: int = 3
    error_alert_cooldown_seconds: int = 1_800

    page_timeout_seconds: int = 30
    headless: bool = True
    autostart_enabled: bool = False
    start_minimized: bool = True
    smtp: SmtpConfig = field(default_factory=SmtpConfig)


@dataclass(slots=True, frozen=True)
class AvailabilityResult:
    """一次完整网站检查返回的结构化结果。

    没有预约属于正常结果，并不等同于程序错误。``screenshot_path`` 通常只在
    有号、出现验证码或需要诊断错误时使用，避免日常检查积累无用截图。
    """

    status: AvailabilityStatus
    checked_at: datetime
    location_name: str
    available_dates: tuple[AvailableDate, ...] = ()
    details: str = ""
    screenshot_path: Path | None = None
    error_type: str | None = None

    @property
    def date_signature(self) -> tuple[str, ...]:
        """返回稳定的日期签名，用于判断可预约日期是否发生变化。"""

        return tuple(sorted(date.value for date in self.available_dates))


@dataclass(slots=True)
class AlertState:
    """用于跨重启抑制重复提醒的持久化状态。

    成功检查会更新 ``last_status`` 和 ``date_signature``。失败检查会递增
    ``consecutive_failures``，直到达到配置的提醒阈值。错误签名和时间用于
    限制重复故障邮件，``recovery_pending`` 则保证恢复正常后只发送一次
    恢复通知。
    """

    last_status: AvailabilityStatus | None = None
    date_signature: tuple[str, ...] = ()
    last_alert_at: datetime | None = None
    consecutive_failures: int = 0
    last_error_signature: str = ""
    last_error_alert_at: datetime | None = None
    recovery_pending: bool = False


__all__ = [
    "AlertState",
    "AvailabilityResult",
    "AvailabilityStatus",
    "AvailableDate",
    "MonitorConfig",
    "SmtpConfig",
    "SmtpSecurity",
]

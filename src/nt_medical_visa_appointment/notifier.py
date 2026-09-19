"""构建并发送预约、故障和恢复邮件。"""

from __future__ import annotations

import logging
import mimetypes
import smtplib
import ssl
from collections.abc import Callable
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

from .config import ConfigError, ensure_valid_config
from .credentials import CredentialError, get_smtp_password
from .models import AvailabilityResult, MonitorConfig, SmtpSecurity


LOGGER = logging.getLogger(__name__)
SMTP_TIMEOUT_SECONDS = 30
PasswordProvider = Callable[[], str | None]


class EmailNotificationError(RuntimeError):
    """邮件配置、连接或发送失败时抛出的中文友好异常。"""


class EmailNotifier:
    """根据监控配置发送中文邮件，不保存或记录 SMTP 密码。"""

    def __init__(
        self,
        config: MonitorConfig,
        password_provider: PasswordProvider | None = None,
    ) -> None:
        self.config = config
        self._password_provider = password_provider or get_smtp_password

    def send_test(self) -> None:
        """发送一封不包含预约结果的配置测试邮件。"""

        self._send(
            subject="[Bupa 预约监控] 测试邮件",
            body=(
                "邮件配置测试成功。\n\n"
                f"监控地点：{self.config.location_name}\n"
                f"监控项目：{', '.join(self.config.assessment_codes)}\n"
                f"监控时区：{self.config.timezone_name}\n"
            ),
        )

    def send_availability(self, result: AvailabilityResult) -> None:
        """发送有号提醒，并在存在截图时附加截图。"""

        date_lines = "\n".join(
            f"- {date.label or date.value}（{date.value}）"
            for date in result.available_dates
        )
        body = (
            "Bupa Darwin 页面发现可预约日期。\n\n"
            f"检查时间：{self._format_time(result.checked_at)}\n"
            f"地点：{result.location_name}\n"
            f"项目：{', '.join(self.config.assessment_codes)}\n"
            f"可预约日期：\n{date_lines}\n\n"
            f"预约页面：{self.config.base_url}\n\n"
            "本程序只负责提醒，不会自动完成预约。"
        )
        self._send(
            subject="[Bupa 预约监控] Darwin 发现可预约日期",
            body=body,
            attachment=result.screenshot_path,
        )

    def send_error(self, result: AvailabilityResult) -> None:
        """发送监控故障或访问限制提醒。"""

        body = (
            "Bupa 预约监控连续检查失败，需要人工查看。\n\n"
            f"检查时间：{self._format_time(result.checked_at)}\n"
            f"地点：{result.location_name}\n"
            f"错误类型：{result.error_type or '未知'}\n"
            f"说明：{result.details or '没有额外说明'}\n"
        )
        self._send(
            subject="[Bupa 预约监控] 监控故障",
            body=body,
            attachment=result.screenshot_path,
        )

    def send_recovery(self, checked_at: datetime) -> None:
        """发送一次恢复通知，说明网页检查已经重新正常工作。"""

        self._send(
            subject="[Bupa 预约监控] 监控已恢复",
            body=(
                "Bupa 预约监控已经恢复正常。\n\n"
                f"恢复时间：{self._format_time(checked_at)}\n"
                f"地点：{self.config.location_name}\n"
            ),
        )

    def _send(
        self,
        *,
        subject: str,
        body: str,
        attachment: Path | None = None,
    ) -> None:
        """校验配置、构建邮件并通过配置的 SMTP 模式发送。"""

        try:
            ensure_valid_config(self.config, require_email=True)
        except ConfigError as exc:
            raise EmailNotificationError(str(exc)) from exc

        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = self.config.smtp.sender
        message["To"] = ", ".join(self.config.smtp.recipients)
        message.set_content(body)
        if attachment is not None:
            self._add_attachment(message, attachment)

        try:
            self._send_message(message)
        except EmailNotificationError:
            raise
        except (CredentialError, OSError, smtplib.SMTPException) as exc:
            raise EmailNotificationError(f"邮件发送失败：{exc}") from exc

    def _send_message(self, message: EmailMessage) -> None:
        """打开 SMTP 连接，按配置升级加密并发送邮件。"""

        smtp = self.config.smtp
        context = ssl.create_default_context()
        if smtp.security is SmtpSecurity.SSL:
            connection: smtplib.SMTP = smtplib.SMTP_SSL(
                smtp.host,
                smtp.port,
                timeout=SMTP_TIMEOUT_SECONDS,
                context=context,
            )
        else:
            connection = smtplib.SMTP(
                smtp.host,
                smtp.port,
                timeout=SMTP_TIMEOUT_SECONDS,
            )

        with connection as server:
            server.ehlo()
            if smtp.security is SmtpSecurity.STARTTLS:
                server.starttls(context=context)
                server.ehlo()

            if smtp.username:
                password = self._password_provider()
                if not password:
                    raise EmailNotificationError(
                        "尚未在 Windows 凭据管理器中保存 SMTP 密码。"
                    )
                server.login(smtp.username, password)
            server.send_message(message)

    @staticmethod
    def _add_attachment(message: EmailMessage, path: Path) -> None:
        """尽力添加截图；附件读取失败不应阻止关键提醒邮件。"""

        try:
            content = path.read_bytes()
        except OSError as exc:
            LOGGER.warning("无法读取邮件附件 %s：%s", path, exc)
            return

        content_type, _ = mimetypes.guess_type(path.name)
        maintype, subtype = (
            content_type.split("/", maxsplit=1)
            if content_type and "/" in content_type
            else ("application", "octet-stream")
        )
        message.add_attachment(
            content,
            maintype=maintype,
            subtype=subtype,
            filename=path.name,
        )

    @staticmethod
    def _format_time(value: datetime) -> str:
        """以包含时区缩写的易读格式显示检查时间。"""

        return value.strftime("%Y-%m-%d %H:%M:%S %Z")


__all__ = ["EmailNotificationError", "EmailNotifier"]

"""提醒判断和 SMTP 邮件发送的无网络测试。"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from nt_medical_visa_appointment.gui import should_send_availability
from nt_medical_visa_appointment.models import (
    AvailabilityResult,
    AvailabilityStatus,
    AvailableDate,
    MonitorConfig,
    SmtpConfig,
    SmtpSecurity,
)
from nt_medical_visa_appointment.notifier import EmailNotificationError, EmailNotifier


DARWIN = ZoneInfo("Australia/Darwin")
CHECKED_AT = datetime(2026, 9, 19, 8, 10, tzinfo=DARWIN)


def make_config(*, security: SmtpSecurity = SmtpSecurity.STARTTLS) -> MonitorConfig:
    """创建可以发送模拟邮件的完整配置。"""

    port = 465 if security is SmtpSecurity.SSL else 587
    return MonitorConfig(
        smtp=SmtpConfig(
            host="smtp.example.com",
            port=port,
            security=security,
            username="sender@example.com",
            sender="sender@example.com",
            recipients=("receiver@example.com",),
        )
    )


def make_available_result(*, value: str = "2/10/2026") -> AvailabilityResult:
    """创建一个带单个日期的有号结果。"""

    return AvailabilityResult(
        status=AvailabilityStatus.AVAILABLE,
        checked_at=CHECKED_AT,
        location_name="Jobfit Darwin",
        available_dates=(AvailableDate(value, "Fri 2 Oct", active=True),),
    )


def test_availability_alert_rules_cover_first_change_and_repeat() -> None:
    """首次、日期变化和达到重复间隔都应提醒。"""

    result = make_available_result()
    assert should_send_availability(result, None, (), None, 600)
    assert should_send_availability(
        result,
        AvailabilityStatus.UNAVAILABLE,
        (),
        CHECKED_AT,
        600,
    )
    assert should_send_availability(
        result,
        AvailabilityStatus.AVAILABLE,
        ("old-date",),
        CHECKED_AT,
        600,
    )
    assert not should_send_availability(
        result,
        AvailabilityStatus.AVAILABLE,
        result.date_signature,
        CHECKED_AT - timedelta(seconds=599),
        600,
    )
    assert should_send_availability(
        result,
        AvailabilityStatus.AVAILABLE,
        result.date_signature,
        CHECKED_AT - timedelta(seconds=600),
        600,
    )


@pytest.mark.parametrize(
    "status",
    [AvailabilityStatus.UNAVAILABLE, AvailabilityStatus.ERROR, AvailabilityStatus.CAPTCHA],
)
def test_non_available_results_never_send_availability_alert(status) -> None:
    """无号和故障状态不能误触发预约提醒。"""

    result = AvailabilityResult(status, CHECKED_AT, "Jobfit Darwin")
    assert not should_send_availability(result, None, (), None, 600)


def test_starttls_email_contains_details_attachment_and_no_password(tmp_path) -> None:
    """STARTTLS 邮件应包含预约信息和附件，但不能包含密码。"""

    screenshot = tmp_path / "available.png"
    screenshot.write_bytes(b"fake-image")
    result = make_available_result()
    result = AvailabilityResult(
        result.status,
        result.checked_at,
        result.location_name,
        result.available_dates,
        screenshot_path=screenshot,
    )
    context_manager = MagicMock()
    server = MagicMock()
    context_manager.__enter__.return_value = server

    with patch(
        "nt_medical_visa_appointment.notifier.smtplib.SMTP",
        return_value=context_manager,
    ) as smtp_class:
        EmailNotifier(make_config(), lambda: "smtp-secret").send_availability(result)

    smtp_class.assert_called_once()
    server.starttls.assert_called_once()
    server.login.assert_called_once_with("sender@example.com", "smtp-secret")
    message = server.send_message.call_args.args[0]
    body = message.get_body().get_content()
    assert "Jobfit Darwin" in body
    assert "501, 502, 705" in body
    assert "2/10/2026" in body
    assert "https://" in body
    assert "smtp-secret" not in message.as_string()
    assert len(list(message.iter_attachments())) == 1


def test_ssl_uses_direct_encrypted_connection_without_starttls() -> None:
    """SSL 模式不能再次调用 STARTTLS。"""

    context_manager = MagicMock()
    server = MagicMock()
    context_manager.__enter__.return_value = server

    with patch(
        "nt_medical_visa_appointment.notifier.smtplib.SMTP_SSL",
        return_value=context_manager,
    ) as ssl_class:
        EmailNotifier(
            make_config(security=SmtpSecurity.SSL),
            lambda: "smtp-secret",
        ).send_test()

    ssl_class.assert_called_once()
    server.starttls.assert_not_called()
    server.send_message.assert_called_once()


def test_missing_password_and_incomplete_email_config_are_rejected() -> None:
    """登录所需密码或基础邮件配置缺失时必须明确失败。"""

    context_manager = MagicMock()
    context_manager.__enter__.return_value = MagicMock()
    with patch(
        "nt_medical_visa_appointment.notifier.smtplib.SMTP",
        return_value=context_manager,
    ):
        with pytest.raises(EmailNotificationError, match="尚未.*密码"):
            EmailNotifier(make_config(), lambda: None).send_test()

    with pytest.raises(EmailNotificationError, match="配置无效"):
        EmailNotifier(MonitorConfig(), lambda: "unused").send_test()


def test_error_and_recovery_use_their_own_templates(monkeypatch) -> None:
    """故障与恢复通知应使用不同主题和正确时间。"""

    notifier = EmailNotifier(make_config(), lambda: "smtp-secret")
    sent: list[dict[str, object]] = []
    monkeypatch.setattr(notifier, "_send", lambda **kwargs: sent.append(kwargs))
    error = AvailabilityResult(
        AvailabilityStatus.ERROR,
        CHECKED_AT,
        "Jobfit Darwin",
        details="页面超时",
        error_type="TimeoutException",
    )

    notifier.send_error(error)
    notifier.send_recovery(CHECKED_AT + timedelta(minutes=5))

    assert len(sent) == 2
    assert "监控故障" in str(sent[0]["subject"])
    assert "TimeoutException" in str(sent[0]["body"])
    assert "监控已恢复" in str(sent[1]["subject"])
    assert "08:15:00" in str(sent[1]["body"])

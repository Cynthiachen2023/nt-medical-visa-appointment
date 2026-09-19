"""Selenium 检测逻辑的无网络单元测试。"""

from __future__ import annotations

from pathlib import Path

import nt_medical_visa_appointment.monitor as monitor_module
from nt_medical_visa_appointment.models import AvailabilityStatus, MonitorConfig
from nt_medical_visa_appointment.monitor import (
    AVAILABILITY_BUTTON_SELECTOR,
    AppointmentMonitor,
    MonitorError,
    extract_available_dates,
    find_bundled_browser,
)


class FakeButton:
    """只实现日期提取函数实际使用的 WebElement 接口。"""

    def __init__(
        self,
        value: str | None,
        label: str,
        *,
        classes: str = "pagination-navigation-btn",
        visible: bool = True,
        enabled: bool = True,
    ) -> None:
        self.value = value
        self.text = label
        self.classes = classes
        self.visible = visible
        self.enabled = enabled

    def is_displayed(self) -> bool:
        return self.visible

    def is_enabled(self) -> bool:
        return self.enabled

    def get_attribute(self, name: str) -> str | None:
        if name == "data-value":
            return self.value
        if name == "class":
            return self.classes
        return None


class FakeDriver:
    """记录 ``quit`` 是否执行，不启动任何浏览器进程。"""

    def __init__(self) -> None:
        self.closed = False

    def quit(self) -> None:
        self.closed = True


class NoScreenshotMonitor(AppointmentMonitor):
    """测试中禁用截图，避免写入真实 Local AppData。"""

    def _capture_screenshot(self, driver, category):
        return None


def test_extract_available_dates_filters_and_normalizes_buttons() -> None:
    """只保留可见、启用且不重复的日期按钮。"""

    buttons = [
        FakeButton(
            "2/10/2026",
            "Fri  \n 2 Oct",
            classes="pagination-navigation-btn active",
        ),
        FakeButton("5/10/2026", "Mon 5 Oct"),
        FakeButton("hidden", "Hidden", visible=False),
        FakeButton("disabled", "Disabled", enabled=False),
        FakeButton("2/10/2026", "Fri 2 Oct", classes="active"),
    ]

    dates = extract_available_dates(buttons)

    assert [(item.value, item.label, item.active) for item in dates] == [
        ("2/10/2026", "Fri 2 Oct", True),
        ("5/10/2026", "Mon 5 Oct", False),
    ]


def test_button_text_is_used_when_data_value_is_missing() -> None:
    """网站缺少 ``data-value`` 时仍可使用清理后的按钮文字。"""

    dates = extract_available_dates([FakeButton(None, "Tue  6 Oct")])

    assert len(dates) == 1
    assert dates[0].value == "Tue 6 Oct"
    assert dates[0].label == "Tue 6 Oct"


def test_availability_selector_is_exact() -> None:
    """防止后续重构把业务判据改成只检查空容器。"""

    assert AVAILABILITY_BUTTON_SELECTOR == "#divPaginationNavigation button"


def test_monitor_retries_once_and_closes_each_driver() -> None:
    """普通页面错误应重试一次，并关闭两个浏览器实例。"""

    drivers: list[FakeDriver] = []

    def factory(_config: MonitorConfig) -> FakeDriver:
        driver = FakeDriver()
        drivers.append(driver)
        return driver

    class FailingMonitor(NoScreenshotMonitor):
        def _run_check(self, driver):
            raise MonitorError("模拟页面结构错误")

    result = FailingMonitor(MonitorConfig(), factory).check_once()

    assert result.status is AvailabilityStatus.ERROR
    assert result.error_type == "MonitorError"
    assert len(drivers) == 2
    assert all(driver.closed for driver in drivers)


def test_blocked_page_is_not_retried_or_bypassed() -> None:
    """访问验证应立即返回 CAPTCHA，不继续制造请求。"""

    drivers: list[FakeDriver] = []

    def factory(_config: MonitorConfig) -> FakeDriver:
        driver = FakeDriver()
        drivers.append(driver)
        return driver

    class BlockedMonitor(NoScreenshotMonitor):
        def _run_check(self, driver):
            raise monitor_module._BlockedPageError("verify you are human")

    result = BlockedMonitor(MonitorConfig(), factory).check_once()

    assert result.status is AvailabilityStatus.CAPTCHA
    assert len(drivers) == 1
    assert drivers[0].closed is True


def test_chrome_options_follow_headless_setting() -> None:
    """普通运行隐藏浏览器，诊断运行可以显示浏览器。"""

    headless = monitor_module._build_chrome_options(MonitorConfig(), None)
    visible = monitor_module._build_chrome_options(
        MonitorConfig(headless=False),
        None,
    )

    assert "--headless=new" in headless.arguments
    assert "--headless=new" not in visible.arguments
    assert "--window-size=1440,1000" in headless.arguments


def test_bundled_browser_requires_matching_driver(tmp_path, monkeypatch) -> None:
    """浏览器和驱动必须成对存在，避免启动不匹配版本。"""

    monkeypatch.setattr(monitor_module, "_program_root", lambda: Path(tmp_path))
    browser = tmp_path / "browser" / "chrome.exe"
    driver = tmp_path / "browser" / "chromedriver.exe"
    browser.parent.mkdir(parents=True)
    browser.write_bytes(b"browser")

    assert find_bundled_browser() is None

    driver.write_bytes(b"driver")
    assert find_bundled_browser() == (browser, driver)

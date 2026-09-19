"""使用 Selenium 检查 Bupa Darwin 是否出现可预约日期。

本模块只负责完成一次独立检查。定时循环、邮件提醒和界面状态由 GUI 层
处理，避免核心网页流程与桌面界面互相耦合。
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable, Iterable
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait

from .config import get_app_data_dir
from .models import (
    AvailabilityResult,
    AvailabilityStatus,
    AvailableDate,
    MonitorConfig,
)


LOGGER = logging.getLogger(__name__)

INDIVIDUAL_BUTTON_ID = "ContentPlaceHolder1_btnInd"
SUBURB_INPUT_ID = "ContentPlaceHolder1_SelectLocation1_txtSuburb"
STATE_SELECT_ID = "ContentPlaceHolder1_SelectLocation1_ddlState"
SEARCH_BUTTON_SELECTOR = "input.blue-button[value='Search']"
CONTINUE_BUTTON_ID = "ContentPlaceHolder1_btnCont"
AVAILABILITY_BUTTON_SELECTOR = "#divPaginationNavigation button"

BLOCKED_PAGE_MARKERS = (
    "captcha",
    "verify you are human",
    "access denied",
    "too many requests",
    "unusual traffic",
)


class MonitorError(RuntimeError):
    """页面结构或内容不符合预期时抛出的监控异常。"""


class _BlockedPageError(MonitorError):
    """网站显示验证码或访问限制时使用的内部异常。"""


DriverFactory = Callable[[MonitorConfig], WebDriver]


def _program_root() -> Path:
    """返回开发仓库或 PyInstaller 应用所在目录。"""

    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def find_bundled_browser() -> tuple[Path, Path] | None:
    """查找打包或开发目录中成对出现的浏览器与驱动。

    Chrome 和 ChromeDriver 必须同时存在才会使用。只发现其中一个时继续
    使用开发环境的 Selenium Manager，避免混用不匹配的版本。
    """

    root = _program_root()
    candidates = (
        (
            root / "browser" / "chrome.exe",
            root / "browser" / "chromedriver.exe",
        ),
        (
            root / "browser" / "chrome-win64" / "chrome.exe",
            root / "browser" / "chromedriver-win64" / "chromedriver.exe",
        ),
        (
            root / ".build" / "browser" / "chrome-win64" / "chrome.exe",
            root
            / ".build"
            / "browser"
            / "chromedriver-win64"
            / "chromedriver.exe",
        ),
    )
    return next(
        ((browser, driver) for browser, driver in candidates if browser.is_file() and driver.is_file()),
        None,
    )


def _build_chrome_options(config: MonitorConfig, browser_path: Path | None) -> Options:
    """创建一致、安静且适合后台监控的 Chrome 参数。"""

    options = Options()
    if browser_path is not None:
        options.binary_location = str(browser_path)
    if config.headless:
        options.add_argument("--headless=new")
    options.add_argument("--window-size=1440,1000")
    options.add_argument("--lang=en-AU")
    options.add_argument("--disable-notifications")
    options.add_argument("--disable-popup-blocking")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    options.add_experimental_option(
        "prefs",
        {
            "credentials_enable_service": False,
            "profile.password_manager_enabled": False,
        },
    )
    return options


def create_chrome_driver(config: MonitorConfig) -> WebDriver:
    """优先启动内置浏览器，开发环境则使用 Selenium Manager。

    正式安装包必须包含固定版本浏览器和驱动；Selenium Manager 仅作为源码
    开发时的便利后备方案，不会成为最终用户的运行依赖。
    """

    bundled = find_bundled_browser()
    browser_path = bundled[0] if bundled else None
    options = _build_chrome_options(config, browser_path)

    if bundled:
        driver = webdriver.Chrome(
            service=Service(executable_path=str(bundled[1])),
            options=options,
        )
    else:
        driver = webdriver.Chrome(options=options)

    driver.set_page_load_timeout(config.page_timeout_seconds)
    driver.set_script_timeout(config.page_timeout_seconds)
    return driver


def extract_available_dates(buttons: Iterable[WebElement]) -> tuple[AvailableDate, ...]:
    """从可见且可点击的日期按钮中提取稳定、去重的日期信息。"""

    dates: list[AvailableDate] = []
    seen: set[tuple[str, str]] = set()
    for button in buttons:
        if not button.is_displayed() or not button.is_enabled():
            continue

        label = " ".join(button.text.split())
        value = (button.get_attribute("data-value") or label).strip()
        if not value and not label:
            continue

        identity = (value, label)
        if identity in seen:
            continue
        seen.add(identity)
        classes = (button.get_attribute("class") or "").split()
        dates.append(AvailableDate(value=value, label=label, active="active" in classes))
    return tuple(dates)


class AppointmentMonitor:
    """用全新浏览器会话执行一次预约可用性检查。"""

    def __init__(
        self,
        config: MonitorConfig,
        driver_factory: DriverFactory | None = None,
    ) -> None:
        self.config = config
        self._driver_factory = driver_factory or create_chrome_driver

    def check_once(self) -> AvailabilityResult:
        """执行一次检查；普通网页错误会重建浏览器后再尝试一次。"""

        last_error: Exception | None = None
        for attempt in range(2):
            driver: WebDriver | None = None
            try:
                driver = self._driver_factory(self.config)
                return self._run_check(driver)
            except _BlockedPageError as exc:
                LOGGER.warning("网站显示验证码或访问限制：%s", exc)
                return AvailabilityResult(
                    status=AvailabilityStatus.CAPTCHA,
                    checked_at=self._now(),
                    location_name=self.config.location_name,
                    details=str(exc),
                    screenshot_path=self._capture_screenshot(driver, "blocked"),
                    error_type=type(exc).__name__,
                )
            except (MonitorError, TimeoutException, WebDriverException, OSError) as exc:
                last_error = exc
                LOGGER.warning("第 %s 次监控尝试失败：%s", attempt + 1, exc)
                if attempt == 1:
                    return AvailabilityResult(
                        status=AvailabilityStatus.ERROR,
                        checked_at=self._now(),
                        location_name=self.config.location_name,
                        details=str(exc),
                        screenshot_path=self._capture_screenshot(driver, "error"),
                        error_type=type(exc).__name__,
                    )
            finally:
                self._close_driver(driver)

        # 循环固定执行两次，下面只是为了让类型检查器确认函数必有返回值。
        raise AssertionError(f"监控重试流程意外结束：{last_error}")

    def _run_check(self, driver: WebDriver) -> AvailabilityResult:
        """在已经启动的 WebDriver 中执行完整页面流程。"""

        wait = WebDriverWait(
            driver,
            self.config.page_timeout_seconds,
            poll_frequency=0.25,
        )
        driver.get(self.config.base_url)
        self._raise_if_blocked(driver)

        wait.until(EC.element_to_be_clickable((By.ID, INDIVIDUAL_BUTTON_ID))).click()
        suburb_input = wait.until(EC.visibility_of_element_located((By.ID, SUBURB_INPUT_ID)))
        self._raise_if_blocked(driver)

        suburb_input.clear()
        suburb_input.send_keys(self.config.postcode)
        Select(driver.find_element(By.ID, STATE_SELECT_ID)).select_by_value(self.config.state)
        wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, SEARCH_BUTTON_SELECTOR))).click()

        location_selector = f"input.rbLocation[value='{self.config.location_id}']"
        location_radio = wait.until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, location_selector))
        )
        self._raise_if_blocked(driver)
        location_row = location_radio.find_element(By.XPATH, "ancestor::tr[1]")
        if self.config.location_name.casefold() not in location_row.text.casefold():
            raise MonitorError(
                f"地点 ID {self.config.location_id} 的搜索结果不是 "
                f"{self.config.location_name}。"
            )

        if not location_radio.is_selected():
            location_radio.click()
        wait.until(EC.element_to_be_clickable((By.ID, CONTINUE_BUTTON_ID))).click()

        for code in self.config.assessment_codes:
            label = wait.until(
                EC.presence_of_element_located(
                    (By.XPATH, f"//label[contains(normalize-space(.), '({code})')]")
                )
            )
            checkbox_id = label.get_attribute("for")
            if not checkbox_id:
                raise MonitorError(f"项目 ({code}) 的标签没有关联复选框。")
            checkbox = wait.until(EC.element_to_be_clickable((By.ID, checkbox_id)))
            if not checkbox.is_selected():
                checkbox.click()
            if not checkbox.is_selected():
                raise MonitorError(f"无法选择体检项目 ({code})。")

        self._raise_if_blocked(driver)
        products_url = driver.current_url
        wait.until(EC.element_to_be_clickable((By.ID, CONTINUE_BUTTON_ID))).click()
        wait.until(
            lambda current_driver: current_driver.current_url != products_url
            or bool(current_driver.find_elements(By.CSS_SELECTOR, AVAILABILITY_BUTTON_SELECTOR))
        )
        wait.until(
            lambda current_driver: current_driver.execute_script(
                "return document.readyState"
            )
            == "complete"
        )
        self._raise_if_blocked(driver)

        # 无号页面没有日期容器，因此给动态内容一个有限等待时间，超时即为无号。
        availability_wait = min(10, self.config.page_timeout_seconds)
        try:
            WebDriverWait(driver, availability_wait, poll_frequency=0.25).until(
                lambda current_driver: bool(
                    extract_available_dates(
                        current_driver.find_elements(
                            By.CSS_SELECTOR,
                            AVAILABILITY_BUTTON_SELECTOR,
                        )
                    )
                )
            )
        except TimeoutException:
            pass

        dates = extract_available_dates(
            driver.find_elements(By.CSS_SELECTOR, AVAILABILITY_BUTTON_SELECTOR)
        )
        if dates:
            return AvailabilityResult(
                status=AvailabilityStatus.AVAILABLE,
                checked_at=self._now(),
                location_name=self.config.location_name,
                available_dates=dates,
                details=f"发现 {len(dates)} 个可预约日期按钮。",
                screenshot_path=self._capture_screenshot(driver, "available"),
            )

        return AvailabilityResult(
            status=AvailabilityStatus.UNAVAILABLE,
            checked_at=self._now(),
            location_name=self.config.location_name,
            details="页面未出现可预约日期按钮。",
        )

    def _raise_if_blocked(self, driver: WebDriver) -> None:
        """识别常见访问限制；发现后停止流程而不是尝试绕过。"""

        try:
            body_text = driver.find_element(By.TAG_NAME, "body").text.casefold()
        except WebDriverException:
            return
        marker = next((item for item in BLOCKED_PAGE_MARKERS if item in body_text), None)
        if marker:
            raise _BlockedPageError(f"网页出现访问验证或限制提示：{marker}")

    def _capture_screenshot(self, driver: WebDriver | None, category: str) -> Path | None:
        """尽力保存诊断截图；截图失败不应覆盖真正的监控结果。"""

        if driver is None:
            return None
        timestamp = self._now().strftime("%Y%m%d-%H%M%S")
        path = get_app_data_dir() / "screenshots" / f"{category}-{timestamp}.png"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if driver.save_screenshot(str(path)):
                return path
        except (OSError, WebDriverException) as exc:
            LOGGER.warning("无法保存监控截图：%s", exc)
        return None

    def _now(self) -> datetime:
        """返回带 Darwin 时区信息的当前时间。"""

        return datetime.now(ZoneInfo(self.config.timezone_name))

    @staticmethod
    def _close_driver(driver: WebDriver | None) -> None:
        """关闭浏览器；退出错误只记录，不影响已经得到的检查结果。"""

        if driver is None:
            return
        try:
            driver.quit()
        except WebDriverException as exc:
            LOGGER.warning("关闭浏览器时发生错误：%s", exc)


__all__ = [
    "AVAILABILITY_BUTTON_SELECTOR",
    "AppointmentMonitor",
    "MonitorError",
    "create_chrome_driver",
    "extract_available_dates",
    "find_bundled_browser",
]

"""面向普通用户的 Tkinter 桌面界面和内存调度逻辑。"""

from __future__ import annotations

import queue
import re
import threading
import tkinter as tk
from dataclasses import replace
from datetime import datetime, time, timedelta
from tkinter import messagebox, ttk
from zoneinfo import ZoneInfo

from .config import (
    ConfigError,
    get_config_path,
    load_config,
    save_config,
    validate_config,
)
from .credentials import CredentialError, get_smtp_password, save_smtp_password
from .models import (
    AvailabilityResult,
    AvailabilityStatus,
    MonitorConfig,
    SmtpConfig,
    SmtpSecurity,
)
from .monitor import AppointmentMonitor
from .notifier import EmailNotificationError, EmailNotifier


def is_within_window(now: datetime, start: time, end: time) -> bool:
    """判断当前时间是否位于监控窗口，支持跨午夜窗口。"""

    current = now.timetz().replace(tzinfo=None)
    if start < end:
        return start <= current < end
    return current >= start or current < end


def should_send_availability(
    result: AvailabilityResult,
    previous_status: AvailabilityStatus | None,
    previous_signature: tuple[str, ...],
    last_alert_at: datetime | None,
    repeat_seconds: int,
) -> bool:
    """判断本次有号结果是否满足首次、变化或定时重复提醒条件。"""

    if result.status is not AvailabilityStatus.AVAILABLE:
        return False
    if previous_status is not AvailabilityStatus.AVAILABLE:
        return True
    if result.date_signature != previous_signature:
        return True
    if last_alert_at is None:
        return True
    return (result.checked_at - last_alert_at).total_seconds() >= repeat_seconds


class AppointmentMonitorApp(tk.Tk):
    """显示运行状态、编辑配置并在后台执行监控。"""

    def __init__(self) -> None:
        super().__init__()
        self.title("Bupa Darwin 预约监控")
        self.geometry("760x640")
        self.minsize(700, 580)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self._events: queue.Queue[tuple[str, object]] = queue.Queue()
        self._monitoring = False
        self._worker_busy = False
        self._email_busy = False
        self._next_check_at: datetime | None = None

        # 去重状态只保存在内存中；重启程序后重新开始计算。
        self._last_status: AvailabilityStatus | None = None
        self._last_signature: tuple[str, ...] = ()
        self._last_alert_at: datetime | None = None
        self._consecutive_failures = 0
        self._last_error_alert_at: datetime | None = None
        self._recovery_pending = False

        try:
            self.config = load_config()
            load_error = ""
        except ConfigError as exc:
            self.config = MonitorConfig()
            load_error = str(exc)

        self._create_variables()
        self._build_window()
        self._load_fields_from_config()

        if load_error:
            self._set_status("配置文件无法读取，请检查设置。")
            self._append_log(load_error)
            self.notebook.select(self.settings_tab)
        elif self._email_configuration_ready():
            self._monitoring = True
            self._next_check_at = self._now()
            self._set_status("监控已启动，等待首次检查。")
        else:
            self._set_status("请先完成邮件设置。")
            self.notebook.select(self.settings_tab)

        self.after(200, self._poll_events)
        self.after(1000, self._clock_tick)

    def _create_variables(self) -> None:
        """创建所有界面变量，便于读取和刷新控件。"""

        self.clock_var = tk.StringVar(value="--")
        self.status_var = tk.StringVar(value="正在初始化…")
        self.last_check_var = tk.StringVar(value="尚未检查")
        self.next_check_var = tk.StringVar(value="--")
        self.dates_var = tk.StringVar(value="尚未发现可预约日期")

        self.start_time_var = tk.StringVar()
        self.end_time_var = tk.StringVar()
        self.poll_minutes_var = tk.StringVar()
        self.repeat_minutes_var = tk.StringVar()
        self.smtp_host_var = tk.StringVar()
        self.smtp_port_var = tk.StringVar()
        self.smtp_security_var = tk.StringVar()
        self.smtp_username_var = tk.StringVar()
        self.smtp_sender_var = tk.StringVar()
        self.smtp_recipients_var = tk.StringVar()
        self.smtp_password_var = tk.StringVar()

    def _build_window(self) -> None:
        """构建状态和设置两个标签页。"""

        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.notebook = ttk.Notebook(self)
        self.notebook.grid(row=0, column=0, sticky="nsew", padx=12, pady=12)

        self.status_tab = ttk.Frame(self.notebook, padding=18)
        self.settings_tab = ttk.Frame(self.notebook, padding=18)
        self.notebook.add(self.status_tab, text="监控状态")
        self.notebook.add(self.settings_tab, text="设置")
        self._build_status_tab()
        self._build_settings_tab()

    def _build_status_tab(self) -> None:
        """构建运行状态、操作按钮和简短日志。"""

        self.status_tab.columnconfigure(1, weight=1)
        rows = (
            ("Darwin 时间", self.clock_var),
            ("当前状态", self.status_var),
            ("上次检查", self.last_check_var),
            ("下次检查", self.next_check_var),
            ("预约日期", self.dates_var),
        )
        for row, (label, variable) in enumerate(rows):
            ttk.Label(self.status_tab, text=f"{label}：").grid(
                row=row,
                column=0,
                sticky="nw",
                padx=(0, 12),
                pady=6,
            )
            ttk.Label(
                self.status_tab,
                textvariable=variable,
                wraplength=520,
                justify="left",
            ).grid(row=row, column=1, sticky="nw", pady=6)

        buttons = ttk.Frame(self.status_tab)
        buttons.grid(row=5, column=0, columnspan=2, sticky="w", pady=(18, 12))
        ttk.Button(buttons, text="开始监控", command=self.start_monitoring).pack(
            side="left", padx=(0, 8)
        )
        ttk.Button(buttons, text="暂停监控", command=self.pause_monitoring).pack(
            side="left", padx=(0, 8)
        )
        ttk.Button(buttons, text="立即检查", command=self.check_now).pack(side="left")

        ttk.Label(self.status_tab, text="运行日志：").grid(
            row=6, column=0, columnspan=2, sticky="w", pady=(8, 4)
        )
        log_frame = ttk.Frame(self.status_tab)
        log_frame.grid(row=7, column=0, columnspan=2, sticky="nsew")
        self.status_tab.rowconfigure(7, weight=1)
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log_text = tk.Text(log_frame, height=14, state="disabled", wrap="word")
        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")

    def _build_settings_tab(self) -> None:
        """构建面向普通用户的监控时间和邮件表单。"""

        self.settings_tab.columnconfigure(1, weight=1)
        target_text = "Jobfit Darwin · 0810 / NT · 项目 501、502、705"
        ttk.Label(self.settings_tab, text="固定监控目标：").grid(
            row=0, column=0, sticky="w", pady=5
        )
        ttk.Label(self.settings_tab, text=target_text).grid(
            row=0, column=1, sticky="w", pady=5
        )

        fields: list[tuple[str, tk.StringVar, bool]] = [
            ("开始时间（HH:MM）", self.start_time_var, False),
            ("结束时间（HH:MM）", self.end_time_var, False),
            ("检查间隔（分钟）", self.poll_minutes_var, False),
            ("重复提醒（分钟）", self.repeat_minutes_var, False),
            ("SMTP 服务器", self.smtp_host_var, False),
            ("SMTP 端口", self.smtp_port_var, False),
            ("SMTP 用户名", self.smtp_username_var, False),
            ("发件地址", self.smtp_sender_var, False),
            ("收件地址（逗号分隔）", self.smtp_recipients_var, False),
            ("SMTP 密码", self.smtp_password_var, True),
        ]

        row = 1
        for label, variable, secret in fields[:6]:
            self._add_entry(row, label, variable, secret)
            row += 1

        ttk.Label(self.settings_tab, text="加密方式：").grid(
            row=row, column=0, sticky="w", pady=5
        )
        security_box = ttk.Combobox(
            self.settings_tab,
            textvariable=self.smtp_security_var,
            values=tuple(item.value for item in SmtpSecurity),
            state="readonly",
        )
        security_box.grid(row=row, column=1, sticky="ew", pady=5)
        row += 1

        for label, variable, secret in fields[6:]:
            self._add_entry(row, label, variable, secret)
            row += 1

        ttk.Label(
            self.settings_tab,
            text="密码留空会保留已经存入 Windows 凭据管理器的密码。",
            foreground="#555555",
        ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(2, 12))
        row += 1

        buttons = ttk.Frame(self.settings_tab)
        buttons.grid(row=row, column=0, columnspan=2, sticky="w")
        ttk.Button(buttons, text="保存设置", command=self.save_settings).pack(
            side="left", padx=(0, 8)
        )
        ttk.Button(buttons, text="保存并发送测试邮件", command=self.test_email).pack(
            side="left"
        )

    def _add_entry(
        self,
        row: int,
        label: str,
        variable: tk.StringVar,
        secret: bool,
    ) -> None:
        """添加一行标签和输入框。"""

        ttk.Label(self.settings_tab, text=f"{label}：").grid(
            row=row, column=0, sticky="w", pady=5, padx=(0, 12)
        )
        ttk.Entry(
            self.settings_tab,
            textvariable=variable,
            show="●" if secret else "",
        ).grid(row=row, column=1, sticky="ew", pady=5)

    def _load_fields_from_config(self) -> None:
        """把当前配置显示在设置标签页。"""

        smtp = self.config.smtp
        self.start_time_var.set(self.config.start_time.strftime("%H:%M"))
        self.end_time_var.set(self.config.end_time.strftime("%H:%M"))
        self.poll_minutes_var.set(str(self.config.poll_interval_seconds // 60))
        self.repeat_minutes_var.set(str(self.config.alert_repeat_seconds // 60))
        self.smtp_host_var.set(smtp.host or "smtp.gmail.com")
        self.smtp_port_var.set(str(smtp.port))
        self.smtp_security_var.set(smtp.security.value)
        self.smtp_username_var.set(smtp.username)
        self.smtp_sender_var.set(smtp.sender)
        self.smtp_recipients_var.set(", ".join(smtp.recipients))
        self.smtp_password_var.set("")

    def _config_from_fields(self) -> MonitorConfig:
        """解析设置表单，并返回尚未写入磁盘的新配置。"""

        try:
            start = time.fromisoformat(self.start_time_var.get().strip())
            end = time.fromisoformat(self.end_time_var.get().strip())
            poll_seconds = int(self.poll_minutes_var.get().strip()) * 60
            repeat_seconds = int(self.repeat_minutes_var.get().strip()) * 60
            port = int(self.smtp_port_var.get().strip())
            security = SmtpSecurity(self.smtp_security_var.get().strip())
        except ValueError as exc:
            raise ConfigError("时间、分钟、端口或加密方式格式不正确。") from exc

        recipients = tuple(
            address.strip()
            for address in re.split(r"[,;]", self.smtp_recipients_var.get())
            if address.strip()
        )
        smtp = SmtpConfig(
            host=self.smtp_host_var.get().strip(),
            port=port,
            security=security,
            username=self.smtp_username_var.get().strip(),
            sender=self.smtp_sender_var.get().strip(),
            recipients=recipients,
        )
        candidate = replace(
            self.config,
            start_time=start,
            end_time=end,
            poll_interval_seconds=poll_seconds,
            alert_repeat_seconds=repeat_seconds,
            smtp=smtp,
        )
        errors = validate_config(candidate, require_email=True)
        if errors:
            raise ConfigError("配置无效：\n" + "\n".join(f"- {item}" for item in errors))
        return candidate

    def _save_settings(self, *, show_message: bool) -> bool:
        """验证表单并保存普通配置和可选的新密码。"""

        try:
            candidate = self._config_from_fields()
            password = self.smtp_password_var.get()
            if candidate.smtp.username and not password and not get_smtp_password():
                raise ConfigError("SMTP 用户名需要配套的密码。")
            if password:
                save_smtp_password(password)
            save_config(candidate)
        except (ConfigError, CredentialError, OSError) as exc:
            messagebox.showerror("无法保存设置", str(exc), parent=self)
            return False

        self.config = candidate
        self.smtp_password_var.set("")
        self._append_log("设置已保存。")
        if show_message:
            messagebox.showinfo("设置已保存", "配置保存成功。", parent=self)
        return True

    def save_settings(self) -> None:
        """保存设置并启动监控。"""

        if self._save_settings(show_message=True):
            self.start_monitoring()

    def test_email(self) -> None:
        """保存设置后在后台发送测试邮件。"""

        if not self._save_settings(show_message=False):
            return
        self._start_email("test", None)

    def start_monitoring(self) -> None:
        """启用自动检查，并安排一次尽快执行的检查。"""

        if not self._email_configuration_ready():
            messagebox.showwarning("设置未完成", "请先保存完整的邮件设置。", parent=self)
            self.notebook.select(self.settings_tab)
            return
        self._monitoring = True
        self._next_check_at = self._now()
        self._set_status("监控已启动。")
        self._append_log("自动监控已启动。")

    def pause_monitoring(self) -> None:
        """暂停后续自动检查；正在执行的检查会正常结束。"""

        self._monitoring = False
        self._next_check_at = None
        self.next_check_var.set("--")
        self._set_status("监控已暂停。")
        self._append_log("自动监控已暂停。")

    def check_now(self) -> None:
        """忽略时间窗口，立即发起一次人工检查。"""

        self._start_check(manual=True)

    def _clock_tick(self) -> None:
        """刷新时钟，并在到达时间时启动后台检查。"""

        now = self._now()
        self.clock_var.set(now.strftime("%Y-%m-%d %H:%M:%S %Z"))
        if self._monitoring and not self._worker_busy:
            if is_within_window(now, self.config.start_time, self.config.end_time):
                if self._next_check_at is None or now >= self._next_check_at:
                    self._start_check(manual=False)
                elif self._next_check_at:
                    self.next_check_var.set(self._format_time(self._next_check_at))
            else:
                self._next_check_at = None
                self.next_check_var.set("等待下一个监控时段")
                self._set_status("当前不在监控时段内。")
        self.after(1000, self._clock_tick)

    def _start_check(self, *, manual: bool) -> None:
        """启动单个后台 Selenium 检查，拒绝重复并发。"""

        if self._worker_busy:
            if manual:
                messagebox.showinfo("正在检查", "当前检查尚未结束。", parent=self)
            return
        errors = validate_config(self.config)
        if errors:
            messagebox.showerror("配置错误", "\n".join(errors), parent=self)
            return

        self._worker_busy = True
        self._set_status("正在检查 Bupa 页面…")
        self._append_log("开始检查预约页面。")
        config_snapshot = self.config

        def worker() -> None:
            result = AppointmentMonitor(config_snapshot).check_once()
            self._events.put(("monitor_result", result))

        threading.Thread(target=worker, name="appointment-check").start()

    def _handle_monitor_result(self, result: AvailabilityResult) -> None:
        """更新界面、错误计数和内存提醒状态。"""

        self._worker_busy = False
        self.last_check_var.set(self._format_time(result.checked_at))
        if self._monitoring:
            self._next_check_at = result.checked_at + timedelta(
                seconds=self.config.poll_interval_seconds
            )

        previous_status = self._last_status
        previous_signature = self._last_signature

        if result.status in (AvailabilityStatus.ERROR, AvailabilityStatus.CAPTCHA):
            self._handle_failure(result)
            return

        had_failures = self._consecutive_failures > 0
        self._consecutive_failures = 0
        self._set_status(
            "发现可预约日期！"
            if result.status is AvailabilityStatus.AVAILABLE
            else "本次检查没有发现预约。"
        )
        self._append_log(result.details)

        if result.status is AvailabilityStatus.AVAILABLE:
            self.dates_var.set(
                "；".join(date.label or date.value for date in result.available_dates)
            )
            if should_send_availability(
                result,
                previous_status,
                previous_signature,
                self._last_alert_at,
                self.config.alert_repeat_seconds,
            ):
                self._start_email("availability", result)
        else:
            self.dates_var.set("尚未发现可预约日期")

        self._last_status = result.status
        self._last_signature = result.date_signature
        if had_failures and self._recovery_pending:
            self._start_email("recovery", result.checked_at)

    def _handle_failure(self, result: AvailabilityResult) -> None:
        """处理检查错误、故障阈值和 CAPTCHA 暂停。"""

        self._consecutive_failures += 1
        self._last_status = result.status
        self._set_status(f"监控检查失败：{result.details}")
        self._append_log(
            f"检查失败（连续 {self._consecutive_failures} 次）：{result.details}"
        )

        if result.status is AvailabilityStatus.CAPTCHA:
            self._monitoring = False
            self._next_check_at = None
            self._append_log("检测到访问验证，已暂停自动监控。")
            self._start_email("error", result)
            return

        if self._consecutive_failures < self.config.error_alert_threshold:
            return
        cooldown_elapsed = (
            self._last_error_alert_at is None
            or (result.checked_at - self._last_error_alert_at).total_seconds()
            >= self.config.error_alert_cooldown_seconds
        )
        if cooldown_elapsed:
            self._start_email("error", result)

    def _start_email(self, kind: str, payload: object | None) -> None:
        """在后台发送邮件，避免 SMTP 网络请求阻塞窗口。"""

        if self._email_busy:
            self._append_log("已有邮件正在发送，本次邮件将在后续检查中重试。")
            return
        self._email_busy = True
        config_snapshot = self.config

        def worker() -> None:
            notifier = EmailNotifier(config_snapshot)
            try:
                if kind == "test":
                    notifier.send_test()
                elif kind == "availability" and isinstance(payload, AvailabilityResult):
                    notifier.send_availability(payload)
                elif kind == "error" and isinstance(payload, AvailabilityResult):
                    notifier.send_error(payload)
                elif kind == "recovery" and isinstance(payload, datetime):
                    notifier.send_recovery(payload)
                else:
                    raise EmailNotificationError("无法识别要发送的邮件类型。")
            except EmailNotificationError as exc:
                self._events.put(("email_error", (kind, str(exc))))
            else:
                self._events.put(("email_success", (kind, self._now())))

        threading.Thread(target=worker, name=f"email-{kind}").start()

    def _handle_email_success(self, kind: str, sent_at: datetime) -> None:
        """记录邮件成功时间，用于内存去重和恢复通知。"""

        self._email_busy = False
        if kind == "availability":
            self._last_alert_at = sent_at
            self._append_log("预约提醒邮件已发送。")
        elif kind == "error":
            self._last_error_alert_at = sent_at
            self._recovery_pending = True
            self._append_log("故障提醒邮件已发送。")
        elif kind == "recovery":
            self._recovery_pending = False
            self._append_log("恢复通知邮件已发送。")
        elif kind == "test":
            self._append_log("测试邮件已发送。")
            messagebox.showinfo("测试成功", "测试邮件已经发送。", parent=self)

    def _poll_events(self) -> None:
        """在 Tk 主线程中处理后台线程返回的事件。"""

        try:
            while True:
                event, payload = self._events.get_nowait()
                if event == "monitor_result" and isinstance(payload, AvailabilityResult):
                    self._handle_monitor_result(payload)
                elif event == "email_success" and isinstance(payload, tuple):
                    kind, sent_at = payload
                    self._handle_email_success(str(kind), sent_at)
                elif event == "email_error" and isinstance(payload, tuple):
                    self._email_busy = False
                    kind, error = payload
                    self._append_log(f"邮件发送失败：{error}")
                    if kind == "test":
                        messagebox.showerror("测试邮件失败", str(error), parent=self)
        except queue.Empty:
            pass
        self.after(200, self._poll_events)

    def _email_configuration_ready(self) -> bool:
        """检查普通邮件配置和需要的凭据是否都已就绪。"""

        if validate_config(self.config, require_email=True):
            return False
        if not self.config.smtp.username:
            return True
        try:
            return bool(get_smtp_password())
        except CredentialError as exc:
            self._append_log(str(exc))
            return False

    def _append_log(self, message: str) -> None:
        """在窗口中追加带 Darwin 时间的简短日志。"""

        timestamp = self._now().strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{timestamp}] {message}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _set_status(self, message: str) -> None:
        """更新主状态文字。"""

        self.status_var.set(message)

    def _now(self) -> datetime:
        """返回当前 Darwin 时间。"""

        return datetime.now(ZoneInfo(self.config.timezone_name))

    @staticmethod
    def _format_time(value: datetime) -> str:
        """格式化包含时区的时间。"""

        return value.strftime("%Y-%m-%d %H:%M:%S %Z")

    def _on_close(self) -> None:
        """关闭窗口前提醒用户监控也会停止。"""

        if messagebox.askyesno(
            "退出监控",
            "关闭程序后将停止预约监控，确定退出吗？",
            parent=self,
        ):
            self.destroy()


__all__ = [
    "AppointmentMonitorApp",
    "is_within_window",
    "should_send_availability",
]

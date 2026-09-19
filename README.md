# NT Medical Visa Appointment Monitor

**[下载 Windows 版 v0.1.0](https://github.com/Cynthiachen2023/nt-medical-visa-appointment/releases/tag/v0.1.0)**


**[查看用户使用指南](https://github.com/Cynthiachen2023/nt-medical-visa-appointment/wiki/%E7%94%A8%E6%88%B7%E4%BD%BF%E7%94%A8%E6%8C%87%E5%8D%97)**

一个面向 Windows 的 Bupa Medical Visa Services 预约监控工具。程序使用
Selenium 定时检查 Jobfit Darwin 是否出现可预约日期，并通过电子邮件提醒用户。

本项目只负责监控和提醒，不会自动选择时间、填写个人资料、提交预约或绕过
CAPTCHA。收到提醒后，仍需用户自行完成预约。

## 功能

- 中文桌面界面，可设置监控时段和检查间隔。
- 监控 Medical Examination (501)、Chest X-Ray (502) 和 Serum Creatinine and eGFR (705)。
- 首次发现有号、可用日期变化或持续有号时发送邮件提醒。
- 连续检查失败时发送故障提醒，恢复正常后发送恢复通知。
- 检测到 CAPTCHA 或访问限制时自动暂停。
- SMTP 密码保存在 Windows Credential Manager，不写入配置文件。

## 下载与使用

普通用户请从 [Releases](https://github.com/Cynthiachen2023/nt-medical-visa-appointment/releases/tag/v0.1.0)
下载 Windows ZIP，完整解压后双击 `BupaAppointmentMonitor.exe`。不要只复制 EXE，
程序还需要同目录中的运行库和内置浏览器。

首次启动后配置监控时间和 SMTP 邮件，发送测试邮件成功后点击“开始监控”。完整步骤、
Gmail 配置和常见问题请查看 [Wiki 用户使用指南](https://github.com/Cynthiachen2023/nt-medical-visa-appointment/wiki/%E7%94%A8%E6%88%B7%E4%BD%BF%E7%94%A8%E6%8C%87%E5%8D%97)。

程序必须保持运行，电脑也必须保持开机和联网。关闭窗口后监控会停止。

## 从源码运行

源码运行需要 Windows 10/11、Python 3.11–3.13 和 Google Chrome：

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m nt_medical_visa_appointment
```

命令行维护功能：

```powershell
# 检查一次，不发送邮件
.\.venv\Scripts\python.exe -m nt_medical_visa_appointment --once

# 显示浏览器执行一次检查
.\.venv\Scripts\python.exe -m nt_medical_visa_appointment --once --headed

# 输出不包含密码和邮箱地址的环境诊断
.\.venv\Scripts\python.exe -m nt_medical_visa_appointment --diagnostics
```

## 构建免 Python 版本

```powershell
.\build\build.ps1
```

脚本会安装构建依赖、运行测试、下载并校验固定版本的 Chrome 和 ChromeDriver，
然后使用 PyInstaller 生成：

```text
.build\dist\BupaAppointmentMonitor\
```

如需跳过重复安装或清理旧构建，可使用：

```powershell
.\build\build.ps1 -SkipDependencyInstall
.\build\build.ps1 -Clean
```

## 提醒规则

- 第一次发现有号或日期变化：立即提醒。
- 持续有号：默认每 10 分钟再次提醒。
- 连续三次检查失败：发送故障提醒，同类故障 30 分钟内不重复发送。
- 故障恢复：发送一次恢复通知。
- CAPTCHA 或访问限制：发送提醒并暂停自动监控。

提醒去重状态保存在内存中，重新启动程序后会重新计算。程序不会绕过网站限制，
请使用合理的检查频率并遵守 Bupa 网站的服务条款。

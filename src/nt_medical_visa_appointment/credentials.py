"""通过 Windows Credential Manager 保存 SMTP 密码。

普通配置文件适合保存服务器地址和邮箱，但不应包含明文密码。本模块通过
``keyring`` 使用当前 Windows 用户的凭据库，让密码与源码、日志和 JSON
配置完全分离。
"""

from __future__ import annotations

import keyring
from keyring.errors import KeyringError, PasswordDeleteError

from . import APP_NAME


CREDENTIAL_SERVICE = APP_NAME
SMTP_PASSWORD_KEY = "smtp-password"


class CredentialError(RuntimeError):
    """Windows 凭据读取或写入失败时抛出的中文友好异常。"""


def get_smtp_password() -> str | None:
    """读取当前 Windows 用户保存的 SMTP 密码。

    返回 ``None`` 表示尚未保存密码，而不是凭据系统发生故障。调用者不应
    把返回的密码写入日志、配置文件或界面状态信息。
    """

    try:
        return keyring.get_password(CREDENTIAL_SERVICE, SMTP_PASSWORD_KEY)
    except KeyringError as exc:
        raise CredentialError(f"无法从 Windows 凭据管理器读取 SMTP 密码：{exc}") from exc


def save_smtp_password(password: str) -> None:
    """把 SMTP 密码保存到当前 Windows 用户的凭据库。

    密码中的前导或尾随空格可能是密码本身的一部分，因此不会自动清理；
    这里只拒绝完全为空的值。
    """

    if not password:
        raise ValueError("SMTP 密码不能为空。")

    try:
        keyring.set_password(CREDENTIAL_SERVICE, SMTP_PASSWORD_KEY, password)
    except KeyringError as exc:
        raise CredentialError(f"无法将 SMTP 密码保存到 Windows 凭据管理器：{exc}") from exc


def delete_smtp_password() -> bool:
    """删除已保存的 SMTP 密码，并返回是否确实删除了凭据。

    用户尚未保存密码时返回 ``False``，方便卸载或重置配置重复调用。其他
    凭据系统错误仍会转换为 ``CredentialError``，避免静默忽略真实故障。
    """

    try:
        keyring.delete_password(CREDENTIAL_SERVICE, SMTP_PASSWORD_KEY)
    except PasswordDeleteError:
        return False
    except KeyringError as exc:
        raise CredentialError(f"无法从 Windows 凭据管理器删除 SMTP 密码：{exc}") from exc
    return True


__all__ = [
    "CREDENTIAL_SERVICE",
    "SMTP_PASSWORD_KEY",
    "CredentialError",
    "delete_smtp_password",
    "get_smtp_password",
    "save_smtp_password",
]

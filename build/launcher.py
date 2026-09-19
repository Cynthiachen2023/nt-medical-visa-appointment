"""PyInstaller 使用的顶层入口。

直接打包包内的 ``__main__.py`` 会让 Python 把它当作普通脚本，导致相对导入
失去包上下文。这个文件只负责从正式包中调用统一入口，不包含任何业务逻辑。
"""

from nt_medical_visa_appointment.__main__ import main


if __name__ == "__main__":
    raise SystemExit(main())

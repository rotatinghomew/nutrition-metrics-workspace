"""应用层统一错误类型。"""
from __future__ import annotations


class CheckError(ValueError):
    """校核失败：带机器可读 code 与可直接展示给业务人员的具体原因。"""

    def __init__(self, code: str, message: str):
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message


class RegistryError(ValueError):
    """登记册持久化或结构错误。"""

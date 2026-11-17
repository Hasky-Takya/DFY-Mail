"""非交互服务的凭据边界；此模块不读取配置文件、不联网、不记录凭据。"""

import os
import sys
from getpass import getpass


def require_credential(env_name, prompt, *, input_func=None):
    """环境非空值优先并 trim；只有 TTY 才可调用入口传入的 getpass。"""
    if type(env_name) is not str or not env_name or "\x00" in env_name or "=" in env_name:
        raise ValueError("凭据环境变量名称无效。")
    value = os.environ.get(env_name, "").strip()
    if value:
        return value
    try:
        interactive = sys.stdin is not None and sys.stdin.isatty()
    except Exception:
        interactive = False
    if not interactive:
        raise ValueError("缺少运行凭据；非交互模式必须通过环境变量提供。")
    reader = getpass if input_func is None else input_func
    try:
        value = reader(prompt)
    except Exception:
        raise RuntimeError("无法读取运行凭据。") from None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("运行凭据不能为空。")
    return value.strip()

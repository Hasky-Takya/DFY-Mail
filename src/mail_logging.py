"""各脚本共用的状态日志：UTF-8、按天轮转，不记录问答或邮件正文。"""

import logging
import os
import re
import traceback
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path


LOG_DIR = Path(__file__).resolve().parent.parent / "log" / "output_log"


class SafeFormatter(logging.Formatter):
    def __init__(self, secrets=(), script="mail.py"):
        super().__init__("%(asctime)s | %(levelname)s | pid=%(process)d | %(filename)s:%(lineno)d | %(name)s | %(message)s")
        self.script = script
        self.secrets = tuple(value for value in secrets if isinstance(value, str) and value)

    def formatException(self, exc_info):
        # 不保存异常消息、源码行或局部变量，避免带出请求正文和凭据。
        frames = traceback.extract_tb(exc_info[2])
        stack = "\n".join(
            f'  File "{frame.filename}", line {frame.lineno}, in {frame.name}' for frame in frames
        )
        return stack + "\n" + exc_info[0].__name__

    def format(self, record):
        cached_exception = record.exc_text
        record.exc_text = None
        try:
            message = record.getMessage()
            if record.exc_info:
                message += "\n" + self.formatException(record.exc_info)
            # 每行都带时间、入口及实际产生日志的源文件。
            prefix = (
                f"{self.formatTime(record)} | {record.levelname} | pid={record.process} | "
                f"entry={self.script} | {record.filename}:{record.lineno} | {record.name} | "
            )
            text = "\n".join(prefix + line for line in message.splitlines())
        finally:
            record.exc_text = cached_exception
        for secret in sorted(self.secrets, key=len, reverse=True):
            text = text.replace(secret, "[敏感信息已隐藏]")
        text = re.sub(r"(?i)\bsk-[A-Za-z0-9_-]+", "[API密钥已隐藏]", text)
        text = re.sub(r"[^\s<>@]+@[^\s<>@]+", "[邮箱已隐藏]", text)
        return text


def close_logging() -> None:
    """仅关闭本项目的处理器，不影响宿主或测试框架的日志。"""
    logger = logging.getLogger("dfy")
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        handler.close()


def configure_logging(log_dir: str | Path = LOG_DIR, secrets=(), *, script="mail.py") -> Path:
    """按入口分文件，保留 14 个轮转备份；重复配置不会重复输出。"""
    directory = Path(log_dir)
    directory.mkdir(parents=True, exist_ok=True)
    script = Path(script).name
    if not re.fullmatch(r"[A-Za-z0-9_-]+\.py", script):
        raise ValueError("日志入口名称必须是 Python 脚本文件名。")
    path = directory / (Path(script).stem + ".log")
    formatter = SafeFormatter(secrets, script=script)
    file_handler = TimedRotatingFileHandler(
        path, when="midnight", interval=1, backupCount=14, encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    close_logging()
    logger = logging.getLogger("dfy")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    return path


def set_log_secrets(*secrets: str) -> None:
    for handler in logging.getLogger("dfy").handlers:
        if isinstance(handler.formatter, SafeFormatter):
            values = set(handler.formatter.secrets)
            for value in secrets:
                if isinstance(value, str) and value:
                    values.add(value)
                    if value.strip():
                        values.add(value.strip())
            handler.formatter.secrets = tuple(values)


def start_logging(script: str) -> Path:
    """CLI 在任何配置读取或网络操作之前调用。"""
    try:
        path = configure_logging(script=script)
        set_log_secrets(os.getenv("DEEPSEEK_API_KEY", ""), os.getenv("MAIL_AUTH_CODE", ""))
        return path
    except Exception:
        close_logging()
        raise SystemExit("无法创建状态日志，请检查 log/output_log 的路径和写入权限。") from None

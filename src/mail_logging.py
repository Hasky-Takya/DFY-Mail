"""监听专用日志：UTF-8 文件按天轮转，同时输出到终端。"""

import logging
import re
import traceback
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path


LOG_DIR = Path(__file__).resolve().parent.parent / "log" / "mail_log"


class SafeFormatter(logging.Formatter):
    def __init__(self, secrets=()):
        super().__init__("%(asctime)s | %(levelname)s | pid=%(process)d | %(name)s | %(message)s")
        self.secrets = tuple(value for value in secrets if isinstance(value, str) and value)

    def formatException(self, exc_info):
        # 保留调用栈和异常类型，不保存可能带请求正文／凭据的原始异常消息。
        frames = traceback.extract_tb(exc_info[2])
        stack = "\n".join(
            f'  File "{frame.filename}", line {frame.lineno}, in {frame.name}' for frame in frames
        )
        return stack + "\n" + exc_info[0].__name__

    def format(self, record):
        cached_exception = record.exc_text
        record.exc_text = None
        try:
            text = super().format(record)
        finally:
            record.exc_text = cached_exception
        for secret in sorted(self.secrets, key=len, reverse=True):
            text = text.replace(secret, "[敏感信息已隐藏]")
        text = re.sub(r"(?i)\bsk-[A-Za-z0-9_-]+", "[API密钥已隐藏]", text)
        text = re.sub(r"[^\s<>@]+@[^\s<>@]+", "[邮箱已隐藏]", text)
        return text


def configure_logging(log_dir: str | Path = LOG_DIR, secrets=()) -> Path:
    """文件保留最近 14 个轮转备份；重复配置不会重复添加处理器。"""
    directory = Path(log_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "mail.log"
    logger = logging.getLogger("dfy")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        handler.close()
    formatter = SafeFormatter(secrets)
    file_handler = TimedRotatingFileHandler(
        path, when="midnight", interval=1, backupCount=14, encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    return path


def set_log_secrets(*secrets: str) -> None:
    for handler in logging.getLogger("dfy").handlers:
        if isinstance(handler.formatter, SafeFormatter):
            handler.formatter.secrets = tuple(value for value in secrets if value)

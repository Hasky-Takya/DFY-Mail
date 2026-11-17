"""共用状态日志：UTF-8、时间及体积轮转，日志故障不改变邮件发送结果。"""

import logging
import os
import re
import sys
import time
import traceback
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = PROJECT_ROOT / "log" / "output_log"
_LOGGING_ERROR = "DFY_LOGGING_ERROR: status logging unavailable.\n"
_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
_DEFAULTS = {
    "directory": "log/output_log",
    "file_level": "INFO",
    "console_level": "INFO",
    "backup_count": 14,
    "max_bytes": 10485760,
    "utc": False,
}


def normalize_logging_settings(settings: dict) -> dict:
    """校验 logging 节；错误不回显值，显式 null、未知键和 bool-as-int 均拒绝。"""
    if type(settings) is not dict:
        raise ValueError("logging 配置必须是对象。")
    if settings.keys() - _DEFAULTS.keys():
        raise ValueError("logging 配置包含未知字段。")
    result = dict(_DEFAULTS)
    result.update(settings)
    directory = result["directory"]
    if type(directory) is not str or not directory.strip() or "\x00" in directory:
        raise ValueError("logging.directory 必须是非空路径字符串。")
    for key in ("file_level", "console_level"):
        if type(result[key]) is not str or result[key] not in _LEVELS:
            raise ValueError("logging 日志级别无效。")
    if type(result["backup_count"]) is not int or not 1 <= result["backup_count"] <= 365:
        raise ValueError("logging.backup_count 必须在 1 到 365 之间。")
    if type(result["max_bytes"]) is not int or not 1024 <= result["max_bytes"] <= 1073741824:
        raise ValueError("logging.max_bytes 必须在 1024 到 1073741824 之间。")
    if type(result["utc"]) is not bool:
        raise ValueError("logging.utc 必须是布尔值。")
    return result


def _safe_stderr():
    # 不调用 logging、traceback 或 repr(record/exception)，也不依赖 raiseExceptions。
    try:
        sys.stderr.write(_LOGGING_ERROR)
        sys.stderr.flush()
    except Exception:
        pass


class SafeFormatter(logging.Formatter):
    def __init__(self, secrets=(), script="mail.py"):
        super().__init__("%(asctime)s | %(levelname)s | pid=%(process)d | %(filename)s:%(lineno)d | %(name)s | %(message)s")
        self.script = script
        self.secrets = tuple({item for value in secrets if isinstance(value, str) and value
                              for item in (value, value.strip()) if item})

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


class _SafeHandler:
    failed = False

    def handleError(self, record):
        self.failed = True
        _safe_stderr()

    def flush(self):
        try:
            super().flush()
        except Exception:
            self.handleError(None)

    def close(self):
        try:
            super().close()
        except Exception:
            self.handleError(None)

    def emit(self, record):
        try:
            super().emit(record)
        except Exception:
            self.handleError(record)


class SafeConsoleHandler(_SafeHandler, logging.StreamHandler):
    pass


class SafeRotatingFileHandler(_SafeHandler, TimedRotatingFileHandler):
    """保持 TimedRotatingFileHandler 兼容，时间/体积轮转共用一个保留上限。"""

    def __init__(self, filename, *, backup_count, max_bytes, utc):
        self.maxBytes = max_bytes
        self._last_rotation = 0
        super().__init__(filename, when="midnight", interval=1, backupCount=backup_count,
                         encoding="utf-8", errors="backslashreplace", utc=utc)

    def shouldRollover(self, record):
        if super().shouldRollover(record):
            return True
        if self.stream is None:
            self.stream = self._open()
        self.stream.seek(0, os.SEEK_END)
        size = self.stream.tell()
        # 空文件允许一条超限记录，避免在每条大记录前产生空备份。
        return size > 0 and size + len((self.format(record) + self.terminator).encode(
            "utf-8", errors="backslashreplace")) >= self.maxBytes

    def getFilesToDelete(self):
        base = Path(self.baseFilename)
        pattern = re.compile(re.escape(base.name) + r"\.\d{4}-\d{2}-\d{2}(?:\.\d{20})?$")
        backups = sorted(path for path in base.parent.iterdir() if pattern.fullmatch(path.name))
        return [str(path) for path in backups[:-self.backupCount]]

    def doRollover(self):
        try:
            if self.stream:
                self.stream.close()
                self.stream = None
            now = int(time.time())
            clock = time.gmtime if self.utc else time.localtime
            # 同日多次体积轮转不能覆盖之前备份；序号跨同一处理器始终递增。
            stamp = max(time.time_ns(), self._last_rotation + 1)
            date = time.strftime("%Y-%m-%d", clock(now))
            target = f"{self.baseFilename}.{date}.{stamp:020d}"
            while os.path.exists(target):
                stamp += 1
                target = f"{self.baseFilename}.{date}.{stamp:020d}"
            self._last_rotation = stamp
            if os.path.exists(self.baseFilename):
                self.rotate(self.baseFilename, target)
            for path in self.getFilesToDelete():
                os.remove(path)
            if not self.delay:
                self.stream = self._open()
            # 父类处理午夜、本地时区及夏令时的时间表。
            rollover_at = self.computeRollover(now)
            while rollover_at <= now:
                rollover_at += self.interval
            if not self.utc:
                dst_now = clock(now).tm_isdst
                dst_then = clock(rollover_at).tm_isdst
                if dst_now != dst_then:
                    rollover_at += -3600 if not dst_now else 3600
            self.rolloverAt = rollover_at
        except Exception:
            self.handleError(None)


def close_logging() -> None:
    """仅关闭本项目的处理器，不影响宿主或测试框架的日志。"""
    logger = logging.getLogger("dfy")
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            _safe_stderr()


def _directory_path(log_dir):
    value = os.environ.get("DFY_LOG_DIR")
    if value is not None:
        if not value.strip() or "\x00" in value:
            raise ValueError("DFY_LOG_DIR 必须是非空路径。")
        directory = Path(value.strip())
    else:
        directory = Path(log_dir)
    if not directory.is_absolute():
        directory = (PROJECT_ROOT / directory).resolve()
        if not directory.is_relative_to(PROJECT_ROOT):
            raise ValueError("相对日志路径不能离开项目目录。")
    return directory


def _registered_secrets():
    return tuple(value for handler in logging.getLogger("dfy").handlers
                 if isinstance(handler.formatter, SafeFormatter)
                 for value in handler.formatter.secrets)


def configure_logging(log_dir: str | Path = LOG_DIR, secrets=(), *, script="mail.py", settings=None) -> Path:
    """旧参数兼容；settings 是 logging 节，显式 directory/DFY_LOG_DIR 可覆盖路径。"""
    options = normalize_logging_settings({} if settings is None else settings)
    directory = _directory_path(options["directory"] if settings is not None and "directory" in settings else log_dir)
    script = Path(script).name
    if not re.fullmatch(r"[A-Za-z0-9_-]+\.py", script):
        raise ValueError("日志入口名称必须是 Python 脚本文件名。")
    path = directory / (Path(script).stem + ".log")
    formatter = SafeFormatter(_registered_secrets() + tuple(secrets), script=script)
    file_handler = None
    try:
        directory.mkdir(parents=True, exist_ok=True)
        file_handler = SafeRotatingFileHandler(path, backup_count=options["backup_count"],
                                               max_bytes=options["max_bytes"], utc=options["utc"])
        file_handler.setLevel(options["file_level"])
        file_handler.setFormatter(formatter)
        console_handler = SafeConsoleHandler()
        console_handler.setLevel(options["console_level"])
        console_handler.setFormatter(formatter)
    except Exception:
        if file_handler is not None:
            file_handler.close()
        raise RuntimeError("无法初始化状态日志。") from None
    logger = logging.getLogger("dfy")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    # 单次替换列表，避免重配瞬间零处理器触发 lastResort 原样输出 record。
    previous = logger.handlers
    logger.handlers = [file_handler, console_handler]
    for handler in previous:
        try:
            handler.close()
        except Exception:
            _safe_stderr()
    return path


def apply_logging_settings(settings=None) -> Path:
    """启动后应用 logging 节，保留路径、入口、秘密集合；不添加重复处理器。"""
    handlers = logging.getLogger("dfy").handlers
    current = next((handler for handler in handlers if isinstance(handler, SafeRotatingFileHandler)), None)
    if current is None:
        raise RuntimeError("状态日志尚未初始化。")
    path = configure_logging(Path(current.baseFilename).parent,
                             script=current.formatter.script, settings=settings)
    if not check_logging_health():
        raise RuntimeError("状态日志健康检查失败。")
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


def check_logging_health() -> bool:
    """仅在启动边界使用：直接写固定诊断并 flush，失败返回 False、不退出。"""
    handlers = logging.getLogger("dfy").handlers
    if not handlers:
        return False
    record = logging.LogRecord("dfy", logging.INFO, __file__, 0, "状态日志启动检查", (), None)
    for handler in handlers:
        try:
            # 即使设为 ERROR 也需实际验证文件写入，不经级别过滤。
            handler.handle(record)
            handler.flush()
        except Exception:
            handler.handleError(None)
    return not any(getattr(handler, "failed", False) for handler in handlers)


def start_logging(script: str) -> Path:
    """CLI 在任何配置读取或网络操作之前调用；初始化失败为固定非零退出码。"""
    try:
        path = configure_logging(script=script)
        set_log_secrets(os.getenv("DEEPSEEK_API_KEY", ""), os.getenv("MAIL_AUTH_CODE", ""))
        if not check_logging_health():
            raise RuntimeError("状态日志健康检查失败。")
        return path
    except Exception:
        close_logging()
        _safe_stderr()
        raise SystemExit(1) from None

"""离线凭据/日志故障回归；只使用临时目录和 fake TTY。"""

import importlib.util
import io
import logging
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from unittest.mock import Mock, patch


SRC = Path(__file__).resolve().parents[1] / "src"


def load_local(name):
    spec = importlib.util.spec_from_file_location("isolated_" + name, SRC / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runtime = load_local("runtime_config")
logs = load_local("mail_logging")
SENTINEL = "PRIVATE_FAILURE_SENTINEL"


class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_environment_wins_and_trims_without_prompt_or_tty_probe(self):
        reader = Mock(side_effect=AssertionError("must not prompt"))
        stdin = Mock()
        stdin.isatty.side_effect = AssertionError("must not inspect TTY")
        with patch.dict(os.environ, {"TEST_CREDENTIAL": "  credential-value \n"}), patch.object(sys, "stdin", stdin):
            self.assertEqual(runtime.require_credential("TEST_CREDENTIAL", "prompt", input_func=reader), "credential-value")
        reader.assert_not_called()

    def test_missing_or_blank_noninteractive_never_prompts(self):
        reader = Mock(side_effect=AssertionError("must not prompt"))
        for value in ("", " \t\n"):
            with self.subTest(value=value), patch.dict(os.environ, {"TEST_CREDENTIAL": value}), \
                 patch.object(sys, "stdin", Mock(isatty=Mock(return_value=False))), \
                 redirect_stderr(io.StringIO()) as stderr:
                with self.assertRaises(ValueError):
                    runtime.require_credential("TEST_CREDENTIAL", "prompt " + SENTINEL, input_func=reader)
                self.assertEqual(stderr.getvalue(), "")
        reader.assert_not_called()

    def test_fake_tty_uses_injected_getpass_and_trims(self):
        reader = Mock(return_value="  typed-credential  ")
        with patch.object(sys, "stdin", Mock(isatty=Mock(return_value=True))):
            self.assertEqual(runtime.require_credential("TEST_CREDENTIAL", "prompt", input_func=reader), "typed-credential")
        reader.assert_called_once_with("prompt")

    def test_unusable_stdin_fails_closed(self):
        for stdin in (None, Mock(isatty=Mock(side_effect=OSError(SENTINEL)))):
            with self.subTest(stdin=stdin), patch.object(sys, "stdin", stdin):
                with self.assertRaises(ValueError) as result:
                    runtime.require_credential("TEST_CREDENTIAL", SENTINEL)
                self.assertNotIn(SENTINEL, str(result.exception))

    def test_tty_empty_or_reader_failure_is_safe(self):
        with patch.object(sys, "stdin", Mock(isatty=Mock(return_value=True))):
            for value in (None, "", "   "):
                with self.assertRaises(ValueError):
                    runtime.require_credential("TEST_CREDENTIAL", "prompt", input_func=Mock(return_value=value))
            for error in (EOFError(SENTINEL), OSError(SENTINEL), RuntimeError(SENTINEL)):
                with self.assertRaises(RuntimeError) as result:
                    runtime.require_credential("TEST_CREDENTIAL", "prompt", input_func=Mock(side_effect=error))
                self.assertNotIn(SENTINEL, str(result.exception))


class LoggingBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(logs.close_logging)
        env = patch.dict(os.environ, {}, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def configure(self, settings=None):
        path = logs.configure_logging(self.directory, script="dfy_receive.py", settings=settings)
        logging.getLogger("dfy").handlers[1].setStream(io.StringIO())
        return path

    def file_handler(self):
        return logging.getLogger("dfy").handlers[0]

    def test_legacy_signature_secrets_trim_and_reconfigure_stays_redacted(self):
        path = logs.configure_logging(self.directory, ("  legacy-credential  ",), script="mail.py")
        logging.getLogger("dfy").handlers[1].setStream(io.StringIO())
        logging.getLogger("dfy.mail").info("legacy-credential")
        with redirect_stderr(io.StringIO()):
            logs.apply_logging_settings()
        logging.getLogger("dfy").handlers[1].setStream(io.StringIO())
        logging.getLogger("dfy.mail").info("legacy-credential")
        self.assertNotIn("legacy-credential", path.read_text(encoding="utf-8"))
        self.assertEqual(len(logging.getLogger("dfy").handlers), 2)

    def test_defaults_and_strict_validation(self):
        self.assertEqual(logs.normalize_logging_settings({}), {
            "directory": "log/output_log", "file_level": "INFO", "console_level": "INFO",
            "backup_count": 14, "max_bytes": 10485760, "utc": False,
        })
        invalid = [None, [], {"enabled": True}, {"log_dir": "x"},
                   {"backup_count": True}, {"backup_count": 0}, {"backup_count": 366},
                   {"max_bytes": True}, {"max_bytes": 1023}, {"max_bytes": 1073741825},
                   {"utc": 0}, {"file_level": "info"}, {"console_level": 20},
                   {"directory": ""}, {"directory": Path("x")}]
        invalid += [{key: None} for key in logs.normalize_logging_settings({})]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                logs.normalize_logging_settings(value)

    def test_relative_paths_project_anchored_and_environment_overrides(self):
        with patch.object(logs, "PROJECT_ROOT", self.directory):
            self.assertEqual(logs._directory_path("log/output_log"), self.directory / "log" / "output_log")
            with self.assertRaises(ValueError):
                logs._directory_path("../escape")
            with patch.dict(os.environ, {"DFY_LOG_DIR": str(self.directory / "env-log")}):
                path = self.configure({"directory": "ignored"})
                self.assertEqual(path.parent, self.directory / "env-log")
            with patch.dict(os.environ, {"DFY_LOG_DIR": "  "}):
                with self.assertRaises(ValueError):
                    self.configure()

    def test_apply_settings_retains_secrets_script_and_two_handlers(self):
        self.configure()
        logs.set_log_secrets("  private-credential  ")
        with redirect_stderr(io.StringIO()):
            for _ in range(3):
                path = logs.apply_logging_settings({"file_level": "DEBUG", "console_level": "ERROR", "utc": True})
            logging.getLogger("dfy.graph.query").info("private-credential 图谱状态")
        self.assertEqual(len(logging.getLogger("dfy").handlers), 2)
        self.assertEqual(self.file_handler().level, logging.DEBUG)
        self.assertTrue(self.file_handler().utc)
        self.assertIsInstance(self.file_handler(), TimedRotatingFileHandler)
        self.assertNotIn("private-credential", path.read_text(encoding="utf-8"))
        self.assertIn("图谱状态", path.read_text(encoding="utf-8"))
        self.assertIn("entry=dfy_receive.py", path.read_text(encoding="utf-8"))

    def test_size_and_time_rollovers_unique_and_total_retention_bounded(self):
        path = self.configure({"max_bytes": 1024, "backup_count": 3})
        handler = self.file_handler()
        with redirect_stderr(io.StringIO()):
            for index in range(4):
                logging.getLogger("dfy.graph.store").info("batch-%s %s", index, "中" * 350)
            backups = list(self.directory.glob(path.name + ".*"))
            self.assertEqual(len(backups), 3)
            self.assertEqual(len({item.name for item in backups}), 3)
            for index in range(3):
                self.assertTrue(any("batch-" + str(index) in item.read_text(encoding="utf-8") for item in backups))
            handler.rolloverAt = int(time.time()) - 1
            logging.getLogger("dfy.graph.store").info("time-boundary")
            self.assertEqual(len(list(self.directory.glob(path.name + ".*"))), 3)
            for index in range(10):
                logging.getLogger("dfy.graph.store").info("later-%s %s", index, "x" * 1100)
            self.assertEqual(len(list(self.directory.glob(path.name + ".*"))), 3)
        self.assertTrue(handler.rolloverAt > time.time())

    def test_write_flush_format_and_rollover_failures_do_not_raise_or_leak(self):
        path = self.configure()
        logs.set_log_secrets(SENTINEL)
        handler = self.file_handler()
        for target, method in ((handler.stream, "write"), (handler.stream, "flush"),
                               (handler, "format"), (handler, "rotate")):
            with self.subTest(method=method), redirect_stderr(io.StringIO()) as stderr:
                handler.failed = False
                with patch.object(target, method, side_effect=OSError(SENTINEL)):
                    if method == "rotate":
                        handler.doRollover()
                    else:
                        logging.getLogger("dfy.smtp").error("record " + SENTINEL)
                self.assertTrue(handler.failed)
                self.assertIn("DFY_LOGGING_ERROR", stderr.getvalue())
                self.assertNotIn(SENTINEL, stderr.getvalue())
                self.assertNotIn("Traceback", stderr.getvalue())
        # 文件故障不能改变已经成功 SMTP 的返回/状态；文件里也没有异常消息。
        self.assertNotIn(SENTINEL, path.read_text(encoding="utf-8"))

    def test_failed_reconfiguration_preserves_old_handlers_and_secrets(self):
        path = self.configure()
        logs.set_log_secrets(SENTINEL)
        previous = logging.getLogger("dfy").handlers[:]
        with patch.object(logs.SafeRotatingFileHandler, "_open", side_effect=OSError(SENTINEL)):
            with self.assertRaises(RuntimeError) as result:
                logs.apply_logging_settings({"file_level": "DEBUG"})
        self.assertNotIn(SENTINEL, str(result.exception))
        self.assertEqual(logging.getLogger("dfy").handlers, previous)
        logging.getLogger("dfy.smtp").info(SENTINEL)
        self.assertNotIn(SENTINEL, path.read_text(encoding="utf-8"))

    def test_rollover_reopen_and_cleanup_failure_safe(self):
        path = self.configure({"backup_count": 1})
        handler = self.file_handler()
        logging.getLogger("dfy.smtp").info("state")
        for target, method in ((handler, "_open"), (logs.os, "remove")):
            with self.subTest(method=method), redirect_stderr(io.StringIO()) as stderr:
                handler.failed = False
                with patch.object(target, method, side_effect=OSError(SENTINEL)):
                    handler.doRollover()
                self.assertTrue(handler.failed)
                self.assertNotIn(SENTINEL, stderr.getvalue())
                self.assertIn("DFY_LOGGING_ERROR", stderr.getvalue())
                # 下一条仍能尝试恢复，不把日志故障变成业务异常。
                logging.getLogger("dfy.smtp").info("state-recovered")
        self.assertNotIn(SENTINEL, path.read_text(encoding="utf-8"))

    def test_console_failure_sanitized_and_health_does_not_exit(self):
        path = self.configure()
        logs.set_log_secrets(SENTINEL)
        console = logging.getLogger("dfy").handlers[1]
        console.stream = Mock(write=Mock(side_effect=OSError(SENTINEL)))
        with redirect_stderr(io.StringIO()) as stderr:
            logging.getLogger("dfy.smtp").error(SENTINEL)
            self.assertFalse(logs.check_logging_health())
        self.assertNotIn(SENTINEL, stderr.getvalue())
        self.assertNotIn(SENTINEL, path.read_text(encoding="utf-8"))
        self.assertIn("DFY_LOGGING_ERROR", stderr.getvalue())

    def test_start_initialization_and_probe_failures_fixed_nonzero(self):
        configure = logs.configure_logging
        for failure in ("mkdir", "open", "write", "flush"):
            with self.subTest(failure=failure), redirect_stderr(io.StringIO()) as stderr:
                setup = patch.object(logs, "configure_logging", side_effect=lambda **kwargs: configure(self.directory, **kwargs))
                targets = {"mkdir": (Path, "mkdir"), "open": (logs.SafeRotatingFileHandler, "_open"),
                           "write": (logs.SafeRotatingFileHandler, "emit"), "flush": (logs.SafeRotatingFileHandler, "flush")}
                target, attribute = targets[failure]
                with setup, patch.object(target, attribute, side_effect=OSError(SENTINEL)):
                    with self.assertRaises(SystemExit) as result:
                        logs.start_logging("mail.py")
                self.assertEqual(result.exception.code, 1)
                self.assertIn("DFY_LOGGING_ERROR", stderr.getvalue())
                self.assertNotIn(SENTINEL, stderr.getvalue())
                self.assertFalse(logging.getLogger("dfy").handlers)


if __name__ == "__main__":
    unittest.main()

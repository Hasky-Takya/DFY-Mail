"""统一状态日志测试：只使用临时文件、模拟模型和邮箱。"""

import io
import logging
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from test_receive import llm, load_module, mail, mail_logging, proactive, receive, sdk


class OutputLoggingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(mail_logging.close_logging)
        argv = patch.object(sys, "argv", ["script.py"])
        argv.start()
        self.addCleanup(argv.stop)
        for module in (receive, proactive):
            lock = patch.object(module, "SingleInstanceLock")
            lock.start()
            self.addCleanup(lock.stop)
        self.directory = Path(self.tmp.name)

    def start(self, script):
        return mail_logging.configure_logging(self.directory, script=script)

    def test_routing_multiline_and_secrets(self):
        self.assertEqual(mail_logging.LOG_DIR, Path(__file__).resolve().parents[1] / "log" / "output_log")
        for script in ("dfy_test.py", "dfy_mail.py", "dfy_receive.py", "mail.py"):
            with self.subTest(script=script):
                path = self.start(script)
                mail_logging.set_log_secrets("custom-auth")
                mail_logging.set_log_secrets(" private-key ")
                logging.getLogger("dfy.chat").info("第一行\n第二行 custom-auth private-key")
                text = path.read_text(encoding="utf-8")
                self.assertEqual(path.name, Path(script).stem + ".log")
                self.assertNotIn("custom-auth", text)
                self.assertNotIn("private-key", text)
                for line in text.splitlines():
                    self.assertRegex(line, r"^\d{4}-\d\d-\d\d .*\| INFO \| pid=\d+")
                    self.assertIn("entry=" + script, line)
                    self.assertRegex(line, r"test_logging.py:\d+")
                self.assertEqual(len(logging.getLogger("dfy").handlers), 2)

    def test_model_logs_only_status_on_success_and_exception(self):
        path = self.start("dfy_test.py")
        client = MagicMock()
        client.chat.completions.create.return_value = SimpleNamespace(choices=[
            SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content="PRIVATE ANSWER"))
        ])
        with patch.object(llm, "OpenAI", return_value=client):
            bot = llm.DeepSeekChat("private-api")
            self.assertEqual(bot.ask("PRIVATE QUESTION", system_prompt="PRIVATE PROMPT"), "PRIVATE ANSWER")
            client.chat.completions.create.side_effect = RuntimeError("PRIVATE FAILURE private-api")
            with self.assertRaises(RuntimeError):
                bot.ask("PRIVATE QUESTION")
        text = path.read_text(encoding="utf-8")
        self.assertIn("请求完成", text)
        self.assertIn("duration=", text)
        self.assertIn("RuntimeError", text)
        for secret in ("PRIVATE ANSWER", "PRIVATE QUESTION", "PRIVATE PROMPT", "PRIVATE FAILURE", "private-api"):
            self.assertNotIn(secret, text)

    def test_chat_main_keeps_answer_only_on_terminal(self):
        with patch.object(llm, "start_logging", side_effect=self.start), \
             patch.object(llm, "DeepSeekChat") as factory, \
             patch.dict(os.environ, {"DEEPSEEK_API_KEY": "private-api"}), \
             patch("builtins.input", side_effect=["PRIVATE QUESTION", "exit"]), \
             redirect_stdout(io.StringIO()) as output:
            factory.return_value.ask.return_value = "PRIVATE ANSWER"
            llm.main()
        text = (self.directory / "dfy_test.log").read_text(encoding="utf-8")
        self.assertIn("PRIVATE ANSWER", output.getvalue())
        self.assertIn("交互问答结束", text)
        for private in ("PRIVATE ANSWER", "PRIVATE QUESTION", "private-api"):
            self.assertNotIn(private, text)

    def test_mail_read_body_not_logged_and_custom_env_used(self):
        settings = {"assistant": {"auth_code_env": "CUSTOM_MAIL_AUTH"}}
        with patch.object(mail, "start_logging", side_effect=self.start), \
             patch.object(mail, "_load_settings", return_value=settings), \
             patch.object(mail, "MailClient") as factory, \
             patch.dict(os.environ, {"CUSTOM_MAIL_AUTH": "private-auth"}), \
             patch.object(sys, "argv", ["mail.py", "read"]), \
             redirect_stdout(io.StringIO()) as output:
            factory.return_value.read_user_emails.return_value = [{"body": "PRIVATE BODY"}]
            mail.main()
            self.assertEqual(factory.call_args.kwargs["auth_code"], "private-auth")
        text = (self.directory / "mail.log").read_text(encoding="utf-8")
        self.assertIn("PRIVATE BODY", output.getvalue())
        self.assertIn("数量=1", text)
        self.assertNotIn("PRIVATE BODY", text)
        self.assertNotIn("private-auth", text)

    def test_argparse_help_and_error_logged_without_arguments(self):
        for argv, code in ((["mail.py", "--help"], 0), (["mail.py", "PRIVATE INVALID ARG"], 2)):
            with self.subTest(code=code), \
                 patch.object(mail, "start_logging", side_effect=self.start), \
                 patch.object(sys, "argv", argv), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as result:
                    mail.main()
                self.assertEqual(result.exception.code, code)
            text = (self.directory / "mail.log").read_text(encoding="utf-8")
            self.assertIn(f"退出码={code}", text)
            self.assertNotIn("PRIVATE INVALID ARG", text)

    def test_proactive_error_logged_without_original_message(self):
        with patch.object(proactive, "start_logging", side_effect=self.start), \
             patch.object(proactive, "load_prompt", side_effect=RuntimeError("PRIVATE BODY")):
            with self.assertRaises(SystemExit) as result:
                proactive.main()
            self.assertEqual(result.exception.code, 1)
        text = (self.directory / "dfy_mail.log").read_text(encoding="utf-8")
        self.assertIn("主动发信进程结束", text)
        self.assertIn("RuntimeError", text)
        self.assertNotIn("PRIVATE BODY", text)

    def test_listener_error_logged_without_original_message(self):
        with patch.object(receive, "start_logging", side_effect=self.start), \
             patch.object(receive, "_load_settings", side_effect=ValueError("PRIVATE CONFIG")):
            with self.assertRaises(SystemExit) as result:
                receive.main()
            self.assertEqual(result.exception.code, 1)
        text = (self.directory / "dfy_receive.log").read_text(encoding="utf-8")
        self.assertIn("监听进程结束", text)
        self.assertNotIn("PRIVATE CONFIG", text)

    def test_chat_interrupt_and_unexpected_error(self):
        for error, code in ((EOFError(), None), (RuntimeError("PRIVATE ERROR"), 1)):
            with self.subTest(error=type(error).__name__), \
                 patch.object(llm, "start_logging", side_effect=self.start), \
                 patch.object(llm, "DeepSeekChat"), \
                 patch.dict(os.environ, {"DEEPSEEK_API_KEY": "private-key"}), \
                 patch("builtins.input", side_effect=error), redirect_stdout(io.StringIO()):
                if code is None:
                    llm.main()
                else:
                    with self.assertRaises(SystemExit) as result:
                        llm.main()
                    self.assertEqual(result.exception.code, code)
            text = (self.directory / "dfy_test.log").read_text(encoding="utf-8")
            self.assertIn("交互问答结束", text)
            self.assertNotIn("PRIVATE ERROR", text)

    def test_proactive_success_without_mail_or_credentials_in_log(self):
        with patch.object(proactive, "start_logging", side_effect=self.start), \
             patch.object(proactive, "load_prompt"), patch.object(proactive, "load_summary_prompt"), \
             patch.object(proactive, "_load_settings", return_value={"assistant": {"auth_code_env": "CUSTOM_AUTH"}}), \
             patch.dict(os.environ, {"DEEPSEEK_API_KEY": "private-api", "CUSTOM_AUTH": "private-auth"}), \
             patch.object(proactive, "send_deepseek_email", return_value="PRIVATE ID") as send:
            proactive.main()
            send.assert_called_once_with(api_key="private-api", auth_code="private-auth")
        text = (self.directory / "dfy_mail.log").read_text(encoding="utf-8")
        self.assertIn("SMTP 已接受", text)
        for private in ("private-api", "private-auth", "PRIVATE ID"):
            self.assertNotIn(private, text)

    def test_mail_error_safe_and_exit_code_preserved(self):
        with patch.object(mail, "start_logging", side_effect=self.start), \
             patch.object(mail, "_load_settings", side_effect=OSError("PRIVATE ERROR")), \
             patch.object(sys, "argv", ["mail.py", "check"]), redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as result:
                mail.main()
            self.assertEqual(result.exception.code, 1)
        text = (self.directory / "mail.log").read_text(encoding="utf-8")
        self.assertIn("退出码=1", text)
        self.assertNotIn("PRIVATE ERROR", text)

    def test_rotation_and_safe_exception_chain(self):
        path = self.start("dfy_receive.py")
        try:
            try:
                raise ValueError("PRIVATE CAUSE")
            except ValueError as cause:
                raise RuntimeError("PRIVATE ERROR") from cause
        except RuntimeError:
            logging.getLogger("dfy.receive").error("运行失败", exc_info=True)
        text = path.read_text(encoding="utf-8")
        self.assertIn("RuntimeError", text)
        self.assertNotIn("PRIVATE CAUSE", text)
        self.assertNotIn("PRIVATE ERROR", text)
        for line in text.splitlines():
            self.assertIn("entry=dfy_receive.py", line)
        handler = logging.getLogger("dfy").handlers[0]
        handler.doRollover()
        logging.getLogger("dfy.mail").info("轮转完成")
        self.assertEqual(len(list(self.directory.glob("dfy_receive.log.*"))), 1)
        self.assertIn("轮转完成", path.read_text(encoding="utf-8"))
        self.assertEqual(handler.backupCount, 14)

    def test_mail_value_error_does_not_echo_private_message(self):
        with patch.object(mail, "start_logging", side_effect=self.start), \
             patch.object(mail, "_load_settings", side_effect=ValueError("PRIVATE VALUE")), \
             patch.object(sys, "argv", ["mail.py", "check"]), redirect_stderr(io.StringIO()) as terminal:
            with self.assertRaises(SystemExit) as result:
                mail.main()
            self.assertEqual(result.exception.code, 1)
        self.assertNotIn("PRIVATE VALUE", terminal.getvalue())
        self.assertNotIn("PRIVATE VALUE", (self.directory / "mail.log").read_text(encoding="utf-8"))

    def test_close_logging_does_not_close_host_handlers(self):
        root = logging.getLogger()
        handler = logging.StreamHandler(io.StringIO())
        root.addHandler(handler)
        self.addCleanup(root.removeHandler, handler)
        self.start("dfy_test.py")
        mail_logging.close_logging()
        self.assertIn(handler, root.handlers)
        self.assertFalse(logging.getLogger("dfy").handlers)

    def test_import_does_not_configure_logging(self):
        with patch.dict(sys.modules, {"openai": sdk, "mail_logging": mail_logging}), \
             patch.object(mail_logging, "configure_logging") as configure:
            load_module("import_only_llm", "dfy_test.py")
            load_module("import_only_mail", "mail.py")
            configure.assert_not_called()

    def test_default_path_independent_of_current_directory(self):
        old = Path.cwd()
        try:
            os.chdir(self.directory)
            self.assertEqual(mail_logging.LOG_DIR, Path(__file__).resolve().parents[1] / "log" / "output_log")
        finally:
            os.chdir(old)

    def test_log_failure_prevents_chat_start(self):
        with patch.object(llm, "start_logging", side_effect=SystemExit(1)), \
             patch.object(llm, "DeepSeekChat") as factory, patch.object(llm, "getpass") as getpass:
            with self.assertRaises(SystemExit) as result:
                llm.main()
            self.assertEqual(result.exception.code, 1)
            factory.assert_not_called()
            getpass.assert_not_called()

    def test_start_registers_environment_secrets(self):
        configure = mail_logging.configure_logging
        with patch.object(mail_logging, "configure_logging", side_effect=lambda **kwargs: configure(self.directory, **kwargs)), \
             patch.dict(os.environ, {"DEEPSEEK_API_KEY": "private-api", "MAIL_AUTH_CODE": "private-auth"}):
            path = mail_logging.start_logging("dfy_mail.py")
            logging.getLogger("dfy.proactive").info("private-api private-auth")
        text = path.read_text(encoding="utf-8")
        self.assertNotIn("private-api", text)
        self.assertNotIn("private-auth", text)

    def test_log_start_failure_is_safe(self):
        with patch.object(mail_logging, "configure_logging", side_effect=OSError("PRIVATE PATH")):
            with self.assertRaises(SystemExit) as result:
                mail_logging.start_logging("mail.py")
        self.assertNotIn("PRIVATE PATH", str(result.exception))
        self.assertIn("无法创建状态日志", str(result.exception))


if __name__ == "__main__":
    unittest.main()

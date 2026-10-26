"""离线测试：python -B -m unittest discover -s tests -v。不会连接邮箱或 DeepSeek。"""

import importlib.util
import json
import logging
import sys
import tempfile
import unittest
from contextlib import contextmanager
from email.message import EmailMessage
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch


SRC = Path(__file__).resolve().parent.parent / "src"


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, SRC / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mail_logging = load_module("tested_mail_logging", "mail_logging.py")
sys.modules["mail_logging"] = mail_logging
mail_images = load_module("tested_mail_images", "mail_images.py")
sys.modules["mail_images"] = mail_images
vision = load_module("tested_vision", "vision.py")
sys.modules["vision"] = vision
mail = load_module("tested_mail", "mail.py")
# 模拟整个 SDK，即使尚未安装依赖也能运行。
sdk = ModuleType("openai")


class OpenAIError(Exception):
    pass


class APIConnectionError(OpenAIError):
    pass


class APIStatusError(OpenAIError):
    def __init__(self, status_code):
        self.status_code = status_code


sdk.OpenAIError = OpenAIError
sdk.APIConnectionError = APIConnectionError
sdk.APIStatusError = APIStatusError
sdk.OpenAI = MagicMock()
with patch.dict(sys.modules, {"openai": sdk}):
    llm = load_module("tested_llm", "dfy_test.py")
story_module = load_module("tested_story_memory", "story_memory.py")
sys.modules["story_memory"] = story_module
memory_module = load_module("tested_dialog_memory", "dialog_memory.py")
with patch.dict(sys.modules, {"openai": sdk, "dfy_test": llm, "mail": mail, "mail_logging": mail_logging, "dialog_memory": memory_module}):
    receive = load_module("tested_receive", "dfy_receive.py")
    proactive = load_module("tested_proactive", "dfy_mail.py")


SUMMARY = json.dumps({"user_summary": "用户提出问题。", "assistant_summary": "助手给出回答。"}, ensure_ascii=False)


def mock_summary(question):
    request = json.loads(question)
    result = json.loads(SUMMARY)
    if request["user_email"] is None:
        result["user_summary"] = None
    if "mail_subject" in request.get("json_templates", {}).get("output_template", {}):
        result["mail_subject"] = "一起聊聊今天的新发现"
    return json.dumps(result, ensure_ascii=False)


def mock_answer(question, **kwargs):
    return mock_summary(question) if kwargs.get("json_mode") else "回答"


def mock_send(*args, **kwargs):
    return kwargs["message_id"]


def message(uid, identifier=None, body="问题"):
    return {
        "uid": str(uid), "uidvalidity": "10",
        "message_id": identifier if identifier is not None else f"<m{uid}@example.com>",
        "subject": "提问", "references": "<old@example.com>", "body": body,
    }


def fetch_size(raw):
    return "OK", [f"1 (RFC822.SIZE {len(raw)})".encode("ascii")]


def raw_email(uid, sender="User <user@example.com>", headers=None):
    result = EmailMessage()
    result["From"] = sender
    result["Subject"] = "中文问题"
    result["Message-ID"] = f"<m{uid}@example.com>"
    for key, value in (headers or {}).items():
        result[key] = value
    result.set_content("邮件正文")
    return result.as_bytes()


class MailTests(unittest.TestCase):
    def setUp(self):
        self.client = mail.MailClient.__new__(mail.MailClient)
        self.client.assistant = {"email": "assistant@126.com"}
        self.client.user_email = "user@example.com"
        self.connection = MagicMock()
        self.state = {"UIDVALIDITY": b"10", "UIDNEXT": b"101"}
        self.connection.response.side_effect = lambda key: (key, [self.state[key]])
        self.connection.status.return_value = ("OK", [b'"INBOX" ()'])
        self.connection.uid.return_value = ("NO", [])

        @contextmanager
        def fake_imap():
            yield self.connection
        self.client._imap = fake_imap

    def test_baseline_and_empty_range(self):
        self.assertEqual(self.client.get_mailbox_cursor(), (10, 100))
        self.assertEqual(self.client.read_user_emails_since((10, 100)), ((10, 100), []))
        self.connection.uid.assert_not_called()

    def test_missing_uidnext_uses_status(self):
        self.state["UIDNEXT"] = None
        self.connection.status.return_value = ("OK", [b'"INBOX" (UIDNEXT 101 UIDVALIDITY 10)'])
        self.assertEqual(self.client.get_mailbox_cursor(), (10, 100))
        self.connection.status.assert_called_once_with("INBOX", "(UIDVALIDITY UIDNEXT)")
        self.assertEqual(self.client.read_user_emails_since((10, 100)), ((10, 100), []))
        self.connection.uid.assert_not_called()

    def test_status_empty_mailbox(self):
        self.state["UIDNEXT"] = None
        self.connection.status.return_value = ("OK", [b'INBOX (UIDVALIDITY 10 UIDNEXT 1)'])
        self.assertEqual(self.client.get_mailbox_cursor(), (10, 0))

    def test_status_invalid_or_changed_stops(self):
        self.state["UIDNEXT"] = None
        for response in [
            ("NO", [b"unavailable"]),
            ("OK", [b"INBOX (UIDVALIDITY 10)"]),
            ("OK", [b"INBOX (UIDVALIDITY 10 UIDNEXT 0)"]),
            ("OK", [b"INBOX (UIDVALIDITY 11 UIDNEXT 101)"]),
        ]:
            with self.subTest(response=response):
                self.connection.status.return_value = response
                with self.assertRaises(mail.MailboxStateError):
                    self.client.get_mailbox_cursor()

    def test_uid_search_fallback_baseline_without_body_fetch(self):
        self.state["UIDNEXT"] = None
        self.connection.status.return_value = ("OK", [b"INBOX (UIDVALIDITY 10)"])
        self.connection.uid.return_value = ("OK", [b"3 50 100"])
        self.assertEqual(self.client.get_mailbox_cursor(), (10, 100))
        self.connection.uid.assert_called_once_with("search", None, "ALL")

    def test_uid_search_empty_and_deleted_mailbox(self):
        self.state["UIDNEXT"] = None
        for ids, maximum in [(b"", 0), (b"2 50", 50)]:
            with self.subTest(ids=ids):
                self.connection.uid.return_value = ("OK", [ids])
                self.assertEqual(self.client.get_mailbox_cursor(), (10, maximum))
                self.assertEqual(self.client.read_user_emails_since((10, 100)), ((10, 100), []))

    def test_uid_search_fallback_receives_new_mail(self):
        self.state["UIDNEXT"] = None
        self.connection.uid.side_effect = [
            ("OK", [b"50 100 101"]),
            ("OK", [b"100 101"]),
            fetch_size(raw_email(101)),
            ("OK", [(b"body", raw_email(101))]),
        ]
        cursor, messages = self.client.read_user_emails_since((10, 100))
        self.assertEqual(cursor, (10, 101))
        self.assertEqual([item["uid"] for item in messages], ["101"])
        self.assertEqual(self.connection.uid.call_count, 4)

    def test_status_rejected_still_uses_search(self):
        self.state["UIDNEXT"] = None
        self.connection.status.return_value = ("NO", [b"unsupported"])
        self.connection.uid.return_value = ("OK", [b"100"])
        self.assertEqual(self.client.get_mailbox_cursor(), (10, 100))

    def test_search_cannot_bypass_missing_validity(self):
        self.state["UIDNEXT"] = None
        self.state["UIDVALIDITY"] = None
        self.connection.uid.return_value = ("OK", [b"100"])
        with self.assertRaises(mail.MailboxStateError):
            self.client.get_mailbox_cursor()
        self.connection.uid.assert_not_called()

    def test_search_invalid_response_stops(self):
        self.state["UIDNEXT"] = None
        for response in [("NO", []), ("OK", [b"1 invalid"]), ("OK", [None])]:
            with self.subTest(response=response):
                self.connection.uid.return_value = response
                with self.assertRaises(mail.MailboxStateError):
                    self.client.get_mailbox_cursor()

    def test_status_fallback_finds_new_messages(self):
        self.state["UIDNEXT"] = None
        self.connection.status.return_value = ("OK", [b'INBOX (UIDVALIDITY 10 UIDNEXT 102)'])
        self.connection.uid.side_effect = [
            ("OK", [b"101"]), fetch_size(raw_email(101)),
            ("OK", [(b"body", raw_email(101))]),
        ]
        cursor, messages = self.client.read_user_emails_since((10, 100))
        self.assertEqual(cursor, (10, 101))
        self.assertEqual([item["uid"] for item in messages], ["101"])
        self.assertEqual(self.connection.uid.call_count, 3)

    def test_many_messages_and_old_uid_exclusion(self):
        self.state["UIDNEXT"] = b"162"

        def command(name, *args):
            if name == "search":
                self.assertEqual(args, (None, "UID", "101:161", "FROM", '"user@example.com"'))
                return "OK", [b"99 " + b" ".join(str(i).encode() for i in reversed(range(101, 162)))]
            raw = raw_email(int(args[0]))
            if args[1] == "(RFC822.SIZE)":
                return fetch_size(raw)
            self.assertEqual(args[1], "(INTERNALDATE BODY.PEEK[])")
            return "OK", [(b"body", raw), b")"]
        self.connection.uid.side_effect = command
        cursor, messages = self.client.read_user_emails_since((10, 100))
        self.assertEqual(cursor, (10, 161))
        self.assertEqual([int(m["uid"]) for m in messages], list(range(101, 162)))
        self.assertEqual(self.connection.uid.call_count, 1 + 2 * 61)

    def test_automatic_and_sender_filter(self):
        for headers in [
            {"Auto-Submitted": "auto-replied"}, {"Return-Path": "<>"},
            {"List-Id": "<list.example.com>"}, {"Precedence": "bulk"},
        ]:
            self.assertIsNone(self.client._parse_user_email(raw_email(1, headers=headers), b"1", "10", automatic=True))
        self.assertIsNone(self.client._parse_user_email(
            raw_email(1, sender='"user@example.com" <evil@example.com>'), b"1", "10", automatic=True
        ))
        self.assertIsNotNone(self.client._parse_user_email(
            raw_email(1, headers={"Auto-Submitted": "no"}), b"1", "10", automatic=True
        ))

    def test_filter_logs_specific_reason_without_addresses(self):
        for headers, code in [
            ({"Auto-Submitted": "auto-replied"}, "auto_submitted"),
            ({"Return-Path": "<>"}, "empty_return_path"),
            ({"List-Id": "<list.example.com>"}, "list_id"),
            ({"Precedence": "bulk"}, "precedence"),
        ]:
            with self.subTest(code=code), self.assertLogs("dfy.mail", level="INFO") as output:
                self.client._parse_user_email(raw_email(1, headers=headers), b"1", "10", automatic=True)
            self.assertIn(code, output.output[0])
            self.assertNotIn("user@example.com", output.output[0])

    def test_diagnose_fetches_only_headers_and_never_sends(self):
        header = b'From: User <user@example.com>\r\nAuto-Submitted: auto-replied\r\n\r\n'
        self.connection.uid.return_value = ("OK", [(b"header", header)])
        self.client._smtp = MagicMock()
        result = self.client.diagnose_email(1760718099)
        self.assertFalse(result["reply_allowed"])
        self.assertIn("auto_submitted", result["reason"])
        args = self.connection.uid.call_args.args
        self.assertEqual(args[:2], ("fetch", "1760718099"))
        self.assertIn("BODY.PEEK[HEADER.FIELDS", args[2])
        self.client._smtp.assert_not_called()

    def test_chinese_sender_display_name_is_accepted(self):
        result = self.client._parse_user_email(
            raw_email(1, sender="中文昵称 <user@example.com>"), b"1", "10", automatic=True
        )
        self.assertIsNotNone(result)

    def test_malformed_display_name_with_unique_address_is_accepted(self):
        for sender in ["name:tag <user@example.com>", "a..b <user@example.com>", "中文昵称 <user@example.com>"]:
            raw = (f"From: {sender}\r\nSubject: test\r\n\r\nquestion").encode()
            with self.subTest(sender=sender):
                parsed = self.client._parse_user_email(raw, b"1", "10", automatic=True)
                self.assertIsNotNone(parsed)
                self.assertEqual(parsed["from"], "user@example.com")

    def test_fallback_rejects_ambiguous_or_other_addresses(self):
        for sender in [
            "name:tag <evil@example.com>",
            "name:tag <user@example.com>, <evil@example.com>",
            "name:tag <user@example.com> trailing",
            '"unclosed <user@example.com>',
            "user@example.com <evil@example.com>",
            "name <user@@example.com>",
            "name:tag <user@example.com>\r\nFrom: evil@example.com",
        ]:
            raw = (f"From: {sender}\r\n\r\nquestion").encode()
            with self.subTest(sender=sender):
                self.assertIsNone(self.client._parse_user_email(raw, b"1", "10", automatic=True))

    def test_compatible_sender_still_filters_auto_reply(self):
        raw = b"From: name:tag <user@example.com>\r\nAuto-Submitted: auto-replied\r\n\r\nquestion"
        self.assertIsNone(self.client._parse_user_email(raw, b"1", "10", automatic=True))

    def test_empty_plain_text_falls_back_to_html(self):
        original = EmailMessage()
        original["From"] = "user@example.com"
        original.set_content("   ")
        original.add_alternative("<p>请计算 18 加 24。</p>", subtype="html")
        parsed = self.client._parse_user_email(original.as_bytes(), b"1", "10", automatic=True)
        self.assertEqual(parsed["body"], "请计算 18 加 24。")

    def test_filtered_batch_still_advances_cursor(self):
        self.state["UIDNEXT"] = b"102"
        self.connection.uid.side_effect = [
            ("OK", [b"101"]),
            fetch_size(raw_email(101, headers={"Auto-Submitted": "auto-generated"})),
            ("OK", [(b"body", raw_email(101, headers={"Auto-Submitted": "auto-generated"}))]),
        ]
        self.assertEqual(self.client.read_user_emails_since((10, 100)), ((10, 101), []))

    def test_html_and_attachment_parsing(self):
        original = EmailMessage()
        original["From"] = "user@example.com"
        original["Subject"] = "中文主题"
        original.set_content("<p>你好</p><script>hidden</script><div>正文 &amp; 内容</div>", subtype="html")
        original.add_attachment(b"private", maintype="application", subtype="octet-stream", filename="file.bin")
        result = self.client._parse_user_email(original.as_bytes(), b"1", "10", automatic=True)
        self.assertEqual(result["subject"], "中文主题")
        self.assertIn("正文 & 内容", result["body"])
        self.assertNotIn("hidden", result["body"])
        self.assertNotIn("private", result["body"])

    def test_state_changed_or_missing(self):
        self.state["UIDVALIDITY"] = b"11"
        with self.assertRaises(mail.MailboxStateError):
            self.client.read_user_emails_since((10, 100))
        self.state["UIDVALIDITY"] = None
        with self.assertRaises(mail.MailboxStateError):
            self.client.get_mailbox_cursor()

    def test_fetch_failure_does_not_return_cursor(self):
        self.state["UIDNEXT"] = b"102"
        self.connection.uid.side_effect = [("OK", [b"101"]), mail.imaplib.IMAP4.abort("fake")]
        with self.assertRaises(mail.imaplib.IMAP4.abort):
            self.client.read_user_emails_since((10, 100))

    def test_abort_has_command_stage_without_changing_exception_type(self):
        failure = mail.imaplib.IMAP4.abort("socket error: EOF")
        action = MagicMock(side_effect=failure)
        with self.assertRaises(mail.imaplib.IMAP4.abort) as caught:
            mail.MailClient._imap_command("UID SEARCH 查询用户新邮件", action)
        self.assertIs(caught.exception, failure)
        self.assertEqual(caught.exception.mail_stage, "UID SEARCH 查询用户新邮件")

    def test_status_abort_keeps_failure_stage(self):
        self.state["UIDNEXT"] = None
        self.connection.status.side_effect = mail.imaplib.IMAP4.abort("socket error: EOF")
        with self.assertRaises(mail.imaplib.IMAP4.abort) as caught:
            self.client.get_mailbox_cursor()
        self.assertEqual(caught.exception.mail_stage, "STATUS 查询邮箱 UID 状态")
        self.connection.uid.assert_not_called()

    def test_explicit_message_id_preserved_and_injection_rejected(self):
        smtp = MagicMock()
        @contextmanager
        def fake_smtp():
            yield smtp
        self.client._smtp = fake_smtp
        result = self.client.send_email("测试", "正文", message_id="<prepared@dfy.local>")
        self.assertEqual(result, "<prepared@dfy.local>")
        self.assertEqual(str(smtp.send_message.call_args.args[0]["Message-ID"]), result)
        for bad in ["bad", "<a@b> <c@d>", "<a@b>\r\nInjected: evil"]:
            with self.assertRaises(ValueError):
                self.client.send_email("测试", "正文", message_id=bad)

    def test_threads_and_old_send_interface(self):
        smtp = MagicMock()

        @contextmanager
        def fake_smtp():
            yield smtp
        self.client._smtp = fake_smtp
        self.client.send_email("测试", "正文")
        sent = smtp.send_message.call_args.args[0]
        self.assertNotIn("In-Reply-To", sent)
        self.assertNotIn("Auto-Submitted", sent)
        self.client.send_email(
            "Re: 测试", "正文", in_reply_to="<m1@example.com>\r\nInjected: evil",
            references="<old@example.com>", auto_replied=True,
        )
        sent = smtp.send_message.call_args.args[0]
        self.assertEqual(str(sent["In-Reply-To"]), "<m1@example.com>")
        self.assertEqual(str(sent["References"]), "<old@example.com> <m1@example.com>")
        self.assertEqual(str(sent["Auto-Submitted"]), "auto-replied")
        self.assertNotIn("Injected", sent)
        self.assertEqual(smtp.send_message.call_args.kwargs["to_addrs"], ["user@example.com"])


class ReceiverTests(unittest.TestCase):
    def setUp(self):
        self.box, self.bot = MagicMock(), MagicMock()
        self.box.get_mailbox_cursor.return_value = (10, 0)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.memory_path = Path(self.tmp.name) / "dialog.json"
        self.bot.ask.side_effect = mock_answer
        self.box.send_email.side_effect = mock_send
        self.listener = self.make_listener(self.box, self.bot, "回复指令")
        self.listener.start()

    def make_listener(self, box, bot, prompt):
        return receive.MailReceiver(box, bot, prompt, memory_path=self.memory_path, summary_prompt="输出 JSON 梗概")

    def test_actual_body_reaches_sdk_and_generated_answer_is_sent(self):
        original = EmailMessage()
        original["From"] = "user@example.com"
        original["Subject"] = "算术问题"
        original.set_content("请计算 18 加 24，并解释结果。")
        parsed = mail.MailClient._parse_user_email(
            self._parser_client(), original.as_bytes(), b"1", "10", automatic=True
        )
        self.box.read_user_emails_since.return_value = ((10, 1), [parsed])
        bot = llm.DeepSeekChat(api_key="fake-test-key")
        create = bot.client.chat.completions.create
        create.return_value.choices = [MagicMock(finish_reason="stop")]
        first, second = MagicMock(), MagicMock()
        first.choices = [MagicMock(finish_reason="stop")]
        first.choices[0].message.content = "18 加 24 等于 42。"
        second.choices = [MagicMock(finish_reason="stop")]
        second.choices[0].message.content = SUMMARY
        create.side_effect = [first, second]
        self.addCleanup(lambda: setattr(create, "side_effect", None))
        listener = self.make_listener(self.box, bot, "根据用户正文回答")
        listener.start()
        listener.poll_once()
        messages = create.call_args_list[-2].kwargs["messages"]
        self.assertEqual(messages[0], {"role": "system", "content": "根据用户正文回答"})
        self.assertEqual(messages[1]["role"], "user")
        self.assertIn("请计算 18 加 24，并解释结果。", messages[1]["content"])
        self.assertIn("算术问题", messages[1]["content"])
        self.assertEqual(self.box.send_email.call_args.args[1], "18 加 24 等于 42。")

    @staticmethod
    def _parser_client():
        client = mail.MailClient.__new__(mail.MailClient)
        client.assistant = {"email": "assistant@126.com"}
        client.user_email = "user@example.com"
        return client

    def test_json_mode_is_optional_sdk_parameter(self):
        bot = llm.DeepSeekChat(api_key="fake-test-key")
        create = bot.client.chat.completions.create
        create.return_value.choices = [MagicMock(finish_reason="stop")]
        create.return_value.choices[0].message.content = SUMMARY
        bot.ask("输出 JSON", json_mode=True)
        self.assertEqual(create.call_args.kwargs["response_format"], {"type": "json_object"})
        bot.ask("普通问题")
        self.assertNotIn("response_format", create.call_args.kwargs)

    def test_truncated_nonempty_answer_is_rejected(self):
        bot = llm.DeepSeekChat(api_key="fake-test-key")
        create = bot.client.chat.completions.create
        create.return_value.choices = [MagicMock(finish_reason="length")]
        create.return_value.choices[0].message.content = "未完成的回答"
        with self.assertRaises(ValueError):
            bot.ask("问题")

    def test_old_single_question_call_remains_compatible(self):
        bot = llm.DeepSeekChat(api_key="fake-test-key")
        create = bot.client.chat.completions.create
        create.return_value.choices = [MagicMock(finish_reason="stop")]
        create.return_value.choices[0].message.content = "你好"
        self.assertEqual(bot.ask("你好"), "你好")
        self.assertEqual(create.call_args.kwargs["messages"], [{"role": "user", "content": "你好"}])

    def test_interval_and_subject(self):
        self.assertEqual(receive.interval_seconds({"check_interval_minutes": 1}), 60)
        for value in [0, -1, True, "1", float("inf"), float("nan")]:
            with self.assertRaises(ValueError):
                receive.interval_seconds({"check_interval_minutes": value})
        self.assertEqual(receive.reply_subject("你好"), "Re: 你好")
        self.assertEqual(receive.reply_subject("Re: 你好"), "Re: 你好")

    def test_queue_dedup_and_no_id(self):
        self.bot.ask.assert_not_called()
        self.box.send_email.assert_not_called()
        self.box.read_user_emails_since.side_effect = [
            ((10, 3), [message(1), message(2, "<m1@example.com>"), message(3, body=" ")]),
            ((10, 3), [message(1)]), ((10, 5), [message(4, ""), message(5, "")]),
        ]
        self.listener.poll_once()
        self.assertEqual(self.box.send_email.call_count, 1)
        self.listener.poll_once()
        self.assertEqual(self.box.send_email.call_count, 1)
        self.listener.poll_once()
        self.assertEqual(self.box.send_email.call_count, 3)
        self.assertFalse(self.listener.pending)
        self.assertTrue(any("问题" in call.args[0] for call in self.bot.ask.call_args_list))

    def test_api_failure_retains_pending(self):
        self.box.read_user_emails_since.side_effect = [
            ((10, 2), [message(1), message(2)]), ((10, 3), [message(2), message(3)]),
        ]
        self.bot.ask.side_effect = ["answer1", SUMMARY, APIConnectionError(), "answer2", SUMMARY, "answer3", SUMMARY]
        self.listener.poll_once()
        self.assertEqual(self.box.send_email.call_count, 1)
        self.assertEqual(len(self.listener.pending), 1)
        self.listener.poll_once()
        self.assertEqual(self.box.send_email.call_count, 3)
        self.assertFalse(self.listener.pending)

    def test_api_permanent_and_retry_limit(self):
        self.box.read_user_emails_since.return_value = ((10, 1), [message(1)])
        self.bot.ask.side_effect = APIStatusError(401)
        with self.assertRaises(RuntimeError):
            self.listener.poll_once()
        self.box.send_email.assert_not_called()
        self.bot.ask.side_effect = APIConnectionError()
        self.listener.pending[0]["attempts"] = 0
        self.listener.poll_once()
        self.listener.poll_once()
        with self.assertRaises(RuntimeError):
            self.listener.poll_once()

    def test_missing_prompt_rejected_and_default_path(self):
        self.assertEqual(receive.PROMPT_PATH, SRC.parent / "config" / "prompt.json")
        with patch("pathlib.Path.open", unittest.mock.mock_open(read_data='{"reply_task": ["回答来信"]}')):
            with self.assertRaises(ValueError):
                receive.load_reply_prompt()

    def test_mailbox_state_error_propagates(self):
        self.box.read_user_emails_since.side_effect = mail.MailboxStateError("changed")
        with self.assertRaises(mail.MailboxStateError):
            self.listener.poll_once()
        self.box.send_email.assert_not_called()

    def test_receive_disconnect_retains_cursor(self):
        self.box.read_user_emails_since.side_effect = mail.imaplib.IMAP4.abort("fake disconnect")
        self.box.describe_error.side_effect = lambda exc: str(exc)
        with self.assertLogs("dfy.receive", level="INFO") as output:
            self.listener.poll_once()
        self.assertEqual(self.listener.cursor, (10, 0))
        self.box.send_email.assert_not_called()
        self.assertTrue(any("abort" in line for line in output.output))
        self.assertTrue(all("fake disconnect" not in record.getMessage() for record in output.records))

    def test_recovers_next_poll_without_resetting_baseline(self):
        self.box.describe_error.side_effect = lambda exc: str(exc)
        self.box.read_user_emails_since.side_effect = [
            mail.imaplib.IMAP4.abort("socket EOF"), ((10, 1), [message(1)]),
        ]
        with patch("builtins.print"):
            self.listener.poll_once()
            self.listener.poll_once()
        self.assertEqual(self.box.get_mailbox_cursor.call_count, 1)
        self.assertEqual([call.args[0] for call in self.box.read_user_emails_since.call_args_list], [(10, 0), (10, 0)])
        self.box.send_email.assert_called_once()

    def test_receive_logs_healthy_empty_poll(self):
        self.box.read_user_emails_since.return_value = ((10, 0), [])
        with self.assertLogs("dfy.receive", level="INFO") as output:
            self.listener.poll_once()
        self.assertTrue(any("未发现" in line for line in output.output))

    def test_diagnostic_redacts_credentials_and_email(self):
        self.client = mail.MailClient.__new__(mail.MailClient)
        self.client.assistant = {"email": "assistant@126.com"}
        self.client.user_email = "user@example.com"
        self.client._auth_code = "private-code"
        exc = mail.imaplib.IMAP4.abort("LOGIN private-code assistant@126.com failed\r\nserver BYE")
        text = self.client.describe_error(exc)
        self.assertNotIn("private-code", text)
        self.assertNotIn("assistant@126.com", text)
        self.assertNotIn("\n", text)
        self.assertIn("BYE", text)

    def test_smtp_exception_stops_without_retry(self):
        self.box.read_user_emails_since.return_value = ((10, 1), [message(1)])
        self.box.send_email.side_effect = mail.smtplib.SMTPServerDisconnected("fake")
        with self.assertRaisesRegex(RuntimeError, "不确定"):
            self.listener.poll_once()
        self.assertEqual(self.box.send_email.call_count, 1)
        self.assertEqual(len(self.listener.pending), 1)

    def test_period_skips_missed_ticks(self):
        listener = self.make_listener(MagicMock(), MagicMock(), "reply")
        with (
            patch.object(listener, "start"),
            patch.object(listener, "poll_once", side_effect=[None, KeyboardInterrupt]),
            patch.object(receive.time, "monotonic", side_effect=[0, 0, 130, 130]),
            patch.object(receive.time, "sleep") as sleep,
        ):
            with self.assertRaises(KeyboardInterrupt):
                listener.run(60)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [60, 50])


class LoggingTests(unittest.TestCase):
    def tearDown(self):
        logger = logging.getLogger("dfy")
        for handler in logger.handlers[:]:
            logger.removeHandler(handler)
            handler.close()

    def test_file_logging_redaction_and_rotation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = mail_logging.configure_logging(tmp, secrets=("private-auth", "private-api"))
            logger = logging.getLogger("dfy.receive")
            logger.info("测试日志 private-auth private-api user@example.com sk-fakeKey")
            try:
                raise RuntimeError("PRIVATE MAIL BODY")
            except RuntimeError:
                logger.error("模拟未预期异常", exc_info=True)
            content = path.read_text(encoding="utf-8")
            self.assertIn("测试日志", content)
            self.assertIn("pid=", content)
            self.assertIn("RuntimeError", content)
            for secret in ("private-auth", "private-api", "user@example.com", "sk-fakeKey", "PRIVATE MAIL BODY"):
                self.assertNotIn(secret, content)
            handler = logging.getLogger("dfy").handlers[0]
            self.assertEqual(handler.backupCount, 14)
            handler.doRollover()
            logger.info("轮转之后")
            self.assertIn("轮转之后", path.read_text(encoding="utf-8"))
            self.assertEqual(len(list(Path(tmp).glob("mail.log.*"))), 1)
            # 重复初始化不能重复输出或占用旧文件。
            mail_logging.configure_logging(tmp)
            self.assertEqual(len(logging.getLogger("dfy").handlers), 2)
            self.tearDown()

    def test_cached_exception_cannot_bypass_safe_formatter(self):
        try:
            raise RuntimeError("PRIVATE ERROR BODY")
        except RuntimeError:
            record = logging.LogRecord("dfy", logging.ERROR, __file__, 1, "safe event", (), sys.exc_info())
        logging.Formatter().format(record)
        self.assertIn("PRIVATE ERROR BODY", record.exc_text)
        formatted = mail_logging.SafeFormatter().format(record)
        self.assertNotIn("PRIVATE ERROR BODY", formatted)
        self.assertIn("RuntimeError", formatted)

    def test_listener_events_saved_without_mail_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = mail_logging.configure_logging(tmp)
            box, bot = MagicMock(), MagicMock()
            box.get_mailbox_cursor.return_value = (10, 0)
            box.read_user_emails_since.return_value = ((10, 1), [message(1, body="PRIVATE BODY")])
            bot.ask.side_effect = lambda question, **kw: mock_summary(question) if kw.get("json_mode") else "PRIVATE ANSWER"
            box.send_email.side_effect = mock_send
            listener = receive.MailReceiver(box, bot, "PRIVATE PROMPT", memory_path=Path(tmp) / "dialog.json", summary_prompt="JSON summary")
            listener.start()
            listener.poll_once()
            content = path.read_text(encoding="utf-8")
            self.assertIn("基线建立完成", content)
            self.assertIn("SMTP 已接受回复", content)
            for private in ("PRIVATE BODY", "PRIVATE ANSWER", "PRIVATE PROMPT", "用户提出问题。", "助手给出回答。"):
                self.assertNotIn(private, content)
            self.tearDown()


if __name__ == "__main__":
    unittest.main()

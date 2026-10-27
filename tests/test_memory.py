"""对话记忆测试：只用临时文件和模拟模型/邮箱，不读取真实记忆或联网。"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from test_receive import (
    SUMMARY, fetch_size, message, memory_module as memory, mail, mock_answer, mock_send, proactive, receive,
)


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "dialog.json"
        self.store = memory.DialogMemory(self.path)

    def record(self, incoming=None):
        summaries = {"user_summary": "用户提问。" if incoming else None, "assistant_summary": "助手回答。"}
        return memory.new_record(self.store.snapshot(), incoming, summaries)

    def sent_history(self, identifier, sent_at):
        r = self.record()
        r.update(status="sent", sent_at=sent_at, assistant_message_id=identifier)
        return r

    def test_missing_empty_and_invalid_file(self):
        self.assertEqual(self.store.snapshot()["dialogs"], [])
        self.path.write_text("", encoding="utf-8")
        self.assertEqual(self.store.snapshot()["revision"], 0)
        self.path.write_text("broken private json", encoding="utf-8")
        with self.assertRaises(memory.MemoryError):
            self.store.snapshot()
        self.assertEqual(self.path.read_text(encoding="utf-8"), "broken private json")

    def test_classification_strict_latest_before_receipt(self):
        records = [self.sent_history("<old@example.com>", "2026-10-25T00:00:00+00:00"),
                   self.sent_history("<latest@example.com>", "2026-10-25T00:10:00+00:00"),
                   self.sent_history("<future@example.com>", "2026-10-25T00:30:00+00:00")]
        incoming = message(1)
        incoming["received_at"] = "2026-10-25T00:20:00+00:00"
        for parent, references, expected in [
            ("<latest@example.com>", "", "用户回复上一封邮件"),
            ("<old@example.com>", "<latest@example.com>", "用户主动发送"),
            (None, "<latest@example.com> <old@example.com>", "用户主动发送"),
            (None, "<latest@example.com> <old@example.com> <latest@example.com>", "用户回复上一封邮件"),
            ("invalid", "<latest@example.com>", "用户主动发送"),
            ("<latest@example.com> <old@example.com>", "", "用户主动发送"),
            (None, "", "用户主动发送"),
        ]:
            with self.subTest(parent=parent, references=references):
                incoming.update(in_reply_to=parent, references=references)
                self.assertEqual(memory.classify_dialog(records, incoming), expected)
        self.assertEqual(memory.classify_dialog(records, None), "deepseek自己主动发送")

    def test_second_precision_receipt_does_not_exclude_same_second_parent(self):
        records = [self.sent_history("<same@example.com>", "2026-10-25T00:10:00.500000+00:00"),
                   self.sent_history("<next@example.com>", "2026-10-25T00:10:01+00:00")]
        incoming = message(1)
        incoming.update(received_at="2026-10-25T00:10:00+00:00", received_at_source="imap_internaldate", in_reply_to="<same@example.com>")
        self.assertEqual(memory.classify_dialog(records, incoming), "用户回复上一封邮件")
        incoming["received_at_source"] = "observed_at"
        self.assertEqual(memory.classify_dialog(records, incoming), "用户主动发送")

    def test_prepared_unknown_not_used_as_history(self):
        snapshot = self.store.snapshot()
        record = self.record()
        self.store.prepare(record, snapshot["revision"])
        with self.assertRaises(memory.MemoryError):
            self.store.snapshot()
        self.store.update(record["id"], "sending")
        self.store.update(record["id"], "unknown")
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertIsNone(data["dialogs"][0]["sent_at"])
        self.assertEqual(data["dialogs"][0]["status"], "unknown")

    def test_invalid_new_record_cannot_corrupt_memory(self):
        record = self.record()
        record["assistant_summary"] = ""
        with self.assertRaises(memory.MemoryError):
            self.store.prepare(record, 0)
        self.assertEqual(self.store.snapshot()["dialogs"], [])

    def test_atomic_replace_failure_keeps_old_file(self):
        self.path.write_text(json.dumps(self.store.snapshot()), encoding="utf-8")
        original = self.path.read_bytes()
        with patch.object(memory.os, "replace", side_effect=OSError("fake")):
            with self.assertRaises(memory.MemoryError):
                self.store.prepare(self.record(), 0)
        self.assertEqual(self.path.read_bytes(), original)

    def test_revision_and_lock_prevent_conflicting_writes(self):
        record = self.record()
        with self.store._lock():
            with self.assertRaises(memory.MemoryError):
                memory.DialogMemory(self.path).snapshot()
        self.store.prepare(record, 0)
        self.store.update(record["id"], "sending")
        self.store.update(record["id"], "sent", sent_at=memory.now_iso())
        other = self.record()
        with self.assertRaises(memory.MemoryError):
            self.store.prepare(other, 0)
        self.assertEqual(len(self.store.snapshot()["dialogs"]), 1)

    def test_history_budget_redaction_and_all_records(self):
        history = [self.sent_history(f"<m{i}@example.com>", "2026-10-25T00:00:00+00:00") for i in range(3)]
        history[0]["assistant_summary"] = "private-key preference"
        store = memory.DialogMemory(self.path, secrets=("private-key",))
        data = {"dialogs": history, "revision": 0}
        question = store.question(data, "当前正文")
        self.assertIn("当前正文", question)
        self.assertEqual(question.count('"assistant_summary"'), 3)
        self.assertNotIn("private-key", question)
        with patch.object(memory, "CONTEXT_LIMIT", 1):
            with self.assertRaises(memory.MemoryError):
                store.question(data, "当前正文")
        self.assertEqual(len(data["dialogs"]), 3)

    def test_summary_validation_and_proactive_null(self):
        bot = MagicMock()
        bot.ask.return_value = SUMMARY
        result = memory.summarize(bot, "JSON summary", "问题", "回答")
        self.assertEqual(result["user_summary"], "用户提出问题。")
        self.assertTrue(bot.ask.call_args.kwargs["json_mode"])
        for bad in ["invalid", "[]", '{"user_summary": "u"}',
                    json.dumps({"user_summary": "u", "assistant_summary": "a\nb"})]:
            bot.ask.return_value = bad
            with self.assertRaises(memory.SummaryError):
                memory.summarize(bot, "JSON summary", "问题", "回答")
        bot.ask.return_value = SUMMARY
        with self.assertRaises(memory.SummaryError):
            memory.summarize(bot, "JSON summary", None, "回答")
        bot.ask.return_value = json.dumps({"user_summary": None, "assistant_summary": "主动问候。"})
        self.assertIsNone(memory.summarize(bot, "JSON summary", None, "回答")["user_summary"])

    def test_success_records_only_after_smtp_acceptance(self):
        snapshot, record = self.store.snapshot(), self.record()
        box = MagicMock()
        def send(*args, **kwargs):
            on_disk = json.loads(self.path.read_text(encoding="utf-8"))["dialogs"][0]
            self.assertEqual(on_disk["status"], "sending")
            self.assertIsNone(on_disk["sent_at"])
            return kwargs["message_id"]
        box.send_email.side_effect = send
        result = memory.send_recorded(self.store, snapshot, record, box, "主题", "正文")
        saved = self.store.snapshot()["dialogs"][0]
        self.assertEqual(result, saved["assistant_message_id"])
        self.assertIsNotNone(saved["sent_at"])
        self.assertIsNone(saved["received_at"])
        box.send_email.assert_called_once()

    def test_smtp_unknown_and_sent_save_failure_not_retried(self):
        snapshot, record = self.store.snapshot(), self.record()
        box = MagicMock()
        box.send_email.side_effect = OSError("fake disconnect")
        with self.assertRaises(memory.MemoryError):
            memory.send_recorded(self.store, snapshot, record, box, "主题", "正文")
        box.send_email.assert_called_once()
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["dialogs"][0]["status"], "unknown")
        with self.assertRaises(memory.MemoryError):
            self.store.snapshot()

    def test_internaldate_and_observation_source(self):
        raw, received = mail._fetch_email_data([
            (b'1 (UID 1 INTERNALDATE "25-Oct-2026 08:00:00 +0800" BODY[] {10}', b"body")
        ])
        self.assertEqual(raw, b"body")
        self.assertEqual(received, "2026-10-25T00:00:00+00:00")
        box = mail.MailClient.__new__(mail.MailClient)
        box.assistant = {"email": "assistant@126.com"}
        box.user_email = "user@example.com"
        data = box._parse_user_email(b'From: user@example.com\r\nDate: bad\r\nIn-Reply-To: <x@example.com>\r\n\r\nbody', b"1", "10")
        self.assertEqual(data["received_at_source"], "observed_at")
        self.assertEqual(data["in_reply_to"], "<x@example.com>")
        self.assertIsNotNone(data["received_at"])


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "dialog.json"
        self.store = memory.DialogMemory(self.path)
        self.box, self.bot = MagicMock(), MagicMock()
        self.box.get_mailbox_cursor.return_value = (10, 0)
        self.box.send_email.side_effect = mock_send
        self.bot.ask.side_effect = mock_answer
        self.now = 1000.0

    def listener(self):
        result = receive.MailReceiver(self.box, self.bot, "根据正文回答", memory_path=self.path, summary_prompt="JSON summary", clock=lambda: self.now)
        result.start()
        return result

    def test_real_fetch_metadata_round_trip_can_be_saved(self):
        self.box.read_user_emails_since.return_value = ((10, 1), [])
        parser = mail.MailClient.__new__(mail.MailClient)
        parser.assistant = {"email": "assistant@126.com"}
        parser.user_email = "user@example.com"
        connection = MagicMock()
        connection.response.side_effect = lambda key: (key, [b"10" if key == "UIDVALIDITY" else b"2"])
        connection.uid.side_effect = [
            ("OK", [b"1"]),
            fetch_size(b"From: user@example.com\r\n\r\nquestion"),
            ("OK", [(b'1 (INTERNALDATE "25-Oct-2026 08:00:00 +0800" BODY[] {50}', b"From: user@example.com\r\n\r\nquestion")]),
        ]
        from contextlib import contextmanager
        @contextmanager
        def imap():
            yield connection
        parser._imap = imap
        cursor, batch = parser.read_user_emails_since((10, 0))
        self.assertEqual(batch[0]["received_at_source"], "imap_internaldate")
        self.box.read_user_emails_since.return_value = (cursor, batch)
        listener = self.listener()
        listener.poll_once()
        saved = self.store.snapshot()["dialogs"][0]
        self.assertEqual(saved["received_at_source"], "imap_internaldate")

    def test_second_reply_gets_first_summary_and_current_body(self):
        self.box.read_user_emails_since.side_effect = [((10, 1), [message(1)]), ((10, 2), [message(2, body="新的问题")])]
        listener = self.listener()
        listener.poll_once()
        listener.poll_once()
        calls = [c for c in self.bot.ask.call_args_list if not c.kwargs.get("json_mode")]
        self.assertIn("用户提出问题。", calls[1].args[0])
        self.assertIn("新的问题", calls[1].args[0])
        self.assertEqual(calls[1].kwargs["system_prompt"], "根据正文回答")
        self.assertEqual(len(self.store.snapshot()["dialogs"]), 2)

    def test_summary_retry_reuses_generated_body(self):
        self.box.read_user_emails_since.return_value = ((10, 1), [message(1)])
        self.bot.ask.side_effect = ["回答", "not-json", SUMMARY]
        listener = self.listener()
        listener.poll_once()
        self.box.send_email.assert_not_called()
        listener.poll_once()
        self.assertEqual(self.bot.ask.call_count, 3)
        self.box.send_email.assert_called_once()

    def test_summary_network_retry_restores_cached_body_and_deadline_after_restart(self):
        from test_receive import APIConnectionError
        self.box.read_user_emails_since.return_value = ((10, 1), [message(1)])
        self.bot.ask.side_effect = ["CACHED ANSWER", APIConnectionError(), SUMMARY]
        listener = self.listener()
        listener.poll_once()
        task_id = listener.pending[0]["task_id"]
        row = listener.store.get(task_id)
        self.assertEqual(row["status"], "retry")
        self.assertEqual(row["payload"]["body"], "CACHED ANSWER")
        self.assertEqual(row["next_attempt_at"], self.now + 2)
        restarted = self.listener()
        self.assertEqual(len(restarted.pending), 1)
        self.assertEqual(restarted.pending[0]["task_id"], task_id)
        restarted.poll_once()
        self.assertEqual(self.bot.ask.call_count, 2)
        self.box.send_email.assert_not_called()
        self.now += 2
        restarted.poll_once()
        self.assertEqual(self.bot.ask.call_count, 3)
        self.assertTrue(self.bot.ask.call_args.kwargs["json_mode"])
        self.assertEqual(self.box.send_email.call_args.args[1], "CACHED ANSWER")
        self.box.send_email.assert_called_once()
        self.assertEqual(restarted.store.get(task_id)["status"], "done")

    def test_confirmation_failure_prevents_second_poll_send(self):
        self.box.read_user_emails_since.return_value = ((10, 1), [message(1)])
        listener = self.listener()
        original = listener.memory.update
        def update(identifier, status, **kwargs):
            if status == "sent":
                raise memory.MemoryError("fake disk failure")
            return original(identifier, status, **kwargs)
        with patch.object(listener.memory, "update", side_effect=update):
            self.assertFalse(listener.poll_once())
        rows = listener.store.list_tasks(statuses=("blocked_unknown",), kind="reply")
        self.assertEqual(len(rows), 1)
        self.assertEqual(listener.blocked_reason, "send_or_projection_unconfirmed")
        self.assertFalse(listener.poll_once())
        self.now += 86400
        self.assertFalse(listener.poll_once())
        self.box.send_email.assert_called_once()
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["dialogs"][0]["status"], "sending")

    def test_rate_limit_retains_pending_for_next_cycle(self):
        from test_receive import APIStatusError
        self.box.read_user_emails_since.return_value = ((10, 1), [message(1)])
        self.bot.ask.side_effect = [APIStatusError(429), "回答", SUMMARY]
        listener = self.listener()
        listener.poll_once()
        self.box.send_email.assert_not_called()
        self.assertEqual(len(listener.pending), 1)
        listener.poll_once()
        self.bot.ask.assert_called_once()
        self.assertEqual(listener.pending[0]["next_attempt_at"], self.now + 2)
        self.now += 2
        listener.poll_once()
        self.box.send_email.assert_called_once()

    def test_pending_model_retry_continues_during_imap_outage(self):
        from test_receive import APIConnectionError
        self.box.read_user_emails_since.side_effect = [
            ((10, 1), [message(1)]), mail.imaplib.IMAP4.abort("fake outage"),
        ]
        self.box.describe_error.return_value = "fake outage"
        self.bot.ask.side_effect = [APIConnectionError(), "回答", SUMMARY]
        listener = self.listener()
        listener.poll_once()
        self.now += 2
        self.assertFalse(listener.poll_once())
        self.box.send_email.assert_called_once()
        self.assertEqual(listener.cursor, (10, 1))

    def test_bad_summary_never_sends_and_quarantines_after_three(self):
        self.box.read_user_emails_since.return_value = ((10, 1), [message(1)])
        self.bot.ask.side_effect = ["回答", "bad", "bad", "bad"]
        listener = self.listener()
        listener.poll_once()
        listener.poll_once()
        listener.poll_once()
        rows = listener.store.list_tasks(statuses=("quarantined",), kind="reply")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["payload"]["invalid_failures"], 3)
        self.assertEqual(rows[0]["payload"]["body"], "回答")
        self.assertFalse(listener.pending)
        listener.poll_once()
        self.assertEqual(self.bot.ask.call_count, 4)
        self.box.send_email.assert_not_called()
        self.assertEqual(self.store.snapshot()["dialogs"], [])

    def test_user_reply_to_latest_is_saved(self):
        snapshot = self.store.snapshot()
        prior = memory.new_record(snapshot, None, {"user_summary": None, "assistant_summary": "助手问候。"})
        memory.send_recorded(self.store, snapshot, prior, self.box, "主题", "正文")
        incoming = message(1)
        incoming.update(received_at=memory.now_iso(), received_at_source="imap_internaldate", in_reply_to=prior["assistant_message_id"])
        self.box.read_user_emails_since.return_value = ((10, 1), [incoming])
        listener = self.listener()
        listener.poll_once()
        saved = self.store.snapshot()["dialogs"][-1]
        self.assertEqual(saved["type"], "用户回复上一封邮件")
        self.assertEqual(saved["received_at"], incoming["received_at"])
        self.assertIsNotNone(saved["sent_at"])

    def test_proactive_reads_memory_and_records_null_user(self):
        config = Path(self.tmp.name) / "prompt.json"
        config.write_text(json.dumps({"subject": "主动来信", "prompt": "写信任务", "summary_prompt": "JSON summary"}), encoding="utf-8")
        snapshot = self.store.snapshot()
        prior = memory.new_record(snapshot, None, {"user_summary": None, "assistant_summary": "之前谈到运动。"})
        memory.send_recorded(self.store, snapshot, prior, self.box, "主题", "正文")
        self.bot.ask.side_effect = ["基于历史的新邮件", json.dumps({"user_summary": None, "assistant_summary": "再次问候。", "mail_subject": "再聊聊最近的运动计划"})]
        self.box._auth_code = "fake-auth"
        with patch.object(proactive, "MailClient", return_value=self.box), patch.object(proactive, "DeepSeekChat", return_value=self.bot):
            result = proactive.send_deepseek_email("fake-key", "fake-auth", config, memory_path=self.path)
        self.assertIn("之前谈到运动。", self.bot.ask.call_args_list[0].args[0])
        request = self.bot.ask.call_args_list[0].args[0]
        payload = json.loads(request[request.index("{"):])
        self.assertEqual(payload["conversation_state"]["mode"], "manual_proactive")
        self.assertEqual(payload["conversation_state"]["after_latest_assistant"]["contact"], "unknown")
        saved = self.store.snapshot()["dialogs"][-1]
        self.assertEqual(saved["type"], "deepseek自己主动发送")
        self.assertIsNone(saved["received_at"])
        self.assertIsNone(saved["user_summary"])
        self.assertEqual(result, saved["assistant_message_id"])
        self.assertEqual(self.box.send_email.call_args.args[0], "再聊聊最近的运动计划")
        self.assertEqual(saved["subject"], "再聊聊最近的运动计划")


if __name__ == "__main__":
    unittest.main()

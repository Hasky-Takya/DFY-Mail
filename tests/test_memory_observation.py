"""收信和主动入口的记忆观测接线；只使用临时记忆和模拟邮箱。"""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from test_receive import mail, memory_module, message, mock_send, mock_summary, receive
from test_clock import clock


def payload(question):
    return json.loads(question[question.index("{"):])


class ObservationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.box = MagicMock()
        self.box.get_mailbox_cursor.return_value = (10, 0)
        self.box.send_email.side_effect = mock_send
        self.bot = MagicMock()
        self.bot.ask.side_effect = lambda question, **kwargs: mock_summary(question) if kwargs.get("json_mode") else "answer"
        self.now = 1000.0
        self.receiver = receive.MailReceiver(self.box, self.bot, "reply", memory_path=Path(self.tmp.name) / "dialog.json", summary_prompt="summary", clock=lambda: self.now)

    def test_current_and_batch_activities_provided_without_other_body(self):
        self.receiver.start()
        first, second = message(1, body="CURRENT BODY"), message(2, body="OTHER PRIVATE BODY")
        self.box.read_user_emails_since.return_value = ((10, 2), [first, second])
        self.receiver.poll_once()
        request = self.bot.ask.call_args_list[0].args[0]
        data = payload(request)
        self.assertEqual(data["conversation_state"]["mode"], "reply")
        self.assertIsNotNone(data["conversation_state"]["current_event"])
        self.assertIn("CURRENT BODY", request)
        self.assertNotIn("OTHER PRIVATE BODY", request)
        self.assertTrue(self.receiver.observation["sync_ok"])
        self.assertEqual(len(self.receiver.observed_user_activity), 1)
        for event in self.receiver.observed_user_activity:
            self.assertNotIn("body", event)
            self.assertNotIn("subject", event)
        self.box.read_user_emails_since.return_value = ((10, 2), [first, second])
        self.receiver.poll_once()
        self.assertEqual(len(self.receiver.observed_user_activity), 1)

    def test_empty_user_mail_counts_contact_but_not_clock_reset(self):
        callback = MagicMock()
        self.receiver.on_user_message = callback
        self.receiver.start()
        self.box.read_user_emails_since.return_value = ((10, 1), [message(1, body=" ")])
        self.receiver.poll_once()
        self.assertEqual(len(self.receiver.observed_user_activity), 1)
        callback.assert_not_called()
        self.bot.ask.assert_not_called()

    def test_prune_preserves_empty_mail_reply_evidence(self):
        store = self.receiver.memory
        record = memory_module.new_record(store.snapshot(), None, {"user_summary": None, "assistant_summary": "sent"})
        record.update(status="sent", sent_at="2026-10-25T10:00:00+00:00")
        store._write({"schema_version": 1, "revision": 1, "dialogs": [record]})
        self.receiver.start()
        reply, later = message(1, body=" "), message(2, body=" ")
        reply.update(received_at="2026-10-25T11:00:00+00:00", received_at_source="imap_internaldate", in_reply_to=record["assistant_message_id"])
        later.update(received_at="2026-10-25T11:10:00+00:00", received_at_source="imap_internaldate")
        self.box.read_user_emails_since.return_value = ((10, 2), [reply, later])
        self.receiver.poll_once()
        self.assertEqual(len(self.receiver.observed_user_activity), 2)
        state = memory_module.conversation_state(store.snapshot()["dialogs"], observed_user_activity=self.receiver.observed_user_activity, as_of="2026-10-25T12:00:00+00:00")
        self.assertEqual(state["after_latest_assistant"]["reply_relation"], "确证")
        self.bot.ask.assert_not_called()

    def test_many_empty_messages_do_not_accumulate_duplicate_context(self):
        self.receiver.start()
        items = [message(index, body=" ") for index in range(1, 401)]
        for index, item in enumerate(items, 1):
            item.update(received_at=f"2026-10-25T10:{(index // 60) % 60:02d}:{index % 60:02d}+00:00", received_at_source="imap_internaldate")
        self.box.read_user_emails_since.return_value = ((10, 400), items)
        self.receiver.poll_once()
        self.assertEqual(len(self.receiver.observed_user_activity), 1)
        self.assertEqual(self.receiver.observed_user_activity[0]["uid"], "400")
        self.assertFalse(self.receiver.pending)
        self.bot.ask.assert_not_called()

    def test_observation_metadata_is_deduplicated_without_body(self):
        first = {"uid": "1", "uidvalidity": "10", "message_id": "<same@example.com>", "body": "PRIVATE BODY", "subject": "PRIVATE SUBJECT"}
        second = dict(first, uid="2")
        self.receiver.observe_user_activity(first)
        self.receiver.observe_user_activity(first)
        self.receiver.observe_user_activity(second)
        self.assertEqual(len(self.receiver.observed_user_activity), 1)
        self.assertNotIn("PRIVATE BODY", str(self.receiver.observed_user_activity))
        self.assertNotIn("PRIVATE SUBJECT", str(self.receiver.observed_user_activity))

    def test_reply_to_old_mail_is_contact_after_latest_not_reply_to_latest(self):
        store = self.receiver.memory
        snapshot = store.snapshot()
        old = memory_module.new_record(snapshot, None, {"user_summary": None, "assistant_summary": "old"})
        old.update(status="sent", sent_at="2026-10-25T10:00:00+00:00")
        latest = memory_module.new_record(snapshot, None, {"user_summary": None, "assistant_summary": "latest"})
        latest.update(status="sent", sent_at="2026-10-25T11:00:00+00:00")
        store._write({"schema_version": 1, "revision": 2, "dialogs": [old, latest]})
        incoming = message(1)
        incoming.update(received_at="2026-10-25T11:30:00+00:00", received_at_source="imap_internaldate", in_reply_to=old["assistant_message_id"])
        self.receiver.start()
        self.box.read_user_emails_since.return_value = ((10, 1), [incoming])
        with patch.object(receive, "now_iso", return_value="2026-10-25T12:00:00+00:00"):
            self.receiver.poll_once()
        state = payload(self.bot.ask.call_args_list[0].args[0])["conversation_state"]
        self.assertEqual(state["after_latest_assistant"]["contact"], "observed")
        self.assertEqual(state["after_latest_assistant"]["reply_relation"], "未知")
        self.assertEqual(state["current_event"]["reply_to_assistant_message_id"], old["assistant_message_id"])
        self.assertEqual(state["current_event"]["type"], "用户主动发送")
        self.assertEqual(store.snapshot()["dialogs"][-1]["reply_to_assistant_message_id"], old["assistant_message_id"])

    def test_failed_sync_marks_observation_stale(self):
        self.receiver.start()
        self.box.read_user_emails_since.return_value = ((10, 0), [])
        self.receiver.poll_once()
        watermark = self.receiver.observation["coverage_through"]
        self.box.read_user_emails_since.side_effect = mail.imaplib.IMAP4.abort("private")
        self.assertFalse(self.receiver.poll_once())
        self.assertFalse(self.receiver.observation["sync_ok"])
        self.assertEqual(self.receiver.observation["coverage_through"], watermark)
        self.assertEqual(self.receiver.sync_retry_at, self.now + 2)
        calls = self.box.read_user_emails_since.call_count
        self.assertFalse(self.receiver.poll_once())
        self.assertEqual(self.box.read_user_emails_since.call_count, calls)
        self.assertFalse(self.receiver.observation["sync_ok"])
        self.assertEqual(self.receiver.observation["coverage_through"], watermark)
        self.box.read_user_emails_since.side_effect = None
        self.now += 2
        self.assertTrue(self.receiver.poll_once())
        self.assertTrue(self.receiver.observation["sync_ok"])
        self.assertEqual(self.receiver.sync_retry_at, 0)

    def test_clock_missing_historical_time_does_not_break_later_generation(self):
        now = datetime(2026, 10, 25, 12, tzinfo=timezone.utc)
        mono = [1000.0]
        wall = [now]
        self.box.latest_user_activity.return_value = {"uid": "5", "uidvalidity": "10", "received_at": None, "received_at_source": "unknown"}
        self.box.get_mailbox_cursor.return_value = (10, 5)
        self.box.read_user_emails_since.return_value = ((10, 5), [])
        self.bot.ask.side_effect = ["answer", json.dumps({"user_summary": None, "assistant_summary": "new topic", "mail_subject": "聊聊最近的新发现"})]
        prompts = {"task": "topic", "system_prompt": "persona", "summary_prompt": "summary", "subject": "subject"}
        scheduler = clock.IdleMailScheduler(self.receiver, prompts, {}, randint=lambda low, high: 60, now=lambda: wall[0], monotonic=lambda: mono[0])
        scheduler.start()
        mono[0] += 3600
        wall[0] += timedelta(hours=1)
        scheduler.tick()
        self.box.send_email.assert_called_once()
        state = payload(self.bot.ask.call_args_list[0].args[0])["conversation_state"]
        self.assertIsNone(state["latest_known_user_activity"])

    def test_clock_start_activity_enters_context_without_claimed_reply(self):
        now = datetime(2026, 10, 25, 12, tzinfo=timezone.utc)
        self.box.latest_user_activity.return_value = {
            "uid": "5", "uidvalidity": "10", "received_at": "2026-10-25T10:00:00+00:00", "received_at_source": "imap_internaldate",
        }
        self.box.get_mailbox_cursor.return_value = (10, 5)
        self.box.read_user_emails_since.return_value = ((10, 5), [])
        proactive_summary = json.dumps({"user_summary": None, "assistant_summary": "new topic", "mail_subject": "聊聊最近的新发现"})
        self.bot.ask.side_effect = ["answer", proactive_summary]
        prompts = {"task": "topic", "system_prompt": "persona", "summary_prompt": "summary", "subject": "subject"}
        scheduler = clock.IdleMailScheduler(self.receiver, prompts, {}, randint=lambda low, high: 60, now=lambda: now, monotonic=lambda: 1000)
        scheduler.start()
        scheduler.tick()
        state = payload(self.bot.ask.call_args_list[0].args[0])["conversation_state"]
        self.assertEqual(state["mode"], "scheduled_proactive")
        self.assertIn("2026-10-25T10:00:00", json.dumps(state))
        self.assertEqual(self.receiver.observed_user_activity[0]["uid"], "5")


if __name__ == "__main__":
    unittest.main()

"""随机主动来信：可控时间、模拟邮箱与模型，无真实网络。"""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from test_receive import (
    llm, load_module, mail, mail_logging, memory_module, message,
    mock_send, mock_summary, receive, sdk,
)
import sys

with patch.dict(sys.modules, {
    "openai": sdk, "dfy_receive": receive, "dfy_test": llm,
    "dialog_memory": memory_module, "mail": mail, "mail_logging": mail_logging,
}):
    clock = load_module("tested_clock", "dfy_clock.py")


class ClockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(mail_logging.close_logging)
        self.path = Path(self.tmp.name)
        self.wall = datetime(2026, 10, 25, 12, tzinfo=timezone.utc)
        self.mono = 1000.0
        self.box = MagicMock()
        self.box.get_mailbox_cursor.return_value = (10, 0)
        self.box.latest_user_activity.return_value = None
        self.box.read_user_emails_since.return_value = ((10, 0), [])
        self.box.send_email.side_effect = mock_send
        self.bot = MagicMock()
        def answer(question, **kwargs):
            if not kwargs.get("json_mode"):
                return "PRIVATE ANSWER"
            data = json.loads(question)
            result = {"user_summary": None if data["user_email"] is None else "用户提问。", "assistant_summary": "助手回答。"}
            if "mail_subject" in data.get("json_templates", {}).get("output_template", {}):
                result["mail_subject"] = "一起聊聊今天的新发现"
            return json.dumps(result, ensure_ascii=False)
        self.bot.ask.side_effect = answer
        self.receiver = receive.MailReceiver(
            self.box, self.bot, "PRIVATE REPLY", memory_path=self.path / "dialog.json",
            summary_prompt="PRIVATE SUMMARY PROMPT", clock=lambda: self.wall.timestamp(),
        )
        self.rng = MagicMock(return_value=60)
        self.prompts = {"system_prompt": "PRIVATE PERSONA", "task": "PRIVATE TOPIC", "subject": "PRIVATE SUBJECT", "summary_prompt": "PRIVATE SUMMARY PROMPT"}
        self.scheduler = clock.IdleMailScheduler(
            self.receiver, self.prompts, {}, randint=self.rng,
            now=lambda: self.wall, monotonic=lambda: self.mono,
        )

    def advance(self, seconds):
        self.wall += timedelta(seconds=seconds)
        self.mono += seconds

    def due(self):
        self.scheduler.start()
        self.advance(3600)

    def test_range_validation_and_defaults(self):
        self.assertEqual(clock.idle_range({}), (60, 240))
        self.assertEqual(clock.idle_range({"idle_min_minutes": 2, "idle_max_minutes": 2}), (2, 2))
        for low, high in ((True, 240), (60, False), (0, 240), (60, 59), (60.0, 240), (-1, 240)):
            with self.subTest(low=low, high=high), self.assertRaises(ValueError):
                clock.idle_range({"idle_min_minutes": low, "idle_max_minutes": high})

    def test_no_history_waits_and_boundary_sends(self):
        self.scheduler.start()
        self.rng.assert_called_once_with(60, 240)
        self.advance(3599)
        self.scheduler.tick()
        self.bot.ask.assert_not_called()
        self.advance(1)
        self.scheduler.tick()
        self.box.send_email.assert_called_once()
        self.assertEqual(self.box.send_email.call_args.args[0], "一起聊聊今天的新发现")
        self.assertNotEqual(self.box.send_email.call_args.args[0], self.prompts["subject"])
        self.assertEqual(self.rng.call_count, 2)
        self.assertEqual(self.scheduler.deadline, self.mono + 3600)
        dialogs = self.receiver.memory.snapshot()["dialogs"]
        self.assertIsNone(dialogs[0]["user_summary"])
        self.assertEqual(dialogs[0]["status"], "sent")
        call = self.bot.ask.call_args_list[0]
        self.assertEqual(call.kwargs["system_prompt"], "PRIVATE PERSONA")
        self.assertIn("PRIVATE TOPIC", call.args[0])

    def test_successful_send_restarts_from_completion(self):
        self.due()
        answer = self.bot.ask.side_effect
        def slow_answer(question, **kwargs):
            self.advance(20)
            return answer(question, **kwargs)
        def slow_send(*args, **kwargs):
            self.advance(30)
            return kwargs["message_id"]
        self.bot.ask.side_effect = slow_answer
        self.box.send_email.side_effect = slow_send
        self.scheduler.tick()
        self.assertEqual(self.scheduler.deadline, self.mono + 3600)

    def test_upper_endpoint_and_equal_range(self):
        self.rng.return_value = 240
        self.scheduler.start()
        self.assertEqual(self.scheduler.deadline, self.mono + 240 * 60)

    def test_old_user_history_is_due(self):
        self.box.latest_user_activity.return_value = {"received_at": (self.wall - timedelta(hours=3)).isoformat(), "received_at_source": "imap_internaldate"}
        self.scheduler.start()
        self.scheduler.tick()
        self.box.send_email.assert_called_once()
        self.box.latest_user_activity.assert_called_once_with((10, 0))

    def test_confirmed_proactive_time_prevents_restart_immediate_send(self):
        record = memory_module.new_record(self.receiver.memory.snapshot(), None, {"user_summary": None, "assistant_summary": "history"})
        record.update(status="sent", sent_at=(self.wall - timedelta(minutes=10)).isoformat())
        self.receiver.memory._write({"schema_version": 1, "revision": 1, "dialogs": [record]})
        self.box.latest_user_activity.return_value = {"received_at": (self.wall - timedelta(days=1)).isoformat(), "received_at_source": "imap_internaldate"}
        self.scheduler.start()
        self.assertEqual(self.scheduler.deadline, self.mono + 50 * 60)

    def test_reply_sent_time_does_not_extend_idle(self):
        incoming = message(1)
        incoming.update(received_at=(self.wall - timedelta(hours=2)).isoformat(), received_at_source="imap_internaldate")
        record = memory_module.new_record(self.receiver.memory.snapshot(), incoming, {"user_summary": "question", "assistant_summary": "answer"})
        record.update(status="sent", sent_at=self.wall.isoformat())
        self.receiver.memory._write({"schema_version": 1, "revision": 1, "dialogs": [record]})
        self.scheduler.start()
        self.assertEqual(self.scheduler.deadline, self.mono)

    def test_missing_and_future_history_time_is_conservative(self):
        for received in (None, (self.wall + timedelta(days=1)).isoformat()):
            self.scheduler.deadline = None
            self.scheduler.last_activity = None
            self.receiver.cursor = None
            self.box.latest_user_activity.return_value = {"received_at": received, "received_at_source": "imap_internaldate" if received else "unknown"}
            self.scheduler.start()
            self.assertEqual(self.scheduler.deadline, self.mono + 3600)

    def test_new_mail_resets_once_before_reply_not_after(self):
        self.scheduler.start()
        self.advance(3000)
        incoming = message(1, body="PRIVATE BODY")
        incoming.update(received_at=self.wall.isoformat(), received_at_source="imap_internaldate")
        self.box.read_user_emails_since.return_value = ((10, 1), [incoming])
        def answer(question, **kwargs):
            self.advance(30)
            return mock_summary(question) if kwargs.get("json_mode") else "PRIVATE ANSWER"
        self.bot.ask.side_effect = answer
        expected = self.mono + 3600
        self.scheduler.tick()
        self.assertEqual(self.scheduler.deadline, expected)
        self.assertEqual(self.rng.call_count, 2)
        self.scheduler.tick()  # 重复 UID 不应再次重抽。
        self.assertEqual(self.rng.call_count, 2)

    def test_empty_mail_does_not_reset(self):
        self.scheduler.start()
        self.box.read_user_emails_since.return_value = ((10, 1), [message(1, body=" ")])
        self.scheduler.tick()
        self.assertEqual(self.rng.call_count, 1)

    def test_arrival_during_generation_cancels_proactive(self):
        self.due()
        incoming = message(1)
        incoming.update(received_at=self.wall.isoformat(), received_at_source="imap_internaldate")
        self.box.read_user_emails_since.side_effect = [((10, 0), []), ((10, 1), [incoming])]
        self.scheduler.tick()
        # 只回复用户，主动草稿不发送。
        self.assertEqual(self.box.send_email.call_count, 1)
        self.assertEqual(self.box.send_email.call_args.kwargs["in_reply_to"], incoming["message_id"])
        self.assertEqual(self.receiver.memory.snapshot()["dialogs"][0]["type"], "用户主动发送")
        self.assertEqual(self.rng.call_count, 2)

    def test_imap_failure_prevents_generation(self):
        self.due()
        self.box.read_user_emails_since.side_effect = mail.imaplib.IMAP4.abort("PRIVATE ERROR")
        self.scheduler.tick()
        self.bot.ask.assert_not_called()
        self.box.send_email.assert_not_called()

    def test_final_check_failure_defers_draft_until_successful_recheck(self):
        self.due()
        self.box.read_user_emails_since.side_effect = [((10, 0), []), mail.imaplib.IMAP4.abort("PRIVATE ERROR")]
        self.scheduler.tick()
        self.box.send_email.assert_not_called()
        task_id = self.scheduler.draft["task_id"]
        self.assertEqual(self.receiver.store.get(task_id)["payload"]["body"], "PRIVATE ANSWER")
        self.scheduler.tick()
        self.box.send_email.assert_not_called()
        self.box.read_user_emails_since.side_effect = None
        self.advance(2)
        self.scheduler.tick()
        self.box.send_email.assert_called_once()
        self.assertEqual(self.receiver.store.get(task_id)["status"], "done")

    def test_pending_blocks_proactive_even_after_successful_imap(self):
        self.due()
        with patch.object(self.receiver, "poll_once", return_value=True):
            self.receiver.pending.append({})
            self.scheduler.tick()
        self.bot.ask.assert_not_called()

    def test_final_memory_revision_change_cancels_draft(self):
        self.due()
        def poll():
            if self.bot.ask.call_count:
                record = memory_module.new_record(self.receiver.memory.snapshot(), None, {"user_summary": None, "assistant_summary": "other task"})
                record.update(status="sent", sent_at=self.wall.isoformat())
                self.receiver.memory._write({"schema_version": 1, "revision": 3, "dialogs": [record]})
            return True
        with patch.object(self.receiver, "poll_once", side_effect=poll):
            self.scheduler.tick()
        self.box.send_email.assert_not_called()
        self.assertIsNone(self.scheduler.draft)

    def test_failed_sync_with_pending_processed_still_returns_false(self):
        self.scheduler.start()
        incoming = message(1)
        incoming.update(received_at=self.wall.isoformat(), received_at_source="imap_internaldate")
        self.receiver.store.ingest(self.receiver.account, (10, 1), [incoming])
        self.receiver.cursor = (10, 1)
        self.receiver._load_pending()
        self.box.read_user_emails_since.side_effect = mail.imaplib.IMAP4.abort("PRIVATE ERROR")
        self.assertFalse(self.receiver.poll_once())
        self.assertFalse(self.receiver.pending)
        self.box.send_email.assert_called_once()

    def test_new_user_activity_clears_summary_retry_draft(self):
        self.due()
        answer = self.bot.ask.side_effect
        def fail_summary(question, **kwargs):
            return "invalid json" if kwargs.get("json_mode") else "OLD DRAFT"
        self.bot.ask.side_effect = fail_summary
        self.scheduler.tick()
        self.assertEqual(self.scheduler.draft["body"], "OLD DRAFT")
        self.scheduler.on_user_message({"received_at": self.wall.isoformat()})
        self.assertIsNone(self.scheduler.draft)
        self.bot.ask.side_effect = answer
        self.advance(3600)
        self.scheduler.tick()
        self.assertEqual(self.box.send_email.call_args.args[1], "PRIVATE ANSWER")

    def test_generation_network_retries_indefinitely_with_capped_backoff(self):
        self.due()
        self.bot.ask.side_effect = clock.APIConnectionError()
        for count, delay in enumerate((2, 4, 8, 16, 32, 64, 128, 256, 256, 256), 1):
            self.scheduler.tick()
            self.assertEqual(self.bot.ask.call_count, count)
            row = self.receiver.store.get(self.scheduler.draft["task_id"])
            self.assertEqual(row["status"], "retry")
            self.assertEqual(row["next_attempt_at"], self.wall.timestamp() + delay)
            self.scheduler.tick()
            self.assertEqual(self.bot.ask.call_count, count)
            self.advance(delay)
        self.assertFalse(self.receiver.store.list_tasks(statuses=("quarantined",), kind="proactive"))
        self.box.send_email.assert_not_called()

    def test_imap_failure_does_not_reset_generation_backoff(self):
        self.due()
        self.bot.ask.side_effect = clock.APIConnectionError()
        self.scheduler.tick()
        task_id = self.scheduler.draft["task_id"]
        self.advance(2)
        self.scheduler.tick()
        self.box.read_user_emails_since.side_effect = mail.imaplib.IMAP4.abort("PRIVATE ERROR")
        self.scheduler.tick()
        self.box.read_user_emails_since.side_effect = None
        self.advance(2)
        self.scheduler.tick()
        self.assertEqual(self.bot.ask.call_count, 2)
        self.advance(2)
        self.scheduler.tick()
        self.assertEqual(self.bot.ask.call_count, 3)
        self.assertEqual(self.scheduler.draft["task_id"], task_id)
        self.assertEqual(self.scheduler.draft["network_failures"], 3)
        self.assertEqual(self.scheduler.draft["next_attempt_at"], self.wall.timestamp() + 8)
        self.box.send_email.assert_not_called()

    def test_summary_retry_reuses_body(self):
        self.due()
        self.bot.ask.side_effect = ["PRIVATE ANSWER", "invalid json", json.dumps({"user_summary": None, "assistant_summary": "助手回答。", "mail_subject": "一起聊聊今天的新发现"})]
        self.scheduler.tick()
        self.box.send_email.assert_not_called()
        self.scheduler.tick()
        self.assertEqual(self.bot.ask.call_count, 3)
        self.assertTrue(self.bot.ask.call_args_list[2].kwargs["json_mode"])
        self.box.send_email.assert_called_once()

    def test_summary_quarantines_after_three_attempts(self):
        self.due()
        self.bot.ask.side_effect = ["BODY", "bad", "bad", "bad"]
        self.scheduler.tick()
        self.scheduler.tick()
        self.scheduler.tick()
        self.assertEqual(self.bot.ask.call_count, 4)
        rows = self.receiver.store.list_tasks(statuses=("quarantined",), kind="proactive")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["payload"]["body"], "BODY")
        self.assertEqual(rows[0]["payload"]["invalid_failures"], 3)
        self.assertIsNone(self.scheduler.draft)
        self.assertGreater(self.scheduler.deadline, self.mono)
        self.scheduler.tick()
        self.assertEqual(self.bot.ask.call_count, 4)
        self.box.send_email.assert_not_called()

    def test_imap_failure_does_not_reset_summary_retry_limit(self):
        self.due()
        self.bot.ask.side_effect = lambda question, **kwargs: "bad json" if kwargs.get("json_mode") else "BODY"
        self.scheduler.tick()
        self.scheduler.tick()
        self.box.read_user_emails_since.side_effect = mail.imaplib.IMAP4.abort("PRIVATE ERROR")
        self.scheduler.tick()
        self.box.read_user_emails_since.side_effect = None
        self.advance(2)
        self.scheduler.tick()
        rows = self.receiver.store.list_tasks(statuses=("quarantined",), kind="proactive")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["payload"]["invalid_failures"], 3)
        self.assertEqual(rows[0]["payload"]["body"], "BODY")
        self.assertEqual(self.bot.ask.call_count, 4)
        self.box.send_email.assert_not_called()

    def test_permanent_api_error_does_not_retry(self):
        self.due()
        self.bot.ask.side_effect = clock.APIStatusError(401)
        self.scheduler.tick()
        self.assertEqual(self.receiver.store.get_control("service:api:paused"), "api_auth_access_failed")
        self.scheduler.tick()
        self.bot.ask.assert_called_once()
        self.box.send_email.assert_not_called()

    def test_send_uncertainty_stops_no_retry(self):
        self.due()
        self.box.send_email.side_effect = OSError("PRIVATE BODY")
        self.scheduler.tick()
        self.assertTrue(self.scheduler.stopped)
        rows = self.receiver.store.list_tasks(statuses=("blocked_unknown",), kind="proactive")
        self.assertEqual(len(rows), 1)
        self.scheduler.tick()
        self.advance(24 * 3600)
        self.scheduler.tick()
        self.box.send_email.assert_called_once()
        data = json.loads((self.path / "dialog.json").read_text(encoding="utf-8"))
        self.assertEqual(data["dialogs"][0]["status"], "unknown")
        self.assertEqual(self.rng.call_count, 1)

    def test_pending_memory_stops_start(self):
        record = memory_module.new_record(self.receiver.memory.snapshot(), None, {"user_summary": None, "assistant_summary": "unconfirmed"})
        self.receiver.memory._write({"schema_version": 1, "revision": 1, "dialogs": [record]})
        self.scheduler.start()
        self.assertEqual(self.receiver.blocked_reason, "memory_recovery_required")
        self.scheduler.tick()
        self.box.send_email.assert_not_called()

    def test_clock_rollback_then_user_mail_still_waits_full_interval(self):
        self.scheduler.start()
        self.wall -= timedelta(days=1)
        self.scheduler.on_user_message({"received_at": self.wall.isoformat()})
        self.assertEqual(self.scheduler.deadline, self.mono + 3600)

    def test_wall_clock_change_does_not_trigger_deadline(self):
        self.scheduler.start()
        self.wall += timedelta(days=10)
        self.scheduler.tick()
        self.bot.ask.assert_not_called()

    def test_sleep_recovery_sends_only_once(self):
        self.scheduler.start()
        self.advance(24 * 3600)
        self.scheduler.tick()
        self.scheduler.tick()
        self.box.send_email.assert_called_once()

    def test_status_log_does_not_store_private_content(self):
        with redirect_stderr(io.StringIO()):
            path = mail_logging.configure_logging(self.path, script="dfy_clock.py")
            self.due()
            self.scheduler.tick()
        text = path.read_text(encoding="utf-8")
        self.assertIn("wait_minutes=60", text)
        self.assertIn("主动发送成功", text)
        self.assertIn("entry=dfy_clock.py", text)
        for value in ("PRIVATE ANSWER", "PRIVATE REPLY", "PRIVATE TOPIC", "PRIVATE SUBJECT", "PRIVATE PERSONA", "PRIVATE SUMMARY PROMPT"):
            self.assertNotIn(value, text)

    def test_main_errors_are_logged_without_secrets(self):
        def start(script):
            return mail_logging.configure_logging(self.path, script=script)
        with redirect_stderr(io.StringIO()), patch.object(clock, "start_logging", side_effect=start), \
             patch.object(clock, "SingleInstanceLock"), \
             patch.object(clock, "_load_settings", side_effect=ValueError("PRIVATE CONFIG")):
            with self.assertRaises(SystemExit) as result:
                clock.main()
            self.assertEqual(result.exception.code, 1)
        text = (self.path / "dfy_clock.log").read_text(encoding="utf-8")
        self.assertIn("进程结束", text)
        self.assertNotIn("PRIVATE CONFIG", text)

    def test_main_wires_one_receiver_and_scheduler(self):
        settings = {"assistant": {"auth_code_env": "CLOCK_AUTH"}}
        prompts = dict(self.prompts, reply_prompt="reply")
        with redirect_stderr(io.StringIO()), \
             patch.object(clock, "start_logging", side_effect=lambda script: mail_logging.configure_logging(self.path, script=script)), \
             patch.object(clock, "SingleInstanceLock"), \
             patch.object(clock, "_load_settings", return_value=settings), \
             patch.object(clock, "load_clock_prompts", return_value=prompts), \
             patch.dict(clock.os.environ, {"DEEPSEEK_API_KEY": "PRIVATE KEY", "CLOCK_AUTH": "PRIVATE AUTH"}), \
             patch.object(clock, "MailClient") as mailbox, patch.object(clock, "DeepSeekChat") as bot, \
             patch.object(clock, "MailReceiver") as receiver, patch.object(clock, "IdleMailScheduler") as scheduler:
            clock.main()
            mailbox.assert_called_once_with(config_path=clock.DEFAULT_CONFIG, auth_code="PRIVATE AUTH")
            bot.assert_called_once_with(api_key="PRIVATE KEY")
            scheduler.assert_called_once_with(receiver.return_value, prompts, settings)
            scheduler.return_value.run.assert_called_once()
        text = (self.path / "dfy_clock.log").read_text(encoding="utf-8")
        self.assertNotIn("PRIVATE KEY", text)
        self.assertNotIn("PRIVATE AUTH", text)

    def test_prompts_use_shared_persona_not_first_greeting(self):
        common = self.path / "prompt.json"
        positive = self.path / "positive_prompt.json"
        common.write_text(json.dumps({"prompt": {"角色": ["PERSONA"]}, "send_task": ["FIRST GREETING"], "reply_task": ["REPLY"], "summary_prompt": "SUMMARY"}), encoding="utf-8")
        positive.write_text(json.dumps({"subject": "TOPIC", "prompt": {"主动": ["START TOPIC"]}}), encoding="utf-8")
        prompts = clock.load_clock_prompts(common, positive)
        self.assertIn("PERSONA", prompts["system_prompt"])
        self.assertIn("START TOPIC", prompts["task"])
        self.assertNotIn("FIRST GREETING", str(prompts))
        for subject in ("", "bad\nsubject", None):
            positive.write_text(json.dumps({"subject": subject, "prompt": "task"}), encoding="utf-8")
            with self.assertRaises(ValueError):
                clock.load_clock_prompts(common, positive)


if __name__ == "__main__":
    unittest.main()

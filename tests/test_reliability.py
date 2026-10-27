"""持久任务与无限网络退避集成，完全离线。"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from test_receive import receive, mail, memory_module as memory, message, mock_send, SUMMARY, retry_policy


class ReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "dialog.json"
        self.now = [1000.0]
        self.box, self.bot = MagicMock(), MagicMock()
        self.box.get_mailbox_cursor.return_value = (10, 0)
        self.box.send_email.side_effect = mock_send
        self.box.read_user_emails_since.return_value = ((10, 1), [message(1)])
        self.bot.ask.side_effect = lambda q, **kw: SUMMARY if kw.get("json_mode") else "answer"

    def listener(self):
        return receive.MailReceiver(self.box, self.bot, "persona", memory_path=self.path,
                                    summary_prompt="summary", clock=lambda: self.now[0])

    def test_deleted_queue_is_not_silently_recreated(self):
        listener = self.listener()
        listener.start()
        listener.store.path.unlink()
        import sqlite3
        with self.assertRaises(sqlite3.OperationalError):
            listener.store.cursor(listener.account)
        self.assertFalse(listener.store.path.exists())

    def test_initialized_queue_missing_on_restart_is_blocked(self):
        first = self.listener()
        first.store.path.unlink()
        with self.assertRaises(memory.MemoryError):
            self.listener()

    def test_released_proactive_task_is_consumed_without_stale_snapshot(self):
        from test_clock import clock
        listener = self.listener()
        listener.start()
        row = listener.store.create(listener.account, {'snapshot': {}, 'body': 'bad', 'invalid_failures': 3}, kind='proactive')
        listener.store.quarantine(row['id'])
        listener.store.retry(row['id'])
        prompts = {'task': 'topic', 'system_prompt': 'persona', 'summary_prompt': 'summary', 'subject': 'unused'}
        scheduler = clock.IdleMailScheduler(listener, prompts, {}, randint=lambda a,b: 60)
        draft = scheduler._current_draft(listener.memory.snapshot())
        self.assertEqual(draft['task_id'], row['id'])
        self.assertEqual(draft['snapshot']['revision'], 0)
        self.assertNotIn('body', draft)

    def test_backoff_caps_and_never_overflows(self):
        self.assertEqual([retry_policy.retry_delay(n) for n in range(1, 11)], [2, 4, 8, 16, 32, 64, 128, 256, 256, 256])
        self.assertEqual(retry_policy.retry_delay(10 ** 100), 256)

    def test_restart_resumes_pending_and_old_cursor(self):
        first = self.listener()
        first.start()
        self.bot.ask.side_effect = receive.APIConnectionError()
        first.poll_once()
        self.assertEqual(first.cursor, (10, 1))
        self.box.get_mailbox_cursor.return_value = (10, 9)
        second = self.listener()
        second.start()
        self.assertEqual(second.cursor, (10, 1))
        self.assertEqual(len(second.pending), 1)
        self.box.read_user_emails_since.return_value = ((10, 9), [])
        self.now[0] += 2
        self.bot.ask.side_effect = lambda q, **kw: SUMMARY if kw.get("json_mode") else "answer"
        second.poll_once()
        self.box.send_email.assert_called_once()
        self.assertFalse(second.store.list_tasks(account=second.account))

    def test_network_failures_more_than_three_survive(self):
        listener = self.listener()
        listener.start()
        self.bot.ask.side_effect = receive.APIConnectionError()
        for attempt in range(1, 11):
            listener.poll_once()
            row = listener.store.list_tasks(account=listener.account)[0]
            self.assertEqual(row["status"], "retry")
            self.assertEqual(row["next_attempt_at"], self.now[0] + min(2 ** attempt, 256))
            self.now[0] = row["next_attempt_at"]
        self.assertEqual(self.bot.ask.call_count, 10)
        self.box.send_email.assert_not_called()

    def test_bad_task_isolated_other_mail_succeeds(self):
        listener = self.listener()
        listener.start()
        self.box.read_user_emails_since.return_value = ((10, 2), [message(1, body="bad"), message(2, body="good")])
        def answer(question, **kwargs):
            if kwargs.get("json_mode"):
                return "invalid" if json.loads(question)["user_email"] == "bad" else SUMMARY
            return "answer"
        self.bot.ask.side_effect = answer
        for _ in range(3):
            listener.poll_once()
        isolated = listener.store.list_tasks(statuses=("quarantined",), account=listener.account)
        self.assertEqual(len(isolated), 1)
        self.box.send_email.assert_called_once()
        self.assertEqual(isolated[0]["payload"]["message"]["body"], "bad")

    def test_api_auth_pauses_but_receives_new_mail(self):
        listener = self.listener()
        listener.start()
        self.bot.ask.side_effect = receive.APIStatusError(401)
        listener.poll_once()
        self.assertTrue(listener.store.get_control("service:api:paused"))
        self.box.read_user_emails_since.return_value = ((10, 2), [message(2)])
        listener.poll_once()
        self.assertEqual(len(listener.store.list_tasks(account=listener.account)), 2)
        self.assertEqual(self.bot.ask.call_count, 1)

    def test_smtp_unknown_durable_block_no_resend(self):
        listener = self.listener()
        listener.start()
        self.box.send_email.side_effect = OSError("unknown delivery")
        self.assertFalse(listener.poll_once())
        listener.poll_once()
        self.box.send_email.assert_called_once()
        blocked = listener.store.list_tasks(statuses=("blocked_unknown",), account=listener.account)
        self.assertEqual(len(blocked), 1)
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(data["dialogs"][0]["status"], "unknown")


if __name__ == "__main__":
    unittest.main()

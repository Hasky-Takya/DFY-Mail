"""联合提取与安全发送集成：仅合成邮件、临时记忆及模拟模型。"""

import io
import json
from contextlib import redirect_stderr
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from test_receive import mail, mail_logging, memory_module as memory, message, mock_send, proactive, receive, story_module as story
from test_clock import clock


BODY = "明天和桃井一起测试游戏。"
ANSWER = "好，我们明天一起测试游戏！"


def combined(user=True, content=BODY, source="user", *, generate_subject=False):
    return json.dumps({
        **({"mail_subject": "一起试试明天的新游戏"} if generate_subject else {}),
        "user_summary": "用户计划测试游戏。" if user else None,
        "assistant_summary": "助手计划一起测试游戏。",
        "event_updates": [{
            "target_id": None, "title": "测试游戏", "content": content,
            "source": source, "domain": "shared_story", "importance": 4,
            "people": ["用户", "桃井"], "status": "planned", "tense": "future",
            "event_time": {"raw": "明天", "start": None, "end": None},
            "evidence": content,
        }], "memory_updates": [],
    }, ensure_ascii=False)


class StoryIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / "dialog.json"
        self.store = memory.DialogMemory(self.path)
        self.box = MagicMock()
        self.box.get_mailbox_cursor.return_value = (10, 0)
        self.box.read_user_emails_since.return_value = ((10, 1), [message(1, body=BODY)])
        self.box.latest_user_activity.return_value = None
        self.box.send_email.side_effect = mock_send
        self.bot = MagicMock()
        self.bot.ask.side_effect = [ANSWER, combined()]

    def extract(self):
        snapshot = self.store.snapshot(include_story=True)
        output = memory.summarize(self.bot, "摘要", BODY, ANSWER, story_context=snapshot["story_context"], extraction_prompt="四字段提取")
        incoming = message(1, body=BODY)
        incoming.update(received_at="2026-10-25T10:00:00+00:00", received_at_source="imap_internaldate")
        return snapshot, memory.new_record(snapshot, incoming, output)

    def test_joint_summary_sent_and_reloaded(self):
        self.bot.ask.side_effect = [combined()]
        snapshot, record = self.extract()
        self.assertEqual(self.store.snapshot()["dialogs"], [])
        memory.send_recorded(self.store, snapshot, record, self.box, "subject", ANSWER)
        context = self.store.snapshot(include_story=True)["story_context"]
        self.assertEqual(context["revision"], 1)
        self.assertEqual(len(context["events"]), 1)
        self.assertEqual(context["events"][0]["content"], BODY)
        self.assertEqual(context["events"][0]["source_refs"][0]["dialog_id"], record["id"])
        self.assertTrue((self.root / "event.json").exists())
        self.assertTrue((self.root / "memory.json").exists())
        self.assertNotIn("story_context", json.loads(self.path.read_text(encoding="utf-8")))
        self.store.snapshot(include_story=True)
        self.box.send_email.assert_called_once()

    def test_sent_projection_failure_recovery_never_resends(self):
        self.bot.ask.side_effect = [combined()]
        snapshot, record = self.extract()
        original = memory.reconcile_locked
        def fail_after_sent(data, directory):
            if any(item["status"] == "sent" and "story_updates" in item for item in data["dialogs"]):
                raise story.StoryError("simulated projection failure")
            return original(data, directory)
        with patch.object(memory, "reconcile_locked", side_effect=fail_after_sent):
            with self.assertRaises(memory.MemoryError) as exc:
                memory.send_recorded(self.store, snapshot, record, self.box, "subject", ANSWER)
            self.assertIn("邮件已确认发送", str(exc.exception))
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["dialogs"][0]["status"], "sent")
        self.assertEqual(self.store.snapshot(include_story=True)["story_context"]["revision"], 1)
        self.box.send_email.assert_called_once()

    def test_unknown_send_does_not_project(self):
        self.bot.ask.side_effect = [combined()]
        snapshot, record = self.extract()
        self.box.send_email.side_effect = OSError("failed")
        with self.assertRaises(memory.MemoryError):
            memory.send_recorded(self.store, snapshot, record, self.box, "subject", ANSWER)
        self.assertEqual(json.loads((self.root / "event.json").read_text(encoding="utf-8"))["events"], [])
        with self.assertRaises(memory.MemoryError):
            self.store.snapshot(include_story=True)

    def test_receiver_uses_story_and_only_two_calls(self):
        listener = receive.MailReceiver(self.box, self.bot, "persona", memory_path=self.path, summary_prompt="summary", extraction_prompt="extract")
        listener.start()
        listener.poll_once()
        self.assertEqual(self.bot.ask.call_count, 2)
        request = self.bot.ask.call_args_list[0].args[0]
        self.assertIn('"story_context"', request)
        extract_input = json.loads(self.bot.ask.call_args_list[1].args[0])
        self.assertEqual(extract_input["user_email"], BODY)
        self.assertIn("story_context", extract_input)
        self.assertEqual(self.store.snapshot(include_story=True)["story_context"]["revision"], 1)

    def test_manual_entry_uses_joint_protocol(self):
        prompt = self.root / "prompt.json"
        prompt.write_text(json.dumps({"subject": "测试", "prompt": "persona", "summary_prompt": "summary", "story_extraction_prompt": "extract"}), encoding="utf-8")
        self.bot.ask.side_effect = [ANSWER, combined(False, ANSWER, "llm", generate_subject=True)]
        self.box._auth_code = "fake-auth"
        with patch.object(proactive, "MailClient", return_value=self.box), patch.object(proactive, "DeepSeekChat", return_value=self.bot):
            proactive.send_deepseek_email("fake-api", "fake-auth", prompt, memory_path=self.path)
        self.assertEqual(self.bot.ask.call_count, 2)
        self.assertEqual(self.box.send_email.call_args.args[0], "一起试试明天的新游戏")
        self.assertEqual(self.store.snapshot(include_story=True)["story_context"]["events"][0]["source"], "llm")

    def test_clock_success_commits_story_and_resets(self):
        listener = receive.MailReceiver(self.box, self.bot, "persona", memory_path=self.path, summary_prompt="summary", extraction_prompt="extract")
        prompts = {"task": "topic", "system_prompt": "persona", "subject": "test", "summary_prompt": "summary"}
        mono = [0.0]
        scheduler = clock.IdleMailScheduler(listener, prompts, {}, randint=lambda a, b: 60,
                                            now=lambda: clock.datetime(2026, 10, 25, tzinfo=clock.timezone.utc),
                                            monotonic=lambda: mono[0])
        scheduler.start()
        self.box.read_user_emails_since.return_value = ((10, 0), [])
        mono[0] = 3600
        self.bot.ask.side_effect = [ANSWER, combined(False, ANSWER, "llm", generate_subject=True)]
        scheduler.tick()
        self.box.send_email.assert_called_once()
        self.assertEqual(self.box.send_email.call_args.args[0], "一起试试明天的新游戏")
        self.assertEqual(self.store.snapshot(include_story=True)["story_context"]["revision"], 1)
        self.assertEqual(scheduler.deadline, 7200)
        self.assertEqual(self.bot.ask.call_count, 2)

    def test_clock_cancelled_draft_does_not_project(self):
        listener = receive.MailReceiver(self.box, self.bot, "persona", memory_path=self.path, summary_prompt="summary", extraction_prompt="extract")
        prompts = {"task": "topic", "system_prompt": "persona", "subject": "test", "summary_prompt": "summary"}
        mono = [0.0]
        scheduler = clock.IdleMailScheduler(listener, prompts, {}, randint=lambda a, b: 60,
                                            now=lambda: clock.datetime(2026, 10, 25, tzinfo=clock.timezone.utc),
                                            monotonic=lambda: mono[0])
        scheduler.start()
        mono[0] = 3600
        self.bot.ask.side_effect = [ANSWER, combined(False, ANSWER, "llm", generate_subject=True)]
        self.box.read_user_emails_since.side_effect = [((10, 0), []), mail.imaplib.IMAP4.abort("temporary")]
        scheduler.tick()
        self.box.send_email.assert_not_called()
        self.assertEqual(self.store.snapshot(include_story=True)["story_context"]["events"], [])

    def test_story_content_does_not_enter_status_log(self):
        self.addCleanup(mail_logging.close_logging)
        with redirect_stderr(io.StringIO()):
            path = mail_logging.configure_logging(self.root / "logs", script="dfy_receive.py")
            listener = receive.MailReceiver(self.box, self.bot, "persona", memory_path=self.path, summary_prompt="summary", extraction_prompt="extract")
            listener.start()
            listener.poll_once()
        text = path.read_text(encoding="utf-8")
        self.assertIn("投影已确认", text)
        self.assertNotIn(BODY, text)
        self.assertNotIn(ANSWER, text)
        self.assertNotIn("测试游戏", text)

    def test_joint_invalid_evidence_not_sent(self):
        self.bot.ask.side_effect = [combined(content="虚构用户说过的话")]
        snapshot = self.store.snapshot(include_story=True)
        with self.assertRaises(memory.SummaryError):
            memory.summarize(self.bot, "summary", BODY, ANSWER, story_context=snapshot["story_context"], extraction_prompt="extract")
        self.box.send_email.assert_not_called()

    def test_joint_budget_and_old_protocol(self):
        self.bot.ask.return_value = combined()
        self.bot.ask.side_effect = None
        with self.assertRaises(memory.SummaryError):
            memory.summarize(self.bot, "summary", BODY, ANSWER)
        with patch.object(memory, "CONTEXT_LIMIT", 10), self.assertRaises(memory.MemoryError):
            memory.summarize(self.bot, "summary", BODY, ANSWER, story_context=story.empty_context(), extraction_prompt="extract")


if __name__ == "__main__":
    unittest.main()

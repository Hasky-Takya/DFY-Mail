"""模板和定向修正测试；无真实模型、邮件或记忆访问。"""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import MagicMock, patch

from test_receive import memory_module as memory, receive, proactive, mail_logging, message, mock_send, story_module as story
from test_clock import clock
from test_story_integration import BODY, ANSWER, combined


class ExtractionRepairTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(mail_logging.close_logging)
        self.root = Path(self.tmp.name)
        self.bot, self.box = MagicMock(), MagicMock()
        self.box.get_mailbox_cursor.return_value = (10, 0)
        self.box.read_user_emails_since.return_value = ((10, 1), [message(1, body=BODY)])
        self.box.latest_user_activity.return_value = None
        self.box.send_email.side_effect = mock_send

    def test_template_and_feedback_in_model_input(self):
        bad = combined(content="PRIVATE WRONG QUOTE")
        self.bot.ask.side_effect = [bad, combined()]
        args = dict(story_context=story.empty_context(), extraction_prompt="extract")
        with self.assertRaises(memory.SummaryError) as failed:
            memory.summarize(self.bot, "summary", BODY, ANSWER, **args)
        feedback = failed.exception.repair_feedback()
        self.assertEqual(feedback["error"]["code"], "evidence_not_in_source")
        self.assertEqual(feedback["error"]["path"], "event_updates[0].evidence")
        self.assertNotIn("PRIVATE", json.dumps(feedback["error"]))
        memory.summarize(self.bot, "summary", BODY, ANSWER, validation_feedback=feedback, **args)
        first = json.loads(self.bot.ask.call_args_list[0].args[0])
        second = json.loads(self.bot.ask.call_args_list[1].args[0])
        self.assertIn("json_templates", first)
        self.assertNotIn("repair", first)
        self.assertEqual(second["repair"], feedback)
        self.assertEqual(first["assistant_email"], second["assistant_email"])
        template = first["json_templates"]
        self.assertEqual(set(template["output_template"]), {"user_summary", "assistant_summary", "event_updates", "memory_updates"})
        self.assertIsNone(memory.extraction_template(False)["output_template"]["user_summary"])

    def test_receiver_repair_keeps_body_and_logs_safe_error(self):
        self.bot.ask.side_effect = [ANSWER, combined(content="PRIVATE WRONG QUOTE"), combined()]
        listener = receive.MailReceiver(self.box, self.bot, "persona", memory_path=self.root / "dialog.json", summary_prompt="summary", extraction_prompt="extract")
        with redirect_stderr(io.StringIO()):
            log = mail_logging.configure_logging(self.root / "logs", script="dfy_receive.py")
            listener.start()
            listener.poll_once()
            self.box.send_email.assert_not_called()
            listener.poll_once()
        self.assertEqual(self.bot.ask.call_count, 3)
        self.box.send_email.assert_called_once()
        repaired = json.loads(self.bot.ask.call_args_list[2].args[0])
        self.assertEqual(repaired["repair"]["error"]["code"], "evidence_not_in_source")
        text = log.read_text(encoding="utf-8")
        self.assertIn("code=evidence_not_in_source", text)
        self.assertIn("field=event_updates[0].evidence", text)
        for secret in (BODY, ANSWER, "PRIVATE WRONG QUOTE"):
            self.assertNotIn(secret, text)

    def test_manual_repair_keeps_body(self):
        prompt = self.root / "prompt.json"
        prompt.write_text(json.dumps({"subject": "test", "prompt": "persona", "summary_prompt": "summary", "story_extraction_prompt": "extract"}), encoding="utf-8")
        self.box._auth_code = "fake-auth"
        self.bot.ask.side_effect = [ANSWER, combined(False, "PRIVATE WRONG QUOTE", "llm", generate_subject=True), combined(False, ANSWER, "llm", generate_subject=True)]
        with patch.object(proactive, "MailClient", return_value=self.box), patch.object(proactive, "DeepSeekChat", return_value=self.bot):
            proactive.send_deepseek_email("fake-api", "fake-auth", prompt, memory_path=self.root / "dialog.json")
        self.assertEqual(self.bot.ask.call_count, 3)
        self.box.send_email.assert_called_once()
        self.assertEqual(self.box.send_email.call_args.args[0], "一起试试明天的新游戏")
        repaired = json.loads(self.bot.ask.call_args_list[2].args[0])
        self.assertIn("repair", repaired)
        self.assertEqual(repaired["repair"]["error"]["code"], "evidence_not_in_source")

    def test_clock_repair_and_three_attempt_limit(self):
        listener = receive.MailReceiver(self.box, self.bot, "persona", memory_path=self.root / "dialog.json", summary_prompt="summary", extraction_prompt="extract")
        mono = [0.0]
        prompts = {"task": "topic", "system_prompt": "persona", "subject": "test", "summary_prompt": "summary"}
        scheduler = clock.IdleMailScheduler(listener, prompts, {}, randint=lambda a, b: 60, monotonic=lambda: mono[0])
        scheduler.start()
        mono[0] = 3600
        self.box.read_user_emails_since.return_value = ((10, 0), [])
        self.bot.ask.side_effect = [ANSWER, combined(False, "BAD QUOTE", "llm", generate_subject=True), combined(False, "BAD QUOTE", "llm", generate_subject=True), combined(False, "BAD QUOTE", "llm", generate_subject=True)]
        scheduler.tick()
        scheduler.tick()
        with self.assertRaises(RuntimeError):
            scheduler.tick()
        self.assertEqual(self.bot.ask.call_count, 4)
        self.assertIn("repair", json.loads(self.bot.ask.call_args_list[2].args[0]))
        self.box.send_email.assert_not_called()

    def test_invalidated_clock_draft_drops_feedback(self):
        listener = receive.MailReceiver(self.box, self.bot, "persona", memory_path=self.root / "dialog.json", summary_prompt="summary", extraction_prompt="extract")
        mono = [0.0]
        prompts = {"task": "topic", "system_prompt": "persona", "subject": "test", "summary_prompt": "summary"}
        scheduler = clock.IdleMailScheduler(listener, prompts, {}, randint=lambda a, b: 60, monotonic=lambda: mono[0])
        scheduler.start()
        self.box.read_user_emails_since.return_value = ((10, 0), [])
        mono[0] = 3600
        self.bot.ask.side_effect = [ANSWER, combined(False, "BAD QUOTE", "llm", generate_subject=True)]
        scheduler.tick()
        self.assertIn("validation_feedback", scheduler.draft)
        scheduler.on_user_message({"received_at": clock.utc_now().isoformat()})
        self.assertIsNone(scheduler.draft)
        self.bot.ask.side_effect = [ANSWER, combined(False, ANSWER, "llm", generate_subject=True)]
        mono[0] += 3600
        scheduler.tick()
        request = json.loads(self.bot.ask.call_args_list[-1].args[0])
        self.assertNotIn("repair", request)

    def test_invalid_context_is_not_retryable_summary_error(self):
        self.bot.ask.return_value = combined()
        with self.assertRaises(memory.MemoryError):
            memory.summarize(self.bot, "summary", BODY, ANSWER, story_context={}, extraction_prompt="extract")
        self.bot.ask.assert_not_called()

    def test_duplicate_keys_and_nonfinite_not_accepted(self):
        for output in ('{"user_summary":"x","user_summary":"y","assistant_summary":"z"}',
                       '{"user_summary":NaN,"assistant_summary":"z"}'):
            self.bot.ask.return_value = output
            with self.subTest(output=output), self.assertRaises(memory.SummaryError) as failed:
                memory.summarize(self.bot, "summary", BODY, ANSWER)
            self.assertEqual(failed.exception.details["code"], "invalid_json")

    def test_feedback_counts_toward_budget(self):
        self.bot.ask.return_value = combined()
        with patch.object(memory, "CONTEXT_LIMIT", 3000), self.assertRaises(memory.MemoryError):
            memory.summarize(self.bot, "summary", BODY, ANSWER,
                             validation_feedback={"previous_output": "x" * 4000})
        self.bot.ask.assert_not_called()


if __name__ == "__main__":
    unittest.main()

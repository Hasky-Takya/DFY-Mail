"""Graph integration: synthetic mail and disposable directories only."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from test_receive import memory_module as memory, message, mock_send, proactive, receive, llm
from test_story_integration import BODY, ANSWER, combined
from test_clock import clock
from graph_memory import empty_candidate
from graph_store import GraphStore, GraphStoreError


def output(user=True, generate_subject=False):
    raw = json.loads(combined(user, BODY if user else ANSWER,
                              "user" if user else "llm", generate_subject=generate_subject))
    source = "user" if user else "llm"
    quote = BODY if user else ANSWER
    graph = empty_candidate()
    graph["facts"] = [{"ref": "E1", "text": "用户与桃井计划一起测试游戏。",
                       "category": "event", "source": source, "domain": "shared_story", "evidence": quote}]
    graph["entities"] = [{"ref": "P1", "type": "person", "name": "桃井", "aliases": ["momoi"],
                          "target_id": None, "expected_version": None,
                          "source": source, "domain": "shared_story", "evidence": quote}]
    graph["states"] = [{"target_id": None, "expected_version": None,
                        "subject": "P1", "predicate": "working_on", "value": "测试游戏",
                        "state": "active", "valid_until": None, "support_ref": "E1",
                        "source": source, "domain": "shared_story", "evidence": quote}]
    raw["graph"] = graph
    return json.dumps(raw, ensure_ascii=False)


class GraphIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / "dialog.json"
        self.memory = memory.DialogMemory(self.path, graph_settings={"enabled": True})
        self.box = MagicMock()
        self.box.get_mailbox_cursor.return_value = (10, 0)
        self.box.read_user_emails_since.return_value = ((10, 1), [message(1, body=BODY)])
        self.box.latest_user_activity.return_value = None
        self.box.send_email.side_effect = mock_send
        self.bot = MagicMock()

    def extract(self, raw=None):
        snapshot = self.memory.snapshot(include_story=True)
        self.bot.ask.return_value = output() if raw is None else raw
        result = memory.summarize(self.bot, "summary", BODY, ANSWER,
                                  story_context=snapshot["story_context"], extraction_prompt="extract",
                                  graph_context=snapshot.get("graph_context"),
                                  graph_canonical=snapshot.get("graph_canonical"), graph_settings={"enabled": True})
        incoming = message(1, body=BODY)
        incoming.update(received_at="2026-10-25T10:00:00+00:00", received_at_source="imap_internaldate")
        return snapshot, memory.new_record(snapshot, incoming, result)

    def test_sent_graph_uses_same_new_story_id(self):
        snapshot, record = self.extract()
        self.assertEqual(record["graph_updates"]["status"], "complete")
        self.assertFalse(json.loads((self.root / "event_v2.json").read_text())["events"])
        memory.send_recorded(self.memory, snapshot, record, self.box, "subject", ANSWER)
        final = self.memory.snapshot(include_story=True)
        event_id = final["story_context"]["events"][0]["id"]
        exported = json.loads((self.root / "event_v2.json").read_text(encoding="utf-8"))
        self.assertEqual(exported["events"][0]["id"], event_id)
        self.assertEqual(exported["events"][0]["text"], "用户与桃井计划一起测试游戏。")
        self.assertEqual(exported["states"][0]["value"], "测试游戏")
        self.assertEqual(final["dialogs"][0]["status"], "sent")
        self.box.send_email.assert_called_once()
        text = self.memory.question(final, "下一步?", story_context=final["story_context"])
        request = json.loads(text.split("\n", 1)[1])
        self.assertIn("graph_context", request)
        self.assertEqual(request["story_context"]["events"][0]["content"], "用户与桃井计划一起测试游戏。")

    def test_optional_invalid_graph_does_not_hide_core_failure(self):
        raw = json.loads(output())
        raw["graph"]["entities"][0]["evidence"] = "not in source"
        snapshot, record = self.extract(json.dumps(raw))
        self.assertEqual(record["graph_updates"]["status"], "incomplete")
        memory.send_recorded(self.memory, snapshot, record, self.box, "subject", ANSWER)
        raw["assistant_summary"] = None
        with self.assertRaises(memory.SummaryError):
            self.extract(json.dumps(raw))
        self.box.send_email.assert_called_once()

    def test_old_model_response_records_incomplete(self):
        snapshot, record = self.extract(combined())
        self.assertEqual(record["graph_updates"]["status"], "incomplete")
        self.assertEqual(record["graph_updates"]["reason"], "unavailable")

    def test_unknown_smtp_does_not_apply_graph(self):
        snapshot, record = self.extract()
        self.box.send_email.side_effect = OSError("simulated")
        with self.assertRaises(memory.MemoryError):
            memory.send_recorded(self.memory, snapshot, record, self.box, "subject", ANSWER)
        self.assertEqual(json.loads((self.root / "event_v2.json").read_text())["events"], [])
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["dialogs"][0]["status"], "unknown")

    def test_new_projection_failure_does_not_change_sent_or_repeat_smtp(self):
        snapshot, record = self.extract()
        with patch.object(GraphStore, "sync", side_effect=GraphStoreError("synthetic")):
            memory.send_recorded(self.memory, snapshot, record, self.box, "subject", ANSWER)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["dialogs"][0]["status"], "sent")
        self.memory.snapshot(include_story=True)
        self.box.send_email.assert_called_once()

    def test_receiver_enabled_has_two_model_calls(self):
        self.bot.ask.side_effect = [ANSWER, output()]
        receiver = receive.MailReceiver(self.box, self.bot, "persona", memory_path=self.path,
                                        summary_prompt="summary", extraction_prompt="extract",
                                        graph_settings={"enabled": True})
        receiver.start()
        receiver.poll_once()
        self.assertEqual(self.bot.ask.call_count, 2)
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(data["dialogs"][0]["graph_updates"]["status"], "complete")
        self.box.send_email.assert_called_once()

    def test_manual_enabled_has_two_model_calls(self):
        prompt = self.root / "prompt.json"
        prompt.write_text(json.dumps({"subject": "测试", "prompt": "persona", "summary_prompt": "summary",
                                      "story_extraction_prompt": "extract"}), encoding="utf-8")
        self.box._auth_code = "fake-auth"
        self.bot.ask.side_effect = [ANSWER, output(False, True)]
        with patch.object(proactive, "MailClient", return_value=self.box), patch.object(proactive, "DeepSeekChat", return_value=self.bot):
            proactive.send_deepseek_email("fake", "fake", prompt, memory_path=self.path,
                                          graph_settings={"enabled": True})
        self.assertEqual(self.bot.ask.call_count, 2)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["dialogs"][0]["graph_updates"]["status"], "complete")

    def test_clock_enabled_has_two_model_calls(self):
        self.box.read_user_emails_since.return_value = ((10, 0), [])
        self.bot.ask.side_effect = [ANSWER, output(False, True)]
        receiver = receive.MailReceiver(self.box, self.bot, "persona", memory_path=self.path,
                                        summary_prompt="summary", extraction_prompt="extract",
                                        graph_settings={"enabled": True})
        scheduler = clock.IdleMailScheduler(receiver,
                    {"task": "write", "system_prompt": "persona", "summary_prompt": "summary"},
                    {"idle_min_minutes": 1, "idle_max_minutes": 1, "check_interval_minutes": 1},
                    monotonic=lambda: 1000)
        scheduler.start()
        scheduler.deadline = 0
        scheduler.tick()
        self.assertEqual(self.bot.ask.call_count, 2)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["dialogs"][0]["graph_updates"]["status"], "complete")

    def test_image_observation_is_not_graph_user_evidence(self):
        snapshot = self.memory.snapshot(include_story=True)
        raw = json.loads(combined())
        raw["graph"] = empty_candidate()
        raw["graph"]["entities"] = [{"ref": "P1", "type": "person", "name": "图中人物", "aliases": [],
                                    "target_id": None, "expected_version": None, "source": "user",
                                    "domain": "user_report", "evidence": "IMAGE_ONLY"}]
        self.bot.ask.return_value = json.dumps(raw)
        result = memory.summarize(self.bot, "summary", BODY + " IMAGE_ONLY", ANSWER,
                                  user_evidence={"text": BODY, "images": [{"image_id": "image-1", "text": "IMAGE_ONLY"}]},
                                  story_context=snapshot["story_context"], extraction_prompt="extract",
                                  graph_settings={"enabled": True})
        self.assertEqual(result["graph_candidate"]["reason"], "extraction_failed")

    def test_response_shapes_are_controlled_value_errors(self):
        bot = llm.DeepSeekChat("synthetic")
        for response in (None, "not json", [], {}, SimpleNamespace(choices=None),
                         SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=None)]),
                         SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=123))])):
            with self.subTest(response_type=type(response).__name__):
                bot.client.chat.completions.create.return_value = response
                with self.assertRaises(llm.ResponseProtocolError):
                    bot.ask("synthetic")


if __name__ == "__main__":
    unittest.main()

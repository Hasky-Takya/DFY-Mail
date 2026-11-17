"""Server startup, bounded replay and final-visible graph integration offline."""
import copy
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from test_receive import memory_module as memory, mail_logging, proactive, receive
from test_clock import clock
from test_context_selection import context_with_items
from test_story_integration import BODY, ANSWER, combined
import story_memory as story
from context_selection import select_context, visible_story_ids


class ServerHardeningTests(unittest.TestCase):
    def test_stream_replay_holds_no_past_contexts(self):
        import weakref
        class Tracked(dict):
            pass
        refs = []
        def apply(current, package):
            # Only the immediate current context may survive at the next apply.
            self.assertLessEqual(sum(ref() is not None for ref in refs), 1)
            value = Tracked(revision=current["revision"] + 1,
                            events=[], memories=[], applied_dialog_ids=[])
            refs.append(weakref.ref(value))
            return value
        records = [{"id": str(i), "status": "sent", "story_updates": {
            "dialog_id": str(i), "event_updates": [], "memory_updates": []}} for i in range(400)]
        with patch.object(story, "validate_package"), patch.object(story, "_apply", new=apply):
            final = story.replay_checked({"dialogs": records})
        self.assertEqual(final["revision"], 400)
        self.assertEqual(sum(ref() is not None for ref in refs), 1)

    def test_graph_only_source_is_actually_visible_and_closure_respected(self):
        graph = {"events": [{"id": "E", "version": 1}], "facts": [], "entities": [],
                 "relations": [], "states": [], "event_relations": [
                     {"id": "R", "subject": "E", "object": "OTHER", "support_ref": "E"}]}
        payload, text = select_context({"graph_context": graph}, limit=3000,
                                        render=lambda x: json.dumps(x))
        self.assertEqual(visible_story_ids(payload)["events"], {"E"})
        self.assertEqual(payload["graph_context"]["event_relations"], [])
        self.assertEqual(len(graph["event_relations"]), 1)

    def test_graph_enable_default_snapshot_without_story(self):
        with tempfile.TemporaryDirectory() as directory:
            store = memory.DialogMemory(Path(directory) / "dialog.json", graph_settings={"enabled": True})
            snap = store.snapshot()
            self.assertIn("graph_context", snap)
            self.assertNotIn("story_context", snap)
            self.assertEqual(snap["graph_canonical"]["entities"], [])

    def test_draft_invalidates_when_graph_changes_without_dialog_revision(self):
        old = {"revision": 3, "graph_context": {"generation": "old"}, "graph_settings": {"enabled": True}}
        new = {"revision": 3, "graph_context": {"generation": "new"}, "graph_settings": {"enabled": True}}
        self.assertNotEqual(memory.snapshot_basis(old), memory.snapshot_basis(new))
        scheduler = object.__new__(clock.IdleMailScheduler)
        scheduler.activity_version = 7
        scheduler.draft = {"snapshot": old, "version": 7, "task_id": "old-task", "body": "stale"}
        scheduler.receiver = SimpleNamespace(store=MagicMock(), account="synthetic")
        scheduler.receiver.store.create.return_value = {"id": "new-task"}
        result = scheduler._current_draft(new)
        self.assertEqual(result["snapshot"], new)
        self.assertNotIn("body", result)
        scheduler.receiver.store.finish.assert_called_once_with("old-task")

    def test_new_graph_logger_enters_utf8_file_once(self):
        import logging
        with tempfile.TemporaryDirectory() as directory:
            path = mail_logging.configure_logging(directory, script="dfy_clock.py")
            try:
                logging.getLogger("dfy.graph").warning("图谱恢复测试 code=synthetic_graph")
                for handler in logging.getLogger("dfy").handlers:
                    handler.flush()
                text = path.read_text(encoding="utf-8")
                self.assertEqual(text.count("图谱恢复测试"), 1)
                self.assertIn("dfy.graph", text)
            finally:
                mail_logging.close_logging()

    def test_service_template_uses_one_clock_and_bounded_resources(self):
        template = (Path(__file__).resolve().parents[1] / "deploy/dfy-mail.service.example").read_text(encoding="utf-8")
        self.assertIn("ExecStart=/srv/dfy/.venv/bin/python -B /srv/dfy/src/dfy_clock.py", template)
        self.assertNotIn("dfy_receive.py", template)
        for value in ("StartLimitIntervalSec=300", "StartLimitBurst=3", "MemoryMax=1G", "Restart=on-failure", "UMask=0077"):
            self.assertIn(value, template)

    def test_noninteractive_missing_credentials_fail_all_mail_entries(self):
        settings = {"assistant": {"auth_code_env": "CUSTOM_MAIL_SECRET"}, "graph": {"enabled": True}}
        for module in (clock, receive, proactive):
            for env in ({}, {"DEEPSEEK_API_KEY": "synthetic-key"}):
                with self.subTest(entry=module.__name__, has_api=bool(env)), tempfile.TemporaryDirectory() as directory:
                    with patch.dict(os.environ, env, clear=True), \
                         patch("runtime_config.sys.stdin", SimpleNamespace(isatty=lambda: False)), \
                         patch.object(module, "start_logging"), patch.object(module, "close_logging"), \
                         patch.object(module, "apply_logging_settings"), \
                         patch.object(module, "_load_settings", return_value=copy.deepcopy(settings)), \
                         patch.object(module, "getpass", side_effect=AssertionError("must not prompt")), \
                         patch.object(module, "MailClient") as box, \
                         patch.object(module, "DeepSeekChat") as model:
                        lock = patch.object(module, "SingleInstanceLock", return_value=MagicMock())
                        with lock:
                            if module is proactive:
                                with patch.object(module, "load_prompt"), patch.object(module, "load_summary_prompt"), \
                                     patch.object(module, "load_story_extraction_prompt"), patch("sys.argv", ["dfy_mail.py"]):
                                    with self.assertRaises(SystemExit) as caught:
                                        module.main()
                            elif module is clock:
                                with patch.object(module, "load_clock_prompts", return_value={}):
                                    with self.assertRaises(SystemExit) as caught:
                                        module.main()
                            else:
                                with patch.object(module, "load_reply_prompt"), patch.object(module, "load_summary_prompt"), \
                                     patch.object(module, "load_story_extraction_prompt"):
                                    with self.assertRaises(SystemExit) as caught:
                                        module.main()
                        self.assertEqual(caught.exception.code, 1)
                        box.assert_not_called()
                        model.assert_not_called()


if __name__ == "__main__":
    unittest.main()

"""Preview tests use synthetic events and a fake model; no external requests."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_graph_migration import sent_record
from graph_memory import empty_candidate

SPEC = importlib.util.spec_from_file_location("event_preview_tool", Path(__file__).resolve().parents[1] / "tools/preview_snapshot_events.py")
preview = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(preview)


class FakeBot:
    calls = 0

    def __init__(self, **kwargs):
        pass

    def ask(self, question, **kwargs):
        type(self).calls += 1
        item = json.loads(question)["source"]
        candidate = empty_candidate()
        candidate["facts"] = [{"ref": item["id"], "text": "Alex计划去安静的图书馆。", "category": "event",
                               "source": item["source"], "domain": item["domain"],
                               "evidence": item["source_refs"][-1]["quote"]}]
        return json.dumps(candidate)


class PreviewTests(unittest.TestCase):
    def test_isolated_checkpoint_and_resume_never_touch_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "snapshot" / "event.json"
            source.parent.mkdir()
            _, context = sent_record()
            original = json.dumps({"schema_version": 1, "revision": context["revision"],
                                   "applied_dialog_ids": context["applied_dialog_ids"], "events": context["events"]})
            source.write_text(original, encoding="utf-8")
            output = root / "preview"
            args = ["--source", str(source), "--output", str(output), "--confirm-api"]
            FakeBot.calls = 0
            with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "synthetic-not-a-real-key"}), patch.dict(sys.modules, {"dfy_test": SimpleNamespace(DeepSeekChat=FakeBot)}):
                self.assertEqual(preview.main(args), 0)
                self.assertEqual(preview.main(args + ["--resume"]), 0)
            self.assertEqual(FakeBot.calls, 1)
            self.assertEqual(source.read_text(encoding="utf-8"), original)
            self.assertEqual(list(source.parent.iterdir()), [source])
            summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["completed"], 1)
            self.assertTrue((output / "comparison.md").exists())
            self.assertTrue(json.loads((output / "preview_manifest.json").read_text())["source_unchanged"])

    def test_no_confirmation_prevents_any_model_or_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "preview"
            with self.assertRaises(SystemExit):
                preview.main(["--output", str(output)])
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()

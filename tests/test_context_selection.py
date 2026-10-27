"""Synthetic request selection only: no real memory/config/mailbox/network."""

import copy
import json
import unittest
from unittest.mock import MagicMock, patch

from test_receive import memory_module as memory, story_module as story
from test_conversation_state import assistant, incoming, stamp
from test_story_memory import BODY, event, memory as memory_update, raw


def context_with_items(count=25):
    context = story.empty_context()
    for index in range(count):
        updates = story.validate_updates(raw([event()], [memory_update()]),
                                         story_context=context, user_body=BODY, assistant_body=BODY)
        package = story.materialize_updates(updates, dialog_id=f"dialog-{index}", story_context=context,
                                           observed_at=stamp(index), user_message_id="<u@test>",
                                           assistant_message_id="<a@test>")
        context = story._apply(context, package)
    return context


def summary_output(updates=None):
    return json.dumps({"user_summary": "User summary", "assistant_summary": "Assistant summary",
                       **(raw() if updates is None else updates)})


class ContextSelectionTests(unittest.TestCase):
    def setUp(self):
        self.store = memory.DialogMemory("unused-test-path")

    def question_payload(self, history, current="CURRENT", **kwargs):
        text = self.store.question({"dialogs": history}, current, as_of=stamp(200), **kwargs)
        return json.loads(text.split("\n", 1)[1]), text

    def test_recent_twenty_each_sorted_no_io_no_mutation(self):
        context = context_with_items()
        history = [assistant(index, f"<a{index}@test>") for index in range(30)]
        before = copy.deepcopy((history, context))
        with patch.object(memory.Path, "open", side_effect=AssertionError("No IO")):
            payload, _ = self.question_payload(list(reversed(history)), story_context=context)
        self.assertEqual([item["id"] for item in payload["history"]], [item["id"] for item in history[-20:]])
        self.assertEqual(payload["selection"]["dialogs"], {"total": 30, "selected": 20, "omitted": 10})
        for kind in ("events", "memories"):
            self.assertEqual(len(payload["story_context"][kind]), 20)
            self.assertEqual(payload["selection"][kind], {"total": 25, "selected": 20, "omitted": 5})
        self.assertNotIn("applied_dialog_ids", payload["story_context"])
        self.assertEqual((history, context), before)

    def test_budget_discards_oldest_and_keeps_current_exact(self):
        history = [assistant(index, f"<a{index}@test>") for index in range(8)]
        for index, record in enumerate(history):
            record["assistant_summary"] = str(index) + "x" * 1000
        current = "用户原始正文\n" * 100
        with patch.object(memory, "CONTEXT_LIMIT", 6500):
            payload, text = self.question_payload(history, current)
        self.assertLessEqual(len(text), 6500)
        self.assertEqual(payload["current"], current)
        selected = payload["selection"]["dialogs"]["selected"]
        self.assertGreater(selected, 0)
        self.assertLess(selected, 8)
        self.assertEqual([item["id"] for item in payload["history"]], [item["id"] for item in history[-selected:]])
        self.assertEqual(len(history), 8)

    def test_required_content_exceeds_budget_without_model_call(self):
        bot = MagicMock()
        for body_key in ("user", "assistant"):
            with self.subTest(body=body_key), patch.object(memory, "CONTEXT_LIMIT", 3000):
                with self.assertRaises(memory.ContextBudgetError):
                    memory.summarize(bot, "summary", "x" * 4000 if body_key == "user" else "u",
                                     "x" * 4000 if body_key == "assistant" else "a")
        bot.ask.assert_not_called()
        with patch.object(memory, "CONTEXT_LIMIT", 3000), self.assertRaises(memory.ContextBudgetError):
            self.question_payload([], "x" * 4000)
        self.assertTrue(issubclass(memory.ContextBudgetError, memory.MemoryError))

    def test_question_system_prompt_counts_toward_budget(self):
        with patch.object(memory, "CONTEXT_LIMIT", 3000), self.assertRaises(memory.ContextBudgetError):
            self.question_payload([], system_prompt="system" * 600)

    def test_summarize_accepts_full_history_and_strips_packages(self):
        bot = MagicMock()
        bot.ask.return_value = summary_output()
        history = [assistant(index, f"<a{index}@test>") for index in range(30)]
        for record in history:
            record["story_updates"] = {"private-persisted-package": "x" * 10000}
        context = context_with_items()
        original = copy.deepcopy((history, context))
        result = memory.summarize(bot, "summary", BODY, BODY, story_context=context,
                                  extraction_prompt="extract", history=list(reversed(history)))
        payload = json.loads(bot.ask.call_args.args[0])
        self.assertEqual(len(payload["history"]), 20)
        self.assertNotIn("story_updates", payload["history"][0])
        self.assertIs(result["story_candidate"]["context"], context)
        self.assertEqual(len(result["story_candidate"]["context"]["events"]), 25)
        self.assertEqual((history, context), original)

    def test_newest_updated_not_created_and_full_refs_materialize(self):
        context = context_with_items()
        old_event = context["events"][0]
        target = old_event["id"]
        updates = story.validate_updates(raw([event(target_id=target)]), story_context=context,
                                         user_body=BODY, assistant_body=BODY)
        package = story.materialize_updates(updates, dialog_id="refresh", story_context=context,
                                           observed_at=stamp(50), user_message_id="<u@test>",
                                           assistant_message_id="<a@test>")
        context = story._apply(context, package)
        bot = MagicMock()
        bot.ask.return_value = summary_output(raw([event(target_id=target)]))
        result = memory.summarize(bot, "summary", BODY, BODY, story_context=context, extraction_prompt="extract")
        visible = json.loads(bot.ask.call_args.args[0])["story_context"]["events"]
        self.assertEqual(visible[-1]["id"], target)
        self.assertEqual(visible[-1]["version"], 2)
        self.assertEqual(len(visible[-1]["source_refs"]), 1)
        self.assertEqual(visible[-1]["origin"], old_event["origin"])
        result["story_candidate"]["observed_at"] = stamp(60)
        record = memory.new_record({"dialogs": []}, incoming(55), result)
        self.assertEqual(record["story_updates"]["event_updates"][0]["version"], 3)
        self.assertEqual(record["story_updates"]["event_updates"][0]["source_refs"][:-1],
                         context["events"][0]["source_refs"])
        self.assertEqual(len(record["story_updates"]["event_updates"][0]["source_refs"]), 3)

    def test_hidden_target_and_related_event_rejected(self):
        context = context_with_items()
        hidden_event = context["events"][0]["id"]
        hidden_memory = context["memories"][0]
        cases = [
            (raw([event(target_id=hidden_event)]), "unknown_target"),
            (raw(memories=[memory_update(target_id=hidden_memory["id"], origin=hidden_memory["origin"])]), "unknown_target"),
            (raw(memories=[memory_update(related_event_ids=[hidden_event])]), "unknown_related_event"),
        ]
        bot = MagicMock()
        for updates, code in cases:
            bot.ask.return_value = summary_output(updates)
            with self.subTest(code=code), self.assertRaises(memory.SummaryError) as caught:
                memory.summarize(bot, "summary", BODY, BODY, story_context=context, extraction_prompt="extract")
            self.assertEqual(caught.exception.details["code"], code)

    def test_budget_removed_target_rejected_after_full_validation(self):
        context = context_with_items(2)
        for item in context["events"]:
            item["content"] = "x" * 4000
        bot = MagicMock()
        bot.ask.return_value = summary_output(raw([event(target_id=context["events"][0]["id"])]))
        with patch.object(memory, "CONTEXT_LIMIT", 6000), self.assertRaises(memory.SummaryError) as caught:
            memory.summarize(bot, "summary", BODY, BODY, story_context=context, extraction_prompt="extract")
        self.assertEqual(caught.exception.details["code"], "unknown_target")
        self.assertLess(len(json.loads(bot.ask.call_args.args[0])["story_context"]["events"]), 2)

    def test_invalid_hidden_canonical_item_still_stops_before_model(self):
        context = context_with_items()
        context["events"][0]["source_refs"] = []
        bot = MagicMock()
        with self.assertRaises(memory.MemoryError):
            memory.summarize(bot, "summary", BODY, BODY, story_context=context, extraction_prompt="extract")
        bot.ask.assert_not_called()
        with self.assertRaises(memory.MemoryError):
            self.question_payload([], story_context=context)

    def test_state_calculated_on_full_history_but_lists_compact(self):
        prior = assistant(1)
        activities = [incoming(index + 2, message_id=f"<u{index}@test>",
                               in_reply_to=prior["assistant_message_id"]) for index in range(50)]
        activities[0]["received_at"] = stamp(100)
        full = memory.conversation_state([prior], as_of=stamp(200), observed_user_activity=activities)
        payload, _ = self.question_payload([prior], observed_user_activity=activities)
        state = payload["conversation_state"]
        for key in ("after_latest_assistant", "elapsed", "latest_known_user_activity", "consecutive_proactive_without_observed_user"):
            self.assertEqual(state[key], full[key])
        for key in ("known_reply_relations", "observed_user_activity"):
            self.assertEqual(len(state[key]), 20)
            self.assertEqual(payload["selection"][key]["omitted"], 30)
            self.assertEqual(state[key][-1]["received_at"], stamp(100))

    def test_repair_error_preserved_previous_output_omitted_when_needed(self):
        bot = MagicMock()
        bot.ask.return_value = summary_output()
        feedback = {"error": {"code": "invalid_fields", "path": "$", "hint": "Use template"},
                    "previous_output": "sensitive-old-output" * 2000}
        original = copy.deepcopy(feedback)
        with patch.object(memory, "CONTEXT_LIMIT", 5000), self.assertLogs("dfy.memory", level="INFO") as logs:
            memory.summarize(bot, "summary", BODY, BODY, story_context=story.empty_context(),
                             extraction_prompt="extract", validation_feedback=feedback)
        payload = json.loads(bot.ask.call_args.args[0])
        self.assertEqual(payload["repair"], {"error": feedback["error"]})
        self.assertTrue(payload["selection"]["repair_previous_output_omitted"])
        self.assertNotIn("sensitive-old-output", " ".join(logs.output))
        self.assertNotIn(BODY, " ".join(logs.output))
        self.assertEqual(feedback, original)
        self.assertLessEqual(len(bot.ask.call_args.args[0]) + len(bot.ask.call_args.kwargs["system_prompt"]), 5000)

    def test_legacy_no_story_question_and_summary(self):
        payload, _ = self.question_payload([])
        self.assertNotIn("story_context", payload)
        bot = MagicMock()
        bot.ask.return_value = json.dumps({"user_summary": "u", "assistant_summary": "a"})
        result = memory.summarize(bot, "summary", "user", "assistant")
        self.assertEqual(result, {"user_summary": "u", "assistant_summary": "a"})
        self.assertNotIn("story_context", json.loads(bot.ask.call_args.args[0]))


if __name__ == "__main__":
    unittest.main()

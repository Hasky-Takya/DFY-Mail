"""Conversation-state core: synthetic metadata only; no mailbox, config or network."""

import copy
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "conversation_state_memory", Path(__file__).resolve().parents[1] / "src" / "dialog_memory.py"
)
memory = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(memory)


def stamp(seconds):
    return f"2026-10-25T00:{seconds // 60:02}:{seconds % 60:02}+00:00"


def incoming(seconds=20, **kwargs):
    return {"received_at": stamp(seconds), "received_at_source": "imap_internaldate",
            "message_id": "<user@example.test>", **kwargs}


def assistant(seconds=10, identifier="<assistant@example.test>", message=None):
    record = memory.new_record({"dialogs": []}, message, {
        "user_summary": "User summary" if message else None,
        "assistant_summary": "Assistant summary",
    })
    record.update(id=identifier, assistant_message_id=identifier, status="sent", sent_at=stamp(seconds))
    return record


def observation(**kwargs):
    return {"started_at": stamp(0), "baseline": [10, 0], "sync_ok": True,
            "last_successful_sync": stamp(30), "coverage_through": stamp(30),
            "history_may_be_incomplete": True, **kwargs}


def state(history=None, **kwargs):
    return memory.conversation_state([assistant()] if history is None else history,
                                     as_of=stamp(40), **kwargs)


def after(**kwargs):
    return state(**kwargs)["after_latest_assistant"]


class ReplyTests(unittest.TestCase):
    def test_old_reply_preserves_three_way_classification(self):
        old, latest = assistant(), assistant(15, "<latest@example.test>")
        msg = incoming(in_reply_to=old["assistant_message_id"])
        self.assertEqual(memory.resolve_reply([old, latest], msg), (old["assistant_message_id"], "in_reply_to"))
        self.assertEqual(memory.classify_dialog([old, latest], msg), "用户主动发送")
        record = memory.new_record({"dialogs": [old, latest]}, msg,
                                   {"user_summary": "u", "assistant_summary": "a"})
        self.assertEqual(record["reply_to_assistant_message_id"], old["assistant_message_id"])
        self.assertEqual(record["reply_relation_source"], "in_reply_to")

    def test_strict_parent_never_falls_back(self):
        record = assistant()
        for parent in ("", "bad", "<missing@example.test>", 5, [],
                       "<assistant@example.test> <extra@example.test>"):
            with self.subTest(parent=parent):
                self.assertEqual(memory.resolve_reply([record], incoming(
                    in_reply_to=parent, references=record["assistant_message_id"])), (None, None))

    def test_references_only_last_id_and_none_parent(self):
        record = assistant()
        target = record["assistant_message_id"]
        for parent in ({}, {"in_reply_to": None}):
            self.assertEqual(memory.resolve_reply([record], incoming(references="<x@example.test> " + target, **parent)),
                             (target, "references"))
            self.assertEqual(memory.resolve_reply([record], incoming(references=target + " <unknown@example.test>", **parent)),
                             (None, None))
        self.assertEqual(memory.resolve_reply([record], None), (None, None))

    def test_sent_candidates_and_original_seconds_upper(self):
        record = assistant()
        record["sent_at"] = "2026-10-25T00:00:10.500000+00:00"
        msg = incoming(10, in_reply_to=record["assistant_message_id"])
        self.assertEqual(memory.resolve_reply([record], msg)[0], record["assistant_message_id"])
        self.assertEqual(memory.classify_dialog([record], msg), "用户回复上一封邮件")
        msg["received_at_source"] = "observed_at"
        self.assertEqual(memory.resolve_reply([record], msg), (None, None))
        msg["received_at_source"] = "imap_internaldate"
        for status in ("prepared", "sending", "unknown"):
            record["status"] = status
            self.assertEqual(memory.resolve_reply([record], msg), (None, None))
        record.update(status="sent", sent_at=stamp(11))
        self.assertEqual(memory.resolve_reply([record], msg), (None, None))


class SchemaTests(unittest.TestCase):
    def validate(self, records):
        return memory.DialogMemory._validate({"schema_version": 1, "revision": 0, "dialogs": records})

    def test_legacy_schema_read_without_migration_or_inference(self):
        old = assistant(30, message=incoming())
        old["type"] = "用户回复上一封邮件"
        for key in memory._RELATION_FIELDS:
            del old[key]
        original = copy.deepcopy(old)
        self.validate([old])
        result = state([old])
        self.assertEqual(old, original)
        self.assertEqual(result["known_reply_relations"], [])
        self.assertIsNone(result["last_recorded_user"]["reply_to_assistant_message_id"])

    def test_new_relation_roundtrip_and_optional_null(self):
        prior = assistant()
        reply = assistant(30, "<reply@example.test>", incoming(in_reply_to=prior["assistant_message_id"]))
        reply.update(reply_to_assistant_message_id=prior["assistant_message_id"], reply_relation_source="in_reply_to")
        self.validate([prior, reply])
        self.assertEqual(state([prior, reply])["known_reply_relations"][0]["reply_relation_source"], "in_reply_to")
        self.validate([assistant()])
        self.assertIsNone(assistant()["reply_to_assistant_message_id"])

    def test_invalid_relation_field_types_target_time_and_source(self):
        prior = assistant()
        reply = assistant(30, "<reply@example.test>", incoming())
        reply.update(reply_to_assistant_message_id=prior["assistant_message_id"], reply_relation_source="references")
        for target, source in ((5, "references"), ([], "references"), (None, "references"),
                               (prior["assistant_message_id"], None),
                               (prior["assistant_message_id"], "guessed"),
                               (prior["assistant_message_id"], []),
                               ("<missing@example.test>", "in_reply_to")):
            bad = {**reply, "reply_to_assistant_message_id": target, "reply_relation_source": source}
            with self.subTest(target=target, source=source), self.assertRaises(memory.MemoryError):
                self.validate([prior, bad])
        for key in memory._RELATION_FIELDS:
            bad = dict(reply)
            del bad[key]
            with self.assertRaises(memory.MemoryError):
                self.validate([prior, bad])
        for change in ({"sent_at": stamp(21)}, {"status": "unknown", "sent_at": None}):
            with self.assertRaises(memory.MemoryError):
                self.validate([{**prior, **change}, reply])
        with self.assertRaises(memory.MemoryError):
            self.validate([{**prior, "reply_to_assistant_message_id": prior["assistant_message_id"],
                            "reply_relation_source": "in_reply_to"}])
        with self.assertRaises(memory.MemoryError):
            self.validate([prior, {**reply, "reply_to_assistant_message_id": reply["assistant_message_id"]}])


class ConversationTests(unittest.TestCase):
    def test_manual_and_no_history_are_unknown(self):
        for mode in (None, "manual_proactive", "scheduled_proactive"):
            result = state(mode=mode)
            self.assertEqual(result["after_latest_assistant"]["contact"], "unknown")
            self.assertIsNone(result["after_latest_assistant"]["waiting_seconds"])
            self.assertEqual(result["consecutive_proactive_without_observed_user"], 0)
        self.assertIsNone(state([])["last_recorded_assistant"])

    def test_success_interval_and_program_elapsed(self):
        result = state(observation=observation())
        self.assertEqual(result["after_latest_assistant"], {
            "contact": "not_observed_in_available_data", "reply_relation": "未知", "waiting_seconds": 20.0})
        self.assertEqual(result["elapsed"]["since_last_recorded_assistant_seconds"], 30.0)
        self.assertEqual(result["consecutive_proactive_without_observed_user"], 1)

    def test_missing_or_invalid_interval_never_claims_absence(self):
        for change in ({"sync_ok": False}, {"sync_ok": None}, {"started_at": stamp(11)},
                       {"coverage_through": stamp(9)}, {"coverage_through": stamp(41)},
                       {"coverage_through": None}, {"started_at": None}):
            with self.subTest(change=change):
                self.assertEqual(after(observation=observation(**change))["contact"], "unknown")

    def test_current_unpersisted_counts_during_failed_sync(self):
        prior = assistant()
        result = state(current_message=incoming(in_reply_to=prior["assistant_message_id"]),
                       observation=observation(sync_ok=False), mode="reply")
        self.assertEqual(result["after_latest_assistant"], {
            "contact": "observed", "reply_relation": "确证", "waiting_seconds": None})
        self.assertEqual(result["current_event"]["kind"], "user_message")
        self.assertEqual(result["latest_known_user_activity"]["user_message_id"], "<user@example.test>")
        self.assertEqual(result["elapsed"]["since_latest_known_user_activity_seconds"], 20)

    def test_activity_dict_list_none_and_empty_body_are_equivalent(self):
        event = incoming(body="", in_reply_to="invalid")
        for activity in (event, [event]):
            result = state(observed_user_activity=activity, observation=observation(sync_ok=False))
            self.assertEqual(result["after_latest_assistant"]["contact"], "observed")
            self.assertEqual(result["after_latest_assistant"]["reply_relation"], "未知")
        self.assertEqual(state(observed_user_activity=None), state(observed_user_activity=[]))

    def test_old_reply_contact_not_reply_to_latest(self):
        old, recent = assistant(5, "<old@example.test>"), assistant()
        result = state([old, recent], current_message=incoming(in_reply_to=old["assistant_message_id"]))
        self.assertEqual(result["after_latest_assistant"]["contact"], "observed")
        self.assertEqual(result["after_latest_assistant"]["reply_relation"], "未知")
        self.assertEqual(result["known_reply_relations"][0]["reply_to_assistant_message_id"], old["assistant_message_id"])

    def test_current_and_observed_internaldate_known_during_receipt_second(self):
        prior = assistant(0)
        msg = incoming(60, in_reply_to=prior["assistant_message_id"])
        for kwargs in ({"current_message": msg}, {"observed_user_activity": [msg]}):
            with self.subTest(kwargs=kwargs):
                result = memory.conversation_state(
                    [prior], as_of="2026-10-25T00:01:00.200000+00:00", **kwargs)
                self.assertEqual(result["after_latest_assistant"]["contact"], "observed")
                self.assertEqual(result["after_latest_assistant"]["reply_relation"], "确证")
                self.assertEqual(result["latest_known_user_activity"]["received_at"], stamp(60))
                self.assertEqual(len(result["known_reply_relations"]), 1)
                self.assertEqual(result["elapsed"]["since_latest_known_user_activity_seconds"], 0.2)

    def test_same_second_unknown_contact_but_exact_header_relation(self):
        prior = assistant()
        for source in ("imap_internaldate", "observed_at"):
            result = state(current_message=incoming(10, received_at_source=source,
                           in_reply_to=prior["assistant_message_id"]), observation=observation())
            self.assertEqual(result["after_latest_assistant"]["contact"], "unknown")
            self.assertEqual(result["after_latest_assistant"]["reply_relation"], "确证")
            self.assertIsNone(result["after_latest_assistant"]["waiting_seconds"])
        prior["sent_at"] = "2026-10-25T00:00:10.500000+00:00"
        result = state([prior], current_message=incoming(10, in_reply_to=prior["assistant_message_id"]), observation=observation())
        self.assertEqual(result["after_latest_assistant"]["contact"], "unknown")
        self.assertEqual(result["after_latest_assistant"]["reply_relation"], "确证")

    def test_future_user_and_future_assistant_do_not_make_negative_durations(self):
        result = state(current_message=incoming(50), observation=observation())
        self.assertEqual(result["after_latest_assistant"]["contact"], "unknown")
        self.assertIsNone(result["latest_known_user_activity"])
        result = state([assistant(50)], current_message=incoming(55), observation=observation())
        self.assertIsNone(result["elapsed"]["since_last_recorded_assistant_seconds"])
        self.assertIsNone(result["after_latest_assistant"]["waiting_seconds"])
        self.assertEqual(result["after_latest_assistant"]["contact"], "unknown")

    def test_positive_evidence_beats_failure_future_other_and_uncertainty(self):
        result = state(observed_user_activity=[incoming(), incoming(50), incoming(10)],
                       observation=observation(sync_ok=False))
        self.assertEqual(result["after_latest_assistant"]["contact"], "observed")

    def test_older_known_activity_does_not_block_covered_absence(self):
        result = state(observed_user_activity=incoming(5), observation=observation())
        self.assertEqual(result["after_latest_assistant"]["contact"], "not_observed_in_available_data")
        self.assertEqual(result["latest_known_user_activity"]["received_at"], stamp(5))

    def test_observed_at_is_not_rewritten_to_arrival(self):
        event = {"observed_at": stamp(20), "message_id": "<seen@example.test>"}
        result = state(observed_user_activity=event)
        self.assertEqual(result["after_latest_assistant"]["contact"], "observed")
        self.assertIsNone(result["latest_known_user_activity"]["received_at"])
        self.assertEqual(result["latest_known_user_activity"]["observed_at"], stamp(20))
        self.assertIn("不保证已读、送达或用户情绪", result["scope"])
        self.assertIn("不是精确到达时间", result["scope"])

    def test_proactive_streak_is_only_supported_covered_suffix(self):
        records = [assistant(5, "<a@example.test>"), assistant(10, "<b@example.test>")]
        result = state(records, observation=observation())
        self.assertEqual(result["consecutive_proactive_without_observed_user"], 2)
        result = state(records, observation=observation(started_at=stamp(8)))
        self.assertEqual(result["consecutive_proactive_without_observed_user"], 1)
        result = state(records, observation=observation(), current_message=incoming())
        self.assertEqual(result["consecutive_proactive_without_observed_user"], 0)

    def test_unknown_source_without_time_stays_unknown(self):
        event = {"received_at": None, "received_at_source": "unknown"}
        result = state(observed_user_activity=event, observation=observation())
        self.assertEqual(result["after_latest_assistant"]["contact"], "unknown")
        self.assertIsNone(result["latest_known_user_activity"])
        self.assertEqual(result["observed_user_activity"][0]["received_at_source"], "unknown")

    def test_current_classification_and_proactive_modes(self):
        result = state(current_message=incoming(in_reply_to="<assistant@example.test>"))
        self.assertEqual(result["current_event"]["type"], "用户回复上一封邮件")
        self.assertEqual(result["current_event"]["initiated_by"], "user")
        for mode in ("manual_proactive", "scheduled_proactive"):
            event = state(mode=mode)["current_event"]
            self.assertEqual(event["type"], "deepseek自己主动发送")
            self.assertEqual(event["initiated_by"], "assistant")

    def test_future_successful_sync_is_unknown_even_if_coverage_is_past(self):
        result = state(observation=observation(last_successful_sync=stamp(50)))
        self.assertEqual(result["after_latest_assistant"]["contact"], "unknown")
        result = state(observed_user_activity=incoming(observed_at=stamp(50)), observation=observation())
        self.assertEqual(result["after_latest_assistant"]["contact"], "unknown")
        self.assertIsNone(result["latest_known_user_activity"])

    def test_purity_no_io_and_no_mutation(self):
        history = [assistant()]
        event, obs = incoming(), observation()
        original = copy.deepcopy((history, event, obs))
        with patch.object(Path, "open", side_effect=AssertionError("unexpected IO")):
            state(history, current_message=event, observed_user_activity=[event], observation=obs)
        self.assertEqual((history, event, obs), original)

    def test_input_validation_is_offline(self):
        for kwargs in ({"observed_user_activity": "bad"}, {"observed_user_activity": [None]},
                       {"observation": {"sync_ok": 1}}, {"observation": {"baseline": [1]}},
                       {"observation": {"started_at": "bad"}},
                       {"current_message": {"received_at": stamp(20)}},
                       {"observed_user_activity": {"uid": []}}):
            with self.subTest(kwargs=kwargs), self.assertRaises(memory.MemoryError):
                state(**kwargs)


class QuestionTests(unittest.TestCase):
    def setUp(self):
        self.store = memory.DialogMemory("unused-memory-path", secrets=("private-key",))
        self.snapshot = {"schema_version": 1, "revision": 0, "dialogs": [assistant()]}

    def payload(self, **kwargs):
        return json.loads(self.store.question(self.snapshot, "Current body", as_of=stamp(40), **kwargs).split("\n", 1)[1])

    def test_old_two_position_arguments_and_enriched_history(self):
        text = self.store.question(self.snapshot, "Current body")
        self.assertIn("Current body", text)
        history = self.payload()["history"][0]
        for key in ("id", "initiated_by", "received_at_source", "assistant_message_id", "user_message_id"):
            self.assertIn(key, history)
        self.assertEqual(history["initiated_by"], "assistant")

    def test_metadata_whitelist_redaction_and_no_mutation(self):
        event = incoming(in_reply_to="<assistant@example.test>", body="RAW PRIVATE BODY",
                         subject="PRIVATE SUBJECT", from_address="PRIVATE ADDRESS",
                         metadata={"password": "PRIVATE METADATA"}, uid="private-key-uid")
        obs = observation(secret="PRIVATE OBSERVATION", baseline=["private-key", 0])
        before = copy.deepcopy((self.snapshot, event, obs))
        text = self.store.question(self.snapshot, "Current body", current_message=event,
                                   observed_user_activity=[event], observation=obs,
                                   mode="reply", as_of=stamp(40))
        self.assertNotIn("private-key", text)
        self.assertIn("[凭据已隐藏]", text)
        for secret in ("RAW PRIVATE BODY", "PRIVATE SUBJECT", "PRIVATE ADDRESS", "PRIVATE METADATA", "PRIVATE OBSERVATION"):
            self.assertNotIn(secret, text)
        payload = json.loads(text.split("\n", 1)[1])
        self.assertNotIn("in_reply_to", payload["conversation_state"]["current_event"])
        self.assertNotIn("references", payload["conversation_state"]["current_event"])
        self.assertEqual((self.snapshot, event, obs), before)

    def test_metadata_counts_toward_budget_and_optional_history_can_be_omitted(self):
        original = copy.deepcopy(self.snapshot)
        with self.assertRaises(memory.MemoryError):
            self.store.question(self.snapshot, "short", observed_user_activity=incoming(uid="x" * 60000), as_of=stamp(40))
        self.assertEqual(self.snapshot, original)
        text = self.store.question(self.snapshot, "short", as_of=stamp(40))
        with patch.object(memory, "CONTEXT_LIMIT", len(text)):
            bounded = self.store.question(self.snapshot, "short", as_of=stamp(40))
            self.assertLessEqual(len(bounded), len(text))
        with patch.object(memory, "CONTEXT_LIMIT", len(text) - 100):
            bounded = self.store.question(self.snapshot, "short", as_of=stamp(40))
            self.assertLessEqual(len(bounded), len(text) - 100)
            payload = json.loads(bounded.split("\n", 1)[1])
            self.assertEqual(payload["history"], [])
            self.assertEqual(payload["current"], "short")
            self.assertEqual(payload["selection"]["dialogs"]["omitted"], 1)
        with patch.object(memory, "CONTEXT_LIMIT", 100):
            with self.assertRaises(memory.ContextBudgetError):
                self.store.question(self.snapshot, "CURRENT" * 100, as_of=stamp(40))
        self.assertEqual(self.snapshot, original)


if __name__ == "__main__":
    unittest.main()

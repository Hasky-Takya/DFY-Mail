"""Story tests use only synthetic values and temporary directories, never the app."""

import copy
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "story_memory_under_test", Path(__file__).resolve().parents[1] / "src" / "story_memory.py"
)
story = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(story)
AT = "2026-10-25T08:00:00+08:00"
LATER = "2026-10-26T08:00:00+08:00"
BODY = "We planned tomorrow's walk. I prefer quiet places. secret-credential"


def event(**changes):
    value = {"target_id": None, "title": "Walk", "content": "Plan a walk",
             "source": "user", "domain": "shared_story", "importance": 3,
             "people": ["User"], "status": "planned", "tense": "future",
             "event_time": {"raw": "tomorrow", "start": None, "end": None},
             "evidence": "planned tomorrow's walk"}
    value.update(changes)
    return value


def memory(**changes):
    value = {"target_id": None, "content": "Prefers quiet places", "source": "user",
             "domain": "user_preference", "importance": 2, "people": [],
             "category": "preference", "status": "active", "origin": "reported_preference",
             "evidence": "I prefer quiet places."}
    value.update(changes)
    return value


def raw(events=None, memories=None):
    return {"event_updates": [] if events is None else events,
            "memory_updates": [] if memories is None else memories}


def validate(value, context=None, **kwargs):
    args = dict(story_context=story.empty_context() if context is None else context,
                user_body=BODY, assistant_body=BODY)
    args.update(kwargs)
    return story.validate_updates(value, **args)


def package(value=None, context=None, identifier="dialog-1", at=AT):
    context = story.empty_context() if context is None else context
    value = raw([event()], [memory()]) if value is None else value
    return story.materialize_updates(validate(value, context), dialog_id=identifier,
                                     story_context=context, observed_at=at,
                                     user_message_id="<user@example.test>",
                                     assistant_message_id="<assistant@example.test>")


def record(pkg, status="sent"):
    return {"id": pkg["dialog_id"], "status": status, "story_updates": pkg,
            "user_message_id": "<user@example.test>", "assistant_message_id": "<assistant@example.test>"}


class StoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)

    def reconcile(self, records):
        return story.reconcile_locked({"dialogs": records}, self.directory)

    def disk(self, filename):
        return json.loads((self.directory / filename).read_text(encoding="utf-8"))

    def test_empty_context_is_fresh(self):
        one = story.empty_context()
        one["events"].append({})
        self.assertEqual(story.empty_context(), {"revision": 0, "applied_dialog_ids": [], "events": [], "memories": []})

    def test_exact_model_root_and_fields(self):
        values = [None, [], {}, {**raw(), "reason": "extra"}, raw([{"target_id": None}]),
                  raw([{**event(), "id": "injected"}]), raw([event(event_time={"raw": None})]),
                  raw(memories=[{**memory(), "title": "extra"}])]
        for value in values:
            with self.subTest(value=value), self.assertRaises(story.StoryValidationError):
                validate(value)
        self.assertEqual(validate(json.dumps(raw())), raw())
        self.assertEqual(validate(raw(memories=[memory()]))["memory_updates"][0]["related_event_ids"], [])

    def test_json_duplicate_and_nonfinite_rejected(self):
        for text in ('{"event_updates":[],"event_updates":[],"memory_updates":[]}',
                     '{"event_updates":NaN,"memory_updates":[]}'):
            with self.assertRaises(story.StoryValidationError):
                validate(text)

    def test_enums_types_lengths_limits(self):
        changes = [{"importance": True}, {"importance": 0}, {"importance": 6}, {"importance": 1.0},
                   {"source": "assistant"}, {"source": []}, {"domain": "fiction"},
                   {"status": "active"}, {"tense": "yesterday"}, {"people": "person"},
                   {"people": ["x", "x"]}, {"people": ["x"] * 33},
                   {"content": "x" * 4001}, {"title": " "}, {"evidence": "x" * 1001}]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(story.StoryValidationError):
                validate(raw([event(**change)]))
        for value in (raw([event()] * 33), raw(memories=[memory(status="planned")]),
                      raw(memories=[memory(related_event_ids=["missing"])])):
            with self.assertRaises(story.StoryValidationError):
                validate(value)

    def test_domain_is_bound_to_evidence_source_at_every_validation_boundary(self):
        for domain, source in (("user_report", "llm"), ("user_preference", "llm"),
                               ("assistant_statement", "user")):
            with self.subTest(domain=domain, source=source):
                with self.assertRaises(story.StoryValidationError):
                    validate(raw([event(domain=domain, source=source)]))
                pkg = package()
                pkg["event_updates"][0].update(domain=domain, source=source)
                with self.assertRaises(story.StoryValidationError):
                    story.validate_package(pkg)
        for domain, source in (("user_report", "user"), ("user_preference", "user"),
                               ("assistant_statement", "llm"), ("uncertain", "llm"),
                               ("shared_story", "llm")):
            validate(raw([event(domain=domain, source=source)]))
        with self.assertRaises(story.StoryValidationError):
            validate(raw(memories=[memory(source="llm")]))

    def test_quote_must_be_exact_in_selected_current_body(self):
        for kwargs in ({"user_body": None}, {"user_body": "other turn"}):
            with self.assertRaises(story.StoryValidationError):
                validate(raw([event()]), **kwargs)
        with self.assertRaises(story.StoryValidationError):
            validate(raw([event(source="llm")]), assistant_body="different")
        with self.assertRaises(story.StoryValidationError):
            validate(raw([event(evidence="PLANNED tomorrow's walk")]))
        validate(raw([event(source="llm")]), user_body=None)

    def test_time_iso_timezone_range_and_program_anchor(self):
        validate(raw([event(event_time={"raw": "tomorrow", "start": LATER, "end": None})]),
                 anchors={"as_of": AT, "received_at": None, "received_at_source": "unknown"})
        for value in ({"raw": "tomorrow", "start": "2026-10-26T08:00:00", "end": None},
                      {"raw": "tomorrow", "start": LATER, "end": AT},
                      {"raw": None, "start": AT, "end": None},
                      {"raw": "last year", "start": None, "end": None},
                      {"raw": "tomorrow", "start": None, "end": None, "anchor": AT}):
            with self.subTest(value=value), self.assertRaises(story.StoryValidationError):
                validate(raw([event(event_time=value)]))
        with self.assertRaises(story.StoryValidationError):
            validate(raw(), anchors={"as_of": "not a timestamp"})
        pkg = package(raw([event(tense="past", status="ongoing")]))
        self.assertEqual(pkg["event_updates"][0]["status"], "ongoing")
        self.assertEqual(pkg["event_updates"][0]["event_time"]["anchor"], AT)

    def test_timestamp_requires_iso_separator_and_valid_numeric_offset(self):
        for stamp in ("2026-10-26X08:00:00+08:00", "2026-10-26 08:00:00+08:00",
                      "20261026T080000+0800", "2026-W43-1T08:00:00+08:00",
                      "2026-10-26T08:00:00+08:99", "2026-10-26T08:00:00+24:00",
                      "2026-02-30T08:00:00Z"):
            with self.subTest(stamp=stamp), self.assertRaises(story.StoryValidationError):
                validate(raw(), anchors={"as_of": stamp})
        for stamp in ("2026-10-26T00:00:00Z", "2026-10-26T00:00+00:00",
                      "2026-10-26T00:00:00.123456+00:00"):
            validate(raw(), anchors={"as_of": stamp})

    def test_redaction_after_evidence_validation_in_all_text(self):
        ev = event(title="secret-credential", content="secret-credential", people=["secret-credential"],
                   evidence="secret-credential")
        mem = memory(content="secret-credential", origin="secret-credential", evidence="secret-credential")
        validated = validate(raw([ev], [mem]), secrets=("secret-credential",))
        self.assertNotIn("secret-credential", json.dumps(validated))
        self.assertEqual(validated["event_updates"][0]["evidence"], "[REDACTED]")
        with self.assertRaises(story.StoryValidationError):
            validate(raw([event(evidence="[REDACTED]")]), secrets=("secret-credential",))
        pkg = story.materialize_updates(validated, dialog_id="clean", story_context=story.empty_context(),
                                        observed_at=AT, user_message_id=None, assistant_message_id="<a@test>")
        self.assertNotIn("secret-credential", json.dumps(pkg))
        rec = record(pkg)
        rec.update(user_message_id=None, assistant_message_id="<a@test>")
        context = self.reconcile([rec])
        self.assertNotIn("secret-credential", json.dumps(context))

    def test_materialization_deterministic_immutable_and_owned_fields(self):
        value, context = raw([event()], [memory()]), story.empty_context()
        original = copy.deepcopy(value)
        one, two = package(value, context), package(value, context)
        self.assertEqual(one, two)
        self.assertEqual(value, original)
        self.assertEqual(context, story.empty_context())
        self.assertEqual(one["base_revision"], 0)
        ev = one["event_updates"][0]
        self.assertEqual(ev["version"], 1)
        self.assertEqual(ev["created_at"], AT)
        self.assertEqual(ev["origin"], "user")
        self.assertEqual(ev["source_refs"], [{"dialog_id": "dialog-1", "message_id": "<user@example.test>",
                                            "source": "user", "quote": "planned tomorrow's walk", "time": AT}])
        self.assertNotIn("evidence", ev)
        self.assertNotIn("target_id", ev)

    def test_replay_first_and_idempotent_no_clock_or_new_ids(self):
        pkg = package()
        with patch.object(story.uuid, "uuid5", wraps=story.uuid.uuid5) as ids:
            context = self.reconcile([record(pkg)])
            again = self.reconcile([record(pkg)])
            self.assertEqual(context, again)
            # UUID5 validates committed IDs; no random IDs or current time exists.
            self.assertTrue(ids.called)
        self.assertEqual(context["revision"], 1)
        self.assertEqual(context["applied_dialog_ids"], ["dialog-1"])
        before = [(self.directory / filename).stat().st_mtime_ns for filename in ("event.json", "memory.json")]
        with patch.object(story.os, "replace", side_effect=AssertionError("unnecessary replace")):
            self.reconcile([record(pkg)])
        self.assertEqual(before, [(self.directory / filename).stat().st_mtime_ns for filename in ("event.json", "memory.json")])

    def test_existing_versions_origin_refs_and_related_events(self):
        first = package()
        context = self.reconcile([record(first)])
        ev, mem = context["events"][0], context["memories"][0]
        second = package(raw([event(target_id=ev["id"], status="completed", source="llm")],
                             [memory(target_id=mem["id"], status="superseded", related_event_ids=[ev["id"]])]),
                         context, "dialog-2", LATER)
        updated = self.reconcile([record(first), record(second)])
        self.assertEqual(updated["revision"], 2)
        self.assertEqual(updated["events"][0]["version"], 2)
        self.assertEqual(updated["events"][0]["origin"], "user")
        self.assertEqual(updated["events"][0]["created_at"], AT)
        self.assertEqual(updated["events"][0]["source_refs"][0], ev["source_refs"][0])
        self.assertEqual(updated["events"][0]["source_refs"][1]["message_id"], "<assistant@example.test>")
        self.assertEqual(updated["memories"][0]["related_event_ids"], [ev["id"]])

    def test_source_trace_cannot_change_origin_repeat_dialog_or_drop_llm_message_id(self):
        first = package()
        context = self.reconcile([record(first)])
        second = package(raw([event(target_id=context["events"][0]["id"], source="llm")]),
                         context, "dialog-2", LATER)
        bad = copy.deepcopy(second)
        bad["event_updates"][0]["origin"] = "llm"
        with self.assertRaises(story.StoryValidationError):
            story.validate_package(bad)
        bad = copy.deepcopy(second)
        bad["event_updates"][0]["source_refs"][0]["dialog_id"] = "dialog-2"
        with self.assertRaises(story.StoryValidationError):
            story.validate_package(bad)
        bad = copy.deepcopy(second)
        bad["event_updates"][0]["source_refs"][-1]["message_id"] = None
        with self.assertRaises(story.StoryValidationError):
            story.validate_package(bad)
        bad = copy.deepcopy(second)
        item = bad["event_updates"][0]
        item["source_refs"][0]["time"] = "2026-10-27T08:00:00+08:00"
        item["created_at"] = item["source_refs"][0]["time"]
        with self.assertRaises(story.StoryValidationError):
            story.validate_package(bad)

    def test_memory_cannot_reference_same_package_new_event_on_replay(self):
        pkg = package()
        pkg["memory_updates"][0]["related_event_ids"] = [pkg["event_updates"][0]["id"]]
        with self.assertRaises(story.StoryError):
            self.reconcile([record(pkg)])
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_user_without_message_id_is_not_proactive_but_no_user_body_is(self):
        value = validate(raw([event()]))
        pkg = story.materialize_updates(value, dialog_id="no-mail-id", story_context=story.empty_context(),
                                        observed_at=AT, user_message_id=None,
                                        assistant_message_id="<assistant@example.test>")
        rec = record(pkg)
        rec["user_message_id"] = None
        self.assertEqual(self.reconcile([rec])["revision"], 1)
        with self.assertRaises(story.StoryValidationError):
            validate(raw([event()]), user_body=None)

    def test_targets_unknown_wrong_kind_duplicate_and_origin_rejected(self):
        context = self.reconcile([record(package())])
        ev, mem = context["events"][0], context["memories"][0]
        for value in (raw([event(target_id="unknown")]), raw([event(target_id=mem["id"])]),
                      raw([event(target_id=ev["id"])] * 2),
                      raw(memories=[memory(target_id=mem["id"])] * 2),
                      raw(memories=[memory(target_id=mem["id"], origin="rewritten")])):
            with self.subTest(value=value), self.assertRaises(story.StoryValidationError):
                validate(value, context)
        with self.assertRaises(story.StoryValidationError):
            validate(raw([event(target_id=ev["id"])]), context, secrets=(ev["id"],))

    def test_empty_package_commits_boundary_and_legacy_pending_skipped(self):
        pkg = package(raw())
        records = [{"id": "old", "status": "sent"}]
        for status in ("prepared", "sending", "unknown"):
            records.append(record(pkg, status))
        self.assertEqual(self.reconcile(records), story.empty_context())
        records.append(record(pkg))
        context = self.reconcile(records)
        self.assertEqual(context["revision"], 1)
        self.assertEqual(context["events"], [])
        self.assertEqual(self.disk("memory.json")["applied_dialog_ids"], ["dialog-1"])

    def test_pure_package_validation_does_not_touch_disk(self):
        pkg = package()
        with patch.object(story.Path, "read_text", side_effect=AssertionError("read")), \
                patch.object(story.os, "replace", side_effect=AssertionError("write")):
            self.assertIs(story.validate_package(pkg), pkg)
        for change in ({"schema_version": True}, {"base_revision": True}, {"extra": 1}, {"observed_at": "bad"}):
            bad = copy.deepcopy(pkg)
            bad.update(change)
            with self.assertRaises(story.StoryValidationError):
                story.validate_package(bad)

    def test_package_new_id_anchor_ref_schema_binding_rejected(self):
        original = package()
        for field, value in (("id", "invented"), ("version", True), ("created_at", LATER),
                             ("source_refs", []), ("importance", True)):
            bad = copy.deepcopy(original)
            bad["event_updates"][0][field] = value
            with self.subTest(field=field), self.assertRaises(story.StoryValidationError):
                story.validate_package(bad)
        for key, value in (("dialog_id", "other"), ("time", LATER), ("source", "llm")):
            bad = copy.deepcopy(original)
            bad["event_updates"][0]["source_refs"][-1][key] = value
            with self.assertRaises(story.StoryValidationError):
                story.validate_package(bad)
        bad = copy.deepcopy(original)
        bad["event_updates"][0]["event_time"]["anchor"] = LATER
        with self.assertRaises(story.StoryValidationError):
            story.validate_package(bad)

    def test_dialog_and_message_id_binding_rejected(self):
        for key, value in (("id", "wrong"), ("user_message_id", "wrong"), ("assistant_message_id", "wrong")):
            pkg = package(raw([event(source="llm" if key == "assistant_message_id" else "user")]))
            rec = record(pkg)
            rec[key] = value
            with self.subTest(key=key), self.assertRaises(story.StoryError):
                self.reconcile([rec])
        rec = record(package())
        rec["type"] = "deepseek自己主动发送"
        with self.assertRaises(story.StoryError):
            self.reconcile([rec])
        self.assertFalse((self.directory / "event.json").exists())

    def test_absolute_replay_rejects_wrong_base_version_or_prior_reference(self):
        first = package()
        context = self.reconcile([record(first)])
        second = package(raw([event(target_id=context["events"][0]["id"])]), context, "dialog-2", LATER)
        variations = []
        bad = copy.deepcopy(second)
        bad["base_revision"] = 0
        variations.append(bad)
        bad = copy.deepcopy(second)
        bad["event_updates"][0]["origin"] = "tampered"
        variations.append(bad)
        bad = copy.deepcopy(second)
        bad["event_updates"][0]["source_refs"][0]["quote"] = "tampered"
        variations.append(bad)
        for bad in variations:
            with self.assertRaises(story.StoryError):
                self.reconcile([record(first), record(bad)])
        self.assertEqual(self.disk("event.json")["revision"], 1)
        with self.assertRaises(story.StoryError):
            self.reconcile([record(first), record(first)])

    def test_missing_empty_bom_and_rebuild_full_context(self):
        pkg = package()
        (self.directory / "event.json").write_text(" \n", encoding="utf-8")
        context = self.reconcile([record(pkg)])
        path = self.directory / "memory.json"
        path.write_text(json.dumps(self.disk("memory.json")), encoding="utf-8-sig")
        self.assertEqual(self.reconcile([record(pkg)]), context)
        (self.directory / "event.json").unlink()
        self.assertEqual(self.reconcile([record(pkg)]), context)
        self.assertEqual(len(context["events"]), 1)
        self.assertEqual(len(context["memories"]), 1)

    def test_corrupt_nonempty_either_file_blocks_both_writes(self):
        pkg = package()
        for filename in ("event.json", "memory.json"):
            with self.subTest(filename=filename):
                for path in self.directory.iterdir():
                    path.unlink()
                path = self.directory / filename
                path.write_text("private broken json", encoding="utf-8")
                with patch.object(story.os, "replace", side_effect=AssertionError("must validate both first")):
                    with self.assertRaises(story.StoryError):
                        self.reconcile([record(pkg)])
                self.assertEqual(path.read_text(encoding="utf-8"), "private broken json")
                self.assertEqual(len(list(self.directory.iterdir())), 1)

    def test_divergent_tampered_ahead_unknown_and_bool_projection_rejected(self):
        pkg = package()
        self.reconcile([record(pkg)])
        original = self.disk("event.json")
        variations = []
        for change in ({"revision": 2}, {"revision": True}, {"schema_version": True},
                       {"applied_dialog_ids": ["other"]}, {"extra": 1}):
            variations.append({**copy.deepcopy(original), **change})
        bad = copy.deepcopy(original)
        bad["events"][0]["importance"] = True
        variations.append(bad)
        bad = copy.deepcopy(original)
        bad["events"][0]["content"] = "tampered"
        variations.append(bad)
        path = self.directory / "event.json"
        for bad in variations:
            text = json.dumps(bad)
            path.write_text(text, encoding="utf-8")
            with self.assertRaises(story.StoryError):
                self.reconcile([record(pkg)])
            self.assertEqual(path.read_text(encoding="utf-8"), text)

    def test_partial_replace_failure_then_recovery_no_duplicate_application(self):
        pkg = package()
        replace = story.os.replace
        count = 0
        def fail_second(source, target):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError("synthetic second projection failure")
            return replace(source, target)
        with patch.object(story.os, "replace", side_effect=fail_second):
            with self.assertRaises(story.StoryError):
                self.reconcile([record(pkg)])
        self.assertEqual(self.disk("event.json")["revision"], 1)
        self.assertFalse((self.directory / "memory.json").exists())
        self.assertEqual({p.name for p in self.directory.iterdir()}, {"event.json"})
        context = self.reconcile([record(pkg)])
        self.assertEqual(context["revision"], 1)
        self.assertEqual(context["events"][0]["version"], 1)
        self.assertEqual(self.disk("memory.json")["revision"], 1)

    def test_second_replace_failure_recovers_existing_unequal_prefixes_and_versions(self):
        first = package()
        context = self.reconcile([record(first)])
        old_memory = (self.directory / "memory.json").read_bytes()
        second = package(raw([event(target_id=context["events"][0]["id"], status="completed")],
                             [memory(target_id=context["memories"][0]["id"], status="superseded")]),
                         context, "dialog-2", LATER)
        replace = story.os.replace
        def fail_memory(source, target):
            if Path(target).name == "memory.json":
                raise OSError("synthetic second projection failure")
            return replace(source, target)
        with patch.object(story.os, "replace", side_effect=fail_memory):
            with self.assertRaises(story.StoryError):
                self.reconcile([record(first), record(second)])
        self.assertEqual(self.disk("event.json")["revision"], 2)
        self.assertEqual((self.directory / "memory.json").read_bytes(), old_memory)
        self.assertEqual({p.name for p in self.directory.iterdir()}, {"event.json", "memory.json"})
        context = self.reconcile([record(first), record(second)])
        self.assertEqual(context["revision"], 2)
        for kind in ("events", "memories"):
            self.assertEqual(context[kind][0]["version"], 2)
            self.assertEqual(len(context[kind][0]["source_refs"]), 2)
        with patch.object(story.os, "replace", side_effect=AssertionError("already reconciled")):
            self.assertEqual(self.reconcile([record(first), record(second)]), context)

    def test_first_replace_and_fsync_failures_leave_original_bytes(self):
        first = package()
        context = self.reconcile([record(first)])
        second = package(raw(), context, "dialog-2", LATER)
        originals = {p: p.read_bytes() for p in self.directory.iterdir()}
        for operation in ("replace", "fsync"):
            with patch.object(story.os, operation, side_effect=OSError("synthetic failure")):
                with self.assertRaises(story.StoryError):
                    self.reconcile([record(first), record(second)])
            self.assertEqual({p: p.read_bytes() for p in self.directory.iterdir()}, originals)

    def test_read_permissions_and_write_permissions_fail_closed(self):
        pkg = package()
        with patch.object(story.Path, "read_text", side_effect=PermissionError("synthetic")):
            with self.assertRaises(story.StoryError):
                self.reconcile([record(pkg)])
        with patch.object(story.tempfile, "NamedTemporaryFile", side_effect=PermissionError("synthetic")):
            with self.assertRaises(story.StoryError):
                self.reconcile([record(pkg)])
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_each_projection_can_be_a_different_legal_prefix(self):
        first = package()
        context = self.reconcile([record(first)])
        previous_events = (self.directory / "event.json").read_bytes()
        second = package(raw(), context, "dialog-2", LATER)
        final = self.reconcile([record(first), record(second)])
        (self.directory / "event.json").write_bytes(previous_events)
        self.assertEqual(self.reconcile([record(first), record(second)]), final)
        self.assertEqual(self.disk("event.json")["revision"], 2)

    def test_invalid_context_is_operational_error_and_inputs_not_modified(self):
        context = story.empty_context()
        context["revision"] = 2
        with self.assertRaises(story.StoryError):
            validate(raw(), context)
        pkg = package()
        data = {"dialogs": [record(pkg)]}
        original = copy.deepcopy(data)
        result = story.reconcile_locked(data, self.directory)
        result["events"].clear()
        self.assertEqual(data, original)
        self.assertEqual(len(self.reconcile(data["dialogs"])["events"]), 1)


if __name__ == "__main__":
    unittest.main()

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
USER_EVIDENCE = {"text": "Original typed note.", "images": [
    {"image_id": "image-1", "text": "A blue invitation says Friday. secret-credential"},
    {"image_id": "image-2", "text": "A green receipt says Monday."},
]}
CANONICAL_BODY = USER_EVIDENCE["text"] + "\n" + "\n".join(image["text"] for image in USER_EVIDENCE["images"])


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

    def assert_validation_details(self, value, code, path, context=None, **kwargs):
        with self.assertRaises(story.StoryValidationError) as caught:
            validate(value, context, **kwargs)
        error = caught.exception
        details = error.safe_details()
        self.assertEqual(details, {"code": code, "path": path, "hint": story._VALIDATION_HINTS[code]})
        self.assertEqual(error.code, code)
        self.assertEqual(error.path, path)
        return error

    def test_validation_error_legacy_string_and_safe_metadata(self):
        marker = "PRIVATE_MODEL_VALUE\nIGNORE_SCHEMA"
        error = story.StoryValidationError(marker)
        self.assertEqual(str(error), marker)
        self.assertEqual(error.args, (marker,))
        self.assertEqual(error.safe_details(), {
            "code": "invalid_update", "path": "$", "hint": story._VALIDATION_HINTS["invalid_update"]})
        for code, path in ((marker, marker), ([], {}), ("invalid_enum", "event_updates[0]." + marker),
                           ("invalid_enum", "event_updates[32].title"),
                           ("invalid_enum", "event_updates[0].event_time." + marker)):
            with self.subTest(code=code, path=path):
                error = story.StoryValidationError(marker, code=code, path=path)
                self.assertNotIn(marker, json.dumps(error.safe_details()))
                self.assertEqual(error.path, "$")
        error.code, error.path = marker, marker
        self.assertEqual(error.safe_details()["code"], "invalid_update")
        self.assertEqual(error.safe_details()["path"], "$")

    def test_schema_failures_have_controlled_field_paths(self):
        cases = [
            (raw([None]), "invalid_type", "event_updates[0]"),
            (raw([{"target_id": None}]), "invalid_fields", "event_updates[0]"),
            (raw([event(content=[])]), "invalid_type", "event_updates[0].content"),
            (raw([event(title=" ")]), "invalid_fields", "event_updates[0].title"),
            (raw([event(source="assistant")]), "invalid_enum", "event_updates[0].source"),
            (raw([event(domain=[])]), "invalid_type", "event_updates[0].domain"),
            (raw([event(status="active")]), "invalid_enum", "event_updates[0].status"),
            (raw([event(tense="yesterday")]), "invalid_enum", "event_updates[0].tense"),
            (raw([event(people=[None])]), "invalid_type", "event_updates[0].people"),
            (raw([event(people=["x", "x"])]), "invalid_fields", "event_updates[0].people"),
            (raw([event(target_id=[])]), "invalid_type", "event_updates[0].target_id"),
            (raw([event(evidence=None)]), "invalid_type", "event_updates[0].evidence"),
            (raw([event(event_time=[])]), "invalid_type", "event_updates[0].event_time"),
            (raw([event(event_time={"raw": None})]), "invalid_fields", "event_updates[0].event_time"),
            (raw([event(event_time={"raw": 2, "start": None, "end": None})]),
             "invalid_type", "event_updates[0].event_time.raw"),
            (raw([event(event_time={"raw": "tomorrow", "start": "bad", "end": None})]),
             "invalid_time", "event_updates[0].event_time.start"),
            (raw([event(event_time={"raw": "tomorrow", "start": None, "end": []})]),
             "invalid_time", "event_updates[0].event_time.end"),
            (raw([event(event_time={"raw": "tomorrow", "start": LATER, "end": AT})]),
             "invalid_time", "event_updates[0].event_time.end"),
            (raw([event(event_time={"raw": None, "start": AT, "end": None})]),
             "invalid_time", "event_updates[0].event_time.raw"),
            (raw(memories=[memory(status="planned")]), "invalid_enum", "memory_updates[0].status"),
            (raw(memories=[memory(category=[])]), "invalid_type", "memory_updates[0].category"),
            (raw(memories=[memory(origin=[])]), "invalid_type", "memory_updates[0].origin"),
            (raw(memories=[memory(related_event_ids=[None])]),
             "invalid_type", "memory_updates[0].related_event_ids"),
            (raw([event(), event(content=None)]), "invalid_type", "event_updates[1].content"),
        ]
        for value, code, path in cases:
            with self.subTest(code=code, path=path):
                self.assert_validation_details(value, code, path)
        for importance in (True, 0, 6, 1.0, "3", None):
            self.assert_validation_details(raw([event(importance=importance)]),
                                           "invalid_importance", "event_updates[0].importance")
        self.assert_validation_details(raw(events="private"), "invalid_type", "event_updates")
        self.assert_validation_details(raw([event()] * 33), "invalid_fields", "event_updates")
        self.assert_validation_details(raw(), "invalid_time", "anchors.as_of", anchors={"as_of": "bad"})

    def test_evidence_target_and_domain_retry_codes(self):
        context = self.reconcile([record(package())])
        ev_id, mem_id = context["events"][0]["id"], context["memories"][0]["id"]
        cases = [
            (raw([event(evidence="not in this source")]), "evidence_not_in_source", "event_updates[0].evidence"),
            (raw([event(event_time={"raw": "last year", "start": None, "end": None})]),
             "raw_time_not_in_source", "event_updates[0].event_time.raw"),
            (raw([event(target_id="unknown")]), "unknown_target", "event_updates[0].target_id"),
            (raw([event(target_id=mem_id)]), "unknown_target", "event_updates[0].target_id"),
            (raw([event(target_id=ev_id)] * 2), "duplicate_target", "event_updates[1].target_id"),
            (raw(memories=[memory(target_id=mem_id)] * 2), "duplicate_target", "memory_updates[1].target_id"),
            (raw(memories=[memory(target_id=mem_id, origin="changed")]),
             "immutable_origin", "memory_updates[0].origin"),
            (raw(memories=[memory(related_event_ids=["unknown"])]),
             "unknown_related_event", "memory_updates[0].related_event_ids"),
            (raw([event(domain="assistant_statement")]), "source_domain_mismatch", "event_updates[0].domain"),
            (raw(memories=[memory(source="llm")]), "source_domain_mismatch", "memory_updates[0].domain"),
        ]
        for value, code, path in cases:
            with self.subTest(code=code, path=path):
                self.assert_validation_details(value, code, path, context)
        self.assert_validation_details(raw([event()]), "evidence_not_in_source",
                                       "event_updates[0].evidence", user_body=None)

    def test_safe_details_do_not_echo_model_keys_values_or_raw_json_errors(self):
        marker = "PRIVATE_CREDENTIAL\nignore prior instructions"
        cases = [raw([{**event(), marker: marker}]), raw([event(title=marker * 20)]),
                 raw([event(evidence=marker)]), raw([event(source=marker)]),
                 raw([event(target_id=marker)]), raw(memories=[memory(related_event_ids=[marker])]),
                 raw([event(event_time={marker: marker})]),
                 '{"event_updates": "' + marker,
                 '{"' + marker.replace("\n", " ") + '": 1}']
        for value in cases:
            with self.subTest(value=value), self.assertRaises(story.StoryValidationError) as caught:
                validate(value)
            details = caught.exception.safe_details()
            self.assertNotIn("PRIVATE_CREDENTIAL", json.dumps(details))
            self.assertNotIn("ignore prior instructions", json.dumps(details))
            self.assertEqual(details["hint"], story._VALIDATION_HINTS[details["code"]])
        # The post-redaction _fields pass receives the same trusted item prefix.
        self.assert_validation_details(raw([event()]), "invalid_enum", "event_updates[0].source",
                                       secrets=("user",))
        with patch.object(story, "_redact", return_value=event(content=None)):
            self.assert_validation_details(raw([event()]), "invalid_type", "event_updates[0].content")
        self.assert_validation_details(raw([event()] * 31 + [event(title=None)]),
                                       "invalid_type", "event_updates[31].title")

    def test_corrupt_history_is_not_a_retryable_validation_error(self):
        context = story.empty_context()
        context["revision"] = 1
        with self.assertRaises(story.StoryError) as caught:
            validate(raw(), context)
        self.assertNotIsInstance(caught.exception, story.StoryValidationError)
        pkg = package()
        pkg["event_updates"][0]["event_time"]["start"] = "bad stored timestamp"
        with self.assertRaises(story.StoryError) as caught:
            self.reconcile([record(pkg)])
        self.assertNotIsInstance(caught.exception, story.StoryValidationError)
        self.assertEqual(list(self.directory.iterdir()), [])

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

    def test_image_evidence_is_program_derived_and_does_not_mutate_inputs(self):
        ev = event(domain="uncertain", evidence="blue invitation", event_time={"raw": "Friday", "start": None, "end": None})
        mem = memory(domain="uncertain", evidence="green receipt")
        value, metadata = raw([ev], [mem]), copy.deepcopy(USER_EVIDENCE)
        original = copy.deepcopy(value)
        validated = validate(value, user_body=CANONICAL_BODY, user_evidence=metadata)
        self.assertEqual(value, original)
        self.assertEqual(metadata, USER_EVIDENCE)
        for key, image_id in (("event_updates", "image-1"), ("memory_updates", "image-2")):
            self.assertEqual(validated[key][0]["_validated_source"], {
                "evidence_origin": "image_observation", "image_ids": [image_id]})
        pkg = story.materialize_updates(validated, dialog_id="visual", story_context=story.empty_context(),
                                        observed_at=AT, user_message_id=None,
                                        assistant_message_id="<assistant@example.test>")
        self.assertNotIn("_validated_source", json.dumps(pkg))
        for key, image_id in (("event_updates", "image-1"), ("memory_updates", "image-2")):
            self.assertEqual(pkg[key][0]["source_refs"][-1], {
                "dialog_id": "visual", "message_id": None, "source": "user", "time": AT,
                "quote": validated[key][0]["evidence"],
                "evidence_origin": "image_observation", "image_ids": [image_id]})

    def test_metadata_prefers_original_text_and_leaves_legacy_or_llm_unchanged(self):
        metadata = {"text": BODY, "images": [{"image_id": "image-1", "text": BODY}]}
        original = raw([event()], [memory()])
        self.assertEqual(validate(original, user_evidence=metadata), validate(original))
        self.assertEqual(validate(raw([event(source="llm")]), user_evidence=USER_EVIDENCE),
                         validate(raw([event(source="llm")])))
        # Metadata is authoritative even when the canonical body includes a quote.
        self.assert_validation_details(raw([event()]), "evidence_not_in_source", "event_updates[0].evidence",
                                       user_evidence=USER_EVIDENCE)
        # Omission retains the old unrestricted-body substring contract.
        joined = "Friday. secret-credential\nA green"
        validate(raw(memories=[memory(evidence=joined)]), user_body=CANONICAL_BODY)

    def test_image_only_domains_have_controlled_retry_hint(self):
        for domain in story.DOMAINS - {"uncertain"}:
            for kind in ("event", "memory"):
                with self.subTest(domain=domain, kind=kind):
                    value = raw([event(domain=domain, evidence="blue invitation",
                                       event_time={"raw": None, "start": None, "end": None})]) if kind == "event" else raw(
                                           memories=[memory(domain=domain, evidence="blue invitation")])
                    self.assert_validation_details(value, "image_requires_uncertain",
                                                   f"{kind}_updates[0].domain", user_body=CANONICAL_BODY,
                                                   user_evidence=USER_EVIDENCE)

    def test_cross_fragment_evidence_and_raw_time_are_rejected(self):
        for quote in ("Friday. secret-credential\nA green", "note.\nA blue"):
            self.assert_validation_details(raw(memories=[memory(domain="uncertain", evidence=quote)]),
                                           "evidence_not_in_source", "memory_updates[0].evidence",
                                           user_body=CANONICAL_BODY, user_evidence=USER_EVIDENCE)
        for quote, raw_time in (("blue invitation", "Monday"), ("Original typed", "Friday")):
            self.assert_validation_details(raw([event(domain="uncertain", evidence=quote,
                                                      event_time={"raw": raw_time, "start": None, "end": None})]),
                                           "raw_time_not_in_source", "event_updates[0].event_time.raw",
                                           user_body=CANONICAL_BODY, user_evidence=USER_EVIDENCE)
        # A duplicate image quote still has one deterministic source: first match.
        metadata = {"text": "", "images": [{"image_id": "image-1", "text": "invitation"},
                                              {"image_id": "image-2", "text": "invitation Monday"}]}
        self.assert_validation_details(raw([event(domain="uncertain", evidence="invitation",
                                                  event_time={"raw": "Monday", "start": None, "end": None})]),
                                       "raw_time_not_in_source", "event_updates[0].event_time.raw",
                                       user_evidence=metadata)

    def test_model_cannot_supply_provenance_even_with_metadata(self):
        for key, value in (("_validated_source", {"evidence_origin": "image_observation", "image_ids": ["image-1"]}),
                           ("evidence_origin", "image_observation"), ("image_ids", ["image-1"]),
                           ("source_refs", [])):
            for kwargs in ({}, {"user_evidence": USER_EVIDENCE}):
                with self.subTest(key=key, kwargs=kwargs):
                    self.assert_validation_details(raw([event(**{key: value})]), "invalid_fields",
                                                   "event_updates[0]", **kwargs)

    def test_program_evidence_schema_and_safe_paths_are_bounded(self):
        marker = "PRIVATE_METADATA_VALUE"
        values = [[], {}, {"text": None, "images": []}, {"text": "", "images": {}},
                  {"text": "", "images": [{"image_id": marker, "text": marker}]},
                  {"text": "", "images": [{"image_id": "image-1", "text": None}]},
                  {"text": "", "images": [{"image_id": "image-1", "text": ""}] * 2},
                  {"text": "", "images": [{"image_id": "image-5", "text": ""}]},
                  {"text": "", "images": [], marker: marker}]
        for metadata in values:
            with self.subTest(metadata=metadata), self.assertRaises(story.StoryValidationError) as caught:
                validate(raw(), user_evidence=metadata)
            self.assertNotIn(marker, json.dumps(caught.exception.safe_details()))
            self.assertIn(caught.exception.path, {"user_evidence", "user_evidence.text", "user_evidence.images"})
        validate(raw(), user_evidence={"text": "", "images": []})
        with self.assertRaises(story.StoryValidationError):
            validate(raw(), user_body=None, user_evidence=USER_EVIDENCE)
        error = story.StoryValidationError(marker, code="image_requires_uncertain",
                                           path="user_evidence.images[0]." + marker)
        self.assertEqual(error.path, "$")
        self.assertNotIn(marker, json.dumps(error.safe_details()))

    def test_image_evidence_redaction_keeps_program_locator(self):
        validated = validate(raw(memories=[memory(domain="uncertain", evidence="secret-credential")]),
                             user_body=CANONICAL_BODY, user_evidence=USER_EVIDENCE,
                             secrets=("secret-credential", "image-1"))
        self.assertEqual(validated["memory_updates"][0]["evidence"], "[REDACTED]")
        self.assertEqual(validated["memory_updates"][0]["_validated_source"]["image_ids"], ["image-1"])
        self.assertNotIn("secret-credential", json.dumps(validated))

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

    def test_image_refs_persist_replay_and_extend_legacy_history_without_rewrite(self):
        first = package()
        old_package = copy.deepcopy(first)
        context = self.reconcile([record(first)])
        old_refs = copy.deepcopy(context["events"][0]["source_refs"])
        ev, mem = context["events"][0], context["memories"][0]
        updates = raw([event(target_id=ev["id"], domain="uncertain", evidence="blue invitation",
                             event_time={"raw": "Friday", "start": None, "end": None})],
                      [memory(target_id=mem["id"], domain="uncertain", evidence="green receipt")])
        validated = validate(updates, context, user_body=CANONICAL_BODY, user_evidence=USER_EVIDENCE)
        second = story.materialize_updates(validated, dialog_id="dialog-2", story_context=context,
                                           observed_at=LATER, user_message_id="<user@example.test>",
                                           assistant_message_id="<assistant@example.test>")
        # The dialog log is also synthetic and persists the frozen record package.
        log = self.directory / "dialog.json"
        records = [record(first), record(second)]
        log.write_text(json.dumps({"dialogs": records}), encoding="utf-8")
        final = story.reconcile_locked(json.loads(log.read_text(encoding="utf-8")), self.directory)
        self.assertEqual(first, old_package)
        for kind, image_id in (("events", "image-1"), ("memories", "image-2")):
            item = final[kind][0]
            self.assertEqual(item["version"], 2)
            self.assertEqual(item["source_refs"][0], context[kind][0]["source_refs"][0])
            self.assertEqual(len(item["source_refs"][0]), 5)
            self.assertEqual(item["source_refs"][-1]["image_ids"], [image_id])
            self.assertEqual(item["source_refs"][-1]["evidence_origin"], "image_observation")
        self.assertEqual(final["events"][0]["source_refs"][:-1], old_refs)
        before = {path: path.read_bytes() for path in self.directory.iterdir()}
        with patch.object(story.os, "replace", side_effect=AssertionError("idempotent replay must not write")):
            self.assertEqual(story.reconcile_locked(json.loads(log.read_text(encoding="utf-8")), self.directory), final)
        self.assertEqual({path: path.read_bytes() for path in self.directory.iterdir()}, before)
        # A later typed correction may use a normal domain, retaining image history.
        third = package(raw([event(target_id=ev["id"])]), final, "dialog-3", LATER)
        latest = self.reconcile(records + [record(third)])
        self.assertEqual(latest["events"][0]["source_refs"][:-1], final["events"][0]["source_refs"])
        self.assertEqual(len(latest["events"][0]["source_refs"][-1]), 5)
        # Image history is immutable, even when a forged old locator is structurally valid.
        forged = copy.deepcopy(third)
        forged["event_updates"][0]["source_refs"][1]["image_ids"] = ["image-4"]
        originals = {path: path.read_bytes() for path in self.directory.iterdir()}
        with self.assertRaises(story.StoryError):
            self.reconcile(records + [record(forged)])
        self.assertEqual({path: path.read_bytes() for path in self.directory.iterdir()}, originals)

    def test_image_reference_optional_pair_schema_user_only_and_uncertain(self):
        original = package(raw([event(domain="uncertain")], [memory(domain="uncertain")]))
        image_meta = {"evidence_origin": "image_observation", "image_ids": ["image-4"]}
        for key in ("event_updates", "memory_updates"):
            valid = copy.deepcopy(original)
            valid[key][0]["source_refs"][-1].update(image_meta)
            self.assertIs(story.validate_package(valid), valid)
            invalid = [
                {"evidence_origin": "image_observation"}, {"image_ids": ["image-1"]},
                {"evidence_origin": "typed", "image_ids": ["image-1"]},
                {"evidence_origin": "image_observation", "image_ids": []},
                {"evidence_origin": "image_observation", "image_ids": "image-1"},
                {"evidence_origin": "image_observation", "image_ids": [None]},
                {"evidence_origin": "image_observation", "image_ids": ["image-0"]},
                {"evidence_origin": "image_observation", "image_ids": ["image-5"]},
                {"evidence_origin": "image_observation", "image_ids": ["image-01"]},
                {"evidence_origin": "image_observation", "image_ids": ["image-1", "image-2"]},
                {**image_meta, "extra": "PRIVATE"},
            ]
            for provenance in invalid:
                bad = copy.deepcopy(original)
                bad[key][0]["source_refs"][-1].update(provenance)
                with self.subTest(key=key, provenance=provenance), self.assertRaises(story.StoryValidationError):
                    story.validate_package(bad)
            bad = copy.deepcopy(valid)
            bad[key][0]["domain"] = "shared_story"
            with self.assertRaises(story.StoryValidationError) as caught:
                story.validate_package(bad)
            self.assertEqual(caught.exception.code, "image_requires_uncertain")
            bad = copy.deepcopy(valid)
            bad[key][0]["source"] = "llm"
            bad[key][0]["source_refs"][-1].update(source="llm", message_id="<assistant@example.test>")
            if key == "event_updates":
                bad[key][0]["origin"] = "llm"
            with self.assertRaises(story.StoryValidationError):
                story.validate_package(bad)

    def test_image_projection_tampering_and_private_materialization_shape_rejected(self):
        validated = validate(raw(memories=[memory(domain="uncertain", evidence="blue invitation")]),
                             user_body=CANONICAL_BODY, user_evidence=USER_EVIDENCE)
        pkg = story.materialize_updates(validated, dialog_id="visual", story_context=story.empty_context(),
                                        observed_at=AT, user_message_id="<user@example.test>",
                                        assistant_message_id="<assistant@example.test>")
        self.reconcile([record(pkg)])
        projected = self.disk("memory.json")
        projected["memories"][0]["source_refs"][-1]["image_ids"] = ["image-2"]
        (self.directory / "memory.json").write_text(json.dumps(projected), encoding="utf-8")
        before = {path: path.read_bytes() for path in self.directory.iterdir()}
        with self.assertRaises(story.StoryError):
            self.reconcile([record(pkg)])
        self.assertEqual({path: path.read_bytes() for path in self.directory.iterdir()}, before)
        for provenance in (None, {}, {"evidence_origin": "typed", "image_ids": ["image-1"]}):
            bad = copy.deepcopy(validated)
            bad["memory_updates"][0]["_validated_source"] = provenance
            with self.assertRaises(story.StoryValidationError):
                story.materialize_updates(bad, dialog_id="visual", story_context=story.empty_context(),
                                          observed_at=AT, user_message_id="<user@example.test>",
                                          assistant_message_id="<assistant@example.test>")

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

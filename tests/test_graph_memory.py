import copy
import unittest

from graph_memory import (GraphError, digest, empty_candidate, freeze_candidate,
                          incomplete_package, stable_id, validate_candidate, validate_package)
from story_memory import empty_context, materialize_updates, validate_updates


NOW = "2026-11-14T12:00:00+08:00"
BODY = "Alice works on Project X. Bob is Alice's friend."


def entity(ref="P1", name="Alice", **changes):
    item = {"ref": ref, "type": "person", "name": name, "aliases": [], "target_id": None,
            "expected_version": None, "source": "user", "domain": "user_report", "evidence": "Alice"}
    item.update(changes)
    return item


def edge(**changes):
    item = {"target_id": None, "expected_version": None, "subject": "P1", "predicate": "friend_of",
            "object": "P2", "state": "active", "support_ref": None,
            "source": "user", "domain": "user_report", "evidence": "Alice"}
    item.update(changes)
    return item


class GraphMemoryTests(unittest.TestCase):
    def validate(self, raw, **changes):
        args = dict(story_context=empty_context(), user_body=BODY, assistant_body="Assistant said hello.")
        args.update(changes)
        return validate_candidate(raw, **args)

    def freeze(self, validated, **changes):
        args = dict(dialog_id="dialog-1", observed_at=NOW, user_message_id="<u1>", assistant_message_id="<a1>")
        args.update(changes)
        return freeze_candidate(validated, **args)

    def candidate(self):
        raw = empty_candidate()
        raw["entities"] = [entity(), entity("P2", "Bob", evidence="Bob")]
        raw["relations"] = [edge()]
        return raw

    def assert_code(self, code, action):
        with self.assertRaises(GraphError) as caught:
            action()
        self.assertEqual(caught.exception.code, code)

    def test_canonical_digest_and_stable_ids(self):
        self.assertEqual(digest({"a": 1, "b": 2}), digest({"b": 2, "a": 1}))
        self.assertNotEqual(digest(True), digest(1))
        self.assertNotEqual(stable_id("entity", "A", "B"), stable_id("entity", "AB"))
        self.assert_code("invalid_type", lambda: digest(float("nan")))

    def test_validation_is_pure_and_freeze_idempotent(self):
        raw = self.candidate()
        original = copy.deepcopy(raw)
        validated = self.validate(raw)
        self.assertEqual(raw, original)
        package = self.freeze(validated)
        self.assertEqual(package, self.freeze(validated))
        self.assertEqual(package["relations"][0]["subject"], package["entities"][0]["id"])
        self.assertEqual(package["entities"][0]["version"], 1)
        self.assertEqual(package["source_refs"][0], {"kind": "mail", "id": "<u1>", "version": 1, "digest": digest(BODY)})
        self.assertEqual(package["entities"][0]["evidence"]["message_id"], "<u1>")
        self.assertIs(validate_package(package), package)

    def test_symmetric_relations_have_one_identity(self):
        first = self.candidate()
        reverse = copy.deepcopy(first)
        reverse["relations"][0].update(subject="P2", object="P1")
        left = self.freeze(self.validate(first))["relations"][0]
        right = self.freeze(self.validate(reverse))["relations"][0]
        self.assertEqual(left, right)
        self.assertLessEqual(left["subject"], left["object"])
        duplicate = copy.deepcopy(first)
        duplicate["relations"].append(reverse["relations"][0])
        self.assert_code("duplicate_identity", lambda: self.validate(duplicate))

    def test_event_cannot_be_reclassified_as_preference(self):
        raw = empty_candidate()
        raw["facts"] = [{"ref": "E1", "text": "not a preference", "category": "preference",
                         "source": "user", "domain": "user_report", "evidence": "Alice"}]
        self.assert_code("unknown_reference", lambda: self.validate(raw))

    def test_multiline_short_text_is_rejected(self):
        raw = self.candidate()
        raw["entities"][0]["name"] = "Alice\nDifferent assertion"
        with self.assertRaises(GraphError):
            self.validate(raw)

    def test_empty_and_incomplete_packages(self):
        package = self.freeze(self.validate(empty_candidate()))
        self.assertEqual(package["source_refs"], [])
        incomplete = incomplete_package("dialog-1", NOW, "extraction_failed")
        self.assertIs(validate_package(incomplete), incomplete)
        self.assert_code("invalid_enum", lambda: incomplete_package("dialog-1", NOW, "SECRET exception"))

    def test_strict_root_duplicate_json_and_total_limit(self):
        for raw in ({}, [], {**empty_candidate(), "evil": 1}, '{"facts":[],"facts":[]}'):
            self.assert_code("invalid_fields", lambda raw=raw: self.validate(raw))
        self.assert_code("too_many_updates", lambda: self.validate(self.candidate(), settings={"max_updates": 2}))

    def test_exact_current_evidence_and_proactive_guard(self):
        raw = self.candidate()
        raw["entities"][0]["evidence"] = "alice"
        self.assert_code("evidence_not_in_source", lambda: self.validate(raw))
        self.assert_code("evidence_not_in_source", lambda: self.validate(self.candidate(), user_body=None))
        self.assert_code("evidence_not_in_source", lambda: self.validate(self.candidate(), user_body="", source_evidence={"text": BODY}))

    def test_domain_and_source_binding(self):
        raw = self.candidate()
        raw["entities"][0]["domain"] = "assistant_statement"
        self.assert_code("source_domain_mismatch", lambda: self.validate(raw))

    def test_hidden_targets_and_endpoints(self):
        raw = self.candidate()
        raw["relations"][0]["object"] = "hidden-entity"
        self.assert_code("unknown_reference", lambda: self.validate(raw))
        raw = self.candidate()
        raw["entities"][0].update(target_id="hidden-entity", expected_version=1)
        self.assert_code("unknown_target", lambda: self.validate(raw))

    def test_mutation_uses_exact_versions_and_preserves_identity(self):
        first = self.freeze(self.validate(self.candidate()))
        graph = {key: first[key] for key in empty_candidate()}
        old = first["entities"][0]
        raw = empty_candidate()
        raw["entities"] = [entity(target_id=old["id"], expected_version=old["version"], aliases=["Al"])]
        second = self.freeze(self.validate(raw, graph_context=graph), dialog_id="dialog-2")
        self.assertEqual(second["entities"][0]["id"], old["id"])
        self.assertEqual(second["entities"][0]["version"], 2)
        raw["entities"][0]["expected_version"] = True
        self.assert_code("invalid_type", lambda: self.validate(raw, graph_context=graph))
        raw["entities"][0]["expected_version"] = 2
        self.assert_code("version_conflict", lambda: self.validate(raw, graph_context=graph))
        raw["entities"][0].update(expected_version=1, name="Other")
        self.assert_code("immutable_identity", lambda: self.validate(raw, graph_context=graph))

    def test_relations_cannot_retarget_existing_edges(self):
        package = self.freeze(self.validate(self.candidate()))
        graph = {key: package[key] for key in empty_candidate()}
        old = package["relations"][0]
        raw = empty_candidate()
        raw["relations"] = [edge(target_id=old["id"], expected_version=1,
                                  subject=old["subject"], object=old["subject"])]
        self.assert_code("immutable_identity", lambda: self.validate(raw, graph_context=graph))

    def test_new_entities_normalized_but_domains_not_merged(self):
        raw = empty_candidate()
        raw["entities"] = [entity(), entity("P2", " ALICE ")]
        self.assert_code("duplicate_identity", lambda: self.validate(raw))
        raw["entities"][1]["domain"] = "shared_story"
        package = self.freeze(self.validate(raw))
        self.assertNotEqual(package["entities"][0]["id"], package["entities"][1]["id"])

    def story_package(self):
        raw = {"event_updates": [{"target_id": None, "content": "Alice works on Project X.", "source": "user",
                "domain": "user_report", "importance": 3, "people": ["Alice"], "status": "ongoing", "title": "Work",
                "tense": "present", "event_time": {"raw": None, "start": None, "end": None}, "evidence": "Alice"}], "memory_updates": []}
        validated = validate_updates(raw, story_context=empty_context(), user_body=BODY, assistant_body="")
        return materialize_updates(validated, dialog_id="dialog-1", story_context=empty_context(), observed_at=NOW,
                                   user_message_id="<u1>", assistant_message_id="<a1>")

    def test_story_alias_resolution_and_canonical_binding(self):
        raw = empty_candidate()
        raw["facts"] = [{"ref": "E1", "text": "Alice works", "category": "event", "source": "user", "domain": "user_report", "evidence": "Alice"}]
        validated = self.validate(raw)
        self.assert_code("unknown_reference", lambda: self.freeze(validated))
        story_package = self.story_package()
        package = self.freeze(validated, story_package=story_package)
        story_item = story_package["event_updates"][0]
        self.assertEqual(package["facts"][0]["ref"], story_item["id"])
        self.assertIn({"kind": "event", "id": story_item["id"], "version": 1, "digest": digest(story_item)}, package["source_refs"])
        self.assertEqual(story_item["status"], "ongoing")

    def test_existing_story_reference_matches_source_domain(self):
        story_item = self.story_package()["event_updates"][0]
        context = {"events": [story_item], "memories": []}
        raw = empty_candidate()
        raw["facts"] = [{"ref": story_item["id"], "text": "Alice", "category": "event", "source": "llm", "domain": "assistant_statement", "evidence": "Assistant"}]
        self.assert_code("source_domain_mismatch", lambda: self.validate(raw, story_context=context))

    def test_tamper_detection_and_record_binding_without_bodies(self):
        package = self.freeze(self.validate(self.candidate()))
        record = {"id": "dialog-1", "user_message_id": "<u1>", "assistant_message_id": "<a1>"}
        self.assertIs(validate_package(package, record=record), package)
        record["user_message_id"] = "<different>"
        self.assert_code("invalid_binding", lambda: validate_package(package, record=record))
        package["entities"][0]["name"] = "Tamper"
        self.assert_code("invalid_binding", lambda: validate_package(package))

    def test_changed_validated_candidate_cannot_freeze(self):
        validated = self.validate(self.candidate())
        validated["entities"][0]["name"] = "Tamper"
        self.assert_code("invalid_binding", lambda: self.freeze(validated))

    def test_states_timestamp_and_enum(self):
        raw = empty_candidate()
        raw["entities"] = [entity()]
        raw["states"] = [{"target_id": None, "expected_version": None, "subject": "P1", "predicate": "working_on",
                          "value": "Project X", "state": "active", "valid_until": NOW, "support_ref": None,
                          "source": "user", "domain": "user_report", "evidence": "Project X"}]
        self.assertEqual(self.freeze(self.validate(raw))["states"][0]["value"], "Project X")
        raw["states"][0]["valid_until"] = "2026-11-14T12:00:00"
        self.assert_code("invalid_time", lambda: self.validate(raw))

    def test_event_self_edges_rejected_before_and_after_alias_freeze(self):
        story_package = self.story_package()
        raw = empty_candidate()
        raw["event_relations"] = [edge(subject="E1", object="E1", predicate="follows")]
        self.assert_code("self_loop", lambda: self.validate(raw))
        event = story_package["event_updates"][0]
        raw["event_relations"][0]["object"] = event["id"]
        validated = self.validate(raw, story_context={"events": [event], "memories": []})
        self.assert_code("self_loop", lambda: self.freeze(validated, story_package=story_package))

    def test_predicate_endpoint_matrix_and_local_event_participation(self):
        for predicate, valid_type in (("friend_of", "person"), ("sibling_of", "person"),
                ("planned_with", "person"), ("member_of", "group"), ("works_on", "project"),
                ("participates_in", "project"), ("organizes", "project")):
            for object_type in ("person", "group", "project", "place"):
                raw = self.candidate()
                raw["entities"][1]["type"] = object_type
                raw["relations"][0]["predicate"] = predicate
                if object_type == valid_type:
                    self.freeze(self.validate(raw))
                else:
                    self.assert_code("invalid_endpoint_type", lambda: self.validate(raw))
        raw = empty_candidate()
        raw["entities"] = [entity()]
        raw["relations"] = [edge(predicate="participates_in", object="E1", support_ref="E1")]
        pkg = self.freeze(self.validate(raw), story_package=self.story_package())
        self.assertEqual(pkg["relations"][0]["object"], self.story_package()["event_updates"][0]["id"])
        self.assertIs(validate_package(pkg), pkg)

    def test_fixed_person_aliases_new_identity_and_domains(self):
        raw = empty_candidate()
        raw["entities"] = [entity(name="助手")]
        pkg = self.freeze(self.validate(raw))
        self.assertEqual(pkg["entities"][0]["name"], "才羽桃井")
        self.assertIn("助手", pkg["entities"][0]["aliases"])
        raw["entities"] = [entity(name="桃井")]
        self.assert_code("unknown_target", lambda: self.validate(raw, graph_context={kind: pkg[kind] for kind in empty_candidate()}))
        raw["entities"] = [entity(name="才羽桃井", domain="shared_story")]
        self.assertNotEqual(self.freeze(self.validate(raw))["entities"][0]["id"], pkg["entities"][0]["id"])
        raw["entities"] = [entity(name="我"), entity("P2", "你")]
        self.assertEqual([item["name"] for item in self.freeze(self.validate(raw))["entities"]], ["我", "你"])

    def test_current_story_update_rebinds_displayed_old_id(self):
        story_package = self.story_package()
        old = copy.deepcopy(story_package["event_updates"][0])
        old["content"] = "Old content"
        context = {"events": [old], "memories": []}
        raw = empty_candidate()
        raw["facts"] = [{"ref": old["id"], "text": "Alice", "category": "event", "source": "user", "domain": "user_report", "evidence": "Alice"}]
        package = self.freeze(self.validate(raw, story_context=context), story_package=story_package)
        source = next(ref for ref in package["source_refs"] if ref["kind"] == "event")
        self.assertEqual(source["digest"], digest(story_package["event_updates"][0]))
        self.assertNotEqual(source["digest"], digest(old))

    def test_unshown_duplicate_entity_must_not_be_silently_merged(self):
        package = self.freeze(self.validate(self.candidate()))
        graph = {key: package[key] for key in empty_candidate()}
        raw = empty_candidate()
        raw["entities"] = [entity()]
        self.assert_code("unknown_target", lambda: self.validate(raw, graph_context=graph))
        self.assert_code("invalid_type", lambda: self.validate(empty_candidate(), graph_context=[]))

    def test_unknown_item_keys_and_safe_diagnostics(self):
        raw = self.candidate()
        raw["entities"][0]["SECRET"] = "private"
        self.assert_code("invalid_fields", lambda: self.validate(raw))
        error = GraphError("SECRET", code="SECRET", path="entities[0].SECRET")
        self.assertNotIn("SECRET", str(error))
        self.assertEqual(error.safe_details()["path"], "$")
        error.path = ["private"]
        self.assertEqual(error.safe_details()["path"], "$")


if __name__ == "__main__":
    unittest.main()

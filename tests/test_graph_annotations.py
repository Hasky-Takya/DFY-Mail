import copy
import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from graph_annotations import freeze_annotation, validate_annotation
from graph_memory import GraphError, digest, empty_candidate, freeze_candidate, validate_candidate


WHEN = "2020-01-02T03:04:05Z"


def source():
    return {"id": "legacy-memory", "version": 2, "content": "Alice joined Project X.", "source": "user",
            "domain": "user_report", "updated_at": WHEN,
            "source_refs": [{"source": "user", "quote": "Bob and Alice are friends."},
                            {"source": "llm", "quote": "Untrusted assistant quote."}]}


def candidate(quote="Alice"):
    raw = empty_candidate()
    raw["facts"] = [{"ref": "legacy-memory", "text": "Alice joined a project", "category": "memory",
                     "source": "user", "domain": "user_report", "evidence": quote}]
    raw["entities"] = [{"ref": "P1", "type": "person", "name": "Alice", "aliases": [],
                        "target_id": None, "expected_version": None,
                        "source": "user", "domain": "user_report", "evidence": quote}]
    return raw


class GraphAnnotationTests(unittest.TestCase):
    def freeze(self, raw=None, **kwargs):
        item = source()
        args = dict(source_item=item, kind="memory", story_context={"events": [], "memories": [item]})
        args.update(kwargs)
        return freeze_annotation(candidate() if raw is None else raw, **args)

    def assert_code(self, code, action):
        with self.assertRaises(GraphError) as caught:
            action()
        self.assertEqual(caught.exception.code, code)

    def test_historical_freeze_is_pure_stable_and_has_no_mail(self):
        raw = candidate()
        original = copy.deepcopy(raw)
        package = self.freeze(raw)
        self.assertEqual(raw, original)
        self.assertEqual(package, self.freeze(raw))
        self.assertEqual(package["format"], "dfy.graph_annotation")
        self.assertEqual(package["observed_at"], WHEN)
        self.assertNotIn("dialog_id", package)
        self.assertTrue(all(ref["kind"] != "mail" for ref in package["source_refs"]))
        self.assertIn({"kind": "memory", "id": "legacy-memory", "version": 2, "digest": digest(source())}, package["source_refs"])
        evidence = package["entities"][0]["evidence"]
        self.assertEqual(evidence, {"origin": "legacy_item", "source": "user", "quote": "Alice", "source_ref": "legacy-memory", "source_version": 2})
        self.assertIs(validate_annotation(package), package)
        self.assertIs(validate_annotation(package, {"events": [], "memories": [source()]}), package)

    def test_individual_legacy_quote_can_supply_evidence(self):
        package = self.freeze(candidate("Bob and Alice"))
        self.assertEqual(package["entities"][0]["evidence"]["origin"], "legacy_quote")

    def test_no_cross_fragment_or_other_source_evidence(self):
        self.assert_code("evidence_not_in_source", lambda: self.freeze(candidate("Project X.\nBob")))
        self.assert_code("evidence_not_in_source", lambda: self.freeze(candidate("Untrusted assistant quote")))
        self.assert_code("evidence_not_in_source", lambda: self.freeze(candidate("alice")))

    def test_wrong_canonical_version_and_domain_rejected(self):
        altered = source()
        altered["version"] = 3
        self.assert_code("invalid_binding", lambda: self.freeze(source_item=altered))
        raw = candidate()
        raw["facts"][0]["domain"] = "shared_story"
        self.assert_code("source_domain_mismatch", lambda: self.freeze(raw))

    def test_package_tampering_and_stale_context_rejected(self):
        package = self.freeze()
        stale = source()
        stale["content"] = "Changed source"
        self.assert_code("invalid_binding", lambda: validate_annotation(package, {"memories": [stale]}))
        package["entities"][0]["evidence"]["origin"] = "mail"
        self.assert_code("invalid_binding", lambda: validate_annotation(package))

    def test_no_local_story_aliases_or_hidden_graph_targets(self):
        raw = candidate()
        raw["facts"][0]["ref"] = "M1"
        self.assert_code("unknown_reference", lambda: self.freeze(raw))
        raw = candidate()
        raw["entities"][0].update(target_id="hidden", expected_version=1)
        self.assert_code("unknown_target", lambda: self.freeze(raw))

    def test_legacy_entity_can_be_updated_online_and_vice_versa(self):
        historical = self.freeze()
        graph = {key: historical[key] for key in empty_candidate()}
        old = graph["entities"][0]
        raw = empty_candidate()
        item = candidate()["entities"][0]
        item.update(target_id=old["id"], expected_version=1, aliases=["Al"])
        raw["entities"] = [item]
        validated = validate_candidate(raw, story_context={}, graph_context=graph, user_body="Alice", assistant_body="")
        online = freeze_candidate(validated, dialog_id="current", observed_at="2026-01-01T00:00:00Z", user_message_id="<u>", assistant_message_id="<a>")
        self.assertEqual(online["entities"][0]["version"], 2)
        graph = {key: online[key] for key in empty_candidate()}
        raw["entities"][0]["expected_version"] = 2
        package = self.freeze(raw, graph_context=graph)
        self.assertEqual(package["entities"][0]["version"], 3)
        self.assertEqual(package["entities"][0]["evidence"]["origin"], "legacy_item")

    def test_new_historical_assertions_share_live_semantic_matrix(self):
        raw = candidate()
        raw["entities"].append({**raw["entities"][0], "ref": "P2", "name": "Bob"})
        relation = {"target_id": None, "expected_version": None, "subject": "P1", "object": "P2",
                    "predicate": "participates_in", "state": "active", "support_ref": None,
                    "source": "user", "domain": "user_report", "evidence": "Alice"}
        raw["relations"] = [relation]
        self.assert_code("invalid_endpoint_type", lambda: self.freeze(raw))
        raw["relations"][0].update(predicate="friend_of", object="P1")
        self.assert_code("self_loop", lambda: self.freeze(raw))
        raw["relations"][0].update(predicate="works_on", object="P2")
        raw["entities"][1]["type"] = "project"
        self.assertEqual(self.freeze(raw)["relations"][0]["predicate"], "works_on")

    def test_legacy_bad_edge_schema_remains_replayable(self):
        from graph_memory import stable_id, _identity
        raw = candidate()
        raw["entities"].append({**raw["entities"][0], "ref": "P2", "name": "Bob"})
        raw["relations"] = [{"target_id": None, "expected_version": None, "subject": "P1", "object": "P2",
                             "predicate": "friend_of", "state": "active", "support_ref": None,
                             "source": "user", "domain": "user_report", "evidence": "Alice"}]
        package = self.freeze(raw)
        item = package["relations"][0]
        item.update(predicate="participates_in")
        item["id"] = stable_id("relation", *_identity("relations", item))
        package["package_id"] = stable_id("annotation", {key: value for key, value in package.items() if key != "package_id"})
        self.assertIs(validate_annotation(package), package)

    def test_empty_annotation_and_kind_aliases(self):
        package = self.freeze(empty_candidate(), kind="memories")
        self.assertEqual(package["status"], "complete")
        self.assertEqual(package["facts"], [])
        self.assert_code("invalid_enum", lambda: self.freeze(kind="mail"))


if __name__ == "__main__":
    unittest.main()

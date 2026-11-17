"""Disposable graph cache tests use only synthetic values and temporary dirs."""

import copy
from contextlib import closing
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch


SRC = Path(__file__).resolve().parents[1] / "src"
SPEC = importlib.util.spec_from_file_location("graph_store_under_test", SRC / "graph_store.py")
graph = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(graph)
# These pure modules perform no application startup or persistent-data reads.
sys.path.insert(0, str(SRC))
try:
    import graph_memory as core
finally:
    sys.path.remove(str(SRC))

AT = "2026-10-25T08:00:00+08:00"
LATER = "2026-10-26T08:00:00+08:00"
BODY = "Alex likes quiet places and is working on a test project."


def source(identifier="event-1", *, version=1, domain="shared_story", category=None):
    value = {"id": identifier, "version": version, "content": "Full uncompressed test source",
             "domain": domain, "source": "user", "status": "planned", "importance": 3,
             "people": ["Alex"], "created_at": AT, "updated_at": AT if version == 1 else LATER,
             "source_refs": [{"dialog_id": "dialog-1", "message_id": "<user@test.invalid>",
                              "source": "user", "quote": "quiet places", "time": AT}]}
    if category:
        value.update(category=category, status="active", origin="reported_preference", related_event_ids=[])
    else:
        value.update(title="Synthetic event", tense="future", event_time={"raw": "tomorrow", "start": None,
                                                                          "end": None, "anchor": AT})
    return value


def context(*items):
    return {"revision": 1, "applied_dialog_ids": ["dialog-1"],
            "events": [item for item in items if "category" not in item],
            "memories": [item for item in items if "category" in item]}


def dialog(package=None, **changes):
    value = {"id": "dialog-1", "status": "sent", "user_message_id": "<user@test.invalid>",
             "assistant_message_id": "<assistant@test.invalid>"}
    if package is not None:
        value["graph_updates"] = package
    value.update(changes)
    return value


def seal(package):
    package = copy.deepcopy(package)
    package.pop("package_id", None)
    package["package_id"] = core.stable_id("package", package)
    core.validate_package(package)
    return package


def package(story_item=None, *, identifier="dialog-1", fact_text="Compact view", entity=False,
            state=False, observed_at=AT):
    refs = [{"kind": "mail", "id": "<user@test.invalid>", "version": 1, "digest": core.digest(BODY)}]
    evidence = {"source": "user", "quote": "quiet places", "message_id": "<user@test.invalid>",
                "dialog_id": identifier}
    domain = story_item["domain"] if story_item else "shared_story"
    result = {"schema_version": 1, "dialog_id": identifier, "observed_at": observed_at,
              "status": "complete", "reason": None, "source_refs": refs, **core.empty_candidate()}
    if story_item:
        kind = "memory" if "category" in story_item else "event"
        refs.append({"kind": kind, "id": story_item["id"], "version": story_item["version"],
                     "digest": core.digest(story_item)})
        category = "preference" if kind == "memory" else "event"
        fact = {"id": core.stable_id("fact", domain, story_item["id"], category),
                "version": 1, "ref": story_item["id"], "text": fact_text,
                "category": category, "source": "user", "domain": domain, "evidence": evidence}
        result["facts"].append(fact)
    if entity or state:
        person = {"id": core.stable_id("entity", domain, "person", "alex"), "version": 1,
                  "type": "person", "name": "Alex", "aliases": [], "source": "user",
                  "domain": domain, "evidence": evidence}
        result["entities"].append(person)
    if state:
        item = {"subject": person["id"], "predicate": "working_on", "value": "Synthetic test project",
                "state": "active", "support_ref": story_item["id"] if story_item else None,
                "valid_until": LATER, "source": "user", "domain": domain, "evidence": evidence,
                "version": 1}
        item["id"] = core.stable_id("state", domain, item["subject"], item["predicate"])
        result["states"].append(item)
    return seal(result)


class GraphStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def store(self, **kwargs):
        store = graph.GraphStore(self.directory, **kwargs)
        self.addCleanup(store.close)
        return store

    def write_manifest(self, **changes):
        value = {"schema_version": 1, "mode": "shadow", **changes}
        (self.directory / graph.MANIFEST_FILENAME).write_text(json.dumps(value), encoding="utf-8")

    def test_baseline_deterministic_lossless_and_conservative(self):
        event = source()
        memory = source("memory-1", domain="user_preference", category="preference")
        snapshot = context(event, memory)
        original = copy.deepcopy(snapshot)
        store = self.store()
        view = store.sync({"dialogs": [dialog()]}, snapshot)
        self.assertEqual(snapshot, original)
        self.assertEqual(view["schema_version"], 2)
        self.assertEqual(view["format"], "dfy.compact_event_graph")
        self.assertEqual(view["events"][0]["text"], event["content"])
        for field in ("id", "version", "domain", "status", "importance"):
            self.assertEqual(view["events"][0][field], event[field])
        self.assertTrue(view["events"][0]["projection_pending"])
        self.assertNotIn("source_refs", view["events"][0])
        self.assertNotIn("quiet places", json.dumps(view))
        self.assertEqual(view["preferences"][0]["id"], memory["id"])
        self.assertEqual(view["preferences"][0]["source_ref"]["digest"], core.digest(memory))
        self.assertEqual({row["predicate"] for row in view["relations"]}, {"mentioned_in"})
        self.assertEqual(len(view["entities"]), 2)  # same name, different domains
        self.assertEqual(view["coverage"], {"status": "partial", "pending_source_ids": ["event-1", "memory-1"],
                                             "incomplete_dialog_ids": ["dialog-1"],
                                             "rejected_assertions_count": 0, "rejected_assertion_ids": []})
        self.assertEqual(store.sync({"dialogs": [dialog()]}, snapshot), view)
        self.assertEqual(graph.synchronize(self.directory, {"dialogs": [dialog()]}, snapshot), view)

    def test_sent_only_and_package_at_dialog_sibling(self):
        event = source()
        pkg = package(event)
        store = self.store()
        view = store.sync({"dialogs": [dialog(pkg, status="prepared")]}, context(event))
        self.assertEqual(view["facts"], [])
        view = store.sync({"dialogs": [dialog(pkg)]}, context(event))
        self.assertEqual(view["events"][0]["text"], "Compact view")
        self.assertEqual(view["events"][0]["id"], event["id"])
        self.assertEqual(view["events"][0]["version"], event["version"])
        self.assertEqual(view["coverage"]["status"], "complete")
        self.assertEqual(store.get_fact(pkg["facts"][0]["id"])["ref"], event["id"])
        self.assertEqual(store.get_fact(event["id"])["text"], "Compact view")
        self.assertNotIn("evidence", view["facts"][0])
        self.assertEqual(store.canonical_graph_context()["facts"], pkg["facts"])
        self.assertEqual(store.canonical_graph_context([])["facts"], [])

    def test_disabled_active_still_publishes_without_touching_legacy(self):
        self.write_manifest(mode="active", state="completed", backups={})
        legacy = self.directory / "event_legacy.json"
        legacy.write_text("legacy sentinel", encoding="utf-8")
        store = self.store(settings={"enabled": False})
        view = store.sync({"dialogs": [dialog()]}, context(source()))
        self.assertEqual(graph.event_filename(self.directory), "event_legacy.json")
        self.assertEqual(json.loads((self.directory / "event.json").read_text(encoding="utf-8")), view)
        self.assertEqual(json.loads((self.directory / "event_v2.json").read_text(encoding="utf-8")), view)
        self.assertEqual(legacy.read_text(encoding="utf-8"), "legacy sentinel")

    def test_shadow_never_writes_formal_event_or_authority(self):
        (self.directory / "event.json").write_text("legacy sentinel", encoding="utf-8")
        (self.directory / "dialog.json").write_text("authority sentinel", encoding="utf-8")
        graph.synchronize(self.directory, {"dialogs": [dialog()]}, context(source()))
        self.assertEqual(graph.event_filename(self.directory), "event.json")
        self.assertEqual((self.directory / "event.json").read_text(encoding="utf-8"), "legacy sentinel")
        self.assertEqual((self.directory / "dialog.json").read_text(encoding="utf-8"), "authority sentinel")

    def test_identical_sync_does_not_write_sql_or_export(self):
        store = self.store()
        data, snapshot = {"dialogs": [dialog()]}, context(source())
        view = store.sync(data, snapshot)
        stamp = (self.directory / "event_v2.json").stat().st_mtime_ns
        statements = []
        store._db.set_trace_callback(statements.append)
        with patch.object(graph, "_project", side_effect=AssertionError("must not reproject")):
            self.assertEqual(store.sync(data, snapshot), view)
        self.assertFalse(any(sql.startswith(("BEGIN", "DELETE", "INSERT", "UPDATE")) for sql in statements))
        self.assertEqual((self.directory / "event_v2.json").stat().st_mtime_ns, stamp)
        changed = copy.deepcopy(data)
        changed["dialogs"].append(dialog(status="prepared", id="unsent-other"))
        self.assertEqual(store.sync(changed, snapshot)["generation"], view["generation"])

    def test_current_source_hash_version_required_for_annotation(self):
        old = source()
        pkg = package(old)
        updated = source(version=2)
        updated["content"] = "A revised full canonical source"
        store = self.store()
        view = store.sync({"dialogs": [dialog()]}, context(updated), annotations=[pkg])
        self.assertEqual(view["events"][0]["text"], updated["content"])
        self.assertTrue(view["events"][0]["projection_pending"])
        tampered = copy.deepcopy(old)
        tampered["content"] = "Changed same-version canonical content"
        view = store.sync({"dialogs": [dialog()]}, context(tampered), annotations=[pkg])
        self.assertEqual(view["events"][0]["text"], tampered["content"])

    def test_source_revision_keeps_graph_version_lineage(self):
        import story_memory as story
        initial = story.empty_context()
        update = {"target_id": None, "title": "Test plan", "content": BODY,
                  "source": "user", "domain": "shared_story", "importance": 3,
                  "people": ["Alex"], "status": "planned", "tense": "future",
                  "event_time": {"raw": None, "start": None, "end": None}, "evidence": "quiet places"}
        def commit(ctx, identifier, timestamp):
            validated = story.validate_updates({"event_updates": [update], "memory_updates": []},
                                               story_context=ctx, user_body=BODY, assistant_body="reply")
            return story.materialize_updates(validated, dialog_id=identifier, story_context=ctx,
                                              observed_at=timestamp, user_message_id="<user@test.invalid>",
                                              assistant_message_id="<assistant@test.invalid>")
        story1 = commit(initial, "dialog-1", AT)
        first = story1["event_updates"][0]
        midway = story._apply(initial, story1)
        update["target_id"] = first["id"]
        update["content"] = "The plan was revised."
        story2 = commit(midway, "dialog-2", LATER)
        second = story2["event_updates"][0]
        pkg1 = package(first, entity=True)
        pkg2 = package(second, identifier="dialog-2", entity=True, fact_text="Revised compact view", observed_at=LATER)
        for kind in ("facts", "entities"):
            before = pkg1[kind][0]
            pkg2[kind][0]["version"] = 2
            pkg2["source_refs"].append({"kind": "fact" if kind == "facts" else "entity",
                                        "id": before["id"], "version": 1, "digest": core.digest(before)})
        pkg2 = seal(pkg2)
        rows = [dialog(pkg1, story_updates=story1), dialog(pkg2, id="dialog-2", story_updates=story2)]
        store = self.store()
        final = story._apply(midway, story2)
        view = store.sync({"dialogs": rows}, final)
        self.assertEqual(view["events"][0]["text"], "Revised compact view")
        self.assertEqual(store.canonical_graph_context()["facts"][0]["version"], 2)
        self.assertEqual(store.canonical_graph_context()["entities"][0]["version"], 2)
        self.assertEqual(view["coverage"]["status"], "complete")
        self.assertEqual(store.sync({"dialogs": rows}, final), view)

    def test_annotations_hash_validation_all_lines_and_replay(self):
        event = source()
        pkg = package(event)
        path = self.directory / "graph_annotations.jsonl"
        envelope = {"package": pkg, "hash": core.digest(pkg)}
        path.write_text(json.dumps(envelope) + "\n", encoding="utf-8")
        store = self.store()
        view = store.sync({"dialogs": [dialog()]}, context(event))
        self.assertEqual(view["coverage"]["status"], "complete")
        saved = (self.directory / "event_v2.json").read_bytes()
        for bad in ("\n", "not JSON\n", '{"package":{},"hash":"invalid"}\n',
                    '{"x":1,"x":2}\n', '{"x":NaN}\n'):
            with self.subTest(bad=bad):
                path.write_text(json.dumps(envelope) + "\n" + bad, encoding="utf-8")
                with self.assertRaisesRegex(graph.GraphStoreError, "^Graph projection unavailable;"):
                    store.sync({"dialogs": [dialog()]}, context(event))
                self.assertEqual(store.export(), view)
                self.assertEqual((self.directory / "event_v2.json").read_bytes(), saved)

    def test_invalid_package_is_not_silently_ignored(self):
        event = source()
        store = self.store()
        before = store.sync({"dialogs": [dialog()]}, context(event))
        invalid = package(event)
        invalid["facts"][0]["text"] = "PRIVATE_CHANGED_WITHOUT_HASH"
        with self.assertRaises(graph.GraphStoreError) as caught:
            store.sync({"dialogs": [dialog(invalid)]}, context(event))
        self.assertNotIn("PRIVATE", str(caught.exception))
        self.assertEqual(store.export(), before)
        wrong_dialog = dialog(package(event), id="wrong-dialog")
        with self.assertRaises(graph.GraphStoreError):
            store.sync({"dialogs": [wrong_dialog]}, context(event))

    def test_transaction_rollback_preserves_all_tables(self):
        event = source()
        store = self.store()
        before = store.sync({"dialogs": [dialog()]}, context(event))
        store._db.execute("CREATE TRIGGER forbid_fact BEFORE INSERT ON facts BEGIN SELECT RAISE(ABORT, 'test'); END")
        revised = source(version=2)
        with self.assertRaises(graph.GraphStoreError):
            store.sync({"dialogs": [dialog()]}, context(revised))
        self.assertEqual(store.export(), before)
        self.assertEqual(store.get_fact(event["id"])["version"], 1)

    def test_corrupt_database_is_not_overwritten(self):
        path = self.directory / graph.DB_FILENAME
        sentinel = b"this is a damaged sqlite database with private data"
        path.write_bytes(sentinel)
        with self.assertRaises(graph.GraphStoreError) as caught:
            self.store()
        self.assertEqual(path.read_bytes(), sentinel)
        self.assertNotIn("private", str(caught.exception))

    def test_wrong_database_schema_and_version_are_not_reset(self):
        path = self.directory / graph.DB_FILENAME
        with closing(sqlite3.connect(path)) as db:
            db.execute("CREATE TABLE sentinel(id TEXT)")
            db.execute("INSERT INTO sentinel VALUES ('keep')")
            db.commit()
        with self.assertRaises(graph.GraphStoreError):
            self.store()
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(db.execute("SELECT * FROM sentinel").fetchall(), [("keep",)])

    def test_schema_tables_indexes_quick_check_and_reopen(self):
        event = source()
        store = self.store()
        view = store.sync({"dialogs": [dialog(package(event, entity=True))]}, context(event))
        self.assertEqual(store._db.execute("PRAGMA user_version").fetchone()[0], graph.DB_VERSION)
        self.assertEqual(store._db.execute("PRAGMA quick_check").fetchall(), [("ok",)])
        for table in graph._TABLES:
            indexes = store._db.execute(f"PRAGMA index_list({table})").fetchall()
            self.assertTrue(indexes)
        store.close()
        self.assertEqual(self.store().export(), view)
        with self.assertRaises(graph.GraphStoreError):
            store.export()

    def test_strict_manifest(self):
        self.assertEqual(graph.read_manifest(self.directory)["mode"], "shadow")
        cases = [{"schema_version": 2, "mode": "active"}, {"schema_version": True, "mode": "active"},
                 {"schema_version": 1, "mode": "unknown"}, {"schema_version": 1, "mode": []},
                 {"schema_version": 1, "mode": "active", "active": True},
                 {"schema_version": 1, "mode": "shadow", "source_digest": "invalid"}]
        path = self.directory / graph.MANIFEST_FILENAME
        for value in cases:
            with self.subTest(value=value):
                path.write_text(json.dumps(value), encoding="utf-8")
                with self.assertRaises(graph.GraphStoreError):
                    graph.read_manifest(self.directory)
        path.write_text('{"schema_version":1,"mode":"shadow","mode":"active"}', encoding="utf-8")
        with self.assertRaises(graph.GraphStoreError):
            graph.event_filename(self.directory)

    def test_request_view_bounds_dedup_visible_scope_and_baseline_hidden(self):
        events = [source("event-" + str(index)) for index in range(8)]
        memory = source("memory-1", domain="user_preference", category="preference")
        store = self.store()
        store.sync({"dialogs": [dialog()]}, context(*events, memory))
        view = store.request_view(limit=2, max_chars=3000)
        self.assertLessEqual(len(graph._dump(view)), 3000)
        self.assertTrue(all(len(view[kind]) <= 2 for kind in graph._COLLECTIONS))
        self.assertEqual(view["entities"], [])
        ids = [item["id"] for kind in ("preferences", "profiles", "memories") for item in view[kind]]
        self.assertEqual(len(ids), len(set(ids)))
        filtered = store.request_view(limit=10, max_chars=12000, visible_ids={"event-1"}, scope="shared_story")
        self.assertEqual([item["id"] for item in filtered["events"]], ["event-1"])
        self.assertEqual(filtered["preferences"], [])
        with self.assertRaises(graph.GraphStoreError):
            store.request_view(max_chars=5)
        with self.assertRaises(graph.GraphStoreError):
            store.request_view(limit=True)

    def test_request_fact_keeps_semantic_id_and_source_reference(self):
        event = source()
        pkg = package(event, entity=True)
        store = self.store()
        store.sync({"dialogs": [dialog(pkg)]}, context(event))
        view = store.request_view(max_chars=16000)
        self.assertEqual(view["facts"][0]["id"], pkg["facts"][0]["id"])
        self.assertEqual(view["facts"][0]["source_ref"]["id"], event["id"])
        self.assertEqual(view["events"], [])
        self.assertEqual([item["id"] for item in view["entities"]], [pkg["entities"][0]["id"]])

    def test_queries_parameterized_and_context_is_one_hop(self):
        event = source()
        store = self.store()
        view = store.sync({"dialogs": [dialog()]}, context(event))
        self.assertIsNone(store.get_fact("' OR 1=1 --"))
        entity = view["entities"][0]
        self.assertEqual(store.resolve_entity("  ALEX ")[0]["id"], entity["id"])
        ctx = store.get_entity_context(entity["id"], limit=1)
        self.assertEqual(len(ctx["relations"]), 1)
        self.assertEqual(ctx["events"][0]["id"], event["id"])
        self.assertEqual(len(store.get_event_context(event["id"], limit=0)["relations"]), 0)
        self.assertEqual(store.get_entity_context("unknown")["entities"], [])

    def test_expired_and_retracted_states_omit_current_view(self):
        event = source()
        pkg = package(event, entity=True, state=True)
        store = self.store()
        view = store.sync({"dialogs": [dialog(pkg)]}, context(event))
        self.assertEqual(len(view["states"]), 1)
        late = seal({**pkg, "observed_at": LATER})
        view = store.sync({"dialogs": [dialog(late)]}, context(event))
        self.assertEqual(view["states"], [])
        retracted = copy.deepcopy(pkg)
        retracted["states"][0]["state"] = "retracted"
        retracted = seal(retracted)
        view = store.sync({"dialogs": [dialog(retracted)]}, context(event))
        self.assertEqual(view["states"], [])

    def test_read_views_expire_states_without_new_mail(self):
        event = source()
        pkg = package(event, entity=True, state=True)
        store = self.store()
        self.assertEqual(len(store.sync({"dialogs": [dialog(pkg)]}, context(event))["states"]), 1)
        # The deterministic export is an as-of snapshot, while reads must also
        # account for wall time passing even if no new source has arrived.
        self.assertEqual(store.request_view()["states"], [])
        entity_id = pkg["entities"][0]["id"]
        self.assertEqual(store.get_entity_context(entity_id)["states"], [])

    def test_historical_annotation_without_mail_or_dialog_id(self):
        annotations = graph._core("graph_annotations")
        memory = source("historical-memory", domain="user_report", category="profile")
        raw = core.empty_candidate()
        raw["facts"] = [{"ref": memory["id"], "text": "Short historical view", "category": "profile",
                         "source": "user", "domain": "user_report", "evidence": "Full uncompressed"}]
        pkg = annotations.freeze_annotation(raw, source_item=memory, kind="memory",
                                            story_context=context(memory))
        self.assertNotIn("dialog_id", pkg)
        path = self.directory / "graph_annotations.jsonl"
        path.write_text(json.dumps({"package": pkg, "hash": core.digest(pkg)}) + "\n", encoding="utf-8")
        store = self.store()
        view = store.sync({"dialogs": []}, context(memory))
        self.assertEqual(view["coverage"]["status"], "complete")
        self.assertEqual(view["memories"][0]["text"], "Short historical view")
        self.assertEqual(view["profiles"][0]["source_ref"]["id"], memory["id"])
        self.assertEqual(store.canonical_graph_context()["facts"][0]["evidence"]["origin"], "legacy_item")
        changed = copy.deepcopy(memory)
        changed["content"] = "Changed canonical body"
        view = store.sync({"dialogs": []}, context(changed))
        self.assertEqual(view["memories"][0]["text"], changed["content"])
        self.assertEqual(view["facts"], [])

    def test_historical_then_online_dependency_replays_independent_of_file_order(self):
        annotations = graph._core("graph_annotations")
        memory = source("historical-memory", domain="user_report", category="profile")
        raw = core.empty_candidate()
        raw["entities"] = [{"ref": "P1", "type": "person", "name": "Alex", "aliases": [],
                            "target_id": None, "expected_version": None,
                            "source": "user", "domain": "user_report", "evidence": "Full"}]
        prior = annotations.freeze_annotation(raw, source_item=memory, kind="memory", story_context=context(memory))
        old = prior["entities"][0]
        online_raw = core.empty_candidate()
        online_raw["entities"] = [{**raw["entities"][0], "target_id": old["id"], "expected_version": 1,
                                   "aliases": ["Al"], "evidence": "Alex"}]
        validated = core.validate_candidate(online_raw, story_context=context(memory),
                                             graph_context={kind: prior[kind] for kind in core.KINDS},
                                             user_body=BODY, assistant_body="")
        online = core.freeze_candidate(validated, dialog_id="dialog-1", observed_at=LATER,
                                       user_message_id="<user@test.invalid>",
                                       assistant_message_id="<assistant@test.invalid>")
        store = self.store()
        view = store.sync({"dialogs": [dialog(online)]}, context(memory), annotations=[prior])
        projected = [item for item in view["entities"] if item["id"] == old["id"]][0]
        self.assertEqual(projected["version"], 2)
        self.assertEqual(projected["aliases"], ["Al"])
        self.assertEqual(store.canonical_graph_context()["entities"][0]["version"], 2)

    def test_empty_or_entity_only_annotation_does_not_claim_text_compaction(self):
        annotations = graph._core("graph_annotations")
        event = source()
        store = self.store()
        for raw in (core.empty_candidate(), {**core.empty_candidate(), "entities": [
                {"ref": "P1", "type": "person", "name": "Alex", "aliases": [],
                 "target_id": None, "expected_version": None, "source": "user",
                 "domain": event["domain"], "evidence": "Full"}]}):
            pkg = annotations.freeze_annotation(raw, source_item=event, kind="event", story_context=context(event))
            view = store.sync({"dialogs": [dialog()]}, context(event), annotations=[pkg])
            self.assertEqual(view["coverage"]["pending_source_ids"], [event["id"]])
            self.assertTrue(view["events"][0]["projection_pending"])

    def test_coverage_historical_backfill_clears_only_missing_graph_dialogs(self):
        annotations = graph._core("graph_annotations")
        event = source()
        raw = core.empty_candidate()
        raw["facts"] = [{"ref": event["id"], "text": "Historical event", "category": "event",
                         "source": "user", "domain": event["domain"], "evidence": "Full"}]
        pkg = annotations.freeze_annotation(raw, source_item=event, kind="event", story_context=context(event))
        store = self.store()
        view = store.sync({"dialogs": [dialog()]}, context(event), annotations=[pkg])
        self.assertEqual(view["coverage"]["status"], "complete")
        incomplete = core.incomplete_package(dialog_id="dialog-1", observed_at=AT, reason="extraction_failed")
        view = store.sync({"dialogs": [dialog(incomplete)]}, context(event), annotations=[pkg])
        self.assertEqual(view["coverage"]["incomplete_dialog_ids"], ["dialog-1"])
        empty = {"revision": 0, "applied_dialog_ids": [], "events": [], "memories": []}
        self.assertEqual(store.sync({"dialogs": [dialog()]}, empty)["coverage"]["status"], "complete")

    def test_logical_cache_corruption_is_not_silently_repaired(self):
        store = self.store()
        data, snapshot = {"dialogs": [dialog()]}, context(source())
        store.sync(data, snapshot)
        store._db.execute("UPDATE view SET value=? WHERE id=?", ("{}", "current"))
        with self.assertRaises(graph.GraphStoreError):
            store.sync(data, snapshot)
        store.close()
        with self.assertRaises(graph.GraphStoreError):
            self.store()

    def test_sqlite_resources_strict_settings_and_read_cache_invalidation(self):
        for settings in ({"unknown": 1}, {"sqlite_cache_kib": True}, {"sqlite_cache_kib": 0}):
            with self.assertRaises(graph.GraphStoreError):
                self.store(settings=settings)
        store = self.store(settings={"sqlite_cache_kib": 1024})
        self.assertEqual(store._db.execute("PRAGMA cache_size").fetchone()[0], -1024)
        self.assertEqual(store._db.execute("PRAGMA temp_store").fetchone()[0], 1)
        data, snapshot = {"dialogs": [dialog()]}, context(source())
        store.sync(data, snapshot)
        statements = []
        store._db.set_trace_callback(statements.append)
        store.export()
        store.export()
        self.assertEqual(sum("SELECT value FROM view" in sql for sql in statements), 1)
        with closing(sqlite3.connect(store.path)) as other:
            other.execute("UPDATE view SET value='{}' WHERE id='current'")
            other.commit()
        with self.assertRaises(graph.GraphStoreError):
            store.export()
        with self.assertRaises(graph.GraphStoreError):
            store.export()

    def test_legacy_bad_edges_isolated_without_rewriting_hash_or_refs(self):
        event = source()
        pkg = package(event, entity=True)
        evidence = pkg["entities"][0]["evidence"]
        person = pkg["entities"][0]["id"]
        for kind, subject, predicate, object_id in (("event_relations", event["id"], "follows", event["id"]),
                ("relations", person, "participates_in", person)):
            item = {"version": 1, "subject": subject, "predicate": predicate, "object": object_id,
                    "state": "active", "support_ref": None, "source": "user", "domain": event["domain"],
                    "evidence": evidence}
            item["id"] = core.stable_id(core._SOURCE_KINDS[kind], *core._identity(kind, item))
            pkg[kind].append(item)
        pkg = seal(pkg)  # Historical structural validation must still accept it.
        original = copy.deepcopy(pkg)
        store = self.store()
        view = store.sync({"dialogs": [dialog(pkg)]}, context(event))
        self.assertEqual(pkg, original)
        self.assertEqual(view["event_relations"], [])
        self.assertFalse(any(item["predicate"] == "participates_in" for item in view["relations"]))
        self.assertEqual(view["coverage"]["rejected_assertions_count"], 2)
        self.assertEqual(view["coverage"]["status"], "partial")
        bounded = store.request_view(limit=1, max_chars=12000)
        self.assertEqual(len(bounded["coverage"]["rejected_assertion_ids"]), 1)
        self.assertEqual(bounded["coverage"]["rejected_assertions_count"], 2)
        self.assertEqual(store.canonical_graph_context()["relations"], [])

    def test_identity_cross_domain_and_multi_source_ambiguity_is_not_merged(self):
        event = source()
        event["people"] = ["助手", "才羽桃井"]
        pkg = package(event, entity=True)
        evidence = pkg["entities"][0]["evidence"]
        pkg["entities"] = []
        for name, item_source, domain in (("助手", "user", "shared_story"),
                ("才羽桃井", "llm", "shared_story"), ("才羽桃井", "user", "user_report")):
            item_evidence = {**evidence, "source": item_source}
            if item_source == "llm":
                item_evidence["message_id"] = "<assistant@test.invalid>"
                pkg["source_refs"].append({"kind": "mail", "id": item_evidence["message_id"],
                                           "version": 1, "digest": core.digest("reply")})
            item = {"version": 1, "type": "person", "name": name, "aliases": [],
                    "source": item_source, "domain": domain, "evidence": item_evidence}
            item["id"] = core.stable_id("entity", *core._identity("entities", item))
            pkg["entities"].append(item)
        pkg = seal(pkg)
        store = self.store()
        view = store.sync({"dialogs": [dialog(pkg)]}, context(event))
        same_domain = [item for item in view["entities"] if item["domain"] == "shared_story"]
        self.assertEqual(len(same_domain), 4)  # Two baseline + two ambiguous real targets.
        self.assertFalse(any(item.get("origin_ids") for item in same_domain))
        self.assertEqual(len([item for item in view["entities"] if item["domain"] == "user_report"]), 1)

    def test_safe_graph_logging_never_contains_source_text(self):
        store = self.store()
        pkg = package(source())
        pkg["facts"][0]["text"] = "PRIVATE_BODY_MUST_NOT_LOG"
        with self.assertLogs("dfy.graph", level="WARNING") as captured:
            with self.assertRaises(graph.GraphStoreError):
                store.sync({"dialogs": [dialog(pkg)]}, context(source()))
        self.assertNotIn("PRIVATE_BODY", " ".join(captured.output))
        self.assertTrue(all("code=" in line for line in captured.output))

    def test_projection_identity_real_origin_ids_and_request_targets(self):
        event = source()
        event["people"] = ["助手", "桃井", "才羽桃井", "我", "你"]
        pkg = package(event, entity=True)
        evidence = pkg["entities"][0]["evidence"]
        pkg["entities"] = []
        for name in ("助手", "才羽桃井"):
            item = {"version": 1, "type": "person", "name": name, "aliases": [],
                    "source": "user", "domain": event["domain"], "evidence": evidence}
            item["id"] = core.stable_id("entity", *core._identity("entities", item))
            pkg["entities"].append(item)
        old_alias, canonical = pkg["entities"]
        project = {"version": 1, "type": "project", "name": "Project X", "aliases": [],
                   "source": "user", "domain": event["domain"], "evidence": evidence}
        project["id"] = core.stable_id("entity", *core._identity("entities", project))
        pkg["entities"].append(project)
        relation = {"version": 1, "subject": old_alias["id"], "predicate": "works_on", "object": project["id"],
                    "state": "active", "support_ref": None, "source": "user", "domain": event["domain"], "evidence": evidence}
        relation["id"] = core.stable_id("relation", *core._identity("relations", relation))
        pkg["relations"].append(relation)
        pkg = seal(pkg)
        original = copy.deepcopy(pkg)
        store = self.store()
        view = store.sync({"dialogs": [dialog(pkg)]}, context(event))
        self.assertEqual(pkg, original)
        self.assertEqual(len(view["entities"]), 4)  # Momoi/project/I/you.
        momoi = store.resolve_entity("助手")[0]
        self.assertEqual(momoi["name"], "才羽桃井")
        self.assertEqual(momoi["id"], canonical["id"])
        self.assertIn(old_alias["id"], momoi["origin_ids"])
        self.assertEqual(store.get_entity_context(old_alias["id"])["entities"][0]["id"], canonical["id"])
        raw = store.canonical_graph_context()
        self.assertEqual(raw["entities"], sorted(pkg["entities"], key=lambda item: item["id"]))
        request = store.request_view(limit=20, max_chars=30000)
        target = next(item for item in request["entities"] if item["id"] == old_alias["id"])
        self.assertTrue(target["target_alias"])
        self.assertEqual(target["name"], old_alias["name"])
        self.assertEqual(request["relations"][0]["subject"], old_alias["id"])
        update = core.empty_candidate()
        update["relations"] = [{key: copy.deepcopy(value) for key, value in relation.items()
                                if key not in {"id", "version", "evidence"}}]
        update["relations"][0].update(target_id=relation["id"], expected_version=1, evidence="quiet places", state="contested")
        validated = core.validate_candidate(update, story_context=context(event), graph_context=raw,
                                            user_body=BODY, assistant_body="")
        frozen = core.freeze_candidate(validated, dialog_id="dialog-2", observed_at=LATER,
                                       user_message_id="<u2>", assistant_message_id="<a2>")
        self.assertEqual(frozen["relations"][0]["subject"], old_alias["id"])
        self.assertIn({"kind": "entity", "id": old_alias["id"], "version": 1, "digest": core.digest(old_alias)}, frozen["source_refs"])
        self.assertNotEqual(self.store(settings={"assistant_name": "Other"}).sync(
            {"dialogs": [dialog(pkg)]}, context(event))["generation"], view["generation"])

    def test_context_manager_and_no_export_for_invalid_new_input(self):
        with graph.GraphStore(self.directory) as store:
            self.assertEqual(store.export(), {})
            with self.assertRaises(graph.GraphStoreError):
                store.sync({"dialogs": []}, {"revision": "invalid"})
            self.assertFalse((self.directory / "event_v2.json").exists())
        with self.assertRaises(graph.GraphStoreError):
            store.get_fact("anything")


if __name__ == "__main__":
    unittest.main()

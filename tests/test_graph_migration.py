"""Migration safety tests. All sources are synthetic and live in temporary dirs."""

import copy
from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))
try:
    import graph_memory as core
    import graph_migrate as migration
    import graph_store
    import story_memory as story
    from task_store import SingleInstanceLock
finally:
    sys.path.remove(str(SRC))

AT = "2026-10-25T08:00:00+08:00"
LATER = "2026-10-26T08:00:00+08:00"
BODY = "Synthetic Alex plans a quiet library visit."


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def sent_record(identifier="dialog-1", *, context=None, complete=True, at=AT):
    context = story.empty_context() if context is None else context
    user_id = "<user-" + identifier + "@test.invalid>"
    assistant_id = "<assistant-" + identifier + "@test.invalid>"
    raw = {"event_updates": [{
        "target_id": None, "content": BODY, "title": "Synthetic library visit",
        "source": "user", "domain": "shared_story", "importance": 3,
        "people": ["Alex"], "status": "planned", "tense": "future",
        "event_time": {"raw": None, "start": None, "end": None},
        "evidence": "quiet library visit",
    }], "memory_updates": []}
    validated = story.validate_updates(raw, story_context=context,
                                       user_body=BODY, assistant_body="Synthetic reply")
    package = story.materialize_updates(validated, dialog_id=identifier, story_context=context,
                                        observed_at=at, user_message_id=user_id,
                                        assistant_message_id=assistant_id)
    record = {"id": identifier, "type": "用户主动发送", "status": "sent",
              "received_at": at, "received_at_source": "observed_at", "sent_at": at,
              "user_summary": "Synthetic request", "assistant_summary": "Synthetic reply summary",
              "user_message_id": user_id, "assistant_message_id": assistant_id,
              "story_updates": package}
    if complete:
        candidate = core.empty_candidate()
        candidate["facts"] = [{"ref": "E1", "text": "Library visit", "category": "event",
                                "source": "user", "domain": "shared_story", "evidence": "quiet library visit"}]
        candidate = core.validate_candidate(candidate, story_context=context,
                                             user_body=BODY, assistant_body="Synthetic reply")
        record["graph_updates"] = core.freeze_candidate(
            candidate, dialog_id=identifier, story_package=package, observed_at=at,
            user_message_id=user_id, assistant_message_id=assistant_id, story_context=context)
    return record, story._apply(context, package)


class FakeBot:
    def __init__(self):
        self.calls = []

    def ask(self, question, **kwargs):
        self.calls.append((json.loads(question), kwargs))
        annotations = []
        for row in self.calls[-1][0]["sources"]:
            source = row["item"]
            candidate = core.empty_candidate()
            candidate["facts"] = [{"ref": source["id"], "text": "Library visit",
                                   "category": "event" if row["kind"] == "events" else "memory",
                                   "source": source["source"], "domain": source["domain"],
                                   "evidence": source["source_refs"][-1]["quote"]}]
            annotations.append({"source_id": source["id"], "candidate": candidate})
        return json.dumps({"annotations": annotations})


class GraphMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def seed(self, *, complete=True, projections=True):
        record, context = sent_record(complete=complete)
        data = {"schema_version": 1, "revision": 1, "dialogs": [record]}
        write_json(self.directory / "dialog.json", data)
        if projections:
            write_json(self.directory / "event.json", story._projection(context, "events"))
            write_json(self.directory / "memory.json", story._projection(context, "memories"))
        return data, context

    def test_custom_graph_settings_match_build_and_cutover_generation(self):
        self.seed()
        settings = {"enabled": True, "assistant_name": "TestAssistant", "assistant_aliases": ["helper"],
                    "user_name": "TestUser", "user_aliases": ["reader"], "sqlite_cache_kib": 4096}
        built = migration.build(self.directory, settings)
        preview = migration.dry_run(self.directory, settings)
        self.assertEqual(built["generation"], preview["generation"])
        active = migration.cutover(self.directory, settings)
        self.assertEqual(active["mode"], "active")
        self.assertEqual(active["generation"], built["generation"])

    def files(self):
        return {path.relative_to(self.directory).as_posix(): (path.read_bytes(), path.stat().st_mtime_ns)
                for path in self.directory.rglob("*") if path.is_file()}

    def assert_code(self, code, call, *args, **kwargs):
        with self.assertRaises(migration.MigrationError) as caught:
            call(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)

    def run_main(self, args, **kwargs):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = migration.main(args, **kwargs)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_default_dry_run_empty_nonexistent_directory_has_no_side_effects(self):
        missing = self.directory / "not-created"
        with patch.object(graph_store, "GraphStore", side_effect=AssertionError("Writable cache forbidden")), \
                patch.object(migration, "SingleInstanceLock", side_effect=AssertionError("No read locks")), \
                patch("task_store.TaskStore", side_effect=AssertionError("No task database")):
            code, output, errors = self.run_main(["--directory", str(missing)])
            self.assertEqual(code, 0, errors)
            self.assertEqual(json.loads(output)["command"], "dry-run")
            self.assertEqual(migration.query(missing)["results"]["events"], [])
        self.assertFalse(missing.exists())
        self.assertEqual(self.files(), {})

    def test_dry_run_query_read_only_even_with_corrupt_unused_sqlite(self):
        _, context = self.seed()
        (self.directory / graph_store.DB_FILENAME).write_bytes(b"not a database")
        before = self.files()
        with patch.object(graph_store, "GraphStore", side_effect=AssertionError("Writable cache forbidden")):
            report = migration.dry_run(self.directory)
            result = migration.query(self.directory, kind="events", identifier=context["events"][0]["id"])
            self.assertEqual(report["coverage"]["status"], "complete")
            self.assertEqual(result["results"]["events"][0]["text"], "Library visit")
        self.assertEqual(self.files(), before)

    def test_settings_only_graph_section_no_mail_validation(self):
        settings = self.directory / "settings.json"
        write_json(settings, {"email": None, "password": [], "graph": {"query_limit": 2, "batch_size": 1}})
        config = migration.load_graph_settings(settings)
        self.assertEqual(config["query_limit"], 2)
        self.assertFalse(config["enabled"])
        for bad in ({"enabled": 1}, {"query_limit": True}, {"bad": 3}, []):
            write_json(settings, {"graph": bad})
            self.assert_code("invalid_settings", migration.load_graph_settings, settings)
        write_json(settings, {"graph": {"batch_size": 1}})
        code, output, errors = self.run_main(["dry-run", "--directory", str(self.directory / "absent"),
                                              "--settings", str(settings), "--query-limit", "4"])
        self.assertEqual(code, 0, errors)

    def test_strict_dialog_validation_and_replay_bindings(self):
        data, _ = self.seed()
        data["dialogs"][0]["assistant_summary"] = ""
        write_json(self.directory / "dialog.json", data)
        self.assert_code("invalid_source", migration.dry_run, self.directory)
        data, _ = self.seed()
        del data["dialogs"][0]["graph_updates"]
        data["dialogs"][0]["story_updates"]["event_updates"][0]["source_refs"][-1]["message_id"] = "<wrong@test.invalid>"
        write_json(self.directory / "dialog.json", data)
        self.assert_code("invalid_source", migration.dry_run, self.directory)
        (self.directory / "dialog.json").write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
        self.assert_code("invalid_input", migration.dry_run, self.directory)

    def test_exact_prefix_check_is_type_sensitive_and_read_only(self):
        self.seed()
        write_json(self.directory / "event.json", story._projection(story.empty_context(), "events"))
        before = self.files()
        self.assertEqual(migration.dry_run(self.directory)["projections"]["events"], "prefix")
        self.assertEqual(before, self.files())
        value = json.loads((self.directory / "event.json").read_text(encoding="utf-8"))
        value["revision"] = False
        write_json(self.directory / "event.json", value)
        before = self.files()
        self.assert_code("invalid_projection", migration.build, self.directory)
        self.assertEqual((self.directory / "event.json").read_bytes(), before["event.json"][0])
        self.assertFalse((self.directory / graph_store.DB_FILENAME).exists())

    def test_build_reconciles_only_after_graph_sync_and_never_changes_dialog(self):
        self.seed(projections=False)
        original = (self.directory / "dialog.json").read_bytes()
        events = []
        original_sync = graph_store.GraphStore.sync
        original_reconcile = story.reconcile_locked

        def sync(store, *args, **kwargs):
            self.assertTrue((self.directory / "service.lock").exists())
            self.assertTrue((self.directory / "dialog.json.lock").exists())
            events.append("sync")
            return original_sync(store, *args, **kwargs)

        def reconcile(*args, **kwargs):
            events.append("reconcile")
            return original_reconcile(*args, **kwargs)

        with patch.object(graph_store.GraphStore, "sync", sync), patch.object(story, "reconcile_locked", reconcile), \
                patch("task_store.TaskStore", side_effect=AssertionError("No task DB")):
            report = migration.build(self.directory)
        self.assertEqual(events, ["sync", "reconcile"])
        self.assertEqual(report["mode"], "shadow")
        self.assertEqual(report["state"], "built")
        self.assertEqual((self.directory / "dialog.json").read_bytes(), original)
        self.assertEqual(json.loads((self.directory / "event.json").read_text(encoding="utf-8"))["schema_version"], 1)
        self.assertEqual(json.loads((self.directory / "event_v2.json").read_text(encoding="utf-8"))["schema_version"], 2)
        stamp = (self.directory / "event_v2.json").stat().st_mtime_ns
        migration.build(self.directory)
        self.assertEqual(stamp, (self.directory / "event_v2.json").stat().st_mtime_ns)

    def test_write_commands_require_service_and_dialog_locks(self):
        self.seed()
        for operation in (migration.build, migration.cutover, migration.rollback):
            with SingleInstanceLock(self.directory / "service.lock"):
                self.assert_code("lock_unavailable", operation, self.directory)
        with migration.DialogMemory(self.directory / "dialog.json")._lock():
            self.assert_code("lock_unavailable", migration.build, self.directory)
        with SingleInstanceLock(self.directory / "service.lock"):
            self.assert_code("lock_unavailable", migration.annotate, self.directory,
                             input_path=self.directory / "input.jsonl")

    def test_pending_delivery_blocks_every_mutation_and_dry_run_reports_it(self):
        data, _ = self.seed()
        row = copy.deepcopy(data["dialogs"][0])
        row.update(id="pending", status="unknown", sent_at=None)
        row.pop("story_updates")
        row.pop("graph_updates")
        data["dialogs"].append(row)
        write_json(self.directory / "dialog.json", data)
        self.assertEqual(migration.dry_run(self.directory)["pending_dialogs"], 1)
        for operation in (migration.build, migration.cutover, migration.rollback):
            self.assert_code("pending_dialogs", operation, self.directory)
        self.assert_code("pending_dialogs", migration.annotate, self.directory,
                         input_path=self.directory / "input.jsonl")
        self.assertFalse((self.directory / graph_store.DB_FILENAME).exists())

    def test_cutover_requires_explicit_current_legacy_and_shadow(self):
        self.seed(projections=False)
        self.assert_code("legacy_not_current", migration.cutover, self.directory)
        self.seed()
        self.assert_code("shadow_missing", migration.cutover, self.directory)
        migration.build(self.directory)
        shadow = json.loads((self.directory / "event_v2.json").read_text(encoding="utf-8"))
        shadow["generation"] = "0" * 64
        write_json(self.directory / "event_v2.json", shadow)
        self.assert_code("shadow_stale", migration.cutover, self.directory)
        write_json(self.directory / "event.json", shadow)
        self.assert_code("invalid_projection", migration.cutover, self.directory)
        self.assertEqual(graph_store.read_manifest(self.directory)["mode"], "shadow")

    def test_partial_cutover_refused_unless_explicit_allow_partial(self):
        self.seed(complete=False)
        migration.build(self.directory)
        self.assert_code("coverage_partial", migration.cutover, self.directory)
        self.assertFalse((self.directory / "event_legacy.json").exists())
        result = migration.cutover(self.directory, allow_partial=True)
        self.assertEqual(result["mode"], "active")
        self.assertEqual(result["coverage"]["status"], "partial")

    def test_cutover_backup_hashes_exact_original_event_and_manifest(self):
        self.seed()
        migration.build(self.directory)
        event_before = (self.directory / "event.json").read_bytes()
        manifest_before = (self.directory / migration.MANIFEST).read_bytes()
        report = migration.cutover(self.directory)
        self.assertEqual(report["state"], "completed")
        self.assertEqual(graph_store.event_filename(self.directory), "event_legacy.json")
        self.assertEqual((self.directory / "event_legacy.json").read_bytes(), event_before)
        manifest = graph_store.read_manifest(self.directory)
        self.assertEqual(set(manifest), {"schema_version", "mode", "state", "backups", "source_digest"})
        for key, original in (("event", event_before), ("manifest", manifest_before)):
            entry = manifest["backups"][key]
            self.assertEqual((self.directory / entry["path"]).read_bytes(), original)
            self.assertEqual(entry["sha256"], migration.hashlib.sha256(original).hexdigest())
        self.assertEqual(json.loads((self.directory / "event.json").read_text(encoding="utf-8"))["schema_version"], 2)
        migration.cutover(self.directory)
        self.assertEqual(graph_store.read_manifest(self.directory)["backups"], manifest["backups"])

    def test_crash_after_active_manifest_is_recoverable_without_reading_compact_as_legacy(self):
        self.seed()
        migration.build(self.directory)
        with patch.object(migration, "_sync", side_effect=migration.MigrationError("write_failed")):
            self.assert_code("write_failed", migration.cutover, self.directory)
        self.assertEqual(graph_store.read_manifest(self.directory)["state"], "pending")
        self.assertEqual(graph_store.event_filename(self.directory), "event_legacy.json")
        self.assert_code("recovery_required", migration.build, self.directory)
        # Simulate partial export; story reads must still select valid legacy copy.
        write_json(self.directory / "event.json", {"schema_version": 2, "format": "dfy.compact_event_graph"})
        self.assertEqual(migration.read_snapshot(self.directory)["projections"]["events"], "current")
        self.assertEqual(migration.cutover(self.directory)["state"], "completed")

    def test_crash_before_manifest_keeps_original_legacy_source(self):
        self.seed()
        migration.build(self.directory)
        original_event = (self.directory / "event.json").read_bytes()
        real_atomic = migration._atomic_json

        def crash(path, value):
            if path.name == migration.MANIFEST and value["mode"] == "active":
                raise migration.MigrationError("write_failed")
            return real_atomic(path, value)

        with patch.object(migration, "_atomic_json", crash):
            self.assert_code("write_failed", migration.cutover, self.directory)
        self.assertEqual(graph_store.read_manifest(self.directory)["mode"], "shadow")
        self.assertEqual((self.directory / "event.json").read_bytes(), original_event)
        self.assertEqual(migration.cutover(self.directory)["state"], "completed")

    def test_rollback_reconstructs_latest_sent_history_not_backup(self):
        data, first = self.seed()
        migration.build(self.directory)
        migration.cutover(self.directory)
        record, latest = sent_record("dialog-2", context=first, at=LATER)
        data["dialogs"].append(record)
        data["revision"] += 1
        write_json(self.directory / "dialog.json", data)
        original_dialog = (self.directory / "dialog.json").read_bytes()
        result = migration.rollback(self.directory)
        self.assertEqual(result["mode"], "shadow")
        self.assertEqual(result["state"], "rolled_back")
        self.assertEqual(json.loads((self.directory / "event.json").read_text(encoding="utf-8")), story._projection(latest, "events"))
        self.assertEqual((self.directory / "dialog.json").read_bytes(), original_dialog)
        self.assertEqual(graph_store.event_filename(self.directory), "event.json")
        self.assertEqual(migration.rollback(self.directory)["revision"], 2)

    def test_rollback_crash_recovery_uses_safe_active_legacy(self):
        self.seed()
        migration.build(self.directory)
        migration.cutover(self.directory)
        original_atomic = migration._atomic_json

        def crash(path, value):
            if path.name == migration.MANIFEST and value["mode"] == "shadow":
                raise migration.MigrationError("write_failed")
            return original_atomic(path, value)

        with patch.object(migration, "_atomic_json", crash):
            self.assert_code("write_failed", migration.rollback, self.directory)
        self.assertEqual(graph_store.read_manifest(self.directory)["state"], "rollback_pending")
        self.assertEqual(graph_store.event_filename(self.directory), "event_legacy.json")
        self.assert_code("recovery_required", migration.cutover, self.directory)
        self.assertEqual(migration.rollback(self.directory)["mode"], "shadow")

    def test_manifest_rejects_nonwhitelisted_fields_and_invalid_state(self):
        self.seed()
        for extra in ({"surprise": "private"}, {"state": "not-supported"}):
            write_json(self.directory / migration.MANIFEST, {"schema_version": 1, "mode": "shadow", **extra})
            self.assert_code("invalid_manifest", migration.dry_run, self.directory)

    def test_help_runs_without_application_or_sdk_imports(self):
        script = SRC / "graph_migrate.py"
        environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        completed = subprocess.run([sys.executable, "-B", str(script), "--help"],
                                   cwd=str(self.directory), capture_output=True, text=True,
                                   env=environment, timeout=20)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        for command in ("dry-run", "build", "annotate", "cutover", "rollback", "query"):
            self.assertIn(command, completed.stdout)
        self.assertEqual(self.files(), {})

    def test_error_output_contains_only_fixed_code_type_never_private_content(self):
        secret = "PRIVATE_BODY_12345"
        (self.directory / "dialog.json").write_text(secret, encoding="utf-8")
        code, output, errors = self.run_main(["dry-run", "--directory", str(self.directory)])
        self.assertEqual(code, 2)
        self.assertEqual(output, "")
        self.assertEqual(set(json.loads(errors)), {"ok", "code", "type"})
        self.assertNotIn(secret, errors)
        self.assertNotIn(str(self.directory), errors)
        code, _, errors = self.run_main(["--query-limit", secret])
        self.assertEqual(code, 2)
        self.assertNotIn(secret, errors)

    def test_llm_requires_confirmation_and_key_before_import_or_factory(self):
        self.seed(complete=False)
        bot = FakeBot()
        factory = unittest.mock.Mock(return_value=bot)
        self.assert_code("confirmation_required", migration.annotate, self.directory, llm=True,
                         api_key="synthetic", bot_factory=factory)
        with patch.dict(os.environ, {}, clear=True):
            self.assert_code("missing_api_key", migration.annotate, self.directory, llm=True,
                             confirm_api=True, bot_factory=factory)
        factory.assert_not_called()
        self.assertFalse((self.directory / "service.lock").exists())
        self.assertFalse((self.directory / migration.ANNOTATIONS).exists())

    def test_llm_fakebot_batch_limit_and_historical_provenance(self):
        data, context = self.seed(complete=False)
        for n in range(2, 4):
            record, context = sent_record("dialog-" + str(n), context=context, complete=False, at=LATER)
            data["dialogs"].append(record)
        data["revision"] = len(data["dialogs"])
        write_json(self.directory / "dialog.json", data)
        bot = FakeBot()
        original = (self.directory / "dialog.json").read_bytes()
        with patch.object(migration, "create_bot", side_effect=AssertionError("No actual API factory")):
            result = migration.annotate(self.directory, {"batch_size": 2}, llm=True,
                                        confirm_api=True, api_key="synthetic-key",
                                        bot_factory=lambda **kwargs: bot)
        self.assertEqual(len(bot.calls), 2)
        self.assertEqual(len(bot.calls[0][0]["sources"]), 1)
        self.assertEqual(result["imported"], 2)
        self.assertEqual(len(result["coverage"]["pending_source_ids"]), 1)
        self.assertEqual((self.directory / "dialog.json").read_bytes(), original)
        entries = migration._read_annotations(self.directory)
        for entry in entries:
            package = entry["package"]
            self.assertEqual(entry["hash"], core.digest(package))
            self.assertEqual(package["format"], "dfy.graph_annotation")
            self.assertNotIn("dialog_id", package)
            self.assertTrue(all(ref["kind"] != "mail" for ref in package["source_refs"]))
            self.assertEqual(package["facts"][0]["evidence"]["source_version"], 1)
        migration.annotate(self.directory, {"batch_size": 2}, llm=True, confirm_api=True,
                           api_key="synthetic-key", bot_factory=lambda **kwargs: bot)
        self.assertEqual(len(bot.calls), 3)
        self.assertEqual(len(bot.calls[-1][0]["sources"]), 1)

    def test_offline_import_atomic_validated_and_idempotent(self):
        self.seed(complete=False)
        bot = FakeBot()
        migration.annotate(self.directory, llm=True, confirm_api=True,
                           api_key="synthetic", bot_factory=lambda **kwargs: bot)
        entries = migration._read_annotations(self.directory)
        input_path = self.directory / "frozen-input.jsonl"
        input_path.write_text("".join(json.dumps(entry) + "\n" for entry in entries), encoding="utf-8")
        (self.directory / migration.ANNOTATIONS).unlink()
        with patch.object(migration, "create_bot", side_effect=AssertionError("No network")):
            self.assertEqual(migration.annotate(self.directory, input_path=input_path)["imported"], 1)
            self.assertEqual(migration.annotate(self.directory, input_path=input_path)["imported"], 0)
        before = (self.directory / migration.ANNOTATIONS).read_bytes()
        bad = copy.deepcopy(entries[0])
        bad["hash"] = "0" * 64
        input_path.write_text(json.dumps(entries[0]) + "\n" + json.dumps(bad) + "\n", encoding="utf-8")
        self.assert_code("invalid_annotation", migration.annotate, self.directory, input_path=input_path)
        self.assertEqual((self.directory / migration.ANNOTATIONS).read_bytes(), before)

    def test_annotation_rejects_stale_canonical_hash_even_if_resealed(self):
        self.seed(complete=False)
        bot = FakeBot()
        migration.annotate(self.directory, llm=True, confirm_api=True,
                           api_key="synthetic", bot_factory=lambda **kwargs: bot)
        entry = migration._read_annotations(self.directory)[0]
        (self.directory / migration.ANNOTATIONS).unlink()
        entry["package"]["source_refs"][0]["digest"] = "0" * 64
        package = entry["package"]
        package["package_id"] = core.stable_id("annotation", {key: value for key, value in package.items() if key != "package_id"})
        entry["hash"] = core.digest(package)
        input_path = self.directory / "stale.jsonl"
        input_path.write_text(json.dumps(entry) + "\n", encoding="utf-8")
        self.assert_code("invalid_annotation", migration.annotate, self.directory, input_path=input_path)
        self.assertFalse((self.directory / migration.ANNOTATIONS).exists())

    def test_fakebot_bad_output_is_sanitized_and_does_not_publish(self):
        self.seed(complete=False)
        bot = FakeBot()
        bot.ask = lambda *args, **kwargs: '{"private text":"PRIVATE_BODY"}'
        self.assert_code("invalid_annotation", migration.annotate, self.directory, llm=True,
                         confirm_api=True, api_key="synthetic", bot_factory=lambda **kwargs: bot)
        self.assertFalse((self.directory / migration.ANNOTATIONS).exists())
        self.assertFalse((self.directory / graph_store.DB_FILENAME).exists())

    def test_historical_annotation_allows_complete_cutover_without_fake_mail_refs(self):
        self.seed(complete=False)
        migration.build(self.directory)
        self.assert_code("coverage_partial", migration.cutover, self.directory)
        bot = FakeBot()
        result = migration.annotate(self.directory, llm=True, confirm_api=True,
                                    api_key="synthetic", bot_factory=lambda **kwargs: bot)
        self.assertEqual(result["coverage"]["status"], "complete")
        result = migration.cutover(self.directory)
        self.assertEqual(result["state"], "completed")
        export = json.loads((self.directory / "event.json").read_text(encoding="utf-8"))
        self.assertEqual(export["events"][0]["text"], "Library visit")
        self.assertNotIn("mail", {ref["kind"] for entry in migration._read_annotations(self.directory)
                                  for ref in entry["package"]["source_refs"]})

    def test_llm_multiple_batches_never_exceed_explicit_api_request_cap(self):
        data, context = self.seed(complete=False)
        for n in range(2, 6):
            record, context = sent_record("dialog-" + str(n), context=context, complete=False, at=LATER)
            data["dialogs"].append(record)
        data["revision"] = len(data["dialogs"])
        write_json(self.directory / "dialog.json", data)
        bot = FakeBot()
        result = migration.annotate(self.directory, {"batch_size": 2}, llm=True,
                                    confirm_api=True, api_key="synthetic", max_batches=2,
                                    bot_factory=lambda **kwargs: bot)
        self.assertEqual(len(bot.calls), 4)
        self.assertEqual(result["imported"], 4)
        self.assertEqual(len(result["coverage"]["pending_source_ids"]), 1)

    def test_sequential_history_entities_receive_progressive_canonical_versions(self):
        data, context = self.seed(complete=False)
        record, context = sent_record("dialog-2", context=context, complete=False, at=LATER)
        data["dialogs"].append(record)
        data["revision"] = 2
        write_json(self.directory / "dialog.json", data)

        class EntityBot(FakeBot):
            def ask(self, question, **kwargs):
                response = json.loads(super().ask(question, **kwargs))
                request = self.calls[-1][0]
                prior = request["graph_context"]["entities"]
                source = request["sources"][0]["item"]
                response["annotations"][0]["candidate"]["entities"] = [{
                    "ref": "P1", "type": "person", "name": "Alex", "aliases": [],
                    "target_id": prior[0]["id"] if prior else None,
                    "expected_version": prior[0]["version"] if prior else None,
                    "source": source["source"], "domain": source["domain"], "evidence": "Alex",
                }]
                return json.dumps(response)

        bot = EntityBot()
        result = migration.annotate(self.directory, {"batch_size": 2}, llm=True,
                                    confirm_api=True, api_key="synthetic", bot_factory=lambda **kwargs: bot)
        self.assertEqual(result["imported"], 2)
        self.assertEqual(bot.calls[0][0]["graph_context"]["entities"], [])
        self.assertEqual(bot.calls[1][0]["graph_context"]["entities"][0]["version"], 1)
        packages = migration._read_annotations(self.directory)
        self.assertEqual([row["package"]["entities"][0]["version"] for row in packages], [1, 2])
        graph_context = migration._project(migration.read_snapshot(self.directory), self.directory, canonical=True)
        self.assertEqual(len(graph_context["entities"]), 1)
        self.assertEqual(graph_context["entities"][0]["version"], 2)

    def test_api_exception_and_invalid_existing_jsonl_do_not_publish(self):
        self.seed(complete=False)
        bot = FakeBot()
        bot.ask = unittest.mock.Mock(side_effect=RuntimeError("PRIVATE_BODY api error"))
        self.assert_code("api_failed", migration.annotate, self.directory, llm=True,
                         confirm_api=True, api_key="synthetic", bot_factory=lambda **kwargs: bot)
        self.assertFalse((self.directory / migration.ANNOTATIONS).exists())
        self.assertFalse((self.directory / graph_store.DB_FILENAME).exists())
        (self.directory / migration.ANNOTATIONS).write_text("{\"package\":{},\"hash\":\"bad\"}\n", encoding="utf-8")
        before = self.files()
        self.assert_code("invalid_annotation", migration.dry_run, self.directory)
        self.assert_code("invalid_annotation", migration.query, self.directory)
        self.assertEqual(self.files(), before)

    def test_cutover_without_prior_manifest_backs_up_absence_explicitly(self):
        self.seed()
        migration.build(self.directory)
        (self.directory / migration.MANIFEST).unlink()
        migration.cutover(self.directory)
        entry = graph_store.read_manifest(self.directory)["backups"]["manifest"]
        self.assertEqual(entry, {"path": None, "sha256": None, "absent": True})

    def test_query_bounds_and_safe_filters(self):
        _, context = self.seed()
        entity = migration.query(self.directory, kind="entities", name="  ALEX ")["results"]["entities"]
        self.assertEqual(len(entity), 1)
        for invalid in (True, 0, -1, 201, "2"):
            self.assert_code("invalid_input", migration.query, self.directory, limit=invalid)
        self.assertEqual(migration.query(self.directory, kind="events", identifier="missing")["results"]["events"], [])


if __name__ == "__main__":
    unittest.main()

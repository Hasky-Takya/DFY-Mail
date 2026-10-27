"""Offline queue tests: python -B -m unittest discover -s tests -p test_task_store.py -v."""

from contextlib import redirect_stdout, redirect_stderr
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest


SOURCE = Path(__file__).resolve().parents[1] / "src" / "task_store.py"
spec = importlib.util.spec_from_file_location("tested_task_store", SOURCE)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
TaskStore = module.TaskStore


def message(uid=1, message_id=None, **extra):
    return {"uid": str(uid), "uidvalidity": "10", "message_id": message_id or f"<m{uid}@test>",
            "body": "private inbound body", "subject": "private subject", **extra}


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "private" / "tasks.sqlite3"
        self.store = TaskStore(self.path)
        self.addCleanup(lambda: self.store.close())

    def restart(self):
        self.store.close()
        self.store = TaskStore(self.path)

    def task(self):
        return self.store.ingest("account", (10, 1), [message()])[0]

    def test_explicit_construction_and_pragmas(self):
        self.assertTrue(self.path.is_file())
        self.assertEqual(self.store.connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertEqual(self.store.connection.execute("PRAGMA busy_timeout").fetchone()[0], 5000)
        self.assertEqual(self.store.connection.execute("PRAGMA synchronous").fetchone()[0], 2)
        if os.name != "nt":
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)

    def test_idle_store_does_not_hold_database_handle(self):
        self.task()
        self.assertIsNone(self.store.connection.active)
        # Windows rejects this replacement when sqlite retains an open handle.
        moved = self.path.with_name("moved.sqlite3")
        self.path.rename(moved)
        moved.rename(self.path)
        self.assertEqual(self.store.cursor("account"), (10, 1))
        self.assertIsNone(self.store.connection.active)
        self.store.list_tasks()
        self.assertIsNone(self.store.connection.active)

    def test_import_has_no_filesystem_side_effects(self):
        code = ("import importlib.util; from unittest.mock import patch; "
                f"s=importlib.util.spec_from_file_location('isolated', {str(SOURCE)!r}); "
                "m=importlib.util.module_from_spec(s); "
                "p=patch('sqlite3.connect', side_effect=AssertionError('unexpected IO')); p.start(); "
                "q=patch('pathlib.Path.mkdir', side_effect=AssertionError('unexpected IO')); q.start(); "
                "s.loader.exec_module(m)")
        subprocess.run([sys.executable, "-B", "-c", code], check=True, cwd=self.temporary.name,
                       capture_output=True)

    def test_baseline_restart_and_uidvalidity_change(self):
        self.assertIsNone(self.store.cursor("account"))
        self.store.set_cursor("account", (10, 100))
        self.restart()
        self.assertEqual(self.store.cursor("account"), (10, 100))
        with self.assertRaises(module.UidValidityChanged):
            self.store.set_cursor("account", (11, 0))
        with self.assertRaises(ValueError):
            self.store.set_cursor("account", (10, 99))
        self.assertEqual(self.store.cursor("account"), (10, 100))

    def test_atomic_full_inbound_and_bytes_roundtrip(self):
        original = message(images=[{"bytes": b"\x00\xffPNG", "mime": "image/png"}],
                           nested={module._TAG: "bytes", "base64": "literal not a binary tag"})
        task = self.store.ingest("account", (10, 1), [original])[0]
        self.restart()
        self.assertEqual(self.store.get(task["id"])["payload"], {"message": original, "attempts": 0})
        raw = self.store.connection.execute("SELECT payload FROM messages").fetchone()[0]
        self.assertEqual(module._load(raw), original)
        self.assertEqual(self.store.cursor("account"), (10, 1))

    def test_restart_dedup_uid_and_secondary_message_id(self):
        task = self.task()
        self.restart()
        self.assertEqual(self.store.ingest("account", (10, 1), [message()]), [])
        self.assertEqual(self.store.ingest("account", (10, 2), [message(2, " <m1@test> ")]), [])
        self.assertEqual(len(self.store.list_tasks(account="account")), 1)
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 2)
        self.assertEqual(self.store.get(task["id"])["status"], "pending")
        self.assertEqual(len(self.store.ingest("another", (10, 1), [message()])), 1)

    def test_blank_message_ids_do_not_dedup(self):
        batch = [message(1), message(2)]
        for item in batch:
            item["message_id"] = ""
        self.assertEqual(len(self.store.ingest("account", (10, 2), batch)), 2)

    def test_cursor_and_all_rows_rollback_on_fault(self):
        self.store.set_cursor("account", (10, 0))
        self.store.connection.execute("""CREATE TRIGGER fault BEFORE INSERT ON tasks
            WHEN (SELECT COUNT(*) FROM tasks)=1 BEGIN SELECT RAISE(ABORT,'injected'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.ingest("account", (10, 2), [message(1), message(2)])
        self.assertEqual(self.store.cursor("account"), (10, 0))
        self.assertEqual(self.store.list_tasks(), [])
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0)
        self.store.connection.execute("DROP TRIGGER fault")
        self.assertEqual(len(self.store.ingest("account", (10, 2), [message(1), message(2)])), 2)

    def test_serialization_failure_rolls_back_batch(self):
        self.store.set_cursor("account", (10, 0))
        with self.assertRaises(ValueError):
            self.store.ingest("account", (10, 2), [message(1), message(2, bad=object())])
        self.assertEqual(self.store.cursor("account"), (10, 0))
        self.assertEqual(self.store.list_tasks(), [])
        self.assertFalse(self.store.connection.in_transaction)

    def test_invalid_cursor_and_uid_do_not_commit(self):
        invalid = [(0, 1), (-1, 1), (True, 1), (10, -1), (10, 1.5), (10, "-1"),
                   (10, "１"), (4294967296, 1), (10,), "10:1"]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.store.set_cursor("account", value)
        for value in [None, "0", -1, "not uid", True, 4294967296]:
            with self.subTest(uid=value), self.assertRaises(ValueError):
                self.store.ingest("account", (10, 1), [message(uid=value)])
        self.assertIsNone(self.store.cursor("account"))

    def test_validity_mismatch_and_uid_beyond_cursor_rollback(self):
        for item, exception in [(message(uidvalidity="11"), module.UidValidityChanged),
                                (message(2), ValueError)]:
            with self.assertRaises(exception):
                self.store.ingest("account", (10, 1), [item])
            self.assertIsNone(self.store.cursor("account"))

    def test_save_restarts_cache_and_controls(self):
        task = self.task()
        payload = task["payload"]
        payload.update(body="private generated answer", summaries={"assistant_summary": "summary"},
                       validation_feedback={"previous_output": "private invalid output"}, attempts=2)
        self.store.save(task["id"], payload, status="retry", next_attempt_at=123, error_code="limited")
        self.store.set_control("service:vision:paused", True)
        self.store.set_control("clock:state", {"next": 42, "draft": b"\xff"})
        self.restart()
        restored = self.store.get(task["id"])
        self.assertEqual(restored["payload"], payload)
        self.assertEqual(restored["next_attempt_at"], 123)
        self.assertEqual(self.store.get_control("clock:state"), {"next": 42, "draft": b"\xff"})
        self.assertTrue(self.store.list_controls()["service:vision:paused"])
        self.assertEqual(self.store.get_control("missing", 23), 23)
        self.store.delete_control("service:vision:paused")
        self.assertIsNone(self.store.get_control("service:vision:paused"))

    def test_enqueue_key_idempotent_across_restart_and_scope(self):
        task = self.store.enqueue("account", "proactive", {"body": "draft"}, key="clock:1")
        self.restart()
        again = self.store.enqueue("account", "proactive", {"body": "different"}, key="clock:1")
        self.assertEqual(again["id"], task["id"])
        self.assertEqual(again["payload"], {"body": "draft"})
        other = self.store.enqueue("other", "proactive", {}, key="clock:1")
        self.assertNotEqual(other["id"], task["id"])
        self.assertEqual(len(self.store.list_tasks(kind="proactive", account="account")), 1)
        self.assertEqual(self.store.list_tasks(account="account"), [])

    def test_manual_quarantine_retry_resets_poisoned_cache(self):
        task = self.task()
        payload = {**task["payload"], "body": "bad", "summaries": "bad", "validation_feedback": {"bad": True},
                   "attempts": "malformed", "snapshot": {}, "cache_invalid": True}
        self.store.quarantine(task["id"], payload, error_code="malformed")
        self.assertEqual(self.store.list_tasks(), [])
        with self.assertRaises(module.TaskStoreError):
            self.store.save(task["id"], payload)
        self.restart()
        restored = self.store.retry(task["id"])
        self.assertEqual(restored["payload"], {"message": message(), "attempts": 0,
                                               "invalid_failures": 0, "network_failures": 0, "next_attempt_at": 0})
        self.assertEqual(restored["status"], "pending")
        self.assertIsNone(restored["error_code"])
        with self.assertRaises(module.TaskStoreError):
            self.store.retry(task["id"])

    def test_sending_requires_bound_dialog_and_cannot_retry(self):
        task = self.task()
        with self.assertRaises(module.TaskStoreError):
            self.store.save(task["id"], task["payload"], status="sending")
        self.store.save(task["id"], task["payload"], status="sending", dialog_id="dialog1")
        for status in ["pending", "retry", "quarantined", "done"]:
            with self.subTest(status=status), self.assertRaises(module.TaskStoreError):
                self.store.save(task["id"], task["payload"], status=status)
        with self.assertRaises(module.TaskStoreError):
            self.store.retry(task["id"])
        with self.assertRaises(module.TaskStoreError):
            self.store.save(task["id"], task["payload"], status="sending", dialog_id="new-dialog")

    def test_reconcile_sent_finishes_and_discards_short_lived_payload(self):
        task = self.task()
        self.store.save(task["id"], task["payload"], status="sending", dialog_id="dialog1")
        self.restart()
        self.assertEqual(self.store.reconcile({"dialogs": [{"id": "dialog1", "status": "sent"}]}),
                         {"done": 1, "pending": 0, "blocked_unknown": 0})
        self.assertEqual(self.store.get(task["id"])["payload"], {})
        self.assertEqual(self.store.get(task["id"])["status"], "done")
        self.assertEqual(self.store.connection.execute("SELECT payload FROM messages").fetchone()[0], "{}")
        self.assertEqual(self.store.ingest("account", (10, 1), [message()]), [])
        with self.assertRaises(module.TaskStoreError):
            self.store.save(task["id"], {})

    def test_finish_scrubs_duplicate_message_payloads_including_later_duplicates(self):
        task = self.task()
        self.store.ingest("account", (10, 2), [message(2, "<m1@test>", images=[b"private image"])])
        self.store.finish(task["id"])
        self.store.ingest("account", (10, 3), [message(3, "<m1@test>", images=[b"later private image"])])
        rows = self.store.connection.execute("SELECT payload FROM messages").fetchall()
        self.assertEqual([row[0] for row in rows], ["{}", "{}", "{}"])

    def test_reconcile_uncertain_dialogs_never_released(self):
        for index, status in enumerate(["prepared", "sending", "unknown"], start=1):
            task = self.store.create("account", {"body": "draft"})
            self.store.save(task["id"], task["payload"], status="sending", dialog_id=f"dialog{index}")
            self.store.reconcile([{"id": f"dialog{index}", "status": status}])
            self.assertEqual(self.store.get(task["id"])["status"], "blocked_unknown")
            with self.assertRaises(module.TaskStoreError):
                self.store.retry(task["id"])
            self.store.reconcile([])
            self.assertEqual(self.store.get(task["id"])["status"], "blocked_unknown")

    def test_reconcile_no_dialog_sending_requeues_but_unknown_blocks(self):
        task = self.task()
        self.store.save(task["id"], task["payload"], status="sending", dialog_id="not-prepared")
        self.restart()
        self.assertEqual(self.store.reconcile([])["pending"], 1)
        self.assertEqual(self.store.get(task["id"])["status"], "pending")
        self.assertIsNone(self.store.get(task["id"])["dialog_id"])
        self.store.save(task["id"], task["payload"], status="unknown", dialog_id="lost")
        self.store.reconcile([])
        self.assertEqual(self.store.get(task["id"])["status"], "blocked_unknown")
        with self.assertRaises(module.TaskStoreError):
            self.store.save(task["id"], task["payload"], status="sending")

    def test_reconcile_bound_pending_is_also_blocked(self):
        task = self.task()
        self.store.save(task["id"], task["payload"], dialog_id="dialog1")
        self.store.reconcile([{"id": "dialog1", "status": "prepared"}])
        self.assertEqual(self.store.get(task["id"])["status"], "blocked_unknown")

    def test_invalid_reconcile_metadata_does_not_requeue(self):
        task = self.task()
        self.store.save(task["id"], task["payload"], status="sending", dialog_id="dialog1")
        with self.assertRaises(ValueError):
            self.store.reconcile([{"id": "dialog1", "status": "invalid"}])
        self.assertEqual(self.store.get(task["id"])["status"], "sending")

    def test_cli_metadata_no_payloads_or_account(self):
        task = self.task()
        self.store.save(task["id"], {**task["payload"], "body": "generated answer"})
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(module.main(["--db", str(self.path), "list"]), 0)
        metadata = json.loads(output.getvalue())[0]
        self.assertEqual(metadata["id"], task["id"])
        for secret in ["private", "generated", "account", "subject", "payload"]:
            self.assertNotIn(secret, output.getvalue())

    def test_cli_retry_and_paused_controls(self):
        task = self.task()
        self.store.quarantine(task["id"])
        self.store.set_control("service:model:paused", True)
        self.store.set_control("clock:state", {"body": "private"})
        output = io.StringIO()
        with redirect_stdout(output):
            module.main(["--db", str(self.path), "paused"])
            module.main(["--db", str(self.path), "unpause", "service:model:paused"])
            module.main(["--db", str(self.path), "retry", task["id"]])
        self.assertNotIn("private", output.getvalue())
        self.assertIsNone(self.store.get_control("service:model:paused"))
        self.assertEqual(self.store.get(task["id"])["status"], "pending")
        self.assertEqual(self.store.get_control("clock:state"), {"body": "private"})
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            module.main(["--db", str(self.path), "unpause", "clock:state"])

    def test_finish_and_missing_task(self):
        task = self.task()
        self.store.finish(task["id"])
        self.assertEqual(self.store.get(task["id"])["status"], "done")
        self.assertIsNone(self.store.get("missing"))
        with self.assertRaises(KeyError):
            self.store.finish("missing")

    def test_process_lock_nonblocking_and_release(self):
        path = Path(self.temporary.name) / "service.lock"
        first, second = module.SingleInstanceLock(path), module.SingleInstanceLock(path)
        try:
            self.assertIs(first.acquire(), first)
            with self.assertRaises(module.AlreadyRunningError):
                second.acquire()
            first.release()
            with second:
                with self.assertRaises(module.AlreadyRunningError):
                    first.acquire()
            with first:
                pass
        finally:
            first.release()
            second.release()

    def test_lock_released_by_process_exit(self):
        path = Path(self.temporary.name) / "service.lock"
        code = ("import importlib.util; "
                f"s=importlib.util.spec_from_file_location('locks', {str(SOURCE)!r}); "
                "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); "
                f"lock=m.SingleInstanceLock({str(path)!r}); lock.acquire(); print('locked', flush=True); input()")
        process = subprocess.Popen([sys.executable, "-B", "-c", code], cwd=self.temporary.name,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(process.stdout.readline().strip(), "locked")
            with self.assertRaises(module.AlreadyRunningError):
                module.SingleInstanceLock(path).acquire()
            process.communicate("\n", timeout=10)
            self.assertEqual(process.returncode, 0)
            with module.SingleInstanceLock(path):
                pass
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()


if __name__ == "__main__":
    unittest.main()

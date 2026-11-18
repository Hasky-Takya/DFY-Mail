"""Offline integration tests for the durable v2 recording path."""

import copy
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from context_selection import RECENT_LIMIT, select_context
from dialogueManager import dialog_memory
from dialogueManager.dialog_memory import DialogMemory, MemoryError, new_record, recover_recorded, send_recorded
from memoryManager import batch_store
from memoryManager.story_memory import validate_updates
from task_store import TaskStore


ACCOUNT = "default-mailbox"
SUBJECT = "Garden notes"
BODY = "The seedlings are ready. This full draft stays out of memory."
USER_BODY = "I prefer mint. This full incoming mail stays out of memory."
OBSERVED_AT = "2026-06-01T12:01:00+00:00"
ACCEPTED_AT = "2026-06-01T12:02:00+00:00"
MEMORY_FILES = ("dialog.json", "event.json", "memory.json")
PROJECTIONS = ("event.json", "memory.json")


def story_updates(*, reply=True):
    event = {
        "target_id": None,
        "title": "Seedlings ready",
        "content": "The seedlings are ready to plant.",
        "source": "llm",
        "domain": "assistant_statement",
        "importance": 3,
        "people": [],
        "status": "completed",
        "tense": "present",
        "event_time": {"raw": None, "start": None, "end": None},
        "evidence": "The seedlings are ready.",
    }
    memory = {
        "target_id": None,
        "content": "The user prefers mint.",
        "source": "user",
        "domain": "user_preference",
        "importance": 3,
        "people": [],
        "status": "active",
        "category": "gardening",
        "origin": "Garden correspondence",
        "evidence": "I prefer mint.",
        "related_event_ids": [],
    }
    return {"event_updates": [event], "memory_updates": [memory] if reply else []}


class SimulatedCrash(BaseException):
    """Leave the persisted state as it was at the interruption point."""


class MemoryV2RuntimeTest(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / "memory"
        self.memory_path = self.root / "dialog.json"
        self.db_path = self.root / "tasks.sqlite3"
        self.memory = DialogMemory(self.memory_path)
        self.store = TaskStore(self.db_path)
        self.addCleanup(self.store.close)
        self.mailbox = Mock(spec=["send_email", "read_user_emails_since", "get_mailbox_cursor", "_auth_code"])
        self.mailbox.send_email.side_effect = lambda subject, body, *, message_id, **kwargs: message_id

    def read_memory(self, name):
        return json.loads((self.root / name).read_text(encoding="utf-8"))

    def memory_bytes(self):
        return {name: (self.root / name).read_bytes() for name in MEMORY_FILES}

    @staticmethod
    def inbound_mail():
        return {
            "uid": 15,
            "uidvalidity": 9,
            "message_id": "<garden-inbound@example.invalid>",
            "received_at": "2026-06-01T12:00:00+00:00",
            "received_at_source": "imap_internaldate",
            "subject": SUBJECT,
            "body": USER_BODY,
        }

    def make_reply(self):
        snapshot = recover_recorded(self.memory, self.store, include_story=True)
        inbound = self.inbound_mail()
        (task,) = self.store.ingest(ACCOUNT, (9, 15), [inbound])
        context = snapshot["story_context"]
        updates = validate_updates(story_updates(), story_context=context, user_body=USER_BODY, assistant_body=BODY)
        record = new_record(snapshot, inbound, {
            "user_summary": "The user describes an herb preference.",
            "assistant_summary": "The assistant discusses seedlings.",
            "story_candidate": {"context": context, "observed_at": OBSERVED_AT, "updates": updates},
        }, subject=SUBJECT)
        return snapshot, task, record

    def send(self, snapshot, task, record):
        with patch.object(dialog_memory, "now_iso", return_value=ACCEPTED_AT):
            return send_recorded(
                self.memory, snapshot, record, self.mailbox, SUBJECT, BODY,
                store=self.store, task_id=task["id"], task_payload=dict(task["payload"], body=BODY),
                in_reply_to=record["user_message_id"], auto_replied=True,
            )

    def restart(self, *, include_story=True):
        self.store.close()
        with TaskStore(self.db_path) as store, patch.object(
            dialog_memory, "materialize_updates", side_effect=AssertionError("recovery must replay the saved plan"),
        ):
            snapshot = recover_recorded(DialogMemory(self.memory_path), store, include_story=include_story)
        return snapshot

    def assert_done(self, task):
        with TaskStore(self.db_path) as store:
            row = store.get(task["id"])
            self.assertEqual(row["status"], "done")
            self.assertEqual(row["payload"], {})
            self.assertEqual(store.send_rows(), [])
            self.assertEqual(store.cursor(ACCOUNT), (9, 15))

    def assert_committed(self, record, package):
        dialog = self.read_memory("dialog.json")
        self.assertEqual(dialog["schema_version"], 2)
        self.assertEqual(len(dialog["dialogs"]), 1)
        self.assertEqual(dialog["dialogs"][0], dict(record, status="sent", sent_at=ACCEPTED_AT))
        header = {key: package[key] for key in ("base_revision", "dialog_id", "observed_at")}
        self.assertEqual(self.read_memory("event.json"), {"schema_version": 2, "revision": 1, "batches": [
            dict(header, event_updates=package["event_updates"]),
        ]})
        memory_item = dict(package["memory_updates"][0], source="dialogue", evidence_source="user")
        self.assertEqual(self.read_memory("memory.json"), {"schema_version": 2, "revision": 1, "batches": [
            dict(header, memory_updates=[memory_item]),
        ]})
        self.assertEqual(package["event_updates"][0]["source_refs"], [{
            "dialog_id": record["id"], "message_id": record["assistant_message_id"], "source": "llm",
            "quote": "The seedlings are ready.", "time": OBSERVED_AT,
        }])
        self.assertEqual(memory_item["source_refs"], [{
            "dialog_id": record["id"], "message_id": record["user_message_id"], "source": "user",
            "quote": "I prefer mint.", "time": OBSERVED_AT,
        }])
        for contents in self.memory_bytes().values():
            self.assertNotIn(BODY.encode(), contents)
            self.assertNotIn(USER_BODY.encode(), contents)

    @staticmethod
    def without_package(record):
        return {key: value for key, value in record.items() if key != "story_updates"}

    def test_empty_recovery_initializes_v2_and_snapshot_is_read_only(self):
        snapshot = recover_recorded(self.memory, self.store, include_story=True)
        self.assertEqual(self.read_memory("dialog.json"), {"schema_version": 2, "revision": 0, "dialogs": []})
        for name in PROJECTIONS:
            self.assertEqual(self.read_memory(name), {"schema_version": 2, "revision": 0, "batches": []})
        self.assertEqual(snapshot["story_context"]["revision"], 0)
        with patch.object(batch_store, "atomic_write", side_effect=AssertionError("snapshot must not repair")):
            self.assertEqual(self.memory.snapshot(include_story=True), snapshot)

    def test_nonempty_legacy_memory_is_not_reinitialized(self):
        original = b'{"schema_version":1,"revision":0,"dialogs":[]}\n'
        self.memory_path.write_bytes(original)
        with self.assertRaises(MemoryError):
            recover_recorded(self.memory, self.store)
        self.assertEqual(self.memory_path.read_bytes(), original)
        for name in PROJECTIONS:
            self.assertFalse((self.root / name).exists())

    def test_restart_never_rebuilds_a_missing_projection_without_a_plan(self):
        recover_recorded(self.memory, self.store)
        (self.root / "event.json").unlink()
        remaining = {name: (self.root / name).read_bytes() for name in ("dialog.json", "memory.json")}
        for read in (self.memory.snapshot, self.restart):
            with self.assertRaises(MemoryError):
                read(include_story=False)
        self.assertFalse((self.root / "event.json").exists())
        self.assertEqual({name: (self.root / name).read_bytes() for name in remaining}, remaining)
        self.mailbox.send_email.assert_not_called()

    def test_send_arms_before_smtp_and_saves_exact_split_batch(self):
        snapshot, task, record = self.make_reply()
        prepared = copy.deepcopy(record)
        dialog_record = self.without_package(prepared)

        def smtp(subject, body, *, message_id, **kwargs):
            with TaskStore(self.db_path) as reader:
                (row,) = reader.send_rows()
            self.assertEqual(row["id"], task["id"])
            self.assertEqual(row["status"], "sending")
            plan = row["payload"]["send_plan"]
            self.assertEqual(plan["phase"], "armed")
            self.assertEqual(plan["record"], dialog_record)
            self.assertEqual(plan["package"], prepared["story_updates"])
            self.assertIsNone(plan["sent_at"])
            self.assertEqual(self.read_memory("dialog.json")["dialogs"][0]["status"], "sending")
            self.assertEqual([self.read_memory(name)["revision"] for name in PROJECTIONS], [0, 0])
            return message_id

        self.mailbox.send_email.side_effect = smtp
        self.assertEqual(self.send(snapshot, task, record), prepared["assistant_message_id"])
        self.mailbox.send_email.assert_called_once_with(
            SUBJECT, BODY, message_id=prepared["assistant_message_id"],
            in_reply_to=prepared["user_message_id"], auto_replied=True,
        )
        self.assert_committed(dialog_record, prepared["story_updates"])
        self.assert_done(task)
        story = self.memory.snapshot(include_story=True)["story_context"]
        self.assertEqual(story["revision"], 1)
        self.assertEqual(story["applied_dialog_ids"], [prepared["id"]])
        self.assertEqual(story["events"], prepared["story_updates"]["event_updates"])
        self.assertEqual(story["memories"], prepared["story_updates"]["memory_updates"])

    def test_update_keeps_id_and_history_but_loads_only_latest_version(self):
        snapshot, task, record = self.make_reply()
        self.send(snapshot, task, record)
        snapshot = self.memory.snapshot(include_story=True)
        context = snapshot["story_context"]
        previous = context["events"][0]
        updates = story_updates(reply=False)
        updates["event_updates"][0].update(target_id=previous["id"], content="The seedlings can go outside.")
        checked = validate_updates(updates, story_context=context, user_body=None, assistant_body=BODY)
        updated = new_record(snapshot, None, {
            "assistant_summary": "The seedlings can go outside.", "user_summary": None,
            "story_candidate": {"context": context, "observed_at": ACCEPTED_AT, "updates": checked},
        }, subject=SUBJECT)
        second_task = self.store.create(ACCOUNT, {}, kind="manual")
        self.send(snapshot, second_task, updated)
        event_log = self.read_memory("event.json")
        versions = [batch["event_updates"][0] for batch in event_log["batches"]]
        self.assertEqual([item["id"] for item in versions], [previous["id"]] * 2)
        self.assertEqual([item["version"] for item in versions], [1, 2])
        self.assertEqual(versions[1]["source_refs"][:-1], previous["source_refs"])
        self.assertEqual(self.memory.snapshot(include_story=True)["story_context"]["events"], versions[1:])
        self.assertEqual(self.read_memory("memory.json")["batches"][-1]["memory_updates"], [])

    def test_recent_limit_is_applied_independently_to_each_list(self):
        items = [{"id": str(i), "sent_at": ACCEPTED_AT} for i in range(RECENT_LIMIT + 2)]
        payload = {"history": items, "story_context": {"events": items.copy(), "memories": items.copy()}}
        chosen, _ = select_context(payload, limit=60000, render=json.dumps)
        for group in (chosen["history"], chosen["story_context"]["events"], chosen["story_context"]["memories"]):
            self.assertEqual([item["id"] for item in group], [str(i) for i in range(2, RECENT_LIMIT + 2)])
        self.assertEqual(len(items), RECENT_LIMIT + 2)

    def test_restart_releases_staged_task_interrupted_before_arm(self):
        snapshot, task, record = self.make_reply()
        with patch.object(TaskStore, "arm_send", side_effect=SimulatedCrash):
            with self.assertRaises(SimulatedCrash):
                self.send(snapshot, task, record)
        self.assertEqual(self.store.get(task["id"])["payload"]["send_plan"]["phase"], "staged")
        self.assertTrue(self.read_memory("dialog.json")["dialogs"])
        restarted = self.restart()
        self.assertEqual(restarted["dialogs"], [])
        self.assertEqual(restarted["story_context"]["revision"], 0)
        with TaskStore(self.db_path) as store:
            row = store.get(task["id"])
            self.assertEqual(row["status"], "pending")
            self.assertIsNone(row["dialog_id"])
            self.assertEqual(row["payload"]["message"], task["payload"]["message"])
            self.assertNotIn("send_plan", row["payload"])
        self.mailbox.send_email.assert_not_called()

    def assert_saved_acceptance(self, task, record):
        row = self.store.get(task["id"])
        self.assertEqual(row["status"], "sending")
        plan = row["payload"]["send_plan"]
        self.assertEqual(plan["phase"], "accepted")
        self.assertEqual(plan["sent_at"], ACCEPTED_AT)
        self.assertEqual(plan["record"], self.without_package(record))
        self.assertEqual(plan["package"], record["story_updates"])
        return plan

    def test_restart_replays_accepted_plan_after_partial_projection_write(self):
        snapshot, task, record = self.make_reply()
        prepared = copy.deepcopy(record)
        real_write = batch_store.atomic_write
        written = []

        def interrupted_write(path, data, *args, **kwargs):
            name = Path(path).name
            if name in PROJECTIONS:
                if written:
                    raise OSError("simulated interruption writing the second projection")
                written.append(name)
            return real_write(path, data, *args, **kwargs)

        with patch.object(batch_store, "atomic_write", side_effect=interrupted_write):
            with self.assertRaises(MemoryError):
                self.send(snapshot, task, record)
        self.assertEqual(len(written), 1)
        self.assertCountEqual([self.read_memory(name)["revision"] for name in PROJECTIONS], [0, 1])
        plan = self.assert_saved_acceptance(task, prepared)
        # Deliberately alter the original process's objects; only the SQLite plan
        # is authoritative, including its frozen evidence and acceptance time.
        record.clear()
        task["payload"].clear()
        self.restart(include_story=False)
        self.assert_committed(plan["record"], plan["package"])
        self.assert_done(task)
        after = self.memory_bytes()
        self.restart()
        self.assertEqual(self.memory_bytes(), after)
        self.mailbox.send_email.assert_called_once()

    def test_restart_does_not_overwrite_a_conflicting_partial_batch(self):
        snapshot, task, record = self.make_reply()
        real_write = batch_store.atomic_write

        def fail_memory_write(path, data):
            if path.name == "memory.json":
                raise OSError("simulated second-write failure")
            return real_write(path, data)

        with patch.object(batch_store, "atomic_write", side_effect=fail_memory_write):
            with self.assertRaises(MemoryError):
                self.send(snapshot, task, record)
        event = self.read_memory("event.json")
        event["batches"][-1]["event_updates"][0]["title"] = "Different evidence history"
        real_write(self.root / "event.json", event)
        before = self.memory_bytes()
        with self.assertRaisesRegex(MemoryError, "suffix differs"):
            self.restart()
        self.assertEqual(self.memory_bytes(), before)
        self.assert_saved_acceptance(task, record)
        self.mailbox.send_email.assert_called_once()

    def make_receiver(self, mailbox, bot, store):
        from dfy_receive import MailReceiver

        return MailReceiver(
            mailbox, bot, "Test reply prompt", memory_path=self.memory_path,
            summary_prompt="Test summary prompt", extraction_prompt="Test extraction prompt", task_store=store,
        )

    def test_start_does_not_create_an_imap_baseline_when_memory_is_invalid(self):
        self.root.mkdir(exist_ok=True)
        self.memory_path.write_text("{}", encoding="utf-8")
        receiver = self.make_receiver(self.mailbox, Mock(spec=["ask"]), self.store)
        with self.assertLogs("dfy.receive", level="ERROR"):
            with self.assertRaisesRegex(MemoryError, "未建立新基线"):
                receiver.start()
        self.assertIsNone(self.store.cursor(ACCOUNT))
        self.mailbox.get_mailbox_cursor.assert_not_called()

    def test_receiver_recovery_finishes_an_accepted_task_without_mail_or_model(self):
        snapshot, task, record = self.make_reply()
        prepared = copy.deepcopy(record)
        with patch.object(batch_store.BatchStore, "commit", side_effect=batch_store.BatchStoreError("test fault")):
            with self.assertRaises(MemoryError):
                self.send(snapshot, task, record)
        plan = self.assert_saved_acceptance(task, prepared)
        bot = Mock(spec=["ask"])
        mailbox = Mock(spec=["send_email", "read_user_emails_since", "get_mailbox_cursor"])
        self.store.close()
        with TaskStore(self.db_path) as store:
            receiver = self.make_receiver(mailbox, bot, store)
            recovered = receiver.recover_memory()
        self.assertIsNotNone(recovered)
        self.assertIsNone(receiver.blocked_reason)
        self.assert_committed(plan["record"], plan["package"])
        self.assert_done(task)
        self.assertEqual(mailbox.mock_calls, [])
        self.assertEqual(bot.mock_calls, [])
        self.mailbox.send_email.assert_called_once()

    def test_restart_completes_task_when_json_commit_already_succeeded(self):
        snapshot, task, record = self.make_reply()
        prepared = copy.deepcopy(record)
        with patch.object(TaskStore, "finish_send", side_effect=sqlite3.OperationalError("simulated SQLite failure")):
            with self.assertRaises(MemoryError):
                self.send(snapshot, task, record)
        plan = self.assert_saved_acceptance(task, prepared)
        self.assert_committed(plan["record"], plan["package"])
        after = self.memory_bytes()
        self.restart()
        self.assertEqual(self.memory_bytes(), after)
        self.assert_done(task)
        self.mailbox.send_email.assert_called_once()

    def test_restart_blocks_when_smtp_returned_but_acceptance_was_not_saved(self):
        snapshot, task, record = self.make_reply()
        with patch.object(TaskStore, "accept_send", side_effect=sqlite3.OperationalError("simulated SQLite failure")):
            with self.assertRaises(MemoryError):
                self.send(snapshot, task, record)
        self.assertEqual(self.store.get(task["id"])["payload"]["send_plan"]["phase"], "armed")
        for _ in range(2):
            with self.assertRaises(MemoryError):
                self.restart()
            with TaskStore(self.db_path) as store:
                row = store.get(task["id"])
                self.assertEqual(row["status"], "sending")
                self.assertEqual(row["payload"]["send_plan"]["phase"], "armed")
                self.assertEqual(row["dialog_id"], record["id"])
                self.assertEqual(store.list_tasks(account=ACCOUNT), [])
        self.assertEqual([self.read_memory(name)["revision"] for name in PROJECTIONS], [0, 0])
        self.assertIsNone(self.read_memory("dialog.json")["dialogs"][0]["sent_at"])
        self.mailbox.send_email.assert_called_once()

    def test_receiver_poll_saves_reply_and_inbound_evidence(self):
        inbound = self.inbound_mail()
        self.mailbox.read_user_emails_since.return_value = (9, 15), [inbound]
        bot = Mock(spec=["ask"])
        bot.ask.side_effect = [BODY, self.model_extraction(reply=True)]
        receiver = self.make_receiver(self.mailbox, bot, self.store)
        self.assertIsNotNone(receiver.recover_memory())
        receiver.cursor = (9, 14)
        self.store.set_cursor(ACCOUNT, receiver.cursor)
        self.assertTrue(receiver.poll_once())
        self.assertEqual(bot.ask.call_count, 2)
        snapshot = self.memory.snapshot(include_story=True)
        (record,) = snapshot["dialogs"]
        self.mailbox.send_email.assert_called_once_with(
            "Re: " + SUBJECT, BODY, message_id=record["assistant_message_id"], in_reply_to=inbound["message_id"],
            references="", auto_replied=True,
        )
        self.assertEqual(record["user_message_id"], inbound["message_id"])
        (item,) = self.read_memory("memory.json")["batches"][0]["memory_updates"]
        self.assertEqual((item["source"], item["evidence_source"]), ("dialogue", "user"))
        self.assertEqual(item["source_refs"][0]["message_id"], inbound["message_id"])
        (task,) = self.store.list_tasks(statuses="done", account=ACCOUNT)
        self.assertEqual(task["dialog_id"], record["id"])
        self.assert_done(task)

    def test_clock_tick_recovers_a_projection_failure_without_resending(self):
        from dfy_clock import IdleMailScheduler

        self.mailbox.read_user_emails_since.return_value = (9, 0), []
        bot = Mock(spec=["ask"])
        bot.ask.side_effect = [BODY, self.model_extraction(reply=False)]
        receiver = self.make_receiver(self.mailbox, bot, self.store)
        self.assertIsNotNone(receiver.recover_memory())
        receiver.cursor = (9, 0)
        self.store.set_cursor(ACCOUNT, receiver.cursor)
        prompts = {"task": "Write a test email.", "system_prompt": "Test prompt", "summary_prompt": "Test summary"}
        fixed_now = datetime.fromisoformat(ACCEPTED_AT)
        scheduler = IdleMailScheduler(receiver, prompts, {}, randint=lambda low, high: low,
                                      now=lambda: fixed_now, monotonic=lambda: 0)
        scheduler.deadline = 0
        real_commit = batch_store.BatchStore.commit

        def interrupt_after_smtp(batch, state):
            if self.mailbox.send_email.called:
                raise batch_store.BatchStoreError("test fault after SMTP")
            return real_commit(batch, state)

        with patch.object(batch_store.BatchStore, "commit", interrupt_after_smtp):
            with self.assertLogs("dfy.clock", level="CRITICAL"):
                scheduler.tick()
        self.assertTrue(scheduler.stopped)
        (row,) = self.store.send_rows()
        self.assertEqual(row["payload"]["send_plan"]["phase"], "accepted")
        scheduler.tick()
        self.assertFalse(scheduler.stopped)
        self.assertEqual(self.store.get(row["id"])["status"], "done")
        snapshot = self.memory.snapshot(include_story=True)
        self.assertEqual(len(snapshot["dialogs"]), 1)
        self.assertEqual(snapshot["story_context"]["revision"], 1)
        self.assertEqual(bot.ask.call_count, 2)
        self.mailbox.send_email.assert_called_once()

    @staticmethod
    def model_extraction(*, reply):
        result = dict(
            story_updates(reply=reply), user_summary="The user describes an herb preference." if reply else None,
            assistant_summary="The assistant discusses seedlings.",
        )
        if not reply:
            result["mail_subject"] = SUBJECT
        return json.dumps(result)

    def test_manual_resume_returns_the_original_id_without_repeating_model_or_smtp(self):
        from dfy_mail import send_deepseek_email

        prompt_path = self.root.parent / "prompt.json"
        prompt_path.write_text(json.dumps({
            "subject": "Prompt fallback",
            "prompt": "Write an offline test email.",
            "summary_prompt": "Summarize the test email.",
            "story_extraction_prompt": "Extract updates with evidence.",
        }), encoding="utf-8")
        bot = Mock(spec=["ask"])
        bot.ask.side_effect = [BODY, self.model_extraction(reply=False)]
        self.mailbox._auth_code = "test-auth-code"
        arguments = {
            "api_key": "test-api-key", "auth_code": "test-auth-code", "prompt_path": prompt_path,
            "config_path": self.root.parent / "unused-settings.json", "memory_path": self.memory_path,
        }
        with (
            patch("dfy_mail.DeepSeekChat", return_value=bot) as model_factory,
            patch("dfy_mail.MailClient", return_value=self.mailbox),
            patch("dfy_mail.set_log_secrets"),
        ):
            with patch.object(TaskStore, "finish_send", side_effect=sqlite3.OperationalError("test fault")):
                with self.assertRaises(MemoryError):
                    send_deepseek_email(**arguments)
            (interrupted,) = self.store.send_rows()
            identifier = interrupted["payload"]["send_plan"]["record"]["assistant_message_id"]
            self.assertEqual(send_deepseek_email(**arguments, resume_task=interrupted["id"]), identifier)
        model_factory.assert_called_once()
        self.assertEqual(bot.ask.call_count, 2)
        self.mailbox.send_email.assert_called_once_with(SUBJECT, BODY, message_id=identifier)
        snapshot = self.memory.snapshot(include_story=True)
        (record,) = snapshot["dialogs"]
        self.assertEqual(record["assistant_message_id"], identifier)
        self.assertEqual(record["status"], "sent")
        self.assertNotIn("story_updates", record)
        self.assertEqual(snapshot["story_context"]["events"][0]["source_refs"][0]["message_id"], identifier)
        self.assertEqual(self.read_memory("memory.json")["batches"][0]["memory_updates"], [])
        with TaskStore(self.db_path) as store:
            (row,) = store.list_tasks(statuses="done", kind="manual", account=ACCOUNT)
            self.assertEqual(row["payload"], {})
            self.assertEqual(row["dialog_id"], record["id"])


if __name__ == "__main__":
    unittest.main()

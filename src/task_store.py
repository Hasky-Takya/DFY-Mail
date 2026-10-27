"""Durable, standard-library task queue. Importing this module performs no I/O.

Runtime contract: TaskStore(memory_path.parent / "tasks.sqlite3").  Ingest
returns task rows; the mutable work/cache dictionary is row["payload"].  Save a
bound ``sending`` row BEFORE SMTP (and before preparing its dialog), then finish
only after a confirmed send. Reconcile against the complete dialog snapshot on
restart. A dialog in prepared/sending/unknown is never automatically resent.
Neither this module nor its maintenance CLI logs or prints message payloads.
"""

import argparse
import base64
from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import sqlite3
import time
import uuid


class TaskStoreError(RuntimeError):
    """A queue invariant was violated; diagnostics never contain payloads."""


class UidValidityChanged(TaskStoreError):
    """The mailbox identity changed and requires explicit operator handling."""


class AlreadyRunningError(TaskStoreError):
    """Another process owns the service lock."""


_STATUSES = {"pending", "retry", "quarantined", "sending", "unknown", "blocked_unknown", "done"}
_UNSAFE = {"sending", "unknown", "blocked_unknown"}
_TAG = "__task_store_type__"


def _private(path, mode):
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def _encode(value):
    if isinstance(value, bytes):
        return {_TAG: "bytes", "base64": base64.b64encode(value).decode("ascii")}
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise ValueError("JSON object keys must be strings")
        if _TAG in value:
            return {_TAG: "dict", "items": [[key, _encode(item)] for key, item in value.items()]}
        return {key: _encode(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_encode(item) for item in value]
    if value is None or type(value) in (str, int, float, bool):
        return value
    raise ValueError("Unsupported payload value type")


def _decode(value):
    if isinstance(value, list):
        return [_decode(item) for item in value]
    if isinstance(value, dict):
        if value.get(_TAG) == "bytes" and set(value) == {_TAG, "base64"}:
            return base64.b64decode(value["base64"], validate=True)
        if value.get(_TAG) == "dict" and set(value) == {_TAG, "items"}:
            return {key: _decode(item) for key, item in value["items"]}
        return {key: _decode(item) for key, item in value.items()}
    return value


def _dump(value):
    try:
        return json.dumps(_encode(value), ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError, RecursionError):
        raise ValueError("Payload is not supported JSON") from None


def _load(value):
    try:
        return _decode(json.loads(value))
    except (TypeError, ValueError, KeyError, RecursionError):
        raise TaskStoreError("Invalid stored JSON") from None


def _number(value, minimum=0):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError("Invalid mailbox cursor or UID")
    if isinstance(value, str) and (not value.isascii() or not value.isdecimal()):
        raise ValueError("Invalid mailbox cursor or UID")
    result = int(value)
    if not minimum <= result <= 4294967295:
        raise ValueError("Invalid mailbox cursor or UID")
    return result


def _cursor(value):
    if not isinstance(value, (tuple, list)) or len(value) != 2:
        raise ValueError("Cursor must be (UIDVALIDITY, UID)")
    return _number(value[0], 1), _number(value[1])


def _text(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("A nonempty identifier is required")
    return value


def _deadline(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError("Invalid retry deadline")
    return value


class SingleInstanceLock:
    """Nonblocking OS process lock, held by an open file until release/exit.

    Never unlink the lockfile: doing so permits locks on different inodes.
    The runtime must retain this object for the complete service lifetime.
    """

    def __init__(self, path):
        self.path = Path(path)
        self._file = None

    def acquire(self):
        if self._file is not None:
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _private(self.path.parent, 0o700)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        handle = os.fdopen(fd, "r+b", buffering=0)
        _private(self.path, 0o600)
        try:
            if os.name == "nt":
                import msvcrt
                # Locking one byte also works for an empty file on Windows.
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise AlreadyRunningError("Service process lock is already held or unavailable") from None
        self._file = handle
        return self

    def release(self):
        handle, self._file = self._file, None
        if handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.release()


class _BufferedCursor:
    """Materialized rows do not retain a SQLite file handle while idle."""

    def __init__(self, cursor):
        self.lastrowid = cursor.lastrowid
        self.rowcount = cursor.rowcount
        self._rows = cursor.fetchall() if cursor.description is not None else []
        self._position = 0

    def fetchone(self):
        if self._position >= len(self._rows):
            return None
        row = self._rows[self._position]
        self._position += 1
        return row

    def fetchall(self):
        rows = self._rows[self._position:]
        self._position = len(self._rows)
        return rows

    def __iter__(self):
        while True:
            row = self.fetchone()
            if row is None:
                return
            yield row


class _Connection:
    """Compatibility facade; only transactions hold a live SQLite connection."""

    def __init__(self, path):
        self.path = path
        self.active = None

    def open(self):
        uri = self.path.resolve().as_uri() + "?mode=rw"
        connection = sqlite3.connect(uri, uri=True, timeout=5, isolation_level=None)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA secure_delete=ON")
        except BaseException:
            connection.close()
            raise
        return connection

    @property
    def in_transaction(self):
        return self.active is not None and self.active.in_transaction

    def execute(self, sql, parameters=()):
        if self.active is not None:
            return self.active.execute(sql, parameters)
        connection = self.open()
        try:
            return _BufferedCursor(connection.execute(sql, parameters))
        finally:
            connection.close()

    def executescript(self, script):
        connection = self.open()
        try:
            return _BufferedCursor(connection.executescript(script))
        finally:
            connection.close()

    def close(self):
        if self.active is not None:
            self.active.close()
            self.active = None


class TaskStore:
    """SQLite queue with atomic cursor/message/task commits and durable controls."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _private(self.path.parent, 0o700)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        os.close(fd)
        _private(self.path, 0o600)
        self.connection = _Connection(self.path)
        try:
            self.connection.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS cursors (
                    account TEXT PRIMARY KEY, uidvalidity INTEGER NOT NULL, uid INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY, account TEXT NOT NULL,
                    uidvalidity INTEGER NOT NULL, uid INTEGER NOT NULL,
                    message_id TEXT NOT NULL, payload TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    UNIQUE(account, uidvalidity, uid)
                );
                CREATE INDEX IF NOT EXISTS messages_identifier ON messages(account, message_id);
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, account TEXT NOT NULL, kind TEXT NOT NULL,
                    message_row_id INTEGER UNIQUE REFERENCES messages(id),
                    enqueue_key TEXT,
                    payload TEXT NOT NULL, status TEXT NOT NULL,
                    next_attempt_at REAL NOT NULL DEFAULT 0,
                    error_code TEXT, dialog_id TEXT,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS tasks_work ON tasks(account, kind, status);
                CREATE UNIQUE INDEX IF NOT EXISTS tasks_enqueue_key ON tasks(account, kind, enqueue_key);
                CREATE TABLE IF NOT EXISTS controls (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                COMMIT;
            """)
        except BaseException:
            self.connection.close()
            raise

    @contextmanager
    def _transaction(self):
        if self.connection.active is not None:
            raise TaskStoreError("Nested transactions are not supported")
        connection = self.connection.open()
        self.connection.active = connection
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            self.connection.active = None
            connection.close()
            _private(self.path, 0o600)
            for suffix in ("-journal", "-wal", "-shm"):
                _private(str(self.path) + suffix, 0o600)

    def close(self):
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def cursor(self, account):
        row = self.connection.execute(
            "SELECT uidvalidity,uid FROM cursors WHERE account=?", (_text(account),)
        ).fetchone()
        return tuple(row) if row is not None else None

    def _set_cursor(self, account, value):
        previous = self.cursor(account)
        if previous is not None:
            if previous[0] != value[0]:
                raise UidValidityChanged("Mailbox UIDVALIDITY changed; cursor was not reset")
            if value[1] < previous[1]:
                raise ValueError("Mailbox cursor must not move backwards")
        self.connection.execute(
            "INSERT INTO cursors VALUES (?,?,?) ON CONFLICT(account) DO UPDATE SET uid=excluded.uid",
            (account, *value),
        )

    def set_cursor(self, account, cursor):
        account, cursor = _text(account), _cursor(cursor)
        with self._transaction():
            self._set_cursor(account, cursor)

    def _insert_task(self, account, payload, kind, task_id=None, message_row_id=None):
        task_id = _text(task_id) if task_id is not None else uuid.uuid4().hex
        now = time.time()
        self.connection.execute(
            "INSERT INTO tasks (id,account,kind,message_row_id,payload,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,'pending',?,?)",
            (task_id, account, _text(kind), message_row_id, _dump(payload), now, now),
        )
        return task_id

    def create(self, account, payload, kind="proactive", task_id=None):
        if not isinstance(payload, dict):
            raise ValueError("Task payload must be an object")
        with self._transaction():
            identifier = self._insert_task(_text(account), payload, kind, task_id)
        return self.get(identifier)

    def enqueue(self, account, kind, payload, key=None):
        """Create work, or return the existing task for an account/kind/key."""
        account, kind = _text(account), _text(kind)
        if not isinstance(payload, dict):
            raise ValueError("Task payload must be an object")
        if key is not None:
            key = _text(key)
        with self._transaction():
            row = None if key is None else self.connection.execute(
                "SELECT id FROM tasks WHERE account=? AND kind=? AND enqueue_key=?", (account, kind, key),
            ).fetchone()
            if row is not None:
                identifier = row["id"]
            else:
                identifier = self._insert_task(account, payload, kind)
                if key is not None:
                    self.connection.execute("UPDATE tasks SET enqueue_key=? WHERE id=?", (key, identifier))
        return self.get(identifier)

    def ingest(self, account, cursor, messages):
        """Persist the complete inbound batch and cursor together; never use RAM dedup."""
        account, cursor = _text(account), _cursor(cursor)
        added = []
        with self._transaction():
            self._set_cursor(account, cursor)
            for message in messages:
                if not isinstance(message, dict):
                    raise ValueError("Inbound message must be an object")
                uid = _number(message.get("uid"), 1)
                validity = _number(message.get("uidvalidity", cursor[0]), 1)
                if validity != cursor[0]:
                    raise UidValidityChanged("Inbound message UIDVALIDITY differs from cursor")
                if uid > cursor[1]:
                    raise ValueError("Inbound UID exceeds committed cursor")
                identifier = message.get("message_id", message.get("message-id", message.get("messageId", "")))
                if not isinstance(identifier, str):
                    raise ValueError("Invalid Message-ID type")
                identifier = identifier.strip()
                encoded = _dump(message)
                if self.connection.execute(
                    "SELECT 1 FROM messages WHERE account=? AND uidvalidity=? AND uid=?",
                    (account, validity, uid),
                ).fetchone():
                    continue
                duplicate = bool(identifier) and self.connection.execute(
                    "SELECT 1 FROM messages WHERE account=? AND message_id=? LIMIT 1",
                    (account, identifier),
                ).fetchone() is not None
                if duplicate and self.connection.execute(
                    "SELECT 1 FROM tasks t JOIN messages m ON m.id=t.message_row_id "
                    "WHERE m.account=? AND m.message_id=? AND t.status='done' LIMIT 1",
                    (account, identifier),
                ).fetchone() is not None:
                    encoded = "{}"  # A completed duplicate needs identity, not another image/body copy.
                result = self.connection.execute(
                    "INSERT INTO messages(account,uidvalidity,uid,message_id,payload,created_at) VALUES(?,?,?,?,?,?)",
                    (account, validity, uid, identifier, encoded, time.time()),
                )
                if not duplicate:
                    added.append(self._insert_task(
                        account, {"message": message, "attempts": 0}, "reply", message_row_id=result.lastrowid,
                    ))
        return [self.get(identifier) for identifier in added]

    @staticmethod
    def _row(row):
        if row is None:
            return None
        result = dict(row)
        result["payload"] = _load(result["payload"])
        return result

    def get(self, task_id):
        return self._row(self.connection.execute("SELECT * FROM tasks WHERE id=?", (_text(task_id),)).fetchone())

    def list_tasks(self, statuses=("pending", "retry"), kind="reply", account=None):
        if isinstance(statuses, str):
            statuses = (statuses,)
        statuses = tuple(statuses)
        if not statuses:
            return []
        if not all(status in _STATUSES for status in statuses):
            raise ValueError("Invalid task status")
        where = ["status IN (" + ",".join("?" for _ in statuses) + ")"]
        params = list(statuses)
        if kind is not None:
            where.append("kind=?")
            params.append(_text(kind))
        if account is not None:
            where.append("account=?")
            params.append(_text(account))
        rows = self.connection.execute(
            "SELECT * FROM tasks WHERE " + " AND ".join(where) + " ORDER BY created_at,rowid", params,
        ).fetchall()
        return [self._row(row) for row in rows]

    def _required(self, task_id):
        task = self.get(task_id)
        if task is None:
            raise KeyError("Task does not exist")
        return task

    def save(self, task_id, payload, status="pending", next_attempt_at=0, error_code=None, dialog_id=None):
        if status not in _STATUSES or not isinstance(payload, dict):
            raise ValueError("Invalid task status or payload")
        _deadline(next_attempt_at)
        if error_code is not None:
            _text(error_code)
        if dialog_id is not None:
            _text(dialog_id)
        encoded = _dump(payload)
        with self._transaction():
            task = self._required(task_id)
            bound_dialog = dialog_id if dialog_id is not None else task["dialog_id"]
            if task["status"] == "done" and status != "done":
                raise TaskStoreError("Completed task cannot be reopened")
            if task["status"] == "quarantined" and status != "quarantined":
                raise TaskStoreError("Quarantined task requires manual retry")
            if task["status"] in _UNSAFE:
                permitted = {
                    "sending": {"sending", "unknown", "blocked_unknown"},
                    "unknown": {"unknown", "blocked_unknown"},
                    "blocked_unknown": {"blocked_unknown"},
                }
                if status not in permitted[task["status"]] or bound_dialog != task["dialog_id"]:
                    raise TaskStoreError("Uncertain send cannot be directly retried or rebound")
            if status == "sending" and not bound_dialog:
                raise TaskStoreError("Sending task must be bound to a dialog first")
            self.connection.execute(
                "UPDATE tasks SET payload=?,status=?,next_attempt_at=?,error_code=?,dialog_id=?,updated_at=? WHERE id=?",
                (encoded, status, next_attempt_at, error_code, bound_dialog, time.time(), task_id),
            )
        return self.get(task_id)

    def quarantine(self, task_id, payload=None, error_code=None):
        if payload is None:
            payload = self._required(task_id)["payload"]
        return self.save(task_id, payload, status="quarantined", error_code=error_code)

    def retry(self, task_id):
        """Operator-only quarantine release; uncertain sends are NEVER released."""
        with self._transaction():
            task = self._required(task_id)
            if task["status"] != "quarantined" or task["dialog_id"] is not None:
                raise TaskStoreError("Only quarantined tasks without a send plan may be manually retried")
            payload = task["payload"]
            # Retain the inbound message and successful independent work; drop
            # malformed output/feedback, stale snapshots and poisoned cache flags.
            for key in ("body", "summaries", "validation_feedback", "feedback", "snapshot",
                        "malformed", "cache_invalid", "summary_failed", "quarantined"):
                payload.pop(key, None)
            payload["attempts"] = 0
            payload["invalid_failures"] = 0
            payload["network_failures"] = 0
            payload["next_attempt_at"] = 0
            self.connection.execute(
                "UPDATE tasks SET payload=?,status='pending',next_attempt_at=0,error_code=NULL,updated_at=? WHERE id=?",
                (_dump(payload), time.time(), task_id),
            )
        return self.get(task_id)

    def _finish(self, task_id):
        # Short-lived generated bodies/images are no longer needed after success.
        self.connection.execute(
            "UPDATE tasks SET payload='{}',status='done',next_attempt_at=0,error_code=NULL,updated_at=? WHERE id=?",
            (time.time(), task_id),
        )
        self.connection.execute(
            "UPDATE messages SET payload='{}' WHERE "
            "id=(SELECT message_row_id FROM tasks WHERE id=?) OR "
            "(message_id!='' AND (account,message_id) IN "
            "(SELECT m.account,m.message_id FROM messages m JOIN tasks t ON t.message_row_id=m.id WHERE t.id=?))",
            (task_id, task_id),
        )

    def finish(self, task_id):
        """Caller attests that the bound dialog was confirmed sent."""
        with self._transaction():
            self._required(task_id)
            self._finish(task_id)
        return self.get(task_id)

    def reconcile(self, dialog_records):
        """Use a COMPLETE dialog snapshot; absent sending plans are safe to requeue.

        The caller must save sending before preparing its dialog, so a missing
        dialog proves SMTP could not yet have started. Unknown/blocked_unknown
        remain blocked even if their dialog is missing.
        """
        if isinstance(dialog_records, dict):
            dialog_records = dialog_records["dialogs"]
        records = {}
        for record in dialog_records:
            identifier = _text(record.get("id", record.get("dialog_id")))
            status = record.get("status")
            if status not in {"sent", "prepared", "sending", "unknown"} or identifier in records:
                raise ValueError("Invalid or duplicate dialog metadata")
            records[identifier] = status
        counts = {"done": 0, "blocked_unknown": 0, "pending": 0}
        with self._transaction():
            tasks = self.connection.execute("SELECT id,status,dialog_id FROM tasks WHERE status!='done'").fetchall()
            for task in tasks:
                dialog_status = records.get(task["dialog_id"])
                if dialog_status == "sent":
                    self._finish(task["id"])
                    counts["done"] += 1
                elif dialog_status is not None or task["status"] in {"unknown", "blocked_unknown"}:
                    self.connection.execute(
                        "UPDATE tasks SET status='blocked_unknown',error_code='send_outcome_unknown',updated_at=? WHERE id=?",
                        (time.time(), task["id"]),
                    )
                    counts["blocked_unknown"] += 1
                elif task["status"] == "sending":
                    self.connection.execute(
                        "UPDATE tasks SET status='pending',dialog_id=NULL,error_code=NULL,next_attempt_at=0,updated_at=? WHERE id=?",
                        (time.time(), task["id"]),
                    )
                    counts["pending"] += 1
        return counts

    def set_control(self, key, value):
        with self._transaction():
            self.connection.execute(
                "INSERT INTO controls VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (_text(key), _dump(value)),
            )

    def get_control(self, key, default=None):
        row = self.connection.execute("SELECT value FROM controls WHERE key=?", (_text(key),)).fetchone()
        return default if row is None else _load(row["value"])

    def list_controls(self):
        return {row["key"]: _load(row["value"]) for row in self.connection.execute("SELECT * FROM controls ORDER BY key")}

    def delete_control(self, key):
        with self._transaction():
            self.connection.execute("DELETE FROM controls WHERE key=?", (_text(key),))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Local queue maintenance; payloads are never printed")
    parser.add_argument("--db", required=True, type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="list task metadata only")
    retry = commands.add_parser("retry", help="manually release a quarantined task only")
    retry.add_argument("id")
    commands.add_parser("paused", aliases=["services"], help="list paused control keys only")
    unpause = commands.add_parser("unpause", help="clear an explicitly chosen paused service control")
    unpause.add_argument("key")
    args = parser.parse_args(argv)
    # Maintenance must never silently create a second queue due to a typo.
    if not args.db.is_file():
        parser.error("Queue database does not exist")
    try:
        with TaskStore(args.db) as store:
            if args.command == "list":
                # Do not even decode task payloads for metadata inspection.
                fields = ("id", "kind", "status", "next_attempt_at", "error_code", "dialog_id", "created_at", "updated_at")
                rows = store.connection.execute("SELECT " + ",".join(fields) + " FROM tasks ORDER BY created_at,rowid")
                print(json.dumps([dict(row) for row in rows], ensure_ascii=True))
            elif args.command == "retry":
                result = store.retry(args.id)
                print(json.dumps({"id": result["id"], "status": result["status"]}))
            elif args.command in {"paused", "services"}:
                print(json.dumps(sorted(key for key, value in store.list_controls().items()
                                        if "paused" in key and value), ensure_ascii=True))
            else:
                if "paused" not in args.key:
                    raise TaskStoreError("Unpause requires a paused service control key")
                store.delete_control(args.key)
                print(json.dumps({"key": args.key, "paused": False}, ensure_ascii=True))
    except (TaskStoreError, ValueError, KeyError, sqlite3.Error, OSError):
        parser.exit(2, "Queue operation failed; no payload details are displayed.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
